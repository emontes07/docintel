"""Offline enrichment contracts and execution tests. No customer fixtures."""

import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest


def live_bundle(bundle):
    from backend.models.enrichment import LiveBundle

    return LiveBundle.model_validate({
        **bundle.model_dump(exclude={"execution_mode", "generated_response"}),
        "execution_mode": "live_inference",
    })


def run_live(bundle, **kwargs):
    from backend.extract import run_enrichment

    return run_enrichment(live_bundle(bundle), execution_mode="live_inference", **kwargs)


def test_import_does_not_request_blob_delegation_key():
    probe = """
from azure.storage.blob import BlobServiceClient
from unittest.mock import patch
with patch.object(BlobServiceClient, 'get_user_delegation_key') as request:
    import backend.core
    request.assert_not_called()
"""
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Network access is forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def bundle():
    from backend.models.enrichment import OfflineBundle

    return OfflineBundle.model_validate({
        "execution_mode": "offline_replay",
        "manifest": {
            "product": {"item_id": "sample-1", "vendor": "Synthetic", "mpn": "TEST-1", "hierarchy_node": "test-valves"},
            "attributes": [{"attribute_id": "material", "description": "Body material", "value_type": "string", "examples": ["EXAMPLE-NOT-EVIDENCE"]}],
            "source_ids": ["technical-pdf"],
        },
        "sources": [{
            "source_id": "technical-pdf",
            "product": {"item_id": "sample-1", "vendor": "Synthetic", "mpn": "TEST-1", "hierarchy_node": "test-valves"},
            "document": {
                "source": "fixtures/synthetic.pdf", "cache_key": "fixture-v1",
                "parsed_at": "2026-01-01T00:00:00Z",
                "paragraphs": [{"text": "Body material: brass.", "page_number": 2}],
            },
        }],
        "generated_response": {"candidates": [{
            "attribute_id": "material", "value": "brass",
            "evidence_ids": ["technical-pdf:fixture-v1:paragraph=0"],
        }]},
    })


