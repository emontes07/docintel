import copy
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.batch import BatchService
from backend.batch_store import Conflict, Missing, SQLiteStore, read_json, write_json
from backend.batch_worker import (
    COMPACT_PROMPT_FORMAT, COMPACT_PROMPT_INSTRUCTIONS, RealBatchProcessor,
    compact_inference_prompt, run_batch,
)
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
                for evidence in model_evidence(payload):
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
def test_real_worker_budget_denial_preserves_independently_authorized_sources(configured, monkeypatch, execution_scope):
    store, record, approval = configured
    analyses, queries, _, calls = install_services(monkeypatch)
    if execution_scope == "internal_only":
        internal_only(configured, monkeypatch)
    approval["limits"]["analysis"] = 0
    _, version = read_json(store, "configuration/real-pilot-approval.json")
    write_json(store, "configuration/real-pilot-approval.json", approval, version)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    assert not analyses
    detail = BatchService(store).detail(record["id"], "row-2", record["owner"])
    assert detail["state"] == "unresolved"
    assert any(entry.get("error") == "budget_exhausted" for entry in detail["provenance"])
    assert detail["consumption"]["attempted"]["analysis"] == 0
    if execution_scope == "internal_only":
        assert not queries and not calls
    else:
        assert len(queries) == len(calls) == 1
        assert detail["machine_result"]["attributes"][1]["status"] == "proposed"


def test_web_fetch_budget_denial_preserves_internal_result_and_safe_details(configured, monkeypatch):
    store, record, approval = configured
    _, queries, pages, calls = install_services(monkeypatch)
    approval["limits"]["web_retrieval"] = 0
    _, version = read_json(store, "configuration/real-pilot-approval.json")
    write_json(store, "configuration/real-pilot-approval.json", approval, version)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    detail = BatchService(store).detail(record["id"], "row-2", record["owner"])
    assert detail["machine_result"]["attributes"][0]["status"] == "proposed"
    error = detail["provenance"][1]["page_errors"][0]
    assert error["error"] == "budget_exhausted"
    assert (error["budget"], error["requested"], error["remaining"]) == ("web_retrieval", 1, 0)
    assert len(queries) == len(calls) == 1 and not pages
    assert detail["consumption"]["attempted"]["web_retrieval"] == 0


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
            evidence = next((entry for entry in model_evidence(payload) if entry["source_tier"] == "vendor_table"), None)
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
    assert len(calls) == 1
    assert processor.inference_provenance[-1]["method"] == "compatible_response_cache"
    cached = [path for path in store.keys("real-inferences/") if path != old_key]
    assert len(cached) == 1
    cached_response = read_json(store, cached[0])[0]
    assert cached_response["prompt_format"] == COMPACT_PROMPT_FORMAT
    expected = {"max_completion_tokens": 2048}
    if deployment == "gpt-5":
        expected["reasoning_effort"] = "minimal"
    assert cached_response["request_parameters"] == expected
    assert guard.metadata()["attempted"]["inference"] == 1


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


def model_evidence(payload):
    assert payload["evidence_columns"] == ["evidence_id", "group", "text_index", "location"]
    return [
        {**payload["evidence_groups"][group], "evidence_id": reference, "text": payload["evidence_texts"][text_index]}
        for reference, group, text_index, _ in payload["evidence"]
    ]


def expand_prompt_evidence(payload):
    expanded = []
    for _, group, text_index, location in payload["evidence"]:
        shared = dict(payload["evidence_groups"][group])
        id_prefix = shared.pop("evidence_id_prefix")
        locator_prefix = shared.pop("source_locator_prefix")
        id_suffix, locator_suffix = (location, location) if isinstance(location, str) else location
        expanded.append({
            **shared, "text": payload["evidence_texts"][text_index],
            "evidence_id": id_prefix + id_suffix, "source_locator": locator_prefix + locator_suffix,
        })
    return sorted(expanded, key=lambda entry: entry["evidence_id"])


