"""Real guard + unchanged production worker; no network and no live allowance.

DOCINTEL_TEST_GAPFILL_WORK enables the retained private-store gate.
DOCINTEL_TEST_GAPFILL_DOCUMENTS supplies the already approved local document copies.
DOCINTEL_TEST_GAPFILL_OUTPUT optionally writes a new private gate in that root.
Every supplied completion, search lead and retrieved page is REPRODUCTION ONLY.
"""

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import batch_worker as worker, real_pilot
from backend.batch import BatchService
from backend.batch_store import Conflict, read_json, write_json
from backend.core.websearch import WebIQSearchResult
from backend.core.websearch_webiq import WebIQSearchClient
from backend.models.enrichment import ExtractionResponse
from backend.workbooks import read_workbook
from scripts import gapfill_continuation as helper
from tests.test_row_rerun import (
    WorkerMemoryStore, all_records, configured, final_case, inject_approved_documents,
    network_blocked, put, recovery_case, row_case, start_row,
)


LABEL = "REPRODUCTION ONLY: invented offline responses and pages, not recovered output or product findings."


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Gapfill tests forbid all network and paid service calls")

    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("socket.socket.connect_ex", deny)
    monkeypatch.setattr("socket.getaddrinfo", deny)
    monkeypatch.setattr(worker, "sleep", lambda _: None)


def policy_for(batch):
    items = {}
    for item in batch["items"][:2]:
        sources = [source for source in item["sources"] if source["kind"] == "web"
                   and source["source_tier"] == "manufacturer_web"]
        from urllib.parse import urlsplit

        items[item["item_key"]] = {
            "manufacturer": "Mueller", "mpn": item["manifest"]["product"]["mpn"],
            "attribute_terms": {entry["attribute_id"]: entry["attribute_id"]
                                for entry in item["manifest"]["attributes"]},
            "source_ids": [source["source_id"] for source in sources],
            "allowed_hosts": sorted({urlsplit(source["url"]).hostname for source in sources}),
        }
    return {
        "batch_sha256": real_pilot.binding_digest(batch), "items": items,
        "max_cost_microdollars": 210920, "max_search_calls": 2, "max_direct_page_attempts": 4,
        "max_inference_calls": 2, "max_input_tokens": 26000, "max_output_tokens": 2048,
    }


def candidate_for(store, batch, configuration=None):
    records = {key: json.loads(raw) for key, (raw, _) in all_records(store).items()
               if key.endswith(".json")}
    configuration = configuration or {
        "public_web_policy": policy_for(batch),
        "web_environment": {"WEBSEARCH_PROVIDER": "webiq", "WEBIQ_ENDPOINT": "https://api.microsoft.ai/v3/search/web"},
        "web_prices": {"search": "0.0125", "web_retrieval": "0"}, "fixed_cost_microdollars": 198000,
    }
    scope = helper.amendment_scope(records, **configuration)
    ready = max(datetime.now(timezone.utc),
                datetime.fromisoformat(records[real_pilot.ROW_RERUN_KEY]["operating_expires_at"]) + timedelta(seconds=1))
    return {**scope, "readiness_at": ready.isoformat(),
            "operating_expires_at": (ready + timedelta(seconds=5400)).isoformat(),
            "not_before": ready.isoformat(), "expires_at": (ready + timedelta(seconds=1200)).isoformat()}


def runtime(monkeypatch, approval, candidate):
    from backend.core.config import settings

    for key, value in {**approval["environment"], **candidate["web_environment"],
                       "DOCINTEL_REAL_PILOT_ENABLED": "true",
                       "DOCINTEL_REAL_PILOT_OPERATOR_IDS": approval["approved_by"],
                       "DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID": approval["identities"]["worker_principal_id"],
                       "DOCINTEL_REAL_PILOT_EXECUTION_SCOPE": "full",
                       "DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED": "true",
                       "DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON": json.dumps(candidate["public_web_policy"])}.items():
        monkeypatch.setenv(key, value)
        if hasattr(settings, key):
            monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(real_pilot, "_now", lambda: datetime.fromisoformat(candidate["not_before"]) + timedelta(seconds=1))


