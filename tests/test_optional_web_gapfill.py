"""Synthetic-only optional-web readiness. Every service is replaced before execution."""

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.batch import BatchService
from backend.batch_store import read_json, write_json
from backend.batch_worker import RealBatchProcessor, prepare_inference_request, run_batch
from backend.core.websearch import WebIQSearchResult, WebSearchError
from backend.core.websearch_policy import (
    OPTIONAL_WEB_ENABLED_ENV, OPTIONAL_WEB_POLICY_ENV, OptionalWebPolicy, PublicWebScope,
    configured_optional_web_policy,
)
from backend.extract import ExecutionConfigurationError
from backend.models.enrichment import Candidate, ExtractionResponse, Manifest
from backend.multisource import OptionalTierSkipped
from backend.real_pilot import RealPilotBudgetExceeded, RealPilotGuard, binding_digest
from tests.test_real_batch_worker import configured, install_services, internal_only, two_items
from tests.test_multisource import source


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Optional web tests must not use live services")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.delenv("DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED", raising=False)
    monkeypatch.delenv("DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON", raising=False)


def policy_for(record, **changes):
    return OptionalWebPolicy(
        batch_sha256=binding_digest(record),
        items={
            item["item_key"]: PublicWebScope(
                manufacturer="Synthetic", mpn=item["manifest"]["product"]["mpn"],
                attribute_terms={entry["attribute_id"]: entry["attribute_id"] for entry in item["manifest"]["attributes"]},
                source_ids=["web"], allowed_hosts=["manufacturer.invalid"],
            )
            for item in record["items"]
        },
        max_cost_microdollars=changes.pop("max_cost_microdollars", 1000000),
        **changes,
    )


def install_optional(monkeypatch, record, **changes):
    policy = policy_for(record, **changes)
    instances = []
    monkeypatch.setenv("DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED", "true")
    monkeypatch.setenv("DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON", policy.model_dump_json())

    def processor(store, record, guard, *, optional_web_policy=None):
        assert optional_web_policy == policy
        instance = RealBatchProcessor(store, record, guard, optional_web_policy=optional_web_policy)
        instances.append(instance)
        return instance

    monkeypatch.setattr("backend.batch_worker.RealBatchProcessor", processor)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.validate_configuration", lambda _: None)
    return instances


def detail(store, record):
    return BatchService(store).detail(record["id"], "row-2", record["owner"])


def rebind(configured):
    store, record, approval = configured
    approval["batch_sha256"] = binding_digest(record)
    for key, value in (
        ("configuration/real-pilot-approval.json", approval),
        (f"batches/{record['id']}.json", record),
    ):
        _, version = read_json(store, key)
        write_json(store, key, value, version)


def test_optional_queries_only_bound_public_pending_terms_and_persists_original(configured, monkeypatch):
    store, record, _ = configured
    private = "PRIVATE-CUSTOMER-IDENTITY"
    web = record["items"][0]["sources"][1]
    web["applicability"] = copy.deepcopy(web["applicability"])
    web["applicability"][0]["identity_terms"] = [private]
    record["items"][0]["manifest"]["existing_values"] = {"Unsupported": "PRIVATE-CUSTOMER-VALUE"}
    rebind(configured)
    _, queries, pages, calls = install_services(monkeypatch)
    instances = install_optional(monkeypatch, record)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    observed = detail(store, record)
    assert queries == ["Synthetic PART-001 Outlet"]
    assert len(pages) == 1 and len(calls) == 2
    serialized = json.dumps(queries)
    assert private not in serialized and "PRIVATE-CUSTOMER-VALUE" not in serialized
    assert "Body Material" not in serialized and "Unsupported" not in serialized
    original = observed["provenance"][1]["original_pages"][0]
    assert original["url"] == pages[0]
    assert original["content_sha256"] == "d" * 64
    assert datetime.fromisoformat(original["retrieved_at"]).tzinfo is not None
    evidence = observed["machine_result"]["evidence"][-1]
    assert evidence["source_version"] == "sha256:" + "d" * 64
    assert evidence["provider_retrieved_at"] and evidence["discovery_method"] == "supplied_reference"
    assert evidence["attribute_ids"] == ["Outlet"]
    assert instances[0].optional_attempted == {"search": 1, "web_retrieval": 1, "inference": 1}
    assert set(observed["consumption"]["attempted"]) == {"analysis", "inference", "search", "web_retrieval", "retrieval"}


