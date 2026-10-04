import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.batch import BatchService
from backend.batch_store import Conflict, Missing, SQLiteStore, read_json, write_json
from backend.batch_worker import RealBatchProcessor, run_batch
from backend.core.docintel import ParsedDocument, ParsedParagraph
from backend.core.llm import LLM_API_VERSION
from backend.models.enrichment import AttributeDefinition, Candidate, ExtractionResponse, Manifest, ProductKey
from backend.real_pilot import RealPilotGuard, binding_digest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("No live calls from real-pilot regression tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    # Keep synthetic fixture files in the runner's selected scratch directory;
    # production SQLiteStore placement restrictions remain unchanged.
    store = SQLiteStore.__new__(SQLiteStore)
    store.path = tmp_path / "batches.sqlite3"
    with store.connect() as connection:
        connection.execute("CREATE TABLE records (key TEXT PRIMARY KEY, value BLOB NOT NULL, version TEXT NOT NULL)")
    store.path.chmod(0o600)
    product = ProductKey(item_id="001", vendor="Synthetic", mpn="PART-001", hierarchy_node="Valves")
    names = ["Body Material", "Outlet", "Unsupported"]
    pdf = b"%PDF-1.7 synthetic\n%%EOF"
    scope = {"product": product.model_dump(), "identity_terms": ["Synthetic", "PART-001"],
             "attribute_ids": names, "qualification": "Exact synthetic product only."}
    sources = [
        {"reference": "synthetic.pdf", "source_id": "pdf", "kind": "blob", "format": "pdf",
         "source_tier": "internal_pdf", "blob": "documents/synthetic.pdf",
         "sha256": hashlib.sha256(pdf).hexdigest(), "products": [product.model_dump()], "applicability": [scope]},
        {"reference": "https://manufacturer.invalid/product", "source_id": "web", "kind": "web", "format": "web",
         "source_tier": "manufacturer_web", "url": "https://manufacturer.invalid/product",
         "products": [product.model_dump()], "applicability": [scope]},
    ]
    manifest = Manifest(
        product=product, attributes=[AttributeDefinition(attribute_id=name, description=name, value_type="string") for name in names],
        source_ids=["pdf", "web"],
    )
    item = {"item_key": "row-2", "row": 2, "manifest": manifest.model_dump(mode="json"), "sources": sources,
            "original": {"PIMITEM Number": "001", "Vendor Name": "Synthetic", "MPN": "PART-001"},
            "warnings": [], "errors": []}
    owner = "11111111-1111-4111-8111-111111111111/22222222-2222-4222-8222-222222222222"
    record = {
        "id": "a" * 64, "owner": owner, "items": [item], "mode": "real_pilot",
        "state": "queued", "valid": True, "product_count": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "input_hashes": {"manifest": "b" * 64, "attributes": "c" * 64},
        "attribute_reference": "attributes.xlsx",
        "original_definitions": [{"node": "Valves", "potential_attribute_name": name} for name in names],
    }
    environment = {
        "AZURE_CLIENT_ID": "", "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://parser.invalid",
        "LLM_ENDPOINT": "https://model.invalid", "LLM_DEPLOYMENT": "synthetic", "AOAI_API_VERSION": LLM_API_VERSION,
        "WEBSEARCH_PROVIDER": "webiq", "WEBIQ_ENDPOINT": "https://api.microsoft.ai/v3/search/web",
    }
    operator = "33333333-3333-4333-8333-333333333333"
    worker = "44444444-4444-4444-8444-444444444444"
    for name, value in {**environment, "DOCINTEL_REAL_PILOT_ENABLED": "true",
                        "DOCINTEL_REAL_PILOT_OPERATOR_IDS": operator,
                        "DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID": worker}.items():
        monkeypatch.setenv(name, value)
    from backend.core.config import settings
    for name, value in environment.items():
        if hasattr(settings, name):
            monkeypatch.setattr(settings, name, value)
    approval = {
        "schema_version": 1, "approved": True, "id": str(uuid.uuid4()), "approved_by": operator,
        "not_before": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "batch_id": record["id"], "owner": owner, "batch_sha256": binding_digest(record),
        "customer_processing_approved": True,
        "identities": {"api_principal_id": "55555555-5555-4555-8555-555555555555", "worker_principal_id": worker},
        "environment": environment,
        "limits": {"products": 1, "executions": 2, "analysis": 2, "inference": 4, "search": 1,
                   "web_retrieval": 3, "retrieval": 0, "analysis_pages": 10,
                   "input_tokens": 100000, "output_tokens": 8192, "spend_microdollars": 1000000},
        "unit_prices_usd": {"analysis_page": "0.001", "input_token": "0.000001",
                            "output_token": "0.000001", "search": "0.001", "web_retrieval": "0.001"},
    }
    write_json(store, "configuration/real-pilot-approval.json", approval)
    write_json(store, f"batches/{record['id']}.json", record)
    store.write_bytes("documents/synthetic.pdf", pdf)
    return store, record, approval


def internal_only(configured, monkeypatch):
    from backend.real_pilot import EXTERNAL_ENVIRONMENT_KEYS, EXTERNAL_OPERATIONS, INTERNAL_PRICE_KEYS
    from backend.core.config import settings

    store, _, approval = configured
    approval["execution_scope"] = "internal_only"
    approval["customer_processing_approved"] = True
    for operation in EXTERNAL_OPERATIONS:
        approval["limits"][operation] = 0
    approval["environment"] = {
        name: value for name, value in approval["environment"].items()
        if name not in EXTERNAL_ENVIRONMENT_KEYS
    }
    approval["unit_prices_usd"] = {
        name: value for name, value in approval["unit_prices_usd"].items()
        if name in INTERNAL_PRICE_KEYS
    }
    _, version = read_json(store, "configuration/real-pilot-approval.json")
    write_json(store, "configuration/real-pilot-approval.json", approval, version)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_EXECUTION_SCOPE", "internal_only")
    monkeypatch.delenv("WEBIQ_API_KEY", raising=False)
    monkeypatch.delenv("WEBIQ_ENDPOINT", raising=False)
    monkeypatch.setattr(settings, "WEBIQ_API_KEY", None)
    monkeypatch.setattr(settings, "WEBIQ_ENDPOINT", None)
    external = Mock(side_effect=AssertionError("Internal-only mode must never construct or call external providers"))
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient", external)
    monkeypatch.setattr("backend.core.websearch.fetch_original_page", external)
    monkeypatch.setattr("backend.core.pilot_sources.retrieve_document", external)
    return external


def install_services(monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "LLM_ENDPOINT", "https://model.invalid")
    monkeypatch.setattr(settings, "LLM_DEPLOYMENT", "synthetic")
    monkeypatch.setattr("backend.batch_worker.ManagedIdentityCredential", lambda **_: Mock())
    monkeypatch.setattr("backend.batch_worker.get_bearer_token_provider", lambda *_: lambda: "synthetic")
    analyses = []
    queries = []
    pages = []
    calls = []

    class Parser:
        def __init__(self, **kwargs):
            pass

        def extract_pdf_bytes(self, content, *, source, page_limit):
            analyses.append((source, page_limit))
            return ParsedDocument(
                source=source, cache_key="sha256:" + hashlib.sha256(content).hexdigest(),
                parsed_at=datetime.now(timezone.utc),
                raw_text="Synthetic PART-001 Body Material: brass",
                paragraphs=[ParsedParagraph(text="Synthetic PART-001; Body Material: brass", page_number=1)],
            )

    class LLM:
        def __init__(self, **kwargs):
            self.sync_client = Mock()
            self.sync_client.with_options.return_value = self.sync_client
            self.last_usage = {"input_tokens": 50, "output_tokens": 20}

        def complete_structured(self, system, user, schema, **kwargs):
            payload = json.loads(user)
            calls.append((payload, kwargs))
            candidates = []
            for attribute in payload["attributes"]:
                for evidence in payload["evidence"]:
                    for part in evidence["text"].split("; "):
                        if part.startswith(attribute["attribute_id"] + ": "):
                            candidates.append(Candidate(
                                attribute_id=attribute["attribute_id"], value=part.split(": ", 1)[1],
                                evidence_ids=[evidence["evidence_id"]], supporting_quote=part,
                                qualification="Named component for exact synthetic product.",
                            ))
            return ExtractionResponse(candidates=candidates)

    def search(self, query, allowed_domains=None, *, authorized=False):
        assert authorized and allowed_domains == ["manufacturer.invalid"]
        queries.append(query)
        return []

    def page(url, *, allowed_hosts, authorized):
        assert authorized and allowed_hosts == ["manufacturer.invalid"]
        pages.append(url)
        return SimpleNamespace(
            text="Synthetic PART-001; Outlet: swivel", final_url=url, content_hash="d" * 64,
            retrieved_at=datetime.now(timezone.utc), media_type="text/html",
        )

    monkeypatch.setattr("backend.batch_worker.DocumentIntelligenceService", Parser)
    monkeypatch.setattr("backend.batch_worker.LLMClient", LLM)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.search", search)
    monkeypatch.setattr("backend.core.websearch.fetch_original_page", page)
    return analyses, queries, pages, calls


def test_real_worker_merges_internal_and_web_with_durable_no_repeat(configured, monkeypatch):
    store, record, _ = configured
    analyses, queries, pages, calls = install_services(monkeypatch)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    service = BatchService(store)
    detail = service.detail(record["id"], "row-2", record["owner"])
    result = detail["machine_result"]
    assert [attribute["status"] for attribute in result["attributes"]] == ["proposed", "proposed", "missing_evidence"]
    assert [entry["source_tier"] for entry in result["evidence"]] == ["internal_pdf", "manufacturer_web"]
    assert analyses == [("batchblob:///documents/synthetic.pdf", 5)]
    assert len(queries) == len(pages) == 1
    assert "Body Material" not in queries[0] and "Outlet" in queries[0]
    assert "001/" not in queries[0]
    assert len(calls) == 2 and all(call[1] == {"max_retries": 1, "max_completion_tokens": 2048} for call in calls)
    before = store.read_bytes(f"results/{record['id']}/row-2.json")
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    assert len(calls) == 2 and store.read_bytes(f"results/{record['id']}/row-2.json") == before
    with pytest.raises(Missing):
        service.detail(record["id"], "row-2", "another-owner")
    service.review(record["id"], "row-2", record["owner"], {
        "attribute_id": "Outlet", "decision": "approve", "candidate_index": 0, "reason": "Synthetic review",
    })
    assert store.read_bytes(f"results/{record['id']}/row-2.json") == before
    from backend.workbooks import read_workbook
    export = read_workbook(service.export(record["id"], record["owner"]))
    assert export["Results"][1]["Source tiers"] == "manufacturer_web"
    assert export["Evidence"][0]["Source tier"] == "internal_pdf"


@pytest.mark.parametrize("execution_scope", ["full", "internal_only"])
def test_real_worker_default_off_never_calls_service(configured, monkeypatch, execution_scope):
    store, record, _ = configured
    install_services(monkeypatch)
    if execution_scope == "internal_only":
        internal_only(configured, monkeypatch)
    monkeypatch.delenv("DOCINTEL_REAL_PILOT_ENABLED")
    with pytest.raises(ValueError):
        run_batch(store, record["id"], concurrency=1, item_limit=1)
    assert not store.keys("items/")


@pytest.mark.parametrize("execution_scope", ["full", "internal_only"])
def test_real_worker_budget_failure_stops_before_later_paid_work(configured, monkeypatch, execution_scope):
    store, record, approval = configured
    analyses, queries, _, calls = install_services(monkeypatch)
    if execution_scope == "internal_only":
        internal_only(configured, monkeypatch)
    approval["limits"]["analysis"] = 0
    _, version = read_json(store, "configuration/real-pilot-approval.json")
    write_json(store, "configuration/real-pilot-approval.json", approval, version)
    from backend.extract import ExecutionConfigurationError
    with pytest.raises(ExecutionConfigurationError):
        run_batch(store, record["id"], concurrency=1, item_limit=1)
    assert not analyses and not queries and not calls


def test_source_failure_preserves_external_proposals(configured, monkeypatch):
    store, record, _ = configured
    _, queries, _, calls = install_services(monkeypatch)
    from backend.core.docintel import DocumentIntelligenceError
    monkeypatch.setattr(
        "backend.batch_worker.DocumentIntelligenceService",
        Mock(return_value=SimpleNamespace(extract_pdf_bytes=Mock(side_effect=DocumentIntelligenceError("failed")))),
    )
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    result, _ = read_json(store, f"results/{record['id']}/row-2.json")
    assert result["attributes"][0]["status"] == "retrieval_failed"
    assert result["attributes"][1]["status"] == "proposed"
    assert len(queries) == len(calls) == 1


def test_compatible_prior_parse_avoids_paid_analysis(configured, monkeypatch):
    store, record, _ = configured
    analyses, _, _, _ = install_services(monkeypatch)
    from backend.batch import digest
    from backend.pilot import PARSER_VERSION
    binding = record["items"][0]["sources"][0]
    location = "batchblob:///" + binding["blob"]
    document = ParsedDocument(
        source=location, cache_key="sha256:" + binding["sha256"], parsed_at=datetime.now(timezone.utc),
        raw_text="Synthetic PART-001",
        paragraphs=[ParsedParagraph(text="Synthetic PART-001; Body Material: brass", page_number=1)],
    )
    key = "parses/" + digest((location + binding["sha256"] + PARSER_VERSION).encode()) + ".json"
    write_json(store, key, {
        "parser_version": PARSER_VERSION, "origin": "prior_approved_analysis",
        "document": document.model_dump(mode="json"), "document_sha256": digest(document.model_dump_json().encode()),
    })
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    assert not analyses
    detail = BatchService(store).detail(record["id"], "row-2", record["owner"])
    assert detail["provenance"][0]["parse_origin"] == "prior_approved_analysis"


def test_new_optional_contract_fields_do_not_change_legacy_machine_hash(configured):
    store, record, _ = configured
    from backend.extract import run_enrichment
    from backend.models.enrichment import OfflineBundle
    from backend.batch import digest
    result = run_enrichment(OfflineBundle(
        manifest=Manifest.model_validate(record["items"][0]["manifest"]),
        generated_response=ExtractionResponse(candidates=[]),
    )).model_dump(mode="json")
    for definition in result["manifest"]["attributes"]:
        for key in ["definition_context", "type_guidance", "definition_node", "unit_resolved"]:
            definition.pop(key)
    legacy_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    write_json(store, f"items/{record['id']}/row-2.json", {"state": "unresolved"})
    write_json(store, f"results/{record['id']}/row-2.json", result)
    assert BatchService(store).detail(record["id"], "row-2", record["owner"])["machine_sha256"] == digest(legacy_json.encode())


@pytest.mark.parametrize("execution_scope", ["full", "internal_only"])
@pytest.mark.parametrize("pdf_identity_matches", [True, False])
def test_vendor_rows_supplement_pdf_before_web_without_variant_leakage(configured, monkeypatch, execution_scope, pdf_identity_matches):
    store, record, approval = configured
    from backend.workbooks import write_workbook
    table = write_workbook({"Catalog": [
        ["MPN", "Inlet"],
        ["PART-002", "wrong-variant"],
        ["PART-001", "threaded"],
    ]})
    store.write_bytes("documents/vendor.xlsx", table)
    item = record["items"][0]
    product = item["manifest"]["product"]
    item["manifest"]["attributes"].append(
        AttributeDefinition(attribute_id="Inlet", description="Inlet", value_type="string").model_dump(mode="json")
    )
    item["manifest"]["source_ids"].append("vendor")
    item["sources"].append({
        "reference": "vendor.xlsx", "source_id": "vendor", "kind": "blob", "format": "xlsx",
        "source_tier": "vendor_table", "blob": "documents/vendor.xlsx", "sha256": hashlib.sha256(table).hexdigest(),
        "products": [product],
        "table": {"sheet": "Catalog", "mpn_column": "MPN", "expected_vendor": "Synthetic"},
        "applicability": [{"product": product, "identity_terms": ["PART-001"], "attribute_ids": ["Inlet"],
                           "qualification": "Single-vendor synthetic export; exact row only."}],
    })
    approval["batch_sha256"] = binding_digest(record)
    for path, value in [(f"batches/{record['id']}.json", record), ("configuration/real-pilot-approval.json", approval)]:
        _, version = read_json(store, path)
        write_json(store, path, value, version)
    _, queries, _, calls = install_services(monkeypatch)
    import backend.batch_worker as worker
    original_llm = worker.LLMClient
    if not pdf_identity_matches:
        class UnmatchedDrawing:
            def __init__(self, **kwargs):
                pass

            def extract_pdf_bytes(self, content, *, source, page_limit):
                return ParsedDocument(
                    source=source, cache_key="sha256:" + hashlib.sha256(content).hexdigest(),
                    parsed_at=datetime.now(timezone.utc), raw_text="Drawing without approved identity",
                    paragraphs=[ParsedParagraph(text="Unidentified product diagram", page_number=1)],
                )

        monkeypatch.setattr(worker, "DocumentIntelligenceService", UnmatchedDrawing)

    class TableLLM(original_llm):
        def complete_structured(self, system, user, schema, **kwargs):
            payload = json.loads(user)
            evidence = next((entry for entry in payload["evidence"] if entry["source_tier"] == "vendor_table"), None)
            if evidence:
                calls.append((payload, kwargs))
                assert "wrong-variant" not in evidence["text"]
                return ExtractionResponse(candidates=[Candidate(
                    attribute_id="Inlet", value="threaded", evidence_ids=[evidence["evidence_id"]],
                    supporting_quote='"value": "threaded"', qualification="Exact synthetic row and inlet field.",
                )])
            return super().complete_structured(system, user, schema, **kwargs)

    monkeypatch.setattr(worker, "LLMClient", TableLLM)
    external = internal_only(configured, monkeypatch) if execution_scope == "internal_only" else None
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    detail = BatchService(store).detail(record["id"], "row-2", record["owner"])
    assert detail["machine_result"]["attributes"][-1]["status"] == "proposed"
    evidence = next(entry for entry in detail["machine_result"]["evidence"] if entry["source_tier"] == "vendor_table")
    assert "sheet=Catalog&row=3&cells=A3,B3" in evidence["source_locator"]
    if external is None:
        assert "Inlet" not in queries[0]
    else:
        external.assert_not_called()
        assert not queries and len(calls) == (2 if pdf_identity_matches else 1)
        assert detail["consumption"]["execution_scope"] == "internal_only"
        assert detail["coverage"]["externally_supported"] == []
        assert detail["coverage"]["webiq_discovered_support"] == []
        assert detail["machine_result"]["attributes"][1]["status"] == ("missing_evidence" if pdf_identity_matches else "retrieval_failed")
        from backend.workbooks import read_workbook
        exported = read_workbook(BatchService(store).export(record["id"], record["owner"]))
        assert "internal_only" in exported["Provenance"][0]["Source provenance"]
        assert "internal_only" in exported["Provenance"][0]["Consumption reservations and usage"]
        assert {entry["Source tier"] for entry in exported["Evidence"]} == ({"internal_pdf", "vendor_table"} if pdf_identity_matches else {"vendor_table"})
    assert detail["coverage"]["internally_supported"] == (["Body Material", "Inlet"] if pdf_identity_matches else ["Inlet"])
    if not pdf_identity_matches:
        assert detail["machine_result"]["attributes"][0]["status"] == "retrieval_failed"


def test_internal_only_skips_enabled_sharepoint_and_web_metadata_without_reservations(configured, monkeypatch):
    store, record, approval = configured
    analyses, queries, pages, calls = install_services(monkeypatch)
    item = record["items"][0]
    remote = {
        **item["sources"][0], "source_id": "remote", "reference": "https://tenant.sharepoint.com/file.pdf",
        "kind": "sharepoint", "url": "https://tenant.sharepoint.com/file.pdf",
        "enabled": True, "drive_id": "approved-drive", "item_id": "approved-item",
        "tenant_id": "11111111-1111-4111-8111-111111111111",
    }
    remote.pop("blob")
    item["sources"].append(remote)
    item["manifest"]["source_ids"].append("remote")
    approval["batch_sha256"] = binding_digest(record)
    _, version = read_json(store, f"batches/{record['id']}.json")
    write_json(store, f"batches/{record['id']}.json", record, version)
    external = internal_only(configured, monkeypatch)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    external.assert_not_called()
    assert len(analyses) == len(calls) == 1
    assert not queries and not pages
    detail = BatchService(store).detail(record["id"], item["item_key"], record["owner"])
    assert detail["state"] == "unresolved"
    assert [entry["status"] for entry in detail["machine_result"]["attributes"]] == ["proposed", "missing_evidence", "missing_evidence"]
    skipped = {entry["source_id"]: entry for entry in detail["provenance"] if entry.get("skip_reason")}
    assert set(skipped) == {"web", "remote"}
    assert all(entry["retrieval"] == "not_attempted" for entry in skipped.values())
    assert all(detail["consumption"]["attempted"][name] == 0 for name in ("search", "web_retrieval", "retrieval"))
    assert detail["machine_result"]["manifest"]["source_ids"] == item["manifest"]["source_ids"]


def test_internal_only_defensive_provider_entry_points_fail_before_clients(configured, monkeypatch):
    from backend.extract import ExecutionConfigurationError
    store, record, _ = configured
    install_services(monkeypatch)
    external = internal_only(configured, monkeypatch)
    guard = RealPilotGuard(store, record)
    guard.before_execution("explicit-internal")
    processor = RealBatchProcessor(store, record, guard)
    item = record["items"][0]
    binding = item["sources"][1]
    with pytest.raises(ExecutionConfigurationError, match="excluded"):
        processor.web_sources(binding, item, {}, [], {})
    with pytest.raises(ExecutionConfigurationError, match="copies only"):
        processor.document_bytes(binding, item, {})
    external.assert_not_called()
    assert sum(guard.metadata()["attempted"].values()) == 0


@pytest.mark.parametrize("deployment", ["gpt-5", "gpt-5.1", "gpt-5-chat", "synthetic"])
def test_real_pilot_gpt5_minimal_effort_keeps_cap_and_other_model_defaults(configured, monkeypatch, deployment):
    from backend.core.config import settings

    store, record, approval = configured
    _, _, _, calls = install_services(monkeypatch)
    monkeypatch.setattr(settings, "LLM_DEPLOYMENT", deployment)
    approval["environment"]["LLM_DEPLOYMENT"] = deployment
    external = internal_only(configured, monkeypatch)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    external.assert_not_called()
    assert len(calls) == 1
    expected = {"max_retries": 1, "max_completion_tokens": 2048}
    if deployment == "gpt-5":
        expected["reasoning_effort"] = "minimal"
    assert calls[0][1] == expected
    detail = BatchService(store).detail(record["id"], "row-2", record["owner"])
    recorded = detail["inference_provenance"][0]["request_parameters"]
    assert recorded == {name: value for name, value in expected.items() if name != "max_retries"}
    assert detail["consumption"]["reserved"]["output_tokens"] == 2048


@pytest.mark.parametrize("deployment", ["gpt-5", "synthetic"])
def test_gpt5_request_parameters_are_bound_to_cache_identity(configured, monkeypatch, deployment):
    from backend.batch import digest
    from backend.core.config import settings

    store, record, approval = configured
    _, _, _, calls = install_services(monkeypatch)
    monkeypatch.setattr(settings, "LLM_DEPLOYMENT", deployment)
    approval["environment"]["LLM_DEPLOYMENT"] = deployment
    internal_only(configured, monkeypatch)
    guard = RealPilotGuard(store, record)
    guard.before_execution("cache-identity-test")
    processor = RealBatchProcessor(store, record, guard)
    item = record["items"][0]
    system, user = "synthetic", '{"attributes":[],"evidence":[]}'
    old_version = digest((system + user + ExtractionResponse.model_json_schema().__repr__()).encode())
    old_key = "real-inferences/" + processor.key(item, "inference", old_version) + ".json"
    write_json(store, old_key, {"response": {"candidates": []}})
    completion = processor.completion(item)
    completion.complete_structured(system, user, ExtractionResponse)
    completion.complete_structured(system, user, ExtractionResponse)
    assert len(calls) == (1 if deployment == "gpt-5" else 0)
    assert processor.inference_provenance[-1]["method"] == "compatible_response_cache"
    cached = [path for path in store.keys("real-inferences/") if path != old_key]
    if deployment == "gpt-5":
        assert len(cached) == 1
        assert read_json(store, cached[0])[0]["request_parameters"] == {
            "max_completion_tokens": 2048, "reasoning_effort": "minimal",
        }
        assert guard.metadata()["attempted"]["inference"] == 1
    else:
        assert not cached and guard.metadata()["attempted"]["inference"] == 0


def test_webiq_contribution_requires_independent_original_evidence(configured, monkeypatch):
    store, record, _ = configured
    install_services(monkeypatch)
    discovered_url = "https://manufacturer.invalid/discovered"
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.search", lambda *args, **kwargs: [
        SimpleNamespace(url=discovered_url, content="Outlet: fabricated-search-only",
                        retrieved_at=datetime.now(timezone.utc)),
    ])

    def page(url, **kwargs):
        return SimpleNamespace(
            text="Synthetic PART-001; Outlet: swivel" if url == discovered_url else "Manufacturer homepage",
            final_url=url, content_hash=hashlib.sha256(url.encode()).hexdigest(),
            retrieved_at=datetime.now(timezone.utc), media_type="text/html",
        )

    monkeypatch.setattr("backend.core.websearch.fetch_original_page", page)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    detail = BatchService(store).detail(record["id"], "row-2", record["owner"])
    assert detail["coverage"]["webiq_discovered_support"] == ["Outlet"]
    assert "fabricated-search-only" not in json.dumps(detail["machine_result"])
    assert detail["machine_result"]["attributes"][1]["candidates"][0]["value"] == "swivel"
    assert detail["machine_result"]["evidence"][1]["discovery_method"] == "webiq"


def test_identity_terms_do_not_match_part_number_prefixes():
    from backend.batch_worker import identity_matches
    assert identity_matches("Synthetic PART-001; Body Material: brass", ["Synthetic", "PART-001"])
    assert not identity_matches("Synthetic PART-001-A", ["Synthetic", "PART-001"])
    assert not identity_matches("Synthetic PART-0010", ["Synthetic", "PART-001"])


def test_discovery_failure_keeps_independently_supported_reference(configured, monkeypatch):
    from backend.core.websearch import WebSearchError

    store, record, approval = configured
    analyses, queries, pages, calls = install_services(monkeypatch)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.search",
                        Mock(side_effect=WebSearchError("Synthetic failure", code="provider_error")))
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    detail = BatchService(store).detail(record["id"], "row-2", record["owner"])
    assert set(detail["coverage"]["internally_supported"]) == {"Body Material"}
    assert detail["coverage"]["externally_supported"] == ["Outlet"]
    assert detail["coverage"]["webiq_discovered_support"] == []
    assert detail["provenance"][1]["discovery_error"] == "provider_error"
    assert detail["state"] == "unresolved"
    assert len(pages) == 1 and len(calls) == 2
