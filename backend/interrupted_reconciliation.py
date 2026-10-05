"""Operator-pinned, status-only reconciliation; no service discovery or execution."""

import base64
import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.real_pilot import binding_digest


LEDGER_KEY = "budgets/real-pilot.json"
EXECUTION_AUDIT_KEY = "operations/real-pilot-final-rerun.json"
ERROR = (
    "Worker execution was stopped; no current-attempt machine result was persisted. "
    "Remote completion remains unknown. Reservations remain consumed; no automatic retry."
)


def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Reconciliation timestamps require a timezone")
    return parsed


def _validate_evidence(evidence, batch, item_key, state, ledger, execution_audit):
    """Validate pinned offline observations, not a claim that this reads live state."""
    try:
        if set(evidence) != {"execution_id", "execution", "worker", "timing"}:
            raise ValueError("Complete stopped-execution receipts are required")
        execution_id = evidence["execution_id"]
        receipt, worker, timing = evidence["execution"], evidence["worker"], evidence["timing"]
        execution = receipt["execution"]
        properties = execution["properties"]
        containers = properties["template"]["containers"]
        if len(containers) != 1:
            raise ValueError("Stopped worker identity is ambiguous")
        container = containers[0]
        args = container["args"]
        expected_args = [
            "-m", "backend.batch_worker", "--real-pilot", "--batch-id", batch["id"],
            "--concurrency", "1", "--max-batches", "1", "--item-limit", "2",
        ]
        if (
            properties["status"] != "Stopped"
            or receipt["temporary_processing_closed"] is not True
            or type(worker["active_executions"]) is not int or worker["active_executions"] != 0
            or worker["manual_worker"] != "Manual"
            or args != expected_args
            or not re.fullmatch(r".+@sha256:[a-f0-9]{64}", container["image"])
            or container["image"] != worker["worker_image"]
            or not re.fullmatch(
                r"/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.App/jobs/[^/]+/executions/[^/]+",
                execution["id"],
            )
            or execution["id"].rsplit("/", 1)[-1] != execution["name"]
        ):
            raise ValueError("Execution identity or stopped/closed evidence does not match")
        acknowledged = [
            _time(event["eventTimestamp"]) for event in timing["events"]
            if event["operationName"]["value"] == "Microsoft.App/jobs/stop/action"
            and event["status"]["value"] == "Succeeded"
        ]
        if not acknowledged:
            raise ValueError("A successful stop acknowledgement is required")
        stop_acknowledged_at = max(acknowledged)
        started = _time(properties["startTime"])
        observed = _time(receipt["observed_at"])
        checked_at = datetime.now(timezone.utc)
        if not (
            started <= _time(ledger["executions"][execution_id]["started_at"])
            <= _time(execution_audit["recorded_at"]) <= _time(state["started_at"])
            <= stop_acknowledged_at <= observed <= _time(worker["observed_at"])
            <= checked_at
            and stop_acknowledged_at <= _time(timing["observed_at"]) <= checked_at
        ):
            raise ValueError("Stopped evidence does not cover the running attempt")
        candidates = {
            key for key, entry in ledger["executions"].items()
            if started <= _time(entry["started_at"]) <= stop_acknowledged_at
        }
        if (
            candidates != {execution_id}
            or ledger["batch_id"] != batch["id"] or ledger["owner"] != batch["owner"]
            or ledger["batch_sha256"] != binding_digest(batch)
            or ledger["final_rerun"]["execution_id"] != execution_id
            or execution_audit["execution_id"] != execution_id
            or execution_audit["amendment_sha256"] != state["recovery_sha256"]
            or ledger["final_rerun"]["sha256"] != state["recovery_sha256"]
            or item_key not in ledger["final_rerun"]["selected_item_keys"]
            or execution_audit["amendment"]["batch_sha256"] != ledger["batch_sha256"]
            or item_key not in execution_audit["amendment"]["selected_item_keys"]
        ):
            raise ValueError("Stopped execution does not bind the final running attempt")
        return execution["id"], observed.isoformat()
    except (KeyError, TypeError, AttributeError, IndexError):
        raise ValueError("Incomplete or malformed stopped-execution evidence") from None