@pytest.mark.parametrize("enabled", [None, "", "false", "TRUE", "1"])
def test_normal_worker_default_does_not_select_optional_policy(configured, monkeypatch, enabled):
    store, record, _ = configured
    _, queries, pages, calls = install_services(monkeypatch)
    monkeypatch.setenv(OPTIONAL_WEB_POLICY_ENV, "Invalid policy must be ignored without exact opt-in")
    if enabled is not None:
        monkeypatch.setenv(OPTIONAL_WEB_ENABLED_ENV, enabled)
    preflight = Mock(side_effect=AssertionError("Legacy default must not select optional policy"))
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.validate_configuration", preflight)
    assert configured_optional_web_policy() is None
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    assert len(queries) == len(pages) == 1 and len(calls) == 2
    preflight.assert_not_called()
    assert detail(store, record)["machine_result"]["attributes"][0]["status"] == "proposed"


@pytest.mark.parametrize("invalid", ["missing", "malformed", "oversized", "wrong_batch", "wrong_mpn", "unapproved_source"])
def test_normal_worker_rejects_invalid_optin_before_reserving_execution(configured, monkeypatch, invalid):
    store, record, _ = configured
    analyses, queries, pages, calls = install_services(monkeypatch)
    policy = policy_for(record).model_dump()
    if invalid == "wrong_batch":
        policy["batch_sha256"] = "0" * 64
    elif invalid == "wrong_mpn":
        policy["items"]["row-2"]["mpn"] = "OTHER-PUBLIC-PART"
    elif invalid == "unapproved_source":
        policy["items"]["row-2"]["source_ids"] = ["not-approved"]
    monkeypatch.setenv(OPTIONAL_WEB_ENABLED_ENV, "true")
    if invalid != "missing":
        raw = "PRIVATE-INVALID-JSON" if invalid == "malformed" else " " * 65537 if invalid == "oversized" else json.dumps(policy)
        monkeypatch.setenv(OPTIONAL_WEB_POLICY_ENV, raw)
    with pytest.raises(ExecutionConfigurationError) as error:
        run_batch(store, record["id"], concurrency=1, item_limit=1)
    assert "PRIVATE-INVALID-JSON" not in str(error.value)
    assert not analyses and not queries and not pages and not calls
    assert not store.keys("budgets/") and not store.keys("items/")


def test_normal_worker_optin_cannot_enable_internal_only_scope(configured, monkeypatch):
    store, record, _ = configured
    install_services(monkeypatch)
    internal_only(configured, monkeypatch)
    monkeypatch.setenv(OPTIONAL_WEB_ENABLED_ENV, "true")
    monkeypatch.setenv(OPTIONAL_WEB_POLICY_ENV, policy_for(record).model_dump_json())
    with pytest.raises(ExecutionConfigurationError, match="separately authorized full"):
        run_batch(store, record["id"], concurrency=1, item_limit=1)
    assert not store.keys("budgets/") and not store.keys("items/")


def test_normal_cli_entry_selects_explicit_optional_policy(configured, monkeypatch):
    store, record, _ = configured
    _, queries, pages, calls = install_services(monkeypatch)
    policy = policy_for(record, max_direct_page_attempts=0)
    monkeypatch.setenv(OPTIONAL_WEB_ENABLED_ENV, "true")
    monkeypatch.setenv(OPTIONAL_WEB_POLICY_ENV, policy.model_dump_json())
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.validate_configuration", lambda _: None)
    monkeypatch.setattr("backend.batch_worker.configured_store", lambda: store)
    monkeypatch.setattr("sys.argv", [
        "batch_worker", "--real-pilot", "--batch-id", record["id"],
        "--concurrency", "1", "--item-limit", "1",
    ])
    from backend.batch_worker import main

    assert main() == 0
    assert len(queries) == len(calls) == 1 and not pages
    observed = detail(store, record)
    assert observed["provenance"][1]["page_errors"][0]["error"] == "optional_operation_capacity"
    assert observed["consumption"]["attempted"]["web_retrieval"] == 0
    assert observed["machine_result"]["attributes"][0]["status"] == "proposed"