def test_review_annotations_survive_review_copy_and_json_export(bundle):
    from backend.extract import apply_reviews, run_offline
    from backend.models.enrichment import EnrichmentResult, ExtractionResponse, ReviewAnnotation

    original = run_offline(bundle)
    annotated = original.model_copy(deep=True)
    annotation = ReviewAnnotation(
        candidate_index=0,
        text="Supported for the cited component only, not the entire product.",
        author="Test reviewer",
        annotated_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    annotated.attributes[0].review_annotations.append(annotation)
    exported = apply_reviews(annotated, []).model_dump_json(exclude_none=True)
    restored = EnrichmentResult.model_validate_json(exported)
    assert restored.attributes[0].review_annotations == [annotation]
    assert restored.attributes[0].status == "proposed"
    assert restored.attributes[0].review is None
    assert restored.attributes[0].candidates == original.attributes[0].candidates
    assert original.attributes[0].review_annotations == []
    assert '"origin":"review_annotation"' in exported
    assert "review_annotations" not in str(ExtractionResponse.model_json_schema())


def test_legacy_result_without_annotations_still_loads(bundle):
    from backend.extract import run_offline
    from backend.models.enrichment import EnrichmentResult

    payload = run_offline(bundle).model_dump()
    for attribute in payload["attributes"]:
        attribute.pop("review_annotations")
    restored = EnrichmentResult.model_validate(payload)
    assert all(attribute.review_annotations == [] for attribute in restored.attributes)


def test_review_annotation_rejects_nonexistent_candidate(bundle):
    from backend.extract import run_offline
    from backend.models.enrichment import EnrichmentResult

    payload = run_offline(bundle).model_dump()
    payload["attributes"][0]["review_annotations"] = [{
        "candidate_index": 1, "text": "Component-specific support only.",
        "author": "Test reviewer", "annotated_at": "2026-09-30T00:00:00Z",
    }]
    with pytest.raises(ValueError, match="existing candidate"):
        EnrichmentResult.model_validate(payload)


def test_offline_proposal_preserves_evidence_and_time(bundle):
    from backend.extract import run_offline

    observed = datetime(2026, 9, 29, tzinfo=timezone.utc)
    result = run_offline(bundle, observed_at=observed)
    assert result.attributes[0].status == "proposed"
    assert result.attributes[0].review is None
    evidence = result.evidence[0]
    assert evidence.evidence_id == "technical-pdf:fixture-v1:paragraph=0"
    assert evidence.source_locator == "fixtures/synthetic.pdf#page=2&paragraph=0"
    assert evidence.text == bundle.sources[0].document.paragraphs[0].text
    assert evidence.observed_at == observed
    assert evidence.provider_retrieved_at is None
    assert evidence.source_published_at is None
    assert [outcome.status for outcome in result.retrieval[1:]] == ["not_attempted"] * 3


@pytest.mark.parametrize("field", ["item_id", "vendor", "mpn", "hierarchy_node"])
def test_exact_product_filter(bundle, field):
    from backend.extract import run_offline

    setattr(bundle.sources[0].product, field, "different")
    completion = Mock()
    result = run_live(bundle, completion=completion)
    assert result.evidence == []
    assert result.retrieval[0].error_code == "product_mismatch"
    assert result.attributes[0].status == "retrieval_failed"
    completion.complete_structured.assert_not_called()


def test_examples_without_evidence_do_not_produce_values(bundle):
    from backend.extract import run_offline

    bundle.sources[0].document.paragraphs = []
    result = run_offline(bundle)
    assert result.attributes[0].status == "missing_evidence"
    assert result.attributes[0].candidates == []
    assert result.retrieval[0].status == "no_evidence"


def test_retrieval_error_is_not_missing_evidence(bundle):
    from backend.extract import run_offline

    bundle.sources[0].document = None
    bundle.sources[0].error_code = "access_denied"
    result = run_offline(bundle)
    assert result.retrieval[0].status == "failed"
    assert result.attributes[0].status == "retrieval_failed"


@pytest.mark.parametrize("change", ["unknown_citation", "wrong_type", "wrong_unit"])
def test_invalid_candidates_fail_closed(bundle, change):
    from backend.extract import run_offline

    candidate = bundle.generated_response.candidates[0]
    if change == "unknown_citation":
        candidate.evidence_ids = ["invented"]
    elif change == "wrong_type":
        candidate.value = True
    else:
        candidate.unit = "kg"
    result = run_offline(bundle)
    assert result.extraction_error == "invalid_response"
    assert result.attributes[0].status == "extraction_failed"
    assert result.attributes[0].candidates == []


def test_generated_answer_cannot_be_used_as_excerpt(bundle):
    from backend.extract import source_evidence, validate_response

    evidence = source_evidence(bundle.sources[0], datetime.now(timezone.utc))
    evidence[0].content_kind = "generated_answer"
    with pytest.raises(ValueError, match="source excerpt"):
        validate_response(bundle.generated_response, bundle.manifest, evidence)


def test_review_preserves_conflicting_machine_proposals(bundle):
    from backend.extract import apply_reviews, run_offline
    from backend.core.docintel import ParsedParagraph
    from backend.models.enrichment import EnrichmentResult, ReviewDecision

    bundle.sources[0].document.paragraphs.append(ParsedParagraph(text="Body material: steel.", page_number=3))
    alternate = bundle.generated_response.candidates[0].model_copy(update={
        "value": "steel", "evidence_ids": ["technical-pdf:fixture-v1:paragraph=1"],
    })
    bundle.generated_response.candidates.append(alternate)
    result = run_offline(bundle)
    assert result.attributes[0].status == "conflict"
    review = ReviewDecision(
        attribute_id="material", decision="correct", corrected_value="bronze",
        reviewer="synthetic-reviewer", reviewed_at=datetime.now(timezone.utc), reason="Synthetic review test",
    )
    reviewed = apply_reviews(result, [review])
    restored = EnrichmentResult.model_validate_json(reviewed.model_dump_json())
    assert restored.attributes[0].candidates == result.attributes[0].candidates
    assert restored.attributes[0].review == review
    assert result.attributes[0].review is None


def test_existing_values_are_preserved(bundle):
    from backend.extract import run_offline

    bundle.manifest.existing_values = {"material": "existing-value"}
    completion = Mock()
    result = run_live(bundle, completion=completion)
    assert result.manifest.existing_values == bundle.manifest.existing_values
    assert result.attributes[0].status == "existing"
    completion.complete_structured.assert_not_called()


def test_selected_web_provider_never_becomes_success(bundle):
    from backend.core.websearch import ProviderUnavailableError
    from backend.extract import run_offline

    with pytest.raises(ProviderUnavailableError):
        run_offline(bundle, web_provider="webiq")


def test_unrelated_value_is_not_supported_by_valid_citation(bundle):
    from backend.extract import run_offline

    bundle.generated_response.candidates[0].value = "EXAMPLE-NOT-EVIDENCE"
    assert run_offline(bundle).extraction_error == "invalid_response"


def test_provider_times_remain_distinct_and_evidence_ids_stable(bundle):
    from backend.extract import run_offline

    source = bundle.sources[0]
    source.provider_retrieved_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    source.source_published_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    first = run_offline(bundle, observed_at=datetime(2026, 3, 1, tzinfo=timezone.utc)).evidence[0]
    second = run_offline(bundle, observed_at=datetime(2026, 4, 1, tzinfo=timezone.utc)).evidence[0]
    assert first.evidence_id == second.evidence_id
    assert first.observed_at != second.observed_at
    assert first.provider_retrieved_at == second.provider_retrieved_at == source.provider_retrieved_at
    assert first.source_published_at == source.source_published_at


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_approval_and_rejection_do_not_overwrite_candidates(bundle, decision):
    from backend.extract import apply_reviews, run_offline
    from backend.models.enrichment import ReviewDecision

    original = run_offline(bundle)
    review = ReviewDecision(
        attribute_id="material", decision=decision, candidate_index=0 if decision == "approve" else None,
        reviewer="reviewer", reviewed_at=datetime.now(timezone.utc), reason="Synthetic test",
    )
    reviewed = apply_reviews(original, [review])
    assert reviewed.attributes[0].candidates == original.attributes[0].candidates
    assert reviewed.attributes[0].review.decision == decision


def test_unknown_review_candidate_is_rejected(bundle):
    from backend.extract import apply_reviews, run_offline
    from backend.models.enrichment import ReviewDecision

    review = ReviewDecision(
        attribute_id="material", decision="approve", candidate_index=10,
        reviewer="reviewer", reviewed_at=datetime.now(timezone.utc), reason="Synthetic test",
    )
    with pytest.raises(ValueError, match="existing candidate"):
        apply_reviews(run_offline(bundle), [review])


@pytest.mark.parametrize("case", ["positive", "unsupported"])
def test_saved_synthetic_live_contracts_are_offline(case, model_boundary):
    import json
    from pathlib import Path
    from backend.extract import run_enrichment, validate_response
    from backend.models.enrichment import ExtractionResponse, LiveBundle

    fixtures = Path(__file__).parent / "fixtures" / "enrichment"
    payload = json.loads((fixtures / f"{case}-live.json").read_text())
    expected = json.loads((fixtures / f"{case}-expected.json").read_text())
    assert "generated_response" not in payload
    bundle = LiveBundle.model_validate(payload)
    response = ExtractionResponse.model_validate({"candidates": expected["candidates"]})
    client, create, respond = model_boundary
    respond(response)

    result = run_enrichment(bundle, execution_mode="live_inference", completion=client)

    create.assert_called_once()
    assert result.model_call_status == "succeeded"
    assert result.extraction_error is None
    assert [candidate.model_dump() for attribute in result.attributes for candidate in attribute.candidates] == expected["candidates"]
    assert result.evidence[0].model_dump(include=set(expected["evidence"])) == expected["evidence"]
    assert all(attribute.review is None for attribute in result.attributes)
    validate_response(response, result.manifest, result.evidence)
    if case == "unsupported":
        positive = json.loads((fixtures / "positive-expected.json").read_text())
        assert positive["candidates"][0]["value"] not in create.call_args.kwargs["messages"][1]["content"]


def test_missing_candidates_is_not_defaulted_to_abstention():
    from pydantic import ValidationError
    from backend.core.llm import _parse
    from backend.models.enrichment import ExtractionResponse

    parsed = _parse(ExtractionResponse, '{}')

    assert isinstance(parsed, ValidationError)
    assert parsed.errors()[0]["loc"] == ("candidates",)
    assert parsed.errors()[0]["type"] == "missing"


def test_explicit_empty_candidates_remains_valid(bundle, model_boundary):
    from backend.core.llm import _parse
    from backend.models.enrichment import ExtractionResponse

    parsed = _parse(ExtractionResponse, '{"candidates":[]}')
    assert isinstance(parsed, ExtractionResponse)
    client, create, respond = model_boundary
    respond(parsed)

    result = run_live(bundle, completion=client)

    create.assert_called_once()
    assert result.model_call_status == "succeeded"
    assert result.extraction_error is None
    assert result.attributes[0].candidates == []
    assert result.attributes[0].review is None


def test_llm_structured_wrapper_used_without_network(bundle, monkeypatch):
    import json
    from types import SimpleNamespace
    from backend.core.instructions import product_extraction_system_message
    from backend.core.llm import LLMClient
    from backend.extract import run_offline

    client = LLMClient(endpoint="https://example.test", deployment="test", token_provider=lambda: "test")
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=bundle.generated_response.model_dump_json(),
    ))])
    create = Mock(return_value=response)
    monkeypatch.setattr(client.sync_client.chat.completions, "create", create)
    result = run_live(bundle, completion=client)
    assert result.attributes[0].status == "proposed"
    call = create.call_args.kwargs
    assert call["response_format"]["json_schema"]["strict"] is True
    assert "EXAMPLE-NOT-EVIDENCE" not in call["messages"][1]["content"]
    assert call["messages"][0]["content"] == product_extraction_system_message
    prompt = json.loads(call["messages"][1]["content"])
    assert prompt["product"] == bundle.sources[0].product.model_dump()
    assert prompt["attributes"][0]["attribute_id"] == "material"
    assert prompt["attributes"][0]["description"] == "Body material"
    assert prompt["evidence"][0]["text"] == bundle.sources[0].document.paragraphs[0].text
    assert prompt["evidence"][0]["source_id"] == bundle.sources[0].source_id
    assert prompt["evidence"][0]["evidence_id"] == "technical-pdf:fixture-v1:paragraph=0"
    assert prompt["evidence"][0]["content_kind"] == "source_excerpt"
    schema = call["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["candidates"]
    assert set(schema["$defs"]["Candidate"]["required"]) == {
        "attribute_id", "value", "unit", "evidence_ids", "origin",
    }


@pytest.fixture
def model_boundary(monkeypatch):
    from types import SimpleNamespace
    from backend.core.llm import LLMClient

    client = LLMClient(endpoint="https://example.test", deployment="test", token_provider=lambda: "test")
    create = Mock()
    monkeypatch.setattr(client.sync_client.chat.completions, "create", create)

    def respond(payload):
        create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=payload.model_dump_json(),
        ))])

    return client, create, respond


