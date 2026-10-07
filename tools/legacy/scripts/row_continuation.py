"""One append-only PDF-row rerun in the existing release root.

prepare/check/ready are local-only. Only create-once ready starts the 45/90-minute
clocks. Reconciliation is performed separately by the explicitly approved
operator, never by this helper; its pinned status-only receipt and private gate
must exist before ready. publish submits backend then frontend exactly once.
deploy changes only images on the existing API, worker and frontend. activate
installs the bounded amendment, enables processing, starts once and always closes.
Cost/status probes are advisory; only authorization expiry can request a stop.
"""

import argparse
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
from time import monotonic, sleep
import uuid

if __package__:
    from . import final_continuation as historical
    from . import pilot_continuation as prior
    from . import release
else:
    import final_continuation as historical
    import pilot_continuation as prior
    import release


BASELINE_REVISION = "34fd22e13056ef8fea01cc073a573e9f395a6796"
PREFIX = "row-rerun"
CHILD = "row-rerun-v1"
ROW_RERUN_KEY = "configuration/real-pilot-row-rerun.json"
ROW_RERUN_AUDIT_KEY = "operations/real-pilot-row-rerun.json"
TIME_FIELDS = historical.TIME_FIELDS
AMENDMENT_FIELDS = {
    "schema_version", "approved", "approved_by", "approval_sha256", "batch_sha256",
    "ledger_sha256", "prior_final_rerun_sha256", "prior_final_audit_sha256", "prior_execution_ids",
    "selected_item_keys", "item_sha256", "result_sha256", "cached_documents",
    "additional_input_tokens", "effective_input_ceiling", "max_requests", "max_output_tokens",
    *TIME_FIELDS,
}
WINDOW_POLICY = historical.WINDOW_POLICY
VALIDATION_FILES = historical.VALIDATION_FILES
CI_CHECKS = {"Validate Backend": "success", "Validate Frontend": "success", "Validate Release": "success"}
SUCCESSOR_FILES = {
    "backend/real_pilot.py", "scripts/row_continuation.py", "tests/test_row_continuation.py",
    "tests/test_row_rerun.py", "tests/test_final_rerun.py", "DEPLOYMENT.md", "BATCH.md",
}
HELPER_FILES = (
    "scripts/row_continuation.py", "scripts/release.py", "scripts/pilot_continuation.py",
    "scripts/final_continuation.py", "tests/test_row_continuation.py",
)
CONSUMED = {"executions": 3, "inference": 7, "input_tokens": 165722, "output_tokens": 14336}
POLICY = {
    "backend_builds": 1, "frontend_builds": 1, "build_cpu": 2, "build_timeout_seconds": 900,
    "worker_executions": 1, "worker_timeout_seconds": 600, "item_limit": 2,
    "inference_requests": 4, "output_tokens": 8192, "additional_input_tokens": 57868,
    "input_token_ceiling": 257868, "incremental_operating_microdollars": 5000000,
    "analysis": 0, "search": 0, "web_retrieval": 0, "retrieval": 0, "sharepoint": 0, "ford": 0,
}
RATES = {"build": "0.0001", "worker_cpu": "0.000024", "worker_memory": "0.000003",
         "input": "0.000002", "output": "0.000020"}
GATE_CHECKS = {
    "production_run_batch", "real_guard_reservations", "full_four_prompt_match",
    "expected_token_reservations", "zero_new_analysis", "append_only_history",
    "immutable_results", "attempt_history_export", "owner_isolation",
    "status_only_reconciliation", "preserved_consumption", "unchanged_extraction",
}
DECISION_FIELDS = {
    "schema_version", "approved", "approved_by", "baseline_revision", "source_revision",
    "target", "approval_sha256", "history_sha256", "baseline_sha256", "validation_sha256",
    "gate_file", "gate_sha256", "scope_sha256", "snapshot_file", "snapshot_sha256",
    "reconciliation_file", "reconciliation_sha256", "policy", "window_policy",
}
require = release.require
sha = prior.sha
raw_sha = prior.raw_sha
instant = historical.instant


def now():
    return datetime.now(timezone.utc)


def path(work, name):
    return work / f"{PREFIX}-{name}.json"


def baseline_path(work):
    original = path(work, "baseline")
    selected = path(work, "source-selection")
    if selected.exists():
        previous, current = release.private_json(original), release.private_json(selected)
        require(current["supersedes_baseline_sha256"] == raw_sha(original)
                and all(current.get(key) == value for key, value in previous.items() if key != "source_revision"),
                "Superseding source selection must preserve the original prepared baseline")
        return selected
    return original


def source_work(work):
    original = work / CHILD
    return original / "canonical-ledger" if baseline_path(work) != path(work, "baseline") else original


def binding(decision, work=None):
    value = {"decision_sha256": sha(decision), "target": decision["target"]}
    if work is not None:
        value["readiness_sha256"] = raw_sha(path(work, "readiness"))
    return value


def receipt(work, name, decision):
    value = release.private_json(path(work, name))
    require(all(value.get(key) == expected for key, expected in binding(decision, work).items()),
            "Row-rerun receipt binding changed")
    return value


def evidence(work, decision, label):
    candidate = historical.evidence_path(work, decision[label + "_file"])
    require(raw_sha(candidate) == decision[label + "_sha256"], "Pinned " + label + " evidence changed")
    return release.private_json(candidate)


def history(work):
    retained = {}
    for candidate in sorted(work.rglob("*.json")):
        relative = candidate.relative_to(work)
        if relative.parts[0] == CHILD or {"source", "context"} & set(relative.parts):
            continue
        if len(relative.parts) == 1 and candidate.name.startswith(PREFIX + "-"):
            continue
        require(not candidate.is_symlink() and candidate.stat().st_mode & 0o077 == 0,
                "Historical receipts must remain owner-only regular files")
        retained[str(relative)] = raw_sha(candidate)
    return retained


