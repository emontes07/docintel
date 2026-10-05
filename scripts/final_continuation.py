"""One separately approved final rerun; all earlier receipts remain immutable.

Phase A (no clock/live operations): prepare --revision MERGED_SHA; retain exact
CI receipts, including ci.json, in final-rerun-v1; check --decision PRIVATE_JSON.
ci.json binds revision, merged=true, base=main and successful Validate Backend,
Validate Frontend and Validate Release checks on that exact merged revision.
After merged-source CI, the private gate and cost/capacity checks pass,
ready --approve ready creates the immutable clock once: 45 minutes to publish
backend then frontend, 90 minutes overall. No readiness renewal is allowed.
Only after readiness: publish,
deploy, activate --recovery PRIVATE_JSON. Activate performs configure, enable,
start and observe, and always attempts close in finally. Individual configure,
enable, start, observe and close actions remain available. Every mutation
requires --approve ACTION. A failed/unknown attempt is consumed, never retried.

Decision v2 retains those bounds but uses the separately hash-pinned incremental
$5 financial authority/audit, not historical forecasts or incidental maxima.
The existing synchronous ACR poller is metered without another submission;
worker observations read only final-execution actual model usage. Missing usage
is unknown, not a reserved charge. Telemetry is warning-only: it cannot stop a
worker, close processing early, or latch a cost stop. Request/token limits and
worker timeout remain server-enforced; authorized windows still require closure.
Before v2 ready, final-helper-validation.json must separately bind the committed
helper/test bytes and successful CI to the unchanged application decision.
"""

import argparse
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from time import monotonic, sleep
import uuid

if __package__:
    from . import pilot_continuation as prior
    from . import release
else:
    import pilot_continuation as prior
    import release


CHILD = "final-rerun-v1"
RECOVERY_KEY = "configuration/real-pilot-final-rerun.json"
WINDOW_POLICY = {"publication_seconds": 2700, "operating_seconds": 5400, "processing_seconds": 1200}
TIME_FIELDS = {"not_before", "expires_at", "readiness_at", "operating_expires_at"}
POLICY = {
    "backend_builds": 1, "frontend_builds": 1, "build_cpu": 2,
    "build_timeout_seconds": 900, "worker_executions": 1,
    "worker_timeout_seconds": 600, "item_limit": 2, "inference_requests": 4,
    "analysis": 0, "search": 0, "web_retrieval": 0, "retrieval": 0,
    "sharepoint": 0, "ford": 0, "processing_seconds": 1200,
    "remaining_inference_requests": 12, "remaining_input_tokens": 107146,
    "remaining_output_tokens": 24576, "total_microdollars": 10000000,
}
INCREMENTAL_POLICY = {key: value for key, value in POLICY.items() if key != "total_microdollars"}
INCREMENTAL_POLICY["incremental_operating_microdollars"] = 5000000
FINANCIAL_AUTHORITY_SHA256 = "be0add67b35954218a4ba6158acba7abed98ac9305c7d345353fa9e0b99931ed"
FINANCIAL_EVIDENCE_SHA256 = "6c58fa981ae091a17cd6ce15504b1f9dd462ebffd9f5337e54adadbfe6a3acb7"
VALIDATION_FILES = (*prior.VALIDATION_FILES, "frontend-smoke.json", "ci.json")
DECISION_FIELDS = {
    "schema_version", "approved", "approved_by", "target", "approval_sha256",
    "history_sha256", "prior_decision_sha256", "source_revision",
    "validation_sha256", "scope_sha256", "gate_sha256", "cost", "policy",
    "window_policy",
}
DECISION_V2_FIELDS = DECISION_FIELDS | {"gate_file", "financial_authority_sha256"}
GATE_CHECKS = {
    "production_run_batch", "real_guard_reservations", "full_four_prompt_match",
    "expected_token_reservations", "zero_new_analysis", "append_only_history",
    "immutable_results", "attempt_history_export", "owner_isolation", "unknown_citation_diagnostics",
}
require = release.require
sha = prior.sha
raw_sha = prior.raw_sha


def evidence_path(work, name):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json", name),
            "Evidence must be a single root-confined JSON basename")
    path = work / name
    require(not path.is_symlink() and path.resolve().parent == work.resolve(),
            "Financial/gate evidence must remain in the original root")
    require(path.is_file() and path.stat().st_mode & 0o077 == 0, "Evidence must remain an owner-only file")
    return path


def gate_path(work, decision):
    return evidence_path(work, decision["gate_file"] if decision["schema_version"] == 2
                         else "final-recovery-gate.json")


def cost_path(work, decision):
    return evidence_path(work, decision["cost"]["evidence_file"] if decision["schema_version"] == 2
                         else "final-cost-evidence.json")


def microdollars(value):
    return int((value * 1000000).to_integral_value(rounding=ROUND_CEILING))


def incremental_rates(work, decision):
    report = release.private_json(cost_path(work, decision))
    source = report["verified_retained_price_basis"]
    names = {
        "build": "acr_usd_per_vcpu_second", "worker_cpu": "worker_cpu_usd_per_vcpu_second",
        "worker_memory": "worker_memory_usd_per_gib_second",
        "input": "model_input_guard_usd_per_token", "output": "model_output_guard_usd_per_token",
    }
    rates = {name: Decimal(source[key]) for name, key in names.items()}
    require(all(rate.is_finite() and rate > 0 for rate in rates.values()),
            "Verified positive direct-compute/model rates required")
    return rates


def validate_incremental_cost(work, decision, approval):
    cost = decision["cost"]
    require(set(cost) == {"mode", "limit_microdollars", "evidence_file", "evidence_sha256"}
            and cost["mode"] == "incremental_operating"
            and type(cost["limit_microdollars"]) is int and cost["limit_microdollars"] == 5000000
            and cost["evidence_file"] == "final-financial-authority-cost-v2.json",
            "Only the separately approved incremental $5 authority is supported")
    authority_path = evidence_path(work, "final-financial-authority-v2.json")
    authority = release.private_json(authority_path)
    require(raw_sha(authority_path) == decision["financial_authority_sha256"] == FINANCIAL_AUTHORITY_SHA256
            and authority.get("approved") is True
            and authority["application_revision"] == decision["source_revision"]
            and authority["private_gate_receipt"] == decision["gate_file"]
            and authority["private_gate_sha256"] == decision["gate_sha256"]
            and authority["baseline_sha256"] == raw_sha(work / "final-baseline.json")
            and authority["original_ledger_sha256"]
            == release.private_json(gate_path(work, decision))["amendment_scope"]["ledger_sha256"],
            "The new financial authority must bind the accepted source, gate and unchanged baseline")
    path = cost_path(work, decision)
    report = release.private_json(path)
    require(raw_sha(path) == cost["evidence_sha256"] == FINANCIAL_EVIDENCE_SHA256
            and report.get("schema_version") == 2 and report.get("passed") is True
            and report.get("money_ready") is True and report.get("no_live_operations") is True
            and report["accepted_source_context"]["source_revision"] == decision["source_revision"]
            and report["evidence_integrity"]["all_checks_passed"] is True,
            "The immutable independent incremental financial audit must pass")
    for name, expected in report["evidence_integrity"]["source_receipt_raw_sha256"].items():
        retained = evidence_path(work, name)
        require(raw_sha(retained) == expected, "Retained financial source evidence changed")
    financial = report["financial_authority"]
    require(financial["new_separate_incremental_operating_allowance_microdollars"] == 5000000
            and financial["historical_usage_deducted_from_new_allowance"] is False
            and financial["incidental_estimates_are_disclosure_only"] is True
            and financial["incidental_maxima_are_not_a_gate"] is True
            and report["stop_rule"]["threshold_microdollars"] == 5000000
            and report["stop_rule"]["comparison"] == "strictly_greater_than",
            "Historical forecasts and incidental estimates are not incremental charges")
    prior_report = financial["prior_cost_report_preserved"]
    require(raw_sha(evidence_path(work, prior_report["name"])) == prior_report["sha256"],
            "The historical NO_GO report must remain unchanged")
    rates = incremental_rates(work, decision)
    require(rates["input"] == Decimal(approval["unit_prices_usd"]["input_token"])
            and rates["output"] == Decimal(approval["unit_prices_usd"]["output_token"]),
            "Original model guard prices cannot be discounted")
    with localcontext() as context:
        context.prec = 60
        maximum = (
            2 * microdollars(2 * 900 * rates["build"])
            + microdollars(600 * (rates["worker_cpu"] + 2 * rates["worker_memory"]))
            + microdollars(POLICY["remaining_input_tokens"] * rates["input"])
            + microdollars(4 * 2048 * rates["output"])
        )
    require(maximum == report["direct_compute_model_cost"]["maximum_direct_compute_model_microdollars"]
            and maximum <= 5000000,
            "Verified bounded direct compute/model envelope must fit the separate allowance")


