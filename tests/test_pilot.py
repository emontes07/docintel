import hashlib
import json
import uuid

import pytest

from backend.core.docintel import ParsedDocument, ParsedParagraph
from backend.core.pilot_sources import RetrievedPDF, SourceReference
from backend.extract import run_enrichment, source_evidence
from backend.models.enrichment import AttributeDefinition, Candidate, ExtractionResponse, Manifest, OfflineBundle, OfflineSource, ProductKey
from backend.pilot import PARSER_VERSION, PilotConfig, PilotError, PilotService, ReviewInput, RunRequest, document_hash
from tests.test_pilot_sources import PDF


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(PDF)
    source = SourceReference(source_id="ford-local", kind="local", location=str(path), expected_sha256=hashlib.sha256(PDF).hexdigest())
    manifest = Manifest(product=ProductKey(item_id="SYNTHETIC-001", vendor="Synthetic Manufacturer", mpn="MODEL-001", hierarchy_node="synthetic-test"), attributes=[AttributeDefinition(attribute_id="Pressure Rating", description="Synthetic test", value_type="number", unit="PSI"), AttributeDefinition(attribute_id="Seal / Softgoods Material", description="Synthetic test", value_type="string")], source_ids=[source.source_id])
    document = ParsedDocument(source=str(path), cache_key="sha256:" + source.expected_sha256, parsed_at="2026-09-30T12:00:00Z", paragraphs=[ParsedParagraph(text="Synthetic MODEL-001", page_number=1), ParsedParagraph(text="Synthetic example: 42 PSI working pressure requirement of AWWA C800", page_number=1), ParsedParagraph(text="Synthetic inverted-key O-ring material: TEST-RUBBER", page_number=1)])
    supplied = OfflineSource(source_id=source.source_id, product=manifest.product, document=document)
    evidence = source_evidence(supplied, document.parsed_at)
    response = ExtractionResponse(candidates=[Candidate(attribute_id="Pressure Rating", value=42, unit="PSI", evidence_ids=[evidence[1].evidence_id]), Candidate(attribute_id="Seal / Softgoods Material", value="TEST-RUBBER", evidence_ids=[evidence[2].evidence_id])])
    result = run_enrichment(OfflineBundle(manifest=manifest, sources=[supplied], generated_response=response))
    payload = result.model_dump_json().encode()
    home = tmp_path / "private"
    home.mkdir()
    config = PilotConfig(manifest=manifest, sources=[source], replay_source_id=source.source_id, replay_sha256=hashlib.sha256(payload).hexdigest(), imported_artifacts={})
    (home / "config.json").write_text(config.model_dump_json())
    (home / "prior-result.json").write_bytes(payload)
    service = PilotService(home)
    service.cache_path(source).write_text(json.dumps({"parser_version": PARSER_VERSION, "origin": "synthetic_test", "document": document.model_dump(mode="json"), "document_sha256": document_hash(document)}))
    return service


def request(**overrides):
    return RunRequest(**(dict(source_id="ford-local", request_id=uuid.uuid4()) | overrides))


def test_private_scope_is_shared_by_startup_and_service(service):
    path = service.home / "config.json"
    configuration = json.loads(path.read_text())
    configuration["manifest"]["product"]["item_id"] = "SYNTHETIC-OTHER"
    path.write_text(json.dumps(configuration))
    with pytest.raises(ValueError):
        PilotConfig.from_private_home(service.home)
    scope = {"approved_product": configuration["manifest"]["product"], "document_identity_terms": ["Synthetic"]}
    (service.home / "approved-scope.json").write_text(json.dumps(scope))
    before = path.read_bytes()
    assert PilotConfig.from_private_home(service.home).manifest.product.item_id == "SYNTHETIC-OTHER"
    assert PilotService(service.home).config.manifest.product.item_id == "SYNTHETIC-OTHER"
    assert path.read_bytes() == before


def run(service, **overrides):
    run_id, created = service.begin(request(**overrides))
    assert created
    service.execute(run_id)
    return service.get(run_id)


def test_replay_qualifications_and_correction_preserve_machine(service):
    result = run(service)
    assert result["state"] == "completed"
    assert result["stages"]["inference"]["status"] == "replayed"
    assert result["parsing"]["status"] == "cached"
    assert service.catalog()["budget"]["inference_used"] == 0
    reviewed = service.review(result["id"], ReviewInput(attribute_id="Pressure Rating", decision="correct", reviewer="Local tester (unverified)", reason="Synthetic correction exercise", corrected_value=41, corrected_unit="PSI"))
    assert reviewed["machine_result"] == result["machine_result"]
    assert reviewed["machine_sha256"] == result["machine_sha256"]
    attribute = reviewed["reviewed_result"]["attributes"][0]
    assert attribute["candidates"][0]["value"] == 42
    assert attribute["review"]["corrected_value"] == 41
    assert "maximum" in attribute["review_annotations"][0]["text"]
    assert reviewed["schema_version"] == "docintel.review.v1"
    with pytest.raises(PilotError):
        service.review(result["id"], ReviewInput(attribute_id="Pressure Rating", decision="reject", reviewer="test", reason="duplicate"))


def test_duplicate_submission_across_service_instances(service):
    operation = request(mode="live_inference", confirm_live=True)
    run_id, created = service.begin(operation)
    assert created
    another = PilotService(service.home)
    assert another.begin(operation) == (run_id, False)
    with pytest.raises(PilotError):
        another.begin(request(mode="live_inference", confirm_live=True))
    with pytest.raises(ValueError):
        request(mode="live_inference")
    with pytest.raises(ValueError):
        request(parse_mode="fresh")


def test_completed_duplicate_returns_prior_run(service):
    result = run(service)
    assert service.begin(request()) == (result["id"], False)