def original(work, config):
    approval = prior.original(work, config)
    for prefix, attempt in (("continuation", 2), ("final", 3)):
        records = [release.private_json(work / f"{prefix}-{name}.json")
                   for name in ("worker-attempt", "worker-result", "closed")]
        require(records[0]["attempt"] == records[1]["attempt"] == attempt
                and records[1].get("execution_name")
                and len({value["decision_sha256"] for value in records}) == 1
                and all(value["target"] == release.fingerprint(config) for value in records),
                "All three prior executions and their closures must remain consumed")
    child = work / historical.CHILD
    images = release.private_json(child / "images.json")
    previous = {**config, **{kind + "_image": images[kind] for kind in ("backend", "frontend")}}
    release.require_release_images(previous, child)
    release.require_pilot_build_receipts(previous, child)
    return approval, previous


def source_boundary(revision):
    require(isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
            and revision != BASELINE_REVISION, "Exact merged guard/helper successor revision required")
    release.command(["git", "merge-base", "--is-ancestor", BASELINE_REVISION, revision], cwd=release.ROOT)
    changed = set(release.command(
        ["git", "diff", "--name-only", BASELINE_REVISION, revision], cwd=release.ROOT,
    ).decode().splitlines())
    require(changed and changed <= SUCCESSOR_FILES and "backend/real_pilot.py" in changed
            and "scripts/row_continuation.py" in changed,
            "Successor may change only the approved guard/helper/tests/docs; application baseline is immutable")


def prepare(work, config, revision):
    with release.pilot_lock(work):
        approval, _ = original(work, config)
        source_boundary(revision)
        require(not path(work, "baseline").exists() and not (work / CHILD).exists(),
                "Row source preparation is single-use; never reset historical state")
        retained = history(work)
        release.save_once(path(work, "baseline"), {
            "work_root": str(work), "root_pin_sha256": raw_sha(prior.ROOT_PIN),
            "baseline_revision": BASELINE_REVISION, "source_revision": revision,
            "target": release.fingerprint(config), "approval_sha256": sha(approval),
            "history": retained, "history_sha256": sha(retained),
        })
        release.stage(revision, work / CHILD)
        release.build_context(work / CHILD)


def capacity(approval):
    limits = approval["limits"]
    require(limits["input_tokens"] == 200000 and limits["output_tokens"] == 32768
            and limits["inference"] == 16 and limits["executions"] == 2,
            "Original ledger ceilings cannot be replaced")
    require(Decimal(approval["unit_prices_usd"]["input_token"]) == Decimal(RATES["input"])
            and Decimal(approval["unit_prices_usd"]["output_token"]) == Decimal(RATES["output"]),
            "Original model guard prices must remain unchanged")
    return {"input_tokens": 200000 + 57868 - CONSUMED["input_tokens"],
            "output_tokens": limits["output_tokens"] - CONSUMED["output_tokens"],
            "inference_requests": limits["inference"] - CONSUMED["inference"]}


def validate(work, config, decision):
    """No clock, cloud, new approval, ledger replacement or readiness mutation."""
    approval, previous = original(work, config)
    require(set(decision) == DECISION_FIELDS and type(decision["schema_version"]) is int
            and decision["schema_version"] == 1 and decision["approved"] is True
            and decision["approved_by"] == approval["approved_by"]
            and decision["policy"] == POLICY and all(type(value) is int for value in decision["policy"].values())
            and decision["window_policy"] == WINDOW_POLICY, "Exact bounded row-rerun decision required")
    baseline = release.private_json(baseline_path(work))
    require(raw_sha(baseline_path(work)) == decision["baseline_sha256"]
            and baseline["work_root"] == str(work)
            and baseline["root_pin_sha256"] == raw_sha(prior.ROOT_PIN)
            and baseline["history"] == history(work)
            and sha(baseline["history"]) == baseline["history_sha256"],
            "Original root and every historical receipt must remain unchanged")
    expected = {"target": release.fingerprint(config), "approval_sha256": sha(approval),
                "baseline_revision": BASELINE_REVISION, "source_revision": baseline["source_revision"],
                "history_sha256": baseline["history_sha256"]}
    require(all(decision[key] == baseline[key] == value for key, value in expected.items()),
            "Decision source/root/approval/history binding changed")
    source_boundary(decision["source_revision"])
    child = source_work(work)
    require(not child.is_symlink() and child.resolve() == child, "Context must remain in the original root")
    for kind in ("backend", "frontend"):
        release.require_component_source_receipts(child, kind)
    require(release.verify_source(child)["revision"] == decision["source_revision"]
            and sha({name: raw_sha(child / name) for name in VALIDATION_FILES}) == decision["validation_sha256"],
            "Exact-source build/CI evidence changed")
    require(all((release.ROOT / name).read_bytes() == (child / "source" / name).read_bytes()
                for name in HELPER_FILES), "Running helper/publisher bytes differ from validated source")
    ci = release.private_json(child / "ci.json")
    require(ci.get("revision") == decision["source_revision"] and ci.get("merged") is True
            and ci.get("base") == "main" and ci.get("checks") == CI_CHECKS,
            "All required CI must pass on the exact merged successor")
    snapshot = evidence(work, decision, "snapshot")
    reconciled = evidence(work, decision, "reconciliation")
    gate = evidence(work, decision, "gate")
    require(snapshot.get("verified") is True and snapshot.get("temporary_processing_closed") is True
            and snapshot.get("active_executions") == 0 and snapshot.get("consumption") == CONSUMED,
            "Pinned post-closure snapshot must preserve all three executions and reservations")
    require(reconciled.get("verified") is True and reconciled.get("status_only") is True
            and reconciled.get("snapshot_sha256") == decision["snapshot_sha256"]
            and reconciled.get("consumption") == CONSUMED,
            "Pinned hosted status-only reconciliation must not reset consumption")
    require(gate.get("schema_version") == 1 and gate.get("passed") is True
            and gate.get("no_live_operations") is True
            and all(gate.get(key) == decision[key] for key in (
                "target", "approval_sha256", "source_revision", "history_sha256", "scope_sha256",
                "snapshot_sha256", "reconciliation_sha256"))
            and GATE_CHECKS <= set(gate.get("checks", {}))
            and all(value is True for value in gate["checks"].values()),
            "Exact merged-source private append-only gate must pass")
    scope = gate["amendment_scope"]
    previous_amendment = release.private_json(work / "final-configure-attempt.json")["recovery"]
    require(isinstance(scope, dict) and set(scope) == AMENDMENT_FIELDS - TIME_FIELDS
            and sha(scope) == decision["scope_sha256"]
            and scope["schema_version"] == 1 and scope["approved"] is True
            and scope["approved_by"] == approval["approved_by"]
            and scope["approval_sha256"] == sha(approval) and scope["batch_sha256"] == approval["batch_sha256"]
            and len(scope["prior_execution_ids"]) == len(set(scope["prior_execution_ids"])) == 3
            and scope["selected_item_keys"] == ["row-2", "row-3"]
            and scope["ledger_sha256"] == snapshot["ledger_sha256"] == reconciled["ledger_sha256"]
            and scope["prior_final_rerun_sha256"] == sha(previous_amendment)
            and scope["prior_final_audit_sha256"] == snapshot["prior_final_audit_sha256"] == reconciled["prior_final_audit_sha256"]
            and scope["additional_input_tokens"] == 57868 and scope["effective_input_ceiling"] == 257868
            and scope["max_requests"] == 4 and scope["max_output_tokens"] == 8192,
            "Gate must bind the same ledger, prior final audit and selected amendment scope")
    pins = gate["remote_object_sha256"]
    required = {"configuration/real-pilot-approval.json", "budgets/real-pilot.json",
                "operations/real-pilot-final-rerun.json", "batches/" + approval["batch_id"] + ".json"}
    required |= {"items/" + approval["batch_id"] + "/" + key + ".json" for key in scope["selected_item_keys"]}
    required |= set(scope["cached_documents"])
    required |= {key for key, value in scope["result_sha256"].items() if value is not None}
    require(required <= set(pins) and pins == reconciled["object_sha256"]
            and any(key.startswith("operations/interrupted-reconciliation/") and key.endswith(".applied.json")
                    for key in pins)
            and all(isinstance(key, str) and ".." not in key.split("/") and not key.startswith("/")
                    and isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
                    for key, value in pins.items()), "Reconciled snapshot must pin every protected hosted object")
    require(all(pins["items/" + approval["batch_id"] + "/" + key + ".json"] == value
                for key, value in scope["item_sha256"].items())
            and all(pins[key] == value for key, value in scope["cached_documents"].items())
            and all(pins[key] == value for key, value in scope["result_sha256"].items() if value is not None),
            "Hosted object pins and amendment history disagree")
    headroom = capacity(approval)
    require(headroom["input_tokens"] == gate["full_fallthrough_input_tokens"] == 92146
            and gate["planned_input_tokens"] == 89596 and gate["full_output_tokens"] == 8192,
            "Four complete full-fallthrough reservations must fit the precise input supplement")
    maximum = 2 * 2 * 900 * Decimal(RATES["build"]) + 600 * (
        Decimal(RATES["worker_cpu"]) + 2 * Decimal(RATES["worker_memory"]))
    maximum += 92146 * Decimal(RATES["input"]) + 8192 * Decimal(RATES["output"])
    require(maximum == Decimal("0.726132")
            and gate["maximum_direct_microdollars"] == int(maximum * 1000000)
            and gate["verified_rates"] == RATES and gate["incidentals_disclosure_only"] is True
            and gate["historical_forecasts_stacked"] is False,
            "Separate $5 allowance requires verified direct envelope, not stacked historical forecasts")
    for candidate in work.glob(PREFIX + "-*-attempt.json"):
        require(release.private_json(candidate).get("decision_sha256") == sha(decision),
                "Consumed row authority cannot be changed or retried")
    return approval, previous


