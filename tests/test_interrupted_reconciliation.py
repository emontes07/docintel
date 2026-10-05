"""Offline reconciliation tests; private receipts are loaded only by explicit opt-in."""

import base64
import copy
import hashlib
import json
import os
import socket
import sqlite3
from pathlib import Path

import pytest

from backend.batch import BatchService
from backend.batch_store import Conflict, SQLiteStore, read_json, write_json
from backend.interrupted_reconciliation import EXECUTION_AUDIT_KEY, LEDGER_KEY
from backend.real_pilot import binding_digest


BATCH = "b" * 64
EXECUTION = "e" * 64
RECOVERY = "a" * 64
OPERATOR = "00000000-0000-4000-8000-000000000001"
IMAGE = "synthetic.invalid/worker@sha256:" + "f" * 64
START = "2026-10-01T10:00:00+00:00"
ITEM_START = "2026-10-01T10:00:02+00:00"
STOP = "2026-10-01T10:00:03+00:00"
OBSERVED = "2026-10-01T10:00:04+00:00"


def raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


class MemorySQLiteStore(SQLiteStore):
    """Exercise the production SQLite CAS and leases without any filesystem writes."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE records (key TEXT PRIMARY KEY, value BLOB NOT NULL, version TEXT NOT NULL)")
        self.writes = []
        self.before_write = None

    def connect(self):
        return self.connection

    def write_bytes(self, key, value, version=None):
        if self.before_write:
            self.before_write(key, value, version)
        token = super().write_bytes(key, value, version)
        self.writes.append(key)
        return token

    def snapshot(self):
        return {key: self.read_bytes(key) for key in self.keys("") if not key.startswith("locks/")}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Reconciliation must never contact external services")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", OPERATOR)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "false")
    monkeypatch.setenv("DOCINTEL_BATCH_LIVE_ENABLED", "false")
    monkeypatch.setattr("backend.batch_store.configured_store", blocked)


def request_for(store, batch_id, item_key, evidence):
    paths = {
        "batch": f"batches/{batch_id}.json", "item": f"items/{batch_id}/{item_key}.json",
        "ledger": LEDGER_KEY, "execution_audit": EXECUTION_AUDIT_KEY,
    }
    return {
        "expected": {
            name: {"version": version, "sha256": sha(content)}
            for name, path in paths.items() for content, version in [store.read_bytes(path)]
        },
        "evidence": evidence, "evidence_sha256": sha(raw(evidence)),
    }


@pytest.fixture
def case():
    store = MemorySQLiteStore()
    batch = {
        "id": BATCH, "owner": "synthetic-owner", "state": "running", "mode": "real_pilot",
        "items": [{"item_key": "row-2"}, {"item_key": "row-3"}, {"item_key": "row-4"}],
        "product_count": 3, "progress": {"finished": 0, "failed": 0, "unresolved": 0},
        "input_hashes": {}, "attribute_reference": "synthetic", "original_definitions": [],
    }
    previous = {
        "id": "row-2", "batch_id": BATCH, "state": "unresolved", "error": "prior mismatch",
        "previous_attempt": {"state": "interrupted", "error": "original stopped attempt"},
    }
    state = {
        "id": "row-2", "batch_id": BATCH, "state": "running", "started_at": ITEM_START,
        "requested_mode": "real_pilot", "recovery_sha256": RECOVERY,
        "result_key": f"results/{BATCH}/row-2/attempts/{RECOVERY}.json",
        "previous_attempt": previous,
    }
    ledger = {
        "batch_id": BATCH, "owner": batch["owner"], "approved_by": OPERATOR,
        "batch_sha256": binding_digest(batch),
        "executions": {EXECUTION: {"started_at": START}},
        "final_rerun": {"execution_id": EXECUTION, "selected_item_keys": ["row-2"], "sha256": RECOVERY},
        "attempted": {"analysis": 1, "inference": 7}, "reserved": {"input_tokens": 10000},
        "reservations": {"synthetic-in-flight": {"status": "reserved"}}, "invalidated": None,
    }
    execution_audit = {
        "execution_id": EXECUTION, "amendment_sha256": RECOVERY, "recorded_at": START,
        "amendment": {"batch_sha256": binding_digest(batch), "selected_item_keys": ["row-2"]},
    }
    records = {
        f"batches/{BATCH}.json": batch, f"items/{BATCH}/row-2.json": state,
        f"items/{BATCH}/row-3.json": {"id": "row-3", "batch_id": BATCH, "state": "queued"},
        f"items/{BATCH}/row-4.json": {"id": "row-4", "batch_id": BATCH, "state": "deferred"},
        LEDGER_KEY: ledger, EXECUTION_AUDIT_KEY: execution_audit,
        f"results/{BATCH}/row-2.json": {"synthetic_original_result": "immutable"},
        f"response-diagnostics/{BATCH}/row-2/retained.json": {"diagnostic": "synthetic prior mismatch"},
    }
    for key, value in records.items():
        write_json(store, key, value)
    evidence = {
        "execution_id": EXECUTION,
        "execution": {
            "temporary_processing_closed": True, "observed_at": OBSERVED,
            "execution": {
                "id": "/subscriptions/synthetic/resourceGroups/synthetic/providers/Microsoft.App/jobs/synthetic/executions/stopped-1",
                "name": "stopped-1",
                "properties": {
                    "status": "Stopped", "startTime": START,
                    "template": {"containers": [{
                        "image": IMAGE,
                        "args": ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", BATCH,
                                 "--concurrency", "1", "--max-batches", "1", "--item-limit", "2"],
                    }]},
                },
            },
        },
        "worker": {"active_executions": 0, "manual_worker": "Manual", "worker_image": IMAGE, "observed_at": OBSERVED},
        "timing": {
            "observed_at": OBSERVED,
            "events": [{"eventTimestamp": STOP, "operationName": {"value": "Microsoft.App/jobs/stop/action"},
                        "status": {"value": "Succeeded"}}],
        },
    }
    return store, request_for(store, BATCH, "row-2", evidence)


def reconcile(case):
    store, request = case
    return BatchService(store).reconcile_interrupted(BATCH, "row-2", OPERATOR, **request)


def test_reconciliation_is_auditable_idempotent_status_only(case, monkeypatch):
    def no_execution(*args, **kwargs):
        raise AssertionError("Reconciliation must not instantiate a live execution guard")

    monkeypatch.setattr("backend.real_pilot.RealPilotGuard.__init__", no_execution)
    store, request = case
    before = store.snapshot()
    receipt = reconcile(case)
    state = read_json(store, f"items/{BATCH}/row-2.json")[0]
    batch = read_json(store, f"batches/{BATCH}.json")[0]
    original = json.loads(before[f"items/{BATCH}/row-2.json"][0])
    assert state["state"] == batch["state"] == "interrupted"
    assert state["previous_attempt"] == original["previous_attempt"]
    assert state["result_key"] == original["result_key"]
    assert state["started_at"] == original["started_at"]
    assert "finished_at" not in state
    assert state["interruption"]["remote_completion"] == "unknown"
    assert batch["progress"] == {"finished": 1, "failed": 1, "unresolved": 0, "deferred": 1}
    allowed = {f"items/{BATCH}/row-2.json", f"batches/{BATCH}.json"}
    for key, value in before.items():
        if key not in allowed:
            assert store.read_bytes(key) == value
    audit, _ = read_json(store, receipt["audit_key"])
    assert audit["kind"] == "interrupted_reconciliation_intent"
    assert audit["request"]["expected"] == request["expected"]
    assert audit["evidence"] == request["evidence"]
    assert audit["current_attempt_result_absent"] is True
    for name, key in (("batch", f"batches/{BATCH}.json"), ("item", f"items/{BATCH}/row-2.json")):
        assert base64.b64decode(audit["prior"][name]["base64"]) == before[key][0]
        assert audit["prior"][name]["version"] == before[key][1]
    assert set(store.snapshot()) - set(before) == {
        receipt["audit_key"], receipt["audit_key"].removesuffix(".json") + ".applied.json",
    }
    after = store.snapshot()
    assert reconcile(case) == receipt
    assert store.snapshot() == after
    # The batch is terminal even if a later caller invokes the ordinary worker.
    from backend.batch_worker import run_batch

    run_batch(store, BATCH, processor=no_execution)
    assert store.snapshot() == after


@pytest.mark.parametrize("field", ["batch", "item", "ledger", "execution_audit"])
@pytest.mark.parametrize("binding", ["version", "sha256"])
def test_stale_expected_state_or_version_is_rejected_without_record_writes(case, field, binding):
    store, request = case
    request["expected"][field][binding] = "0" * 64
    before = store.snapshot()
    with pytest.raises(Conflict):
        reconcile(case)
    assert store.snapshot() == before


@pytest.mark.parametrize("mutation", [
    "status", "not_closed", "active", "not_manual", "image", "identity", "batch_args",
    "no_stop", "stop_failed", "stop_before_item", "old_observation", "wrong_execution",
    "future_observation",
])
def test_untrustworthy_stop_evidence_is_rejected(case, mutation):
    store, request = case
    evidence = request["evidence"]
    execution = evidence["execution"]["execution"]
    if mutation == "status":
        execution["properties"]["status"] = "Running"
    elif mutation == "not_closed":
        evidence["execution"]["temporary_processing_closed"] = False
    elif mutation == "active":
        evidence["worker"]["active_executions"] = 1
    elif mutation == "not_manual":
        evidence["worker"]["manual_worker"] = "Schedule"
    elif mutation == "image":
        evidence["worker"]["worker_image"] = "synthetic.invalid/worker:mutable"
    elif mutation == "identity":
        execution["name"] = "other-execution"
    elif mutation == "batch_args":
        execution["properties"]["template"]["containers"][0]["args"][4] = "0" * 64
    elif mutation == "no_stop":
        evidence["timing"]["events"] = []
    elif mutation == "stop_failed":
        evidence["timing"]["events"][0]["status"]["value"] = "Failed"
    elif mutation == "stop_before_item":
        evidence["timing"]["events"][0]["eventTimestamp"] = START
    elif mutation == "old_observation":
        evidence["worker"]["observed_at"] = START
    elif mutation == "wrong_execution":
        evidence["execution_id"] = "0" * 64
    elif mutation == "future_observation":
        evidence["worker"]["observed_at"] = "2200-01-01T00:00:00+00:00"
    request["evidence_sha256"] = sha(raw(evidence))
    before = store.snapshot()
    with pytest.raises(ValueError):
        reconcile(case)
    assert store.snapshot() == before


def test_evidence_must_match_trusted_pin(case):
    store, request = case
    request["evidence"]["timing"]["scope"] = "altered after approval"
    before = store.snapshot()
    with pytest.raises(ValueError, match="trusted pin"):
        reconcile(case)
    assert store.snapshot() == before


@pytest.mark.parametrize("environment", ["DOCINTEL_REAL_PILOT_ENABLED", "DOCINTEL_BATCH_LIVE_ENABLED"])
def test_processing_must_stay_disabled(case, monkeypatch, environment):
    monkeypatch.setenv(environment, "true")
    before = case[0].snapshot()
    with pytest.raises(ValueError, match="disabled"):
        reconcile(case)
    assert case[0].snapshot() == before


@pytest.mark.parametrize("actor", ["untrusted", ""])
def test_operator_is_not_an_owner_supplied_claim(case, actor):
    store, request = case
    with pytest.raises(ValueError, match="trusted reconciliation operator"):
        BatchService(store).reconcile_interrupted(BATCH, "row-2", actor, **request)


def test_operator_must_match_consumed_approval(case, monkeypatch):
    store, request = case
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "another-operator")
    with pytest.raises(ValueError, match="consumed approval"):
        BatchService(store).reconcile_interrupted(BATCH, "row-2", "another-operator", **request)


def test_current_machine_result_is_never_replaced_or_relabelled_unknown(case):
    store, _ = case
    state = read_json(store, f"items/{BATCH}/row-2.json")[0]
    write_json(store, state["result_key"], {"synthetic": "persisted current result"})
    before = store.snapshot()
    with pytest.raises(Conflict, match="current-attempt result"):
        reconcile(case)
    assert store.snapshot() == before


@pytest.mark.parametrize("pointer", ["root", "another_attempt"])
def test_result_pointer_must_bind_the_exact_final_attempt(case, pointer):
    store, request = case
    key = f"items/{BATCH}/row-2.json"
    state, version = read_json(store, key)
    state["result_key"] = (
        f"results/{BATCH}/row-2.json" if pointer == "root"
        else f"results/{BATCH}/row-2/attempts/{'0' * 64}.json"
    )
    write_json(store, key, state, version)
    request["expected"] = request_for(store, BATCH, "row-2", request["evidence"])["expected"]
    before = store.snapshot()
    with pytest.raises(Conflict, match="result pointer"):
        reconcile(case)
    assert store.snapshot() == before


def test_another_running_item_is_not_silently_reconciled(case):
    store, _ = case
    key = f"items/{BATCH}/row-3.json"
    state, version = read_json(store, key)
    state["state"] = "running"
    write_json(store, key, state, version)
    before = store.snapshot()
    with pytest.raises(Conflict, match="Another running item"):
        reconcile(case)
    assert store.snapshot() == before


def test_ambiguous_execution_identity_is_rejected(case):
    store, request = case
    ledger, version = read_json(store, LEDGER_KEY)
    ledger["executions"]["0" * 64] = {"started_at": START}
    write_json(store, LEDGER_KEY, ledger, version)
    request["expected"] = request_for(store, BATCH, "row-2", request["evidence"])["expected"]
    before = store.snapshot()
    with pytest.raises(ValueError, match="does not bind"):
        reconcile(case)
    assert store.snapshot() == before


@pytest.mark.parametrize("state_name", ["queued", "unresolved", "interrupted", "recovery_ready", "failed", "deferred"])
def test_only_running_item_can_be_reconciled(case, state_name):
    store, request = case
    key = f"items/{BATCH}/row-2.json"
    state, version = read_json(store, key)
    state["state"] = state_name
    write_json(store, key, state, version)
    request["expected"] = request_for(store, BATCH, "row-2", request["evidence"])["expected"]
    before = store.snapshot()
    with pytest.raises(Conflict, match="exact running"):
        reconcile(case)
    assert store.snapshot() == before


@pytest.mark.parametrize("lock", [BATCH, LEDGER_KEY])
def test_active_lease_prevents_reconciliation(case, lock):
    store, _ = case
    before = store.snapshot()
    with store.lease(lock):
        with pytest.raises(Conflict):
            reconcile(case)
    assert store.snapshot() == before


@pytest.mark.parametrize("failure_point", ["intent", "batch", "item", "applied"])
def test_write_failure_is_resumable_with_same_pins_without_reopening_batch(case, failure_point):
    store, _ = case
    before = store.snapshot()
    tripped = False

    def fail_once(key, value, version):
        nonlocal tripped
        matches = {
            "intent": key.startswith("operations/interrupted-reconciliation/") and not key.endswith(".applied.json"),
            "batch": key == f"batches/{BATCH}.json",
            "item": key == f"items/{BATCH}/row-2.json",
            "applied": key.endswith(".applied.json"),
        }
        if matches[failure_point] and not tripped:
            tripped = True
            raise OSError("Synthetic interruption before write")

    store.before_write = fail_once
    with pytest.raises(OSError):
        reconcile(case)
    if failure_point in {"item", "applied"}:
        assert read_json(store, f"batches/{BATCH}.json")[0]["state"] == "interrupted"
    store.before_write = None
    receipt = reconcile(case)
    assert reconcile(case) == receipt
    assert store.read_bytes(LEDGER_KEY) == before[LEDGER_KEY]
    assert read_json(store, f"items/{BATCH}/row-2.json")[0]["state"] == "interrupted"


def test_cas_race_is_not_overwritten(case):
    store, _ = case
    key = f"items/{BATCH}/row-2.json"
    raced = False

    def race(path, value, version):
        nonlocal raced
        if path == key and not raced:
            raced = True
            original, original_version = read_json(store, key)
            original["concurrent_observation"] = "must not overwrite"
            SQLiteStore.write_bytes(store, key, raw(original), original_version)

    store.before_write = race
    with pytest.raises(Conflict):
        reconcile(case)
    assert read_json(store, key)[0]["state"] == "running"
    assert read_json(store, key)[0]["concurrent_observation"] == "must not overwrite"
    assert read_json(store, f"batches/{BATCH}.json")[0]["state"] == "interrupted"
    with pytest.raises(Conflict):
        reconcile(case)


def test_changed_pin_cannot_replace_existing_intent(case):
    store, request = case
    reconcile(case)
    request["evidence"]["timing"]["scope"] = "different operator claim"
    request["evidence_sha256"] = sha(raw(request["evidence"]))
    before = store.snapshot()
    with pytest.raises(Conflict, match="different reconciliation intent"):
        reconcile(case)
    assert store.snapshot() == before


def rehydrate_private_snapshot(snapshot):
    """Reconstruct an isolated store from API JSON, not original hosted bytes/ETags."""
    store = MemorySQLiteStore()
    batch = copy.deepcopy(snapshot["batch"])
    batch_id = batch["id"]
    for key, value in (
        (f"batches/{batch_id}.json", batch), (LEDGER_KEY, snapshot["ledger"]),
        (EXECUTION_AUDIT_KEY, snapshot["final_audit"]),
    ):
        write_json(store, key, value)
    derived = {
        "attempt_history", "machine_result", "machine_sha256", "reviewed_result",
        "reviewer_identity", "original", "row", "validation_diagnostics",
    }
    for item_key, detail in snapshot["details"].items():
        state = {key: value for key, value in detail.items() if key not in derived}
        write_json(store, f"items/{batch_id}/{item_key}.json", state)
        machines = {
            attempt["result_key"]: attempt["machine_result"]
            for attempt in detail.get("attempt_history", []) if attempt["machine_result"] is not None
        }
        if detail.get("machine_result") is not None:
            machines[BatchService.result_record_key(batch_id, item_key, state)] = detail["machine_result"]
        for key, machine in machines.items():
            write_json(store, key, machine)
        for index, diagnostic in enumerate(detail.get("validation_diagnostics", [])):
            write_json(store, f"response-diagnostics/{batch_id}/{item_key}/snapshot-{index}.json", diagnostic)
    return store


@pytest.mark.skipif(not os.environ.get("DOCINTEL_RECONCILIATION_PRIVATE_WORK"), reason="Private offline receipt rehearsal is opt-in")
def test_private_postclosure_snapshot_rehearsal(monkeypatch):
    home = Path(os.environ["DOCINTEL_RECONCILIATION_PRIVATE_WORK"])
    names = {
        "snapshot": "final-postclosure-private-snapshot.json",
        "execution": "final-postclosure-execution.json",
        "worker": "final-deployed-images-and-stopped-worker.json",
        "timing": "final-worker-timing-observations.json",
    }
    receipt_bytes = {key: (home / name).read_bytes() for key, name in names.items()}
    receipts = {key: json.loads(value) for key, value in receipt_bytes.items()}
    snapshot = receipts["snapshot"]
    store = rehydrate_private_snapshot(snapshot)
    batch_id, actor = snapshot["batch"]["id"], snapshot["ledger"]["approved_by"]
    running = [key for key, detail in snapshot["details"].items() if detail["state"] == "running"]
    if len(running) != 1:
        raise AssertionError("Private rehearsal requires exactly one stale running item")
    item_key = running[0]
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", actor)
    evidence = {
        "execution_id": snapshot["final_audit"]["execution_id"],
        **{key: receipts[key] for key in ("execution", "worker", "timing")},
    }
    request = request_for(store, batch_id, item_key, evidence)
    before = store.snapshot()
    receipt = BatchService(store).reconcile_interrupted(batch_id, item_key, actor, **request)
    state = read_json(store, f"items/{batch_id}/{item_key}.json")[0]
    if state["state"] != "interrupted" or state["previous_attempt"] != snapshot["details"][item_key]["previous_attempt"]:
        raise AssertionError("Private current-state/history reconciliation did not match")
    changed = {key for key, value in before.items() if store.read_bytes(key) != value}
    if changed != {f"items/{batch_id}/{item_key}.json", f"batches/{batch_id}.json"}:
        raise AssertionError("Private rehearsal changed a result, diagnostic, ledger, or another item")
    after = store.snapshot()
    replay = BatchService(store).reconcile_interrupted(batch_id, item_key, actor, **request)
    if receipt != replay or store.snapshot() != after:
        raise AssertionError("Private reconciliation was not idempotent")
    if any((home / names[key]).read_bytes() != value for key, value in receipt_bytes.items()):
        raise AssertionError("A source receipt was modified")