def test_optional_provider_unavailable_is_explicit_and_preserves_internal_result(configured, monkeypatch):
    store, record, _ = configured
    _, queries, pages, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record)
    monkeypatch.setattr(
        "backend.core.websearch_webiq.WebIQSearchClient.validate_configuration",
        Mock(side_effect=WebSearchError("Not configured", code="not_configured")),
    )
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    observed = detail(store, record)
    assert observed["machine_result"]["attributes"][0]["status"] == "proposed"
    assert observed["provenance"][1]["skip_reason"] == "optional_provider_unavailable"
    assert observed["provenance"][1]["discovery_error"] == "not_configured"
    assert not queries and not pages and len(calls) == 1
    assert observed["consumption"]["attempted"]["search"] == 0
    assert BatchService(store).export(record["id"], record["owner"])


@pytest.mark.parametrize("error_code", ["authentication_failed", "permission_denied", "timeout", "throttled"])
def test_optional_search_failure_is_not_retried(configured, monkeypatch, error_code):
    store, record, _ = configured
    _, _, pages, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record)
    search = Mock(side_effect=WebSearchError("Provider unavailable", code=error_code))
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.search", search)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    observed = detail(store, record)
    search.assert_called_once()
    assert not pages and len(calls) == 1
    assert observed["provenance"][1]["discovery_error"] == error_code
    assert observed["consumption"]["attempted"]["search"] == 1
    assert observed["machine_result"]["attributes"][0]["status"] == "proposed"


def test_discovery_leads_are_never_evidence_and_two_direct_attempts_are_bounded(configured, monkeypatch):
    store, record, _ = configured
    _, _, pages, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record)
    leads = [
        WebIQSearchResult(
            url=f"https://manufacturer.invalid/lead-{index}", content="Synthetic PART-001; Outlet: gold",
            retrieved_at=datetime.now(timezone.utc),
        ) for index in range(3)
    ]
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.search", lambda *_args, **_kwargs: leads)

    def page(url, **kwargs):
        pages.append(url)
        return SimpleNamespace(
            text="Synthetic PART-999; Outlet: gold", final_url=url,
            content_hash="e" * 64, retrieved_at=datetime.now(timezone.utc), media_type="text/plain",
        )

    monkeypatch.setattr("backend.core.websearch.fetch_original_page", page)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    observed = detail(store, record)
    assert pages == ["https://manufacturer.invalid/product", leads[0].url]
    assert len(calls) == 1
    assert len(observed["machine_result"]["evidence"]) == 1
    assert len(observed["provenance"][1]["original_pages"]) == 2
    assert "gold" not in json.dumps(observed)
    assert observed["consumption"]["attempted"]["web_retrieval"] == 2


def test_optional_model_still_requires_grounded_value_quote_and_citation(configured, monkeypatch):
    store, record, _ = configured
    _, _, _, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record)
    from backend import batch_worker

    original = batch_worker.LLMClient.complete_structured

    def ungrounded(self, system, user, schema, **kwargs):
        response = original(self, system, user, schema, **kwargs)
        payload = json.loads(user)
        if payload["evidence_groups"][0]["source_tier"] == "manufacturer_web":
            return ExtractionResponse(candidates=[Candidate(
                attribute_id="Outlet", value="gold", evidence_ids=["E1"],
                supporting_quote="Outlet: gold", qualification="Exact synthetic product.",
            )])
        return response

    monkeypatch.setattr(batch_worker.LLMClient, "complete_structured", ungrounded)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    observed = detail(store, record)
    assert len(calls) == 2
    assert observed["machine_result"]["attributes"][0]["status"] == "proposed"
    assert not observed["machine_result"]["attributes"][1]["candidates"]
    assert observed["machine_result"]["validation_diagnostics"]