def ready(work, config, decision):
    with release.pilot_lock(work):
        require(not path(work, "readiness").exists() and not path(work, "readiness-pin").exists()
                and not list(work.glob(PREFIX + "-*-attempt.json")), "Readiness is single-use; never renew")
        approval, _ = validate(work, config, decision)
        head = release.command(["git", "rev-parse", "HEAD"], cwd=release.ROOT).decode().strip()
        require(head == decision["source_revision"], "Ready must use the exact merged CI-tested successor")
        for name in HELPER_FILES:
            local = release.ROOT / name
            require(local.read_bytes() == release.command(["git", "show", head + ":" + name], cwd=release.ROOT)
                    == (source_work(work) / "source" / name).read_bytes(),
                    "Helper/publisher/test bytes must match committed and staged CI source")
        anchor = now()
        previous_amendment = release.private_json(work / "final-configure-attempt.json")["recovery"]
        require(anchor >= instant(previous_amendment["operating_expires_at"]),
                "Row readiness cannot begin before the prior final operating window expires")
        value = {"schema_version": 1, **binding(decision), "source_revision": decision["source_revision"],
                 "window_policy": WINDOW_POLICY, "remaining_capacity": capacity(approval),
                 "readiness_at": anchor.isoformat(),
                 "publication_expires_at": (anchor + timedelta(seconds=2700)).isoformat(),
                 "overall_expires_at": (anchor + timedelta(seconds=5400)).isoformat()}
        release.save_once(path(work, "readiness"), value)
        release.save_once(path(work, "readiness-pin"), {**binding(decision), "readiness_sha256": raw_sha(path(work, "readiness"))})
        return value


def readiness(work, decision):
    value = release.private_json(path(work, "readiness"))
    pin = release.private_json(path(work, "readiness-pin"))
    require(not path(work, "readiness").is_symlink() and not path(work, "readiness-pin").is_symlink()
            and pin == {**binding(decision), "readiness_sha256": raw_sha(path(work, "readiness"))}
            and all(value.get(key) == expected for key, expected in binding(decision).items())
            and value["source_revision"] == decision["source_revision"]
            and value["window_policy"] == WINDOW_POLICY, "Immutable row readiness binding changed")
    require(raw_sha(baseline_path(work)) == decision["baseline_sha256"]
            and all(raw_sha(historical.evidence_path(work, decision[name + "_file"]))
                    == decision[name + "_sha256"] for name in ("gate", "snapshot", "reconciliation")),
            "Readiness prerequisite evidence changed")
    anchor = instant(value["readiness_at"])
    previous_amendment = release.private_json(work / "final-configure-attempt.json")["recovery"]
    require(instant(value["publication_expires_at"]) == anchor + timedelta(seconds=2700)
            and instant(value["overall_expires_at"]) == anchor + timedelta(seconds=5400)
            and anchor >= instant(previous_amendment["operating_expires_at"]),
            "Readiness clocks cannot be extended")
    return value