def test_compact_prompt_is_lossless_and_never_merges_different_applicability(configured):
    from backend.extract import source_evidence
    from backend.models.enrichment import OfflineSource

    _, record, _ = configured
    product = ProductKey.model_validate(record["items"][0]["manifest"]["product"])
    stamp = datetime.now(timezone.utc)
    document = ParsedDocument(
        source="batchblob:///documents/synthetic.pdf", cache_key="sha256:" + "e" * 64, parsed_at=stamp,
        paragraphs=[ParsedParagraph(text="Body Material: brass", page_number=1) for _ in range(79)],
    )
    source = OfflineSource(
        source_id="pdf", product=product, document=document, attribute_ids=["Body Material"],
        qualification="Applicable only to this product. " * 100, provider_retrieved_at=stamp,
    )
    entries = [entry.model_dump(mode="json") for entry in source_evidence(source, stamp)]
    distinct = copy.deepcopy(entries[0])
    distinct.update(evidence_id="another-id", source_locator="opaque:another/location",
                    attribute_ids=["Outlet"], qualification="Different attribute applicability.")
    entries.append(distinct)
    original = {"product": product.model_dump(), "attributes": record["items"][0]["manifest"]["attributes"], "evidence": entries}
    prompt, references = compact_inference_prompt(json.dumps(original))
    projected = json.loads(prompt)
    assert len(json.dumps(original).encode()) > 200000
    assert len(prompt.encode()) < 12000
    assert projected["product"] == original["product"] and projected["attributes"] == original["attributes"]
    assert len(projected["evidence_groups"]) == 2
    assert len(projected["evidence"]) == 80
    assert len(projected["evidence_texts"]) == 1
    assert expand_prompt_evidence(projected) == sorted(entries, key=lambda entry: entry["evidence_id"])
    assert sorted(references.values()) == sorted(entry["evidence_id"] for entry in entries)
    assert references["E1"] == entries[0]["evidence_id"]
    assert entries[0]["evidence_id"] != "E1"


def test_compact_response_rejects_unknown_citation_without_retry(configured, monkeypatch):
    store, record, _ = configured
    install_services(monkeypatch)
    internal_only(configured, monkeypatch)
    client = Mock()
    client.sync_client.with_options.return_value = client.sync_client
    client.last_usage = {"input_tokens": 10, "output_tokens": 10}
    client.complete_structured.return_value = ExtractionResponse(candidates=[Candidate(
        attribute_id="Body Material", value="brass", evidence_ids=["invented-reference"],
        supporting_quote="Body Material: brass", qualification="Exact product.",
    )])
    monkeypatch.setattr("backend.batch_worker.LLMClient", Mock(return_value=client))
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    detail = BatchService(store).detail(record["id"], "row-2", record["owner"])
    assert detail["machine_result"]["extraction_error"] == "invalid_response"
    assert all(not attribute["candidates"] for attribute in detail["machine_result"]["attributes"])
    assert detail["consumption"]["attempted"]["inference"] == 1
    assert detail["consumption"]["actual_usage"]["input_tokens"] == 10
    assert not store.keys("real-inferences/")
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    client.complete_structured.assert_called_once()


def test_compact_citation_selects_only_one_original_location(configured, monkeypatch):
    store, record, _ = configured
    install_services(monkeypatch)
    internal_only(configured, monkeypatch)

    class Parser:
        def __init__(self, **kwargs):
            pass

        def extract_pdf_bytes(self, content, *, source, page_limit):
            text = "Synthetic PART-001; Body Material: brass"
            return ParsedDocument(
                source=source, cache_key="sha256:" + hashlib.sha256(content).hexdigest(),
                parsed_at=datetime.now(timezone.utc), raw_text=text,
                paragraphs=[ParsedParagraph(text=text, page_number=1) for _ in range(2)],
            )

    client = Mock()
    client.sync_client.with_options.return_value = client.sync_client
    client.last_usage = {"input_tokens": 10, "output_tokens": 10}
    client.complete_structured.return_value = ExtractionResponse(candidates=[Candidate(
        attribute_id="Body Material", value="brass", evidence_ids=["E2"],
        supporting_quote="Body Material: brass", qualification="Exact product.",
    )])
    monkeypatch.setattr("backend.batch_worker.DocumentIntelligenceService", Parser)
    monkeypatch.setattr("backend.batch_worker.LLMClient", Mock(return_value=client))
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    result = BatchService(store).detail(record["id"], "row-2", record["owner"])["machine_result"]
    assert len(result["evidence"]) == 2
    selected = result["evidence"][1]["evidence_id"]
    assert result["attributes"][0]["candidates"][0]["evidence_ids"] == [selected]
    assert selected.endswith("paragraph=1")
    cached, _ = read_json(store, store.keys("real-inferences/")[0])
    assert cached["response"]["candidates"][0]["evidence_ids"] == [selected]