def reconcile_interrupted(store, batch_id, item_key, actor, *, expected, evidence, evidence_sha256):
    """Apply an exact, resumable write-ahead reconciliation under batch/budget leases.

    The trusted operator pins the receipt bundle out of band. This method never
    fetches evidence, invokes a processor, instantiates a guard, or adjusts budgets.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", batch_id) or not re.fullmatch(r"row-[1-9][0-9]*", item_key):
        raise ValueError("Exact batch/item identity is required")
    operators = {value.strip() for value in os.environ.get("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "").split(",") if value.strip()}
    if not actor or actor not in operators:
        raise ValueError("A trusted reconciliation operator is required")
    if any(os.environ.get(name, "").lower() == "true" for name in (
        "DOCINTEL_REAL_PILOT_ENABLED", "DOCINTEL_BATCH_LIVE_ENABLED",
    )):
        raise ValueError("Processing switches must remain disabled during reconciliation")
    evidence = copy.deepcopy(evidence)
    expected = copy.deepcopy(expected)
    if not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256) or _sha(_bytes(evidence)) != evidence_sha256:
        raise ValueError("Stopped-execution evidence differs from the operator's trusted pin")
    execution_id = evidence.get("execution_id", "")
    if not isinstance(execution_id, str) or not re.fullmatch(r"[a-f0-9]{64}", execution_id):
        raise ValueError("An exact consumed ledger execution identity is required")
    paths = {
        "batch": f"batches/{batch_id}.json", "item": f"items/{batch_id}/{item_key}.json",
        "ledger": LEDGER_KEY, "execution_audit": EXECUTION_AUDIT_KEY,
    }
    if set(expected) != set(paths) or any(
        not isinstance(value, dict) or set(value) != {"version", "sha256"}
        or not isinstance(value["version"], str) or not value["version"]
        or not isinstance(value["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
        for value in expected.values()
    ):
        raise ValueError("Exact current versions and raw SHA-256 pins are required")
    audit_key = f"operations/interrupted-reconciliation/{batch_id}/{item_key}/{execution_id}.json"
    applied_key = audit_key.removesuffix(".json") + ".applied.json"
    request = {
        "schema_version": 1, "batch_id": batch_id, "item_key": item_key,
        "operator": actor, "expected": expected, "evidence_sha256": evidence_sha256,
        "execution_id": execution_id,
    }
    with store.lease(batch_id) as renew_batch, store.lease(LEDGER_KEY) as renew_ledger:
        def fence():
            renew_batch()
            renew_ledger()

        current = {name: store.read_bytes(path) for name, path in paths.items()}
        for name in ("ledger", "execution_audit"):
            raw, version = current[name]
            if expected[name] != {"version": version, "sha256": _sha(raw)}:
                raise Conflict("Reconciliation consumption or execution audit changed")
        ledger = json.loads(current["ledger"][0])
        if ledger.get("approved_by") != actor:
            raise ValueError("Reconciliation operator does not match the consumed approval")
        try:
            audit, _ = read_json(store, audit_key)
        except Missing:
            audit = None
        if audit is None:
            for name in ("batch", "item"):
                raw, version = current[name]
                if expected[name] != {"version": version, "sha256": _sha(raw)}:
                    raise Conflict("Reconciliation state changed; do not refresh pins automatically")
            batch, state = (json.loads(current[name][0]) for name in ("batch", "item"))
            if (
                batch.get("id") != batch_id or batch.get("state") != "running"
                or batch.get("mode") != "real_pilot"
                or item_key not in {item["item_key"] for item in batch["items"]}
                or state.get("state") != "running" or state.get("id") != item_key
                or state.get("batch_id") != batch_id or state.get("requested_mode") != "real_pilot"
            ):
                raise Conflict("Only the exact running final attempt can be reconciled")
            execution_resource_id, observed = _validate_evidence(
                evidence, batch, item_key, state, ledger, json.loads(current["execution_audit"][0]),
            )
            from backend.batch import BatchService

            result_key = BatchService.result_record_key(batch_id, item_key, state)
            if result_key != f"results/{batch_id}/{item_key}/attempts/{state['recovery_sha256']}.json":
                raise Conflict("The current result pointer does not bind the stopped final attempt")
            try:
                store.read_bytes(result_key)
            except Missing:
                pass
            else:
                raise Conflict("A current-attempt result exists; do not label its outcome unknown")
            recorded_at = datetime.now(timezone.utc).isoformat()
            interruption = {
                "audit_key": audit_key, "execution_id": execution_id,
                "execution_resource_id": execution_resource_id,
                "stopped_observed_at": observed, "reconciled_at": recorded_at,
                "remote_completion": "unknown", "automatic_retry": False,
            }
            state.update(state="interrupted", error=ERROR, interruption=interruption)
            states = []
            for item in batch["items"]:
                if item["item_key"] == item_key:
                    states.append("interrupted")
                else:
                    try:
                        value, _ = read_json(store, f"items/{batch_id}/{item['item_key']}.json")
                        states.append(value["state"])
                    except Missing:
                        states.append("queued")
            if "running" in states:
                raise Conflict("Another running item requires separate stopped-execution evidence")
            batch.update(state="interrupted", updated_at=recorded_at, interruption=interruption)
            batch["progress"] = {
                "finished": sum(value not in {"running", "queued", "recovery_ready", "deferred"} for value in states),
                "unresolved": states.count("unresolved"),
                "failed": states.count("failed") + states.count("interrupted"),
                "deferred": states.count("deferred"),
            }
            audit = {
                "request": request, "evidence": evidence, "recorded_at": recorded_at,
                "kind": "interrupted_reconciliation_intent",
                "prior": {
                    name: {"base64": base64.b64encode(current[name][0]).decode(), **expected[name]}
                    for name in ("batch", "item")
                },
                "replacement": {"batch": batch, "item": state},
                "result_key": result_key, "current_attempt_result_absent": True,
            }
            fence()
            write_json(store, audit_key, audit)
        elif audit.get("request") != request or _sha(_bytes(audit.get("evidence"))) != evidence_sha256:
            raise Conflict("This attempt already has a different reconciliation intent")

        for name in ("batch", "item"):
            raw, version = current[name]
            replacement = _bytes(audit["replacement"][name])
            if raw == replacement:
                continue
            if expected[name] != {"version": version, "sha256": _sha(raw)}:
                raise Conflict("Reconciliation write raced with a state change")
            fence()
            version = store.write_bytes(paths[name], replacement, version)
            current[name] = (replacement, version)
        applied = {
            "kind": "interrupted_reconciliation_applied", "audit_key": audit_key,
            "audit_sha256": _sha(_bytes(audit)), "execution_id": execution_id,
            "records": {
                name: {"version": current[name][1], "sha256": _sha(current[name][0])}
                for name in ("batch", "item")
            },
        }
        fence()
        try:
            write_json(store, applied_key, applied)
        except Conflict:
            if read_json(store, applied_key)[0] != applied:
                raise Conflict("Applied reconciliation record changed") from None
        return applied