def live_window(work, decision, phase, minimum=0):
    value = readiness(work, decision)
    current = now()
    require(current >= instant(value["readiness_at"]), "Readiness has not started")
    if phase == "cleanup":
        return value
    require(phase in {"publication", "overall", "processing"}, "Unknown row phase")
    end = instant(value["publication_expires_at" if phase == "publication" else "overall_expires_at"])
    if phase == "processing":
        end = min(end, instant(receipt(work, "deployed", decision)["processing_expires_at"]))
    require(current < end and (end - current).total_seconds() >= minimum, "Insufficient row " + phase + " window")
    return value


@contextmanager
def live_operations(work, decision, phase, minimum=0):
    live_window(work, decision, phase, minimum)
    pinned = raw_sha(path(work, "readiness"))
    originals = {name: getattr(release, name) for name in (
        "azure", "run_pilot_console", "console_code", "upload_publication_context",
    )}

    def checked(name, operation):
        def call(*args, **kwargs):
            require(raw_sha(path(work, "readiness")) == pinned, "Row readiness changed during action")
            needed = minimum
            if phase == "publication" and (
                name == "upload_publication_context" or args[:3] == ("rest", "--method", "POST")
            ):
                needed = max(900, needed)
            live_window(work, decision, phase, needed)
            return operation(*args, **kwargs)
        return call

    try:
        for name, operation in originals.items():
            setattr(release, name, checked(name, operation))
        yield
    finally:
        for name, operation in originals.items():
            setattr(release, name, operation)


def console(work, config, decision, approval, packet, body, *, phase, minimum=0, timeout=30):
    code, marker = remote_packet(packet, body)
    with live_operations(work, decision, phase, minimum):
        backend = release.app(config, config["backend"], timeout=timeout)
        release.require_pilot_identity(backend, approval["identities"]["api_principal_id"])
        return release.console_code([
            "az", "containerapp", "exec", "--subscription", config["subscription"],
            "-g", config["group"], "-n", config["backend"],
            "--revision", backend["properties"]["latestReadyRevisionName"],
            "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors",
        ], code, marker, timeout=timeout)


def remote_packet(packet, body):
    encoded = release.bounded_metadata(packet)
    marker = "DOCINTEL_ROW_RERUN_OK:" + sha(packet)
    code = f'''import base64, hashlib, json, os, zlib
from backend.batch_store import Missing, configured_store, read_json, write_json
from backend.real_pilot import BUDGET_KEY, _sha256
packet = json.loads(zlib.decompress(base64.b64decode({encoded!r}, validate=True)))
assert _sha256(packet) == {sha(packet)!r}
store = configured_store()
''' + body + f"\nprint({marker!r})\n"
    require(len(base64.b64encode(code.encode())) + 80 <= 16384, "Row metadata exceeds console bound")
    return code, marker


def snapshot_remote(work, config, decision, approval, *, phase="publication"):
    gate = evidence(work, decision, "gate")
    missing = [key for key, value in gate["amendment_scope"]["result_sha256"].items() if value is None]
    packet = {"objects": gate["remote_object_sha256"], "absent": [ROW_RERUN_KEY, ROW_RERUN_AUDIT_KEY, *missing],
              "approval_sha256": sha(approval), "ledger_sha256": gate["amendment_scope"]["ledger_sha256"],
              "consumed": CONSUMED, "prior_execution_ids": gate["amendment_scope"]["prior_execution_ids"]}
    body = '''for key, expected in packet["objects"].items():
    raw, _ = store.read_bytes(key)
    assert hashlib.sha256(raw).hexdigest() == expected
for key in packet["absent"]:
    try:
        store.read_bytes(key)
    except Missing:
        pass
    else:
        raise ValueError("Row rerun already configured or consumed")
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
ledger, _ = read_json(store, BUDGET_KEY)
assert _sha256(approval) == ledger["approval_sha256"] == packet["approval_sha256"]
assert _sha256(ledger) == packet["ledger_sha256"] and not ledger.get("invalidated")
assert set(ledger["executions"]) == set(packet["prior_execution_ids"])
assert len(ledger["executions"]) == packet["consumed"]["executions"]
assert ledger["attempted"]["inference"] == packet["consumed"]["inference"]
assert all(ledger["reserved"][name] == packet["consumed"][name] for name in ("input_tokens", "output_tokens"))
'''
    console(work, config, decision, approval, packet, body, phase=phase, minimum=900 if phase == "publication" else 600)


def preflight(work, config, decision):
    approval, previous = validate(work, config, decision)
    with live_operations(work, decision, "publication", 900):
        release.verify_target(previous)
        release.identity_contract(previous)
        release.require_real_pilot_off(prior.ready(previous, "backend"))
        prior.ready(previous, "frontend")
        prior.require_worker(previous, approval)
        snapshot_remote(work, previous, decision, approval)
        return prior.require_model_capacity(previous, approval)


def warning(work, decision, reason):
    messages = {
        "usage_unavailable": "Cost/usage probe unavailable; missing usage remains unknown.",
        "usage_unknown": "Cost/usage observation is incomplete; reservations are not measured charges.",
        "overbudget": "Observed direct cost exceeds the unchanged incremental $5 allowance.",
        "status_unavailable": "Worker status is unknown; observing the same execution until authorization expiry.",
        "limit_discrepancy": "Telemetry reports a server-limit discrepancy.",
    }
    reason = reason if reason in messages else "usage_unavailable"
    message = messages[reason] + " Warning only: no worker stop or early closure; server limits remain enforced."
    suffix = ""
    try:
        release.save_once(path(work, "warning-" + uuid.uuid4().hex), {
            **binding(decision, work), "observed_at": now().isoformat(), "reason": reason,
            "message": message, "observation_only": True, "worker_stop_requested": False,
        })
    except Exception:
        suffix = " Durable warning unavailable."
    try:
        print("WARNING: " + message + suffix, file=sys.stderr)
    except Exception:
        pass


@contextmanager
def monitoring(work, decision, reason="usage_unavailable"):
    try:
        yield
    except Exception:
        warning(work, decision, reason)