def helper_validation(work, decision):
    path = evidence_path(work, "final-helper-validation.json")
    value = release.private_json(path)
    require(set(value) == {
        "schema_version", "decision_sha256", "application_revision", "helper_revision",
        "helper_sha256", "tests_sha256", "checks",
    } and type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["decision_sha256"] == sha(decision)
            and value["application_revision"] == decision["source_revision"]
            and re.fullmatch(r"[a-f0-9]{40}", value["helper_revision"])
            and value["helper_sha256"] == raw_sha(Path(__file__))
            and value["tests_sha256"] == raw_sha(Path(__file__).resolve().parents[1] / "tests/test_final_continuation.py")
            and value["checks"] == {
                "Validate Backend": "success", "Validate Frontend": "success", "Validate Release": "success",
            }, "Exact helper-only commit/CI receipt must bind this decision without replacing the application source")
    return value


def require_helper_commit(value):
    root = Path(__file__).resolve().parents[1]
    head = release.command(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    require(head == value["helper_revision"], "Readiness must use the committed and CI-tested helper checkout")
    for name, field in (("scripts/final_continuation.py", "helper_sha256"),
                        ("tests/test_final_continuation.py", "tests_sha256")):
        committed = release.command(["git", "show", head + ":" + name], cwd=root)
        require(hashlib.sha256(committed).hexdigest() == value[field],
                "Uncommitted helper/test changes cannot start readiness")


def latest_cost(work, decision):
    paths = sorted(work.glob("final-direct-cost-[0-9][0-9][0-9][0-9][0-9][0-9].json"))
    if not paths:
        return {"components": {}}, 0
    value = receipt(work, paths[-1].stem.removeprefix("final-"), decision)
    return value, int(paths[-1].stem.rsplit("-", 1)[1])


def monitoring_warning(work, decision, reason, error=None):
    """Persist allowlisted diagnostics without exposing console output or controlling work."""
    messages = {
        "usage_unavailable": "Cost/usage probe unavailable; usage remains unknown.",
        "cost_record_unavailable": "Cost telemetry could not be recorded; totals may be incomplete.",
        "publication_telemetry_unavailable": "Publication cost telemetry unavailable.",
        "execution_observation_unavailable": "Worker status probe unavailable; completion remains unknown.",
        "incomplete_cost_observation": "Cost/usage telemetry incomplete; missing usage is not zero.",
        "measured_direct_cost_exceeds_incremental_5_usd": "Observed direct cost exceeds the unchanged $5 allowance.",
        "publication_hard_limit_exceeded": "Publication telemetry reports a server-limit discrepancy.",
        "worker_or_model_hard_limit_exceeded": "Worker/model telemetry reports a server-limit discrepancy.",
    }
    code = reason if reason in messages else "cost_record_unavailable"
    message = (messages[code] + " Warning only; continuing observation without worker stop or early closure. "
               "Server request/token limits and worker timeout remain enforced.")
    error_type = type(error).__name__ if error is not None else None
    if error_type not in {None, "ValueError", "TypeError", "KeyError", "OSError", "TimeoutError", "RuntimeError"}:
        error_type = "Exception"
    persistence = ""
    try:
        release.save_once(work / f"final-monitoring-warning-{uuid.uuid4().hex}.json", {
            **binding(decision, work), "observed_at": now().isoformat(), "severity": "warning",
            "reason": code, "message": message, "error_type": error_type,
            "observation_only": True, "worker_stop_requested": False,
        })
    except Exception:
        persistence = " Durable warning unavailable; no worker control action taken."
    try:
        print(f"WARNING: {message}{persistence}", file=sys.stderr)
    except Exception:
        pass


@contextmanager
def monitoring(work, decision, reason):
    """Observation/parsing/persistence failures must not escape into activation cleanup."""
    try:
        yield
    except Exception as error:
        monitoring_warning(work, decision, reason, error)


def record_cost(work, decision, updates, *, warning_reason=None):
    """Replace cumulative component observations, never add reservations/forecasts."""
    previous, sequence = latest_cost(work, decision)
    for name, update in updates.items():
        known = previous["components"].get(name, {}).get("microdollars")
        if update["microdollars"] is None and known is not None:
            update = {**update, "microdollars": known, "complete": False, "last_known_only": True}
            updates = {**updates, name: update}
    components = {**previous["components"], **updates}
    require(set(components) <= {"backend", "frontend", "worker", "model"}, "Unexpected incremental component")
    require(all(value["microdollars"] is None
                or type(value["microdollars"]) is int and value["microdollars"] >= 0
                for value in components.values()), "Measured quantities cannot be negative or invented")
    known = sum(value["microdollars"] for value in components.values() if value["microdollars"] is not None)
    value = {
        **binding(decision, work), "observed_at": now().isoformat(), "components": components,
        "measured_known_microdollars": known,
        "unknown_components": [name for name, component in components.items() if not component["complete"]],
        "basis": "observed_quantities_at_verified_rates_not_provider_billing",
        "historical_and_incidental_costs_included": False, "reservations_charged": False,
    }
    release.save_once(work / f"final-direct-cost-{sequence + 1:06d}.json", value)
    if value["unknown_components"]:
        monitoring_warning(work, decision, "incomplete_cost_observation")
    if known > 5000000:
        monitoring_warning(work, decision, "measured_direct_cost_exceeds_incremental_5_usd")
    if warning_reason:
        monitoring_warning(work, decision, warning_reason)
    return value


def runtime_seconds(properties, end_key, *, terminal):
    start, end = properties.get("startTime"), properties.get(end_key)
    if not start or terminal and not end:
        return None
    first, last = instant(start), instant(end) if end else now()
    require(first <= last, "Observed runtime cannot be negative")
    elapsed = last - first
    return Decimal(elapsed.days * 86400 + elapsed.seconds) + Decimal(elapsed.microseconds) / 1000000


def build_cost(work, decision, kind, properties):
    terminal = properties.get("status") not in {"Queued", "Started", "Running"}
    seconds = runtime_seconds(properties, "finishTime", terminal=terminal)
    cpu = properties.get("agentConfiguration", {}).get("cpu")
    valid_cpu = type(cpu) is int and cpu > 0
    amount = microdollars(seconds * cpu * incremental_rates(work, decision)["build"]) \
        if seconds is not None and valid_cpu else None
    update = {
        "run_id": properties["runId"], "seconds": str(seconds) if seconds is not None else None,
        "vcpu": cpu, "microdollars": amount, "complete": terminal and amount is not None,
        "status": properties.get("status"), "start_time": properties.get("startTime"),
        "finish_time": properties.get("finishTime"),
    }
    violation = "publication_hard_limit_exceeded" if cpu != 2 or seconds is not None and seconds > 900 else None
    record_cost(work, decision, {kind: update}, warning_reason=violation)
    return update


@contextmanager
def publication_meter(work, config, decision, kind):
    """Observe the existing synchronous ACR poller; never schedule another run."""
    if decision["schema_version"] != 2:
        yield
        return
    original = release.azure
    resource = (f"https://management.azure.com/subscriptions/{config['subscription']}/resourceGroups/"
                f"{config['group']}/providers/Microsoft.ContainerRegistry/registries/{config['registry']}")

    def azure(*args, **kwargs):
        result = original(*args, **kwargs)
        with monitoring(work, decision, "publication_telemetry_unavailable"):
            queued_path = work / CHILD / f"final-{kind}-queued.json"
            if args[:3] == ("rest", "--method", "GET") and "--url" in args and queued_path.exists():
                queued = release.private_json(queued_path)
                run_id = queued["runId"]
                url = resource + "/runs/" + run_id + "?api-version=" + release.PUBLICATION_API_VERSION
                if args[args.index("--url") + 1] == url:
                    require(queued["attempt_sha256"] == raw_sha(work / f"final-{kind}-attempt.json"),
                            "ACR metering must bind only this reserved publication")
                    properties = result["properties"]
                    require(properties["runId"] == run_id, "ACR measured run identity changed")
                    build_cost(work, decision, kind, properties)
        return result

    release.azure = azure
    try:
        yield
    finally:
        release.azure = original


def usage_remote_code(approval, recovery):
    """Only final-execution usage metadata leaves the unchanged application."""
    packet = {"approval_sha256": sha(approval), "recovery": recovery}
    encoded = release.bounded_metadata(packet)
    marker = "DOCINTEL_FINAL_USAGE_OK:" + sha(packet)
    code = f'''import base64, json, zlib
from backend.batch_store import configured_store, read_json
from backend.real_pilot import BUDGET_KEY, _sha256
packet = json.loads(zlib.decompress(base64.b64decode({encoded!r}, validate=True)))
assert _sha256(packet) == {sha(packet)!r}
store = configured_store()
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
assert _sha256(approval) == packet["approval_sha256"]
candidate, _ = read_json(store, {RECOVERY_KEY!r})
assert candidate == packet["recovery"]
ledger, _ = read_json(store, BUDGET_KEY)
assert ledger["approval_sha256"] == packet["approval_sha256"]
final = ledger.get("final_rerun")
prior = candidate["prior_execution_ids"]
if final is None:
    assert _sha256(ledger) == candidate["ledger_sha256"]
    selected = {{}}
    execution = None
else:
    execution = final["execution_id"]
    assert final["sha256"] == _sha256(candidate) and final["baseline_sha256"] == candidate["ledger_sha256"]
    assert execution not in prior and set(ledger["executions"]) == set(prior) | {{execution}}
    selected = {{key: value for key, value in ledger["reservations"].items()
                if value["execution_id"] not in prior}}
    assert all(value["execution_id"] == execution for value in selected.values())
result = {{"recovery_sha256": _sha256(candidate), "execution_id": execution,
          "invalidated": ledger.get("invalidated"),
          "reservations": {{key: {{"operation": value["operation"], "actual_usage": value["actual_usage"]}}
                           for key, value in selected.items()}}}}
print("DOCINTEL_FINAL_USAGE:" + base64.b64encode(json.dumps(result, sort_keys=True).encode()).decode())
print({marker!r})
'''
    require(len(base64.b64encode(code.encode())) + 80 <= 16384, "Final usage metadata exceeds console bound")
    return code, marker


def read_model_usage(work, config, decision, approval, recovery, *, timeout):
    with live_operations(work, decision, "processing"):
        backend = release.app(config, config["backend"], timeout=timeout)
        release.require_pilot_identity(backend, approval["identities"]["api_principal_id"])
        code, marker = usage_remote_code(approval, recovery)
        output = release.console_code([
            "az", "containerapp", "exec", "--subscription", config["subscription"],
            "-g", config["group"], "-n", config["backend"],
            "--revision", backend["properties"]["latestReadyRevisionName"],
            "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors",
        ], code, marker, timeout=timeout)
    lines = output.decode().splitlines()
    reports = [line.removeprefix("DOCINTEL_FINAL_USAGE:") for line in lines
               if line.startswith("DOCINTEL_FINAL_USAGE:")]
    require(marker in lines and len(reports) == 1, "Final usage observation did not uniquely confirm")
    value = json.loads(base64.b64decode(reports[0], validate=True))
    require(set(value) == {"recovery_sha256", "execution_id", "invalidated", "reservations"}
            and value["recovery_sha256"] == sha(recovery), "Final usage belongs to a different recovery")
    return value


def worker_compute(work, decision, properties):
    terminal = properties["status"] in release.TERMINAL
    rates = incremental_rates(work, decision)
    seconds = runtime_seconds(properties, "endTime", terminal=terminal)
    return {"seconds": str(seconds) if seconds is not None else None,
            "microdollars": microdollars(seconds * (rates["worker_cpu"] + 2 * rates["worker_memory"]))
            if seconds is not None else None, "complete": terminal and seconds is not None,
            "vcpu": 1, "gib": 2, "status": properties["status"],
            "start_time": properties.get("startTime"), "end_time": properties.get("endTime")}


def worker_cost(work, decision, properties, usage):
    terminal = properties["status"] in release.TERMINAL
    rates = incremental_rates(work, decision)
    worker = worker_compute(work, decision, properties)
    seconds = Decimal(worker["seconds"]) if worker["seconds"] is not None else None
    tokens = {"input_tokens": 0, "output_tokens": 0, "analysis_pages": 0}
    unknown = []
    reservations = usage["reservations"]
    require(isinstance(reservations, dict) and all(isinstance(value, dict) for value in reservations.values())
            and (not reservations or isinstance(usage["execution_id"], str)
                 and re.fullmatch(r"[a-f0-9]{64}", usage["execution_id"])),
            "Only final-execution actual reservation metadata can be metered")
    violation = bool(usage["invalidated"]) or len(reservations) > 4
    for key, reservation in reservations.items():
        violation |= reservation["operation"] != "inference"
        actual = reservation["actual_usage"]
        if actual is None:
            unknown.append(key)
            continue
        require(set(actual) == set(tokens) and all(type(value) is int and value >= 0 for value in actual.values()),
                "Reported model usage must be nonnegative actual units")
        violation |= actual["output_tokens"] > 2048 or actual["analysis_pages"] != 0
        for name in tokens:
            tokens[name] += actual[name]
    violation |= tokens["input_tokens"] > POLICY["remaining_input_tokens"] or tokens["output_tokens"] > 8192
    amount = (microdollars(tokens["input_tokens"] * rates["input"])
              + microdollars(tokens["output_tokens"] * rates["output"]))
    model = {"actual_usage": tokens, "unknown_reservations": unknown, "execution_id": usage["execution_id"],
             "requests": len(reservations),
             "microdollars": None if unknown and len(unknown) == len(reservations) else amount,
             "complete": terminal and not unknown, "usage_sha256": sha(usage),
             "reservations": {key: {"operation": value["operation"], "actual_usage": value["actual_usage"]}
                              for key, value in reservations.items()}}
    reason = "worker_or_model_hard_limit_exceeded" if violation or seconds is not None and seconds > 600 else None
    return record_cost(work, decision, {"worker": worker, "model": model}, warning_reason=reason)


def stop_worker(work, config, decision, execution, reason):
    path = work / "final-worker-stop-attempt.json"
    if path.exists():
        require(receipt(work, "worker-stop-attempt", decision)["execution_name"] == execution,
                "A stop may target only the original final execution")
        return
    release.save_once(path, {**binding(decision, work), "execution_name": execution, "reason": reason})
    with live_operations(work, decision, "cleanup"):
        release.azure("containerapp", "job", "stop", "--subscription", config["subscription"],
                      "-g", config["group"], "-n", config["job"],
                      "--job-execution-name", execution, timeout=30)


def now():
    return datetime.now(timezone.utc)


def instant(value):
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "Timezone-aware bound required")
    return parsed