def two_items(configured):
    store, record, approval = configured
    second = copy.deepcopy(record["items"][0])
    second.update(item_key="row-3", row=3)
    second["manifest"]["product"].update(item_id="002", mpn="PART-002")
    second["original"].update({"PIMITEM Number": "002", "MPN": "PART-002"})
    content = b"%PDF-1.7 another synthetic\n%%EOF"
    for binding in second["sources"]:
        binding["products"] = [second["manifest"]["product"]]
        binding["applicability"][0]["product"] = second["manifest"]["product"]
        binding["applicability"][0]["identity_terms"] = ["Synthetic", "PART-002"]
        if binding["format"] == "pdf":
            binding.update(blob="documents/second.pdf", sha256=hashlib.sha256(content).hexdigest())
    record["items"].append(second)
    record["product_count"] = approval["limits"]["products"] = 2
    approval["batch_sha256"] = binding_digest(record)
    for path, value in [(f"batches/{record['id']}.json", record), ("configuration/real-pilot-approval.json", approval)]:
        _, version = read_json(store, path)
        write_json(store, path, value, version)
    store.write_bytes("documents/second.pdf", content)


def test_oversized_prompt_is_visible_and_does_not_abort_independent_item(configured, monkeypatch):
    store, record, _ = configured
    analyses, _, _, calls = install_services(monkeypatch)
    two_items(configured)
    internal_only(configured, monkeypatch)

    class Parser:
        def __init__(self, **kwargs):
            pass

        def extract_pdf_bytes(self, content, *, source, page_limit):
            analyses.append(source)
            text = "Synthetic PART-002; Body Material: brass" if source.endswith("second.pdf") else "Synthetic PART-001 " + "X" * 200000
            return ParsedDocument(
                source=source, cache_key="sha256:" + hashlib.sha256(content).hexdigest(),
                parsed_at=datetime.now(timezone.utc), raw_text=text,
                paragraphs=[ParsedParagraph(text=text, page_number=1)],
            )

    monkeypatch.setattr("backend.batch_worker.DocumentIntelligenceService", Parser)
    run_batch(store, record["id"], concurrency=1, item_limit=2)
    first = BatchService(store).detail(record["id"], "row-2", record["owner"])
    second = BatchService(store).detail(record["id"], "row-3", record["owner"])
    assert first["state"] == "unresolved"
    assert first["machine_result"]["model_call_status"] == "not_attempted"
    assert first["machine_result"]["skip_reason"] is None
    assert first["machine_result"]["failure"]["exception_class"] == "RealPilotBudgetExceeded"
    assert first["machine_result"]["failure"]["parameter"] == "input_tokens"
    assert first["inference_provenance"][0]["new_model_call"] is False
    assert first["inference_provenance"][0]["input_bound"] > 200000
    assert second["machine_result"]["attributes"][0]["status"] == "proposed"
    assert len(analyses) == 2 and len(calls) == 1
    ledger, _ = read_json(store, "budgets/real-pilot.json")
    assert ledger["attempted"]["inference"] == 1
    assert read_json(store, f"batches/{record['id']}.json")[0]["state"] == "completed"


def test_authorization_denial_remains_fatal_without_starting_next_item(configured, monkeypatch):
    from backend.extract import ExecutionConfigurationError

    store, record, _ = configured
    two_items(configured)
    internal_only(configured, monkeypatch)
    denied = Mock(side_effect=ValueError("Approval invalidated"))
    monkeypatch.setattr(RealPilotGuard, "reserve", denied)
    with pytest.raises(ExecutionConfigurationError):
        run_batch(store, record["id"], concurrency=1, item_limit=2)
    denied.assert_called_once()
    failed, _ = read_json(store, f"items/{record['id']}/row-2.json")
    assert failed["state"] == "failed" and failed["finished_at"]
    assert "No automatic retry" in failed["error"]
    assert not store.keys(f"items/{record['id']}/row-3")