@pytest.mark.parametrize("oversized_part", ["page", "definition"])
def test_complete_optional_payload_over_cap_skips_without_truncation(configured, monkeypatch, oversized_part):
    store, record, _ = configured
    if oversized_part == "definition":
        record["items"][0]["manifest"]["attributes"][1]["description"] = "Definition detail " * 1200
        rebind(configured)
    _, _, pages, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record)
    text = "Synthetic PART-001; Outlet: swivel" + (" X" * 12000 if oversized_part == "page" else "")

    def page(url, **kwargs):
        pages.append(url)
        return SimpleNamespace(
            text=text, final_url=url, content_hash="e" * 64,
            retrieved_at=datetime.now(timezone.utc), media_type="text/plain",
        )

    monkeypatch.setattr("backend.core.websearch.fetch_original_page", page)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    observed = detail(store, record)
    assert observed["machine_result"]["attributes"][0]["status"] == "proposed"
    assert observed["machine_result"]["evidence"][-1]["text"] == text
    skipped = observed["inference_provenance"][-1]
    assert skipped["skip_reason"] == "optional_complete_input_capacity"
    assert skipped["input_bound"] > 26000
    assert skipped["accounting"]["max_output_tokens"] == 2048
    assert skipped["evidence_truncated"] is False
    assert not skipped["new_model_call"]
    assert len(calls) == 1 and observed["consumption"]["attempted"]["inference"] == 1
    assert any(entry["error_code"] == "optional_complete_input_capacity"
               for entry in observed["machine_result"]["retrieval"] if entry.get("error_code"))


@pytest.mark.parametrize("limits,reason", [
    ({"max_cost_microdollars": 0}, "optional_cost_capacity"),
    ({"max_search_calls": 0}, "optional_operation_capacity"),
])
def test_local_optional_cost_and_operation_denials_are_skips(configured, monkeypatch, limits, reason):
    store, record, _ = configured
    _, queries, pages, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record, **limits)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    observed = detail(store, record)
    assert observed["provenance"][1]["skip_reason"] == reason
    assert observed["machine_result"]["attributes"][0]["status"] == "proposed"
    assert not queries and not pages and len(calls) == 1
    assert observed["consumption"]["attempted"]["search"] == 0


@pytest.mark.parametrize("operation", ["search", "web_retrieval", "inference"])
def test_global_reservation_denial_is_fatal_and_does_not_start_next_item(configured, monkeypatch, operation):
    store, record, _ = configured
    two_items(configured)
    _, queries, _, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record)
    reserve = RealPilotGuard.reserve

    def denied(self, requested, key, **kwargs):
        if requested == operation and (operation != "inference" or queries):
            raise RealPilotBudgetExceeded("spend_microdollars", 10, 0)
        return reserve(self, requested, key, **kwargs)

    monkeypatch.setattr(RealPilotGuard, "reserve", denied)
    with pytest.raises(ExecutionConfigurationError, match="Global reservation guard"):
        run_batch(store, record["id"], concurrency=1, item_limit=2)
    assert len(calls) == 1
    assert not store.keys(f"items/{record['id']}/row-3")
    assert read_json(store, f"items/{record['id']}/row-2.json")[0]["state"] == "failed"


@pytest.mark.parametrize("dimension,limit", [
    ("inference", 0), ("search", 0), ("web_retrieval", 0),
    ("input_tokens", 1), ("output_tokens", 1), ("spend_microdollars", 1),
])
def test_normal_worker_real_global_denial_stops_queued_work(configured, monkeypatch, dimension, limit):
    store, record, approval = configured
    two_items(configured)
    approval["limits"][dimension] = limit
    rebind(configured)
    _, queries, pages, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record)
    with pytest.raises(ExecutionConfigurationError, match="Global reservation guard"):
        run_batch(store, record["id"], concurrency=1, item_limit=2)
    assert not store.keys(f"items/{record['id']}/row-3")
    assert read_json(store, f"items/{record['id']}/row-2.json")[0]["state"] == "failed"
    if dimension in {"search", "web_retrieval"}:
        assert len(calls) == 1
    else:
        assert not calls and not queries and not pages