def test_real_client_candidates_replace_replay_and_preserve_conflicts(bundle, model_boundary):
    import json
    from backend.core.docintel import ParsedParagraph
    from backend.extract import run_offline
    from backend.models.enrichment import AttributeDefinition, ExtractionResponse

    client, create, respond = model_boundary
    candidates = bundle.generated_response.model_copy(deep=True)
    bundle.sources[0].document.paragraphs.append(ParsedParagraph(text="Body material: steel.", page_number=3))
    candidates.candidates.append(candidates.candidates[0].model_copy(update={
        "value": "steel", "evidence_ids": ["technical-pdf:fixture-v1:paragraph=1"],
    }))
    bundle.generated_response = ExtractionResponse(candidates=[])
    bundle.manifest.attributes.append(AttributeDefinition(
        attribute_id="retained", description="Existing value", value_type="string",
    ))
    bundle.manifest.existing_values = {"retained": "unchanged"}
    unselected = bundle.sources[0].model_copy(deep=True)
    unselected.source_id = "unselected"
    unselected.document.paragraphs = [ParsedParagraph(text="UNSELECTED-NOT-EVIDENCE")]
    bundle.sources.append(unselected)
    respond(candidates)

    result = run_live(bundle, completion=client)

    create.assert_called_once()
    request = create.call_args.kwargs
    assert request["model"] == client.deployment
    assert request["response_format"]["json_schema"]["name"] == "ExtractionResponse"
    assert request["response_format"]["json_schema"]["strict"] is True
    prompt = json.loads(request["messages"][1]["content"])
    assert prompt["product"] == bundle.manifest.product.model_dump()
    assert [attribute["attribute_id"] for attribute in prompt["attributes"]] == ["material"]
    assert all("examples" not in attribute for attribute in prompt["attributes"])
    assert {item["source_id"] for item in prompt["evidence"]} == {"technical-pdf"}
    assert "UNSELECTED-NOT-EVIDENCE" not in request["messages"][1]["content"]
    assert result.attributes[0].status == "conflict"
    assert result.attributes[0].candidates == candidates.candidates
    assert all(attribute.review is None for attribute in result.attributes)
    assert result.attributes[1].status == "existing"
    assert result.manifest.existing_values == {"retained": "unchanged"}
    assert [outcome.status for outcome in result.retrieval[1:]] == ["not_attempted"] * 3