def test_interrupted_item_is_not_retried_by_corrected_prompt(configured, monkeypatch):
    store, record, _ = configured
    internal_only(configured, monkeypatch)
    write_json(store, f"items/{record['id']}/row-2.json", {"state": "running"})
    processor = Mock(side_effect=AssertionError("Interrupted item must not run again"))
    run_batch(store, record["id"], processor=processor, concurrency=1, item_limit=1)
    processor.assert_not_called()
    assert read_json(store, f"items/{record['id']}/row-2.json")[0]["state"] == "interrupted"
    assert not store.keys("results/")


def test_actual_private_four_product_prompt_bounds():
    path = os.environ.get("DOCINTEL_TEST_PILOT_PROMPTS")
    if not path:
        pytest.skip("Requires private actual PDF/vendor prompt captures for all four products")
    records = json.loads(Path(path).read_text())
    assert len(records) == 8
    assert len({record["item_key"] for record in records}) == 4
    for item in {record["item_key"] for record in records}:
        calls = [record for record in records if record["item_key"] == item]
        assert len(calls) == 2
        assert {frozenset(entry["source_tier"] for entry in record["user"]["evidence"])
                for record in calls} == {frozenset({"internal_pdf"}), frozenset({"vendor_table"})}
        assert calls[0]["user"]["attributes"] == calls[1]["user"]["attributes"]
    total = 0
    for record in records:
        original = record["user"]
        assert len(json.dumps(original).encode()) == record["original_user_bytes"]
        compact, references = compact_inference_prompt(json.dumps(original))
        projected = json.loads(compact)
        assert expand_prompt_evidence(projected) == sorted(original["evidence"], key=lambda entry: entry["evidence_id"])
        assert sorted(references.values()) == sorted(entry["evidence_id"] for entry in original["evidence"])
        total += (len((record["system"] + COMPACT_PROMPT_INSTRUCTIONS).encode()) + len(compact.encode())
                  + len(json.dumps(record["schema"]).encode()) + 4096)
    assert total <= 200000


@pytest.fixture
def recovery_case(configured, monkeypatch):
    from backend import real_pilot
    from backend.batch_worker import PARSER_VERSION, digest

    store, record, approval = configured
    pacing_clock = [0.0]
    monkeypatch.setattr("backend.batch_worker.monotonic", lambda: pacing_clock[0])
    monkeypatch.setattr("backend.batch_worker.sleep", lambda delay: pacing_clock.__setitem__(0, pacing_clock[0] + delay))
    two_items(configured)
    for index in (3, 4):
        item = copy.deepcopy(record["items"][0])
        item.update(item_key=f"row-{index + 1}", row=index + 1)
        item["manifest"]["product"].update(item_id=f"00{index}", mpn=f"PART-00{index}")
        item["original"].update({"PIMITEM Number": f"00{index}", "MPN": f"PART-00{index}"})
        record["items"].append(item)
    record["product_count"] = approval["limits"]["products"] = 4
    approval["batch_sha256"] = binding_digest(record)
    _, version = read_json(store, f"batches/{record['id']}.json")
    write_json(store, f"batches/{record['id']}.json", record, version)
    internal_only(configured, monkeypatch)
    guard = RealPilotGuard(store, record)
    prior_execution = guard.before_execution("prior-failed-worker")
    reservation = guard.reserve(
        "analysis", guard.operation_key(record["items"][0], tier="analysis",
                                       source_version="original", prompt_version="original"),
        item_key="row-2", analysis_pages=5,
    )
    guard.record_usage(reservation["reservation_id"], analysis_pages=1)
    interrupted, caches = {}, {}
    for item in record["items"][:2]:
        state = {"state": "interrupted", "started_at": "preserved-original-time",
                 "error": "Prior execution failed before inference"}
        key = f"items/{record['id']}/{item['item_key']}.json"
        write_json(store, key, state)
        interrupted[item["item_key"]] = digest(store.read_bytes(key)[0])
        binding = item["sources"][0]
        location = "batchblob:///" + binding["blob"]
        cache = "parses/" + digest((location + binding["sha256"] + PARSER_VERSION + ":pages=1-5").encode()) + ".json"
        text = f"Synthetic {item['manifest']['product']['mpn']}; Body Material: brass"
        document = ParsedDocument(
            source=location, cache_key="sha256:" + binding["sha256"],
            parsed_at=datetime.now(timezone.utc), raw_text=text,
            paragraphs=[ParsedParagraph(text=text, page_number=1)],
        )
        write_json(store, cache, {
            "parser_version": PARSER_VERSION, "origin": "prior_synthetic_fixture_analysis",
            "document": document.model_dump(mode="json"),
            "document_sha256": digest(document.model_dump_json().encode()),
        })
        caches[cache] = digest(store.read_bytes(cache)[0])
    original_ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    start = datetime.fromisoformat(approval["expires_at"]) + timedelta(seconds=1)
    recovery = {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
        "approval_sha256": real_pilot._sha256(approval), "batch_sha256": approval["batch_sha256"],
        "ledger_sha256": real_pilot._sha256(original_ledger), "prior_execution_id": prior_execution,
        "not_before": start.isoformat(), "expires_at": (start + timedelta(minutes=20)).isoformat(),
        "selected_item_keys": ["row-2", "row-3"],
        "interrupted_sha256": interrupted, "cached_documents": caches,
    }
    write_json(store, real_pilot.RECOVERY_KEY, recovery)
    monkeypatch.setattr(real_pilot, "_now", lambda: start + timedelta(seconds=1))
    return store, record, approval, recovery, original_ledger