def scope_sha(recovery):
    return sha({key: value for key, value in recovery.items()
                if key not in TIME_FIELDS})


def binding(decision, work=None):
    value = {"decision_sha256": sha(decision), "target": decision["target"]}
    if work is not None:
        value["readiness_sha256"] = raw_sha(work / "final-readiness.json")
    return value


def receipt(work, name, decision):
    value = release.private_json(work / ("final-" + name + ".json"))
    require(all(value.get(key) == expected for key, expected in binding(decision, work).items()),
            "Final attempt receipt binding changed")
    return value


def history(work):
    paths = sorted(path for path in work.rglob("*.json")
                   if path.relative_to(work).parts[0] not in {CHILD, "source", "context"}
                   and not path.name.startswith("final-")
                   and "source" not in path.relative_to(work).parts
                   and "context" not in path.relative_to(work).parts)
    for path in paths:
        require(not path.is_symlink() and path.stat().st_mode & 0o077 == 0,
                "Prior evidence must remain owner-only regular files")
    return {str(path.relative_to(work)): raw_sha(path) for path in paths}


def original(work, config):
    approval = prior.original(work, config)
    decision = release.private_json(work / "continuation-decision-approved.json")
    prior.validate(work, config, decision)
    prior.receipt(work, "closed", decision)
    attempted = prior.receipt(work, "worker-attempt", decision)
    completed = prior.receipt(work, "worker-result", decision)
    require(attempted["attempt"] == completed["attempt"] == 2
            and isinstance(completed.get("execution_name"), str) and completed["execution_name"],
            "Both earlier worker allowances must remain consumed")
    return approval, decision, prior.published_config(work, config, decision)


