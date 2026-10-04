"""Synthetic, network-free authorization tests; these values approve no real run."""

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

import pytest

from backend import real_pilot
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.real_pilot import APPROVAL_KEY, BUDGET_KEY, RealPilotGuard, binding_digest


class MemoryStore:
    """Exercise the existing store protocol without writing private test data."""

    def __init__(self, records=None):
        self.records = records if records is not None else {}
        self.mutex = threading.Lock()
        self.leased = set()
        self.counter = 0

    def read_bytes(self, key):
        with self.mutex:
            if key not in self.records:
                raise Missing(key)
            return self.records[key]

    def write_bytes(self, key, value, version=None):
        with self.mutex:
            existing = self.records.get(key)
            if (version is None and existing is not None) or (
                version is not None and (existing is None or existing[1] != version)
            ):
                raise Conflict("Conditional write failed")
            self.counter += 1
            token = str(self.counter)
            self.records[key] = (value, token)
            return token

    @contextmanager
    def lease(self, key):
        with self.mutex:
            if key in self.leased:
                raise Conflict("Already leased")
            self.leased.add(key)
        try:
            yield lambda: None
        finally:
            with self.mutex:
                self.leased.remove(key)


def replace(store, key, value):
    _, version = read_json(store, key)
    write_json(store, key, value, version)


@pytest.fixture
def pilot(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Real-pilot guard tests cannot contact services")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "true")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "22222222-2222-4222-8222-222222222222")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID", "66666666-6666-4666-8666-666666666666")
    environment = {
        "AZURE_CLIENT_ID": "33333333-3333-4333-8333-333333333333",
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://analysis.synthetic.invalid",
        "LLM_ENDPOINT": "https://model.synthetic.invalid",
        "LLM_DEPLOYMENT": "synthetic-deployment",
        "AOAI_API_VERSION": "synthetic-version",
        "WEBSEARCH_PROVIDER": "webiq",
        "WEBIQ_ENDPOINT": "https://web.synthetic.invalid",
    }
    monkeypatch.setattr(real_pilot, "current_environment", lambda: environment)
    batch = {
        "id": "a" * 64, "owner": "synthetic-owner", "valid": True,
        "mode": "real_pilot", "state": "queued", "product_count": 1,
        "input_hashes": {"manifest": "b" * 64, "attributes": "c" * 64},
        "attribute_reference": "synthetic-definitions.xlsx",
        "original_definitions": [{"node": "Synthetic", "potential_attribute_name": "Pressure"}],
        "items": [{
            "item_key": "row-2", "row": 2, "errors": [], "warnings": [],
            "original": {"PIMITEM Number": "SYNTHETIC-001"},
            "manifest": {
                "product": {"item_id": "SYNTHETIC-001", "vendor": "Synthetic", "mpn": "SYNTHETIC-PART", "hierarchy_node": "Synthetic"},
                "attributes": [{"attribute_id": "Pressure", "value_type": "number", "unit": "PSI"}],
                "source_ids": ["synthetic-source"], "existing_values": {},
            },
            "sources": [{"source_id": "synthetic-source", "kind": "blob", "blob": "documents/synthetic.pdf", "sha256": "d" * 64}],
        }],
    }
    now = real_pilot._now()
    approval = {
        "schema_version": 1, "approved": True,
        "id": "11111111-1111-4111-8111-111111111111",
        "approved_by": "22222222-2222-4222-8222-222222222222",
        "not_before": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(seconds=1100)).isoformat(),
        "batch_id": batch["id"], "owner": batch["owner"], "batch_sha256": binding_digest(batch),
        "customer_processing_approved": True, "environment": copy.deepcopy(environment),
        "identities": {
            "api_principal_id": "77777777-7777-4777-8777-777777777777",
            "worker_principal_id": "66666666-6666-4666-8666-666666666666",
        },
        "limits": {
            "products": 4, "executions": 2, "analysis": 2, "inference": 16,
            "search": 8, "web_retrieval": 12, "retrieval": 4, "analysis_pages": 10,
            "input_tokens": 200000, "output_tokens": 32768, "spend_microdollars": 1000000,
        },
        # Arbitrary synthetic arithmetic fixtures, not quoted service prices.
        "unit_prices_usd": {"analysis_page": "0.001", "input_token": "0.000001", "output_token": "0.000002", "search": "0.01", "web_retrieval": "0.02"},
    }
    store = MemoryStore()
    write_json(store, APPROVAL_KEY, approval)
    write_json(store, f"batches/{batch['id']}.json", batch)
    return store, batch, approval, environment


