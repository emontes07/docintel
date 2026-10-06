"""Synthetic operator-path tests. No hosted reconciliation, network or worker."""

import base64
import copy
from datetime import timedelta
import hashlib
import json
import shutil
import socket

import pytest

from scripts import row_continuation as row
from tests.test_final_continuation import final_state  # noqa: F401
from tests.test_final_rerun import final_case  # noqa: F401
from tests.test_real_batch_worker import configured, recovery_case  # noqa: F401
from tests.test_release import pilot_release, release  # noqa: F401
from tests.test_release_continuation import continued, continuation  # noqa: F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args):
        pytest.fail("Row helper tests must not contact any service")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def row_state(final_state, monkeypatch):
    state = final_state
    monkeypatch.setattr(row, "release", release)
    monkeypatch.setattr(row, "now", lambda: state.clock)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("No network in row helper tests"))
    monkeypatch.setattr(row, "sleep", lambda _: None)
    state.approval["unit_prices_usd"].update(input_token=row.RATES["input"], output_token=row.RATES["output"])
    release.save(state.work / "real-pilot-approval.json", state.approval)
    pin = release.private_json(continuation.ROOT_PIN)
    pin["original_receipts"]["real-pilot-approval.json"] = row.raw_sha(state.work / "real-pilot-approval.json")
    release.save(continuation.ROOT_PIN, pin)
    first_binding = release.private_json(state.work / "pilot-binding.json")
    first_binding["approval_sha256"] = release.fingerprint(state.approval)
    release.save(state.work / "pilot-binding.json", first_binding)
    for name in ("pilot-execution-attempt-1", "pilot-execution-result-1"):
        value = release.private_json(state.work / (name + ".json"))
        value["binding"] = first_binding
        release.save(state.work / (name + ".json"), value)
    value = release.private_json(state.work / "pilot-disabled.json")
    value["approval_sha256"] = first_binding["approval_sha256"]
    release.save(state.work / "pilot-disabled.json", value)
    old_images = {"revision": state.decision["source_revision"],
                  "backend": "synthetic.azurecr.io/docintel/backend@sha256:" + "7" * 64,
                  "frontend": "synthetic.azurecr.io/docintel/frontend@sha256:" + "8" * 64}
    release.save(state.final_child / "images.json", old_images)
    for name, extra in (("worker-attempt", {"attempt": 3}), ("worker-result", {"attempt": 3, "execution_name": "third"}),
                        ("closed", {})):
        release.save(state.work / ("final-" + name + ".json"),
                     {**row.binding(state.decision), **extra})
    previous_amendment = copy.deepcopy(state.recovery)
    previous_start = state.clock - timedelta(days=1)
    previous_amendment.update(
        readiness_at=previous_start.isoformat(), not_before=previous_start.isoformat(),
        operating_expires_at=(previous_start + timedelta(seconds=5400)).isoformat(),
        expires_at=(previous_start + timedelta(seconds=1200)).isoformat(),
    )
    release.save(state.work / "final-configure-attempt.json", {
        **row.binding(state.decision), "recovery": previous_amendment, "recovery_sha256": row.sha(previous_amendment),
    })
    for kind in ("backend", "frontend"):
        state.resources[kind]["properties"]["template"]["containers"][0]["image"] = old_images[kind]
    state.job["properties"]["template"]["containers"][0]["image"] = old_images["backend"]
    state.revision = "b" * 40
    state.changed = {"backend/real_pilot.py", "scripts/row_continuation.py", "tests/test_row_continuation.py"}
    state.git_calls = []

    def command(args, **kwargs):
        state.git_calls.append(args)
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return b""
        if args[:3] == ["git", "diff", "--name-only"]:
            return "\n".join(sorted(state.changed)).encode()
        if args[:2] == ["git", "rev-parse"]:
            return state.revision.encode()
        if args[:2] == ["git", "show"]:
            return (release.ROOT / args[2].split(":", 1)[1]).read_bytes()
        pytest.fail("Unexpected command: " + repr(args))

    monkeypatch.setattr(release, "command", command)
    child = state.work / row.CHILD
    state.row_child = child

    def stage(revision, work):
        assert work == child and revision == state.revision
        shutil.copytree(state.final_child / "source", work / "source")
        shutil.copytree(state.final_child / "context", work / "context")
        for name in ("scripts/row_continuation.py", "scripts/release.py", "scripts/pilot_continuation.py",
                     "scripts/final_continuation.py", "tests/test_row_continuation.py"):
            for tree in ("source", "context"):
                target = work / tree / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((release.ROOT / name).read_bytes())
        files = {str(value.relative_to(work / "source")): row.raw_sha(value)
                 for value in (work / "source").rglob("*") if value.is_file()}
        modes = {str(value.relative_to(work / "context")): value.stat().st_mode & 0o777
                 for value in (work / "context").rglob("*")}
        release.save(work / "source.json", {"revision": revision, "files": files})
        release.save(work / "context.json", files)
        release.save(work / "context-modes.json", modes)
        for name in ("clean-checks.json", "backend-smoke.json", "frontend-smoke.json", "ci.json"):
            value = release.private_json(state.final_child / name)
            value["revision"] = revision
            if "context_sha256" in value:
                value["context_sha256"], value["modes_sha256"] = release.fingerprint(files), release.fingerprint(modes)
            release.save(work / name, value)

    monkeypatch.setattr(release, "stage", stage)
    monkeypatch.setattr(release, "build_context", lambda work: None)
    row.prepare(state.work, state.config, state.revision)
    state.baseline = release.private_json(row.path(state.work, "baseline"))
    scope = {
        **{key: value for key, value in state.recovery.items()
           if key not in row.TIME_FIELDS | {"prior_recovery_sha256", "result_sha256"}},
        "approval_sha256": row.sha(state.approval), "prior_execution_ids": ["1" * 64, "2" * 64, "3" * 64],
        "prior_final_rerun_sha256": row.sha(previous_amendment), "prior_final_audit_sha256": "9" * 64,
        "additional_input_tokens": 57868, "effective_input_ceiling": 257868,
        "max_requests": 4, "max_output_tokens": 8192,
        "result_sha256": {f"results/{state.approval['batch_id']}/row-2.json": "a" * 64,
                          f"results/{state.approval['batch_id']}/row-3/attempts/" + "8" * 64 + ".json": None},
    }
    state.snapshot = {
        "verified": True, "temporary_processing_closed": True, "active_executions": 0,
        "consumption": copy.deepcopy(row.CONSUMED), "ledger_sha256": scope["ledger_sha256"],
        "prior_final_audit_sha256": "9" * 64,
    }
    release.save(row.path(state.work, "postclosure"), state.snapshot)
    batch = state.approval["batch_id"]
    objects = {key: "a" * 64 for key in (
        "configuration/real-pilot-approval.json", "budgets/real-pilot.json",
        "operations/real-pilot-final-rerun.json", f"batches/{batch}.json",
        f"items/{batch}/row-2.json", f"items/{batch}/row-3.json",
        f"operations/interrupted-reconciliation/{batch}/row-3/" + "3" * 64 + ".json",
        f"operations/interrupted-reconciliation/{batch}/row-3/" + "3" * 64 + ".applied.json",
    )}
    objects.update(scope["cached_documents"])
    objects.update({key: value for key, value in scope["result_sha256"].items() if value is not None})
    objects.update({f"items/{batch}/{key}.json": value for key, value in scope["item_sha256"].items()})
    state.reconciled = {
        "verified": True, "status_only": True, "snapshot_sha256": row.raw_sha(row.path(state.work, "postclosure")),
        "consumption": copy.deepcopy(row.CONSUMED), "ledger_sha256": scope["ledger_sha256"],
        "prior_final_audit_sha256": state.snapshot["prior_final_audit_sha256"], "object_sha256": objects,
    }
    release.save(row.path(state.work, "reconciliation"), state.reconciled)
    state.decision = {
        "schema_version": 1, "approved": True, "approved_by": state.approval["approved_by"],
        **{key: state.baseline[key] for key in (
            "baseline_revision", "source_revision", "target", "approval_sha256", "history_sha256")},
        "baseline_sha256": row.raw_sha(row.path(state.work, "baseline")),
        "validation_sha256": row.sha({name: row.raw_sha(child / name) for name in row.VALIDATION_FILES}),
        "gate_file": row.path(state.work, "private-gate").name,
        "scope_sha256": row.sha(scope), "snapshot_file": row.path(state.work, "postclosure").name,
        "snapshot_sha256": row.raw_sha(row.path(state.work, "postclosure")),
        "reconciliation_file": row.path(state.work, "reconciliation").name,
        "reconciliation_sha256": row.raw_sha(row.path(state.work, "reconciliation")),
        "policy": copy.deepcopy(row.POLICY), "window_policy": copy.deepcopy(row.WINDOW_POLICY),
    }
    state.gate = {
        "schema_version": 1, "passed": True, "no_live_operations": True,
        **{key: state.decision[key] for key in (
            "target", "approval_sha256", "source_revision", "history_sha256", "scope_sha256",
            "snapshot_sha256", "reconciliation_sha256")},
        "checks": {key: True for key in row.GATE_CHECKS}, "amendment_scope": scope,
        "remote_object_sha256": objects, "full_fallthrough_input_tokens": 92146, "planned_input_tokens": 89596,
        "full_output_tokens": 8192, "maximum_direct_microdollars": 726132, "verified_rates": row.RATES,
        "incidentals_disclosure_only": True, "historical_forecasts_stacked": False,
    }
    release.save(row.path(state.work, "private-gate"), state.gate)
    state.decision["gate_sha256"] = row.raw_sha(row.path(state.work, "private-gate"))
    state.old_bytes = {name: (state.work / name).read_bytes() for name in state.baseline["history"]}
    state.calls.clear()
    state.console_calls.clear()
    state.model_metadata_calls.clear()
    state.published.clear()
    state.executions = []
    original_azure = release.azure

    def azure(*args, **kwargs):
        if args[:3] == ("containerapp", "job", "start"):
            state.calls.append(args)
            return {"name": "fourth-worker"}
        if args[:4] == ("containerapp", "job", "execution", "list"):
            assert not row.path(state.work, "closed").exists()
            if not state.executions:
                status, seconds = "Succeeded", 10
            else:
                status, seconds = state.executions.pop(0)
            started = row.instant(row.receipt(state.work, "worker-attempt", state.decision)["attempted_at"])
            state.clock = started + timedelta(seconds=seconds)
            if isinstance(status, Exception):
                raise status
            return [{"name": "fourth-worker", "properties": {
                "status": status, "startTime": started.isoformat(),
                "endTime": state.clock.isoformat() if status in release.TERMINAL else None,
            }}]
        if args[:3] == ("containerapp", "job", "stop"):
            state.calls.append(args)
            assert state.clock >= row.instant(row.amendment(state.work, state.decision)["expires_at"])
            return {}
        return original_azure(*args, **kwargs)

    def execute(config, work, kind, revision, attempt, **kwargs):
        assert work == state.row_child and revision == state.revision
        assert release.private_json(attempt)["kind"] == kind
        assert kwargs == {"expires": row.instant(row.readiness(state.work, state.decision)["publication_expires_at"]),
                          "validate_upload_window": True}
        assert state.published == ([] if kind == "backend" else ["backend"])
        state.published.append(kind)
        started = state.clock
        state.clock += timedelta(seconds=10)
        return {"digest": "sha256:" + ("b" if kind == "backend" else "c") * 64,
                "runId": "fourth-" + kind, "status": "Succeeded", "agentConfiguration": {"cpu": 2},
                "startTime": started.isoformat(), "finishTime": state.clock.isoformat()}

    def console(args, code, marker, **kwargs):
        state.console_calls.append(code)
        return (marker + "\n").encode()

    state.actual_usage = {"execution_id": "e" * 64, "reservations": {
        str(index): {"operation": "inference", "actual_usage": {
            "input_tokens": 1000, "output_tokens": 100, "analysis_pages": 0}} for index in range(4)}}
    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(release, "execute_publication", execute)
    monkeypatch.setattr(release, "console_code", console)
    monkeypatch.setattr(row, "usage", lambda *args: copy.deepcopy(state.actual_usage))
    return state