def record_cost(work, decision, updates):
    files = sorted(work.glob(PREFIX + "-cost-[0-9][0-9][0-9][0-9][0-9][0-9].json"))
    prior_value = receipt(work, files[-1].stem.removeprefix(PREFIX + "-"), decision) if files else {"components": {}}
    components = copy.deepcopy(prior_value["components"])
    for name, value in updates.items():
        require(name in {"backend", "frontend", "worker", "model"}, "Unexpected cost component")
        amount = value["microdollars"]
        require(amount is None or type(amount) is int and amount >= 0, "Invalid cost observation")
        if amount is None and components.get(name, {}).get("microdollars") is not None:
            value = {**value, "microdollars": components[name]["microdollars"],
                     "last_known_only": True, "complete": False}
        components[name] = value
    known = sum(value["microdollars"] for value in components.values() if value["microdollars"] is not None)
    unknown = [name for name, value in components.items() if not value["complete"]]
    sequence = int(files[-1].stem.rsplit("-", 1)[1]) + 1 if files else 1
    release.save_once(path(work, f"cost-{sequence:06d}"), {
        **binding(decision, work), "observed_at": now().isoformat(), "components": components,
        "measured_known_microdollars": known, "unknown_components": unknown,
        "reservations_charged": False, "historical_forecasts_stacked": False,
    })
    if unknown:
        warning(work, decision, "usage_unknown")
    if known > 5000000:
        warning(work, decision, "overbudget")


def build_cost(work, decision, kind, properties):
    with monitoring(work, decision):
        seconds = historical.runtime_seconds(properties, "finishTime", terminal=properties.get("status") == "Succeeded")
        cpu = properties.get("agentConfiguration", {}).get("cpu")
        amount = historical.microdollars(seconds * cpu * Decimal(RATES["build"])) \
            if seconds is not None and type(cpu) is int and cpu > 0 else None
        record_cost(work, decision, {kind: {"microdollars": amount, "complete": amount is not None}})


def publish(work, config, decision):
    with release.pilot_lock(work):
        require(not any(path(work, kind + "-attempt").exists() for kind in ("backend", "frontend")),
                "Row publications already attempted; no retry")
        metadata = preflight(work, config, decision)
        for kind in ("backend", "frontend"):
            with live_operations(work, decision, "publication"):
                value = live_window(work, decision, "publication", 900)
                attempt = path(work, kind + "-attempt")
                release.save_once(attempt, {
                    **binding(decision, work), "attempt_id": str(uuid.uuid4()), "kind": kind,
                    "cpu": 2, "timeout_seconds": 900, "model_capacity": metadata,
                    "status": "attempted_outcome_unknown",
                })
                result = release.execute_publication(
                    config, source_work(work), kind, decision["source_revision"], attempt,
                    expires=instant(value["publication_expires_at"]), validate_upload_window=True,
                )
                build_cost(work, decision, kind, result)
                release.save_once(path(work, kind + "-published"), {
                    **binding(decision, work), **result,
                    "image": config["registry"] + f".azurecr.io/docintel/{kind}@" + result["digest"],
                })
        images = {"revision": decision["source_revision"]}
        for kind in ("backend", "frontend"):
            result = receipt(work, kind + "-published", decision)
            images[kind], images[kind + "_run_id"] = result["image"], result["runId"]
        release.save_once(source_work(work) / "images.json", images)
        release.save_once(path(work, "published"), {**binding(decision, work),
                                                   "images_sha256": raw_sha(source_work(work) / "images.json")})


def published_config(work, config, decision):
    published = receipt(work, "published", decision)
    candidate = source_work(work) / "images.json"
    require(raw_sha(candidate) == published["images_sha256"], "Row image receipt changed")
    images = release.private_json(candidate)
    current = {**config, **{kind + "_image": images[kind] for kind in ("backend", "frontend")}}
    release.require_release_images(current, source_work(work))
    release.require_pilot_build_receipts(current, source_work(work))
    return current


def patch(work, config, decision, label, resource, containers, *, job=False, kind="backend"):
    phase = "cleanup" if label == "close" else "processing" if label == "enable" else "overall"
    with live_operations(work, decision, phase, 0 if phase == "cleanup" else 600):
        current, active = release.active_executions(config) if job else (release.app(config, config[kind]), [])
        require(not active and current == resource, "Resource drift before mutation; never overwrite")
        payload = {"properties": {"template": {"containers": release.writable_containers(containers)}}}
        candidate = path(work, label + "-patch")
        release.save_once(candidate, payload)
        args = ["rest", "--method", "PATCH", "--url",
                "https://management.azure.com" + resource["id"] + "?api-version=2024-03-01",
                "--body", "@" + str(candidate)]
        if resource.get("etag"):
            args.extend(("--headers", "If-Match=" + resource["etag"]))
        release.azure(*args)
        expected = prior.shape(resource)
        expected["template"]["containers"] = release.writable_containers(containers)
        end = monotonic() + 180
        while True:
            actual, active = release.active_executions(config, timeout=30) if job else (
                release.app(config, config[kind], timeout=30), [])
            require(not active, "Worker became active during deployment")
            observed = prior.shape(actual)
            if observed == expected:
                return
            require(observed == prior.shape(resource) and monotonic() < end,
                    "Mutation not confirmed; no repeat request")
            sleep(5)


def deploy(work, config, decision):
    with release.pilot_lock(work), live_operations(work, decision, "overall", 600):
        approval, previous = validate(work, config, decision)
        current = published_config(work, config, decision)
        require(not path(work, "deploy-attempt").exists(), "Row deployment already attempted")
        release.verify_target(previous)
        release.identity_contract(previous)
        backend, frontend = (prior.ready(previous, kind) for kind in ("backend", "frontend"))
        release.require_real_pilot_off(backend)
        job = prior.require_worker(previous, approval)
        snapshot_remote(work, previous, decision, approval, phase="overall")
        release.save_once(path(work, "deploy-attempt"), {
            **binding(decision, work), "backend": prior.shape(backend),
            "frontend": prior.shape(frontend), "worker": prior.shape(job),
        })
        for kind, resource, is_job in (("backend", backend, False), ("worker", job, True),
                                      ("frontend", frontend, False)):
            containers = release.safe_containers(resource)
            containers[0]["image"] = current["frontend_image" if kind == "frontend" else "backend_image"]
            patch(work, previous, decision, "deploy-" + kind, resource, containers,
                  job=is_job, kind="frontend" if kind == "frontend" else "backend")
        for kind in ("backend", "frontend"):
            prior.ready(current, kind)
        started = now()
        expires = min(started + timedelta(seconds=1200), instant(readiness(work, decision)["overall_expires_at"]))
        require((expires - started).total_seconds() >= 600, "Both revisions became ready too late for a full worker")
        release.save_once(path(work, "deployed"), {
            **binding(decision, work), "both_ready_at": started.isoformat(), "processing_expires_at": expires.isoformat(),
        })