@pytest.mark.parametrize("failure", ["unknown_citation", "unsupported_value", "wrong_unit", "existing_attribute", "transport"])
def test_real_client_failure_never_falls_back_to_replay(bundle, model_boundary, failure):
    from backend.extract import run_offline
    from backend.models.enrichment import AttributeDefinition

    client, create, respond = model_boundary
    response = bundle.generated_response.model_copy(deep=True)
    candidate = response.candidates[0]
    if failure == "unknown_citation":
        candidate.evidence_ids = ["invented"]
    elif failure == "unsupported_value":
        candidate.value = "EXAMPLE-NOT-EVIDENCE"
    elif failure == "wrong_unit":
        candidate.unit = "kg"
    elif failure == "existing_attribute":
        bundle.manifest.attributes.append(AttributeDefinition(
            attribute_id="retained", description="Existing value", value_type="string",
        ))
        bundle.manifest.existing_values = {"retained": "unchanged"}
        candidate.attribute_id = "retained"
    respond(response)
    if failure == "transport":
        create.side_effect = RuntimeError("SENSITIVE-ERROR-NOT-FOR-OUTPUT")

    result = run_live(bundle, completion=client)

    create.assert_called_once()
    assert result.extraction_error == ("model_failed" if failure == "transport" else "invalid_response")
    assert result.attributes[0].status == "extraction_failed"
    assert result.attributes[0].candidates == []
    assert result.attributes[0].review is None
    assert "SENSITIVE-ERROR-NOT-FOR-OUTPUT" not in result.model_dump_json()
    assert bundle.generated_response.candidates[0].value == "brass"


@pytest.mark.parametrize("skip", ["no_evidence", "product_mismatch", "all_existing"])
def test_real_client_is_not_called_without_eligible_work(bundle, model_boundary, skip):
    from backend.extract import run_offline

    client, create, respond = model_boundary
    if skip == "no_evidence":
        bundle.sources[0].document.paragraphs = []
    elif skip == "product_mismatch":
        bundle.sources[0].product.item_id = "different"
    else:
        bundle.manifest.existing_values = {"material": "unchanged"}
    run_live(bundle, completion=client)
    create.assert_not_called()


def test_replay_default_does_not_call_real_client(bundle, monkeypatch):
    from backend.core.llm import LLMClient
    from backend.extract import run_offline

    complete = Mock(side_effect=AssertionError("Replay must not call the real client"))
    monkeypatch.setattr(LLMClient, "complete_structured", complete)
    assert run_offline(bundle).attributes[0].status == "proposed"
    complete.assert_not_called()


def test_real_client_cannot_enable_web_retrieval(bundle, model_boundary):
    from backend.core.websearch import ProviderUnavailableError
    from backend.extract import run_offline

    client, create, respond = model_boundary
    with pytest.raises(ProviderUnavailableError):
        run_live(bundle, completion=client, web_provider="webiq")
    create.assert_not_called()


@pytest.mark.parametrize("failure", [ValueError("invalid fixture"), RuntimeError("synthetic transport failure")])
def test_model_errors_are_not_missing_evidence(bundle, failure):
    from backend.extract import run_offline

    completion = Mock()
    completion.complete_structured.side_effect = failure
    result = run_live(bundle, completion=completion)
    assert result.attributes[0].status == "extraction_failed"
    assert result.extraction_error is not None
    assert result.retrieval[0].status == "success"