def prepare(work, config, revision):
    """Export only; the reviewed source and CI must precede live publication."""
    with release.pilot_lock(work):
        approval, old, _ = original(work, config)
        require(re.fullmatch(r"[a-f0-9]{40}", revision or ""), "Full reviewed revision required")
        require(not (work / CHILD).exists(), "Final context already exists; no reset")
        retained = history(work)
        release.save_once(work / "final-baseline.json", {
            "target": release.fingerprint(config), "approval_sha256": sha(approval),
            "prior_decision_sha256": sha(old), "history": retained,
            "history_sha256": sha(retained), "source_revision": revision,
            "work_root": str(work), "root_pin_sha256": raw_sha(prior.ROOT_PIN),
        })
        release.stage(revision, work / CHILD)
        release.build_context(work / CHILD)


def validate_cost(work, decision, approval, old):
    if decision["schema_version"] == 2:
        validate_incremental_cost(work, decision, approval)
        return
    cost = decision["cost"]
    components = {
        "consumed_component_upper_microdollars", "backend_build_upper_microdollars",
        "frontend_build_upper_microdollars", "worker_upper_microdollars",
        "inference_upper_microdollars", "incidental_forecast_microdollars",
    }
    require(set(cost) == components | {"evidence_sha256"}
            and all(type(cost[name]) is int and cost[name] > 0 for name in components),
            "Every consumed/new compute, inference and incidental bound is required")
    previous = old["cost"]
    # The prior inference forecast included unused input; it was not all consumed.
    with localcontext() as context:
        context.prec = 60
        rates = [Decimal(approval["unit_prices_usd"][key]) * 1000000
                 for key in ("input_token", "output_token")]
        consumed_model = (
            rates[0] * (approval["limits"]["input_tokens"] - POLICY["remaining_input_tokens"])
            + rates[1] * (approval["limits"]["output_tokens"] - POLICY["remaining_output_tokens"])
        )
        consumed_floor = sum(previous[name] for name in (
            "consumed_component_upper_microdollars", "build_upper_microdollars",
            "worker_upper_microdollars",
        )) + int(consumed_model.to_integral_value(rounding=ROUND_CEILING))
        model = rates[0] * POLICY["remaining_input_tokens"] + rates[1] * min(
            4 * 2048, POLICY["remaining_output_tokens"])
        upper = int(model.to_integral_value(rounding=ROUND_CEILING))
        if any(rate != rate.to_integral_value() for rate in rates):
            consumed_floor += 3
            upper += 3
    require(cost["consumed_component_upper_microdollars"] >= consumed_floor
            and all(cost[name] >= previous["build_upper_microdollars"] for name in (
                "backend_build_upper_microdollars", "frontend_build_upper_microdollars"))
            and cost["worker_upper_microdollars"] >= previous["worker_upper_microdollars"]
            and cost["incidental_forecast_microdollars"] >= previous["incidental_forecast_microdollars"],
            "Earlier consumption and both new publications cannot be omitted")
    require(cost["inference_upper_microdollars"] >= upper,
            "Four complete inference reservations must fit retained original token prices")
    total = sum(cost[name] for name in components)
    require(total <= POLICY["total_microdollars"], "Original $10 total would be exceeded")
    path = work / "final-cost-evidence.json"
    evidence = release.private_json(path)
    require(not path.is_symlink() and raw_sha(path) == cost["evidence_sha256"]
            and evidence.get("money_ready") is True
            and evidence.get("assurance") == "conservative_forecast_not_billing_cap"
            and evidence.get("approval_granted") is False
            and evidence.get("approved_guard_price_basis_retained") is True
            and evidence.get("original_total_microdollars") == POLICY["total_microdollars"]
            and evidence.get("total_forecast_microdollars") == total
            and evidence.get("remaining_contingency_microdollars") == POLICY["total_microdollars"] - total,
            "Independently reviewed complete remaining-cost forecast required")
    proofs = evidence.get("verification_receipts")
    require(isinstance(proofs, dict) and set(proofs) == {"rates", "incidentals"},
            "Retained rates and updated finite incidental verification required")
    for kind in proofs:
        path = work / f"final-cost-{kind}-verified.json"
        proof = release.private_json(path)
        require(not path.is_symlink() and raw_sha(path) == proofs[kind]
                and proof.get("verified") is True
                and all(proof.get(key) == decision[key] for key in (
                    "target", "history_sha256", "source_revision"))
                and isinstance(proof.get("basis"), str) and proof["basis"].strip(),
                "Unbound or changed cost evidence")
        if kind == "rates":
            require(proof.get("unit_prices_usd") == approval["unit_prices_usd"]
                    and proof.get("component_upper_microdollars") == {
                        key: cost[key] for key in components if key != "incidental_forecast_microdollars"
                    }, "Retained prices must cover consumed costs and both publications")
        else:
            require(prior.incidental_ceiling(proof, execution_window_seconds=5400)
                    == cost["incidental_forecast_microdollars"],
                    "Updated incidental quantities and price evidence do not cover the rerun")


def validate(work, config, decision):
    """Entirely local. In particular, a failed gate cannot trigger model metadata."""
    approval, old, previous_config = original(work, config)
    baseline = release.private_json(work / "final-baseline.json")
    version = decision.get("schema_version")
    require(type(version) is int and version in (1, 2)
            and set(decision) == (DECISION_V2_FIELDS if version == 2 else DECISION_FIELDS)
            and decision["approved"] is True
            and decision["approved_by"] == approval["approved_by"],
            "Exact separately approved final decision required")
    require(decision["policy"] == (INCREMENTAL_POLICY if version == 2 else POLICY)
            and all(type(x) is int for x in decision["policy"].values()),
            "Only the final two builds and one two-item worker are approved")
    require(decision["window_policy"] == WINDOW_POLICY
            and all(type(value) is int for value in decision["window_policy"].values()),
            "Only readiness-anchored 45/90-minute authority is approved")
    retained = history(work)
    require(baseline["work_root"] == str(work)
            and baseline["root_pin_sha256"] == raw_sha(prior.ROOT_PIN)
            and retained == baseline["history"]
            and sha(retained) == baseline["history_sha256"],
            "All original and consumed continuation history must remain unchanged")
    expected = {
        "target": release.fingerprint(config), "approval_sha256": sha(approval),
        "prior_decision_sha256": sha(old), "history_sha256": sha(retained),
        "source_revision": baseline["source_revision"],
    }
    require(all(decision[key] == baseline[key] == value for key, value in expected.items()),
            "Final decision target, prior authority, history or source changed")
    child = work / CHILD
    require(not child.is_symlink() and child.resolve().parent == work,
            "Final context cannot escape its pinned original release root")
    require(sha({name: raw_sha(child / name) for name in VALIDATION_FILES}) == decision["validation_sha256"],
            "Final exact-source CI receipts changed")
    for kind in ("backend", "frontend"):
        release.require_component_source_receipts(child, kind)
    require(release.verify_source(child)["revision"] == decision["source_revision"],
            "Final source revision mismatch")
    ci = release.private_json(child / "ci.json")
    require(ci.get("revision") == decision["source_revision"] and ci.get("merged") is True
            and ci.get("base") == "main"
            and ci.get("checks") == {
                "Validate Backend": "success", "Validate Frontend": "success", "Validate Release": "success",
            }, "All three CI workflows must pass on the exact merged main revision")
    path = gate_path(work, decision)
    gate = release.private_json(path)
    require(raw_sha(path) == decision["gate_sha256"]
            and gate.get("schema_version") == 1 and gate.get("passed") is True
            and gate.get("no_live_operations") is True
            and gate.get("window_policy") == WINDOW_POLICY
            and all(gate.get(key) == decision[key] for key in (
                "target", "approval_sha256", "history_sha256", "source_revision", "scope_sha256"))
            and isinstance(gate.get("checks"), dict) and gate["checks"]
            and GATE_CHECKS <= set(gate["checks"])
            and all(value is True for value in gate["checks"].values()),
            "Private append-only recovery gate must pass before any live action")
    require(isinstance(gate.get("amendment_scope"), dict)
            and not TIME_FIELDS.intersection(gate["amendment_scope"])
            and sha(gate["amendment_scope"]) == decision["scope_sha256"],
            "Private gate must retain the exact validated final amendment scope")
    remaining_capacity(approval)
    validate_cost(work, decision, approval, old)
    for path in work.glob("final-*-attempt.json"):
        candidate = release.private_json(path)
        require(candidate.get("decision_sha256") == sha(decision),
                "Final authority already pinned; no reset or rollover")
    return approval, previous_config