def test_expired_guard_is_fatal_even_when_optional_cost_would_skip(configured, monkeypatch):
    store, record, approval = configured
    two_items(configured)
    install_services(monkeypatch)
    install_optional(monkeypatch, record, max_cost_microdollars=0)
    expires = datetime.fromisoformat(approval["expires_at"])

    def expire(_):
        monkeypatch.setattr("backend.real_pilot._now", lambda: expires)

    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.validate_configuration", expire)
    with pytest.raises(ExecutionConfigurationError):
        run_batch(store, record["id"], concurrency=1, item_limit=2)
    assert not store.keys(f"items/{record['id']}/row-3")


def test_optional_policy_cannot_enable_current_internal_pilot(configured, monkeypatch):
    store, record, _ = configured
    internal_only(configured, monkeypatch)
    guard = RealPilotGuard(store, record)
    with pytest.raises(ExecutionConfigurationError, match="separately authorized full"):
        RealBatchProcessor(store, record, guard, optional_web_policy=policy_for(record))


def test_two_product_plan_has_four_internal_plus_two_optional_calls_and_no_browse(configured, monkeypatch):
    store, record, approval = configured
    two_items(configured)
    for item in record["items"]:
        vendor = copy.deepcopy(item["sources"][0])
        vendor.update(source_id="vendor", format="xlsx", source_tier="vendor_table", reference="synthetic.xlsx")
        item["sources"].append(vendor)
        item["manifest"]["source_ids"].append("vendor")
        web = item["sources"][1]
        suffix = item["manifest"]["product"]["mpn"]
        web["url"] = web["reference"] = f"https://manufacturer.invalid/{suffix}"
    approval["limits"].update(search=2, web_retrieval=4, inference=6, input_tokens=200000, output_tokens=12288)
    rebind(configured)
    analyses, queries, pages, calls = install_services(monkeypatch)
    instances = install_optional(monkeypatch, record)

    def internal(self, binding, item, scope, provenance):
        tier = binding["source_tier"]
        text = "Body Material: brass" if tier == "internal_pdf" else "Outlet: threaded"
        return source(Manifest.model_validate(item["manifest"]), tier, text)

    def search(_self, query, **kwargs):
        queries.append(query)
        mpn = query.split()[1]
        return [WebIQSearchResult(
            url=f"https://manufacturer.invalid/{mpn}-details", content="UNTRUSTED DISCOVERY PASSAGE",
            retrieved_at=datetime.now(timezone.utc),
        )]

    def page(url, **kwargs):
        pages.append(url)
        mpn = "PART-002" if "PART-002" in url else "PART-001"
        return SimpleNamespace(
            text=f"Synthetic {mpn}; Unsupported: documented",
            final_url=url, content_hash="f" * 64,
            retrieved_at=datetime.now(timezone.utc), media_type="text/plain",
        )

    monkeypatch.setattr(RealBatchProcessor, "real_document", internal)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.search", search)
    monkeypatch.setattr("backend.core.websearch.fetch_original_page", page)
    run_batch(store, record["id"], concurrency=1, item_limit=2)
    assert queries == ["Synthetic PART-001 Unsupported", "Synthetic PART-002 Unsupported"]
    assert len(calls) == 6 and len(pages) == 4 and not analyses
    assert instances[0].optional_attempted == {"search": 2, "web_retrieval": 4, "inference": 2}
    ledger, _ = read_json(store, "budgets/real-pilot.json")
    assert ledger["attempted"] == {"analysis": 0, "inference": 6, "search": 2, "web_retrieval": 4, "retrieval": 0}
    for item in record["items"]:
        observed = BatchService(store).detail(record["id"], item["item_key"], record["owner"])
        optional = [entry for entry in observed["inference_provenance"] if entry.get("source_tier") == "manufacturer_web"]
        assert len(optional) == 1 and optional[0]["accounting"]["max_input_tokens"] <= 26000
        assert optional[0]["accounting"]["max_output_tokens"] == 2048
        assert "UNTRUSTED DISCOVERY PASSAGE" not in json.dumps(observed)
        assert all(attribute["status"] == "proposed" for attribute in observed["machine_result"]["attributes"])


