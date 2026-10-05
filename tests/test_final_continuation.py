"""Synthetic-only final rerun helper tests; network access is forbidden."""

import copy
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
    "observe_unknown", "worker_failed", "timeout",
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
            if failure == "observe_unknown":
                raise ValueError("synthetic observation failure")
            if failure == "timeout":
                state.clock += timedelta(seconds=600)
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
    assert len(stops) == (1 if failure == "timeout" else 0)
    if failure == "timeout":
        assert stops[0][stops[0].index("--job-execution-name") + 1] == final.receipt(
            state.work, "worker-result", state.decision)["execution_name"]
    with pytest.raises(ValueError, match="closed"):
        final.activate(state.work, state.config, state.decision, state.recovery)