def start(pilot, execution="slice-1"):
    store, batch, *_ = pilot
    guard = RealPilotGuard(store, batch)
    guard.before_execution(execution)
    return guard


def key(guard, *, version="source-v1", item=None, tier="internal", prompt="prompt-v1"):
    return guard.operation_key(
        item or guard.batch["items"][0], tier=tier,
        source_version=version, prompt_version=prompt,
    )


def reserve_inference(guard, version="source-v1", **kwargs):
    return guard.reserve(
        "inference", key(guard, version=version), item_key="row-2",
        max_input_tokens=kwargs.get("input_tokens", 100),
        max_output_tokens=kwargs.get("output_tokens", 20),
    )


def approve_internal_only(pilot):
    store, _, original, _ = pilot
    approval = copy.deepcopy(original)
    approval["execution_scope"] = "internal_only"
    approval["customer_processing_approved"] = True
    for name in real_pilot.EXTERNAL_OPERATIONS:
        approval["limits"][name] = 0
    approval["environment"] = {
        name: value for name, value in approval["environment"].items()
        if name not in real_pilot.EXTERNAL_ENVIRONMENT_KEYS
    }
    approval["unit_prices_usd"] = {
        name: value for name, value in approval["unit_prices_usd"].items()
        if name in real_pilot.INTERNAL_PRICE_KEYS
    }
    replace(store, APPROVAL_KEY, approval)
    return approval


def test_internal_only_needs_no_web_settings_credentials_prices_or_entitlement(pilot, monkeypatch):
    approval = approve_internal_only(pilot)
    for name in ("WEBIQ_API_KEY", "WEBIQ_ENDPOINT", "WEBSEARCH_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    pilot[3]["WEBIQ_ENDPOINT"] = "unverified"
    pilot[3]["WEBSEARCH_PROVIDER"] = "unavailable"
    guard = start(pilot)
    reservation = reserve_inference(guard)
    guard.record_usage(reservation["reservation_id"], input_tokens=50, output_tokens=5)
    assert guard.execution_scope == guard.metadata()["execution_scope"] == "internal_only"
    assert guard.metadata()["reserved"]["microdollars"] == 140
    assert approval["customer_processing_approved"] is True
    assert set(approval["unit_prices_usd"]) == {"analysis_page", "input_token", "output_token"}


@pytest.mark.parametrize("operation", ["search", "web_retrieval", "retrieval"])
def test_internal_only_requires_zero_external_budgets_and_rejects_external_calls(pilot, operation):
    approval = approve_internal_only(pilot)
    approval["limits"][operation] = 1
    replace(pilot[0], APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="zero"):
        RealPilotGuard(pilot[0], pilot[1])
    approval["limits"][operation] = 0
    replace(pilot[0], APPROVAL_KEY, approval)
    guard = start(pilot)
    with pytest.raises(ValueError, match="forbidden"):
        guard.reserve(operation, key(guard), item_key="row-2")
    assert guard.metadata()["attempted"][operation] == 0
    assert guard.metadata()["reserved"]["microdollars"] == 0


@pytest.mark.parametrize("scope", [None, "", "internal", "FULL", True])
def test_scope_is_explicit_and_validated(pilot, scope):
    pilot[2]["execution_scope"] = scope
    replace(pilot[0], APPROVAL_KEY, pilot[2])
    with pytest.raises(ValueError, match="execution_scope"):
        RealPilotGuard(pilot[0], pilot[1])


def test_omitted_scope_retains_full_approval_and_legacy_ledger_semantics(pilot):
    guard = start(pilot)
    assert guard.execution_scope == guard.metadata()["execution_scope"] == "full"
    reservation = guard.reserve("search", key(guard), item_key="row-2")
    assert reservation["reserved_microdollars"] == 10000
    ledger, _ = read_json(pilot[0], BUDGET_KEY)
    del ledger["execution_scope"]
    replace(pilot[0], BUDGET_KEY, ledger)
    assert RealPilotGuard(pilot[0], pilot[1]).metadata()["execution_scope"] == "full"


@pytest.mark.parametrize("setting", sorted(real_pilot.EXTERNAL_ENVIRONMENT_KEYS))
def test_internal_only_rejects_external_configuration_in_approval(pilot, setting):
    approval = approve_internal_only(pilot)
    approval["environment"][setting] = "unverified"
    replace(pilot[0], APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="omit external"):
        RealPilotGuard(pilot[0], pilot[1])


def test_internal_only_rechecks_runtime_scope_before_services(pilot, monkeypatch):
    approve_internal_only(pilot)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_EXECUTION_SCOPE", "internal_only")
    guard = start(pilot)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_EXECUTION_SCOPE", "full")
    with pytest.raises(ValueError, match="scope mismatch"):
        reserve_inference(guard)
    assert guard.metadata()["invalidated"]
    assert guard.metadata()["attempted"]["inference"] == 0


@pytest.mark.parametrize("change", ["scope", "id", "batch"])
def test_internal_scope_cannot_reset_durable_allowance(pilot, change):
    store, record, original, _ = pilot
    approval = approve_internal_only(pilot)
    guard = start(pilot)
    reserve_inference(guard)
    candidate = copy.deepcopy(record)
    if change == "scope":
        approval = copy.deepcopy(original)
        approval["execution_scope"] = "full"
    elif change == "id":
        approval["id"] = "99999999-9999-4999-8999-999999999999"
    else:
        candidate["id"] = "f" * 64
        approval["batch_id"] = candidate["id"]
        approval["batch_sha256"] = binding_digest(candidate)
        write_json(store, f"batches/{candidate['id']}.json", candidate)
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="changed"):
        RealPilotGuard(store, candidate)
    assert guard.metadata()["invalidated"]
    assert guard.metadata()["attempted"]["inference"] == 1