def test_retrieval_failure_does_not_become_empty_evidence(service):
    run_id, _ = service.begin(request())
    service.execute(run_id, retrieve=lambda source: RetrievedPDF({"status": "failed", "failed_stage": "C_download", "error_code": "authentication_rejected"}))
    result = service.get(run_id)
    assert result["state"] == "failed"
    assert result["source"]["failed_stage"] == "C_download"
    assert result["machine_result"] is None
    assert result["stages"]["inference"]["status"] == "not_attempted"


@pytest.mark.parametrize("field,value", [("cache_key", "sha256:wrong"), ("source", "/other/location")])
def test_parse_cache_integrity(service, field, value):
    path = service.cache_path(service.config.sources[0])
    cached = json.loads(path.read_text())
    cached["document"][field] = value
    path.write_text(json.dumps(cached))
    result = run(service)
    assert result["stages"]["parsing"]["status"] == "failed"
    assert result["stages"]["inference"]["status"] == "not_attempted"


def test_missing_cache_replay_does_not_submit(service):
    service.cache_path(service.config.sources[0]).unlink()
    result = run(service)
    assert result["stages"]["parsing"]["status"] == "failed"
    assert service.catalog()["budget"]["analysis_used"] == 0


def test_live_abstention_and_no_retries(service):
    def completion(bundle, **options):
        assert options == {"execution_mode": "live_inference", "no_retries": True}
        class Abstain:
            def complete_structured(self, system, user, schema):
                assert "TEST-RUBBER" in user
                assert "review_annotations" not in user
                assert "generated_response" not in user
                return schema(candidates=[])
        return run_enrichment(bundle, execution_mode="live_inference", completion=Abstain())
    run_id, _ = service.begin(request(mode="live_inference", confirm_live=True))
    service.execute(run_id, enrichment=completion)
    result = service.get(run_id)
    assert result["state"] == "completed"
    assert all(attribute["status"] == "missing_evidence" for attribute in result["machine_result"]["attributes"])
    assert result["stages"]["validation"]["status"] == "passed"


def test_safe_provider_failure(service):
    def broken(source):
        raise RuntimeError("SECRET signed_url=hidden")
    run_id, _ = service.begin(request())
    service.execute(run_id, retrieve=broken)
    result = service.get(run_id)
    assert "SECRET" not in json.dumps(result)
    assert result["state"] == "failed"


def test_budget_is_persistent(service):
    run_id, _ = service.begin(request(mode="live_inference", confirm_live=True))
    for _ in range(3):
        service.reserve(run_id, "inference")
    with pytest.raises(PilotError):
        PilotService(service.home).reserve(run_id, "inference")


def test_unconfigured_reference_rejected(service):
    with pytest.raises(PilotError):
        service.begin(request(source_id="/etc/passwd"))
    service.config.sources[0].enabled = False
    with pytest.raises(PilotError):
        service.begin(request())


def test_missing_endpoint_is_recorded_without_inference(service, monkeypatch):
    from backend.core.config import settings
    monkeypatch.setattr(settings, "LLM_ENDPOINT", None)
    monkeypatch.setattr(settings, "AI_FOUNDRY_ENDPOINT", "")
    result = run(service, mode="live_inference", confirm_live=True)
    assert result["state"] == "failed"
    assert result["stages"]["inference"]["status"] == "failed"
    assert service.catalog()["budget"]["inference_used"] == 0


def test_live_positive_and_fresh_parse(service):
    cached = json.loads(service.cache_path(service.config.sources[0]).read_text())
    class Parser:
        def extract_pdf_bytes(self, content, *, source):
            assert content == PDF
            assert source == service.config.sources[0].location
            return ParsedDocument.model_validate(cached["document"])
    def completion(bundle, **options):
        class Positive:
            def complete_structured(self, system, user, schema):
                evidence = json.loads(user)["evidence"]
                return schema(candidates=[Candidate(attribute_id="Pressure Rating", value=42, unit="PSI", evidence_ids=[evidence[1]["evidence_id"]])])
        return run_enrichment(bundle, execution_mode="live_inference", completion=Positive())
    run_id, _ = service.begin(request(mode="live_inference", confirm_live=True, parse_mode="fresh"))
    service.execute(run_id, parser=Parser(), enrichment=completion)
    result = service.get(run_id)
    assert result["state"] == "completed"
    assert result["parsing"]["status"] == "live"
    assert result["machine_result"]["attributes"][0]["candidates"][0]["value"] == 42
    assert (service.home / f"parsed-{run_id}.json").exists()
    assert service.catalog()["budget"]["analysis_used"] == 1


def test_product_mismatch_prevents_inference(service):
    path = service.cache_path(service.config.sources[0])
    cached = json.loads(path.read_text())
    document = ParsedDocument.model_validate(cached["document"])
    document.paragraphs[0].text = "Different product"
    cached.update(document=document.model_dump(mode="json"), document_sha256=document_hash(document))
    path.write_text(json.dumps(cached))
    result = run(service)
    assert result["state"] == "failed"
    assert result["stages"]["inference"]["status"] == "not_attempted"


def test_changed_replay_artifact_prevents_replay(service):
    with (service.home / "prior-result.json").open("a") as handle:
        handle.write(" ")
    result = run(service)
    assert result["state"] == "failed"
    assert result["stages"]["inference"]["status"] == "failed"


def test_explicit_interruption_keeps_budget_and_does_not_retry(service):
    run_id, _ = service.begin(request(mode="live_inference", confirm_live=True))
    service.reserve(run_id, "inference")
    service.abandon(run_id)
    service.execute(run_id)
    assert service.get(run_id)["state"] == "failed"
    assert service.catalog()["budget"]["inference_used"] == 1
    with pytest.raises(PilotError):
        service.abandon(run_id)