def test_recovery_runs_only_selected_cached_items_preserving_history(recovery_case, monkeypatch):
    import base64
    from backend import real_pilot
    from backend.workbooks import read_workbook

    store, record, approval, recovery, previous = recovery_case
    analyses, _, _, calls = install_services(monkeypatch)
    original_approval = store.read_bytes(real_pilot.APPROVAL_KEY)[0]
    run_batch(store, record["id"], concurrency=1, item_limit=2)
    assert not analyses and len(calls) == 2
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert len(ledger["executions"]) == 2
    assert ledger["attempted"]["analysis"] == 1
    assert ledger["reserved"]["analysis_pages"] == 5
    assert all(ledger["executions"][key] == value for key, value in previous["executions"].items())
    assert all(ledger["reservations"][key] == value for key, value in previous["reservations"].items())
    assert store.read_bytes(real_pilot.APPROVAL_KEY)[0] == original_approval
    audit = read_json(store, real_pilot.RECOVERY_AUDIT_KEY)[0]
    for item in record["items"][:2]:
        key = item["item_key"]
        state = read_json(store, f"items/{record['id']}/{key}.json")[0]
        raw = base64.b64decode(audit["prior_states"][key]["base64"])
        assert hashlib.sha256(raw).hexdigest() == recovery["interrupted_sha256"][key]
        assert state["previous_attempt"] == json.loads(raw)
    for item in record["items"][2:]:
        state = read_json(store, f"items/{record['id']}/{item['item_key']}.json")[0]
        assert state["state"] == "deferred" and "started_at" not in state
        assert not store.keys(f"results/{record['id']}/{item['item_key']}")
    batch = read_json(store, f"batches/{record['id']}.json")[0]
    assert batch["state"] == "deferred"
    assert batch["progress"]["finished"] == batch["progress"]["deferred"] == 2
    exported = read_workbook(BatchService(store).export(record["id"], record["owner"]))
    assert [row["State"] for row in exported["Errors"]][-2:] == ["deferred", "deferred"]
    with pytest.raises(Missing):
        BatchService(store).export(record["id"], "another-owner")
    before = store.read_bytes(real_pilot.BUDGET_KEY)[0]
    run_batch(store, record["id"], concurrency=1, item_limit=2)
    assert store.read_bytes(real_pilot.BUDGET_KEY)[0] == before