def remaining_capacity(approval):
    # These consumed counters are verified by the exact snapshot/private gate.
    remaining = {
        "inference_requests": approval["limits"]["inference"] - 4,
        "input_tokens": approval["limits"]["input_tokens"] - 92854,
        "output_tokens": approval["limits"]["output_tokens"] - 8192,
    }
    require(remaining == {name: POLICY["remaining_" + name] for name in remaining},
            "Retained original allowance has insufficient remaining capacity")
    return remaining


def ready(work, config, decision):
    """Only the successful local readiness write starts Phase B; never renew it."""
    with release.pilot_lock(work):
        path = work / "final-readiness.json"
        pin = work / "final-readiness-pin.json"
        require(not path.exists() and not pin.exists(), "Final readiness already created; never renew")
        approval, _ = validate(work, config, decision)
        require(not list(work.glob("final-*-attempt.json")), "Final live authority already consumed")
        capacity = remaining_capacity(approval)
        helper = None
        if decision["schema_version"] == 2:
            helper = helper_validation(work, decision)
            require_helper_commit(helper)
        anchor = now()
        value = {
            "schema_version": 1, **binding(decision),
            "source_revision": decision["source_revision"], "gate_sha256": decision["gate_sha256"],
            "cost_sha256": decision["cost"]["evidence_sha256"],
            "validation_sha256": decision["validation_sha256"], "window_policy": WINDOW_POLICY,
            "remaining_capacity": capacity, "readiness_at": anchor.isoformat(),
            "publication_expires_at": (anchor + timedelta(seconds=2700)).isoformat(),
            "overall_expires_at": (anchor + timedelta(seconds=5400)).isoformat(),
        }
        if helper is not None:
            value.update(financial_authority_sha256=decision["financial_authority_sha256"],
                         helper_sha256=helper["helper_sha256"], helper_revision=helper["helper_revision"],
                         helper_validation_sha256=raw_sha(work / "final-helper-validation.json"))
        release.save_once(path, value)
        release.save_once(pin, {**binding(decision), "readiness_sha256": raw_sha(path)})
        return value


def readiness(work, decision):
    path = work / "final-readiness.json"
    value = release.private_json(path)
    pin = release.private_json(work / "final-readiness-pin.json")
    expected = {
        "schema_version": 1, **binding(decision), "source_revision": decision["source_revision"],
        "gate_sha256": decision["gate_sha256"], "cost_sha256": decision["cost"]["evidence_sha256"],
        "validation_sha256": decision["validation_sha256"], "window_policy": WINDOW_POLICY,
        "remaining_capacity": {name: POLICY["remaining_" + name]
                               for name in ("inference_requests", "input_tokens", "output_tokens")},
    }
    if decision["schema_version"] == 2:
        helper = helper_validation(work, decision)
        expected.update(financial_authority_sha256=decision["financial_authority_sha256"],
                        helper_sha256=helper["helper_sha256"], helper_revision=helper["helper_revision"],
                        helper_validation_sha256=raw_sha(work / "final-helper-validation.json"))
        require(raw_sha(evidence_path(work, "final-financial-authority-v2.json"))
                == decision["financial_authority_sha256"] == FINANCIAL_AUTHORITY_SHA256,
                "Incremental financial authority changed")
    require(not path.is_symlink() and not (work / "final-readiness-pin.json").is_symlink()
            and pin == {**binding(decision), "readiness_sha256": raw_sha(path)}
            and set(value) == set(expected) | {"readiness_at", "publication_expires_at", "overall_expires_at"}
            and all(value.get(key) == expected_value for key, expected_value in expected.items()),
            "Exact immutable final readiness and source/decision/gate/cost bindings required")
    require(raw_sha(gate_path(work, decision)) == value["gate_sha256"]
            and raw_sha(cost_path(work, decision)) == value["cost_sha256"]
            and sha({name: raw_sha(work / CHILD / name) for name in VALIDATION_FILES})
            == value["validation_sha256"], "Readiness evidence changed")
    anchor = instant(value["readiness_at"])
    require(instant(value["publication_expires_at"]) == anchor + timedelta(seconds=2700)
            and instant(value["overall_expires_at"]) == anchor + timedelta(seconds=5400),
            "Readiness deadlines must be exactly 2700/5400 seconds")
    return value


def publication_window(work, decision, minimum=900):
    value = readiness(work, decision)
    current = now()
    require(instant(value["readiness_at"]) <= current
            and (instant(value["publication_expires_at"]) - current).total_seconds() >= minimum,
            "Insufficient final publication window; no upload/queue attempt")
    return value


def live_window(work, decision, phase, minimum=0):
    require(phase in {"publication", "overall", "processing", "cleanup"}, "Unknown final live phase")
    value = readiness(work, decision)
    current = now()
    require(instant(value["readiness_at"]) <= current, "Final readiness clock has not started")
    if phase == "cleanup":
        # Disabling authorization/stopping the reserved worker remains fail-safe after expiry.
        return value
    end = instant(value["publication_expires_at" if phase == "publication" else "overall_expires_at"])
    if phase == "processing":
        end = min(end, instant(receipt(work, "deployed", decision)["processing_expires_at"]))
    require(current < end and (end - current).total_seconds() >= minimum,
            f"Insufficient final {phase} window; {minimum} seconds required")
    return value


@contextmanager
def live_operations(work, decision, phase, minimum=0):
    """Guard even nested existing-helper cloud calls, including upload/submit."""
    live_window(work, decision, phase, minimum)
    pinned = raw_sha(work / "final-readiness.json")
    originals = {name: getattr(release, name) for name in (
        "azure", "run_pilot_console", "console_code", "upload_publication_context",
    )}

    def checked(name, operation):
        def call(*args, **kwargs):
            require(raw_sha(work / "final-readiness.json") == pinned, "Final readiness changed during action")
            needed = minimum
            if phase == "publication" and (
                name == "upload_publication_context" or args[:3] == ("rest", "--method", "POST")
            ):
                needed = max(needed, 900)
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


def preflight(work, config, decision):
    approval, previous_config = validate(work, config, decision)
    publication_window(work, decision)
    with live_operations(work, decision, "publication", 900):
        release.verify_target(previous_config)
        release.identity_contract(previous_config)
        release.require_real_pilot_off(prior.ready(previous_config, "backend"))
        prior.ready(previous_config, "frontend")
        prior.require_worker(previous_config, approval)
        gate = release.private_json(gate_path(work, decision))
        code, marker = preflight_remote_code(approval, gate["amendment_scope"])
        backend = release.app(previous_config, previous_config["backend"])
        release.require_pilot_identity(backend, approval["identities"]["api_principal_id"])
        release.run_pilot_console(previous_config, backend, code, marker)
        return prior.require_model_capacity(previous_config, approval)