@pytest.mark.parametrize("stage", ["initialization", "request"])
def test_optional_model_failure_keeps_reservation_and_internal_export(configured, monkeypatch, stage):
    store, record, _ = configured
    _, _, _, calls = install_services(monkeypatch)
    install_optional(monkeypatch, record)
    from backend import batch_worker

    client_type = batch_worker.LLMClient
    creations = []

    def client(**kwargs):
        creations.append(True)
        if len(creations) == 2 and stage == "initialization":
            raise RuntimeError("Unavailable provider; private detail must not leak")
        instance = client_type(**kwargs)
        if len(creations) == 2:
            instance.complete_structured = Mock(side_effect=RuntimeError("Unavailable provider"))
        return instance

    monkeypatch.setattr(batch_worker, "LLMClient", client)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    observed = detail(store, record)
    failed = observed["inference_provenance"][-1]
    assert failed["method"] == "optional_failed"
    assert failed["skip_reason"] == ("optional_model_unavailable" if stage == "initialization" else "optional_model_failed")
    assert failed["new_model_call"] is (stage == "request")
    assert failed["reservation_id"]
    assert len(creations) == 2 and len(calls) == 1
    assert observed["consumption"]["attempted"]["inference"] == 2
    assert observed["machine_result"]["attributes"][0]["status"] == "proposed"
    assert BatchService(store).export(record["id"], record["owner"])


def test_accounting_api_includes_complete_compacted_payload_and_schema():
    user = json.dumps({"product": {"mpn": "PUBLIC-001"}, "attributes": [], "evidence": []})
    request = prepare_inference_request("Instructions Ω", user, ExtractionResponse, deployment="gpt-5")
    accounting = request.accounting
    assert accounting["system_utf8_bytes"] == len(request.system.encode())
    assert accounting["user_utf8_bytes"] == len(request.user.encode())
    assert accounting["response_schema_utf8_bytes"] == len(json.dumps(ExtractionResponse.model_json_schema()).encode())
    assert request.input_bound == sum(accounting[name] for name in (
        "system_utf8_bytes", "user_utf8_bytes", "response_schema_utf8_bytes", "framing_allowance",
    ))
    assert request.request_parameters == {"max_completion_tokens": 2048, "reasoning_effort": "minimal"}
    assert accounting["max_output_tokens"] == 2048


@pytest.mark.parametrize("input_bound,admitted", [(26000, True), (26001, False)])
def test_remeasured_optional_input_boundary_is_admitted_or_skipped_without_truncation(configured, input_bound, admitted):
    store, record, _ = configured
    policy = policy_for(record)
    assert policy.max_input_tokens == 26000
    assert policy.max_input_tokens + policy.max_output_tokens == 28048 < 30000
    guard = RealPilotGuard(store, record)
    guard.before_execution("synthetic-optional-input-boundary")
    processor = RealBatchProcessor(store, record, guard, optional_web_policy=policy)
    item = record["items"][0]
    key = processor.key(item, "inference", "synthetic-boundary")
    if admitted:
        reservation = processor.reserve_optional(
            "inference", key, item_key=item["item_key"], max_input_tokens=input_bound, max_output_tokens=2048,
        )
        assert reservation["reserved_usage"]["input_tokens"] == input_bound
    else:
        with pytest.raises(OptionalTierSkipped, match="optional_complete_input_capacity"):
            processor.reserve_optional(
                "inference", key, item_key=item["item_key"], max_input_tokens=input_bound, max_output_tokens=2048,
            )
    ledger, _ = read_json(store, "budgets/real-pilot.json")
    assert ledger["attempted"]["inference"] == int(admitted)
    assert ledger["reserved"]["input_tokens"] == (input_bound if admitted else 0)
