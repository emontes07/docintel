"""Synthetic, network-denied four-product authority. Never a real approval."""

import copy
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from backend import real_pilot
from backend.batch_store import Conflict, read_json, write_json
from scripts import four_product_continuation as helper
from tests.test_gapfill_rerun import gapfill_case, runtime
from tests.test_row_rerun import (
    all_records, configured as base_configured, final_case, network_blocked, put, recovery_case, row_case, start_row,
)


def records_of(store):
    return {key: json.loads(raw) for key, (raw, _) in all_records(store).items() if key.endswith(".json")}


def duration_assumptions():
    return {
        "basis": "empirical_provider_latency_conditional", "deployment_tpm": 30000,
        "inference_interval_seconds": 31, "worker_timeout_seconds": 600,
        "historical_max_model_seconds": 21.42772, "model_response_allowance_seconds": 22,
        "startup_bookkeeping_seconds": 35, "web_network_allowance_seconds": 150, "forecast_seconds": 548,
        "provider_rate_estimate_verified": False, "byte_bounds_used_as_rate_estimate": False,
        "billing_usage_used_as_rate_estimate": False,
    }


@pytest.fixture
def configured(tmp_path, monkeypatch, request):
    store, batch, approval = base_configured.__wrapped__(tmp_path, monkeypatch)
    if hasattr(request, "param"):
        approval["unit_prices_usd"]["analysis_page"] = request.param
        put(store, real_pilot.APPROVAL_KEY, approval)
    return store, batch, approval


def synthetic_prep(store, *, actual_page_count=5):
    """Faithful append-only accounting shape, explicitly not a recovered DI call."""
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    approval = read_json(store, real_pilot.APPROVAL_KEY)[0]
    reserved_cost = real_pilot.RealPilotGuard._cost(approval, "analysis", 0, 0, 5)
    measured_cost = real_pilot.RealPilotGuard._cost(approval, "analysis", 0, 0, actual_page_count)
    historical = {key: real_pilot._sha256(value) for key, value in records_of(store).items()}
    prefix = real_pilot.FOUR_PRODUCT_PREP_PREFIX
    stamp = datetime.now(timezone.utc).isoformat()
    source_hash = prefix.split("/")[-2]
    contract = {
        "purpose": "one_ford_analysis_preparation_before_readiness", "source_sha256": source_hash,
        "source_location": "batchblob:///documents/av-source-4.pdf",
        "model": "prebuilt-layout", "api_version": "2024-11-30", "request_options": {"pages": "1-5"},
        "max_submissions": 1, "max_analysis_pages": 5, "sdk_retries": 0, "worker_executions": 0,
        "inference": 0, "search": 0, "web_retrieval": 0,
        "identity_route": "local_operator_di_api_mi_storage",
    }
    tenant = "00000000-0000-4000-8000-000000000002"
    subscription = "00000000-0000-4000-8000-000000000003"
    analysis = {"kind": "approved_operator", "credential": "AzureCliCredential",
                "principal_id": approval["approved_by"], "tenant_id": tenant, "subscription_id": subscription}
    storage = {"kind": "api_system_assigned", "credential": "ManagedIdentityCredential",
               "principal_id": approval["identities"]["api_principal_id"], "tenant_id": tenant}
    program = {"source": "# REPRODUCTION ONLY: synthetic storage program, never executed.\n"}
    program["sha256"] = hashlib.sha256(program["source"].encode()).hexdigest()
    packet = {
        "contract": contract, "batch_id": approval["batch_id"], "approved_by": approval["approved_by"],
        "api_principal_id": approval["identities"]["api_principal_id"],
        "analysis_endpoint": approval["environment"]["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"],
        "analysis_identity": analysis, "storage_identity": storage, "tenant_id": tenant, "subscription_id": subscription,
        "local_pdf": {"path": "/REPRODUCTION-ONLY/approved-ford.pdf", "sha256": source_hash, "bytes": 100},
        "history_hash_semantics": "sha256_sorted_compact_ascii_json", "history_sha256": historical,
        "storage_program_sha256": program["sha256"],
    }
    authorization = {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"], "approved_at": stamp,
        "contract": contract, "packet_sha256": real_pilot._sha256(packet),
        "parent_live_executor_only": True, "existing_operator_di_access_verified": True,
        "operator_analysis_api_storage_approved": True,
    }
    authorization_hash = real_pilot._sha256(authorization)
    write_json(store, prefix + "authorization.json", authorization)
    write_json(store, prefix + "ledger-before.json", ledger)
    before_hash = real_pilot._sha256(ledger)
    reservation_id = "d" * 64
    ledger["attempted"]["analysis"] += 1
    ledger["reserved"]["analysis_pages"] += 5
    ledger["reserved"]["microdollars"] += reserved_cost
    ledger["reservations"][reservation_id] = {
        "operation": "analysis", "execution_id": None, "reservation_id": reservation_id,
        "purpose": contract["purpose"], "item_key": "row-4", "item_keys": ["row-4", "row-5"],
        "preparation_authorization_sha256": authorization_hash,
        "reserved_usage": {"input_tokens": 0, "output_tokens": 0, "analysis_pages": 5},
        "actual_usage": None, "estimated_usage_cost_microdollars": None,
        "reserved_microdollars": reserved_cost, "status": "attempt_reserved_completion_unknown",
    }
    claim = {
        "packet_sha256": real_pilot._sha256(packet), "authorization_sha256": authorization_hash,
        "reservation_id": reservation_id, "reserved_microdollars": reserved_cost,
        "reserved_ledger_canonical_sha256": real_pilot._sha256(ledger),
        "source_sha256": source_hash, "source_bytes": 100, "source_etag": "synthetic-etag",
        "blob_verified_at": stamp, "analysis_identity": analysis, "storage_identity": storage,
        "readiness_clock_started": False,
    }
    charged = copy.deepcopy(ledger)
    write_json(store, prefix + "reserved.json", claim)
    write_json(store, prefix + "packet.json", packet)
    write_json(store, prefix + "storage-program.json", program)
    ledger["actual_usage"]["analysis_pages"] += actual_page_count
    ledger["estimated_usage_cost_microdollars"] += measured_cost
    ledger["reservations"][reservation_id].update(
        actual_usage={"input_tokens": 0, "output_tokens": 0, "analysis_pages": actual_page_count},
        estimated_usage_cost_microdollars=measured_cost, status="usage_reported_not_billing", recorded_at=stamp,
    )
    put(store, real_pilot.BUDGET_KEY, ledger)
    cache = real_pilot.FOUR_PRODUCT_PREP_CACHE_KEY
    parser_version = "prebuilt-layout:2024-11-30:mapping-v1"
    document = {"source": contract["source_location"], "cache_key": "sha256:" + source_hash,
                "raw_text": "REPRODUCTION ONLY: synthetic PREP record, not an actual parsed product."}
    receipt = {
        "outcome": "succeeded", "sdk_retries": 0, "request_options": {"pages": "1-5"},
        "source_sha256": source_hash, "source": contract["source_location"],
        "fresh_analysis": True, "legacy_local_parse_reused": False, "readiness_clock_started": False,
        "model_requested": "prebuilt-layout", "model_returned": "prebuilt-layout",
        "api_version_requested": "2024-11-30", "api_version_returned": "2024-11-30",
        "actual_page_count": actual_page_count, "returned_pages": list(range(1, actual_page_count + 1)),
        "reservation_id": reservation_id, "mapped_result_sha256": "f" * 64,
        "parser_version": parser_version, "source_bytes": 100,
        "operation_id": "12345678-1234-4234-8234-123456789abc",
        "sdk_package": "azure-ai-documentintelligence", "sdk_version": "1.0.2",
        "sdk_result_sha256": "c" * 64, "sdk_result_bytes": 100,
        "sdk_result_serialization": "AnalyzeResult.as_dict; sorted ASCII JSON; compact separators",
        "raw_http_response_retained": False, "preparation_authorization_sha256": authorization_hash,
        "cache_key": cache, "storage_identity": storage,
        "analysis_identity": {
            **analysis, "token_audience": "https://cognitiveservices.azure.com",
            "identity_checked_at": stamp, "sdk_token_identity_matched": True,
            "token_signature_validated_locally": False, "token_stored": False,
        },
        "input_provenance": {
            "kind": "approved_local_copy_equal_to_verified_blob", "local_path": packet["local_pdf"]["path"],
            "blob_source": contract["source_location"], "sha256": source_hash, "bytes": 100,
            "blob_etag": "synthetic-etag", "blob_verified_at": stamp,
        },
        **{name: stamp for name in ("started_at", "accepted_at", "completed_at", "mapped_at")},
    }
    write_json(store, prefix + "analysis-receipt.json", receipt)
    write_json(store, prefix + "parsed.json", document)
    write_json(store, prefix + "submitted.json", {
        "operation_id": receipt["operation_id"], "source_sha256": source_hash,
        "source_bytes": 100, "request_options": {"pages": "1-5"},
        "analysis_identity": receipt["analysis_identity"],
    })
    write_json(store, prefix + "attempt.json", {
        "authorization_sha256": authorization_hash, "ledger_before_canonical_sha256": before_hash,
        "contract": contract, "reservation_id": reservation_id, "reserved_at": stamp,
        "reserved_microdollars": reserved_cost, "source_bytes": 100,
        "source_etag": "synthetic-etag", "packet_sha256": real_pilot._sha256(packet),
        "historical_record_sha256": historical,
        "historical_record_hash_semantics": "sha256_sorted_compact_ascii_json",
    })
    envelope = {
        "parser_version": parser_version, "origin": "ford_preparation_analysis_first_five_pages",
        "analysis_receipt_key": prefix + "analysis-receipt.json", "document_sha256": "f" * 64,
        "document": document,
    }
    write_json(store, cache, envelope)
    write_json(store, prefix + "completed.json", {
        "status": "prepared_cache_verified_for_both_products", "worker_executions_charged": 0,
        "readiness_clock_started": False, "analysis_receipt": receipt, "cache_key": cache,
        "reserved_analysis": 1, "reserved_pages": 5, "reserved_microdollars": reserved_cost,
        "ford_extraction_authorized": False, "raw_hashes_are_capture_integrity_only": True,
        "ledger_after_canonical_sha256": real_pilot._sha256(ledger),
        "cache_canonical_sha256": real_pilot._sha256(envelope),
    })
    synthetic_continuation(store, historical, packet, authorization, claim, charged, stamp)
    return prefix + "completed.json"