def active_target(work, config, decision, approval, *, processing=True):
    with live_operations(work, decision, "processing" if processing else "cleanup", 600 if processing else 0):
        receipt(work, "deployed", decision)
        baseline = receipt(work, "deploy-attempt", decision)
        current = published_config(work, config, decision)
        release.verify_target(current)
        release.identity_contract(current)
        backend, frontend = (prior.ready(current, kind) for kind in ("backend", "frontend"))
        job = prior.require_worker(current, approval) if processing else release.active_executions(current)[0]
        for kind, resource in (("backend", backend), ("frontend", frontend), ("worker", job)):
            expected, actual = copy.deepcopy(baseline[kind]), prior.shape(resource)
            expected["template"]["containers"][0]["image"] = current[
                "frontend_image" if kind == "frontend" else "backend_image"]
            if kind == "backend":
                for value in (expected, actual):
                    value["template"]["containers"][0]["env"] = [
                        entry for entry in value["template"]["containers"][0].get("env", [])
                        if entry["name"] not in release.PILOT_API_SETTINGS]
            require(actual == expected, "Row runtime/authentication/identity drift")
        return current, backend, job


def amendment(work, decision):
    gate = evidence(work, decision, "gate")
    value, deployed = readiness(work, decision), receipt(work, "deployed", decision)
    return {**gate["amendment_scope"], "readiness_at": value["readiness_at"],
            "operating_expires_at": value["overall_expires_at"],
            "not_before": deployed["both_ready_at"], "expires_at": deployed["processing_expires_at"]}


def remote_code(approval, candidate, *, write=False, configured=False):
    packet = {"approval_sha256": sha(approval), "batch_id": approval["batch_id"],
              "amendment": candidate, "configured": configured}
    body = f'''from backend.real_pilot import validate_row_rerun
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
assert _sha256(approval) == packet["approval_sha256"]
batch_key = "batches/" + packet["batch_id"] + ".json"
batch, _ = read_json(store, batch_key)
ledger, _ = read_json(store, BUDGET_KEY)
os.environ["DOCINTEL_REAL_PILOT_OPERATOR_IDS"] = approval["approved_by"]
class ReadOnly:
    def read_bytes(self, name, **kwargs):
        return store.read_bytes(name, **kwargs)
candidate = packet["amendment"]
assert validate_row_rerun(candidate, approval, batch, ledger, ReadOnly()) == candidate
try:
    existing, _ = read_json(store, {ROW_RERUN_KEY!r})
except Missing:
    existing = None
assert existing is None or existing == candidate
assert not packet["configured"] or existing == candidate and batch["state"] == "queued"
'''
    if write:
        body += f'''assert existing is None
with store.lease(batch["id"]) as batch_fence, store.lease(BUDGET_KEY) as budget_fence:
    fresh, version = read_json(store, batch_key)
    current_ledger, _ = read_json(store, BUDGET_KEY)
    assert fresh == batch and current_ledger == ledger
    assert fresh["state"] in ("interrupted", "completed", "deferred", "queued")
    assert validate_row_rerun(candidate, approval, fresh, current_ledger, ReadOnly()) == candidate
    batch_fence()
    budget_fence()
    write_json(store, {ROW_RERUN_KEY!r}, candidate)
    assert read_json(store, {ROW_RERUN_KEY!r})[0] == candidate
    if fresh["state"] != "queued":
        fresh["state"] = "queued"
        batch_fence()
        budget_fence()
        write_json(store, batch_key, fresh, version)
    assert read_json(store, batch_key)[0] == fresh
'''
    return remote_packet(packet, body)


def verify_remote(work, config, decision, approval, candidate, *, write=False, configured=False):
    require(candidate == amendment(work, decision), "Amendment must derive from the pinned gate and readiness")
    code, marker = remote_code(approval, candidate, write=write, configured=configured)
    with live_operations(work, decision, "processing", 600):
        backend = release.app(config, config["backend"])
        release.require_pilot_identity(backend, approval["identities"]["api_principal_id"])
        release.run_pilot_console(config, backend, code, marker)


def configure(work, config, decision):
    with release.pilot_lock(work), live_operations(work, decision, "processing", 600):
        approval, _ = validate(work, config, decision)
        require(not path(work, "closed").exists() and not path(work, "configure-attempt").exists(),
                "Row configuration closed or consumed; no retry")
        current, backend, _ = active_target(work, config, decision, approval)
        release.require_real_pilot_off(backend)
        snapshot_remote(work, current, decision, approval, phase="processing")
        candidate = amendment(work, decision)
        verify_remote(work, current, decision, approval, candidate)
        release.save_once(path(work, "configure-attempt"), {
            **binding(decision, work), "amendment": candidate, "amendment_sha256": sha(candidate),
        })
        verify_remote(work, current, decision, approval, candidate, write=True)
        release.save_once(path(work, "configured"), {**binding(decision, work), "amendment_sha256": sha(candidate)})


def configured_amendment(work, decision):
    attempted = receipt(work, "configure-attempt", decision)
    candidate = attempted["amendment"]
    require(candidate == amendment(work, decision)
            and sha(candidate) == attempted["amendment_sha256"] == receipt(work, "configured", decision)["amendment_sha256"],
            "Configured row amendment changed")
    return candidate