def preflight_remote_code(approval, scope):
    """Verify snapshot bindings on the previous image without needing new code."""
    payload = {"approval_sha256": sha(approval), "batch_id": approval["batch_id"], "scope": scope}
    encoded = release.bounded_metadata(payload)
    marker = "DOCINTEL_FINAL_SNAPSHOT_OK:" + sha(payload)
    code = f'''import base64, hashlib, json, zlib
from backend.batch_store import Missing, configured_store, read_json
from backend.real_pilot import BUDGET_KEY, _sha256, binding_digest
packet = json.loads(zlib.decompress(base64.b64decode({encoded!r}, validate=True)))
assert _sha256(packet) == {sha(payload)!r}
store = configured_store()
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
assert _sha256(approval) == packet["approval_sha256"]
scope = packet["scope"]
batch, _ = read_json(store, "batches/" + packet["batch_id"] + ".json")
assert binding_digest(batch) == approval["batch_sha256"] == scope["batch_sha256"]
ledger, _ = read_json(store, BUDGET_KEY)
assert _sha256(ledger) == scope["ledger_sha256"] and not ledger.get("invalidated")
assert ledger["approval_sha256"] == _sha256(approval)
assert list(ledger["executions"]) == scope["prior_execution_ids"]
assert ledger["attempted"]["inference"] == 4
assert ledger["reserved"]["input_tokens"] == 92854 and ledger["reserved"]["output_tokens"] == 8192
assert all(ledger["attempted"][kind] == 0 for kind in ("search", "web_retrieval", "retrieval"))
previous, _ = read_json(store, "configuration/real-pilot-recovery.json")
assert _sha256(previous) == scope["prior_recovery_sha256"]
assert previous["selected_item_keys"] == scope["selected_item_keys"]
assert previous["cached_documents"] == scope["cached_documents"]
for item in batch["items"]:
    key = item["item_key"]
    status, _ = store.read_bytes("items/" + batch["id"] + "/" + key + ".json")
    if key not in scope["selected_item_keys"]:
        assert json.loads(status)["state"] == "deferred"
        continue
    assert hashlib.sha256(status).hexdigest() == scope["item_sha256"][key]
    result, _ = store.read_bytes("results/" + batch["id"] + "/" + key + ".json")
    assert hashlib.sha256(result).hexdigest() == scope["result_sha256"][key]
    try:
        reviews, _ = read_json(store, "reviews/" + batch["id"] + "/" + key + ".json")
    except Missing:
        reviews = []
    assert not reviews
for key, expected in scope["cached_documents"].items():
    content, _ = store.read_bytes(key)
    assert hashlib.sha256(content).hexdigest() == expected
for key in ({RECOVERY_KEY!r}, "operations/real-pilot-final-rerun.json"):
    try:
        store.read_bytes(key)
    except Missing:
        pass
    else:
        raise ValueError("Final rerun already configured or attempted")
print({marker!r})
'''
    require(len(base64.b64encode(code.encode())) + 80 <= 16384, "Final snapshot metadata exceeds transport bound")
    return code, marker


def publish_component(work, config, decision, kind, capacity):
    validate(work, config, decision)
    require(kind in {"backend", "frontend"}, "Only the two approved publications are allowed")
    if kind == "frontend":
        receipt(work, "backend-published", decision)
    else:
        require(not (work / "final-frontend-attempt.json").exists(), "Backend must be the first publication")
    value = publication_window(work, decision)
    path = work / f"final-{kind}-attempt.json"
    release.save_once(path, {
        **binding(decision, work), "attempt_id": str(uuid.uuid4()), "kind": kind,
        "cpu": 2, "timeout_seconds": 900, "status": "attempted_outcome_unknown",
        "model_capacity": capacity, "helper_sha256": raw_sha(Path(__file__)),
    })
    publication_window(work, decision)
    try:
        with publication_meter(work, config, decision, kind), live_operations(work, decision, "publication"):
            result = release.execute_publication(
                config, work / CHILD, kind, decision["source_revision"], path,
                expires=instant(value["publication_expires_at"]), validate_upload_window=True,
            )
    except Exception:
        if decision["schema_version"] == 2:
            with monitoring(work, decision, "cost_record_unavailable"):
                record_cost(work, decision, {kind: {
                    "microdollars": None, "complete": False, "observation": "publication_outcome_unknown",
                }})
        raise
    if decision["schema_version"] == 2:
        with monitoring(work, decision, "publication_telemetry_unavailable"):
            build_cost(work, decision, kind, result)
    release.save_once(work / f"final-{kind}-published.json", {
        **binding(decision, work), **result,
        "image": config["registry"] + f".azurecr.io/docintel/{kind}@" + result["digest"],
    })


def publish(work, config, decision):
    with release.pilot_lock(work):
        require(not any((work / f"final-{kind}-attempt.json").exists() for kind in ("backend", "frontend")),
                "Final publication already attempted; never retry either component")
        capacity = preflight(work, config, decision)
        for kind in ("backend", "frontend"):
            publish_component(work, config, decision, kind, capacity)
        images = {"revision": decision["source_revision"]}
        for kind in ("backend", "frontend"):
            result = receipt(work, kind + "-published", decision)
            images[kind], images[kind + "_run_id"] = result["image"], result["runId"]
        release.save_once(work / CHILD / "images.json", images)
        release.save_once(work / "final-published.json", {
            **binding(decision, work), "images_sha256": raw_sha(work / CHILD / "images.json"),
        })


def published_config(work, config, decision):
    published = receipt(work, "published", decision)
    path = work / CHILD / "images.json"
    require(raw_sha(path) == published["images_sha256"], "Final image receipt changed")
    images = release.private_json(path)
    current = {**config, **{kind + "_image": images[kind] for kind in ("backend", "frontend")}}
    release.require_release_images(current, work / CHILD)
    release.require_pilot_build_receipts(current, work / CHILD)
    return current


def patch(work, config, label, resource, containers, *, decision, job=False, kind="backend"):
    phase = "cleanup" if label == "close" else "processing" if label == "enable" else "overall"
    with live_operations(work, decision, phase, 0 if phase == "cleanup" else 600):
        prior.patch(work, config, label, resource, containers, job=job, kind=kind, prefix="final")


def deploy(work, config, decision):
    with release.pilot_lock(work), live_operations(work, decision, "overall", 600):
        require(not (work / "final-deploy-attempt.json").exists(), "Final deployment already attempted")
        approval, previous_config = validate(work, config, decision)
        current = published_config(work, config, decision)
        value = live_window(work, decision, "overall", 600)
        release.verify_target(previous_config)
        release.identity_contract(previous_config)
        backend, frontend = (prior.ready(previous_config, kind) for kind in ("backend", "frontend"))
        release.require_real_pilot_off(backend)
        job = prior.require_worker(previous_config, approval)
        require(job["properties"]["configuration"]["triggerType"] == "Manual", "Existing manual worker required")
        release.save_once(work / "final-deploy-attempt.json", {
            **binding(decision, work), "backend": prior.shape(backend),
            "frontend": prior.shape(frontend), "worker": prior.shape(job),
        })
        for kind, resource, is_job in (("backend", backend, False), ("worker", job, True),
                                      ("frontend", frontend, False)):
            live_window(work, decision, "overall", 600)
            containers = release.safe_containers(resource)
            containers[0]["image"] = current["frontend_image" if kind == "frontend" else "backend_image"]
            patch(work, previous_config, "deploy-" + kind, resource, containers,
                  decision=decision, job=is_job, kind="frontend" if kind == "frontend" else "backend")
        for kind in ("backend", "frontend"):
            prior.ready(current, kind)
        ready_at = now()
        end = min(ready_at + timedelta(seconds=1200), instant(value["overall_expires_at"]))
        require((end - ready_at).total_seconds() >= 600, "No safe worker window after both apps became ready")
        release.save_once(work / "final-deployed.json", {
            **binding(decision, work), "both_ready_at": ready_at.isoformat(),
            "processing_expires_at": end.isoformat(),
        })


def active_target(work, config, decision, approval, *, processing=True):
    with live_operations(work, decision, "processing" if processing else "cleanup", 600 if processing else 0):
        receipt(work, "deployed", decision)
        baseline = receipt(work, "deploy-attempt", decision)
        current = published_config(work, config, decision)
        release.verify_target(current)
        release.identity_contract(current)
        backend = prior.ready(current, "backend")
        frontend = prior.ready(current, "frontend")
        job = prior.require_worker(current, approval) if processing else release.active_executions(current)[0]
        require(job["properties"]["configuration"]["triggerType"] == "Manual", "Manual worker required")
        for kind, resource in (("backend", backend), ("frontend", frontend), ("worker", job)):
            expected, actual = copy.deepcopy(baseline[kind]), prior.shape(resource)
            expected["template"]["containers"][0]["image"] = current[
                "frontend_image" if kind == "frontend" else "backend_image"]
            if kind == "backend":
                for value in (expected, actual):
                    value["template"]["containers"][0]["env"] = [
                        entry for entry in value["template"]["containers"][0].get("env", [])
                        if entry["name"] not in release.PILOT_API_SETTINGS]
            require(actual == expected, "Final runtime, authentication, identity or storage drift")
        capacity = prior.require_model_capacity(current, approval) if processing else None
        return current, backend, job, capacity