@pytest.fixture
def gapfill_case(row_case, monkeypatch):
    store, batch, approval, _ = row_case
    guard = start_row(row_case)
    for index, bound in enumerate((23000, 23000, 23000, 23146)):
        item = batch["items"][index // 2]
        key = guard.operation_key(item, tier="inference", source_version=str(index), prompt_version="synthetic-row")
        guard.reserve("inference", key, item_key=item["item_key"], max_input_tokens=bound, max_output_tokens=2048)
    for item in batch["items"][:2]:
        key = f"items/{batch['id']}/{item['item_key']}.json"
        state = read_json(store, key)[0]
        state["state"] = "partial_draft"
        put(store, key, state)
        result = read_json(store, f"results/{batch['id']}/{item['item_key']}.json")[0]
        write_json(store, state["result_key"], result)
    candidate = candidate_for(store, batch)
    write_json(store, real_pilot.GAPFILL_KEY, candidate)
    runtime(monkeypatch, approval, candidate)
    return store, batch, approval, candidate


def start(case):
    store, batch, _, _ = case
    guard = real_pilot.RealPilotGuard(store, batch)
    retained_recovery = read_json(store, real_pilot.RECOVERY_KEY)[0]
    assert guard.recovery == retained_recovery and guard.active_recovery == case[3]
    guard.before_execution("fifth-synthetic-execution")
    guard.prepare_recovery(lambda: None)
    assert guard.recovery == retained_recovery and guard.active_recovery == case[3]
    return guard


def reserve(guard, item, operation, index, *, tokens=0):
    key = guard.operation_key(item, tier=operation, source_version=f"gapfill-{index}", prompt_version="synthetic")
    return guard.reserve(operation, key, item_key=item["item_key"],
                         max_input_tokens=tokens, max_output_tokens=2048 if operation == "inference" else 0)


def test_exact_six_additive_reservations_preserve_all_prior_records(gapfill_case):
    store, batch, approval, candidate = gapfill_case
    baseline = all_records(store)
    guard = start(gapfill_case)
    for index, item in enumerate(batch["items"][:2]):
        reserve(guard, item, "inference", index * 3, tokens=24911)
        reserve(guard, item, "inference", index * 3 + 1, tokens=24912)
        reserve(guard, item, "search", index)
        reserve(guard, item, "web_retrieval", index * 2)
        reserve(guard, item, "web_retrieval", index * 2 + 1)
        reserve(guard, item, "inference", index * 3 + 2, tokens=26000)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert helper.consumption(ledger) == {
        "executions": 5, "inference": 17, "input_tokens": 409514, "output_tokens": 34816,
    }
    assert ledger["attempted"]["search"] == 2 and ledger["attempted"]["web_retrieval"] == 4
    page_reservations = [entry for entry in ledger["reservations"].values()
                         if entry["execution_id"] == guard._execution_id and entry["operation"] == "web_retrieval"]
    assert len(page_reservations) == 4 and all(entry["reserved_microdollars"] == 0 for entry in page_reservations)
    baseline_ledger = json.loads(baseline[real_pilot.BUDGET_KEY][0])
    expected_cost = real_pilot.RealPilotGuard._cost(approval, "inference", 151646, 12288, 0) + 25000
    assert ledger["reserved"]["microdollars"] - baseline_ledger["reserved"]["microdollars"] == expected_cost
    assert read_json(store, real_pilot.APPROVAL_KEY)[0] == approval
    for key, value in baseline.items():
        if key != real_pilot.BUDGET_KEY and not key.startswith(f"items/{batch['id']}/"):
            assert store.read_bytes(key) == value
    old = json.loads(baseline[real_pilot.BUDGET_KEY][0])
    assert all(ledger["reservations"][key] == value for key, value in old["reservations"].items())
    assert all(ledger["executions"][key] == value for key, value in old["executions"].items())
    for name in ("recovery", "final_rerun", "row_rerun"):
        assert ledger[name] == old[name]
    assert guard.recovery == read_json(store, real_pilot.RECOVERY_KEY)[0]
    assert guard.active_recovery == candidate
    with pytest.raises((ValueError, Conflict)):
        guard.before_execution("sixth-not-authorized")
    with pytest.raises(real_pilot.RealPilotBudgetExceeded):
        reserve(guard, batch["items"][0], "inference", 99, tokens=1)


def test_explicit_gapfill_admission_preserves_stable_metadata_and_two_item_entrypoint(gapfill_case):
    from backend.core.websearch_policy import OptionalWebPolicy

    store, batch, _, candidate = gapfill_case
    guard = real_pilot.RealPilotGuard(store, batch)
    original_recovery = copy.deepcopy(guard.recovery)
    before = all_records(store)
    worker.RealBatchProcessor(
        store, batch, guard, optional_web_policy=OptionalWebPolicy.model_validate(candidate["public_web_policy"]),
    )
    assert guard.recovery == original_recovery and guard.active_recovery == candidate
    assert all_records(store) == before
    with pytest.raises(ValueError, match="before_execution"):
        reserve(guard, batch["items"][0], "inference", 0, tokens=10)
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    with pytest.raises(ValueError, match="exact two-item"):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=1)
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == ledger