def test_used_full_approval_cannot_reopen_as_internal_only(pilot):
    guard = start(pilot)
    reserve_inference(guard)
    approve_internal_only(pilot)
    with pytest.raises(ValueError, match="changed"):
        RealPilotGuard(pilot[0], pilot[1])
    assert guard.metadata()["invalidated"]
    assert guard.metadata()["attempted"]["inference"] == 1


@pytest.mark.parametrize("enabled", [None, "", "false", "TRUE", "1"])
def test_default_off_and_only_explicit_true(pilot, monkeypatch, enabled):
    if enabled is None:
        monkeypatch.delenv("DOCINTEL_REAL_PILOT_ENABLED")
    else:
        monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", enabled)
    with pytest.raises(ValueError, match="disabled"):
        RealPilotGuard(pilot[0], pilot[1])
    assert BUDGET_KEY not in pilot[0].records


def test_old_synthetic_approval_never_authorizes_real_pilot(pilot):
    store, batch, approval, _ = pilot
    del store.records[APPROVAL_KEY]
    write_json(store, "configuration/live-approval.json", approval)
    with pytest.raises(ValueError, match="server-side"):
        RealPilotGuard(store, batch)


@pytest.mark.parametrize("change", [
    {"approved": False},
    {"approved": "true"},
    {"schema_version": True},
    {"id": "not-an-approval"},
    {"approved_by": "unverified-operator"},
    {"approved_by": "44444444-4444-4444-8444-444444444444"},
    {"owner": "other-owner"},
    {"batch_id": "e" * 64},
    {"batch_sha256": "e" * 64},
    {"customer_processing_approved": "true"},
    {"extra": "do not silently ignore"},
])
def test_invalid_approval_is_denied_without_budget(pilot, change):
    store, batch, approval, _ = pilot
    approval.update(change)
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError):
        RealPilotGuard(store, batch)
    assert BUDGET_KEY not in store.records


@pytest.mark.parametrize("start_delta,end_delta", [(60, 120), (-120, -1), (-1, 1200), (0, 0)])
def test_future_expired_and_overlong_approvals(pilot, start_delta, end_delta):
    store, batch, approval, _ = pilot
    now = real_pilot._now()
    approval["not_before"] = (now + timedelta(seconds=start_delta)).isoformat()
    approval["expires_at"] = (now + timedelta(seconds=end_delta)).isoformat()
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="not active"):
        RealPilotGuard(store, batch)