def recovery_packet(work, decision, approval, recovery):
    value = readiness(work, decision)
    require(scope_sha(recovery) == decision["scope_sha256"]
            and recovery.get("approved") is True and recovery.get("approved_by") == approval["approved_by"]
            and recovery.get("approval_sha256") == sha(approval),
            "Final amendment must bind the privately validated selection and original approval")
    require(recovery["readiness_at"] == value["readiness_at"]
            and recovery["operating_expires_at"] == value["overall_expires_at"],
            "Final amendment must derive its exact readiness and overall expiry from the receipt")
    deployed = receipt(work, "deployed", decision)
    start, end = instant(recovery["not_before"]), instant(recovery["expires_at"])
    require(recovery["not_before"] == deployed["both_ready_at"]
            and recovery["expires_at"] == deployed["processing_expires_at"]
            and instant(value["readiness_at"]) <= start < end <= instant(value["overall_expires_at"])
            and end - start <= timedelta(seconds=1200),
            "Processing is bounded to twenty minutes after both exact revisions are ready")


def processing_window(recovery, minimum=600):
    require(instant(recovery["not_before"]) <= now()
            and (instant(recovery["expires_at"]) - now()).total_seconds() >= minimum,
            "At least 600 seconds must remain before a final worker start")


def remote_code(approval, decision, recovery, *, write=False, configured=False):
    """Uses the backend's validator; emits only a digest marker, not private data."""
    payload = {"approval_sha256": sha(approval), "batch_id": approval["batch_id"],
               "scope_sha256": decision["scope_sha256"], "recovery": recovery,
               "configured": configured}
    encoded = release.bounded_metadata(payload)
    marker = "DOCINTEL_FINAL_RERUN_OK:" + sha(payload)
    code = f'''import base64, json, os, zlib
from backend.batch_store import Missing, configured_store, read_json, write_json
from backend.real_pilot import BUDGET_KEY, _sha256, validate_final_rerun
raw = zlib.decompress(base64.b64decode({encoded!r}, validate=True))
assert len(raw) <= 65536
packet = json.loads(raw)
assert _sha256(packet) == {sha(payload)!r}
store = configured_store()
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
assert _sha256(approval) == packet["approval_sha256"]
batch_key = "batches/" + packet["batch_id"] + ".json"
batch, _ = read_json(store, batch_key)
ledger, _ = read_json(store, BUDGET_KEY)
os.environ["DOCINTEL_REAL_PILOT_OPERATOR_IDS"] = approval["approved_by"]
class ReadOnly:
    def read_bytes(self, name, **kwargs):
        return store.read_bytes(name, **kwargs)
candidate = packet["recovery"]
assert _sha256({{k: v for k, v in candidate.items() if k not in ("not_before", "expires_at", "readiness_at", "operating_expires_at")}}) == packet["scope_sha256"]
assert validate_final_rerun(candidate, approval, batch, ledger, ReadOnly()) == candidate
try:
    existing, _ = read_json(store, {RECOVERY_KEY!r})
except Missing:
    existing = None
assert existing is None or existing == candidate
assert not packet["configured"] or existing == candidate
assert not packet["configured"] or batch["state"] == "queued"
'''
    if write:
        code += f'''assert existing is None
with store.lease(batch["id"]) as fence:
    fresh, version = read_json(store, batch_key)
    assert fresh == batch and fresh["state"] in ("completed", "deferred", "queued")
    current_ledger, _ = read_json(store, BUDGET_KEY)
    assert current_ledger == ledger
    assert validate_final_rerun(candidate, approval, fresh, current_ledger, ReadOnly()) == candidate
    fence()
    write_json(store, {RECOVERY_KEY!r}, candidate)
    verified, _ = read_json(store, {RECOVERY_KEY!r})
    assert verified == candidate
    if fresh["state"] != "queued":
        fresh["state"] = "queued"
        fence()
        write_json(store, batch_key, fresh, version)
    queued, _ = read_json(store, batch_key)
    assert queued == fresh and queued["state"] == "queued"
'''
    code += f"print({marker!r})\n"
    require(len(base64.b64encode(code.encode())) + 80 <= 16384, "Final console metadata exceeds transport bound")
    return code, marker


def verify_remote(work, config, approval, decision, recovery, *, write=False, configured=False):
    recovery_packet(work, decision, approval, recovery)
    processing_window(recovery)
    with live_operations(work, decision, "processing", 600):
        backend = release.app(config, config["backend"])
        release.require_pilot_identity(backend, approval["identities"]["api_principal_id"])
        code, marker = remote_code(approval, decision, recovery, write=write, configured=configured)
        release.run_pilot_console(config, backend, code, marker)


def configure(work, config, decision, recovery):
    with release.pilot_lock(work), live_operations(work, decision, "processing", 600):
        require(not (work / "final-closed.json").exists(), "Final activation is closed")
        approval, _ = validate(work, config, decision)
        recovery_packet(work, decision, approval, recovery)
        processing_window(recovery)
        current, backend, _, capacity = active_target(work, config, decision, approval)
        release.require_real_pilot_off(backend)
        require(not (work / "final-configure-attempt.json").exists(), "Final configure already attempted")
        verify_remote(work, current, approval, decision, recovery)
        release.save_once(work / "final-configure-attempt.json", {
            **binding(decision, work), "recovery": recovery, "recovery_sha256": sha(recovery),
            "model_capacity": capacity,
        })
        processing_window(recovery)
        verify_remote(work, current, approval, decision, recovery, write=True)
        release.save_once(work / "final-configured.json", {**binding(decision, work), "recovery_sha256": sha(recovery)})


def configured_recovery(work, decision, approval):
    candidate = receipt(work, "configure-attempt", decision)
    recovery = candidate["recovery"]
    require(candidate["recovery_sha256"] == receipt(work, "configured", decision)["recovery_sha256"] == sha(recovery),
            "Final configured amendment changed")
    recovery_packet(work, decision, approval, recovery)
    return recovery


def enable(work, config, decision):
    with release.pilot_lock(work), live_operations(work, decision, "processing", 600):
        require(not (work / "final-closed.json").exists()
                and not (work / "final-enablement.json").exists(),
                "Final activation is closed or already attempted")
        approval, _ = validate(work, config, decision)
        recovery = configured_recovery(work, decision, approval)
        current, backend, _, capacity = active_target(work, config, decision, approval)
        release.require_real_pilot_off(backend)
        containers = release.safe_containers(backend)
        entries = release.environment_entries(containers[0])
        require(entries.get("DOCINTEL_PILOT_UPLOAD_ENABLED") in (
            None, {"name": "DOCINTEL_PILOT_UPLOAD_ENABLED", "value": "false"}), "Source upload must remain disabled")
        verify_remote(work, current, approval, decision, recovery, configured=True)
        processing_window(recovery)
        intended = {name: {"name": name, "value": value} for name, value in release.pilot_values(approval).items()}
        release.save_once(work / "final-enablement.json", {
            **binding(decision, work), "recovery_sha256": sha(recovery),
            "baseline": {name: entries.get(name) for name in release.PILOT_API_SETTINGS},
            "intended": intended, "model_capacity": capacity,
        })
        release.merge_environment(containers[0], release.pilot_values(approval))
        patch(work, current, "enable", backend, containers, decision=decision)
        prior.ready(current, "backend")
        release.save_once(work / "final-enabled.json", binding(decision, work))