@pytest.mark.parametrize("field,value", [
    ("additional_inference", 2), ("additional_input_tokens", 151647), ("additional_output_tokens", 2049),
    ("max_requests", 7), ("max_output_tokens", 14336), ("incremental_microdollars", 5000001),
    ("fixed_cost_microdollars", 197999),
])
def test_no_larger_grants_or_smaller_costs(gapfill_case, field, value):
    store, batch, approval, candidate = gapfill_case
    before = all_records(store)
    candidate = {**candidate, field: value}
    with pytest.raises(ValueError):
        real_pilot.validate_gapfill(candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store)
    assert all_records(store) == before


def test_canonical_record_order_does_not_mint_or_reject_authority(gapfill_case):
    store, batch, approval, candidate = gapfill_case
    for key in [real_pilot.BUDGET_KEY, real_pilot.ROW_RERUN_AUDIT_KEY,
                *candidate["result_sha256"], *candidate["cached_documents"],
                *[f"items/{batch['id']}/{key}.json" for key in candidate["selected_item_keys"]]]:
        value, version = read_json(store, key)
        store.write_bytes(key, json.dumps(value, sort_keys=True, indent=3).encode(), version)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert real_pilot.validate_gapfill(candidate, approval, batch, ledger, store) == candidate
    start(gapfill_case)


@pytest.mark.parametrize("operation", ["analysis", "retrieval"])
def test_no_new_analysis_or_internal_retrieval(gapfill_case, operation):
    guard = start(gapfill_case)
    item = gapfill_case[1]["items"][0]
    key = guard.operation_key(item, tier=operation, source_version="forbidden", prompt_version="test")
    with pytest.raises(ValueError):
        guard.reserve(operation, key, item_key=item["item_key"], analysis_pages=1 if operation == "analysis" else 0)


def test_one_web_inference_per_item_even_if_internal_attempts_are_missing(gapfill_case):
    guard = start(gapfill_case)
    item = gapfill_case[1]["items"][0]
    reserve(guard, item, "search", 0)
    reserve(guard, item, "inference", 0, tokens=100)
    with pytest.raises(real_pilot.RealPilotBudgetExceeded, match="gapfill_web_inference"):
        reserve(guard, item, "inference", 1, tokens=100)


def test_full_600_seconds_required_without_charging(gapfill_case, monkeypatch):
    store, batch, _, candidate = gapfill_case
    before = read_json(store, real_pilot.BUDGET_KEY)[0]
    monkeypatch.setattr(real_pilot, "_now", lambda: datetime.fromisoformat(candidate["expires_at"]) - timedelta(seconds=599))
    guard = real_pilot.RealPilotGuard(store, batch)
    with pytest.raises(ValueError, match="600-second"):
        guard.before_execution("too-late")
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == before


def test_incremental_cost_envelope_includes_web_and_fixed_costs(gapfill_case):
    store, batch, approval, candidate = gapfill_case
    candidate = copy.deepcopy(candidate)
    candidate["web_prices"]["search"] = "3"
    with pytest.raises(ValueError, match="incremental envelope"):
        real_pilot.validate_gapfill(candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store)


@pytest.mark.parametrize("field", ["input_token", "output_token", "search"])
def test_zero_price_is_not_allowed_for_model_or_paid_search(gapfill_case, field):
    approval = copy.deepcopy(gapfill_case[2])
    approval["unit_prices_usd"].update(search="0.0125", web_retrieval="0")
    approval["unit_prices_usd"][field] = "0"
    with pytest.raises(ValueError, match="must be positive"):
        real_pilot.RealPilotGuard._cost(approval, "web_retrieval", 0, 0, 0)