def test_timezone_is_mandatory(pilot):
    store, batch, approval, _ = pilot
    approval["expires_at"] = "2099-01-01T00:00:00"
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="timezone"):
        RealPilotGuard(store, batch)


@pytest.mark.parametrize("name", list(real_pilot.HARD_LIMITS))
def test_hard_ceilings_cannot_be_broadened(pilot, name):
    store, batch, approval, _ = pilot
    approval["limits"][name] = real_pilot.HARD_LIMITS[name] + 1
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError):
        RealPilotGuard(store, batch)


@pytest.mark.parametrize("price", [None, 0.01, "0", "-1", "NaN", "Infinity", "1e-6", ""])
def test_missing_or_unbounded_prices_fail_closed(pilot, price):
    store, batch, approval, _ = pilot
    approval["unit_prices_usd"]["input_token"] = price
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError):
        RealPilotGuard(store, batch)


def test_prices_cannot_be_omitted(pilot):
    store, batch, approval, _ = pilot
    del approval["unit_prices_usd"]["search"]
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="prices"):
        RealPilotGuard(store, batch)


@pytest.mark.parametrize("field", ["items", "input_hashes", "original_definitions", "attribute_reference", "owner"])
def test_binding_detects_all_intake_drifts(pilot, field):
    store, batch, _, _ = pilot
    drifted = copy.deepcopy(batch)
    if field == "items":
        drifted[field][0]["sources"][0]["sha256"] = "f" * 64
    elif field == "input_hashes":
        drifted[field]["manifest"] = "f" * 64
    elif field == "original_definitions":
        drifted[field][0]["potential_attribute_name"] = "Other"
    else:
        drifted[field] = "different"
    replace(store, f"batches/{batch['id']}.json", drifted)
    with pytest.raises(ValueError, match="binding"):
        RealPilotGuard(store, batch)


def test_exact_item_and_product_count_are_checked(pilot):
    store, batch, approval, _ = pilot
    batch["product_count"] = 2
    approval["batch_sha256"] = binding_digest(batch)
    replace(store, APPROVAL_KEY, approval)
    replace(store, f"batches/{batch['id']}.json", batch)
    with pytest.raises(ValueError, match="count"):
        RealPilotGuard(store, batch)


@pytest.mark.parametrize("field", ["AZURE_CLIENT_ID", "LLM_ENDPOINT", "LLM_DEPLOYMENT", "WEBIQ_ENDPOINT"])
def test_actual_service_and_identity_must_match_server_approval(pilot, field):
    pilot[3][field] = "different"
    with pytest.raises(ValueError, match="configuration mismatch"):
        start(pilot)


def test_api_preflight_does_not_require_worker_runtime_identity(pilot, monkeypatch):
    pilot[3]["AZURE_CLIENT_ID"] = "88888888-8888-4888-8888-888888888888"
    monkeypatch.delenv("DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID")
    RealPilotGuard(pilot[0], pilot[1])
    assert BUDGET_KEY not in pilot[0].records


@pytest.mark.parametrize("scope", ["full", "internal_only"])
def test_api_preflight_never_reads_worker_endpoints_or_credentials(pilot, monkeypatch, scope):
    if scope == "internal_only":
        approve_internal_only(pilot)
    for name in ("AZURE_CLIENT_ID", "DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID", "WEBIQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_ENDPOINT", "https://api-legacy-endpoint.invalid")
    monkeypatch.setattr(real_pilot, "current_environment", lambda: pytest.fail("API preflight read worker runtime settings"))
    guard = RealPilotGuard(pilot[0], pilot[1])
    assert guard.execution_scope == scope
    assert BUDGET_KEY not in pilot[0].records