@pytest.mark.parametrize("failure", ["changed_cache", "changed_state", "existing_result", "existing_review", "changed_ledger", "wrong_limit"])
def test_recovery_preflight_denials_do_not_consume_worker(recovery_case, failure):
    from backend import real_pilot

    store, record, _, recovery, _ = recovery_case
    if failure == "changed_cache":
        path = next(iter(recovery["cached_documents"]))
        _, version = store.read_bytes(path)
        store.write_bytes(path, b"{}", version)
    elif failure == "changed_state":
        path = f"items/{record['id']}/row-2.json"
        _, version = read_json(store, path)
        write_json(store, path, {"state": "interrupted", "changed": True}, version)
    elif failure in {"existing_result", "existing_review"}:
        prefix = "results" if failure == "existing_result" else "reviews"
        write_json(store, f"{prefix}/{record['id']}/row-2.json", {})
    elif failure == "changed_ledger":
        ledger, version = read_json(store, real_pilot.BUDGET_KEY)
        ledger["actual_billed_microdollars"] = 1
        write_json(store, real_pilot.BUDGET_KEY, ledger, version)
    before = store.read_bytes(real_pilot.BUDGET_KEY)[0]
    with pytest.raises(ValueError):
        run_batch(store, record["id"], concurrency=1, item_limit=4 if failure == "wrong_limit" else 2)
    assert store.read_bytes(real_pilot.BUDGET_KEY)[0] == before
    assert not store.keys(real_pilot.RECOVERY_AUDIT_KEY)
    assert read_json(store, f"items/{record['id']}/row-2.json")[0]["state"] == "interrupted"


def test_recovery_guard_forbids_analysis_external_and_deferred_item(recovery_case):
    from backend import real_pilot

    store, record, *_ = recovery_case
    guard = RealPilotGuard(store, record)
    guard.before_execution("last-worker")
    before = store.read_bytes(real_pilot.BUDGET_KEY)[0]
    with pytest.raises(ValueError, match="deferred"):
        guard.operation_key(record["items"][2], tier="pdf", source_version="x", prompt_version="x")
    for operation in ("analysis", "search", "web_retrieval", "retrieval"):
        key = guard.operation_key(record["items"][0], tier=operation, source_version="x", prompt_version="x")
        with pytest.raises(ValueError):
            guard.reserve(operation, key, item_key="row-2", analysis_pages=5 if operation == "analysis" else 0)
    assert store.read_bytes(real_pilot.BUDGET_KEY)[0] == before


def test_recovery_fatal_guard_preserves_next_interrupted_history(recovery_case, monkeypatch):
    from backend import real_pilot
    from backend.extract import ExecutionConfigurationError

    store, record, _, recovery, _ = recovery_case
    install_services(monkeypatch)
    def fatal(*args, **kwargs):
        raise ValueError("Authorization changed")
    monkeypatch.setattr(RealPilotGuard, "reserve", fatal)
    with pytest.raises(ExecutionConfigurationError):
        run_batch(store, record["id"], concurrency=1, item_limit=2)
    second = read_json(store, f"items/{record['id']}/row-3.json")[0]
    assert second["state"] == "recovery_ready"
    assert second["previous_attempt"]["state"] == "interrupted"
    assert "started_at" not in second
    assert not store.keys("results/")
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert len(ledger["executions"]) == 2 and ledger["attempted"]["inference"] == 0
    with pytest.raises(ValueError, match="execution budget exhausted"):
        run_batch(store, record["id"], concurrency=1, item_limit=2)


def test_recovery_caps_inference_without_resetting_original_allowances(recovery_case):
    from backend import real_pilot

    store, record, _, recovery, previous = recovery_case
    guard = RealPilotGuard(store, record)
    guard.before_execution("last-worker")
    for index in range(4):
        key = guard.operation_key(record["items"][index % 2], tier="pdf", source_version=str(index), prompt_version="x")
        guard.reserve("inference", key, item_key=record["items"][index % 2]["item_key"],
                      max_input_tokens=100, max_output_tokens=2048)
    before = store.read_bytes(real_pilot.BUDGET_KEY)[0]
    key = guard.operation_key(record["items"][0], tier="pdf", source_version="excess", prompt_version="x")
    with pytest.raises(real_pilot.RealPilotBudgetExceeded, match="recovery_inference"):
        guard.reserve("inference", key, item_key="row-2", max_input_tokens=100, max_output_tokens=2048)
    assert store.read_bytes(real_pilot.BUDGET_KEY)[0] == before