def test_cache_hit_and_etag_change(bundle, monkeypatch):
    from types import SimpleNamespace
    from backend.core.docintel import DocumentIntelligenceService

    service = DocumentIntelligenceService(endpoint="https://example.test")
    blob_service = Mock()
    blob = blob_service.get_blob_client.return_value
    blob.get_blob_properties.return_value = SimpleNamespace(etag='"version-one"')
    monkeypatch.setattr(service, "_get_blob_service", lambda: blob_service)
    cached = bundle.sources[0].document
    first_key = service._cache_key("fixtures", "synthetic.pdf", "version-one")
    read_cache = Mock(side_effect=lambda key: cached if key == first_key else None)
    analyze = Mock(return_value=SimpleNamespace(paragraphs=[], tables=[], content=""))
    monkeypatch.setattr(service, "_read_cache", read_cache)
    monkeypatch.setattr(service, "_analyze", analyze)
    write = Mock()
    monkeypatch.setattr(service, "_write_cache", write)
    assert service.extract_document("fixtures/synthetic.pdf") == cached
    analyze.assert_not_called()
    blob.download_blob.assert_not_called()
    blob.get_blob_properties.return_value = SimpleNamespace(etag='"version-two"')
    parsed = service.extract_document("fixtures/synthetic.pdf")
    assert parsed.cache_key != first_key
    analyze.assert_called_once()
    write.assert_called_once()


def test_table_cell_locators_and_unknown_pages(bundle):
    from backend.core.docintel import ParsedTable
    from backend.extract import run_offline

    bundle.sources[0].document.paragraphs[0].page_number = None
    bundle.sources[0].document.tables = [ParsedTable(page_number=4, row_count=1, column_count=2, cells=[["Material", "brass"]])]
    result = run_offline(bundle)
    assert "page=" not in result.evidence[0].source_locator
    assert result.evidence[-1].source_locator.endswith("#page=4&table=0&row=0&column=1")
    assert result.evidence[-1].evidence_id.endswith("table=0&row=0&column=1")


def test_unselected_sources_do_not_supply_evidence(bundle):
    from backend.extract import run_offline

    extra = bundle.sources[0].model_copy(deep=True)
    extra.source_id = "not-selected"
    extra.document.paragraphs[0].text = "Material: unrelated"
    bundle.sources.append(extra)
    assert {item.source_id for item in run_offline(bundle).evidence} == {"technical-pdf"}


def test_missing_fixture_is_a_failure(bundle):
    from backend.extract import run_offline

    bundle.sources = []
    result = run_offline(bundle)
    assert result.retrieval[0].error_code == "fixture_missing"
    assert result.attributes[0].status == "retrieval_failed"


def test_partial_failure_remains_visible_with_a_proposal(bundle):
    from backend.extract import run_offline

    bundle.manifest.source_ids.append("missing")
    result = run_offline(bundle)
    assert result.attributes[0].status == "proposed"
    assert result.retrieval[1].status == "failed"


def test_raw_provider_payload_is_not_a_contract_field(bundle):
    from pydantic import ValidationError
    from backend.models.enrichment import Evidence
    from backend.extract import run_offline

    payload = run_offline(bundle).evidence[0].model_dump()
    payload["raw_provider_payload"] = {"body": "not for storage"}
    with pytest.raises(ValidationError):
        Evidence.model_validate(payload)


def test_units_require_source_support(bundle):
    from backend.extract import run_offline

    bundle.manifest.attributes[0].unit = "kg"
    bundle.generated_response.candidates[0].unit = "kg"
    assert run_offline(bundle).extraction_error == "invalid_response"


def run_cli(*arguments):
    probe = """
import socket
def blocked(*args, **kwargs):
    raise AssertionError('Network forbidden in offline CLI test')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
from backend.extract import main
raise SystemExit(main())
"""
    return subprocess.run([sys.executable, "-c", probe, *map(str, arguments)], capture_output=True, text=True)


def test_cli_synthetic_sample(tmp_path):
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "enrichment" / "synthetic.json"
    output = tmp_path / "result.json"
    command = run_cli(fixture, "--output", output)
    assert command.returncode == 0, command.stderr
    result = json.loads(output.read_text())
    assert result["execution_mode"] == "offline_replay"
    assert [attribute["status"] for attribute in result["attributes"]] == ["proposed", "missing_evidence"]
    assert "review" not in result["attributes"][0]
    assert "provider_retrieved_at" not in result["evidence"][0]
    assert "source_published_at" not in result["evidence"][0]
    assert run_cli(fixture, "--output", output).returncode == 2


def test_cli_webiq_fails_without_writing_output(tmp_path):
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "enrichment" / "synthetic.json"
    output = tmp_path / "result.json"
    command = run_cli(fixture, "--output", output, "--web-provider", "webiq")
    assert command.returncode == 2
    assert "disabled" in command.stderr
    assert not output.exists()


