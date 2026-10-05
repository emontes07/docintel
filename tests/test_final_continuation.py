"""Synthetic-only final rerun helper tests; network access is forbidden."""

import copy
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil

import pytest

from scripts import final_continuation as final
from tests.test_final_rerun import final_case  # noqa: F401
from tests.test_real_batch_worker import configured, recovery_case  # noqa: F401
from tests.test_release import pilot_release, release
from tests.test_release_continuation import continued, continuation, fake_publish


@pytest.fixture
def final_state(continued, monkeypatch):
    state = continued
    monkeypatch.setattr(final, "release", release)
    fake_publish(state, monkeypatch)
    state.old_decision = copy.deepcopy(state.decision)
    release.save(state.work / "continuation-decision-approved.json", state.old_decision)
    for name, extra in (
        ("closed", {}), ("worker-attempt", {"attempt": 2}),
        ("worker-result", {"attempt": 2, "execution_name": "consumed-second-worker"}),
    ):
        release.save(state.work / ("continuation-" + name + ".json"),
                     {**continuation.binding(state.old_decision), **extra})
    state.previous_config = continuation.published_config(state.work, state.config, state.old_decision)
    state.resources["backend"]["properties"]["template"]["containers"][0]["image"] = state.previous_config["backend_image"]
    state.job["properties"]["template"]["containers"][0]["image"] = state.previous_config["backend_image"]
    state.clock = datetime(2040, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(final, "now", lambda: state.clock)
    state.final_child = state.work / final.CHILD
    for tree in ("source", "context"):
        shutil.copytree(state.child / tree, state.final_child / tree)
        (state.final_child / tree / "frontend/Dockerfile").write_text("new reviewed synthetic frontend")
    files = {str(path.relative_to(state.final_child / "source")): final.raw_sha(path)
             for path in (state.final_child / "source").rglob("*") if path.is_file()}
    modes = {str(path.relative_to(state.final_child / "context")): path.stat().st_mode & 0o777
             for path in (state.final_child / "context").rglob("*")}
    revision = "a" * 40
    release.save(state.final_child / "source.json", {"revision": revision, "files": files})
    release.save(state.final_child / "context.json", files)
    release.save(state.final_child / "context-modes.json", modes)
    release.save(state.final_child / "clean-checks.json", {
        "revision": revision, "lock_sha256": files["uv.lock"],
        "backend": "clean_locked_install_offline_tests", "frontend": "clean_npm_ci_checks_build",
    })
    release.save(state.final_child / "ci.json", {
        "revision": revision, "merged": True, "base": "main",
        "checks": {"Validate Backend": "success", "Validate Frontend": "success", "Validate Release": "success"},
    })
    for kind in ("backend", "frontend"):
        release.save(state.final_child / f"{kind}-smoke.json", {
            "revision": revision, "passed": True, "network": "none",
            "context_sha256": release.fingerprint(files), "modes_sha256": release.fingerprint(modes),
            **({"startup": "health_200_anonymous_batch_401", "uid": 10001} if kind == "backend"
               else {"running": True, "permission_failure": False, "probe_exit_code": 0}),
        })
    state.recovery = {
        "schema_version": 1, "approved": True, "approved_by": state.approval["approved_by"],
        "approval_sha256": final.sha(state.approval), "batch_sha256": state.approval["batch_sha256"],
        "ledger_sha256": "c" * 64, "prior_recovery_sha256": "d" * 64,
        "prior_execution_ids": ["1" * 64, "2" * 64], "selected_item_keys": ["row-2", "row-3"],
        "item_sha256": {"row-2": "3" * 64, "row-3": "4" * 64},
        "result_sha256": {"row-2": "5" * 64, "row-3": "6" * 64},
        "cached_documents": state.old_decision["selection"]["cached_documents"],
        "readiness_at": state.clock.isoformat(),
        "operating_expires_at": (state.clock + timedelta(seconds=5400)).isoformat(),
        "not_before": state.clock.isoformat(), "expires_at": (state.clock + timedelta(seconds=1200)).isoformat(),
    }
    retained = final.history(state.work)
    state.retained = {name: (state.work / name).read_bytes() for name in retained}
    base = {
        "target": release.fingerprint(state.config), "approval_sha256": final.sha(state.approval),
        "prior_decision_sha256": final.sha(state.old_decision), "history_sha256": final.sha(retained),
        "source_revision": revision,
    }
    release.save(state.work / "final-baseline.json", {
        **base, "history": retained, "work_root": str(state.work),
        "root_pin_sha256": final.raw_sha(continuation.ROOT_PIN),
    })
    state.decision = {
        **base, "schema_version": 1, "approved": True, "approved_by": state.approval["approved_by"],
        "policy": copy.deepcopy(final.POLICY), "scope_sha256": final.scope_sha(state.recovery),
        "window_policy": copy.deepcopy(final.WINDOW_POLICY),
        "validation_sha256": final.sha({name: final.raw_sha(state.final_child / name)
                                        for name in final.VALIDATION_FILES}),
    }
    gate = {key: state.decision[key] for key in (
        "target", "approval_sha256", "history_sha256", "source_revision", "scope_sha256")}
    release.save(state.work / "final-recovery-gate.json", {
        **gate, "schema_version": 1, "passed": True, "no_live_operations": True,
        "window_policy": copy.deepcopy(final.WINDOW_POLICY),
        "checks": {key: True for key in final.GATE_CHECKS},
        "amendment_scope": {key: value for key, value in state.recovery.items()
                            if key not in final.TIME_FIELDS},
    })
    state.decision["gate_sha256"] = final.raw_sha(state.work / "final-recovery-gate.json")
    components = {
        "consumed_component_upper_microdollars": 4000000,
        "backend_build_upper_microdollars": 1000000, "frontend_build_upper_microdollars": 1000000,
        "worker_upper_microdollars": 1000000, "inference_upper_microdollars": 1000000,
    }
    proof = release.private_json(state.work / "continuation-cost-incidentals-verified.json")
    proof.update({key: state.decision[key] for key in ("target", "history_sha256", "source_revision")})
    proof["execution_window_seconds"] = 5400
    release.save(state.work / "final-cost-incidentals-verified.json", proof)
    release.save(state.work / "final-cost-rates-verified.json", {
        **{key: state.decision[key] for key in ("target", "history_sha256", "source_revision")},
        "verified": True, "basis": "Synthetic independently reviewed retained rates",
        "unit_prices_usd": state.approval["unit_prices_usd"], "component_upper_microdollars": components,
    })
    total = sum(components.values()) + 16000
    release.save(state.work / "final-cost-evidence.json", {
        "money_ready": True, "assurance": "conservative_forecast_not_billing_cap",
        "approval_granted": False, "approved_guard_price_basis_retained": True,
        "original_total_microdollars": 10000000, "total_forecast_microdollars": total,
        "remaining_contingency_microdollars": 10000000 - total,
        "verification_receipts": {kind: final.raw_sha(state.work / f"final-cost-{kind}-verified.json")
                                  for kind in ("rates", "incidentals")},
    })
    state.decision["cost"] = {
        **components, "incidental_forecast_microdollars": 16000,
        "evidence_sha256": final.raw_sha(state.work / "final-cost-evidence.json"),
    }
    state.published = []

    def execute(config, work, kind, revision, attempt, **kwargs):
        assert work == state.final_child
        assert release.private_json(attempt)["kind"] == kind
        value = final.readiness(state.work, state.decision)
        assert kwargs == {"expires": final.instant(value["publication_expires_at"]), "validate_upload_window": True}
        if kind == "frontend":
            assert state.published == ["backend"]
            assert (state.work / "final-backend-published.json").exists()
        state.published.append(kind)
        return {"digest": "sha256:" + ("7" if kind == "backend" else "8") * 64,
                "runId": "new-" + kind, "tool_revision": revision, "tool_sha256": "9" * 64}

    underlying = release.azure

    def azure(*args, **kwargs):
        if args[:3] == ("rest", "--method", "PATCH"):
            payload = json.loads(Path(args[args.index("--body") + 1][1:]).read_text())
            url = args[args.index("--url") + 1]
            resource = state.job if "/jobs/" in url else state.resources[
                "frontend" if "/frontend?" in url else "backend"]
            resource["properties"]["template"]["containers"] = payload["properties"]["template"]["containers"]
            state.calls.append(args)
            return {}
        return underlying(*args, **kwargs)

    monkeypatch.setattr(release, "execute_publication", execute)
    monkeypatch.setattr(release, "azure", azure)
    state.calls.clear()
    state.model_metadata_calls.clear()
    state.console_calls.clear()
    return state


@pytest.fixture
def incremental_state(final_state, monkeypatch):
    state = final_state
    state.legacy_financial_bytes = (state.work / "final-cost-evidence.json").read_bytes()
    gate_file = "final-private-gate-afd06d6.json"
    release.save(state.work / gate_file, release.private_json(state.work / "final-recovery-gate.json"))
    authority = {
        "approved": True, "application_revision": state.decision["source_revision"],
        "private_gate_receipt": gate_file, "private_gate_sha256": final.raw_sha(state.work / gate_file),
        "baseline_sha256": final.raw_sha(state.work / "final-baseline.json"),
        "original_ledger_sha256": state.recovery["ledger_sha256"],
    }
    authority_path = state.work / "final-financial-authority-v2.json"
    release.save(authority_path, authority)
    monkeypatch.setattr(final, "FINANCIAL_AUTHORITY_SHA256", final.raw_sha(authority_path))
    no_go = state.work / "final-phase-a-cost-90min.json"
    release.save(no_go, {"decision": "NO_GO", "historical_forecast_is_not_consumption": True})
    rates = {
        "acr_usd_per_vcpu_second": "0.0001", "worker_cpu_usd_per_vcpu_second": "0.000024",
        "worker_memory_usd_per_gib_second": "0.000003",
        "model_input_guard_usd_per_token": state.approval["unit_prices_usd"]["input_token"],
        "model_output_guard_usd_per_token": state.approval["unit_prices_usd"]["output_token"],
    }
    report = {
        "schema_version": 2, "passed": True, "money_ready": True, "no_live_operations": True,
        "accepted_source_context": {"source_revision": state.decision["source_revision"]},
        "evidence_integrity": {"all_checks_passed": True, "source_receipt_raw_sha256": {
            name: final.raw_sha(state.work / name) for name in (
                "real-pilot-approval.json", "continuation-cost-rates-verified.json",
            )
        }},
        "financial_authority": {
            "new_separate_incremental_operating_allowance_microdollars": 5000000,
            "historical_usage_deducted_from_new_allowance": False,
            "incidental_estimates_are_disclosure_only": True, "incidental_maxima_are_not_a_gate": True,
            "prior_cost_report_preserved": {"name": no_go.name, "sha256": final.raw_sha(no_go)},
        },
        "stop_rule": {"threshold_microdollars": 5000000, "comparison": "strictly_greater_than"},
        "verified_retained_price_basis": rates,
        "direct_compute_model_cost": {"maximum_direct_compute_model_microdollars": 378000 + 107146 + 8192},
        "observed_incidentals_disclosure": {"not_a_gate": True, "estimate_usd": "1000000"},
    }
    report_path = state.work / "final-financial-authority-cost-v2.json"
    release.save(report_path, report)
    monkeypatch.setattr(final, "FINANCIAL_EVIDENCE_SHA256", final.raw_sha(report_path))
    state.decision.update(
        schema_version=2, policy=copy.deepcopy(final.INCREMENTAL_POLICY), gate_file=gate_file,
        gate_sha256=authority["private_gate_sha256"], financial_authority_sha256=final.raw_sha(authority_path),
        cost={"mode": "incremental_operating", "limit_microdollars": 5000000,
              "evidence_file": report_path.name, "evidence_sha256": final.raw_sha(report_path)},
    )
    release.save(state.work / "final-helper-validation.json", {
        "schema_version": 1, "decision_sha256": final.sha(state.decision),
        "application_revision": state.decision["source_revision"], "helper_revision": "b" * 40,
        "helper_sha256": final.raw_sha(Path(final.__file__)), "tests_sha256": final.raw_sha(Path(__file__)),
        "checks": {"Validate Backend": "success", "Validate Frontend": "success", "Validate Release": "success"},
    })
    monkeypatch.setattr(final, "require_helper_commit", lambda value: None)
    original_execute = release.execute_publication

    def execute(*args, **kwargs):
        first = state.clock
        result = original_execute(*args, **kwargs)
        state.clock += timedelta(seconds=10)
        return {**result, "status": "Succeeded", "startTime": first.isoformat(),
                "finishTime": state.clock.isoformat(), "agentConfiguration": {"cpu": 2}}

    monkeypatch.setattr(release, "execute_publication", execute)
    state.model_usage = {
        "execution_id": "e" * 64, "invalidated": None,
        "reservations": {str(index): {
            "operation": "inference", "actual_usage": {"input_tokens": 1000, "output_tokens": 100, "analysis_pages": 0},
        } for index in range(4)},
    }
    state.read_model_usage = final.read_model_usage
    monkeypatch.setattr(final, "read_model_usage", lambda *args, **kwargs: copy.deepcopy(state.model_usage))
    return state


def make_ready(state):
    value = final.ready(state.work, state.config, state.decision)
    state.recovery.update(readiness_at=value["readiness_at"],
                          operating_expires_at=value["overall_expires_at"])
    return value


def deployed(state):
    make_ready(state)
    final.publish(state.work, state.config, state.decision)
    final.deploy(state.work, state.config, state.decision)
    value = final.receipt(state.work, "deployed", state.decision)
    state.recovery.update(not_before=value["both_ready_at"], expires_at=value["processing_expires_at"])


def enabled(state):
    deployed(state)
    final.configure(state.work, state.config, state.decision, state.recovery)
    final.enable(state.work, state.config, state.decision)


def worker_polls(state, monkeypatch, statuses=("Running", "Succeeded")):
    original = release.azure
    polls = []

    def azure(*args, **kwargs):
        if args[:4] == ("containerapp", "job", "execution", "list"):
            assert not (state.work / "final-closed.json").exists()
            assert len(polls) < len(statuses), "Observation must finish without retrying the worker"
            status = statuses[len(polls)]
            polls.append(status)
            started = final.instant(final.receipt(state.work, "worker-attempt", state.decision)["attempted_at"])
            state.clock = started + timedelta(seconds=10 * len(polls))
            if isinstance(status, Exception):
                raise status
            execution = final.receipt(state.work, "worker-result", state.decision)["execution_name"]
            return [{"name": execution, "properties": {
                "status": status, "startTime": started.isoformat(),
                "endTime": state.clock.isoformat() if status in release.TERMINAL else None,
            }}]
        if args[:3] == ("containerapp", "job", "stop"):
            pytest.fail("Telemetry must never issue a worker stop")
        return original(*args, **kwargs)

    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(final, "sleep", lambda _: None)
    return polls


def test_offline_contract_never_replenishes_prior_history(final_state):
    state = final_state
    final.validate(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls
    assert not (state.work / "final-readiness.json").exists()
    assert all((state.work / name).read_bytes() == content for name, content in state.retained.items())


def test_offline_check_has_no_clock_or_readiness_dependency(final_state, monkeypatch):
    def forbidden():
        pytest.fail("Phase A must not read the live clock")

    monkeypatch.setattr(final, "now", forbidden)
    final.validate(final_state.work, final_state.config, final_state.decision)
    assert not (final_state.work / "final-readiness.json").exists()


def test_incremental_authority_is_offline_and_preserves_legacy_costs(incremental_state, monkeypatch):
    state = incremental_state
    monkeypatch.setattr(final, "now", lambda: pytest.fail("Offline financial validation must not start a clock"))
    final.validate(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls and not state.model_metadata_calls
    assert (state.work / "final-cost-evidence.json").read_bytes() == state.legacy_financial_bytes
    assert release.private_json(state.work / "final-phase-a-cost-90min.json")["decision"] == "NO_GO"
    assert not (state.work / "final-readiness.json").exists()


@pytest.mark.parametrize("defect", ["authority", "report", "limit", "mode", "path", "symlink", "permissions", "retained"])
def test_incremental_authority_denies_bad_evidence_before_clock(incremental_state, monkeypatch, defect):
    state = incremental_state
    if defect in {"authority", "report"}:
        name = "final-financial-authority-v2.json" if defect == "authority" else "final-financial-authority-cost-v2.json"
        path = state.work / name
        value = release.private_json(path)
        value["changed"] = True
        release.save(path, value)
    elif defect == "limit":
        state.decision["cost"]["limit_microdollars"] = 5000001
    elif defect == "mode":
        state.decision["cost"]["mode"] = "forgive_all_history"
    elif defect == "path":
        state.decision["gate_file"] = "../" + state.decision["gate_file"]
    elif defect == "symlink":
        path = state.work / state.decision["gate_file"]
        path.unlink()
        path.symlink_to(state.work / "final-recovery-gate.json")
    elif defect == "permissions":
        (state.work / "final-financial-authority-cost-v2.json").chmod(0o644)
    else:
        release.save(state.work / "final-phase-a-cost-90min.json", {"rewritten": True})
    monkeypatch.setattr(final, "now", lambda: pytest.fail("Invalid financial evidence must not read clock"))
    with pytest.raises(ValueError):
        make_ready(state)
    assert not (state.work / "final-readiness.json").exists()
    assert not state.calls and not state.console_calls and not state.model_metadata_calls


def test_incremental_readiness_binds_authority_and_helper(incremental_state):
    state = incremental_state
    ready = make_ready(state)
    assert ready["financial_authority_sha256"] == state.decision["financial_authority_sha256"]
    assert ready["helper_sha256"] == final.raw_sha(Path(final.__file__))
    assert ready["helper_revision"] == "b" * 40 != ready["source_revision"]
    assert ready["helper_validation_sha256"] == final.raw_sha(state.work / "final-helper-validation.json")
    assert ready["cost_sha256"] == state.decision["cost"]["evidence_sha256"]
    assert final.instant(ready["overall_expires_at"]) - final.instant(ready["readiness_at"]) == timedelta(seconds=5400)


@pytest.mark.parametrize("defect", ["missing", "source", "ci", "decision", "helper_bytes", "test_bytes"])
def test_incremental_ready_requires_separate_helper_ci_before_clock(incremental_state, monkeypatch, defect):
    state = incremental_state
    path = state.work / "final-helper-validation.json"
    if defect == "missing":
        path.unlink()
    else:
        value = release.private_json(path)
        if defect == "source":
            value["application_revision"] = value["helper_revision"]
        elif defect == "ci":
            value["checks"]["Validate Release"] = "failure"
        elif defect == "decision":
            value["decision_sha256"] = "0" * 64
        else:
            value["helper_sha256" if defect == "helper_bytes" else "tests_sha256"] = "0" * 64
        release.save(path, value)
    monkeypatch.setattr(final, "now", lambda: pytest.fail("Helper CI must precede readiness clock"))
    final.validate(state.work, state.config, state.decision)
    with pytest.raises(ValueError):
        make_ready(state)
    assert not (state.work / "final-readiness.json").exists()
    assert not state.calls and not state.console_calls


@pytest.mark.parametrize("defect", [None, "head", "uncommitted"])
def test_helper_commit_receipt_verifies_git_without_relabeling_app(final_state, monkeypatch, defect):
    value = {
        "helper_revision": "b" * 40, "helper_sha256": final.raw_sha(Path(final.__file__)),
        "tests_sha256": final.raw_sha(Path(__file__)),
    }

    def command(args, **kwargs):
        if args[1] == "rev-parse":
            return (("c" if defect == "head" else "b") * 40).encode()
        assert args[:2] == ["git", "show"]
        name = args[2].split(":", 1)[1]
        return b"uncommitted" if defect == "uncommitted" else (Path(final.__file__).resolve().parents[1] / name).read_bytes()

    monkeypatch.setattr(release, "command", command)
    if defect:
        with pytest.raises(ValueError, match="checkout|Uncommitted"):
            final.require_helper_commit(value)
    else:
        final.require_helper_commit(value)


def test_incremental_authority_cannot_change_after_readiness(incremental_state):
    state = incremental_state
    make_ready(state)
    path = state.work / "final-financial-authority-v2.json"
    value = release.private_json(path)
    value["approved"] = False
    release.save(path, value)
    with pytest.raises(ValueError, match="authority"):
        final.publish(state.work, state.config, state.decision)
    assert not state.published and not state.model_metadata_calls and not state.console_calls
    assert not (state.work / "final-backend-attempt.json").exists()


def test_incremental_meter_counts_only_completed_final_builds(incremental_state):
    state = incremental_state
    make_ready(state)
    final.publish(state.work, state.config, state.decision)
    value, _ = final.latest_cost(state.work, state.decision)
    assert value["measured_known_microdollars"] == 4000
    assert set(value["components"]) == {"backend", "frontend"}
    assert not value["unknown_components"] and value["reservations_charged"] is False
    assert value["historical_and_incidental_costs_included"] is False
    assert (state.work / "final-cost-evidence.json").read_bytes() == state.legacy_financial_bytes


def test_incremental_missing_build_runtime_warns_without_blocking(incremental_state, monkeypatch, capsys):
    state = incremental_state
    make_ready(state)
    original = release.execute_publication

    def execute(*args, **kwargs):
        value = original(*args, **kwargs)
        value.pop("finishTime")
        return value

    monkeypatch.setattr(release, "execute_publication", execute)
    final.publish(state.work, state.config, state.decision)
    value, _ = final.latest_cost(state.work, state.decision)
    assert value["components"]["backend"]["microdollars"] is None
    assert value["unknown_components"] == ["backend", "frontend"]
    assert final.receipt(state.work, "published", state.decision)
    assert not (state.work / "final-cost-stop.json").exists()
    assert "WARNING" in capsys.readouterr().err


@pytest.mark.parametrize("measured,warning", [(5000000, False), (5000001, True)])
def test_incremental_threshold_warns_without_latching_or_blocking(incremental_state, measured, warning, capsys):
    state = incremental_state
    make_ready(state)
    final.record_cost(state.work, state.decision, {"model": {"microdollars": measured, "complete": True}})
    assert ("WARNING" in capsys.readouterr().err) is warning
    assert not (state.work / "final-cost-stop.json").exists()
    final.publish(state.work, state.config, state.decision)
    assert state.published == ["backend", "frontend"]
    assert state.decision["cost"]["limit_microdollars"] == 5000000


@pytest.mark.parametrize("malformed", [False, True])
def test_incremental_acr_poller_is_warning_only(incremental_state, monkeypatch, malformed, capsys):
    state = incremental_state
    make_ready(state)
    run_id = "only-reserved-run"
    calls = []
    properties = {"runId": run_id, "status": "Running", "agentConfiguration": {"cpu": 2},
                  "startTime": (state.clock - timedelta(seconds=26000)).isoformat()}
    if malformed:
        properties["startTime"] = "invalid timestamp"

    def azure(*args, **kwargs):
        calls.append(args)
        return {"properties": properties}

    monkeypatch.setattr(release, "azure", azure)
    attempt = state.work / "final-backend-attempt.json"
    release.save_once(attempt, final.binding(state.decision, state.work))
    release.save_once(state.final_child / "final-backend-queued.json", {
        "runId": run_id, "attempt_sha256": final.raw_sha(attempt),
    })
    url = (f"https://management.azure.com/subscriptions/{state.config['subscription']}/resourceGroups/"
           f"{state.config['group']}/providers/Microsoft.ContainerRegistry/registries/"
           f"{state.config['registry']}/runs/{run_id}?api-version={release.PUBLICATION_API_VERSION}")
    with final.publication_meter(state.work, state.config, state.decision, "backend"):
        assert release.azure("rest", "--method", "GET", "--url", url) == {"properties": properties}
    assert calls == [("rest", "--method", "GET", "--url", url)]
    assert release.azure is azure
    assert not (state.work / "final-backend-stop-attempt.json").exists()
    assert not (state.work / "final-cost-stop.json").exists()
    assert "WARNING" in capsys.readouterr().err


@pytest.mark.parametrize("unknown,overrun", [(False, False), (True, False), (False, True)])
def test_incremental_worker_meter_warns_and_closes_only_at_completion(incremental_state, monkeypatch, unknown, overrun, capsys):
    state = incremental_state
    deployed(state)
    polls = worker_polls(state, monkeypatch)
    if unknown:
        state.model_usage["reservations"]["0"]["actual_usage"] = None
    if overrun:
        state.model_usage["reservations"]["0"]["actual_usage"]["input_tokens"] = 6000000

    final.activate(state.work, state.config, state.decision, state.recovery)
    value, _ = final.latest_cost(state.work, state.decision)
    assert value["components"]["model"]["microdollars"] == (6003400 if overrun else 3300 if unknown else 4400)
    assert value["components"]["worker"]["microdollars"] == 600
    assert value["measured_known_microdollars"] == (6008000 if overrun else 7900 if unknown else 9000)
    assert value["components"]["model"]["unknown_reservations"] == (["0"] if unknown else [])
    assert final.receipt(state.work, "closed", state.decision)
    release.require_real_pilot_off(state.resources["backend"])
    stops = [call for call in state.calls if call[:3] == ("containerapp", "job", "stop")]
    assert not stops and len(polls) == 2
    assert not (state.work / "final-cost-stop.json").exists()
    final.observe(state.work, state.config, state.decision)
    assert len(polls) == 2
    assert "WARNING" in capsys.readouterr().err
    with pytest.raises(ValueError, match="closed"):
        final.start(state.work, state.config, state.decision)


def test_incremental_all_missing_usage_is_null_not_reserved_spend(incremental_state):
    state = incremental_state
    make_ready(state)
    for reservation in state.model_usage["reservations"].values():
        reservation.update(actual_usage=None, reserved_microdollars=999999999)
    value = final.worker_cost(state.work, state.decision, {
        "status": "Succeeded", "startTime": state.clock.isoformat(),
        "endTime": (state.clock + timedelta(seconds=10)).isoformat(),
    }, state.model_usage)
    assert value["components"]["model"]["microdollars"] is None
    assert value["components"]["model"]["unknown_reservations"] == ["0", "1", "2", "3"]
    assert value["measured_known_microdollars"] == 300
    assert value["unknown_components"] == ["model"]
    assert not (state.work / "final-cost-stop.json").exists()


def test_incremental_marker_failure_warns_and_recovers_without_stopping(incremental_state, monkeypatch, capsys):
    state = incremental_state
    deployed(state)
    original = release.azure
    polls = []

    def azure(*args, **kwargs):
        if args[:4] == ("containerapp", "job", "execution", "list"):
            assert not (state.work / "final-closed.json").exists()
            if polls:
                previous, _ = final.latest_cost(state.work, state.decision)
                assert previous["components"]["model"]["microdollars"] is None
                assert previous["components"]["model"]["observation"] == "usage_unavailable"
            polls.append(args)
            attempted = final.receipt(state.work, "worker-attempt", state.decision)
            first = final.instant(attempted["attempted_at"])
            state.clock = first + timedelta(seconds=10 * len(polls))
            execution = final.receipt(state.work, "worker-result", state.decision)["execution_name"]
            return [{"name": execution, "properties": {
                "status": "Running" if len(polls) == 1 else "Succeeded",
                "startTime": first.isoformat(), "endTime": state.clock.isoformat() if len(polls) == 2 else None,
            }}]
        if args[:3] == ("containerapp", "job", "stop"):
            pytest.fail("A usage probe must never stop the worker")
        return original(*args, **kwargs)

    def usage(*args, **kwargs):
        if len(polls) == 1:
            raise ValueError("Remote console did not confirm the expected marker")
        return copy.deepcopy(state.model_usage)

    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(final, "read_model_usage", usage)
    monkeypatch.setattr(final, "sleep", lambda _: None)
    final.activate(state.work, state.config, state.decision, state.recovery)
    value, _ = final.latest_cost(state.work, state.decision)
    assert len(polls) == 2
    assert value["components"]["worker"]["microdollars"] == 600
    assert value["components"]["model"]["microdollars"] == 4400
    assert not value["unknown_components"]
    assert not (state.work / "final-cost-stop.json").exists()
    assert not (state.work / "final-worker-stop-attempt.json").exists()
    assert final.receipt(state.work, "closed", state.decision)
    assert final.receipt(state.work, "worker-terminal", state.decision)["status"] == "Succeeded"
    assert "WARNING" in capsys.readouterr().err


def test_incremental_real_usage_reader_recovers_from_console_marker_failure(incremental_state, monkeypatch):
    state = incremental_state
    deployed(state)
    polls = worker_polls(state, monkeypatch)
    console_calls = []

    def console(args, code, marker, *, timeout=None):
        if not marker.startswith("DOCINTEL_FINAL_USAGE_OK:"):
            return (marker + "\n").encode()
        console_calls.append(args)
        assert 0 < timeout <= 30
        if len(polls) == 1:
            raise ValueError("Remote console did not confirm the expected marker")
        value = {**state.model_usage, "recovery_sha256": final.sha(state.recovery)}
        return ("DOCINTEL_FINAL_USAGE:" + base64.b64encode(json.dumps(value).encode()).decode()
                + "\n" + marker + "\n").encode()

    monkeypatch.setattr(final, "read_model_usage", state.read_model_usage)
    monkeypatch.setattr(release, "console_code", console)
    final.activate(state.work, state.config, state.decision, state.recovery)
    assert len(console_calls) == len(polls) == 2
    value, _ = final.latest_cost(state.work, state.decision)
    assert value["components"]["model"]["microdollars"] == 4400
    assert not value["unknown_components"]
    assert final.receipt(state.work, "closed", state.decision)


@pytest.mark.parametrize("defect", ["payload", "tokens", "runtime", "record", "corrupt_receipt"])
def test_incremental_telemetry_errors_never_escape_into_early_cleanup(incremental_state, monkeypatch, defect, capsys):
    state = incremental_state
    deployed(state)
    polls = worker_polls(state, monkeypatch)
    secret = "synthetic-secret-do-not-log"
    original_compute, original_save = final.worker_compute, release.save_once
    corrupted = []

    def usage(*args, **kwargs):
        value = copy.deepcopy(state.model_usage)
        if len(polls) == 1:
            if defect == "payload":
                return {secret: secret}
            if defect == "tokens":
                value["reservations"]["0"]["actual_usage"]["input_tokens"] = -1
            if defect == "corrupt_receipt":
                path = sorted(state.work.glob("final-direct-cost-*.json"))[-1]
                path.write_text(secret)
                corrupted.append(path)
        return value

    def compute(*args):
        if defect == "runtime" and len(polls) == 1:
            raise ValueError(secret)
        return original_compute(*args)

    def save(path, value):
        if defect == "record" and len(polls) == 1 and path.name.startswith("final-direct-cost-"):
            raise OSError(secret)
        return original_save(path, value)

    monkeypatch.setattr(final, "read_model_usage", usage)
    monkeypatch.setattr(final, "worker_compute", compute)
    monkeypatch.setattr(release, "save_once", save)
    final.activate(state.work, state.config, state.decision, state.recovery)
    assert len(polls) == 2
    assert final.receipt(state.work, "closed", state.decision)
    assert final.receipt(state.work, "worker-terminal", state.decision)["status"] == "Succeeded"
    warnings = list(state.work.glob("final-monitoring-warning-*.json"))
    assert warnings
    assert all(secret not in path.read_text() for path in warnings)
    assert secret not in capsys.readouterr().err
    assert all(path.read_text() == secret for path in corrupted)
    if defect != "corrupt_receipt":
        assert not final.latest_cost(state.work, state.decision)[0]["unknown_components"]


def test_incremental_missing_usage_retains_last_known_then_recovers(incremental_state, monkeypatch):
    state = incremental_state
    deployed(state)
    polls = worker_polls(state, monkeypatch, ("Running", "Running", "Succeeded"))

    def usage(*args, **kwargs):
        if len(polls) == 2:
            raise ValueError("Remote console did not confirm the expected marker")
        if len(polls) == 3:
            previous, _ = final.latest_cost(state.work, state.decision)
            model = previous["components"]["model"]
            assert model["last_known_only"] and not model["complete"]
            assert model["microdollars"] == 4400
        return copy.deepcopy(state.model_usage)

    monkeypatch.setattr(final, "read_model_usage", usage)
    final.activate(state.work, state.config, state.decision, state.recovery)
    model = final.latest_cost(state.work, state.decision)[0]["components"]["model"]
    assert model["complete"] and "last_known_only" not in model
    assert model["microdollars"] == 4400 and len(polls) == 3


def test_incremental_legacy_cost_stop_is_retained_but_never_controls_worker(incremental_state, monkeypatch):
    state = incremental_state
    deployed(state)
    path = state.work / "final-cost-stop.json"
    release.save_once(path, {**final.binding(state.decision, state.work),
                             "reason": "measured_direct_cost_exceeds_incremental_5_usd",
                             "measured_known_microdollars": 6000000})
    prior_stop = path.read_bytes()
    final.record_cost(state.work, state.decision, {"model": {"microdollars": 6000000, "complete": True}})
    polls = worker_polls(state, monkeypatch)
    final.activate(state.work, state.config, state.decision, state.recovery)
    assert len(polls) == 2 and path.read_bytes() == prior_stop
    assert not (state.work / "final-worker-stop-attempt.json").exists()
    assert all((state.work / name).read_bytes() == content for name, content in state.retained.items())
    assert state.job["properties"]["configuration"]["replicaTimeout"] == 600
    assert state.job["properties"]["configuration"]["replicaRetryLimit"] == 0
    assert state.decision["policy"] == final.INCREMENTAL_POLICY
    assert state.decision["cost"]["limit_microdollars"] == 5000000


@pytest.mark.parametrize("unwritable", [False, True])
def test_monitoring_warnings_are_sanitized_append_only_and_nonfatal(incremental_state, monkeypatch, capsys, unwritable):
    state = incremental_state
    make_ready(state)
    original = release.save_once
    secret = "synthetic-token-and-console-output\nBearer secret"

    def save(path, value):
        if unwritable and path.name.startswith("final-monitoring-warning-"):
            raise OSError(secret)
        original(path, value)

    monkeypatch.setattr(release, "save_once", save)
    final.monitoring_warning(state.work, state.decision, "usage_unavailable", ValueError(secret))
    retained = {path: path.read_bytes() for path in state.work.glob("final-monitoring-warning-*.json")}
    final.monitoring_warning(state.work, state.decision, secret, type(secret, (Exception,), {})(secret))
    assert all(path.read_bytes() == content for path, content in retained.items())
    warnings = list(state.work.glob("final-monitoring-warning-*.json"))
    assert len(warnings) == (0 if unwritable else 2)
    for path in warnings:
        assert path.stat().st_mode & 0o077 == 0
        value = release.private_json(path)
        assert value["observation_only"] and not value["worker_stop_requested"]
        assert value["error_type"] in {"ValueError", "Exception"}
        assert secret not in path.read_text()
    output = capsys.readouterr().err
    assert output.count("WARNING") == 2 and secret not in output
    assert ("Durable warning unavailable" in output) is unwritable
    assert not state.calls and not (state.work / "final-cost-stop.json").exists()


@pytest.mark.parametrize("status_error", [
    ValueError("Remote console did not confirm the expected marker"),
    OSError("synthetic worker-status probe unavailable"),
])
def test_worker_status_probe_recovers_without_early_closure(final_state, monkeypatch, status_error):
    state = final_state
    deployed(state)
    polls = worker_polls(state, monkeypatch, (status_error, "Succeeded"))
    final.activate(state.work, state.config, state.decision, state.recovery)
    assert len(polls) == 2 and final.receipt(state.work, "closed", state.decision)
    assert final.receipt(state.work, "worker-terminal", state.decision)["status"] == "Succeeded"
    assert list(state.work.glob("final-monitoring-warning-*.json"))


@pytest.mark.parametrize("observation", ["unavailable", "overbudget"])
def test_incremental_monitoring_never_extends_authorization_window(incremental_state, monkeypatch, observation):
    state = incremental_state
    deployed(state)
    original = release.azure
    polls = []

    def azure(*args, **kwargs):
        if args[:4] == ("containerapp", "job", "execution", "list"):
            assert not (state.work / "final-closed.json").exists()
            started = final.instant(final.receipt(state.work, "worker-attempt", state.decision)["attempted_at"])
            state.clock = started + timedelta(seconds=10) if not polls else final.instant(state.recovery["expires_at"])
            polls.append(args)
            execution = final.receipt(state.work, "worker-result", state.decision)["execution_name"]
            return [{"name": execution, "properties": {"status": "Running", "startTime": started.isoformat()}}]
        if args[:3] == ("containerapp", "job", "stop"):
            assert state.clock >= final.instant(state.recovery["expires_at"])
            state.calls.append(args)
            return {}
        return original(*args, **kwargs)

    def usage(*args, **kwargs):
        if observation == "unavailable":
            raise ValueError("Remote console did not confirm the expected marker")
        value = copy.deepcopy(state.model_usage)
        value["reservations"]["0"]["actual_usage"]["input_tokens"] = 6000000
        return value

    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(final, "read_model_usage", usage)
    monkeypatch.setattr(final, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="observation expired"):
        final.activate(state.work, state.config, state.decision, state.recovery)
    assert len(polls) == 2
    stops = [args for args in state.calls if args[:3] == ("containerapp", "job", "stop")]
    assert len(stops) == 1
    assert stops[0][stops[0].index("--job-execution-name") + 1] == final.receipt(
        state.work, "worker-result", state.decision)["execution_name"]
    assert final.receipt(state.work, "worker-stop-attempt", state.decision)["reason"] == "authorization_window_expired"
    assert final.receipt(state.work, "closed", state.decision)
    assert not (state.work / "final-cost-stop.json").exists()


def test_observer_has_no_client_side_worker_timeout(incremental_state, monkeypatch):
    state = incremental_state
    deployed(state)
    original = release.azure
    polls = []

    def azure(*args, **kwargs):
        if args[:4] == ("containerapp", "job", "execution", "list"):
            started = final.instant(final.receipt(state.work, "worker-attempt", state.decision)["attempted_at"])
            state.clock = started + timedelta(seconds=601 + len(polls))
            assert state.clock < final.instant(state.recovery["expires_at"])
            polls.append(args)
            execution = final.receipt(state.work, "worker-result", state.decision)["execution_name"]
            return [{"name": execution, "properties": {
                "status": "Running" if len(polls) == 1 else "Failed", "startTime": started.isoformat(),
            }}]
        if args[:3] == ("containerapp", "job", "stop"):
            pytest.fail("Only the server may enforce the worker's 600-second timeout")
        return original(*args, **kwargs)

    def unavailable(*args, **kwargs):
        raise ValueError("Remote console did not confirm the expected marker")

    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(final, "read_model_usage", unavailable)
    monkeypatch.setattr(final, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="terminated unsuccessfully"):
        final.activate(state.work, state.config, state.decision, state.recovery)
    assert len(polls) == 2
    assert final.receipt(state.work, "worker-terminal", state.decision)["status"] == "Failed"
    assert final.receipt(state.work, "closed", state.decision)
    assert state.job["properties"]["configuration"]["replicaTimeout"] == 600
    assert not (state.work / "final-worker-stop-attempt.json").exists()


def test_readiness_clock_starts_after_all_offline_preparation(final_state, monkeypatch):
    state = final_state
    began_preparation = state.clock
    original = final.validate

    def validate(*args):
        state.clock += timedelta(days=3, hours=4)
        return original(*args)

    monkeypatch.setattr(final, "validate", validate)
    value = make_ready(state)
    anchor = final.instant(value["readiness_at"])
    assert anchor == state.clock > began_preparation
    assert final.instant(value["publication_expires_at"]) - anchor == timedelta(seconds=2700)
    assert final.instant(value["overall_expires_at"]) - anchor == timedelta(seconds=5400)
    assert value["remaining_capacity"] == {
        "inference_requests": 12, "input_tokens": 107146, "output_tokens": 24576,
    }
    assert final.readiness(state.work, state.decision) == value
    assert not state.calls and not state.model_metadata_calls and not state.console_calls
    assert not list(state.work.glob("final-*-attempt.json"))


def test_readiness_never_renews_even_after_deadline(final_state):
    state = final_state
    value = make_ready(state)
    before = (state.work / "final-readiness.json").read_bytes()
    state.clock = final.instant(value["overall_expires_at"]) + timedelta(days=1)
    with pytest.raises(ValueError, match="never renew"):
        make_ready(state)
    assert (state.work / "final-readiness.json").read_bytes() == before
    final.validate(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls


def test_incomplete_readiness_pin_fails_closed_without_renewal(final_state, monkeypatch):
    state = final_state
    original = release.save_once

    def save(path, value):
        if path.name == "final-readiness-pin.json":
            raise OSError("synthetic interrupted readiness sealing")
        original(path, value)

    monkeypatch.setattr(release, "save_once", save)
    with pytest.raises(OSError, match="interrupted"):
        make_ready(state)
    assert (state.work / "final-readiness.json").exists()
    with pytest.raises(ValueError, match="never renew"):
        make_ready(state)
    with pytest.raises(ValueError, match="owner-only"):
        final.preflight(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls


@pytest.mark.parametrize("defect", ["gate", "gate_policy", "cost", "ci", "merged", "source"])
def test_failed_phase_a_never_creates_readiness_or_calls_cloud(final_state, defect):
    state = final_state
    if defect in {"gate", "gate_policy"}:
        path = state.work / "final-recovery-gate.json"
        value = release.private_json(path)
        if defect == "gate":
            value["checks"]["append_only_history"] = False
        else:
            value["window_policy"]["operating_seconds"] = 4500
        release.save(path, value)
        state.decision["gate_sha256"] = final.raw_sha(path)
    elif defect == "cost":
        state.decision["cost"]["consumed_component_upper_microdollars"] = 1
    else:
        path = state.final_child / "ci.json"
        value = release.private_json(path)
        if defect == "ci":
            value["checks"]["Validate Frontend"] = "failure"
        elif defect == "merged":
            value["merged"] = False
        else:
            value["revision"] = "b" * 40
        release.save(path, value)
        state.decision["validation_sha256"] = final.sha({
            name: final.raw_sha(state.final_child / name) for name in final.VALIDATION_FILES
        })
    with pytest.raises(ValueError):
        make_ready(state)
    assert not (state.work / "final-readiness.json").exists()
    assert not (state.work / "final-readiness-pin.json").exists()
    assert not state.calls and not state.model_metadata_calls and not state.console_calls


@pytest.mark.parametrize("action", ["preflight", "publish", "deploy", "configure", "enable", "start", "observe", "close", "activate"])
def test_no_live_action_without_readiness(final_state, action):
    state = final_state
    args = (state.work, state.config, state.decision)
    if action in {"configure", "activate"}:
        args += (state.recovery,)
    with pytest.raises((ValueError, FileNotFoundError)):
        getattr(final, action)(*args)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls and not state.published
    assert not list(state.work.glob("final-*-attempt.json"))


@pytest.mark.parametrize("field", [
    "readiness_at", "publication_expires_at", "overall_expires_at", "source_revision",
    "decision_sha256", "gate_sha256", "cost_sha256",
])
def test_readiness_bytes_are_pinned_before_any_live_action(final_state, field):
    state = final_state
    value = make_ready(state)
    value[field] = "changed"
    release.save(state.work / "final-readiness.json", value)
    with pytest.raises(ValueError, match="immutable"):
        final.preflight(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls


@pytest.mark.parametrize("evidence", ["final-recovery-gate.json", "final-cost-evidence.json"])
def test_readiness_rejects_rebound_evidence_without_renewal(final_state, evidence):
    state = final_state
    make_ready(state)
    path = state.work / evidence
    value = release.private_json(path)
    value["changed"] = True
    release.save(path, value)
    if evidence == "final-recovery-gate.json":
        state.decision["gate_sha256"] = final.raw_sha(path)
    else:
        state.decision["cost"]["evidence_sha256"] = final.raw_sha(path)
    with pytest.raises(ValueError, match="immutable"):
        final.preflight(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls


@pytest.mark.parametrize("field", ["publication_expires_at", "overall_expires_at"])
def test_even_rebound_readiness_requires_exact_duration(final_state, field):
    state = final_state
    value = make_ready(state)
    value[field] = (final.instant(value[field]) + timedelta(seconds=1)).isoformat()
    path = state.work / "final-readiness.json"
    release.save(path, value)
    release.save(state.work / "final-readiness-pin.json", {
        **final.binding(state.decision), "readiness_sha256": final.raw_sha(path),
    })
    with pytest.raises(ValueError, match="2700/5400"):
        final.preflight(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls


def test_remaining_model_allowance_is_not_counted_twice_as_consumed(final_state):
    state = final_state
    cost = state.decision["cost"]
    # Synthetic retained token prices are one microdollar per input/output token.
    cost["consumed_component_upper_microdollars"] = 3000000 + 92854 + 8192
    cost["inference_upper_microdollars"] = 107146 + 8192
    rates_path = state.work / "final-cost-rates-verified.json"
    rates = release.private_json(rates_path)
    rates["component_upper_microdollars"] = {
        key: value for key, value in cost.items()
        if key not in {"incidental_forecast_microdollars", "evidence_sha256"}
    }
    release.save(rates_path, rates)
    evidence_path = state.work / "final-cost-evidence.json"
    evidence = release.private_json(evidence_path)
    evidence["verification_receipts"]["rates"] = final.raw_sha(rates_path)
    total = sum(value for key, value in cost.items() if key != "evidence_sha256")
    evidence.update(total_forecast_microdollars=total, remaining_contingency_microdollars=10000000 - total)
    release.save(evidence_path, evidence)
    cost["evidence_sha256"] = final.raw_sha(evidence_path)
    final.validate(state.work, state.config, state.decision)
    assert cost["consumed_component_upper_microdollars"] + cost["inference_upper_microdollars"] == 3000000 + 200000 + 16384


@pytest.mark.parametrize("defect", ["gate", "history", "frontend", "cost", "policy", "window_policy"])
def test_denial_precedes_every_live_operation(final_state, defect):
    state = final_state
    if defect == "gate":
        release.save(state.work / "final-recovery-gate.json", {"passed": False})
    elif defect == "history":
        release.save(state.work / "continuation-worker-attempt.json", {"attempt": 1})
    elif defect == "frontend":
        (state.final_child / "context/frontend/Dockerfile").write_text("untested")
    elif defect == "cost":
        state.decision["cost"]["consumed_component_upper_microdollars"] = 1
    elif defect == "policy":
        state.decision["policy"]["worker_executions"] = 2
    else:
        state.decision["window_policy"]["publication_seconds"] = 5400
    with pytest.raises((ValueError, KeyError)):
        final.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls
    assert not state.published


def test_one_attempt_per_component_and_both_exact_images(final_state):
    state = final_state
    make_ready(state)
    final.publish(state.work, state.config, state.decision)
    assert state.published == ["backend", "frontend"]
    for kind in ("backend", "frontend"):
        attempt = final.receipt(state.work, kind + "-attempt", state.decision)
        assert attempt["cpu"] == 2 and attempt["timeout_seconds"] == 900
    current = final.published_config(state.work, state.config, state.decision)
    assert current["backend_image"] != state.previous_config["backend_image"]
    assert current["frontend_image"] != state.previous_config["frontend_image"]
    with pytest.raises(ValueError, match="never retry"):
        final.publish(state.work, state.config, state.decision)
    assert len(state.published) == 2
    assert all((state.work / name).read_bytes() == content for name, content in state.retained.items())


def test_unknown_build_consumes_new_authority_only(final_state, monkeypatch):
    state = final_state
    make_ready(state)
    underlying = release.execute_publication

    def fail(*args, **kwargs):
        if args[2] == "backend":
            raise ValueError("synthetic unknown submission")
        return underlying(*args, **kwargs)

    monkeypatch.setattr(release, "execute_publication", fail)
    with pytest.raises(ValueError, match="unknown"):
        final.publish(state.work, state.config, state.decision)
    with pytest.raises(ValueError, match="never retry"):
        final.publish(state.work, state.config, state.decision)
    assert (state.work / "final-backend-attempt.json").exists()
    assert not (state.work / "final-frontend-attempt.json").exists()
    assert not (state.work / "final-published.json").exists()
    with pytest.raises(ValueError, match="owner-only"):
        final.deploy(state.work, state.config, state.decision)
    assert not (state.work / "final-deploy-attempt.json").exists()
    assert all((state.work / name).read_bytes() == content for name, content in state.retained.items())


def test_publication_fails_without_full_build_time(final_state):
    state = final_state
    value = make_ready(state)
    state.clock = final.instant(value["publication_expires_at"]) - timedelta(seconds=899)
    with pytest.raises(ValueError, match="publication window"):
        final.publish(state.work, state.config, state.decision)
    assert not state.model_metadata_calls and not state.published


def test_backend_finish_with_insufficient_window_leaves_frontend_unused(final_state, monkeypatch):
    state = final_state
    value = make_ready(state)
    original = release.execute_publication

    def execute(*args, **kwargs):
        result = original(*args, **kwargs)
        state.clock = final.instant(value["publication_expires_at"]) - timedelta(seconds=899)
        return result

    monkeypatch.setattr(release, "execute_publication", execute)
    with pytest.raises(ValueError, match="publication window"):
        final.publish(state.work, state.config, state.decision)
    assert state.published == ["backend"]
    assert (state.work / "final-backend-published.json").exists()
    assert not (state.work / "final-frontend-attempt.json").exists()
    assert not (state.work / "final-published.json").exists()
    with pytest.raises(ValueError, match="owner-only"):
        final.deploy(state.work, state.config, state.decision)
    assert not (state.work / "final-deploy-attempt.json").exists()


def test_frontend_cannot_bypass_sequential_backend_success(final_state):
    state = final_state
    make_ready(state)
    with pytest.raises(ValueError, match="owner-only"):
        final.publish_component(state.work, state.config, state.decision, "frontend", {})
    assert not state.published and not (state.work / "final-frontend-attempt.json").exists()


@pytest.mark.parametrize("operation", ["metadata", "upload", "submit"])
def test_publication_rechecks_900_seconds_before_every_boundary(final_state, monkeypatch, operation):
    state = final_state
    value = make_ready(state)
    uploaded = []
    monkeypatch.setattr(release, "upload_publication_context", lambda *args: uploaded.append(args))

    def execute(*args, **kwargs):
        state.clock = final.instant(value["publication_expires_at"]) - timedelta(seconds=899)
        if operation == "upload":
            return release.upload_publication_context({}, state.work / "unused.tar.gz", 10)
        return release.azure("rest", "--method", "POST",
                             "--url", "/listBuildSourceUploadUrl" if operation == "metadata" else "/scheduleRun")

    monkeypatch.setattr(release, "execute_publication", execute)
    with pytest.raises(ValueError, match="900 seconds"):
        final.publish(state.work, state.config, state.decision)
    assert not state.calls and not uploaded and not state.published
    assert not (state.work / "final-frontend-attempt.json").exists()


def test_readiness_changed_during_live_action_stops_next_cloud_boundary(final_state, monkeypatch):
    state = final_state
    value = make_ready(state)

    def execute(*args, **kwargs):
        value["readiness_at"] = (state.clock + timedelta(seconds=1)).isoformat()
        release.save(state.work / "final-readiness.json", value)
        return release.azure("rest", "--method", "POST", "--url", "/listBuildSourceUploadUrl")

    monkeypatch.setattr(release, "execute_publication", execute)
    with pytest.raises(ValueError, match="changed during action"):
        final.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.published


def test_deployment_only_changes_three_existing_images(final_state):
    state = final_state
    originals = {key: continuation.shape(resource) for key, resource in state.resources.items()}
    originals["worker"] = continuation.shape(state.job)
    deployed(state)
    for kind, resource in (*state.resources.items(), ("worker", state.job)):
        actual = continuation.shape(resource)
        actual["template"]["containers"][0]["image"] = originals[kind]["template"]["containers"][0]["image"]
        assert actual == originals[kind]
    window = final.receipt(state.work, "deployed", state.decision)
    assert final.instant(window["processing_expires_at"]) - final.instant(window["both_ready_at"]) == timedelta(seconds=1200)
    assert len(state.calls) == 3
    with pytest.raises(ValueError, match="already attempted"):
        final.deploy(state.work, state.config, state.decision)
    assert len(state.calls) == 3


def test_deploy_checks_overall_window_before_each_patch(final_state, monkeypatch):
    state = final_state
    value = make_ready(state)
    final.publish(state.work, state.config, state.decision)
    original = release.azure

    def azure(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[:3] == ("rest", "--method", "PATCH"):
            state.clock = final.instant(value["overall_expires_at"]) - timedelta(seconds=599)
        return result

    monkeypatch.setattr(release, "azure", azure)
    with pytest.raises(ValueError, match="600 seconds"):
        final.deploy(state.work, state.config, state.decision)
    assert len(state.calls) == 1
    assert not (state.work / "final-deployed.json").exists()


def test_last_worker_is_attempt_three_and_closure_preserves_reads(final_state):
    state = final_state
    enabled(state)
    final.start(state.work, state.config, state.decision)
    attempt = final.receipt(state.work, "worker-attempt", state.decision)
    assert attempt["attempt"] == 3
    template = release.private_json(state.work / "final-worker-template.json")
    container = template["containers"][0]
    assert container["args"][-6:] == ["--concurrency", "1", "--max-batches", "1", "--item-limit", "2"]
    assert not any(entry["name"] in release.PILOT_EXTERNAL_ENVIRONMENT_KEYS | {"WEBIQ_API_KEY"} for entry in container["env"])
    with pytest.raises(ValueError, match="never retry"):
        final.start(state.work, state.config, state.decision)
    value = final.readiness(state.work, state.decision)
    state.clock = final.instant(value["overall_expires_at"]) + timedelta(hours=1)
    state.model_metadata = None
    final.close(state.work, state.config, state.decision)
    release.require_real_pilot_off(state.resources["backend"])
    assert state.resources["backend"]["properties"]["template"]["containers"][0]["image"].endswith("7" * 64)
    assert final.receipt(state.work, "closed", state.decision)
    assert all((state.work / name).read_bytes() == content for name, content in state.retained.items())


def test_unknown_worker_cannot_retry_but_closes(final_state, monkeypatch):
    state = final_state
    enabled(state)
    underlying = release.azure

    def unknown(*args, **kwargs):
        if args[:3] == ("containerapp", "job", "start"):
            state.active = [{"name": "possibly-running"}]
            raise ValueError("synthetic uncertain start")
        return underlying(*args, **kwargs)

    monkeypatch.setattr(release, "azure", unknown)
    with pytest.raises(ValueError, match="uncertain"):
        final.start(state.work, state.config, state.decision)
    with pytest.raises(ValueError, match="never retry"):
        final.start(state.work, state.config, state.decision)
    final.close(state.work, state.config, state.decision)
    release.require_real_pilot_off(state.resources["backend"])
    assert not (state.work / "final-worker-result.json").exists()


@pytest.mark.parametrize("stage", ["before", "after_reservation"])
def test_worker_requires_full_600_seconds_twice(final_state, monkeypatch, stage):
    state = final_state
    enabled(state)
    if stage == "before":
        state.clock = final.instant(state.recovery["expires_at"]) - timedelta(seconds=599)
    else:
        underlying = release.save_once

        def save(path, value):
            underlying(path, value)
            if path.name == "final-worker-attempt.json":
                state.clock = final.instant(state.recovery["expires_at"]) - timedelta(seconds=599)

        monkeypatch.setattr(release, "save_once", save)
    with pytest.raises(ValueError, match="600 seconds"):
        final.start(state.work, state.config, state.decision)
    assert not any(call[:3] == ("containerapp", "job", "start") for call in state.calls)
    assert (state.work / "final-worker-attempt.json").exists() == (stage == "after_reservation")
    final.close(state.work, state.config, state.decision)


@pytest.mark.parametrize("change", ["scope", "before_ready", "after_window", "readiness_at", "operating_expires_at"])
def test_configure_cannot_expand_selected_scope_or_readiness_window(final_state, change):
    state = final_state
    deployed(state)
    state.console_calls.clear()
    if change == "scope":
        state.recovery["selected_item_keys"].append("row-4")
    elif change == "before_ready":
        state.recovery["not_before"] = (state.clock - timedelta(seconds=1)).isoformat()
    elif change == "after_window":
        state.recovery["expires_at"] = (state.clock + timedelta(seconds=1201)).isoformat()
    else:
        state.recovery[change] = (final.instant(state.recovery[change]) + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        final.configure(state.work, state.config, state.decision, state.recovery)
    assert not state.console_calls
    assert not (state.work / "final-configure-attempt.json").exists()


def test_remote_contract_only_writes_fixed_append_only_amendment(final_state):
    state = final_state
    code, marker = final.remote_code(state.approval, state.decision, state.recovery, write=True)
    compile(code, "<synthetic final console>", "exec")
    assert "validate_final_rerun(candidate, approval, batch, ledger, ReadOnly())" in code
    assert code.count("write_json(store,") == 2
    assert "write_json(store, 'configuration/real-pilot-final-rerun.json', candidate)" in code
    assert "write_json(store, batch_key, fresh, version)" in code
    assert code.index("validate_final_rerun(candidate") < code.index('fresh["state"] = "queued"')
    assert "assert existing is None" in code
    assert marker.startswith("DOCINTEL_FINAL_RERUN_OK:")


def test_scope_excludes_only_four_clock_fields(final_state):
    recovery = final_state.recovery
    original = final.scope_sha(recovery)
    for field in final.TIME_FIELDS:
        assert final.scope_sha({**recovery, field: "simulated"}) == original
    assert final.scope_sha({**recovery, "confirmed_at": "not-a-clock-exception"}) != original
    assert final.scope_sha({**recovery, "approved_by": "other"}) != original


def test_remote_validator_executes_real_backend_without_history_writes(final_case, monkeypatch, capsys):
    from backend import batch_store
    from backend.batch_store import read_json
    from backend.real_pilot import BUDGET_KEY

    store, batch, approval, amendment = final_case
    monkeypatch.setattr(batch_store, "configured_store", lambda: store)
    decision = {"scope_sha256": final.scope_sha(amendment)}
    ledger = copy.deepcopy(read_json(store, BUDGET_KEY)[0])
    code, marker = final.remote_code(approval, decision, amendment, configured=True)
    exec(compile(code, "<final read-only backend validator>", "exec"), {})
    assert marker in capsys.readouterr().out
    assert read_json(store, BUDGET_KEY)[0] == ledger
    before = store.read_bytes(final.RECOVERY_KEY)[0]
    code, _ = final.remote_code(approval, decision, amendment, write=True)
    with pytest.raises(AssertionError):
        exec(compile(code, "<final existing-write denial>", "exec"), {})
    assert store.read_bytes(final.RECOVERY_KEY)[0] == before
    assert read_json(store, BUDGET_KEY)[0] == ledger


def test_incremental_usage_snapshot_reads_only_new_actual_reservations(final_case, monkeypatch, capsys):
    from backend import batch_store, real_pilot
    from tests.test_final_rerun import all_records

    store, batch, approval, amendment = final_case
    monkeypatch.setattr(batch_store, "configured_store", lambda: store)
    code, marker = final.usage_remote_code(approval, amendment)

    def capture():
        before = all_records(store)
        exec(compile(code, "<final measured-usage snapshot>", "exec"), {})
        lines = capsys.readouterr().out.splitlines()
        assert marker in lines
        data = next(line.removeprefix("DOCINTEL_FINAL_USAGE:") for line in lines
                    if line.startswith("DOCINTEL_FINAL_USAGE:"))
        assert all_records(store) == before
        return json.loads(base64.b64decode(data, validate=True))

    assert capture()["reservations"] == {}
    guard = real_pilot.RealPilotGuard(store, batch)
    execution = guard.before_execution("synthetic-new-final-worker")
    operation = guard.operation_key(batch["items"][0], tier="inference",
                                    source_version="synthetic-final", prompt_version="current")
    reserved = guard.reserve("inference", operation, item_key=batch["items"][0]["item_key"],
                             max_input_tokens=10, max_output_tokens=20)
    result = capture()
    assert result["execution_id"] == execution
    assert result["reservations"] == {
        reserved["reservation_id"]: {"operation": "inference", "actual_usage": None},
    }
    guard.record_usage(reserved["reservation_id"], input_tokens=7, output_tokens=4)
    assert capture()["reservations"][reserved["reservation_id"]]["actual_usage"] == {
        "input_tokens": 7, "output_tokens": 4, "analysis_pages": 0,
    }


def test_prepublication_snapshot_probe_rejects_consumed_final_scope(final_case, monkeypatch):
    from backend import batch_store
    from backend.batch_store import read_json
    from backend.real_pilot import BUDGET_KEY

    store, _, approval, amendment = final_case
    monkeypatch.setattr(batch_store, "configured_store", lambda: store)
    scope = {key: value for key, value in amendment.items() if key not in final.TIME_FIELDS}
    code, _ = final.preflight_remote_code(approval, scope)
    before = copy.deepcopy(read_json(store, BUDGET_KEY)[0])
    with pytest.raises(ValueError, match="already configured"):
        exec(compile(code, "<final prepublication snapshot>", "exec"), {})
    assert read_json(store, BUDGET_KEY)[0] == before


@pytest.mark.parametrize("initial_state", ["queued", "completed", "deferred"])
def test_actual_console_create_is_append_only_after_read_only_snapshot(final_case, monkeypatch, capsys, initial_state):
    from backend import batch_store
    from tests.test_final_rerun import ReproductionStore, all_records

    store, batch, approval, amendment = final_case
    originals = {key: value for key, value in all_records(store).items() if key != final.RECOVERY_KEY}
    batch_key = f"batches/{batch['id']}.json"
    original_batch = {**json.loads(originals[batch_key][0]), "state": initial_state}
    originals[batch_key] = (json.dumps(original_batch).encode(), originals[batch_key][1])
    fresh = ReproductionStore(copy.deepcopy(originals))
    monkeypatch.setattr(batch_store, "configured_store", lambda: fresh)
    scope = {key: value for key, value in amendment.items() if key not in final.TIME_FIELDS}
    code, marker = final.preflight_remote_code(approval, scope)
    exec(compile(code, "<final unchanged prepublication snapshot>", "exec"), {})
    assert marker in capsys.readouterr().out
    assert all_records(fresh) == originals
    code, marker = final.remote_code(approval, {"scope_sha256": final.sha(scope)}, amendment, write=True)
    exec(compile(code, "<final append-only configuration>", "exec"), {})
    assert marker in capsys.readouterr().out
    after = all_records(fresh)
    assert set(after) == set(originals) | {final.RECOVERY_KEY}
    assert all(after[key] == value for key, value in originals.items() if key != batch_key)
    assert json.loads(after[batch_key][0]) == {**original_batch, "state": "queued"}
    assert json.loads(after[final.RECOVERY_KEY][0]) == amendment


def test_expired_guard_never_queues_or_installs_amendment(final_case, monkeypatch):
    from backend import batch_store, real_pilot
    from tests.test_final_rerun import ReproductionStore, all_records

    store, batch, approval, amendment = final_case
    originals = {key: value for key, value in all_records(store).items() if key != final.RECOVERY_KEY}
    batch_key = f"batches/{batch['id']}.json"
    original_batch = {**json.loads(originals[batch_key][0]), "state": "completed"}
    originals[batch_key] = (json.dumps(original_batch).encode(), originals[batch_key][1])
    fresh = ReproductionStore(copy.deepcopy(originals))
    monkeypatch.setattr(batch_store, "configured_store", lambda: fresh)
    monkeypatch.setattr(real_pilot, "_now", lambda: final.instant(amendment["expires_at"]) + timedelta(seconds=1))
    code, _ = final.remote_code(approval, {"scope_sha256": final.scope_sha(amendment)}, amendment, write=True)
    with pytest.raises(ValueError):
        exec(compile(code, "<final expired queue denial>", "exec"), {})
    assert all_records(fresh) == originals


@pytest.mark.parametrize("failure", [
    None, "configure", "enable_before", "enable_unknown", "start_unknown",
    "observe_window_expired", "worker_failed", "timeout",
])
def test_activation_always_closes_in_finally(final_state, monkeypatch, failure):
    state = final_state
    deployed(state)
    original_azure = release.azure

    def azure(*args, **kwargs):
        if args[:3] == ("containerapp", "job", "start") and failure == "start_unknown":
            state.calls.append(args)
            raise ValueError("synthetic start outcome unknown")
        if args[:4] == ("containerapp", "job", "execution", "list"):
            if failure == "observe_window_expired":
                state.clock = final.instant(state.recovery["expires_at"])
                raise ValueError("synthetic observation failure")
            if failure == "timeout":
                state.clock = final.instant(state.recovery["expires_at"])
            execution = final.receipt(state.work, "worker-result", state.decision)["execution_name"]
            status = "Running" if failure == "timeout" else "Failed" if failure == "worker_failed" else "Succeeded"
            return [{"name": execution, "properties": {"status": status, "startTime": "synthetic", "endTime": "synthetic"}}]
        if args[:3] == ("containerapp", "job", "stop"):
            state.calls.append(args)
            return {}
        return original_azure(*args, **kwargs)

    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(final, "sleep", lambda _: None)
    if failure == "configure":
        state.recovery["ledger_sha256"] = "f" * 64
    elif failure == "enable_before":
        def enable(*args):
            raise ValueError("synthetic enable prerequisite failure")
        monkeypatch.setattr(final, "enable", enable)
    elif failure == "enable_unknown":
        original_patch = final.patch

        def patch(*args, **kwargs):
            original_patch(*args, **kwargs)
            if args[2] == "enable":
                raise ValueError("synthetic enabled patch outcome unknown")

        monkeypatch.setattr(final, "patch", patch)
    if failure:
        with pytest.raises(ValueError):
            final.activate(state.work, state.config, state.decision, state.recovery)
    else:
        final.activate(state.work, state.config, state.decision, state.recovery)
    assert final.receipt(state.work, "closed", state.decision)
    release.require_real_pilot_off(state.resources["backend"])
    assert state.resources["backend"]["properties"]["template"]["containers"][0]["image"].endswith("7" * 64)
    stops = [call for call in state.calls if call[:3] == ("containerapp", "job", "stop")]
    assert len(stops) == (1 if failure in {"timeout", "observe_window_expired"} else 0)
    if stops:
        assert stops[0][stops[0].index("--job-execution-name") + 1] == final.receipt(
            state.work, "worker-result", state.decision)["execution_name"]
    denial = "processing window" if failure in {"timeout", "observe_window_expired"} else "closed"
    with pytest.raises(ValueError, match=denial):
        final.activate(state.work, state.config, state.decision, state.recovery)