def deployed(state):
    row.ready(state.work, state.config, state.decision)
    row.publish(state.work, state.config, state.decision)
    row.deploy(state.work, state.config, state.decision)


def reseal_gate(state):
    state.decision["scope_sha256"] = row.sha(state.gate["amendment_scope"])
    state.gate["scope_sha256"] = state.decision["scope_sha256"]
    release.save(row.path(state.work, "private-gate"), state.gate)
    state.decision["gate_sha256"] = row.raw_sha(row.path(state.work, "private-gate"))


def test_offline_checks_never_read_clock_or_create_authority(row_state, monkeypatch):
    state = row_state
    monkeypatch.setattr(row, "now", lambda: pytest.fail("Offline gate must not read a live clock"))
    row.validate(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls and not state.model_metadata_calls
    assert not row.path(state.work, "readiness").exists()
    assert all((state.work / name).read_bytes() == content for name, content in state.old_bytes.items())
    assert row.capacity(state.approval) == {"input_tokens": 92146, "output_tokens": 18432, "inference_requests": 9}


def test_cli_check_is_executable_and_ready_requires_exact_action_approval(row_state, monkeypatch, capsys):
    state = row_state
    monkeypatch.setattr(release, "load_config", lambda _: state.config)
    monkeypatch.setattr(row.os, "umask", lambda _: None)
    decision_path = row.path(state.work, "decision-approved")
    release.save(decision_path, state.decision)
    monkeypatch.setattr(row.sys, "argv", ["row_continuation.py", "check", "--work", str(state.work),
                                         "--decision", str(decision_path)])
    row.main()
    assert "Row action confirmed" in capsys.readouterr().out
    monkeypatch.setattr(row.sys, "argv", ["row_continuation.py", "ready", "--work", str(state.work),
                                         "--decision", str(decision_path), "--approve", "publish"])
    with pytest.raises(ValueError, match="Explicit row-action"):
        row.main()
    assert not state.calls and not state.console_calls and not row.path(state.work, "readiness").exists()


@pytest.mark.parametrize("defect", [
    "ci", "gate", "source", "baseline", "history", "authority", "reconciliation", "snapshot",
    "scope", "tokens", "cost", "incidental", "rates", "application", "future_revision",
])
def test_failed_prerequisite_never_starts_clock_or_contacts_cloud(row_state, monkeypatch, defect):
    state = row_state
    if defect == "ci":
        value = release.private_json(state.row_child / "ci.json")
        value["checks"]["Validate Backend"] = "failure"
        release.save(state.row_child / "ci.json", value)
        state.decision["validation_sha256"] = row.sha({
            name: row.raw_sha(state.row_child / name) for name in row.VALIDATION_FILES})
    elif defect in {"gate", "tokens", "cost", "incidental", "rates", "scope"}:
        if defect == "gate":
            state.gate["passed"] = False
        elif defect == "tokens":
            state.gate["full_fallthrough_input_tokens"] = 92147
        elif defect == "cost":
            state.gate["maximum_direct_microdollars"] = 726131
        elif defect == "incidental":
            state.gate["historical_forecasts_stacked"] = True
        elif defect == "rates":
            state.gate["verified_rates"] = {**row.RATES, "input": "0.000001"}
        else:
            state.gate["amendment_scope"]["prior_execution_ids"] = ["1" * 64, "2" * 64]
        reseal_gate(state)
    elif defect == "source":
        (state.row_child / "source/backend/Dockerfile").write_text("changed")
    elif defect == "baseline":
        state.decision["baseline_revision"] = "f" * 40
    elif defect == "history":
        (state.work / "final-worker-result.json").write_text("{}")
    elif defect == "authority":
        state.decision["policy"]["additional_input_tokens"] += 1
    elif defect in {"reconciliation", "snapshot"}:
        row.path(state.work, "reconciliation" if defect == "reconciliation" else "postclosure").unlink()
    elif defect == "application":
        state.changed.add("backend/extract.py")
    else:
        state.decision["source_revision"] = "f" * 40
    monkeypatch.setattr(row, "now", lambda: pytest.fail("Prerequisites must precede readiness clock"))
    with pytest.raises((ValueError, KeyError)):
        row.ready(state.work, state.config, state.decision)
    assert not row.path(state.work, "readiness").exists()
    assert not state.calls and not state.console_calls and not state.model_metadata_calls


def test_readiness_is_once_and_exact_45_90_minutes(row_state):
    state = row_state
    value = row.ready(state.work, state.config, state.decision)
    anchor = row.instant(value["readiness_at"])
    assert row.instant(value["publication_expires_at"]) - anchor == timedelta(seconds=2700)
    assert row.instant(value["overall_expires_at"]) - anchor == timedelta(seconds=5400)
    before = row.path(state.work, "readiness").read_bytes()
    state.clock += timedelta(days=1)
    with pytest.raises(ValueError, match="single-use"):
        row.ready(state.work, state.config, state.decision)
    assert row.path(state.work, "readiness").read_bytes() == before
    assert not state.calls and not state.console_calls


@pytest.mark.parametrize("seconds_before", [1, 0])
def test_readiness_waits_for_previous_final_operating_expiry(row_state, seconds_before):
    state = row_state
    previous = release.private_json(state.work / "final-configure-attempt.json")["recovery"]
    state.clock = row.instant(previous["operating_expires_at"]) - timedelta(seconds=seconds_before)
    if seconds_before:
        with pytest.raises(ValueError, match="prior final operating window"):
            row.ready(state.work, state.config, state.decision)
        assert not row.path(state.work, "readiness").exists()
    else:
        value = row.ready(state.work, state.config, state.decision)
        assert value["readiness_at"] == previous["operating_expires_at"]
    assert not state.calls and not state.console_calls


def test_partial_readiness_cannot_be_resealed_or_renewed(row_state, monkeypatch):
    state = row_state
    original = release.save_once

    def save(candidate, value):
        if candidate == row.path(state.work, "readiness-pin"):
            raise OSError("synthetic pin failure")
        original(candidate, value)

    monkeypatch.setattr(release, "save_once", save)
    with pytest.raises(OSError):
        row.ready(state.work, state.config, state.decision)
    with pytest.raises(ValueError, match="single-use"):
        row.ready(state.work, state.config, state.decision)
    with pytest.raises(ValueError):
        row.preflight(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls


def test_end_to_end_preserves_history_and_uses_only_fourth_attempt(row_state):
    state = row_state
    deployed(state)
    value = row.receipt(state.work, "deployed", state.decision)
    assert row.instant(value["processing_expires_at"]) - row.instant(value["both_ready_at"]) == timedelta(seconds=1200)
    assert state.published == ["backend", "frontend"]
    row.activate(state.work, state.config, state.decision)
    assert row.receipt(state.work, "worker-attempt", state.decision)["attempt"] == 4
    assert row.receipt(state.work, "worker-terminal", state.decision)["status"] == "Succeeded"
    assert row.receipt(state.work, "closed", state.decision)
    assert len([call for call in state.calls if call[:3] == ("containerapp", "job", "start")]) == 1
    assert not [call for call in state.calls if call[:3] == ("containerapp", "job", "stop")]
    release.require_real_pilot_off(state.resources["backend"])
    assert state.job["properties"]["configuration"]["replicaTimeout"] == 600
    assert state.job["properties"]["configuration"]["replicaRetryLimit"] == 0
    assert all((state.work / name).read_bytes() == content for name, content in state.old_bytes.items())
    assert state.resources["frontend"]["properties"]["template"]["containers"][0]["image"].endswith("c" * 64)
    assert state.job["properties"]["template"]["containers"][0]["image"].endswith("b" * 64)
    with pytest.raises(ValueError, match="closed"):
        row.activate(state.work, state.config, state.decision)
    with pytest.raises(ValueError, match="retry"):
        row.publish(state.work, state.config, state.decision)


@pytest.mark.parametrize("failure", ["unknown", "backend_late", "frontend_upload_late"])
def test_publication_single_use_and_full_900_before_every_submit(row_state, monkeypatch, failure):
    state = row_state
    row.ready(state.work, state.config, state.decision)
    original = release.execute_publication

    def execute(*args, **kwargs):
        if failure == "unknown":
            raise ValueError("synthetic publication outcome unknown")
        result = original(*args, **kwargs)
        expires = row.instant(row.readiness(state.work, state.decision)["publication_expires_at"])
        if args[2] == "backend" and failure == "backend_late":
            state.clock = expires - timedelta(seconds=899)
        if args[2] == "frontend" and failure == "frontend_upload_late":
            state.clock = expires - timedelta(seconds=899)
            release.azure("rest", "--method", "POST", "--url", "synthetic.invalid/scheduleRun")
        return result

    monkeypatch.setattr(release, "execute_publication", execute)
    with pytest.raises(ValueError):
        row.publish(state.work, state.config, state.decision)
    assert row.path(state.work, "backend-attempt").exists()
    assert row.path(state.work, "frontend-attempt").exists() is (failure == "frontend_upload_late")
    with pytest.raises(ValueError, match="retry"):
        row.publish(state.work, state.config, state.decision)
    assert not [call for call in state.calls if "scheduleRun" in str(call)]


def test_publication_poll_can_continue_with_less_than_900_after_submission(row_state, monkeypatch):
    state = row_state
    row.ready(state.work, state.config, state.decision)
    original = release.execute_publication

    def execute(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[2] == "frontend":
            expires = row.instant(row.readiness(state.work, state.decision)["publication_expires_at"])
            state.clock = expires - timedelta(seconds=800)
            release.azure("rest", "--method", "GET", "--url", "synthetic.invalid/runs/fourth")
        return result

    azure = release.azure
    monkeypatch.setattr(release, "azure", lambda *args, **kwargs: {} if args[:3] == ("rest", "--method", "GET")
                        else azure(*args, **kwargs))
    monkeypatch.setattr(release, "execute_publication", execute)
    row.publish(state.work, state.config, state.decision)
    assert state.published == ["backend", "frontend"]


def test_processing_window_clips_to_overall_and_requires_full_600(row_state):
    state = row_state
    ready = row.ready(state.work, state.config, state.decision)
    row.publish(state.work, state.config, state.decision)
    state.clock = row.instant(ready["overall_expires_at"]) - timedelta(seconds=750)
    row.deploy(state.work, state.config, state.decision)
    assert row.amendment(state.work, state.decision)["expires_at"] == ready["overall_expires_at"]
    state.clock += timedelta(seconds=151)
    calls = len(state.calls)
    with pytest.raises(ValueError, match="window"):
        row.activate(state.work, state.config, state.decision)
    assert len(state.calls) == calls and not row.path(state.work, "worker-attempt").exists()


@pytest.mark.parametrize("failure", ["configure", "enable", "start_unknown", "worker_failed"])
def test_activation_closes_on_control_failures_without_second_start(row_state, monkeypatch, failure):
    state = row_state
    deployed(state)
    if failure in {"configure", "enable"}:
        def fail(*args):
            raise ValueError("synthetic control failure")
        monkeypatch.setattr(row, failure, fail)
    elif failure == "start_unknown":
        original = release.azure

        def azure(*args, **kwargs):
            if args[:3] == ("containerapp", "job", "start"):
                state.calls.append(args)
                raise ValueError("synthetic unknown start")
            return original(*args, **kwargs)
        monkeypatch.setattr(release, "azure", azure)
    else:
        state.executions = [("Failed", 10)]
    with pytest.raises(ValueError):
        row.activate(state.work, state.config, state.decision)
    assert row.receipt(state.work, "closed", state.decision)
    release.require_real_pilot_off(state.resources["backend"])
    if failure in {"start_unknown", "worker_failed"}:
        assert row.path(state.work, "worker-attempt").exists()
    with pytest.raises(ValueError, match="closed|consumed"):
        row.start(state.work, state.config, state.decision)


@pytest.mark.parametrize("defect", ["marker", "unknown", "overbudget", "malformed", "cost_io", "status"])
def test_telemetry_never_stops_even_after_server_600_seconds(row_state, monkeypatch, defect, capsys):
    state = row_state
    deployed(state)
    state.executions = [("Running", 610), ("Succeeded", 650)]
    if defect == "status":
        state.executions[0] = (ValueError("Remote console did not confirm the expected marker"), 610)
    count = []

    def usage(*args):
        count.append(args)
        value = copy.deepcopy(state.actual_usage)
        if len(count) == 1:
            if defect == "marker":
                raise ValueError("Remote console did not confirm the expected marker")
            if defect == "unknown":
                value["reservations"]["0"]["actual_usage"] = None
            if defect == "overbudget":
                value["reservations"]["0"]["actual_usage"]["input_tokens"] = 3000000
            if defect == "malformed":
                return {"private-data": "never log"}
        return value

    original = release.save_once

    def save(candidate, value):
        if defect == "cost_io" and "-cost-" in candidate.name:
            raise OSError("synthetic-secret-do-not-log")
        original(candidate, value)

    monkeypatch.setattr(row, "usage", usage)
    monkeypatch.setattr(release, "save_once", save)
    row.activate(state.work, state.config, state.decision)
    assert not state.executions
    assert row.receipt(state.work, "worker-terminal", state.decision)["status"] == "Succeeded"
    assert row.receipt(state.work, "closed", state.decision)
    assert not row.path(state.work, "worker-stop-attempt").exists()
    assert not [call for call in state.calls if call[:3] == ("containerapp", "job", "stop")]
    assert list(state.work.glob(row.PREFIX + "-warning-*.json"))
    output = capsys.readouterr().err
    assert "WARNING" in output and "private-data" not in output and "synthetic-secret" not in output
    if defect not in {"cost_io", "status"}:
        latest = release.private_json(sorted(state.work.glob(row.PREFIX + "-cost-*.json"))[-1])
        assert not latest["unknown_components"]
        assert latest["components"]["model"]["microdollars"] == 16000


def test_only_independent_authorization_expiry_stops_reserved_execution(row_state, monkeypatch):
    state = row_state
    deployed(state)
    state.executions = [("Running", 650), ("Running", 1200)]

    def unavailable(*args):
        raise ValueError("Remote console did not confirm the expected marker")

    monkeypatch.setattr(row, "usage", unavailable)
    with pytest.raises(ValueError, match="authorization expired"):
        row.activate(state.work, state.config, state.decision)
    assert not state.executions
    stops = [call for call in state.calls if call[:3] == ("containerapp", "job", "stop")]
    assert len(stops) == 1 and stops[0][stops[0].index("--job-execution-name") + 1] == "fourth-worker"
    assert row.receipt(state.work, "worker-stop-attempt", state.decision)["reason"] == "authorization_window_expired"
    assert row.receipt(state.work, "closed", state.decision)


def test_stop_helper_rejects_worker_timeout_without_authorization_expiry(row_state):
    state = row_state
    deployed(state)
    row.configure(state.work, state.config, state.decision)
    row.enable(state.work, state.config, state.decision)
    row.start(state.work, state.config, state.decision)
    state.clock += timedelta(seconds=601)
    with pytest.raises(ValueError, match="Only independent authorization"):
        row.stop_for_expiry(state.work, state.config, state.decision, "fourth-worker")
    assert not row.path(state.work, "worker-stop-attempt").exists()
    assert not [call for call in state.calls if call[:3] == ("containerapp", "job", "stop")]


def test_warning_receipts_are_append_only_sanitized_and_persistence_nonfatal(row_state, monkeypatch, capsys):
    state = row_state
    row.ready(state.work, state.config, state.decision)
    row.warning(state.work, state.decision, "usage_unavailable")
    files = {candidate: candidate.read_bytes() for candidate in state.work.glob(row.PREFIX + "-warning-*.json")}
    row.warning(state.work, state.decision, "Bearer synthetic-sensitive-data")
    for candidate, raw in files.items():
        assert candidate.read_bytes() == raw and candidate.stat().st_mode & 0o077 == 0
    original = release.save_once

    def save(candidate, value):
        if "-warning-" in candidate.name:
            raise OSError("synthetic-sensitive-data")
        original(candidate, value)

    monkeypatch.setattr(release, "save_once", save)
    row.warning(state.work, state.decision, "usage_unavailable")
    output = capsys.readouterr().err
    assert "synthetic-sensitive-data" not in output and "Durable warning unavailable" in output


def test_remote_candidate_code_uses_new_contract_not_old_amendment(row_state):
    state = row_state
    deployed(state)
    candidate = row.amendment(state.work, state.decision)
    code, marker = row.remote_code(state.approval, candidate, write=True)
    assert "validate_row_rerun" in code and row.ROW_RERUN_KEY in code
    assert "store.lease(BUDGET_KEY)" in code
    assert "validate_final_rerun" not in code and "reconcile_interrupted" not in code
    assert marker.startswith("DOCINTEL_ROW_RERUN_OK:")
    assert len(base64.b64encode(code.encode())) + 80 <= 16384
    compile(code, "<row-remote>", "exec")


@pytest.fixture
def row_backend_case(final_case, monkeypatch):
    from backend import real_pilot
    from backend.batch_store import read_json, write_json
    from tests.test_final_rerun import replace

    store, batch, approval, previous = final_case
    guard = real_pilot.RealPilotGuard(store, batch)
    execution = guard.before_execution("synthetic-consumed-third")
    guard.prepare_recovery(lambda: None)
    for index, count in enumerate((24290, 24289, 24289)):
        item = batch["items"][index // 2]
        key = guard.operation_key(item, tier="inference", source_version=str(index), prompt_version="consumed")
        guard.reserve("inference", key, item_key=item["item_key"], max_input_tokens=count, max_output_tokens=2048)
    results, items = {}, {}
    for index, item in enumerate(batch["items"][:2]):
        key = item["item_key"]
        item_path = f"items/{batch['id']}/{key}.json"
        state = read_json(store, item_path)[0]
        state["state"] = "unresolved" if index == 0 else "interrupted"
        if index == 1:
            audit_key = f"operations/interrupted-reconciliation/{batch['id']}/{key}/{execution}.json"
            state["interruption"] = {"audit_key": audit_key, "execution_id": execution}
        replace(store, item_path, state)
        items[key] = hashlib.sha256(store.read_bytes(item_path)[0]).hexdigest()
        if index == 1:
            write_json(store, audit_key, {"kind": "interrupted_reconciliation_intent",
                                         "replacement": {"item": state}, "synthetic": True})
            write_json(store, audit_key.removesuffix(".json") + ".applied.json", {
                "kind": "interrupted_reconciliation_applied", "execution_id": execution,
                "audit_sha256": row.sha(read_json(store, audit_key)[0]),
                "records": {"item": {"sha256": items[key]}},
            })
        results[state["result_key"]] = None
        original_result = f"results/{batch['id']}/{key}.json"
        results[original_result] = hashlib.sha256(store.read_bytes(original_result)[0]).hexdigest()
    batch["state"] = "interrupted"
    replace(store, f"batches/{batch['id']}.json", batch)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    prior_audit = read_json(store, real_pilot.FINAL_RERUN_AUDIT_KEY)[0]
    ready = row.instant(previous["operating_expires_at"]) + timedelta(seconds=1)
    candidate = {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
        "approval_sha256": row.sha(approval), "batch_sha256": approval["batch_sha256"],
        "ledger_sha256": row.sha(ledger), "prior_final_rerun_sha256": row.sha(previous),
        "prior_final_audit_sha256": row.sha(prior_audit), "prior_execution_ids": list(ledger["executions"]),
        "selected_item_keys": previous["selected_item_keys"], "item_sha256": items, "result_sha256": results,
        "cached_documents": previous["cached_documents"], "additional_input_tokens": 57868,
        "effective_input_ceiling": 257868, "max_requests": 4, "max_output_tokens": 8192,
        "readiness_at": ready.isoformat(), "operating_expires_at": (ready + timedelta(seconds=5400)).isoformat(),
        "not_before": ready.isoformat(), "expires_at": (ready + timedelta(seconds=1200)).isoformat(),
    }
    monkeypatch.setattr(real_pilot, "_now", lambda: ready + timedelta(seconds=2))
    return store, batch, approval, candidate


def test_remote_validator_and_install_execute_against_real_guard_without_consumption_reset(row_backend_case, monkeypatch, capsys):
    from backend import batch_store, real_pilot
    from backend.batch_store import read_json
    from tests.test_final_rerun import all_records

    store, batch, approval, candidate = row_backend_case
    monkeypatch.setattr(batch_store, "configured_store", lambda: store)
    assert row.AMENDMENT_FIELDS == real_pilot.ROW_RERUN_FIELDS == set(candidate)
    original_read = store.read_bytes
    with monkeypatch.context() as check:
        check.setattr(store, "read_bytes", lambda key: (json.dumps(candidate).encode(), None)
                      if key == row.ROW_RERUN_KEY else original_read(key))
        validator = real_pilot.RealPilotGuard.__new__(real_pilot.RealPilotGuard)
        validator.store, validator.batch = store, copy.deepcopy(batch)
        validator._validate_approval(approval, verify_runtime=False)
        validator.verify_recovery(read_json(store, real_pilot.BUDGET_KEY)[0])
    before = all_records(store)
    code, marker = row.remote_code(approval, candidate)
    exec(compile(code, "<row read-only validation>", "exec"), {})
    assert marker in capsys.readouterr().out and all_records(store) == before
    code, marker = row.remote_code(approval, candidate, write=True)
    exec(compile(code, "<row single amendment install>", "exec"), {})
    assert marker in capsys.readouterr().out
    batch_key = f"batches/{batch['id']}.json"
    assert read_json(store, row.ROW_RERUN_KEY)[0] == candidate
    assert read_json(store, batch_key)[0] == {**batch, "state": "queued"}
    for key, value in before.items():
        if key != batch_key:
            assert store.read_bytes(key) == value
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert len(ledger["executions"]) == 3 and ledger["reserved"]["input_tokens"] == 165722
    code, marker = row.remote_code(approval, candidate, configured=True)
    exec(compile(code, "<row configured verification>", "exec"), {})
    assert marker in capsys.readouterr().out
    consumed = all_records(store)
    code, _ = row.remote_code(approval, candidate, write=True)
    with pytest.raises(AssertionError):
        exec(compile(code, "<row repeat install denied>", "exec"), {})
    assert all_records(store) == consumed