def enable(work, config, decision):
    with release.pilot_lock(work), live_operations(work, decision, "processing", 600):
        approval, _ = validate(work, config, decision)
        require(not path(work, "closed").exists() and not path(work, "enablement").exists(),
                "Row enablement closed or consumed")
        candidate = configured_amendment(work, decision)
        current, backend, _ = active_target(work, config, decision, approval)
        release.require_real_pilot_off(backend)
        verify_remote(work, current, decision, approval, candidate, configured=True)
        containers = release.safe_containers(backend)
        entries = release.environment_entries(containers[0])
        require(entries.get("DOCINTEL_PILOT_UPLOAD_ENABLED") in (
            None, {"name": "DOCINTEL_PILOT_UPLOAD_ENABLED", "value": "false"}), "Source upload remains disabled")
        intended = {name: {"name": name, "value": value} for name, value in release.pilot_values(approval).items()}
        release.save_once(path(work, "enablement"), {
            **binding(decision, work), "baseline": {name: entries.get(name) for name in release.PILOT_API_SETTINGS},
            "intended": intended,
        })
        release.merge_environment(containers[0], release.pilot_values(approval))
        patch(work, current, decision, "enable", backend, containers)
        prior.ready(current, "backend")
        release.save_once(path(work, "enabled"), binding(decision, work))


def start(work, config, decision):
    with release.pilot_lock(work), live_operations(work, decision, "processing", 600):
        approval, _ = validate(work, config, decision)
        require(not path(work, "closed").exists() and not path(work, "worker-attempt").exists(),
                "Fourth worker closed or consumed; never start again")
        candidate = configured_amendment(work, decision)
        receipt(work, "enabled", decision)
        current, backend, job = active_target(work, config, decision, approval)
        entries = release.environment_entries(release.safe_containers(backend)[0])
        require(all(entries.get(key) == value for key, value in receipt(work, "enablement", decision)["intended"].items()),
                "Enabled row authorization drift")
        verify_remote(work, current, decision, approval, candidate, configured=True)
        metadata = prior.require_model_capacity(current, approval)
        template = copy.deepcopy(job["properties"]["template"])
        container = template["containers"][0]
        container["command"] = ["/app/.venv/bin/python"]
        container["args"] = ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", approval["batch_id"],
                             "--concurrency", "1", "--max-batches", "1", "--item-limit", "2"]
        container["env"] = [entry for entry in container.get("env", [])
                            if entry["name"] not in release.PILOT_EXTERNAL_ENVIRONMENT_KEYS | {"WEBIQ_API_KEY"}]
        release.merge_environment(container, {**approval["environment"], **release.pilot_values(approval),
                                              "DOCINTEL_REAL_PILOT_EXECUTION_SCOPE": "internal_only"})
        template = release.writable_execution_template(template)
        fresh, running = release.active_executions(current)
        require(not running and fresh == job, "Worker changed before fourth start")
        template_path = path(work, "worker-template")
        release.save_once(template_path, template)
        release.save_once(path(work, "worker-attempt"), {
            **binding(decision, work), "attempt": 4, "amendment_sha256": sha(candidate),
            "execution_sha256": sha(template), "prior_worker_attempt_sha256": raw_sha(work / "final-worker-attempt.json"),
            "attempted_at": now().isoformat(), "state": "start_attempted_completion_unknown", "model_capacity": metadata,
        })
        live_window(work, decision, "processing", 600)
        result = release.azure("containerapp", "job", "start", "--subscription", current["subscription"],
                               "-g", current["group"], "-n", current["job"], "--yaml", str(template_path),
                               resource_snapshot=fresh, before_send=lambda: live_window(work, decision, "processing", 600))
        require(isinstance(result, dict) and isinstance(result.get("name"), str) and result["name"],
                "Fourth start outcome unknown; inspect same execution, never retry")
        release.save_once(path(work, "worker-result"), {
            **binding(decision, work), "attempt": 4, "execution_name": result["name"],
        })


def usage(work, config, decision, approval, candidate, timeout):
    packet = {"approval_sha256": sha(approval), "amendment_sha256": sha(candidate),
              "prior_execution_ids": candidate["prior_execution_ids"]}
    body = f'''ledger, _ = read_json(store, BUDGET_KEY)
candidate, _ = read_json(store, {ROW_RERUN_KEY!r})
assert ledger["approval_sha256"] == packet["approval_sha256"]
assert _sha256(candidate) == packet["amendment_sha256"]
entry = ledger.get("row_rerun")
if entry is None:
    result = {{"execution_id": None, "reservations": {{}}}}
else:
    execution = entry["execution_id"]
    assert entry["sha256"] == packet["amendment_sha256"]
    assert set(ledger["executions"]) == set(packet["prior_execution_ids"]) | {{execution}}
    assert execution not in packet["prior_execution_ids"]
    reservations = {{key: value for key, value in ledger["reservations"].items()
                    if value["execution_id"] == execution}}
    result = {{"execution_id": execution, "reservations": {{
        key: {{"operation": value["operation"], "actual_usage": value["actual_usage"]}}
        for key, value in reservations.items()}}}}
print("DOCINTEL_ROW_USAGE:" + base64.b64encode(json.dumps(result, sort_keys=True).encode()).decode())
'''
    output = console(work, config, decision, approval, packet, body, phase="processing", timeout=timeout)
    lines = output.decode().splitlines()
    matches = [line.removeprefix("DOCINTEL_ROW_USAGE:") for line in lines if line.startswith("DOCINTEL_ROW_USAGE:")]
    require(len(matches) == 1, "Usage probe did not confirm a unique result")
    return json.loads(base64.b64decode(matches[0], validate=True))