@pytest.mark.parametrize("runtime_client_id", [None, ""])
def test_system_assigned_worker_matches_separate_principal_binding(pilot, runtime_client_id):
    store, _, approval, environment = pilot
    approval["environment"]["AZURE_CLIENT_ID"] = ""
    environment["AZURE_CLIENT_ID"] = runtime_client_id
    replace(store, APPROVAL_KEY, approval)
    guard = start(pilot)
    reserve_inference(guard)
    assert guard.metadata()["identities"] == approval["identities"]


@pytest.mark.parametrize("principal", [None, "", "77777777-7777-4777-8777-777777777777"])
def test_worker_principal_must_match_even_with_system_assigned_identity(pilot, monkeypatch, principal):
    store, _, approval, environment = pilot
    approval["environment"]["AZURE_CLIENT_ID"] = ""
    environment["AZURE_CLIENT_ID"] = None
    replace(store, APPROVAL_KEY, approval)
    if principal is None:
        monkeypatch.delenv("DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID")
    else:
        monkeypatch.setenv("DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID", principal)
    with pytest.raises(ValueError, match="worker principal"):
        start(pilot)
    assert BUDGET_KEY not in store.records


@pytest.mark.parametrize("mutation", ["missing", "secret", "http", "credentials"])
def test_environment_allowlist_and_safe_endpoint_shape(pilot, mutation):
    store, batch, approval, environment = pilot
    if mutation == "missing":
        del approval["environment"]["LLM_ENDPOINT"]
    elif mutation == "secret":
        approval["environment"]["WEBIQ_API_KEY"] = "not-a-real-secret"
    else:
        value = "http://synthetic.invalid" if mutation == "http" else "https://user:password@synthetic.invalid"
        approval["environment"]["LLM_ENDPOINT"] = value
        environment["LLM_ENDPOINT"] = value
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError):
        RealPilotGuard(store, batch)


def test_reservations_require_explicit_execution_and_item_membership(pilot):
    guard = RealPilotGuard(pilot[0], pilot[1])
    with pytest.raises(ValueError, match="before_execution"):
        reserve_inference(guard)
    guard.before_execution("slice-1")
    other = copy.deepcopy(guard.batch["items"][0])
    other["manifest"]["product"]["mpn"] = "UNAPPROVED"
    with pytest.raises(ValueError, match="item"):
        key(guard, item=other)
    with pytest.raises(ValueError, match="item"):
        guard.reserve("inference", "invented", item_key="row-2", max_input_tokens=1, max_output_tokens=1)


def test_intake_validation_allowed_but_worker_requires_real_mode(pilot):
    store, batch, _, _ = pilot
    batch.pop("mode")
    replace(store, f"batches/{batch['id']}.json", batch)
    guard = RealPilotGuard(store, batch)
    with pytest.raises(ValueError, match="real_pilot"):
        guard.before_execution("slice-1")
    assert BUDGET_KEY not in store.records


@pytest.mark.parametrize("mode", ["evidence_only", "live_inference"])
def test_other_modes_not_authorized_by_real_approval(pilot, mode):
    store, batch, _, _ = pilot
    batch["mode"] = mode
    replace(store, f"batches/{batch['id']}.json", batch)
    with pytest.raises(ValueError, match="another execution mode"):
        RealPilotGuard(store, batch)


def test_execution_and_paid_attempts_are_durable_across_recreated_guard(pilot):
    store, batch, _, _ = pilot
    first = start(pilot)
    reservation = reserve_inference(first)
    assert reservation["reserved_microdollars"] == 140
    # A separate client reading the same persisted records sees consumed budget.
    reopened = MemoryStore(store.records)
    second = RealPilotGuard(reopened, batch)
    with pytest.raises(Conflict, match="execution already attempted"):
        second.before_execution("slice-1")
    second.before_execution("slice-2")
    with pytest.raises(Conflict, match="operation already attempted"):
        reserve_inference(second)
    with pytest.raises(ValueError, match="execution budget"):
        RealPilotGuard(store, batch).before_execution("slice-3")
    assert second.metadata()["attempted"]["inference"] == 1
    assert second.metadata()["reserved"]["microdollars"] == 140