def synthetic_continuation(store, historical, packet, original_auth, claim, charged, stamp):
    prefix = real_pilot.FOUR_PRODUCT_PREP_PREFIX
    continuation = real_pilot.FOUR_PRODUCT_PREP_CONTINUATION_PREFIX
    completed = read_json(store, prefix + "completed.json")[0]
    receipt = completed["analysis_receipt"]
    failure = {
        "status": "failed_or_unknown_no_retry", "allowance_refunded": False,
        "recorded_at": stamp, "submitted_transport_errors": [],
        "analysis_metadata": {
            "operation_id": None, "error_type": "ClientAuthenticationError",
            "source_sha256": receipt["source_sha256"], "source_bytes": receipt["source_bytes"],
            "request_options": {"pages": "1-5"},
        },
    }
    stopped = dict(historical)
    stopped[real_pilot.BUDGET_KEY] = real_pilot._sha256(charged)
    for name in ("attempt.json", "authorization.json", "ledger-before.json", "reserved.json", "packet.json", "storage-program.json"):
        stopped[prefix + name] = real_pilot._sha256(read_json(store, prefix + name)[0])
    program = "# REPRODUCTION ONLY: synthetic continuation storage audit.\n"
    proof = {
        "status": "validated_no_send", "provider": "document_intelligence",
        "sdk_package": "azure-ai-documentintelligence", "sdk_version": receipt["sdk_version"],
        "transport_package": "azure-core", "transport_version": "1.0.0",
        "method": "POST", "api_version": receipt["api_version_requested"],
        "content_type": "application/octet-stream", "source_sha256": receipt["source_sha256"],
        "body_sha256": receipt["source_sha256"], "source_bytes": receipt["source_bytes"],
        "body_bytes": receipt["source_bytes"], "transport": "in_memory_no_send", "captured_requests": 1,
        "transport_real_calls": 0, "network_calls": 0, "credential_real_calls": 0,
        "authentication_performed": False, "provider_send_performed": False, "provider_response_fabricated": False,
        "request_options": {"pages": "1-5"}, "pages": "1-5", "model_id": "prebuilt-layout",
        "path": "/documentintelligence/documentModels/prebuilt-layout:analyze",
        "query_is_complete": True, "query": {"api-version": [receipt["api_version_requested"]], "pages": ["1-5"]},
        **{key: "a" * 64 for key in ("endpoint_sha256", "request_sha256", "path_sha256", "query_sha256")},
    }
    code = {"backend/sdk_preflight.py": "a" * 64}
    plan = {
        "purpose": "one_continuation_of_existing_ford_reservation",
        "original_packet_sha256": real_pilot._sha256(packet),
        "original_authorization_sha256": real_pilot._sha256(original_auth),
        "claim_sha256": real_pilot._sha256(claim), "reservation_id": claim["reservation_id"],
        "failure": failure, "history_sha256": stopped, "history_hash_semantics": "sha256_sorted_compact_ascii_json",
        "continuation_program_sha256": hashlib.sha256(program.encode()).hexdigest(),
        "new_reservations": 0, "new_reserved_microdollars": 0, "max_submissions": 1, "sdk_retries": 0,
        "native_no_send_proof": proof, "local_code_sha256": code,
        "ci_proof": {"schema_version": 1, "status": "passed", "code_sha256": code, "source_revision": "f" * 40,
                     "checks": [{"name": "REPRODUCTION-ONLY native credential regression",
                                 "run_id": 1, "conclusion": "success"}]},
    }
    auth = {
        "schema_version": 1, "approved": True, "approved_by": original_auth["approved_by"], "approved_at": stamp,
        "continuation_plan_sha256": real_pilot._sha256(plan), "original_packet_sha256": real_pilot._sha256(packet),
        "reservation_id": claim["reservation_id"], "one_continuation_only": True, "no_new_reservation": True,
        "no_retry_or_refund": True, "parent_live_executor_only": True,
    }
    common = {
        "plan_sha256": real_pilot._sha256(plan), "authorization_sha256": real_pilot._sha256(auth),
        "reservation_id": claim["reservation_id"], "new_reservations": 0, "new_reserved_microdollars": 0,
        "readiness_clock_started": False,
    }
    records = {
        "plan.json": plan, "authorization.json": auth, "failure-before.json": failure,
        "program.json": {"source": program, "sha256": plan["continuation_program_sha256"]},
        "attempt.json": {**common, "state": "one_continuation_claimed_no_retry", "started_at": stamp,
                         "claim_sha256": real_pilot._sha256(claim),
                         "ledger_canonical_sha256": claim["reserved_ledger_canonical_sha256"]},
        "completed.json": {
            **common, "status": "existing_reservation_continuation_completed", "completed_at": stamp,
            "original_packet_sha256": real_pilot._sha256(packet),
            "original_failure_canonical_sha256": real_pilot._sha256(failure),
            "original_completed_canonical_sha256": real_pilot._sha256(completed),
            "cache_canonical_sha256": completed["cache_canonical_sha256"],
            "ledger_after_canonical_sha256": completed["ledger_after_canonical_sha256"],
            "operation_id": receipt["operation_id"], "worker_executions_charged": 0,
        },
    }
    for name, value in records.items():
        write_json(store, continuation + name, value)