def test_recovery_validation_is_read_only_and_requires_current_bindings(recovery_case):
    from backend import real_pilot

    store, batch, approval, recovery, ledger = recovery_case
    before = {key: store.read_bytes(key) for key in store.keys("")}
    assert real_pilot.validate_recovery(recovery, approval, batch, ledger, store) == recovery
    bad = {**recovery, "approved": False}
    with pytest.raises(ValueError):
        real_pilot.validate_recovery(bad, approval, batch, ledger, store)
    assert before == {key: store.read_bytes(key) for key in store.keys("")}


def test_recovery_pacing_and_per_request_capacity(recovery_case, monkeypatch):
    from backend import real_pilot

    store, record, *_ = recovery_case
    guard = RealPilotGuard(store, record)
    guard.before_execution("last-worker")
    processor = RealBatchProcessor(store, record, guard)
    clock, delays = [0.0], []
    monkeypatch.setattr("backend.batch_worker.monotonic", lambda: clock[0])
    def wait(delay):
        delays.append(delay)
        clock[0] += delay
    monkeypatch.setattr("backend.batch_worker.sleep", wait)
    for index in range(4):
        key = processor.key(record["items"][0], "inference", str(index))
        processor.reserve_real("inference", key, item_key="row-2", max_input_tokens=100, max_output_tokens=2048)
    assert delays == [61, 61, 61]
    before = store.read_bytes(real_pilot.BUDGET_KEY)[0]
    key = processor.key(record["items"][0], "inference", "too-large")
    with pytest.raises(real_pilot.RealPilotBudgetExceeded, match="recovery_request_tokens"):
        guard.reserve("inference", key, item_key="row-2", max_input_tokens=28000, max_output_tokens=2048)
    assert store.read_bytes(real_pilot.BUDGET_KEY)[0] == before


@pytest.mark.parametrize("change", ["operator", "approval", "window", "selection"])
def test_recovery_cannot_change_authority_or_expand_scope(recovery_case, change):
    from backend import real_pilot

    store, batch, approval, recovery, ledger = recovery_case
    candidate = copy.deepcopy(recovery)
    if change == "operator":
        candidate["approved_by"] = "99999999-9999-4999-8999-999999999999"
    elif change == "approval":
        candidate["approval_sha256"] = "f" * 64
    elif change == "window":
        candidate["expires_at"] = (datetime.fromisoformat(candidate["not_before"]) + timedelta(seconds=1201)).isoformat()
    else:
        candidate["selected_item_keys"].append("row-4")
    before = {key: store.read_bytes(key) for key in store.keys("")}
    with pytest.raises(ValueError):
        real_pilot.validate_recovery(candidate, approval, batch, ledger, store)
    assert before == {key: store.read_bytes(key) for key in store.keys("")}


def test_recovery_cache_loss_after_start_cannot_trigger_new_analysis(recovery_case, monkeypatch):
    from backend.extract import ExecutionConfigurationError

    store, record, _, recovery, previous = recovery_case
    analyses, _, _, calls = install_services(monkeypatch)
    started = [False]
    original_read = store.read_bytes
    original_call = RealBatchProcessor.__call__
    def read(key, *args, **kwargs):
        if started[0] and key in recovery["cached_documents"]:
            raise Missing(key)
        return original_read(key, *args, **kwargs)
    def execute(processor, item, mode):
        started[0] = True
        return original_call(processor, item, mode)
    monkeypatch.setattr(store, "read_bytes", read)
    monkeypatch.setattr(RealBatchProcessor, "__call__", execute)
    with pytest.raises(ExecutionConfigurationError, match="authorization or consumption guard"):
        run_batch(store, record["id"], concurrency=1, item_limit=2)
    assert not analyses and not calls
    ledger = read_json(store, "budgets/real-pilot.json")[0]
    assert ledger["reservations"] == previous["reservations"]
    assert ledger["attempted"]["analysis"] == 1 and ledger["attempted"]["inference"] == 0
    second = read_json(store, f"items/{record['id']}/row-3.json")[0]
    assert second["state"] == "recovery_ready" and "started_at" not in second