def test_failed_or_unknown_calls_remain_reserved(pilot):
    guard = start(pilot)
    reserved = reserve_inference(guard)
    assert reserved["status"] == "attempt_reserved_completion_unknown"
    assert reserved["actual_usage"] is None
    with pytest.raises(Conflict):
        reserve_inference(guard)
    assert guard.metadata()["reserved"]["input_tokens"] == 100
    assert guard.metadata()["actual_usage"]["input_tokens"] == 0
    assert guard.metadata()["unknown_usage_reservations"] == 1
    assert guard.metadata()["usage_reporting_complete"] is False


@pytest.mark.parametrize("field", ["id", "expires_at", "limits", "unit_prices_usd", "approved_by"])
def test_editing_any_approval_field_cannot_reset_allowance(pilot, field):
    store, batch, approval, _ = pilot
    guard = start(pilot)
    reserve_inference(guard)
    if field == "id":
        approval[field] = "55555555-5555-4555-8555-555555555555"
    elif field == "expires_at":
        approval[field] = (real_pilot._now() + timedelta(seconds=1000)).isoformat()
    elif field == "limits":
        approval[field]["spend_microdollars"] += 1
    elif field == "unit_prices_usd":
        approval[field]["input_token"] = "0.0000009"
    else:
        approval[field] = "44444444-4444-4444-8444-444444444444"
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError):
        reserve_inference(guard, "new-source")
    ledger, _ = read_json(store, BUDGET_KEY)
    assert ledger["invalidated"]
    assert ledger["attempted"]["inference"] == 1
    with pytest.raises(ValueError):
        RealPilotGuard(store, batch)


def test_observed_binding_drift_remains_invalidated_after_revert(pilot):
    store, batch, _, _ = pilot
    guard = start(pilot)
    changed = copy.deepcopy(batch)
    changed["items"][0]["manifest"]["attributes"][0]["unit"] = "BAR"
    replace(store, f"batches/{batch['id']}.json", changed)
    with pytest.raises(ValueError, match="binding"):
        reserve_inference(guard)
    replace(store, f"batches/{batch['id']}.json", batch)
    with pytest.raises(ValueError, match="invalidated"):
        RealPilotGuard(store, batch)


def test_new_guard_observing_invalid_approval_permanently_invalidates(pilot):
    store, batch, approval, _ = pilot
    start(pilot)
    original = copy.deepcopy(approval)
    approval["approved"] = False
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="approval"):
        RealPilotGuard(store, batch)
    replace(store, APPROVAL_KEY, original)
    with pytest.raises(ValueError, match="invalidated"):
        RealPilotGuard(store, batch)


def test_usage_after_approval_drift_uses_immutable_original_prices(pilot):
    store, _, approval, _ = pilot
    guard = start(pilot)
    reservation = reserve_inference(guard)
    approval["unit_prices_usd"]["input_token"] = "100"
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError):
        reserve_inference(guard, "new-source")
    guard.record_usage(reservation["reservation_id"], input_tokens=50, output_tokens=5)
    assert guard.metadata()["estimated_usage_cost_microdollars"] == 60


def test_existing_guard_rechecks_enablement_and_expiry(pilot, monkeypatch):
    guard = start(pilot)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "false")
    with pytest.raises(ValueError, match="disabled"):
        reserve_inference(guard)
    assert guard.metadata()["invalidated"]


def test_existing_guard_expiry_blocks_new_calls_but_can_record_late_usage(pilot, monkeypatch):
    guard = start(pilot)
    reservation = reserve_inference(guard)
    later = real_pilot._now() + timedelta(seconds=1201)
    monkeypatch.setattr(real_pilot, "_now", lambda: later)
    with pytest.raises(ValueError, match="not active"):
        reserve_inference(guard, "new-source")
    guard.record_usage(reservation["reservation_id"], input_tokens=50, output_tokens=5)
    assert guard.metadata()["actual_usage"]["input_tokens"] == 50


@pytest.mark.parametrize("limit,value,kwargs", [
    ("inference", 0, {}),
    ("input_tokens", 99, {}),
    ("output_tokens", 19, {}),
    ("spend_microdollars", 139, {}),
])
def test_server_count_token_and_spend_ceilings(pilot, limit, value, kwargs):
    store, _, approval, _ = pilot
    approval["limits"][limit] = value
    replace(store, APPROVAL_KEY, approval)
    guard = start(pilot)
    with pytest.raises(ValueError, match="budget|ceiling"):
        reserve_inference(guard, **kwargs)
    assert guard.metadata()["attempted"]["inference"] == 0
    assert guard.metadata()["reserved"]["microdollars"] == 0