@pytest.fixture
def four_case(row_case, monkeypatch, request):
    store, batch, approval, _ = row_case
    guard = start_row(row_case)
    for index, bound in enumerate((23000, 23000, 23000, 23146)):
        item = batch["items"][index // 2]
        key = guard.operation_key(item, tier="inference", source_version=f"four-prior-{index}", prompt_version="old")
        guard.reserve("inference", key, item_key=item["item_key"], max_input_tokens=bound, max_output_tokens=2048)
    for item in batch["items"][:2]:
        key = f"items/{batch['id']}/{item['item_key']}.json"
        state = read_json(store, key)[0]
        state["state"] = "unresolved"
        put(store, key, state)
        write_json(store, state["result_key"], read_json(store, f"results/{batch['id']}/{item['item_key']}.json")[0])
    receipt_key = synthetic_prep(store, actual_page_count=getattr(request, "param", 5))
    records = records_of(store)
    plan = [{
        "item_key": item["item_key"], "tier": tier, "input_tokens": 10000, "output_tokens": 2048,
        "payload_sha256": hashlib.sha256(f"{item['item_key']}:{tier}".encode()).hexdigest(),
    } for item in batch["items"] for tier in ("internal_pdf", "vendor_table", "manufacturer_web")]
    capacity = {
        **helper.capacity_request(records[real_pilot.BUDGET_KEY], approval, row_case[3], plan,
                                  duration_assumptions=duration_assumptions()),
        "approved": True, "approved_by": approval["approved_by"],
        "approved_at": datetime.now(timezone.utc).isoformat(), "receipt_id": "REPRODUCTION-ONLY-NOT-AUTHORITY",
    }
    configuration = helper.web_configuration(
        records, {item["item_key"]: {entry["attribute_id"]: entry["attribute_id"]
                                    for entry in item["manifest"]["attributes"]} for item in batch["items"]},
        {item["item_key"]: "Synthetic" for item in batch["items"]},
    )
    scope = helper.amendment_scope(
        records, capacity_approval=capacity, plan=plan, prep_receipt_key=receipt_key,
        fixed_cost_microdollars=helper.fixed_cost(records[receipt_key], approval), inference_interval_seconds=31,
        **configuration,
    )
    ready = max(datetime.now(timezone.utc), datetime.fromisoformat(row_case[3]["operating_expires_at"]) + timedelta(seconds=1))
    candidate = {
        **scope, "readiness_at": ready.isoformat(), "operating_expires_at": (ready + timedelta(seconds=5400)).isoformat(),
        "not_before": ready.isoformat(), "expires_at": (ready + timedelta(seconds=1200)).isoformat(),
    }
    write_json(store, real_pilot.FOUR_PRODUCT_KEY, candidate)
    runtime(monkeypatch, approval, candidate)
    return store, batch, approval, candidate


def begin(case):
    store, batch, _, candidate = case
    guard = real_pilot.RealPilotGuard(store, batch)
    assert guard.four_product == guard.active_recovery == candidate and guard.recovery is not None
    guard.before_execution("four-products-only-once")
    guard.prepare_recovery(lambda: None)
    assert guard.four_product == guard.active_recovery == candidate and guard.recovery is not None
    return guard


def reserve(guard, item, operation, index):
    key = guard.operation_key(item, tier=operation, source_version=str(index), prompt_version="four-reproduction")
    return guard.reserve(operation, key, item_key=item["item_key"],
                         max_input_tokens=10000 if operation == "inference" else 0,
                         max_output_tokens=2048 if operation == "inference" else 0,
                         analysis_pages=5 if operation == "analysis" else 0)


@pytest.mark.parametrize("web", [False, True])
def test_all_four_attempts_append_without_refund_or_analysis(four_case, web):
    store, batch, approval, candidate = four_case
    before = records_of(store)
    guard = begin(four_case)
    for index, item in enumerate(batch["items"]):
        state = read_json(store, f"items/{batch['id']}/{item['item_key']}.json")[0]
        assert state["previous_attempt"] == before[f"items/{batch['id']}/{item['item_key']}.json"]
        assert state["result_key"] == guard.result_key(item["item_key"])
        assert "/attempts/" in state["result_key"]
        reserve(guard, item, "inference", index * 3)
        reserve(guard, item, "inference", index * 3 + 1)
        if web:
            reserve(guard, item, "search", index)
            for page in range(candidate["page_limits"][item["item_key"]]):
                reserve(guard, item, "web_retrieval", index * 2 + page)
            reserve(guard, item, "inference", index * 3 + 2)
    after = records_of(store)
    ledger = after[real_pilot.BUDGET_KEY]
    assert ledger["attempted"]["analysis"] == 2 and ledger["reserved"]["analysis_pages"] == 10
    assert len(ledger["executions"]) == 5
    assert ledger["attempted"]["inference"] == (23 if web else 19)
    assert ledger["attempted"]["search"] == (4 if web else 0)
    assert ledger["attempted"]["web_retrieval"] == (6 if web else 0)
    assert ledger["reserved"]["output_tokens"] == 22528 + (24576 if web else 16384)
    assert all(ledger["reservations"][key] == value for key, value in before[real_pilot.BUDGET_KEY]["reservations"].items())
    for key, value in before.items():
        if key != real_pilot.BUDGET_KEY and not key.startswith("items/"):
            assert after[key] == value
    assert after[real_pilot.APPROVAL_KEY] == approval
    with pytest.raises((ValueError, Conflict)):
        guard.before_execution("no-retry-even-with-another-id")


@pytest.mark.parametrize("operation", ["analysis", "retrieval"])
def test_four_product_worker_can_never_analyze_or_sharepoint(four_case, operation):
    guard = begin(four_case)
    with pytest.raises(ValueError):
        reserve(guard, four_case[1]["items"][2], operation, 1)


def test_balanced_pages_and_per_product_request_limits(four_case):
    guard = begin(four_case)
    for item in four_case[1]["items"]:
        reserve(guard, item, "inference", 1)
        reserve(guard, item, "inference", 2)
        with pytest.raises(real_pilot.RealPilotBudgetExceeded):
            reserve(guard, item, "inference", 3)
        reserve(guard, item, "search", 1)
        with pytest.raises(real_pilot.RealPilotBudgetExceeded):
            reserve(guard, item, "search", 2)
        for index in range(four_case[3]["page_limits"][item["item_key"]]):
            reserve(guard, item, "web_retrieval", index)
        with pytest.raises(real_pilot.RealPilotBudgetExceeded):
            reserve(guard, item, "web_retrieval", 9)
        reserve(guard, item, "inference", 3)
        with pytest.raises(real_pilot.RealPilotBudgetExceeded):
            reserve(guard, item, "inference", 4)


def test_every_record_comparison_is_canonical_after_prep(four_case):
    store, batch, approval, candidate = four_case
    for key, (raw, version) in all_records(store).items():
        if key.endswith(".json"):
            store.write_bytes(key, json.dumps(json.loads(raw), indent=3, sort_keys=True).encode(), version)
    assert real_pilot.validate_four_product(
        candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store) == candidate
    begin(four_case)


@pytest.mark.parametrize("mutation", ["refund", "cached", "prep", "history", "capacity", "window", "pages"])
def test_drift_denied_without_consuming_worker(four_case, mutation):
    store, batch, approval, original = four_case
    candidate = copy.deepcopy(original)
    before = read_json(store, real_pilot.BUDGET_KEY)[0]
    if mutation == "refund":
        before["attempted"]["analysis"] -= 1
        put(store, real_pilot.BUDGET_KEY, before)
    elif mutation == "cached":
        key = next(iter(candidate["cached_documents"]))
        put(store, key, {"changed": True})
    elif mutation == "prep":
        put(store, candidate["prep_receipt_key"], {"status": "failed"})
    elif mutation == "history":
        key = next(iter(candidate["result_sha256"]))
        put(store, key, {"changed": True})
    elif mutation == "capacity":
        candidate["capacity_approval"]["additional_input_tokens"] += 1
    elif mutation == "window":
        candidate["operating_expires_at"] = candidate["expires_at"]
    else:
        candidate["page_limits"][batch["items"][0]["item_key"]] = 3
    with pytest.raises((ValueError, Conflict)):
        real_pilot.validate_four_product(candidate, approval, batch, before, store)
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == before


def test_full_worker_window_required_before_charge(four_case, monkeypatch):
    store, _, _, candidate = four_case
    before = read_json(store, real_pilot.BUDGET_KEY)[0]
    monkeypatch.setattr(real_pilot, "_now", lambda: datetime.fromisoformat(candidate["expires_at"]) - timedelta(seconds=599))
    with pytest.raises(ValueError, match="600-second"):
        begin(four_case)
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == before


def test_prior_build_is_counted_once_and_forecasts_are_not_stacked(four_case):
    store, _, approval, candidate = four_case
    assert helper.fixed_cost(read_json(store, candidate["prep_receipt_key"])[0], approval) == 226227
    assert helper.PRIOR_BUILD_USD == helper.Decimal("0.0232268896")


@pytest.mark.parametrize("configured", ["0.01"], indirect=True)
@pytest.mark.parametrize("four_case", [1], indirect=True)
def test_one_actual_prep_page_uses_actual_cost_without_refunding_reservation(four_case):
    store, batch, approval, candidate = four_case
    before = all_records(store)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    completed = read_json(store, candidate["prep_receipt_key"])[0]
    reservation = ledger["reservations"][completed["analysis_receipt"]["reservation_id"]]
    assert completed["analysis_receipt"]["actual_page_count"] == 1
    assert completed["reserved_pages"] == reservation["reserved_usage"]["analysis_pages"] == 5
    assert completed["reserved_microdollars"] == reservation["reserved_microdollars"] == 50000
    assert reservation["estimated_usage_cost_microdollars"] == 10000
    assert helper.fixed_cost(completed, approval) == candidate["fixed_cost_microdollars"] == 231227
    real_pilot.validate_four_product(candidate, approval, batch, ledger, store)
    assert all_records(store) == before
    with pytest.raises(ValueError, match="fixed forecast"):
        real_pilot.validate_four_product(
            {**candidate, "fixed_cost_microdollars": 231226}, approval, batch, ledger, store,
        )
    assert all_records(store) == before


def test_capacity_measurement_is_not_an_approval(four_case):
    candidate = four_case[3]
    assert candidate["capacity_approval"]["additional_inference"] == 7
    assert candidate["capacity_approval"]["additional_output_tokens"] == 14336
    assert candidate["capacity_approval"]["additional_input_tokens"] == 120000
    assert "additional_input_tokens" not in helper.POLICY


def test_complete_amendment_fits_bounded_console_without_network(four_case, monkeypatch):
    monkeypatch.setattr(helper.gap, "console", lambda *a, **k: pytest.fail("Offline frame check cannot contact Azure"))
    packet, body = helper.configuration_packet(four_case[2], four_case[3])
    code, _ = helper.row.remote_packet(packet, body)
    assert len(base64.b64encode(code.encode())) + 80 <= 16384


def pacing_fixture():
    order = ["row-2", "row-4", "row-3", "row-5"]
    plan, requests, web_delays = [], [], []
    clock, previous = 35, -31
    for key in order:
        for tier in ("internal_pdf", "vendor_table", "manufacturer_web"):
            if tier == "manufacturer_web":
                web_delays.append({"item_key": key, "start_seconds": clock, "end_seconds": clock + 37.5})
                clock += 37.5
            entry = {"item_key": key, "tier": tier, "input_tokens": 26000,
                     "output_tokens": 2048, "payload_sha256": hashlib.sha256(f"{key}:{tier}".encode()).hexdigest()}
            start = max(clock, previous + 31)
            clock, previous = start + 22, start
            plan.append(entry)
            requests.append({
                "item_key": key, "tier": tier, "input_byte_bound": 26000, "output_tokens": 2048,
                "payload_sha256": entry["payload_sha256"],
                "start_seconds": start, "end_seconds": clock,
            })
    proof = {
        "deployment_tpm": 30000, "quota_changed": False, "ledger_byte_bounds_preserved": True,
        "worker_timeout_seconds": 600, "basis": "empirical_provider_latency_conditional",
        "duration_assumptions": duration_assumptions(), "worker_elapsed_seconds": clock + 0.5, "requests": requests,
        "simulated_worker_elapsed_seconds": clock, "measured_local_worker_seconds": 0.5,
        "forecast_including_local_seconds": 548.5, "web_delay_events": web_delays,
        "local_measurement": "perf_counter_whole_real_worker_with_memory_store",
        "remote_storage_latency_measured": False,
        "web_allowance_is_hard_deadline": False, "phase_timeouts_bound_dns": False,
        "measured_item_local_seconds": {"row-2": 0.08, "row-3": 0.08, "row-4": 0.12, "row-5": 0.12},
        "measured_four_item_subtotal_seconds": 0.4, "measured_prior_two_item_subtotal_seconds": 0.16,
        "measured_shared_local_seconds": 0.1,
        "prior_two_comparison": "same_run_mueller_item_subtotal_not_historical_replay",
        "remaining_margin_seconds": 51.5,
        "provider_rate_estimate_verified": False, "byte_bounds_used_as_rate_estimate": False,
        "billing_usage_used_as_rate_estimate": False,
    }
    return proof, plan, order


def test_pacing_requires_empirical_assumptions_and_unchanged_byte_bounds():
    proof, plan, order = pacing_fixture()
    assert helper.validate_pacing(proof, plan, execution_order=order, interval_seconds=31) == proof


@pytest.mark.parametrize("changed", [
    "fixed61", "quota", "byte_bound", "mislabelled_basis", "invented_rate_estimate", "three_per_minute", "zero_latency",
    "startup_missing", "order", "forecast", "overhead_missing", "overhead_overrun", "overhead_undercharged",
    "web_delay_missing", "web_delay_unbalanced",
    "web_deadline_claim", "dns_deadline_claim", "historical_comparison_claim", "local_comparison_drift",
])
def test_readiness_denies_unproven_or_oversized_pacing(changed):
    proof, plan, order = pacing_fixture()
    if changed == "fixed61":
        for index, entry in enumerate(proof["requests"]):
            entry.update(start_seconds=35 + 61 * index, end_seconds=65 + 61 * index)
        proof["worker_elapsed_seconds"] = 736
    elif changed == "quota":
        proof["deployment_tpm"] = 60000
    elif changed == "byte_bound":
        proof["requests"][0]["input_byte_bound"] = 5000
    elif changed == "mislabelled_basis":
        proof["basis"] = "provider_rate_estimate"
    elif changed == "invented_rate_estimate":
        proof["provider_rate_estimate_verified"] = True
    elif changed == "three_per_minute":
        proof["requests"][2].update(start_seconds=94, end_seconds=124)
    elif changed == "zero_latency":
        proof["duration_assumptions"]["model_response_allowance_seconds"] = 0
    elif changed == "startup_missing":
        proof["duration_assumptions"]["startup_bookkeeping_seconds"] = 0
    elif changed == "forecast":
        proof["duration_assumptions"].update(web_network_allowance_seconds=225, forecast_seconds=623)
    elif changed == "overhead_missing":
        proof.pop("measured_local_worker_seconds")
    elif changed == "overhead_overrun":
        proof.update(measured_local_worker_seconds=52, forecast_including_local_seconds=600,
                     worker_elapsed_seconds=proof["simulated_worker_elapsed_seconds"] + 52)
    elif changed == "overhead_undercharged":
        proof["worker_elapsed_seconds"] -= 0.25
    elif changed == "web_delay_missing":
        proof["web_delay_events"].pop()
    elif changed == "web_delay_unbalanced":
        proof["web_delay_events"][0]["end_seconds"] -= 1
    elif changed == "web_deadline_claim":
        proof["web_allowance_is_hard_deadline"] = True
    elif changed == "dns_deadline_claim":
        proof["phase_timeouts_bound_dns"] = True
    elif changed == "historical_comparison_claim":
        proof["prior_two_comparison"] = "actual_historical_two_product_worker"
    elif changed == "local_comparison_drift":
        proof["measured_prior_two_item_subtotal_seconds"] += 0.1
    else:
        proof["requests"][3]["item_key"] = "row-3"
    with pytest.raises(ValueError):
        helper.validate_pacing(proof, plan, execution_order=order, interval_seconds=31)


def test_historical_61_second_interval_cannot_authorize_four_product_slice(four_case):
    store, batch, approval, candidate = four_case
    candidate = copy.deepcopy(candidate)
    candidate["inference_interval_seconds"] = 61
    with pytest.raises(ValueError, match="cannot fit"):
        real_pilot.validate_four_product(candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store)


@pytest.mark.parametrize("interval", [30, 32])
def test_only_the_explicit_four_scope_31_second_interval_is_allowed(four_case, interval):
    store, batch, approval, candidate = four_case
    candidate = copy.deepcopy(candidate)
    candidate["inference_interval_seconds"] = interval
    with pytest.raises(ValueError, match="exactly 31"):
        real_pilot.validate_four_product(candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store)


def test_page_allowances_are_balanced_in_interleaved_execution_order(four_case):
    candidate = four_case[3]
    assert candidate["execution_order"] == ["row-2", "row-4", "row-3", "row-5"]
    assert candidate["page_limits"] == {"row-2": 2, "row-4": 2, "row-3": 1, "row-5": 1}
    assert [candidate["public_web_policy"]["items"][key]["max_direct_page_attempts"]
            for key in candidate["execution_order"]] == [2, 2, 1, 1]


@pytest.mark.parametrize("order", [
    ["row-2", "row-3", "row-4", "row-5"],
    ["row-2", "row-5", "row-3", "row-4"],
    ["row-2", "row-4", "row-3", "row-3"],
])
def test_only_the_bound_interleaved_execution_order_is_allowed(four_case, order):
    store, batch, approval, candidate = four_case
    candidate = {**candidate, "execution_order": order}
    with pytest.raises(ValueError, match="interleave manufacturers"):
        real_pilot.validate_four_product(
            candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store,
        )


def test_historical_policy_normalizes_new_defaults_without_ignoring_changed_limits(gapfill_case, monkeypatch):
    from backend.core.websearch_policy import OptionalWebPolicy

    store, batch, _, candidate = gapfill_case
    before = all_records(store)
    assert all("max_direct_page_attempts" not in item for item in candidate["public_web_policy"]["items"].values())
    parsed = OptionalWebPolicy.model_validate(candidate["public_web_policy"])
    assert all(item.max_direct_page_attempts == 2 for item in parsed.items.values())
    monkeypatch.setattr("backend.core.websearch_policy.configured_optional_web_policy", lambda: parsed)
    guard = real_pilot.RealPilotGuard(store, batch)
    assert guard.four_product is None
    guard._validate(verify_runtime=True)
    assert all_records(store) == before
    changed = parsed.model_dump(mode="json")
    next(iter(changed["items"].values()))["max_direct_page_attempts"] = 1
    parsed = OptionalWebPolicy.model_validate(changed)
    with pytest.raises(ValueError, match="runtime public policy"):
        guard._validate(verify_runtime=True)
    assert all_records(store) == before


def test_duration_in_exact_owner_capacity_decision_cannot_drift():
    proof, plan, order = pacing_fixture()
    different = duration_assumptions()
    different.update(model_response_allowance_seconds=23, forecast_seconds=549)
    with pytest.raises(ValueError, match="exact owner capacity decision"):
        helper.validate_pacing(proof, plan, execution_order=order, interval_seconds=31,
                               duration_assumptions=different)


@pytest.mark.parametrize(("bookkeeping", "forecast"), [(50, 563), (70, 583)])
def test_larger_measured_bookkeeping_can_be_explicitly_owner_approved(bookkeeping, forecast):
    assumptions = duration_assumptions()
    assumptions.update(startup_bookkeeping_seconds=bookkeeping, forecast_seconds=forecast)
    assert real_pilot.validate_four_product_duration(assumptions) == assumptions


def test_long_response_allowances_are_serial_not_hidden_by_start_spacing():
    assumptions = duration_assumptions()
    assumptions.update(model_response_allowance_seconds=45, forecast_seconds=725)
    with pytest.raises(ValueError, match="does not fit"):
        real_pilot.validate_four_product_duration(assumptions)


def test_pending_duration_cannot_become_a_capacity_approval(four_case):
    store, batch, approval, candidate = four_case
    candidate = copy.deepcopy(candidate)
    candidate["capacity_approval"]["duration_assumptions"] = None
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    with pytest.raises(ValueError, match="empirical and conditional"):
        real_pilot.validate_four_product(candidate, approval, batch, ledger, store)
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == ledger


def test_any_prep_failure_record_blocks_even_an_existing_completed_record(four_case):
    store, batch, approval, candidate = four_case
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    write_json(store, real_pilot.FOUR_PRODUCT_PREP_PREFIX + "failure.json", {
        "status": "failed_or_unknown_no_retry", "allowance_refunded": False,
    })
    with pytest.raises(ValueError, match="PREP failed or has an unknown outcome"):
        real_pilot.validate_four_product(candidate, approval, batch, ledger, store)
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == ledger


def test_every_prep_stage_is_required_and_pin_bound(four_case):
    candidate = four_case[3]
    assert set(real_pilot.FOUR_PRODUCT_PREP_RECORDS) <= set(candidate["historical_records"])
    assert real_pilot.FOUR_PRODUCT_PREP_CACHE_KEY in candidate["cached_documents"]


@pytest.mark.parametrize(("name", "field", "replacement", "message"), [
    ("analysis-receipt", "model_requested", "prebuilt-read", "actual bounded PREP provenance"),
    ("analysis-receipt", "api_version_requested", "2023-07-31", "actual bounded PREP provenance"),
    ("analysis-receipt", "outcome", "submission_outcome_unknown", "actual bounded PREP provenance"),
    ("analysis-receipt", "returned_pages", [2, 3, 4, 5, 6], "retain every charge"),
    ("analysis-receipt", "sdk_result_sha256", "not-a-hash", "operation lineage changed"),
    ("analysis-receipt", "operation_id", "12345678-1234-4234-8234-123456789abb", "operation lineage changed"),
    ("completed", "ledger_after_canonical_sha256", "0" * 64, "actual bounded PREP provenance"),
    ("completed", "cache_canonical_sha256", "0" * 64, "cache/provenance association changed"),
    ("completed", "ford_extraction_authorized", True, "actual bounded PREP provenance"),
    ("attempt", "ledger_before_canonical_sha256", "0" * 64, "operation lineage changed"),
    ("authorization", "existing_operator_di_access_verified", False, "operation lineage changed"),
    ("submitted", "operation_id", "12345678-1234-4234-8234-123456789abb", "operation lineage changed"),
    ("parsed", "source", "https://unrelated.invalid/source.pdf", "cache/provenance association changed"),
    ("analysis-receipt", "mapped_at", "after-readiness", "completion must precede measured capacity"),
    ("analysis-receipt", "analysis_identity.credential", "ManagedIdentityCredential", "operator DI and API-MI storage"),
    ("analysis-receipt", "analysis_identity.token_stored", True, "operator DI and API-MI storage"),
    ("analysis-receipt", "analysis_identity.token_signature_validated_locally", True, "operator DI and API-MI storage"),
    ("analysis-receipt", "storage_identity.kind", "approved_operator", "operator DI and API-MI storage"),
    ("analysis-receipt", "input_provenance.blob_etag", "wrong-etag", "local input must equal"),
    ("reserved", "reserved_ledger_canonical_sha256", "0" * 64, "reservation preceded operator analysis"),
    ("storage-program", "source", "# Changed program", "storage-only program"),
])
def test_prep_provenance_cannot_be_replaced_by_self_consistent_history_pins(
    four_case, name, field, replacement, message,
):
    store, batch, approval, candidate = four_case
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    candidate = copy.deepcopy(candidate)
    if replacement == "after-readiness":
        replacement = (datetime.fromisoformat(candidate["readiness_at"]) + timedelta(seconds=1)).isoformat()
    key = real_pilot.FOUR_PRODUCT_PREP_PREFIX + name + ".json"
    changed = read_json(store, key)[0]
    target = changed
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = replacement
    put(store, key, changed)
    completed_key = real_pilot.FOUR_PRODUCT_PREP_PREFIX + "completed.json"
    if name == "analysis-receipt":
        completed = read_json(store, completed_key)[0]
        completed["analysis_receipt"] = changed
        put(store, completed_key, completed)
    for key in candidate["historical_records"]:
        candidate["historical_records"][key] = real_pilot._sha256(read_json(store, key)[0])
    candidate["prep_receipt_sha256"] = candidate["historical_records"][completed_key]
    with pytest.raises(ValueError, match=message):
        real_pilot.validate_four_product(candidate, approval, batch, ledger, store)
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == ledger


@pytest.mark.parametrize("key", real_pilot.FOUR_PRODUCT_PREP_RECORDS + real_pilot.FOUR_PRODUCT_PREP_CONTINUATION_RECORDS)
def test_prep_stage_cannot_be_omitted_from_authority(four_case, key):
    store, batch, approval, candidate = four_case
    candidate = copy.deepcopy(candidate)
    candidate["historical_records"].pop(key)
    with pytest.raises(ValueError, match="lineage must remain pinned"):
        real_pilot.validate_four_product(
            candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store,
        )


@pytest.mark.parametrize("name,field,value", [
    ("completed", "new_reservations", 1),
    ("completed", "original_completed_canonical_sha256", "0" * 64),
    ("completed", "operation_id", None),
    ("attempt", "new_reserved_microdollars", 50000),
    ("authorization", "no_new_reservation", False),
    ("failure-before", "allowance_refunded", True),
])
def test_continuation_audit_cannot_rearm_or_fabricate_completion(four_case, name, field, value):
    store, batch, approval, candidate = four_case
    candidate = copy.deepcopy(candidate)
    before = read_json(store, real_pilot.BUDGET_KEY)[0]
    key = real_pilot.FOUR_PRODUCT_PREP_CONTINUATION_PREFIX + name + ".json"
    changed = read_json(store, key)[0]
    changed[field] = value
    put(store, key, changed)
    candidate["historical_records"][key] = real_pilot._sha256(changed)
    with pytest.raises(ValueError):
        real_pilot.validate_four_product(candidate, approval, batch, before, store)
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == before


@pytest.mark.parametrize("fault", [
    "extra-proof-field", "missing-proof-version", "invalid-proof-version", "bool-captured", "bool-network",
    "missing-ci-schema", "bool-ci-schema", "extra-ci-field", "missing-check-run-id",
    "bool-check-run-id", "empty-check-name", "extra-check-field",
])
def test_continuation_native_and_ci_schemas_survive_rehashed_plan(four_case, fault):
    store, batch, approval, candidate = four_case
    candidate = copy.deepcopy(candidate)
    prefix = real_pilot.FOUR_PRODUCT_PREP_CONTINUATION_PREFIX
    before = read_json(store, real_pilot.BUDGET_KEY)[0]
    plan = read_json(store, prefix + "plan.json")[0]
    proof, ci = plan["native_no_send_proof"], plan["ci_proof"]
    if fault == "extra-proof-field":
        proof["source"] = "REPRODUCTION-ONLY raw data is not a metadata receipt"
    elif fault == "missing-proof-version":
        proof.pop("transport_version")
    elif fault == "invalid-proof-version":
        proof["transport_version"] = ""
    elif fault == "bool-captured":
        proof["captured_requests"] = True
    elif fault == "bool-network":
        proof["network_calls"] = False
    elif fault == "missing-ci-schema":
        ci.pop("schema_version")
    elif fault == "bool-ci-schema":
        ci["schema_version"] = True
    elif fault == "extra-ci-field":
        ci["unreviewed"] = True
    elif fault == "missing-check-run-id":
        ci["checks"][0].pop("run_id")
    elif fault == "bool-check-run-id":
        ci["checks"][0]["run_id"] = True
    elif fault == "empty-check-name":
        ci["checks"][0]["name"] = ""
    elif fault == "extra-check-field":
        ci["checks"][0]["unreviewed"] = True
    put(store, prefix + "plan.json", plan)
    authorization = read_json(store, prefix + "authorization.json")[0]
    authorization["continuation_plan_sha256"] = real_pilot._sha256(plan)
    put(store, prefix + "authorization.json", authorization)
    for name in ("attempt.json", "completed.json"):
        record = read_json(store, prefix + name)[0]
        record.update(plan_sha256=real_pilot._sha256(plan),
                      authorization_sha256=real_pilot._sha256(authorization))
        put(store, prefix + name, record)
    for key in candidate["historical_records"]:
        candidate["historical_records"][key] = real_pilot._sha256(read_json(store, key)[0])
    with pytest.raises(ValueError, match="Exact installed native DI no-send proof and focused CI"):
        real_pilot.validate_four_product(candidate, approval, batch, before, store)
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == before


def test_prefixed_owner_identity_and_role_records_are_finitely_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    for name in helper.RETAINED_FILES:
        helper.release.save_once(tmp_path / name, {"label": "REPRODUCTION ONLY", "record": name})
    helper.release.save_once(helper.path(tmp_path, "baseline"), {"generated": True})
    retained = helper.history(tmp_path)
    assert set(retained) == set(helper.RETAINED_FILES)
    assert helper.PREPARATION_IDENTITY_AUTHORIZATION_FILE in retained
    assert helper.EXISTING_ROLE_EVIDENCE_FILE in retained
    helper.release.save_once(tmp_path / "later-unrelated.json", {"does_not_freeze_directory": True})
    candidate = tmp_path / helper.EXISTING_ROLE_EVIDENCE_FILE
    candidate.write_text(json.dumps(helper.release.private_json(candidate), indent=4, sort_keys=True))
    helper.verify_history(tmp_path, retained)


@pytest.mark.parametrize("changed", [None, "worker_grant", "api_grant", "operator_blob", "grant_request", "scope"])
def test_split_identity_capture_preserves_existing_grants_without_iam_changes(configured, tmp_path, monkeypatch, changed):
    _, _, approval = configured
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    config = {"subscription": "00000000-0000-4000-8000-000000000003", "group": "synthetic"}
    account = helper.urlsplit(approval["environment"]["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]).hostname.split(".")[0]
    capture = {
        "schema_version": 1, "approved": True, "record_actual_analysis_identity_in_provenance": True,
        "source_sha256": "b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa",
        "pages": "1-5", "max_analysis_submissions": 1, "worker_executions_for_preparation": 0,
        "permission_grants": 0, "identity_attachments": 0, "deployments": 0, "counter_resets": 0,
        "readiness_clock_started": False, "all_other_authority": "Unchanged from " + helper.OWNER_AUTHORIZATION_FILE,
        "recorded_after_confirmation_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    }
    roles = {
        "role": "Cognitive Services User", "role_definition_id": "a97b65f3-24c7-4388-baec-2e87135dc908",
        "document_intelligence_scope": (f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}"
                                        f"/providers/Microsoft.CognitiveServices/accounts/{account}"),
        "permissions_changed": False, "identity_attachments_changed": False,
        "identities": {
            "worker": {"principal_id": approval["identities"]["worker_principal_id"],
                       "matching_unconditional_role_exists": True, "new_grant_needed_for_worker_analysis": False},
            "api": {"principal_id": approval["identities"]["api_principal_id"], "matching_unconditional_role_exists": False,
                    "assignments_returned_for_this_principal_at_or_above_di_scope": 0},
            "approved_operator": {"principal_id": approval["approved_by"], "matching_unconditional_role_exists": True,
                                  "storage_blob_data_grant_observed": False},
        },
    }
    if changed == "worker_grant":
        roles["identities"]["worker"]["new_grant_needed_for_worker_analysis"] = True
    elif changed == "api_grant":
        roles["identities"]["api"]["matching_unconditional_role_exists"] = True
    elif changed == "operator_blob":
        roles["identities"]["approved_operator"]["storage_blob_data_grant_observed"] = True
    elif changed == "grant_request":
        capture["permission_grants"] = 1
    elif changed == "scope":
        roles["document_intelligence_scope"] += "-wrong"
    helper.release.save_once(tmp_path / helper.PREPARATION_IDENTITY_AUTHORIZATION_FILE, capture)
    helper.release.save_once(tmp_path / helper.EXISTING_ROLE_EVIDENCE_FILE, roles)
    if changed:
        with pytest.raises(ValueError):
            helper.preparation_identity_authorization(tmp_path, approval, config)
    else:
        assert helper.preparation_identity_authorization(tmp_path, approval, config) == (capture, roles)


@pytest.mark.parametrize("phase,elapsed,minimum,allowed", [
    ("publication", 1800, 900, True), ("publication", 1801, 900, False),
    ("processing", 600, 600, True), ("processing", 601, 600, False),
])
def test_operator_fixed_full_windows(tmp_path, monkeypatch, phase, elapsed, minimum, allowed):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    decision = {"target": "synthetic"}
    anchor = datetime.now(timezone.utc)
    readiness = {**helper.binding(decision), "readiness_at": anchor.isoformat(),
                 "publication_expires_at": (anchor + timedelta(seconds=2700)).isoformat(),
                 "overall_expires_at": (anchor + timedelta(seconds=5400)).isoformat(),
                 "model_capacity": {"tokens_per_minute": 30000},
                 "model_capacity_checked_at": anchor.isoformat(), "quota_changed": False,
                 "deployment_no_send": {"status": "validated", "no_send": True}}
    helper.release.save_once(helper.path(tmp_path, "readiness"), readiness)
    helper.release.save_once(helper.path(tmp_path, "readiness-pin"), {**helper.binding(decision), "sha256": helper.sha(readiness)})
    helper.release.save_once(helper.path(tmp_path, "deployed"), {
        **helper.binding(decision), "processing_expires_at": (anchor + timedelta(seconds=1200)).isoformat(),
    })
    monkeypatch.setattr(helper, "now", lambda: anchor + timedelta(seconds=elapsed))
    if allowed:
        helper.window(tmp_path, decision, phase, minimum)
    else:
        with pytest.raises(ValueError):
            helper.window(tmp_path, decision, phase, minimum)
    helper.release.save_once(helper.path(tmp_path, "closed"), helper.binding(decision))
    with pytest.raises(ValueError, match="closed"):
        helper.window(tmp_path, decision, phase, 0)
    assert helper.window(tmp_path, decision, "cleanup") == {}


def test_source_review_allows_exact_track_b_and_pacing_but_not_unreviewed_files(monkeypatch):
    files = {
        "backend/evidence_verification.py": b"reviewed-B",
        "backend/batch_worker.py": b"reviewed-pacing",
        "backend/real_pilot.py": b"reviewed-four-products",
        "scripts/azure_write_schema.py": b"reviewed-schema",
    }
    review = {
        "approved": True, "revision": "b" * 40, "base_revision": "a" * 40, "tree": "c" * 40,
        "workstreams": ["track_b", "four_product", "write_schema", "pacing"],
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
    }

    def command(arguments, **kwargs):
        if arguments[1] == "diff":
            return "\n".join(files).encode()
        if arguments[1] == "show":
            return files[arguments[2].split(":", 1)[1]]
        if arguments[1] == "rev-parse":
            return ("c" * 40).encode()
        return b""

    monkeypatch.setattr(helper.release, "command", command)
    assert helper.source_boundary("b" * 40, review) == review
    files["backend/unreviewed.py"] = b"unreviewed"
    with pytest.raises(ValueError, match="entire changed"):
        helper.source_boundary("b" * 40, review)


@pytest.mark.parametrize("transport", ["azure", "upload_publication_context"])
@pytest.mark.parametrize("blocked", [None, "existing", "four"])
def test_publication_window_composes_after_native_preflight(tmp_path, monkeypatch, transport, blocked):
    events = []

    def window(work, decision, phase, minimum=0):
        assert phase == "publication"
        events.append(("window", minimum))
        if "native" in events and blocked == "four":
            raise ValueError("Four-product clock expired during no-send")

    def existing_window():
        events.append("existing")
        if blocked == "existing":
            raise ValueError("Existing publication clock expired during no-send")

    def native_transport(*args, before_send=None, **kwargs):
        events.append("native")
        before_send()
        events.append("send")

    monkeypatch.setattr(helper, "window", window)
    monkeypatch.setattr(helper.release, transport, native_transport)
    arguments = ("rest", "--method", "POST") if transport == "azure" else ({}, "synthetic-archive", 900)
    if blocked:
        with pytest.raises(ValueError, match="clock expired"):
            with helper.live(tmp_path, {}, "publication"):
                getattr(helper.release, transport)(*arguments, before_send=existing_window)
        assert "send" not in events
    else:
        with helper.live(tmp_path, {}, "publication"):
            getattr(helper.release, transport)(*arguments, before_send=existing_window)
        assert events == [("window", 0), ("window", 900), "native", "existing", ("window", 900), "send"]
        events.clear()
        with helper.live(tmp_path, {}, "publication"):
            getattr(helper.release, transport)(*arguments)
        assert events == [("window", 0), ("window", 900), "native", ("window", 900), "send"]
    assert getattr(helper.release, transport) is native_transport


def test_publication_read_keeps_existing_read_only_call_shape(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(helper, "window", lambda *args: None)
    monkeypatch.setattr(helper.release, "azure", lambda *args, **kwargs: calls.append((args, kwargs)))
    with helper.live(tmp_path, {}, "publication"):
        helper.release.azure("containerapp", "show", "--name", "synthetic")
    assert calls == [(("containerapp", "show", "--name", "synthetic"), {})]


def deployment_proof(stage):
    """Synthetic unit transport metadata; never a private-gate or live proof."""
    return {"status": "validated", "no_send": True, "operation_stage": stage,
            "provider_response_fabricated": stage in {
                "azure_write", "publication_upload_metadata", "publication_schedule_run",
            }, "reproduction_only": True}


def test_preflight_records_are_immutable_private_and_decision_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    decision = {"target": "synthetic"}
    request_binding = {"resource_sha256": "a" * 64, "payload_sha256": "b" * 64}
    record = helper.preflight_recorder(tmp_path, decision, "deploy-worker", request_binding)
    original_binding = copy.deepcopy(request_binding)
    request_binding["payload_sha256"] = "c" * 64
    proof = deployment_proof("worker_secret_redacted")
    record(proof)
    proof["reproduction_only"] = "changed after callback"
    name = "deploy-worker-worker_secret_redacted-preflight"
    stored = helper.receipt(tmp_path, name, decision)
    assert stored["request_binding"] == original_binding
    assert stored["native_no_send"]["reproduction_only"] is True
    assert stored["native_no_send"]["provider_response_fabricated"] is False
    assert helper.path(tmp_path, name).stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already attempted"):
        record(deployment_proof("worker_secret_redacted"))
    record(deployment_proof("worker_secret_credential_bound"))
    assert helper.receipt(tmp_path, "deploy-worker-worker_secret_credential_bound-preflight", decision)
    assert len(list(tmp_path.iterdir())) == 2
    with pytest.raises(ValueError, match="persistence failed; no request sent"):
        helper.release._emit_preflight(record, deployment_proof("worker_secret_redacted"), "worker_secret_redacted")


@pytest.mark.parametrize(("field", "value"), [
    ("status", "failed"), ("no_send", False), ("operation_stage", "../outside"),
])
def test_failed_or_unknown_stage_cannot_be_retained_as_native_proof(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    record = helper.preflight_recorder(tmp_path, {"target": "synthetic"}, "enable", {"payload_sha256": "a" * 64})
    proof = deployment_proof("azure_write")
    proof[field] = value
    with pytest.raises(ValueError):
        record(proof)
    assert not list(tmp_path.iterdir())


def test_publication_retains_each_actual_dynamic_boundary(tmp_path, monkeypatch):
    from contextlib import nullcontext

    decision = {"target": "synthetic", "source_revision": "a" * 40}
    previous = {"frontend_image": "unchanged"}
    stages = ("publication_upload_metadata", "binary_source_upload", "publication_schedule_run")
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    monkeypatch.setattr(helper.release, "pilot_lock", lambda *args: nullcontext())
    monkeypatch.setattr(helper, "live", lambda *args: nullcontext())
    monkeypatch.setattr(helper, "validate", lambda *args: ({}, previous))
    monkeypatch.setattr(helper.release, "verify_target", lambda *args: None)
    monkeypatch.setattr(helper.release, "identity_contract", lambda *args: None)
    monkeypatch.setattr(helper, "mixed_resources", lambda *args: ({}, {}, {}))
    monkeypatch.setattr(helper.release, "require_real_pilot_off", lambda *args: None)
    monkeypatch.setattr(helper.gap, "credential_binding", lambda *args, **kwargs: None)
    monkeypatch.setattr(helper, "remote_snapshot", lambda *args: None)
    monkeypatch.setattr(helper.prior, "require_model_capacity", lambda *args: {"tokens_per_minute": 30000})
    monkeypatch.setattr(helper, "window", lambda *args: {
        "publication_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=2700)).isoformat(),
    })

    def execute(config, work, kind, revision, attempt, *, expires, validate_upload_window, on_preflight):
        assert attempt.is_file() and kind == "backend" and revision == decision["source_revision"]
        assert validate_upload_window is True
        for stage in stages:
            on_preflight(deployment_proof(stage))
        return {"status": "Succeeded", "digest": "sha256:" + "b" * 64}

    monkeypatch.setattr(helper.release, "execute_publication", execute)
    helper.publish(tmp_path, {"registry": "synthetic"}, decision)
    attempt = helper.release.private_json(helper.path(tmp_path, "backend-attempt"))
    for stage in stages:
        stored = helper.receipt(tmp_path, "backend-publication-" + stage + "-preflight", decision)
        assert stored["request_binding"] == {"attempt_sha256": helper.sha(attempt)}
        assert stored["native_no_send"]["operation_stage"] == stage


def test_expiry_stop_retains_proof_for_only_the_reserved_execution(tmp_path, monkeypatch):
    decision = {"target": "synthetic"}
    config = {"subscription": "synthetic", "group": "synthetic", "job": "synthetic"}
    execution = "synthetic-reserved-execution"
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    helper.release.save_once(helper.path(tmp_path, "worker-result"), {
        **helper.binding(decision), "execution_name": execution,
    })
    monkeypatch.setattr(helper, "amendment", lambda *args: {"expires_at": "2000-01-01T00:00:00+00:00"})

    def stop(*args, on_preflight):
        assert args[:3] == ("containerapp", "job", "stop") and args[-2:] == ("--job-execution-name", execution)
        assert helper.path(tmp_path, "expiry-stop-attempt").is_file()
        on_preflight(deployment_proof("azure_write"))
        stored = helper.receipt(tmp_path, "expiry-stop-azure_write-preflight", decision)
        assert stored["request_binding"] == {
            "attempt_sha256": helper.sha(helper.receipt(tmp_path, "expiry-stop-attempt", decision)),
            "execution_name_sha256": helper.sha(execution),
        }

    monkeypatch.setattr(helper.release, "azure", stop)
    with pytest.raises(ValueError, match="only the reserved execution was stopped"):
        helper.observe(tmp_path, config, decision)


def test_operator_write_reuses_authoritative_schema_before_transport(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from scripts import azure_write_schema

    tmp_path.chmod(0o700)
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    decision = {"target": "synthetic"}
    resource = {
        "id": "/subscriptions/sub/resourceGroups/group/providers/Microsoft.App/containerApps/backend",
        "location": "eastus",
        "properties": {"template": {"containers": [{
            "name": "backend", "image": "synthetic@sha256:" + "a" * 64,
            "resources": {"cpu": 1, "memory": "2Gi"},
        }]}},
    }
    containers = copy.deepcopy(resource["properties"]["template"]["containers"])
    containers[0]["image"] = "synthetic@sha256:" + "b" * 64
    updated = copy.deepcopy(resource)
    updated["properties"]["template"]["containers"] = containers
    values = iter([resource, updated])
    sent = []
    monkeypatch.setattr(helper, "live", lambda *a, **k: nullcontext())
    monkeypatch.setattr(helper.release, "app", lambda *a, **k: next(values))
    monkeypatch.setattr(helper.prior, "shape", lambda value: copy.deepcopy(value["properties"]))
    def send(*args, on_preflight, **kwargs):
        on_preflight(deployment_proof("azure_write"))
        sent.append(args)

    monkeypatch.setattr(helper.release, "azure", send)
    helper.patch(tmp_path, {"backend": "backend"}, decision, "deploy-backend", resource, containers)
    payload = helper.release.private_json(helper.path(tmp_path, "deploy-backend-patch"))
    assert payload == azure_write_schema.build_payload(
        "containerApps", location="eastus", properties={"template": {"containers": containers}})
    assert len(sent) == 1 and sent[0][:3] == ("rest", "--method", "PATCH")
    native = helper.receipt(tmp_path, "deploy-backend-azure_write-preflight", decision)
    assert native["request_binding"] == {"resource_sha256": helper.sha(resource), "payload_sha256": helper.sha(payload)}
    assert native["native_no_send"]["provider_response_fabricated"] is True


def test_operator_rejects_unknown_write_before_transport(tmp_path, monkeypatch):
    from contextlib import nullcontext

    monkeypatch.setattr(helper, "live", lambda *a, **k: nullcontext())
    resource = {"properties": {"template": {"containers": [{"name": "backend", "image": "synthetic"}]}}}
    monkeypatch.setattr(helper.release, "app", lambda *a, **k: resource)
    monkeypatch.setattr(helper.release, "azure", lambda *a, **k: pytest.fail("Invalid write reached transport"))
    with pytest.raises(ValueError):
        helper.patch(tmp_path, {"backend": "backend"}, {}, "deploy-backend", resource,
                     [{"name": "backend", "image": "synthetic", "unexpected_readback_field": True}])
    assert not list(tmp_path.iterdir())


def test_operator_secret_binding_uses_namespace_independent_schema_transport(tmp_path, monkeypatch):
    from contextlib import nullcontext

    decision = {"target": "synthetic", "credential_source": "owner_env_file", "webiq_secret_ref": "existing-owner-key"}
    resource = {
        "id": "/subscriptions/00000000-0000-4000-8000-000000000001/resourceGroups/group/providers/Microsoft.App/jobs/worker",
        "location": "eastus", "identity": {"type": "SystemAssigned"},
        "properties": {
            "template": {"containers": [{"name": "worker", "image": "old"}]},
            "configuration": {
                "secrets": [], "triggerType": "Manual", "replicaTimeout": 600, "replicaRetryLimit": 0,
                "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
            },
        },
    }
    containers = [{"name": "worker", "image": "new"}]
    updated = copy.deepcopy(resource)
    updated["properties"]["template"]["containers"] = containers
    updated["properties"]["configuration"]["secrets"] = [{"name": decision["webiq_secret_ref"]}]
    resources = iter([resource, updated])
    calls = []
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    monkeypatch.setattr(helper, "live", lambda *a, **k: nullcontext())
    monkeypatch.setattr(helper.release, "active_executions", lambda *a: (next(resources), []))
    monkeypatch.setattr(helper.prior, "shape", lambda value: copy.deepcopy(value["properties"]))
    monkeypatch.setattr(helper, "window", lambda work, scope, phase, seconds: calls.append((work, scope, phase, seconds)))
    monkeypatch.setattr(helper.release, "azure", lambda *a, **k: pytest.fail("Unexpected direct write transport"))
    monkeypatch.setattr(helper.gap, "secure_worker_patch", lambda *a, **k: pytest.fail("Old namespace wrapper used"))

    def send(config, observed, redacted, *, secret_ref, check_window, on_preflight):
        assert observed == resource and secret_ref == decision["webiq_secret_ref"]
        assert redacted["properties"]["configuration"]["secrets"] == [{"name": secret_ref}]
        assert "location" not in redacted
        for stage in ("worker_secret_redacted", "worker_secret_credential_bound"):
            on_preflight(deployment_proof(stage))
            check_window()

    monkeypatch.setattr(helper.gap, "send_existing_secret_patch", send)
    helper.patch(tmp_path, {}, decision, "deploy-worker", resource, containers,
                 job=True, bind_existing_secret=decision["webiq_secret_ref"])
    assert calls == [(tmp_path, decision, "overall", 600)] * 2
    assert helper.release.private_json(helper.path(tmp_path, "credential-binding"))["value_persisted_locally"] is False
    for stage in ("worker_secret_redacted", "worker_secret_credential_bound"):
        native = helper.receipt(tmp_path, "deploy-worker-" + stage + "-preflight", decision)
        assert native["request_binding"]["resource_sha256"] == helper.sha(resource)


@pytest.mark.parametrize("tokens_per_minute", [30000, 29999, 60000, None])
def test_current_quota_is_checked_before_any_readiness_clock(tmp_path, monkeypatch, tokens_per_minute):
    from contextlib import nullcontext

    calls = []
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    monkeypatch.setattr(helper.release, "pilot_lock", lambda *a: nullcontext())
    monkeypatch.setattr(helper, "validate", lambda *a: ({}, {"target": "approved"}))
    monkeypatch.setattr(helper.release, "verify_target", lambda config: calls.append("target"))
    job = {"properties": {"template": {
        "containers": [{"name": "worker", "image": "synthetic"}], "initContainers": None,
        "volumes": [{"name": "existing", "storageType": "EmptyDir"}],
    }}}
    captured = copy.deepcopy(job)
    monkeypatch.setattr(helper, "mixed_resources", lambda *args: ({}, {}, job))

    def native(config, backend, worker, template):
        assert worker == captured and job == captured
        assert template == {"containers": captured["properties"]["template"]["containers"]}
        calls.append("native")
        return {"status": "validated", "no_send": True}

    monkeypatch.setattr(helper.release, "preflight_deployment", native)

    def capacity(*args):
        calls.append("quota")
        if tokens_per_minute is None:
            raise ValueError("Current quota unavailable")
        return {"tokens_per_minute": tokens_per_minute}

    monkeypatch.setattr(helper.prior, "require_model_capacity", capacity)
    decision = {"target": "synthetic"}
    if tokens_per_minute != 30000:
        with pytest.raises(ValueError):
            helper.ready(tmp_path, {}, decision)
        assert not list(tmp_path.iterdir())
    else:
        value = helper.ready(tmp_path, {}, decision)
        assert value["model_capacity"] == {"tokens_per_minute": 30000}
        assert value["model_capacity_checked_at"] == value["readiness_at"]
        assert value["quota_changed"] is False
        with pytest.raises(ValueError, match="create-once"):
            helper.ready(tmp_path, {}, decision)
    assert calls == ["target", "native", "quota"]
    assert job == captured


@pytest.mark.parametrize("unknown_root", [False, True])
def test_start_uses_execution_template_schema_without_mutating_job(tmp_path, monkeypatch, unknown_root):
    from scripts import azure_write_schema

    config = {"subscription": "synthetic-subscription", "group": "synthetic-group", "job": "synthetic-worker"}
    approval = {"batch_id": "synthetic-batch", "environment": {}}
    candidate = {"web_environment": {}, "public_web_policy": {"enabled": True}}
    job = {"properties": {"template": {
        "containers": [{"name": "worker", "image": "synthetic", "resources": {"cpu": 1, "memory": "2Gi"}}],
        "initContainers": None, "volumes": [{"name": "existing", "storageType": "EmptyDir"}],
    }}}
    if unknown_root:
        job["properties"]["template"]["unreviewed"] = True
    captured = copy.deepcopy(job)
    calls = []
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    monkeypatch.setattr(helper, "validate", lambda *args: (approval, config))
    monkeypatch.setattr(helper, "active_target", lambda *args: config)
    monkeypatch.setattr(helper, "amendment", lambda *args: candidate)
    monkeypatch.setattr(helper, "verify_auth", lambda *args: None)
    read_receipt = helper.receipt
    monkeypatch.setattr(helper, "receipt", lambda work, name, decision: (
        {} if name == "configured" else read_receipt(work, name, decision)
    ))
    monkeypatch.setattr(helper.prior, "require_worker", lambda *args: job)
    monkeypatch.setattr(helper.prior, "require_model_capacity", lambda *args: {"tokens_per_minute": 30000})
    monkeypatch.setattr(helper.release, "pilot_values", lambda *args: {})
    monkeypatch.setattr(helper.release, "active_executions", lambda *args: (job, []))
    monkeypatch.setattr(helper.gap, "credential_binding", lambda *args: None)
    monkeypatch.setattr(helper, "window", lambda work, decision, phase, seconds: calls.append((phase, seconds)))
    project = helper.release.writable_execution_template

    def execution_template(value):
        container = value["containers"][0]
        assert container["command"] == ["/app/.venv/bin/python"]
        assert container["args"][-2:] == ["--item-limit", "4"]
        assert helper.release.environment_entries(container)["DOCINTEL_REAL_PILOT_EXECUTION_SCOPE"]["value"] == "full"
        assert job == captured
        return project(value)

    monkeypatch.setattr(helper.release, "writable_execution_template", execution_template)

    def send(*args, resource_snapshot, before_send, on_preflight):
        assert not unknown_root
        assert args[:3] == ("containerapp", "job", "start")
        assert resource_snapshot == captured and job == captured
        template = helper.release.private_json(helper.path(tmp_path, "worker-template"))
        assert set(template) == {"containers"}
        azure_write_schema.validate_action_payload("jobStart", template)
        assert template["containers"][0]["command"] == ["/app/.venv/bin/python"]
        assert template["containers"][0]["args"][-2:] == ["--item-limit", "4"]
        assert helper.path(tmp_path, "worker-attempt").is_file()
        on_preflight(deployment_proof("azure_write"))
        native = helper.receipt(tmp_path, "worker-start-azure_write-preflight", {"target": "synthetic"})
        assert native["request_binding"] == {
            "resource_sha256": helper.sha(captured), "execution_sha256": helper.sha(template),
        }
        calls.append("preflight")
        before_send()
        calls.append("send")
        return {"name": "synthetic-execution"}

    monkeypatch.setattr(helper.release, "azure", send)
    if unknown_root:
        with pytest.raises(ValueError):
            helper.start(tmp_path, config, {"target": "synthetic"})
        assert not calls and not list(tmp_path.iterdir())
    else:
        helper.start(tmp_path, config, {"target": "synthetic"})
        assert calls == [("processing", 600), "preflight", ("processing", 600), "send"]
    assert job == captured


@pytest.mark.parametrize("raises", [False, True])
def test_native_deployment_failure_cannot_start_readiness(tmp_path, monkeypatch, raises):
    from contextlib import nullcontext

    monkeypatch.setattr(helper.release, "pilot_lock", lambda *args: nullcontext())
    monkeypatch.setattr(helper, "validate", lambda *args: ({}, {}))
    monkeypatch.setattr(helper.release, "verify_target", lambda *args: None)
    monkeypatch.setattr(helper, "mixed_resources", lambda *args: ({}, {}, {"properties": {"template": {}}}))
    monkeypatch.setattr(helper.prior, "require_model_capacity", lambda *args: pytest.fail("No-send failure must block"))

    def fail(*args):
        if raises:
            raise ValueError("Synthetic native constructor failure")
        return {"status": "rejected", "no_send": True}

    monkeypatch.setattr(helper.release, "preflight_deployment", fail)
    with pytest.raises(ValueError):
        helper.ready(tmp_path, {}, {"target": "synthetic"})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("changed", ["absent", "malformed", "stale", "quota"])
def test_window_cannot_accept_an_unchecked_or_stale_quota_receipt(tmp_path, monkeypatch, changed):
    anchor = datetime.now(timezone.utc)
    value = {
        "readiness_at": anchor.isoformat(), "model_capacity": {"tokens_per_minute": 30000},
        "model_capacity_checked_at": anchor.isoformat(), "quota_changed": False,
    }
    if changed == "absent":
        value.pop("model_capacity")
    elif changed == "malformed":
        value["model_capacity"] = None
    elif changed == "stale":
        value["model_capacity_checked_at"] = (anchor - timedelta(seconds=1)).isoformat()
    else:
        value["model_capacity"]["tokens_per_minute"] = 60000
    monkeypatch.setattr(
        helper, "receipt",
        lambda work, name, decision: {"sha256": helper.sha(value)} if name == "readiness-pin" else value,
    )
    with pytest.raises(ValueError, match="current unchanged 30000-TPM"):
        helper.window(tmp_path, {"target": "synthetic"}, "publication", 900)


@pytest.mark.parametrize("failure", ["configure", "enable", "start", "observe"])
def test_operator_activation_always_closes_without_retry(tmp_path, monkeypatch, failure):
    from contextlib import nullcontext

    calls = []
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    monkeypatch.setattr(helper.release, "pilot_lock", lambda *a: nullcontext())
    monkeypatch.setattr(helper, "live", lambda *a, **k: nullcontext())
    monkeypatch.setattr(helper, "window", lambda *a: None)
    monkeypatch.setattr(helper, "validate", lambda *a: None)
    monkeypatch.setattr(helper, "validate_image_smoke", lambda *a: {"reproduction_only": True})
    monkeypatch.setattr(helper, "active_target", lambda *a: None)
    for name in ("configure", "enable", "start", "observe", "close"):
        def call(*args, _name=name):
            calls.append(_name)
            if _name == failure:
                raise ValueError("REPRODUCTION stop")
        monkeypatch.setattr(helper, name, call)
    with pytest.raises(ValueError, match="REPRODUCTION"):
        helper.activate(tmp_path, {}, {"target": "synthetic"})
    assert calls[-1] == "close" and calls.count(failure) == 1
    assert calls.count("start") <= 1


def test_operator_usage_failure_does_not_stop_terminal_worker(tmp_path, monkeypatch):
    sent = []
    anchor = datetime.now(timezone.utc)
    decision = {"target": "synthetic"}
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    monkeypatch.setattr(helper, "now", lambda: anchor)
    monkeypatch.setattr(helper, "amendment", lambda *a: {"expires_at": (anchor + timedelta(seconds=600)).isoformat()})
    helper.release.save_once(helper.path(tmp_path, "worker-result"), {
        **helper.binding(decision), "execution_name": "only-worker",
    })

    def azure(*arguments, **kwargs):
        sent.append(arguments)
        return [{"name": "only-worker", "properties": {"status": "Succeeded"}}]

    monkeypatch.setattr(helper.release, "azure", azure)
    monkeypatch.setattr(helper, "usage", lambda *a, **k: (_ for _ in ()).throw(ValueError("Advisory unavailable")))
    helper.observe(tmp_path, {"subscription": "sub", "group": "group", "job": "job"}, decision)
    assert len(sent) == 1 and "stop" not in sent[0]
    assert helper.path(tmp_path, "worker-terminal").exists()


def test_actual_baseline_accepts_mixed_images_without_inventing_runtime_frontend(tmp_path, monkeypatch):
    config = {"target": "synthetic"}
    records = {
        real_pilot.APPROVAL_KEY: {"approved_by": "synthetic"},
        real_pilot.BUDGET_KEY: {
            "executions": {str(index): {} for index in range(4)},
            "attempted": {"inference": 11}, "reserved": {"input_tokens": 257868, "output_tokens": 22528},
        },
        **{f"history/{index}.json": {"first": index, "second": 1} for index in range(27)},
    }

    def encoded(indent):
        result = {}
        for key, value in records.items():
            raw = json.dumps(value, sort_keys=bool(indent), indent=indent).encode()
            result[key] = {"base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}
        return result

    files = {
        "gapfill-actual-outcome.json": {
            "records": encoded(2), "runtime": {"temporary_processing_closed": True, "active_executions": [],
                                             "backend_image": "new-api", "worker_image": "old-worker"},
        },
        "row-rerun-actual-outcome.json": {"records": encoded(None)},
        "gapfill-deployment-failure.json": {
            "processing_closed": True, "worker_unchanged": True, "worker_executions_started": 0,
            "model_requests": 0, "secret_reference_installed": False,
            "webiq_searches": 0, "direct_page_attempts": 0, "retried": False,
        },
        "gapfill-closed.json": {"target": helper.release.fingerprint(config), "decision_sha256": "same"},
        "gapfill-published.json": {"decision_sha256": "same", "backend_image": "new-api", "frontend_image": "same-frontend"},
        "gapfill-deploy-attempt.json": {
            "decision_sha256": "same", "worker": {"template": {"containers": [{"image": "old-worker"}]}},
            "frontend": {"template": {"containers": [{"image": "same-frontend"}]}},
        },
    }
    monkeypatch.setattr(helper.prior, "root", lambda work: work)
    monkeypatch.setattr(helper.release, "private_json", lambda path, **kwargs: files[path.name])
    _, parsed, runtime = helper.original(tmp_path, config)
    assert parsed == records and runtime["backend_image"] != runtime["worker_image"]
    assert runtime["frontend_image"] == "same-frontend"


def test_owner_capture_is_preserved_as_pending_not_capacity_or_readiness(tmp_path, monkeypatch):
    batch = {"id": "a" * 64, "items": [
        {"item_key": f"row-{index}", "manifest": {"product": {"item_id": f"SYNTHETIC-{index}"}}}
        for index in range(2, 6)
    ]}
    capture = {
        "schema_version": 1, "kind": "owner_authorization_capture_not_readiness", "approved": True,
        "readiness_clock_started": False, "initial_confirmation_at": "2026-10-06T20:16:22.694Z",
        "batch_id": batch["id"], "selected_item_keys": [item["item_key"] for item in batch["items"]],
        "selected_products": [item["manifest"]["product"]["item_id"] for item in batch["items"]],
        "preparation_analysis_exception": {
            "approved_in_followup": True, "before_readiness_clock": True, "analyses": 1,
            "pages": "1-5", "maximum_billable_pages": 5, "sdk_retries": 0, "blind_retries": 0, "worker_executions": 0,
            "source_sha256": "b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa",
        },
        "inference": {"total_requests": 12, "internal_requests": 8, "web_requests": 4,
                      "capacity_additions": None, "capacity_exception_decision": "pending exact measured request to owner"},
        "web": {"searches": 4, "searches_per_product": 1, "paid_browse": 0, "direct_page_attempts": 6},
        "worker": {"executions": 1, "timeout_seconds": 600, "retries": 0,
                   "pacing_adjustments_approved": True, "provider_quota_increase_approved": False},
        "financial": {"operating_envelope_usd": "5.00", "previous_build_cost_included_usd": "0.0232268896",
                      "historical_forecasts_stacked": False},
    }
    before = copy.deepcopy(capture)
    monkeypatch.setattr(helper.release, "private_json", lambda path: capture)
    monkeypatch.setattr(helper.gap, "existing_webiq_key", lambda: pytest.fail("Offline capture must not read credentials"))
    assert helper.owner_authorization(tmp_path, batch) == before and capture == before
    assert not list(tmp_path.iterdir())
    capture["inference"]["capacity_additions"] = {"additional_input_tokens": 1}
    with pytest.raises(ValueError, match="cannot be rewritten"):
        helper.owner_authorization(tmp_path, batch)