def worker_cost(work, config, decision, approval, candidate, properties, timeout):
    try:
        terminal = properties["status"] in release.TERMINAL
        seconds = historical.runtime_seconds(properties, "endTime", terminal=terminal)
        worker = historical.microdollars(seconds * (
            Decimal(RATES["worker_cpu"]) + 2 * Decimal(RATES["worker_memory"]))) if seconds is not None else None
        record_cost(work, decision, {"worker": {"microdollars": worker, "complete": terminal and worker is not None}})
        report = usage(work, config, decision, approval, candidate, timeout)
        tokens, missing = {"input_tokens": 0, "output_tokens": 0, "analysis_pages": 0}, False
        reservations = report["reservations"]
        require(isinstance(reservations, dict), "Malformed usage report")
        violation = len(reservations) > 4
        for reservation in reservations.values():
            actual = reservation["actual_usage"]
            violation |= reservation["operation"] != "inference"
            if actual is None:
                missing = True
                continue
            require(set(actual) == set(tokens) and all(type(value) is int and value >= 0 for value in actual.values()),
                    "Malformed actual model usage")
            for name in tokens:
                tokens[name] += actual[name]
            violation |= actual["output_tokens"] > 2048 or actual["analysis_pages"] != 0
        amount = historical.microdollars(tokens["input_tokens"] * Decimal(RATES["input"])
                                        + tokens["output_tokens"] * Decimal(RATES["output"]))
        record_cost(work, decision, {"model": {
            "microdollars": None if missing or not report["execution_id"] else amount,
            "complete": terminal and not missing and bool(report["execution_id"]), "actual_usage": tokens,
        }})
        if violation or tokens["input_tokens"] > 92146 or tokens["output_tokens"] > 8192:
            warning(work, decision, "limit_discrepancy")
    except Exception:
        warning(work, decision, "usage_unavailable")
        with monitoring(work, decision):
            record_cost(work, decision, {"model": {"microdollars": None, "complete": False}})


def stop_for_expiry(work, config, decision, execution):
    candidate_amendment = configured_amendment(work, decision)
    deadline = min(instant(candidate_amendment["expires_at"]),
                   instant(readiness(work, decision)["overall_expires_at"]))
    require(now() >= deadline, "Only independent authorization expiry permits a helper stop")
    candidate = path(work, "worker-stop-attempt")
    if candidate.exists():
        require(receipt(work, "worker-stop-attempt", decision)["execution_name"] == execution, "Stop target changed")
        return
    release.save_once(candidate, {**binding(decision, work), "execution_name": execution,
                                  "reason": "authorization_window_expired"})
    with live_operations(work, decision, "cleanup"):
        release.azure("containerapp", "job", "stop", "--subscription", config["subscription"],
                      "-g", config["group"], "-n", config["job"], "--job-execution-name", execution, timeout=30)


def observe(work, config, decision):
    approval, _ = validate(work, config, decision)
    value = readiness(work, decision)
    candidate = configured_amendment(work, decision)
    current = published_config(work, config, decision)
    execution = receipt(work, "worker-result", decision)["execution_name"]
    receipt(work, "worker-attempt", decision)
    deadline = min(instant(candidate["expires_at"]), instant(value["overall_expires_at"]))
    terminal = path(work, "worker-terminal")
    if terminal.exists():
        value = receipt(work, "worker-terminal", decision)
        require(value["execution_name"] == execution and value["status"] == "Succeeded",
                "Fourth execution ended unsuccessfully; never retry")
        return
    while now() < deadline:
        properties = None
        with live_operations(work, decision, "processing"):
            with monitoring(work, decision, "status_unavailable"):
                results = release.azure(
                    "containerapp", "job", "execution", "list", "--subscription", current["subscription"],
                    "-g", current["group"], "-n", current["job"], timeout=min(30, (deadline - now()).total_seconds()),
                )
                matches = [value for value in results if value["name"] == execution]
                require(len(matches) == 1 and isinstance(matches[0]["properties"]["status"], str),
                        "Reserved execution not uniquely visible")
                properties = matches[0]["properties"]
        if properties is not None:
            remaining = (deadline - now()).total_seconds()
            if remaining > 0:
                worker_cost(work, current, decision, approval, candidate, properties, min(30, remaining))
            if properties["status"] in release.TERMINAL:
                release.save_once(terminal, {**binding(decision, work), "execution_name": execution, "attempt": 4,
                                             **{key: properties.get(key) for key in ("status", "startTime", "endTime")}})
                require(properties["status"] == "Succeeded", "Fourth worker terminated unsuccessfully; never retry")
                return
        sleep(min(15, max(0, (deadline - now()).total_seconds())))
    stop_for_expiry(work, current, decision, execution)
    raise ValueError("Row authorization expired; stop requested only for the reserved execution")


def close(work, config, decision):
    with release.pilot_lock(work), live_operations(work, decision, "cleanup"):
        approval, _ = validate(work, config, decision)
        current, backend, _ = active_target(work, config, decision, approval, processing=False)
        if not path(work, "enablement").exists():
            release.require_real_pilot_off(backend)
        else:
            baseline = receipt(work, "enablement", decision)
            containers = release.safe_containers(backend)
            entries = release.environment_entries(containers[0])
            actual = {name: entries.get(name) for name in release.PILOT_API_SETTINGS}
            require(actual in (baseline["baseline"], baseline["intended"]), "Unsafe managed-setting closure drift")
            if actual != baseline["baseline"]:
                release.save_once(path(work, "close-attempt"), binding(decision, work))
                for name, entry in baseline["baseline"].items():
                    if entry is None:
                        entries.pop(name, None)
                    else:
                        entries[name] = entry
                containers[0]["env"] = list(entries.values())
                patch(work, current, decision, "close", backend, containers)
            prior.ready(current, "backend")
        if not path(work, "closed").exists():
            release.save_once(path(work, "closed"), binding(decision, work))


def activate(work, config, decision):
    with release.pilot_lock(work):
        validate(work, config, decision)
        live_window(work, decision, "processing", 600)
        receipt(work, "deployed", decision)
        require(not path(work, "closed").exists(), "Row activation already closed")
        release.save_once(path(work, "activation-attempt"), binding(decision, work))
    try:
        configure(work, config, decision)
        enable(work, config, decision)
        start(work, config, decision)
        observe(work, config, decision)
    finally:
        close(work, config, decision)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "ready", "preflight", "publish", "deploy",
                                          "configure", "enable", "start", "observe", "close", "activate"))
    parser.add_argument("--work", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--approve")
    args = parser.parse_args()
    work = prior.root(args.work)
    config = release.load_config(args.config or work / "target.json")
    if args.action == "prepare":
        prepare(work, config, args.revision)
    else:
        decision = release.private_json(args.decision, max_bytes=65536)
        if args.action == "check":
            validate(work, config, decision)
        else:
            require(args.approve == args.action, "Explicit row-action approval required")
            globals()[args.action](work, config, decision)
    print("Row action confirmed; all earlier receipts and consumed reservations retained.")


if __name__ == "__main__":
    main()