def test_cli_reviews_and_retrieval_failure_exit_code(bundle, tmp_path):
    import json

    fixture = tmp_path / "fixture.json"
    fixture.write_text(bundle.model_dump_json())
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps([{
        "attribute_id": "material", "decision": "approve", "candidate_index": 0,
        "reviewer": "synthetic-reviewer", "reviewed_at": "2026-09-29T00:00:00Z", "reason": "Synthetic test",
    }]))
    output = tmp_path / "reviewed.json"
    assert run_cli(fixture, "--output", output, "--reviews", reviews).returncode == 0
    assert json.loads(output.read_text())["attributes"][0]["review"]["decision"] == "approve"
    bundle.sources = []
    fixture.write_text(bundle.model_dump_json())
    failed_output = tmp_path / "failed.json"
    assert run_cli(fixture, "--output", failed_output).returncode == 1
    assert json.loads(failed_output.read_text())["retrieval"][0]["status"] == "failed"


def test_replay_import_initializes_neither_credentials_nor_clients():
    probe = """
import socket
from unittest.mock import patch
def blocked(*args, **kwargs):
    raise AssertionError('Network forbidden')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
with patch('azure.identity.DefaultAzureCredential') as credential, patch('openai.AzureOpenAI') as sync, patch('openai.AsyncAzureOpenAI') as asynchronous:
    from backend.extract import run_offline
    from backend.models.enrichment import OfflineBundle
    from pathlib import Path
    result = run_offline(OfflineBundle.model_validate_json(Path('tests/fixtures/enrichment/synthetic.json').read_text()))
    assert result.model_call_status == 'not_attempted'
    credential.assert_not_called()
    sync.assert_not_called()
    asynchronous.assert_not_called()
"""
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True)


@pytest.mark.parametrize("combination", ["offline_client", "live_without_opt_in", "live_with_replay", "offline_with_live_mode", "unknown_mode"])
def test_programmatic_mode_contradictions_fail_before_call(bundle, combination):
    from backend.extract import ExecutionConfigurationError, ReplayCompletion, run_enrichment, run_offline

    completion = Mock()
    with pytest.raises(ExecutionConfigurationError):
        if combination == "offline_client":
            run_offline(bundle, completion=completion)
        elif combination == "live_without_opt_in":
            run_enrichment(live_bundle(bundle), completion=completion)
        elif combination == "live_with_replay":
            run_enrichment(live_bundle(bundle), execution_mode="live_inference", completion=ReplayCompletion(bundle.generated_response))
        elif combination == "offline_with_live_mode":
            run_enrichment(bundle, execution_mode="live_inference", completion=completion)
        else:
            run_enrichment(bundle, execution_mode="unknown", completion=completion)
    completion.complete_structured.assert_not_called()


@pytest.mark.parametrize("case", ["replay_missing_response", "replay_null_response", "live_dummy_response", "live_null_response", "live_missing_mode"])
def test_input_contracts_reject_missing_or_contradictory_fields(bundle, case):
    from pydantic import ValidationError
    from backend.models.enrichment import LiveBundle, OfflineBundle

    payload = bundle.model_dump()
    if case.startswith("replay"):
        if case == "replay_missing_response":
            del payload["generated_response"]
        else:
            payload["generated_response"] = None
        schema = OfflineBundle
    else:
        payload = live_bundle(bundle).model_dump()
        if case == "live_missing_mode":
            del payload["execution_mode"]
        else:
            payload["generated_response"] = None if case == "live_null_response" else {"candidates": []}
        schema = LiveBundle
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


@pytest.mark.parametrize("outcome", ["success", "empty_response", "invalid_evidence", "schema_failure", "transport_failure", "initialization_failure", "no_evidence", "all_existing"])
def test_live_cli_exports_actual_execution(bundle, model_boundary, monkeypatch, tmp_path, outcome):
    import json
    from backend import extract
    from backend.core.config import settings
    from backend.core.llm import LLMSchemaValidationError
    from backend.models.enrichment import EnrichmentResult, ExtractionResponse

    client, create, respond = model_boundary
    payload = live_bundle(bundle)
    response = bundle.generated_response.model_copy(deep=True)
    if outcome == "empty_response":
        response = ExtractionResponse(candidates=[])
    elif outcome == "invalid_evidence":
        response.candidates[0].evidence_ids = ["invented"]
    elif outcome == "no_evidence":
        payload.sources[0].document.paragraphs = []
    elif outcome == "all_existing":
        payload.manifest.existing_values = {"material": "unchanged"}
    respond(response)
    factory = Mock(return_value=client)
    monkeypatch.setattr(extract, "LLMClient", factory)
    monkeypatch.setattr(settings, "AI_FOUNDRY_ENDPOINT", "https://example.test")
    monkeypatch.setattr(settings, "LLM_DEPLOYMENT", "test")
    if outcome == "transport_failure":
        create.side_effect = RuntimeError("SENSITIVE-FAILURE")
    elif outcome == "schema_failure":
        create.side_effect = LLMSchemaValidationError(ExtractionResponse, "SENSITIVE-FAILURE", [])
    elif outcome == "initialization_failure":
        factory.side_effect = RuntimeError("SENSITIVE-FAILURE")
    input_path = tmp_path / "input.json"
    input_path.write_text(payload.model_dump_json())
    output_path = tmp_path / "output.json"
    monkeypatch.setattr(sys, "argv", ["extract", str(input_path), "--output", str(output_path), "--live"])

    exit_code = extract.main()

    stored = json.loads(output_path.read_text())
    restored = EnrichmentResult.model_validate(stored)
    assert stored["execution_mode"] == restored.execution_mode == "live_inference"
    assert stored["candidate_source"] == "llm"
    assert all(attribute.review is None for attribute in restored.attributes)
    assert all(outcome.status == "not_attempted" for outcome in restored.retrieval[1:])
    assert "SENSITIVE-FAILURE" not in output_path.read_text()
    if outcome in ("no_evidence", "all_existing"):
        assert stored["model_call_status"] == "skipped"
        assert stored["skip_reason"] == ("no_eligible_evidence" if outcome == "no_evidence" else "no_missing_attributes")
        factory.assert_not_called()
        create.assert_not_called()
        assert exit_code == 0
    elif outcome == "initialization_failure":
        assert stored["model_call_status"] == "not_attempted"
        assert stored["extraction_error"] == "model_failed"
        create.assert_not_called()
        assert exit_code == 1
    else:
        factory.assert_called_once_with()
        create.assert_called_once()
        assert stored["model_call_status"] == ("failed" if outcome in ("transport_failure", "schema_failure") else "succeeded")
        assert "skip_reason" not in stored
        if outcome in ("success", "empty_response"):
            assert exit_code == 0
            assert "extraction_error" not in stored
            assert restored.attributes[0].status == ("proposed" if outcome == "success" else "missing_evidence")
        else:
            assert exit_code == 1
            assert restored.attributes[0].candidates == []
            assert stored["extraction_error"] == ("model_failed" if outcome == "transport_failure" else "invalid_response")