@pytest.mark.parametrize("value", [-1, True, 1.0, None, 200001])
def test_invalid_token_estimates_cannot_reach_reservation(pilot, value):
    guard = start(pilot)
    with pytest.raises(ValueError):
        reserve_inference(guard, input_tokens=value)


def test_inference_needs_both_server_token_bounds(pilot):
    guard = start(pilot)
    with pytest.raises(ValueError, match="upper bounds"):
        reserve_inference(guard, input_tokens=0)
    with pytest.raises(ValueError, match="upper bounds"):
        reserve_inference(guard, output_tokens=0)


def test_analysis_page_budget_and_attempts(pilot):
    guard = start(pilot)
    with pytest.raises(ValueError, match="page"):
        guard.reserve("analysis", key(guard), item_key="row-2")
    reservation = guard.reserve("analysis", key(guard), item_key="row-2", analysis_pages=6)
    assert reservation["reserved_microdollars"] == 6000
    with pytest.raises(ValueError, match="analysis_pages"):
        guard.reserve("analysis", key(guard, version="v2"), item_key="row-2", analysis_pages=5)
    guard.reserve("analysis", key(guard, version="v2"), item_key="row-2", analysis_pages=4)
    with pytest.raises(ValueError, match="operation budget"):
        guard.reserve("analysis", key(guard, version="v3"), item_key="row-2", analysis_pages=1)


@pytest.mark.parametrize("scope", ["full", "internal_only"])
def test_customer_processing_consent_is_required_before_any_scope(pilot, scope):
    store, record, original, _ = pilot
    approval = approve_internal_only(pilot) if scope == "internal_only" else copy.deepcopy(original)
    approval["customer_processing_approved"] = False
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="customer processing"):
        RealPilotGuard(store, record)
    assert BUDGET_KEY not in store.records


@pytest.mark.parametrize("scope", ["full", "internal_only"])
def test_revoked_customer_processing_consent_stops_next_call(pilot, scope):
    store, _, approval, _ = pilot
    if scope == "internal_only":
        approval = approve_internal_only(pilot)
    guard = start(pilot)
    reserve_inference(guard)
    approval["customer_processing_approved"] = False
    replace(store, APPROVAL_KEY, approval)
    with pytest.raises(ValueError, match="customer processing"):
        reserve_inference(guard, "different-source")
    assert guard.metadata()["attempted"]["inference"] == 1
    assert guard.metadata()["invalidated"]


def test_internal_retrieval_has_separate_attempt_ceiling_not_assumed_billing(pilot):
    guard = start(pilot)
    for index in range(4):
        reservation = guard.reserve(
            "retrieval", key(guard, version=f"source-{index}"), item_key="row-2",
        )
        assert reservation["reserved_microdollars"] == 0
    with pytest.raises(ValueError, match="operation budget"):
        guard.reserve("retrieval", key(guard, version="source-5"), item_key="row-2")
    with pytest.raises(Conflict, match="already attempted"):
        guard.reserve("retrieval", key(guard, version="source-0"), item_key="row-2")
    assert guard.metadata()["attempted"]["retrieval"] == 4
    assert guard.metadata()["attempted"]["web_retrieval"] == 0
    assert guard.metadata()["reserved"]["microdollars"] == 0
    assert guard.metadata()["actual_billed_microdollars"] is None


def test_internal_retrieval_cannot_bypass_paid_inference_limits(pilot):
    guard = start(pilot)
    with pytest.raises(ValueError, match="cannot authorize"):
        guard.reserve("retrieval", key(guard), item_key="row-2", max_input_tokens=1)
    assert guard.metadata()["attempted"]["retrieval"] == 0


def test_sub_microdollar_rates_round_up_without_floats(pilot):
    store, _, approval, _ = pilot
    approval["unit_prices_usd"]["input_token"] = "0.0000000001"
    approval["unit_prices_usd"]["output_token"] = "0.0000000001"
    replace(store, APPROVAL_KEY, approval)
    guard = start(pilot)
    assert reserve_inference(guard, input_tokens=1, output_tokens=1)["reserved_microdollars"] == 1