def start(work, config, decision):
    with release.pilot_lock(work), live_operations(work, decision, "processing", 600):
        approval, _ = validate(work, config, decision)
        recovery = configured_recovery(work, decision, approval)
        receipt(work, "enabled", decision)
        require(not (work / "final-closed.json").exists()
                and not (work / "final-worker-attempt.json").exists(), "Final worker closed or consumed; never retry")
        current, backend, job, capacity = active_target(work, config, decision, approval)
        entries = release.environment_entries(release.safe_containers(backend)[0])
        require(all(entries.get(name) == value for name, value in receipt(work, "enablement", decision)["intended"].items()),
                "Enabled authorization drift")
        verify_remote(work, current, approval, decision, recovery, configured=True)
        template = copy.deepcopy(job["properties"]["template"])
        container = template["containers"][0]
        container["command"] = ["/app/.venv/bin/python"]
        container["args"] = ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", approval["batch_id"],
                             "--concurrency", "1", "--max-batches", "1", "--item-limit", "2"]
        container["env"] = [entry for entry in container.get("env", [])
                            if entry["name"] not in release.PILOT_EXTERNAL_ENVIRONMENT_KEYS | {"WEBIQ_API_KEY"}]
        release.merge_environment(container, {**approval["environment"], **release.pilot_values(approval),
                                              "DOCINTEL_REAL_PILOT_EXECUTION_SCOPE": "internal_only"})
        template["containers"] = release.writable_containers(template["containers"])
        processing_window(recovery)
        fresh, running = release.active_executions(current)
        require(not running and fresh == job, "Worker changed before the final start")
        path = work / "final-worker-template.json"
        release.save_once(path, template)
        release.save_once(work / "final-worker-attempt.json", {
            **binding(decision, work), "recovery_sha256": sha(recovery), "execution_sha256": sha(template),
            "prior_worker_attempt_sha256": raw_sha(work / "continuation-worker-attempt.json"),
            "attempt": 3, "state": "start_attempted_completion_unknown", "model_capacity": capacity,
            "attempted_at": now().isoformat(),
        })
        processing_window(recovery)
        result = release.azure("containerapp", "job", "start", "--subscription", current["subscription"],
                               "-g", current["group"], "-n", current["job"], "--yaml", str(path))
        require(isinstance(result, dict) and isinstance(result.get("name"), str) and result["name"],
                "Worker start uncertain; inspect existing execution, never repeat")
        release.save_once(work / "final-worker-result.json", {
            **binding(decision, work), "attempt": 3, "execution_name": result["name"],
        })


def observe(work, config, decision):
    """Observe only the already-reserved execution; never start or retry it."""
    approval, _ = validate(work, config, decision)
    ready = readiness(work, decision)
    recovery = configured_recovery(work, decision, approval)
    current = published_config(work, config, decision)
    execution = receipt(work, "worker-result", decision)["execution_name"]
    attempted = receipt(work, "worker-attempt", decision)
    deadline = min(instant(recovery["expires_at"]),
                   instant(ready["overall_expires_at"]),
                   instant(attempted["attempted_at"]) + timedelta(seconds=600))
    timeout = monotonic() + max(0, (deadline - now()).total_seconds())
    terminal = work / "final-worker-terminal.json"
    if terminal.exists():
        existing = receipt(work, "worker-terminal", decision)
        require(existing["execution_name"] == execution,
                "Final terminal receipt execution changed")
        require(existing.get("status") == "Succeeded", "Final worker terminated unsuccessfully; no retry")
        if decision["schema_version"] == 2:
            with monitoring(work, decision, "cost_record_unavailable"):
                if latest_cost(work, decision)[0].get("unknown_components", ["unmeasured"]):
                    monitoring_warning(work, decision, "incomplete_cost_observation")
        return
    while now() < deadline and monotonic() < timeout:
        remaining = min((deadline - now()).total_seconds(), timeout - monotonic())
        if remaining <= 0:
            break
        properties = None
        with live_operations(work, decision, "processing"):
            with monitoring(work, decision, "execution_observation_unavailable"):
                executions = release.azure(
                    "containerapp", "job", "execution", "list", "--subscription", current["subscription"],
                    "-g", current["group"], "-n", current["job"], timeout=min(30, remaining),
                )
                matches = [entry for entry in executions if entry["name"] == execution]
                require(len(matches) == 1, "Started execution not uniquely visible; never resubmit")
                candidate = matches[0]["properties"]
                require(isinstance(candidate["status"], str) and candidate["status"],
                        "Worker status unavailable")
                properties = candidate
        if properties is not None:
            status = properties["status"]
            if decision["schema_version"] == 2:
                try:
                    remaining = min((deadline - now()).total_seconds(), timeout - monotonic())
                    require(remaining > 0, "Worker observation window elapsed before usage measurement")
                    usage = read_model_usage(work, current, decision, approval, recovery,
                                             timeout=min(30, remaining / 2))
                    worker_cost(work, decision, properties, usage)
                except Exception as error:
                    monitoring_warning(work, decision, "usage_unavailable", error)
                    with monitoring(work, decision, "cost_record_unavailable"):
                        record_cost(work, decision, {
                            "worker": worker_compute(work, decision, properties),
                            "model": {"microdollars": None, "complete": False, "observation": "usage_unavailable"},
                        })
            if status in release.TERMINAL:
                release.save_once(terminal, {
                    **binding(decision, work), "execution_name": execution, "attempt": 3,
                    "observed_at": now().isoformat(), "no_resubmission": True,
                    **{name: properties.get(name) for name in ("status", "startTime", "endTime")},
                })
                require(status == "Succeeded", "Final worker terminated unsuccessfully; no retry")
                return
        sleep(min(15, max(0, (deadline - now()).total_seconds()), max(0, timeout - monotonic())))
    stop_worker(work, current, decision, execution, "bounded_worker_window_expired")
    raise ValueError("Final worker observation expired; same execution stop requested, never restart")


def close(work, config, decision):
    """Restores authorization even after deadline/capacity failure or unknown start."""
    with release.pilot_lock(work), live_operations(work, decision, "cleanup"):
        approval, _ = validate(work, config, decision)
        current, backend, _, _ = active_target(work, config, decision, approval, processing=False)
        if not (work / "final-enablement.json").exists():
            release.require_real_pilot_off(backend)
            if not (work / "final-closed.json").exists():
                release.save_once(work / "final-closed.json", binding(decision, work))
            return
        baseline = receipt(work, "enablement", decision)
        containers = release.safe_containers(backend)
        entries = release.environment_entries(containers[0])
        actual = {name: entries.get(name) for name in release.PILOT_API_SETTINGS}
        require(actual in (baseline["baseline"], baseline["intended"]), "Unsafe managed-setting closure drift")
        if actual != baseline["baseline"]:
            release.save_once(work / "final-close-attempt.json", binding(decision, work))
            for name, entry in baseline["baseline"].items():
                if entry is None:
                    entries.pop(name, None)
                else:
                    entries[name] = entry
            containers[0]["env"] = list(entries.values())
            patch(work, current, "close", backend, containers, decision=decision)
        prior.ready(current, "backend")
        if not (work / "final-closed.json").exists():
            release.save_once(work / "final-closed.json", binding(decision, work))


def activate(work, config, decision, recovery):
    """One activation orchestration, with mandatory closure on every outcome."""
    with release.pilot_lock(work):
        validate(work, config, decision)
        live_window(work, decision, "processing", 600)
        receipt(work, "deployed", decision)
        require(not (work / "final-closed.json").exists(), "Final activation is closed")
        release.save_once(work / "final-activation-attempt.json", binding(decision, work))
    try:
        configure(work, config, decision, recovery)
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
    parser.add_argument("--recovery", type=Path)
    parser.add_argument("--approve")
    args = parser.parse_args()
    work = prior.root(args.work)
    config = release.load_config(args.config or work / "target.json")
    if args.action == "prepare":
        prepare(work, config, args.revision)
        return
    decision = release.private_json(args.decision, max_bytes=65536)
    if args.action == "check":
        validate(work, config, decision)
        print("Final local authority, retained history, exact CI and private gate verified; no live operations.")
        return
    require(args.approve == args.action, "Explicit final-action approval required")
    if args.action == "ready":
        ready(work, config, decision)
        print("Final readiness saved once: 45-minute publication and 90-minute overall clocks started; no live operations.")
        return
    if args.action in {"configure", "activate"}:
        globals()[args.action](work, config, decision, release.private_json(args.recovery, max_bytes=65536))
    else:
        globals()[args.action](work, config, decision)
    print("Final action confirmed; prior history and consumed attempts retained.")


if __name__ == "__main__":
    main()