@pytest.mark.parametrize("case", ["replay_default", "live_without_flag", "replay_with_flag", "live_missing_mode", "missing_config", "web_requested", "existing_output"])
def test_cli_mode_gates_do_not_initialize_client(bundle, monkeypatch, tmp_path, capsys, case):
    import json
    from backend import extract
    from backend.core.config import settings

    factory = Mock(side_effect=AssertionError("Client initialization forbidden"))
    monkeypatch.setattr(extract, "LLMClient", factory)
    payload = bundle.model_dump() if case in ("replay_default", "replay_with_flag") else live_bundle(bundle).model_dump()
    if case in ("live_missing_mode", "replay_default"):
        del payload["execution_mode"]
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload, default=str))
    output = tmp_path / "output.json"
    args = ["extract", str(input_path), "--output", str(output)]
    if case not in ("replay_default", "live_without_flag"):
        args.append("--live")
    if case == "web_requested":
        args.extend(["--web-provider", "webiq"])
    if case == "missing_config":
        monkeypatch.setattr(settings, "LLM_ENDPOINT", None)
        monkeypatch.setattr(settings, "AI_FOUNDRY_ENDPOINT", None)
        monkeypatch.setattr(settings, "LLM_DEPLOYMENT", None)
    if case == "existing_output":
        output.write_text("preserved")
    monkeypatch.setattr(sys, "argv", args)
    if case == "replay_default":
        assert extract.main() == 0
        stored = json.loads(output.read_text())
        assert stored["execution_mode"] == "offline_replay"
        assert stored["candidate_source"] == "supplied_response"
        assert stored["model_call_status"] == "not_attempted"
    else:
        with pytest.raises(SystemExit) as error:
            extract.main()
        assert error.value.code == 2
        message = capsys.readouterr().err
        expected = {
            "missing_config": "LLM_ENDPOINT (or AI_FOUNDRY_ENDPOINT) and LLM_DEPLOYMENT",
            "web_requested": "Web retrieval is disabled",
            "existing_output": "Output path must not already exist",
        }.get(case, "execution_mode, generated_response")
        assert expected in message
        if case == "existing_output":
            assert output.read_text() == "preserved"
        else:
            assert not output.exists()
    factory.assert_not_called()


def test_live_accepts_text_endpoint_without_shared_endpoint(bundle, model_boundary, monkeypatch):
    from backend import extract
    from backend.core.config import settings

    client, create, respond = model_boundary
    respond(bundle.generated_response)
    monkeypatch.setattr(settings, "LLM_ENDPOINT", "https://text.example.test/")
    monkeypatch.setattr(settings, "AI_FOUNDRY_ENDPOINT", None)
    monkeypatch.setattr(settings, "LLM_DEPLOYMENT", "test")
    monkeypatch.setattr(extract, "LLMClient", Mock(return_value=client))

    result = run_live(bundle)

    create.assert_called_once()
    assert result.model_call_status == "succeeded"
    assert result.attributes[0].candidates == bundle.generated_response.candidates


def test_shared_clients_are_lazy_and_cached(monkeypatch):
    import backend.core as core

    client = Mock()
    factory = Mock(return_value=client)
    credential = Mock()
    provider = Mock(return_value="synthetic-provider")
    for name in ("llm", "llm_client", "async_llm_client", "credential", "token_provider"):
        monkeypatch.setitem(core.__dict__, name, None)
        monkeypatch.delitem(core.__dict__, name)
    monkeypatch.setattr(core, "DefaultAzureCredential", credential)
    monkeypatch.setattr(core, "get_bearer_token_provider", provider)
    monkeypatch.setattr(core, "LLMClient", factory)
    assert core.llm is client
    assert core.llm_client is client.sync_client
    assert core.async_llm_client is client.async_client
    factory.assert_called_once_with(token_provider="synthetic-provider")
    credential.assert_called_once()