def test_nonsecret_configuration_builder_uses_exact_owner_prices_and_existing_public_identities(gapfill_case):
    store, batch, _, _ = gapfill_case
    records = {key: json.loads(raw) for key, (raw, _) in all_records(store).items() if key.endswith(".json")}
    terms = {item["item_key"]: {entry["attribute_id"]: entry["attribute_id"]
                               for entry in item["manifest"]["attributes"]} for item in batch["items"][:2]}
    configuration = helper.web_configuration(records, terms)
    assert configuration["web_prices"] == {"search": "0.0125", "web_retrieval": "0"}
    assert configuration["fixed_cost_microdollars"] == 198000
    expected = policy_for(batch)
    expected["max_cost_microdollars"] = real_pilot.RealPilotGuard._cost(
        records[real_pilot.APPROVAL_KEY], "inference", 52000, 4096, 0,
    ) + 25000
    from backend.core.websearch_policy import OptionalWebPolicy

    assert OptionalWebPolicy.model_validate(configuration["public_web_policy"]) == OptionalWebPolicy.model_validate(expected)
    assert configuration["web_environment"]["WEBIQ_ENDPOINT"] == "https://api.microsoft.ai/v3/search/web"
    assert "WEBIQ_API_KEY" not in json.dumps(configuration)
    assert configuration["public_web_policy"]["items"]["row-2"]["mpn"] == batch["items"][0]["manifest"]["product"]["mpn"]

@pytest.mark.parametrize("endpoint", [
    "https://api.microsoft.ai/v3/search/browse", "https://api.microsoft.ai/web", "https://webiq.invalid",
])
def test_only_immutable_webiq_endpoint_is_authorized(gapfill_case, endpoint):
    store, batch, approval, candidate = gapfill_case
    candidate = copy.deepcopy(candidate)
    candidate["web_environment"]["WEBIQ_ENDPOINT"] = endpoint
    with pytest.raises(ValueError, match="exact WebIQ"):
        real_pilot.validate_gapfill(candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store)


def test_runtime_public_policy_drift_is_fatal(gapfill_case, monkeypatch):
    guard = start(gapfill_case)
    monkeypatch.setenv("DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON", "{}")
    with pytest.raises(ValueError):
        reserve(guard, gapfill_case[1]["items"][0], "inference", 0, tokens=10)
    assert read_json(gapfill_case[0], real_pilot.BUDGET_KEY)[0]["invalidated"]


def test_fatal_guard_stops_queued_second_item(gapfill_case, monkeypatch):
    store, batch, _, _ = gapfill_case
    calls = []
    original = real_pilot.RealPilotGuard.reserve
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    install_reproduction(monkeypatch, batch)

    def denied(self, *args, **kwargs):
        calls.append(kwargs["item_key"])
        raise ValueError("REPRODUCTION fatal reservation denial")

    monkeypatch.setattr(real_pilot.RealPilotGuard, "reserve", denied)
    with pytest.raises(Exception):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
    assert calls and set(calls) == {batch["items"][0]["item_key"]}
    assert read_json(store, f"items/{batch['id']}/{batch['items'][1]['item_key']}.json")[0]["state"] == "recovery_ready"
    monkeypatch.setattr(real_pilot.RealPilotGuard, "reserve", original)


def install_reproduction(monkeypatch, batch):
    calls, discovery, retrieval = [], [], []
    captured = worker.prepare_inference_request
    requests = []

    def prepare(*args, **kwargs):
        value = captured(*args, **kwargs)
        requests.append(value)
        return value

    class Client:
        def __init__(self, **kwargs):
            self.sync_client, self.last_usage, self.last_response_sha256 = self, None, None

        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self

        def close(self):
            pass

        def complete_structured(self, system, user, schema, **kwargs):
            assert kwargs["max_completion_tokens"] == 2048
            assert requests[-1].system == system and requests[-1].user == user
            calls.append(json.loads(user))
            return ExtractionResponse(candidates=[])

    def search(self, query, allowed_domains=None, **kwargs):
        discovery.append(query)
        return [WebIQSearchResult(
            url="https://" + allowed_domains[0] + "/reproduction-only",
            title=LABEL, content="DISCOVERY ONLY, NEVER ATTRIBUTE EVIDENCE",
            retrieved_at=datetime.now(timezone.utc),
        )]

    def fetch(url, **kwargs):
        retrieval.append(url)
        # Identity only, no invented claim can become an attribute finding.
        text = LABEL + " Mueller " + " ".join(item["manifest"]["product"]["mpn"] for item in batch["items"][:2])
        return SimpleNamespace(final_url=url, text=text, media_type="text/html",
                               content_hash=hashlib.sha256(text.encode()).hexdigest(),
                               retrieved_at=datetime.now(timezone.utc))

    monkeypatch.setattr(worker, "prepare_inference_request", prepare)
    monkeypatch.setattr(worker, "LLMClient", Client)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient", type(
        "ReproductionDiscovery", (WebIQSearchClient,), {
            "__init__": lambda self: WebIQSearchClient.__init__(self, api_key="synthetic-no-send-only"),
            "search": search,
        },
    ))
    monkeypatch.setattr("backend.core.websearch.fetch_original_page", fetch)
    monkeypatch.setattr(worker, "DocumentIntelligenceService", lambda *a, **k: pytest.fail("No new analysis"))
    return calls, requests, discovery, retrieval