def test_actual_usage_is_separate_from_reserved_cost_and_never_billing(pilot):
    guard = start(pilot)
    reservation = reserve_inference(guard)
    guard.record_usage(reservation["reservation_id"], input_tokens=50, output_tokens=5)
    metadata = guard.metadata()
    assert metadata["reserved"] == {"input_tokens": 100, "output_tokens": 20, "analysis_pages": 0, "microdollars": 140}
    assert metadata["actual_usage"] == {"input_tokens": 50, "output_tokens": 5, "analysis_pages": 0}
    assert metadata["estimated_usage_cost_microdollars"] == 60
    assert metadata["usage_reporting_complete"] is True
    assert metadata["actual_billed_microdollars"] is None
    assert metadata["cost_basis"] == "approved_upper_bound_prices_not_actual_billing"
    with pytest.raises(Conflict, match="already recorded"):
        guard.record_usage(reservation["reservation_id"], input_tokens=50, output_tokens=5)


def test_actual_overrun_is_persisted_and_permanently_invalidates(pilot):
    guard = start(pilot)
    reservation = reserve_inference(guard)
    with pytest.raises(ValueError, match="exceeded"):
        guard.record_usage(reservation["reservation_id"], input_tokens=101, output_tokens=20)
    metadata = guard.metadata()
    assert metadata["actual_usage"]["input_tokens"] == 101
    assert metadata["reserved"]["input_tokens"] == 100
    assert metadata["invalidated"] == "actual_usage_exceeded_reservation"
    with pytest.raises(ValueError, match="invalidated"):
        reserve_inference(guard, "new-source")


def test_metadata_does_not_copy_customer_content_or_claim_billing(pilot):
    guard = start(pilot)
    reserve_inference(guard)
    metadata = guard.metadata()
    serialized = json.dumps(metadata)
    assert metadata["approved_by"] == pilot[2]["approved_by"]
    assert metadata["batch_sha256"] == binding_digest(pilot[1])
    assert "SYNTHETIC-PART" not in serialized
    assert "Pressure" not in serialized
    assert "synthetic.pdf" not in serialized
    assert "synthetic.invalid" not in serialized
    assert "unit_prices_usd" not in serialized
    assert "not_actual_billing" in serialized
    metadata["attempted"]["inference"] = 999
    assert guard.metadata()["attempted"]["inference"] == 1


def test_operation_key_stable_across_restart_and_bound_to_context(pilot):
    first = RealPilotGuard(pilot[0], pilot[1])
    second = RealPilotGuard(pilot[0], pilot[1])
    assert key(first) == key(second)
    assert key(first) != key(first, version="different")
    assert key(first) != key(first, tier="different")
    assert key(first) != key(first, prompt="different")


def test_concurrent_reservations_never_exceed_limit(pilot):
    store, batch, approval, _ = pilot
    approval["limits"]["inference"] = 1
    replace(store, APPROVAL_KEY, approval)
    first = start(pilot, "slice-1")
    second = RealPilotGuard(store, batch)
    second.before_execution("slice-2")
    barrier = threading.Barrier(2)

    def attempt(guard, version):
        barrier.wait()
        try:
            return reserve_inference(guard, version)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, first, "v1"), pool.submit(attempt, second, "v2")]
        results = [future.result() for future in futures]
    assert sum(result is not None for result in results) == 1
    assert first.metadata()["attempted"]["inference"] == 1
    assert first.metadata()["reserved"]["microdollars"] == 140


def test_conflicted_reservation_never_retries_or_returns_success(pilot, monkeypatch):
    guard = start(pilot)
    original = pilot[0].write_bytes
    calls = []

    def conflict(path, value, version=None):
        if path == BUDGET_KEY:
            calls.append(path)
            raise Conflict("Injected CAS conflict")
        return original(path, value, version)

    monkeypatch.setattr(pilot[0], "write_bytes", conflict)
    with pytest.raises(Conflict, match="Injected"):
        reserve_inference(guard)
    assert calls == [BUDGET_KEY]
    assert guard.metadata()["attempted"]["inference"] == 0