@pytest.mark.parametrize("kind", ["auth", "nested_auth", "connection", "timeout", "http", "schema", "evidence", "initialization"])
def test_failure_details_are_classified_and_redacted(kind):
    import httpx
    import openai
    from azure.core.exceptions import ClientAuthenticationError
    from backend.core.llm import LLMSchemaValidationError
    from backend.extract import sanitized_failure
    from backend.models.enrichment import ExtractionResponse

    secret = "SYNTHETIC-SECRET-PROMPT-AND-BODY"
    request = httpx.Request("POST", "https://example.test/", headers={"Authorization": secret})
    stage = "service_request"
    if kind == "auth":
        error = ClientAuthenticationError(secret)
        expected = "authentication"
    elif kind == "nested_auth":
        error = openai.APIConnectionError(message=secret, request=request)
        error.__cause__ = ClientAuthenticationError(secret)
        expected = "authentication"
    elif kind == "connection":
        error = openai.APIConnectionError(message=secret, request=request)
        expected = "network_connection"
    elif kind == "timeout":
        error = openai.APITimeoutError(request=request)
        expected = "network_connection"
    elif kind == "http":
        error = openai.BadRequestError(secret, response=httpx.Response(400, request=request), body={
            "error": {"code": "unsupported_parameter", "param": "temperature", "message": secret},
        })
        expected = "service_request"
    elif kind == "schema":
        error = LLMSchemaValidationError(ExtractionResponse, secret, secret)
        expected = "structured_response_parsing"
    elif kind == "evidence":
        error = ValueError(secret)
        stage = expected = "evidence_validation"
    else:
        error = RuntimeError(secret)
        stage = expected = "client_initialization"
    failure = sanitized_failure(error, stage)
    assert failure.stage == expected
    assert secret not in failure.model_dump_json()
    assert failure.exception_class
    if kind == "http":
        assert failure.http_status == 400
        assert failure.service_error_code == "unsupported_parameter"
        assert failure.parameter == "temperature"


def test_unrecognized_service_fields_are_not_echoed():
    import httpx
    import openai
    from backend.extract import sanitized_failure

    secret = "SYNTHETIC-PRIVATE-VALUE"
    error = openai.BadRequestError(secret, response=httpx.Response(400, request=httpx.Request("POST", "https://example.test")), body={
        "code": secret, "param": secret, "message": secret,
    })
    failure = sanitized_failure(error, "service_request")
    assert failure.service_error_code == failure.parameter == "unrecognized_not_displayed"
    assert secret not in failure.model_dump_json()


@pytest.mark.parametrize("outcome", ["valid", "empty", "missing_candidates", "malformed", "service_failure"])
def test_no_retries_cli_uses_one_sdk_request(bundle, monkeypatch, tmp_path, outcome):
    import json
    import httpx
    from backend import extract
    from backend.core.config import settings
    from backend.core.llm import LLMClient

    requests = []
    def respond(request):
        requests.append(request)
        if outcome == "service_failure":
            return httpx.Response(429, json={"error": {"code": "RateLimitExceeded", "message": "SYNTHETIC-PRIVATE"}})
        content = {
            "valid": bundle.generated_response.model_dump_json(),
            "empty": '{"candidates":[]}',
            "missing_candidates": '{}',
            "malformed": "not JSON: SYNTHETIC-PRIVATE",
        }[outcome]
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]})

    client = LLMClient(endpoint="https://example.test", deployment="test", token_provider=lambda: "synthetic-token")
    client.sync_client = client.sync_client.with_options(http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(extract, "LLMClient", Mock(return_value=client))
    monkeypatch.setattr(settings, "AI_FOUNDRY_ENDPOINT", "https://example.test")
    monkeypatch.setattr(settings, "LLM_DEPLOYMENT", "test")
    input_path = tmp_path / "input.json"
    input_path.write_text(live_bundle(bundle).model_dump_json())
    output_path = tmp_path / "output.json"
    monkeypatch.setattr(sys, "argv", ["extract", str(input_path), "--output", str(output_path), "--live", "--no-retries"])
    assert extract.main() == (0 if outcome in ("valid", "empty") else 1)
    stored = json.loads(output_path.read_text())
    assert len(requests) == 1
    assert client.sync_client.max_retries == 0
    assert "SYNTHETIC-PRIVATE" not in output_path.read_text()
    if outcome in ("valid", "empty"):
        assert stored["model_call_status"] == "succeeded"
        assert "failure" not in stored
    else:
        assert stored["model_call_status"] == "failed"
        assert stored["failure"]["stage"] == ("service_request" if outcome == "service_failure" else "structured_response_parsing")
    if outcome == "service_failure":
        assert stored["failure"]["http_status"] == 429
        assert stored["failure"]["service_error_code"] == "RateLimitExceeded"


def test_no_retries_is_not_a_replay_opt_in(bundle):
    from backend.extract import ExecutionConfigurationError, run_enrichment

    with pytest.raises(ExecutionConfigurationError, match="requires live mode"):
        run_enrichment(bundle, no_retries=True)