def test_recovery_fences_history_writes_after_lease_loss(recovery_case, monkeypatch):
    from contextlib import contextmanager
    from backend import real_pilot

    store, record, *_ = recovery_case
    original_lease = store.lease
    def lost():
        raise Conflict("Batch lease lost")
    @contextmanager
    def lease(key):
        with original_lease(key) as renew:
            yield lost if key == record["id"] else renew
    monkeypatch.setattr(store, "lease", lease)
    with pytest.raises(Conflict, match="Batch lease lost"):
        run_batch(store, record["id"], concurrency=1, item_limit=2)
    assert len(read_json(store, real_pilot.BUDGET_KEY)[0]["executions"]) == 2
    assert not store.keys(real_pilot.RECOVERY_AUDIT_KEY)
    assert not store.keys("results/")
    for key in ("row-2", "row-3"):
        assert read_json(store, f"items/{record['id']}/{key}.json")[0]["state"] == "interrupted"


def test_recovery_api_reports_deferral_and_keeps_owner_review_boundary(recovery_case, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.batch_api import development_app
    from backend.batch_auth import actor

    store, record, *_ = recovery_case
    install_services(monkeypatch)
    run_batch(store, record["id"], concurrency=1, item_limit=2)
    monkeypatch.setattr("backend.batch_api.service", lambda: BatchService(store))
    monkeypatch.setitem(development_app.dependency_overrides, actor, lambda: record["owner"])
    client = TestClient(development_app)
    base = f"/api/v1/batches/{record['id']}"
    assert client.get(base).json()["state"] == "deferred"
    detail = client.get(base + "/items/row-4")
    assert detail.status_code == 200
    assert detail.json()["state"] == "deferred" and detail.json()["machine_result"] is None
    assert client.get(base + "/items/row-2").json()["previous_attempt"]["state"] == "interrupted"
    assert client.get(base + "/export").status_code == 200
    assert client.post(base + "/items/row-4/reviews", json={
        "attribute_id": "Body Material", "decision": "approve", "reason": "Synthetic test",
    }).status_code == 422
    assert not store.keys("reviews/")
    monkeypatch.setitem(development_app.dependency_overrides, actor, lambda: "another-owner")
    for suffix in ("", "/items", "/items/row-2", "/items/row-4", "/export"):
        assert client.get(base + suffix).status_code == 404


def test_actual_private_two_product_recovery_prompt_bounds():
    path = os.environ.get("DOCINTEL_TEST_SUBSET_PROMPTS")
    if not path:
        pytest.skip("Requires exact private two-product recovery payload captures")
    records = json.loads(Path(path).read_text())
    assert len(records) == 4
    keys = {record["item_key"] for record in records}
    assert len(keys) == 2
    assert len({record["user"]["product"]["item_id"] for record in records}) == 2
    total = 0
    for key in keys:
        stages = [record for record in records if record["item_key"] == key]
        assert len(stages) == 2
        assert stages[0]["user"]["attributes"] == stages[1]["user"]["attributes"]
        assert [{entry["source_tier"] for entry in record["user"]["evidence"]}
                for record in stages] == [{"internal_pdf"}, {"vendor_table"}]
    for record in records:
        compact, references = compact_inference_prompt(json.dumps(record["user"]))
        assert expand_prompt_evidence(json.loads(compact)) == sorted(
            record["user"]["evidence"], key=lambda entry: entry["evidence_id"])
        assert sorted(references.values()) == sorted(entry["evidence_id"] for entry in record["user"]["evidence"])
        bound = (len((record["system"] + COMPACT_PROMPT_INSTRUCTIONS).encode()) + len(compact.encode())
                 + len(json.dumps(record["schema"]).encode()) + 4096)
        assert bound + 2048 <= 30000
        total += bound
    assert total <= 200000 and 4 * 2048 <= 32768