def test_private_retained_store_real_guard_six_payload_gate(monkeypatch):
    root_value = os.environ.get("DOCINTEL_TEST_GAPFILL_WORK")
    if not root_value:
        pytest.skip("Private retained snapshot opt-in not supplied")
    root = Path(root_value).resolve()
    source_files = helper.release.command(
        ["git", "ls-tree", "-r", "--name-only", helper.BASELINE_REVISION, "--", "backend", "frontend"],
        cwd=helper.release.ROOT,
    ).decode().splitlines()
    for name in source_files:
        if name != "backend/real_pilot.py":
            expected = helper.expected_worker_source() if name == "backend/batch_worker.py" else helper.release.command(
                ["git", "show", helper.BASELINE_REVISION + ":" + name], cwd=helper.release.ROOT,
            )
            assert (helper.release.ROOT / name).read_bytes() == expected, f"Unapproved business-pipeline change: {name}"
    snapshot = helper.snapshot(root)
    before = (root / "row-rerun-actual-outcome.json").read_bytes()
    records = {key: (base64.b64decode(value["base64"], validate=True), value["version"])
               for key, value in snapshot["records"].items()}
    store = WorkerMemoryStore(records)
    approval = read_json(store, real_pilot.APPROVAL_KEY)[0]
    batch = read_json(store, f"batches/{approval['batch_id']}.json")[0]
    assert real_pilot.RealPilotGuard._cost(approval, "inference", 151646, 12288, 0) + 25000 + 198000 == 772052
    inject_approved_documents(store, batch, Path(os.environ["DOCINTEL_TEST_GAPFILL_DOCUMENTS"]))
    configuration_name = os.environ.get("DOCINTEL_TEST_GAPFILL_CONFIGURATION")
    configuration = None
    if configuration_name:
        assert Path(configuration_name).name == configuration_name
        configuration = helper.release.private_json(root / configuration_name)
    candidate = candidate_for(store, batch, configuration)
    write_json(store, real_pilot.GAPFILL_KEY, candidate)
    runtime(monkeypatch, approval, candidate)
    ledger_before = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert real_pilot.validate_gapfill(candidate, approval, batch, ledger_before, store) == candidate
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    ready_records = store.snapshot()
    calls, requests, discovery, retrieval = install_reproduction(monkeypatch, batch)
    worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
    assert len(calls) == len(requests) == 6
    tiers = [value["evidence_groups"][0]["source_tier"] for value in calls]
    assert tiers == ["internal_pdf", "vendor_table", "manufacturer_web"] * 2
    internal = sum(request.input_bound for request, tier in zip(requests, tiers) if tier != "manufacturer_web")
    assert internal == 99646
    assert sum(request.input_bound for request in requests) <= 151646
    assert len(discovery) == 2 and len(retrieval) == 4
    assert all("DISCOVERY ONLY" not in request.user for request in requests)
    assert all(request.input_bound <= 26000 for request, tier in zip(requests, tiers) if tier == "manufacturer_web")
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert ledger["attempted"]["inference"] == 17 and len(ledger["executions"]) == 5
    assert ledger["reserved"]["output_tokens"] == 34816
    assert ledger["reserved"]["input_tokens"] - 257868 == sum(request.input_bound for request in requests)
    assert all(ledger["reservations"][key] == value for key, value in ledger_before["reservations"].items())
    for key, (raw, _) in records.items():
        if key.startswith(("results/", "response-diagnostics/", "configuration/", "operations/", "parses/")):
            assert store.read_bytes(key)[0] == raw
    service = BatchService(store)
    for key in candidate["selected_item_keys"]:
        detail = service.detail(batch["id"], key, batch["owner"])
        previous_state = json.loads(records[f"items/{batch['id']}/{key}.json"][0])
        assert any(attempt["result_key"] == previous_state["result_key"]
                   and attempt["machine_result"] == snapshot["details"][key]["machine_result"]
                   for attempt in detail["attempt_history"])
        with pytest.raises(Exception):
            service.detail(batch["id"], key, "other-owner")
    exported = service.export(batch["id"], batch["owner"])
    assert len(read_workbook(exported)["Attempts"]) >= 8
    assert (root / "row-rerun-actual-outcome.json").read_bytes() == before
    after = all_records(store)
    worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
    assert all_records(store) == after
    fatal_store = WorkerMemoryStore(ready_records)
    fatal_calls = []
    with monkeypatch.context() as fatal_patch:
        def denied(self, *args, **kwargs):
            fatal_calls.append(kwargs["item_key"])
            raise ValueError("REPRODUCTION fatal authorization denial")
        fatal_patch.setattr(real_pilot.RealPilotGuard, "reserve", denied)
        from backend.extract import ExecutionConfigurationError
        with pytest.raises(ExecutionConfigurationError):
            worker.run_batch(fatal_store, batch["id"], concurrency=1, item_limit=2)
    assert fatal_calls and set(fatal_calls) == {candidate["selected_item_keys"][0]}
    second = read_json(fatal_store, f"items/{batch['id']}/{candidate['selected_item_keys'][1]}.json")[0]
    assert second["state"] == "recovery_ready"
    missing_store = WorkerMemoryStore(ready_records)
    with monkeypatch.context() as missing_patch:
        missing_calls, _, missing_discovery, missing_retrieval = install_reproduction(missing_patch, batch)
        from backend.core.config import settings
        missing_patch.setattr(settings, "WEBIQ_API_KEY", None)
        missing_patch.delenv("WEBIQ_API_KEY", raising=False)
        missing_patch.setattr("backend.core.websearch_webiq.WebIQSearchClient", WebIQSearchClient)
        missing_patch.setattr(WebIQSearchClient, "search", lambda *a, **k: pytest.fail("Absent key must not search"))
        missing_patch.setattr("backend.core.websearch.fetch_original_page",
                              lambda *a, **k: pytest.fail("Absent key must not fetch"))
        worker.run_batch(missing_store, batch["id"], concurrency=1, item_limit=2)
    assert len(missing_calls) == 4 and missing_discovery == missing_retrieval == []
    assert [value["evidence_groups"][0]["source_tier"] for value in missing_calls] == [
        "internal_pdf", "vendor_table", "internal_pdf", "vendor_table",
    ]
    missing_ledger = read_json(missing_store, real_pilot.BUDGET_KEY)[0]
    assert missing_ledger["attempted"]["inference"] == 15
    assert missing_ledger["attempted"]["search"] == missing_ledger["attempted"]["web_retrieval"] == 0
    assert missing_ledger["reserved"]["input_tokens"] == 257868 + 99646
    for key in candidate["selected_item_keys"]:
        missing_detail = BatchService(missing_store).detail(batch["id"], key, batch["owner"])
        assert any(entry.get("skip_reason") == "optional_provider_unavailable"
                   and entry.get("discovery_error") == "not_configured" for entry in missing_detail["provenance"])
    output = os.environ.get("DOCINTEL_TEST_GAPFILL_OUTPUT")
    if output:
        assert Path(output).name == output and output.startswith("gapfill-")
        assert configuration_name, "A usable gate must pin reviewed real endpoint/prices/public policy, not mock defaults"
        revision = helper.release.command(["git", "rev-parse", "HEAD"], cwd=helper.release.ROOT).decode().strip()
        for name in helper.SUCCESSOR_FILES - {"DEPLOYMENT.md", "BATCH.md"}:
            assert (helper.release.ROOT / name).read_bytes() == helper.release.command(
                ["git", "show", revision + ":" + name], cwd=helper.release.ROOT,
            ), "Gate output requires exact committed Track A source"
        scope = {key: value for key, value in candidate.items() if key not in helper.TIME_FIELDS}
        helper.release.save_once(root / output, {
            "passed": True, "no_live_operations": True, "label": LABEL, "source_revision": revision,
            "snapshot_sha256": helper.digest_file(root / "row-rerun-actual-outcome.json"),
            "scope_sha256": helper.sha(scope), "amendment_scope": scope,
            "remote_object_sha256": {key: helper.sha(json.loads(raw)) for key, (raw, _) in records.items()},
            "checks": {name: True for name in helper.GATE_CHECKS},
            "full_internal_input_tokens": internal, "six_request_input_cap": 151646,
            "six_request_output_cap": 12288, "historical_forecasts_stacked": False,
            "requests": [{"input_tokens": request.input_bound, "output_tokens": 2048,
                          "source_tier": tier, "accounting": request.accounting}
                         for request, tier in zip(requests, tiers)],
        })
