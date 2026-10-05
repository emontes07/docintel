"""No-network continuation tests; all receipts and source data are synthetic."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import socket

import pytest

from scripts import pilot_continuation as continuation
from tests.test_release import internal_release_approval, pilot_release, release
from tests.test_real_pilot import MemoryStore


@pytest.fixture
def continued(pilot_release, monkeypatch):
    state = pilot_release
    original_app, original_executions = release.app, release.active_executions
    monkeypatch.setattr(release, "app", lambda config, name, **kwargs: original_app(config, name))
    monkeypatch.setattr(release, "active_executions", lambda config, **kwargs: original_executions(config))
    monkeypatch.setattr(continuation, "release", release)
    # The existing release suite separately tests Git exclusion enforcement.
    monkeypatch.setattr(release, "private_path", lambda path: Path(path).expanduser().resolve())
    monkeypatch.setattr(continuation, "ROOT_PIN", state.work.parent / "private-continuation-root.json")
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("No network in continuation tests"))
    state.work.chmod(0o700)
    internal_release_approval(state)
    state.approval["environment"]["LLM_ENDPOINT"] = "https://synthetic-model.cognitiveservices.azure.com/"
    state.approval["owner"] = state.config["tenant"] + "/synthetic-owner"
    now = datetime.now(timezone.utc)
    state.approval["not_before"] = (now - timedelta(hours=2)).isoformat()
    state.approval["expires_at"] = (now - timedelta(hours=2) + timedelta(minutes=20)).isoformat()
    release.save(state.work / "real-pilot-approval.json", state.approval)
    release.save(state.work / "target.json", state.config)
    bound = release.bind_pilot(state.config, state.approval, state.work)
    release.save(state.work / "pilot-execution-attempt-1.json", {
        "binding": bound, "attempt": 1, "state": "start_attempted_completion_unknown",
    })
    release.save(state.work / "pilot-execution-result-1.json", {
        "binding": bound, "attempt": 1, "execution_name": "original-execution",
    })
    release.save(state.work / "pilot-enablement.json", {**bound, "baseline": {}, "intended": {}})
    release.save(state.work / "pilot-enabled.json", bound)
    for name in ("pilot-disable-attempt.json", "pilot-disabled.json"):
        release.save(state.work / name, {"target": bound["target"], "approval_sha256": bound["approval_sha256"]})
    for name in ("backend-publication-attempt.json", "backend-publication-replacement-attempt.json",
                 "backend-publication-supplemental-attempt.json", "backend-publication-supplemental-result.json",
                 "frontend-publication-result.json", "failed-pilot-accounting.json"):
        release.save(state.work / name, {"synthetic_historical_receipt": name})
    history = continuation.history(state.work)
    audit_path = state.work / "synthetic-cost-forecast-audit.json"
    release.save(audit_path, {"synthetic_conservative_forecast": True, "actual_billing": None})
    state.component_floors = {
        "consumed_component_upper_microdollars": 300000,
        "build_upper_microdollars": 100000, "worker_upper_microdollars": 10000,
    }
    release.save(continuation.ROOT_PIN, {
        "schema_version": 1, "work_root": str(state.work), "history_sha256": continuation.sha(history),
        "original_receipts": {
            name: continuation.raw_sha(state.work / name)
            for name in ("target.json", "images.json", "real-pilot-approval.json")
        },
        "component_floors_microdollars": state.component_floors,
        "cost_audit": {"name": audit_path.name, "sha256": continuation.raw_sha(audit_path)},
    })
    state.child = state.work / continuation.CHILD
    for tree in ("source", "context"):
        shutil.copytree(state.work / tree, state.child / tree)
        (state.child / tree / "backend/Dockerfile").write_text("new synthetic backend source")
    files = {str(path.relative_to(state.child / "source")): continuation.raw_sha(path)
             for path in (state.child / "source").rglob("*") if path.is_file()}
    modes = {str(path.relative_to(state.child / "context")): path.stat().st_mode & 0o777
             for path in (state.child / "context").rglob("*")}
    release.save(state.child / "source.json", {"revision": "f" * 40, "files": files})
    release.save(state.child / "context.json", files)
    release.save(state.child / "context-modes.json", modes)
    release.save(state.child / "clean-checks.json", {
        "revision": "f" * 40, "backend": "clean_locked_install_offline_tests",
        "frontend": "unchanged_not_rebuilt", "lock_sha256": files["uv.lock"],
    })
    release.save(state.child / "backend-smoke.json", {
        "revision": "f" * 40, "passed": True, "network": "none",
        "startup": "health_200_anonymous_batch_401", "uid": 10001,
        "context_sha256": release.fingerprint(files), "modes_sha256": release.fingerprint(modes),
    })
    release.save(state.work / "continuation-baseline.json", {
        "target": release.fingerprint(state.config), "approval_sha256": continuation.sha(state.approval),
        "history": history, "history_sha256": continuation.sha(history),
        "source_revision": "f" * 40, "context_child": continuation.CHILD,
        "work_root": str(state.work), "root_pin_sha256": continuation.raw_sha(continuation.ROOT_PIN),
    })
    cost_proofs = {}
    for kind, coverage in (("rates", "original_approved_guard_price_ceilings"),
                           ("incidentals", "prior_and_one_hour_execution_incidentals")):
        path = state.work / f"continuation-cost-{kind}-verified.json"
        release.save(path, {
            "verified": True, "coverage": coverage,
            "target": release.fingerprint(state.config), "history_sha256": continuation.sha(history),
            "source_revision": "f" * 40,
            "basis": "Synthetic independently verified evidence; no actual price or account assertion",
            **({"unit_prices_usd": state.approval["unit_prices_usd"],
                "component_upper_microdollars": {name: 1000000 for name in continuation.COMPONENT_FIELDS}}
               if kind == "rates" else {
                   "upper_microdollars": 16000,
                   "execution_window_seconds": 3600,
                   "storage_month_days": 30,
                   "estimate_basis": "conservative_quantities_not_final_billing",
                   "uncertainty": ["Synthetic provider metering and retained lifetime may differ"],
                   "monthly_retention_disclosure": {
                       "period_days": 30, "automatic_deletion": False,
                       "estimated_microdollars": 100000, "basis": "Synthetic 30-day retention estimate",
                   },
                   "envelopes": {
                       name: {"prior_quantity": 1, "new_quantity": 1, "unit": unit,
                              "unit_price_usd": "0.001", "basis": "Synthetic finite quantity proof"}
                       for name, unit in continuation.INCIDENTAL_UNITS.items()
                   },
               }),
        })
        cost_proofs[kind] = continuation.raw_sha(path)
    release.save(state.work / "continuation-cost-evidence.json", {
        "audit_receipt": {"name": audit_path.name, "sha256": continuation.raw_sha(audit_path)},
        "schema_version": 1, "assurance": "conservative_forecast_not_billing_cap",
        "approval_granted": False, "original_total_microdollars": 10000000,
        "total_forecast_microdollars": 4016000, "remaining_contingency_microdollars": 5984000,
        "synthetic_reviewed_bounds": True, "money_ready": True,
        "decision": {"money": "GO"}, "verification_receipts": cost_proofs,
        "approved_guard_price_basis_retained": True,
        "incidental_forecast_microdollars": 16000,
    })
    state.decision = {
        "schema_version": 1, "approved": True, "approved_by": state.approval["approved_by"],
        "target": release.fingerprint(state.config), "approval_sha256": continuation.sha(state.approval),
        "history_sha256": continuation.sha(history), "source_revision": "f" * 40,
        "validation_sha256": continuation.sha({
            name: continuation.raw_sha(state.child / name) for name in continuation.VALIDATION_FILES
        }),
        "policy": copy.deepcopy(continuation.POLICY),
        "selection": {
            "ledger_sha256": "1" * 64, "prior_execution_id": "2" * 64,
            "selected_item_keys": ["row-2", "row-3"],
            "interrupted_sha256": {"row-2": "5" * 64, "row-3": "6" * 64},
            "cached_documents": {"parses/" + "7" * 64 + ".json": "8" * 64},
        },
        "cost": {
            "consumed_component_upper_microdollars": 1000000, "build_upper_microdollars": 1000000,
            "worker_upper_microdollars": 1000000, "inference_upper_microdollars": 1000000,
            "incidental_forecast_microdollars": 16000,
            "evidence_sha256": continuation.raw_sha(state.work / "continuation-cost-evidence.json"),
        },
        "publication_not_before": (now - timedelta(seconds=1)).isoformat(),
        "publication_expires_at": (now + timedelta(seconds=1150)).isoformat(),
    }
    state.recovery = {
        "schema_version": 1, "approved": True, "approved_by": state.approval["approved_by"],
        "approval_sha256": continuation.sha(state.approval), "batch_sha256": state.approval["batch_sha256"],
        **copy.deepcopy(state.decision["selection"]),
        "not_before": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(seconds=1150)).isoformat(),
    }
    state.old = {name: (state.work / name).read_bytes() for name in history}
    state.model_metadata_calls = []
    state.model_metadata = {
        "id": (f"/subscriptions/{state.config['subscription']}/resourceGroups/{state.config['group']}"
               "/providers/Microsoft.CognitiveServices/accounts/synthetic-model/deployments/synthetic"),
        "sku": {"name": "GlobalStandard", "capacity": 30},
        "properties": {
            "provisioningState": "Succeeded",
            "model": {"format": "OpenAI", "name": "gpt-5", "version": "2025-08-07"},
            "rateLimits": [
                {"key": "request", "count": 300, "renewalPeriod": 60},
                {"key": "token", "count": 30000, "renewalPeriod": 60},
            ],
        },
    }
    underlying_azure = release.azure

    def azure(*args, **kwargs):
        if args[:4] == ("cognitiveservices", "account", "deployment", "show"):
            state.model_metadata_calls.append(args)
            assert kwargs == {"timeout": 60}
            return copy.deepcopy(state.model_metadata)
        if args[:3] == ("rest", "--method", "PATCH"):
            payload = json.loads(Path(args[args.index("--body") + 1][1:]).read_text())
            target = state.job if "/jobs/" in args[args.index("--url") + 1] else state.resources["backend"]
            target["properties"]["template"]["containers"] = payload["properties"]["template"]["containers"]
            state.calls.append(args)
            return {}
        return underlying_azure(*args)

    monkeypatch.setattr(release, "azure", azure)
    return state


def fake_publish(state, monkeypatch):
    def execute(config, work, kind, revision, attempt, **kwargs):
        assert kind == "backend" and work == state.child and attempt.parent == state.work
        assert release.private_json(attempt)["decision_sha256"] == continuation.sha(state.decision)
        assert kwargs["validate_upload_window"] is True
        assert kwargs["expires"].tzinfo is not None
        return {"digest": "sha256:" + "9" * 64, "runId": "new-backend-only",
                "tool_revision": "f" * 40, "tool_sha256": "a" * 64}
    monkeypatch.setattr(release, "execute_publication", execute)
    continuation.publish(state.work, state.config, state.decision)


def deploy_and_configure(state, monkeypatch):
    fake_publish(state, monkeypatch)
    continuation.deploy(state.work, state.config, state.decision)
    continuation.configure(state.work, state.config, state.decision, state.recovery)


def test_local_check_uses_original_root_and_hashes_not_new_allowances(continued):
    state = continued
    assert continuation.root(state.work) == state.work
    assert continuation.validate(state.work, state.config, state.decision) == (state.approval, state.child)
    assert not state.calls and not state.console_calls
    with pytest.raises(ValueError, match="original fixed"):
        continuation.root(state.child)
    assert {name: (state.work / name).read_bytes() for name in state.old} == state.old


def test_standard_publication_cannot_treat_context_child_as_fresh_allowance(continued):
    from argparse import Namespace

    state = continued
    for work in (state.work, state.child, state.child / "another-directory"):
        with pytest.raises(ValueError, match="not a fresh allowance"):
            release.require_unused_publication(work, ["backend"])
        with pytest.raises(ValueError, match="not a fresh allowance"):
            release.run(Namespace(action="publish-backend", work=work))
    assert not state.calls and not state.console_calls


@pytest.mark.parametrize("file", [
    "backend-publication-attempt.json", "backend-publication-replacement-attempt.json",
    "backend-publication-supplemental-result.json", "frontend-publication-result.json",
    "pilot-execution-attempt-1.json", "pilot-disabled.json", "failed-pilot-accounting.json",
])
def test_all_failed_successful_and_consumed_history_is_immutable(continued, file):
    state = continued
    (state.work / file).write_text('{"changed":true}')
    with pytest.raises((ValueError, KeyError)):
        continuation.validate(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls


@pytest.mark.parametrize("change", [
    {"backend_builds": 2}, {"build_cpu": 4}, {"build_timeout_seconds": 901},
    {"worker_executions": 2}, {"worker_timeout_seconds": 601}, {"item_limit": 3},
    {"analysis": 1}, {"inference_requests": 5}, {"search": 1}, {"total_microdollars": 10000001},
])
def test_decision_cannot_expand_scope(continued, change):
    state = continued
    state.decision["policy"].update(change)
    with pytest.raises(ValueError, match="Only one"):
        continuation.validate(state.work, state.config, state.decision)
    assert not state.calls


@pytest.mark.parametrize("defect", ["not-approved", "operator", "budget", "unknown-cost", "cost-drift", "frontend", "smoke", "second-worker"])
def test_preconditions_fail_before_cloud_operations(continued, defect):
    state = continued
    if defect == "not-approved":
        state.decision["approved"] = False
    elif defect == "operator":
        state.decision["approved_by"] = "not-the-original"
    elif defect == "budget":
        state.decision["cost"]["consumed_component_upper_microdollars"] = 10000000
    elif defect == "unknown-cost":
        state.decision["cost"]["build_upper_microdollars"] = 0
    elif defect == "cost-drift":
        release.save(state.work / "continuation-cost-evidence.json", {"changed": True})
    elif defect == "frontend":
        (state.child / "source/frontend/Dockerfile").write_text("not unchanged")
    elif defect == "smoke":
        value = release.private_json(state.child / "backend-smoke.json")
        value["uid"] = 0
        release.save(state.child / "backend-smoke.json", value)
    else:
        release.save(state.work / "pilot-execution-attempt-2.json", {})
    with pytest.raises(ValueError):
        continuation.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls
    assert not (state.work / "continuation-attempt.json").exists()


@pytest.mark.parametrize("change", [
    {"money_ready": False},
    {"money_ready": None},
    {"approved_guard_price_basis_retained": False},
    {"decision": {}},
    {"verification_receipts": {}},
    {"incidental_forecast_microdollars": None},
    {"incidental_forecast_microdollars": -1},
    {"incidental_forecast_microdollars": 6000001},
    {"assurance": "hard_billing_cap"},
    {"approval_granted": True},
    {"total_forecast_microdollars": 4000000},
    {"remaining_contingency_microdollars": 6000000},
    {"audit_receipt": {"name": "../another-root.json", "sha256": "a" * 64}},
    {"decision": {"money": "NO-GO", "bounded_component_estimate": "PASS"}},
])
def test_planning_headroom_never_becomes_spend_authority(continued, change):
    state = continued
    path = state.work / "continuation-cost-evidence.json"
    evidence = release.private_json(path)
    evidence.update(change)
    release.save(path, evidence)
    state.decision["cost"]["evidence_sha256"] = continuation.raw_sha(path)
    with pytest.raises(ValueError, match="NO-GO|unknown|incidental|[Ee]vidence"):
        continuation.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls
    assert not (state.work / "continuation-attempt.json").exists()


@pytest.mark.parametrize("component", sorted(continuation.COMPONENT_FIELDS))
def test_cost_decision_retains_compute_consumption_beyond_service_ledger(continued, component):
    state = continued
    state.decision["cost"][component] = state.component_floors[component] - 1
    with pytest.raises(ValueError, match="off-ledger"):
        continuation.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls


def test_incidental_forecast_is_an_explicit_fifth_budget_component(continued):
    state = continued
    state.decision["cost"]["incidental_forecast_microdollars"] = 6000001
    with pytest.raises(ValueError, match="original \\$10"):
        continuation.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls
    del state.decision["cost"]["incidental_forecast_microdollars"]
    with pytest.raises(ValueError, match="remaining-cost evidence"):
        continuation.validate(state.work, state.config, state.decision)


def test_superseded_cost_receipt_cannot_replace_pinned_final_forecast(continued):
    state = continued
    older = state.work / "synthetic-superseded-cost-audit.json"
    release.save(older, {"money_decision": "NO-GO", "superseded_for_money_only": True})
    evidence_path = state.work / "continuation-cost-evidence.json"
    evidence = release.private_json(evidence_path)
    evidence["audit_receipt"] = {"name": older.name, "sha256": continuation.raw_sha(older)}
    release.save(evidence_path, evidence)
    state.decision["cost"]["evidence_sha256"] = continuation.raw_sha(evidence_path)
    with pytest.raises(ValueError, match="authoritative pinned"):
        continuation.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls


def test_repointed_private_root_pin_cannot_replenish_copied_allowances(continued):
    state = continued
    copied = state.work.parent / "copied-original"
    shutil.copytree(state.work, copied)
    pin = release.private_json(continuation.ROOT_PIN)
    pin["work_root"] = str(copied)
    release.save(continuation.ROOT_PIN, pin)
    with pytest.raises(ValueError, match="root pin changed"):
        continuation.validate(copied, state.config, state.decision)
    assert not state.calls and not state.console_calls


@pytest.mark.parametrize("defect", ["unverified", "prior-coverage", "null-bound", "prices", "target"])
def test_positive_costs_and_flags_without_verified_coverage_are_insufficient(continued, defect):
    state = continued
    kind = "rates" if defect == "prices" else "incidentals"
    path = state.work / f"continuation-cost-{kind}-verified.json"
    proof = release.private_json(path)
    if defect == "unverified":
        proof["verified"] = False
    elif defect == "prior-coverage":
        proof["coverage"] = "future_only"
    elif defect == "null-bound":
        proof["upper_microdollars"] = None
    elif defect == "prices":
        proof["unit_prices_usd"]["input_token"] = "0.0000001"
    else:
        proof["target"] = "different-account"
    release.save(path, proof)
    evidence_path = state.work / "continuation-cost-evidence.json"
    evidence = release.private_json(evidence_path)
    evidence["verification_receipts"][kind] = continuation.raw_sha(path)
    release.save(evidence_path, evidence)
    state.decision["cost"]["evidence_sha256"] = continuation.raw_sha(evidence_path)
    with pytest.raises(ValueError, match="monetary evidence|ceilings|coverage"):
        continuation.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls


def test_retained_price_basis_does_not_require_invoices_or_negotiated_rate_sheet(continued):
    state = continued
    path = state.work / "continuation-cost-evidence.json"
    evidence = release.private_json(path)
    evidence.update(finalized_billing=None, account_specific_price_ceiling_verified=False)
    release.save(path, evidence)
    state.decision["cost"]["evidence_sha256"] = continuation.raw_sha(path)
    continuation.validate(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls


@pytest.mark.parametrize("defect", ["lifetime", "uncertainty", "deletion", "monthly-disclosure", "month-conversion"])
def test_execution_estimate_requires_finite_window_and_retention_disclosure(continued, defect):
    state = continued
    proof = release.private_json(state.work / "continuation-cost-incidentals-verified.json")
    if defect == "lifetime":
        proof["execution_window_seconds"] = None
    elif defect == "uncertainty":
        proof["uncertainty"] = []
    elif defect == "deletion":
        proof["monthly_retention_disclosure"]["automatic_deletion"] = True
    elif defect == "month-conversion":
        proof["storage_month_days"] = None
    else:
        proof["monthly_retention_disclosure"]["estimated_microdollars"] = None
    with pytest.raises(ValueError, match="one-hour|uncertainty|30-day"):
        continuation.incidental_ceiling(proof)


def test_disclosed_monthly_retention_is_not_an_infinite_lifetime_execution_gate(continued):
    state = continued
    path = state.work / "continuation-cost-incidentals-verified.json"
    proof = release.private_json(path)
    proof["monthly_retention_disclosure"]["estimated_microdollars"] = 20000000
    release.save(path, proof)
    evidence_path = state.work / "continuation-cost-evidence.json"
    evidence = release.private_json(evidence_path)
    evidence["verification_receipts"]["incidentals"] = continuation.raw_sha(path)
    release.save(evidence_path, evidence)
    state.decision["cost"]["evidence_sha256"] = continuation.raw_sha(evidence_path)
    continuation.validate(state.work, state.config, state.decision)
    assert not state.calls


@pytest.mark.parametrize("defect", ["missing-category", "unknown-prior", "unknown-new", "wrong-unit", "zero-price", "unevidenced", "dollar-contingency"])
def test_incidental_ceiling_requires_supported_quantities_times_prices(continued, defect):
    state = continued
    path = state.work / "continuation-cost-incidentals-verified.json"
    proof = release.private_json(path)
    envelope = proof["envelopes"]["stored_images"]
    if defect == "missing-category":
        del proof["envelopes"]["rollout_memory"]
    elif defect == "unknown-prior":
        envelope["prior_quantity"] = None
    elif defect == "unknown-new":
        envelope["new_quantity"] = None
    elif defect == "wrong-unit":
        envelope["unit"] = "unknown"
    elif defect == "zero-price":
        envelope["unit_price_usd"] = "0"
    elif defect == "unevidenced":
        envelope["basis"] = ""
    else:
        proof["upper_microdollars"] = 1000000
    release.save(path, proof)
    evidence_path = state.work / "continuation-cost-evidence.json"
    evidence = release.private_json(evidence_path)
    evidence["verification_receipts"]["incidentals"] = continuation.raw_sha(path)
    evidence["incidental_forecast_microdollars"] = proof["upper_microdollars"]
    release.save(evidence_path, evidence)
    state.decision["cost"]["evidence_sha256"] = continuation.raw_sha(evidence_path)
    with pytest.raises(ValueError, match="quantity|quantities|unit|coverage|price|unknown"):
        continuation.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.model_metadata_calls and not state.console_calls


def test_backend_only_publication_retains_original_history(continued, monkeypatch):
    state = continued
    fake_publish(state, monkeypatch)
    images = release.private_json(state.child / "images.json")
    assert images["frontend"] == state.config["frontend_image"]
    assert images["preserved_frontend"]["work"] == str(state.work)
    assert images["backend"].endswith("9" * 64)
    assert continuation.published_config(state.work, state.config, state.decision)["backend_image"] == images["backend"]
    with pytest.raises(ValueError, match="already attempted"):
        continuation.publish(state.work, state.config, state.decision)
    assert {name: (state.work / name).read_bytes() for name in state.old} == state.old


@pytest.mark.parametrize("where", ["metadata", "upload", "schedule", "observe"])
def test_unknown_publication_consumes_the_one_fixed_attempt(continued, monkeypatch, where):
    state = continued
    calls = []

    def fail(*args, **kwargs):
        calls.append(where)
        raise ValueError("Unknown outcome")

    monkeypatch.setattr(release, "execute_publication", fail)
    with pytest.raises(ValueError, match="Unknown"):
        continuation.publish(state.work, state.config, state.decision)
    with pytest.raises(ValueError, match="already attempted"):
        continuation.publish(state.work, state.config, state.decision)
    assert calls == [where]
    assert (state.work / "continuation-attempt.json").exists()
    assert not (state.work / "continuation-published.json").exists()
    changed = copy.deepcopy(state.decision)
    changed["publication_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    with pytest.raises(ValueError, match="pinned"):
        continuation.validate(state.work, state.config, changed)


def test_deploy_preserves_frontend_auth_and_acceptance_without_synthetic_start(continued, monkeypatch):
    state = continued
    original_resources = copy.deepcopy(state.resources)
    original_job = copy.deepcopy(state.job)
    fake_publish(state, monkeypatch)
    continuation.deploy(state.work, state.config, state.decision)
    assert state.resources["frontend"] == original_resources["frontend"]
    for resource in (original_resources["backend"], original_job):
        resource["properties"]["template"]["containers"][0]["image"] = state.resources["backend"]["properties"]["template"]["containers"][0]["image"]
    assert state.resources["backend"] == original_resources["backend"]
    assert state.job == original_job
    assert len(state.calls) == 2
    assert all(call[:3] == ("rest", "--method", "PATCH") for call in state.calls)
    assert not (state.child / "pilot-acceptance.json").exists()
    assert {name: (state.work / name).read_bytes() for name in state.old} == state.old


def test_one_last_worker_has_exact_limits_and_closure_preserves_read_export(continued, monkeypatch):
    state = continued
    deploy_and_configure(state, monkeypatch)
    original_backend = copy.deepcopy(state.resources["backend"])
    original_job = copy.deepcopy(state.job)
    continuation.enable(state.work, state.config, state.decision)
    continuation.start(state.work, state.config, state.decision)
    template = release.private_json(state.work / "continuation-worker-template.json")
    container = template["containers"][0]
    assert container["args"] == [
        "-m", "backend.batch_worker", "--real-pilot", "--batch-id", state.approval["batch_id"],
        "--concurrency", "1", "--max-batches", "1", "--item-limit", "2",
    ]
    assert not set(release.environment_entries(container)) & (release.PILOT_EXTERNAL_ENVIRONMENT_KEYS | {"WEBIQ_API_KEY"})
    assert state.job == original_job
    with pytest.raises(ValueError, match="already attempted"):
        continuation.start(state.work, state.config, state.decision)
    monkeypatch.setattr(release, "pilot_window", lambda _: pytest.fail("Closure must not require an active window"))
    continuation.close(state.work, state.config, state.decision)
    assert state.resources["backend"] == original_backend
    assert state.job == original_job
    assert {name: (state.work / name).read_bytes() for name in state.old} == state.old
    starts = [call for call in state.calls if call[:3] == ("containerapp", "job", "start")]
    assert len(starts) == 1
    assert not any("delete" in call or "--synthetic-acceptance" in call for call in state.calls)


def test_unknown_worker_start_never_repeats_and_can_close(continued, monkeypatch):
    state = continued
    deploy_and_configure(state, monkeypatch)
    continuation.enable(state.work, state.config, state.decision)
    underlying = release.azure
    submissions = []

    def unknown(*args, **kwargs):
        if args[:3] == ("containerapp", "job", "start"):
            submissions.append(args)
            raise ValueError("Unknown worker outcome")
        return underlying(*args, **kwargs)

    monkeypatch.setattr(release, "azure", unknown)
    with pytest.raises(ValueError, match="Unknown"):
        continuation.start(state.work, state.config, state.decision)
    with pytest.raises(ValueError, match="already attempted"):
        continuation.start(state.work, state.config, state.decision)
    assert len(submissions) == 1
    assert not (state.work / "continuation-worker-result.json").exists()
    continuation.close(state.work, state.config, state.decision)
    assert (state.work / "continuation-closed.json").exists()


def test_close_restores_api_flags_while_uncertain_worker_is_active(continued, monkeypatch):
    state = continued
    deploy_and_configure(state, monkeypatch)
    continuation.enable(state.work, state.config, state.decision)
    original_job = copy.deepcopy(state.job)
    monkeypatch.setattr(release, "active_executions", lambda config, **kwargs: (
        copy.deepcopy(state.job), [{"name": "reserved-last-worker", "properties": {"status": "Running"}}],
    ))
    monkeypatch.setattr(release, "pilot_window", lambda _: pytest.fail("Closure must work after expiry"))
    continuation.close(state.work, state.config, state.decision)
    assert state.job == original_job
    flags = release.environment_entries(state.resources["backend"]["properties"]["template"]["containers"][0])
    assert flags.get("DOCINTEL_REAL_PILOT_ENABLED", {}).get("value") != "true"
    assert (state.work / "continuation-closed.json").exists()


def test_patch_reconciles_same_asynchronous_write_without_resubmission(continued, monkeypatch):
    state = continued
    old = copy.deepcopy(state.resources["backend"])
    containers = release.safe_containers(old)
    containers[0]["image"] = "registry.invalid/backend@sha256:" + "9" * 64
    pending, clock = [], [0.0]
    underlying = release.azure
    def asynchronous(*args, **kwargs):
        result = underlying(*args, **kwargs)
        if args[:3] == ("rest", "--method", "PATCH"):
            pending.append(copy.deepcopy(state.resources["backend"]))
            state.resources["backend"] = copy.deepcopy(old)
        return result
    def wait(delay):
        clock[0] += delay
        if pending:
            state.resources["backend"] = pending.pop()
    monkeypatch.setattr(release, "azure", asynchronous)
    monkeypatch.setattr(continuation, "monotonic", lambda: clock[0])
    monkeypatch.setattr(continuation, "sleep", wait)
    continuation.patch(state.work, state.config, "async-test", old, containers)
    assert len([call for call in state.calls if call[:3] == ("rest", "--method", "PATCH")]) == 1
    assert clock[0] == 5
    assert state.resources["backend"]["properties"]["template"]["containers"] == containers


def test_ready_waits_for_revision_without_mutating_resources(continued, monkeypatch):
    state = continued
    current = state.resources["backend"]
    current["properties"]["latestReadyRevisionName"] = "previous-ready"
    clock = [0.0]
    def wait(delay):
        clock[0] += delay
        current["properties"]["latestReadyRevisionName"] = current["properties"]["latestRevisionName"]
    monkeypatch.setattr(continuation, "monotonic", lambda: clock[0])
    monkeypatch.setattr(continuation, "sleep", wait)
    assert continuation.ready(state.config, "backend")["properties"]["latestReadyRevisionName"] != "previous-ready"
    assert clock[0] == 5 and not state.calls


@pytest.mark.parametrize("stage", ["before-reservation", "after-reservation"])
def test_last_start_rechecks_600_seconds_and_can_close_after_denial(continued, monkeypatch, stage):
    state = continued
    deploy_and_configure(state, monkeypatch)
    continuation.enable(state.work, state.config, state.decision)
    real_window = continuation.window
    checks = []

    def advancing_clock(value, minimum=0):
        if minimum == 600:
            checks.append(minimum)
            if stage == "before-reservation" or len(checks) == 2:
                raise ValueError("Insufficient active window for 600-second worker")
        return real_window(value, minimum)

    monkeypatch.setattr(continuation, "window", advancing_clock)
    with pytest.raises(ValueError, match="600-second"):
        continuation.start(state.work, state.config, state.decision)
    assert not any(call[:3] == ("containerapp", "job", "start") for call in state.calls)
    attempted = state.work / "continuation-worker-attempt.json"
    assert attempted.exists() is (stage == "after-reservation")
    if attempted.exists():
        with pytest.raises(ValueError, match="already attempted"):
            continuation.start(state.work, state.config, state.decision)
    original_disabled = (state.work / "pilot-disabled.json").read_bytes()
    flags = release.environment_entries(state.resources["backend"]["properties"]["template"]["containers"][0])
    assert flags["DOCINTEL_REAL_PILOT_ENABLED"]["value"] == "true"
    assert not (state.work / "continuation-closed.json").exists()
    monkeypatch.setattr(continuation, "window", lambda *args: pytest.fail("Expired closure cannot check activation"))
    continuation.close(state.work, state.config, state.decision)
    flags = release.environment_entries(state.resources["backend"]["properties"]["template"]["containers"][0])
    assert flags.get("DOCINTEL_REAL_PILOT_ENABLED", {}).get("value") != "true"
    assert (state.work / "pilot-disabled.json").read_bytes() == original_disabled
    assert (state.work / "continuation-closed.json").exists()


@pytest.mark.parametrize("defect", ["active", "worker-timeout", "worker-identity", "worker-resources", "frontend", "api-setting"])
def test_activation_runtime_drift_fails_closed(continued, monkeypatch, defect):
    state = continued
    deploy_and_configure(state, monkeypatch)
    calls_before = len(state.calls)
    if defect == "active":
        state.active.append({"name": "unexpected-running-worker"})
    elif defect == "worker-timeout":
        state.job["properties"]["configuration"]["replicaTimeout"] = 601
    elif defect == "worker-identity":
        state.job["identity"]["principalId"] = state.config["api_principal_id"]
    elif defect == "worker-resources":
        state.job["properties"]["template"]["containers"][0]["resources"]["cpu"] = 2
    elif defect == "frontend":
        state.resources["frontend"]["properties"]["template"]["containers"][0]["env"].append({"name": "DRIFT", "value": "true"})
    else:
        state.resources["backend"]["properties"]["template"]["containers"][0]["env"].append({"name": "DRIFT", "value": "true"})
    with pytest.raises(ValueError):
        continuation.enable(state.work, state.config, state.decision)
    assert len(state.calls) == calls_before


def test_rollback_is_backend_and_worker_only_and_retains_history(continued, monkeypatch):
    state = continued
    before = copy.deepcopy(state.resources)
    original_job = copy.deepcopy(state.job)
    deploy_and_configure(state, monkeypatch)
    continuation.enable(state.work, state.config, state.decision)
    continuation.close(state.work, state.config, state.decision)
    continuation.rollback(state.work, state.config, state.decision)
    assert state.resources == before
    assert state.job == original_job
    assert {name: (state.work / name).read_bytes() for name in state.old} == state.old


@pytest.mark.parametrize("defect", ["approval-id", "operator", "ledger", "selection", "old-window", "long-window", "outside-cost-window"])
def test_recovery_packet_cannot_replace_original_authority(continued, defect):
    state = continued
    recovery = state.recovery
    if defect == "approval-id":
        recovery["id"] = "new"
    elif defect == "operator":
        recovery["approved_by"] = "someone-else"
    elif defect == "ledger":
        recovery["ledger_sha256"] = "a" * 64
    elif defect == "selection":
        recovery["selected_item_keys"] = ["3" * 64]
    elif defect == "old-window":
        recovery["not_before"] = state.approval["not_before"]
    elif defect == "outside-cost-window":
        state.decision["publication_not_before"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    else:
        recovery["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError):
        continuation.recovery_packet(state.approval, state.decision, recovery)


def remote_fixture(state, monkeypatch):
    from backend import batch_store, real_pilot
    from backend.batch_store import write_json
    class BoundedStore(MemoryStore):
        def read_bytes(self, key, **kwargs):
            value = super().read_bytes(key)
            assert len(value[0]) <= kwargs.get("max_bytes", 10000000)
            return value

    store = BoundedStore()
    keys = state.decision["selection"]["selected_item_keys"]
    batch = {
        "id": state.approval["batch_id"], "owner": state.approval["owner"],
        "valid": True, "mode": "real_pilot", "state": "running", "product_count": 2,
        "input_hashes": {"manifest": "b" * 64, "attributes": "c" * 64},
        "attribute_reference": "synthetic.xlsx", "original_definitions": [],
        "items": [{"item_key": key, "errors": [], "manifest": {
            "product": {"item_id": key, "vendor": "synthetic", "mpn": key},
        }, "sources": [{
            "kind": "blob", "format": "pdf", "blob": "documents/synthetic.pdf",
            "sha256": hashlib.sha256(b"%PDF-1.4 synthetic document").hexdigest(),
        }]} for key in keys],
    }
    state.approval["batch_sha256"] = real_pilot.binding_digest(batch)
    state.recovery["approval_sha256"] = continuation.sha(state.approval)
    state.recovery["batch_sha256"] = state.approval["batch_sha256"]
    ledger = {
        "approval_sha256": continuation.sha(state.approval), "owner": state.approval["owner"],
        "batch_sha256": state.approval["batch_sha256"],
        "executions": {state.decision["selection"]["prior_execution_id"]: {"started_at": state.approval["not_before"]}},
        "attempted": {"inference": 0, "analysis": 2, "search": 0, "web_retrieval": 0, "retrieval": 0},
        "reserved": {"input_tokens": 0, "output_tokens": 0, "microdollars": 10, "analysis_pages": 10},
    }
    state.decision["selection"]["ledger_sha256"] = continuation.sha(ledger)
    for key in keys:
        path = "items/" + batch["id"] + "/" + key + ".json"
        write_json(store, path, {"state": "interrupted", "synthetic": key})
        state.decision["selection"]["interrupted_sha256"][key] = hashlib.sha256(store.records[path][0]).hexdigest()
    parse = next(iter(state.decision["selection"]["cached_documents"]))
    store.write_bytes(parse, b'{"synthetic_cached_document":true}')
    state.decision["selection"]["cached_documents"][parse] = hashlib.sha256(store.records[parse][0]).hexdigest()
    state.recovery.update(copy.deepcopy(state.decision["selection"]))
    store.write_bytes("documents/synthetic.pdf", b"%PDF-1.4 synthetic document")
    write_json(store, real_pilot.APPROVAL_KEY, state.approval)
    write_json(store, real_pilot.BUDGET_KEY, ledger)
    write_json(store, "batches/" + batch["id"] + ".json", batch)
    monkeypatch.setattr(batch_store, "configured_store", lambda: store)
    return store


def test_remote_preflight_is_read_only_and_configure_writes_only_fixed_recovery(continued, monkeypatch, capsys):
    state = continued
    store = remote_fixture(state, monkeypatch)
    before = copy.deepcopy(store.records)
    for recovery in (None, state.recovery):
        code, marker = continuation.remote_code(state.approval, state.decision, recovery)
        exec(compile(code, "<synthetic-readonly-continuation>", "exec"), {})
        assert marker in capsys.readouterr().out.splitlines()
        assert store.records == before
    code, _ = continuation.remote_code(state.approval, state.decision, state.recovery, configured=True)
    with pytest.raises(AssertionError):
        exec(compile(code, "<synthetic-missing-configuration>", "exec"), {})
    assert store.records == before
    code, marker = continuation.remote_code(state.approval, state.decision, state.recovery, write=True)
    exec(compile(code, "<synthetic-configure-continuation>", "exec"), {})
    assert marker in capsys.readouterr().out.splitlines()
    assert set(store.records) - set(before) == {continuation.RECOVERY_KEY}
    assert {key: store.records[key] for key in before} == before
    check, _ = continuation.remote_code(state.approval, state.decision, state.recovery, configured=True)
    exec(compile(check, "<synthetic-confirmed-configuration>", "exec"), {})
    with pytest.raises(AssertionError):
        exec(compile(code, "<synthetic-repeat-continuation>", "exec"), {})


@pytest.mark.parametrize("defect", ["ledger", "status", "cache", "result", "recovery", "source", "owner", "model-cost"])
def test_remote_preflight_denial_never_invalidates_or_writes_ledger(continued, monkeypatch, defect):
    state = continued
    store = remote_fixture(state, monkeypatch)
    if defect == "ledger":
        key = "budgets/real-pilot.json"
    elif defect == "status":
        key = "items/" + state.approval["batch_id"] + "/" + state.recovery["selected_item_keys"][0] + ".json"
    elif defect == "cache":
        key = next(iter(state.recovery["cached_documents"]))
    elif defect == "result":
        key = "results/" + state.approval["batch_id"] + "/" + state.recovery["selected_item_keys"][0] + ".json"
    elif defect == "source":
        key = "documents/synthetic.pdf"
    elif defect == "owner":
        key = "batches/" + state.approval["batch_id"] + ".json"
    elif defect == "model-cost":
        state.decision["cost"]["inference_upper_microdollars"] = 1
        key = None
    else:
        key = continuation.RECOVERY_KEY
    if key is not None:
        store.records[key] = (b'{"changed":true}', "drifted")
    before = copy.deepcopy(store.records)
    code, _ = continuation.remote_code(state.approval, state.decision, state.recovery, write=True)
    with pytest.raises((ValueError, AssertionError, KeyError)):
        exec(compile(code, "<synthetic-invalid-continuation>", "exec"), {})
    assert store.records == before


def test_partial_deployment_can_roll_back_without_fabricating_activation(continued, monkeypatch):
    state = continued
    original_backend = copy.deepcopy(state.resources["backend"])
    original_job = copy.deepcopy(state.job)
    fake_publish(state, monkeypatch)
    underlying = continuation.patch

    def fail_worker(work, config, label, resource, containers, **kwargs):
        if label == "deploy-worker":
            raise ValueError("Synthetic worker patch failure")
        return underlying(work, config, label, resource, containers, **kwargs)

    monkeypatch.setattr(continuation, "patch", fail_worker)
    with pytest.raises(ValueError, match="patch failure"):
        continuation.deploy(state.work, state.config, state.decision)
    assert not (state.work / "continuation-deployed.json").exists()
    assert not (state.work / "continuation-enablement.json").exists()
    continuation.rollback(state.work, state.config, state.decision)
    assert state.resources["backend"] == original_backend
    assert state.job == original_job


def test_ready_revision_must_confirm_authorization_not_only_image(continued, monkeypatch):
    state = continued
    underlying = release.azure

    def stale_revision(*args, **kwargs):
        result = underlying(*args, **kwargs)
        if args[:3] == ("containerapp", "revision", "show"):
            result["properties"]["template"]["containers"][0]["env"].append({
                "name": "DOCINTEL_REAL_PILOT_ENABLED", "value": "true",
            })
        return result

    monkeypatch.setattr(release, "azure", stale_revision)
    with pytest.raises(ValueError, match="authorization settings"):
        continuation.ready(state.config, "backend")
    assert not state.calls


def test_model_capacity_uses_management_metadata_not_inference(continued):
    state = continued
    proof = continuation.preflight(state.work, state.config, state.decision)
    assert proof == {"deployment_sha256": continuation.sha(state.model_metadata), "tokens_per_minute": 30000}
    assert len(state.model_metadata_calls) == 1
    call = state.model_metadata_calls[0]
    assert call[:4] == ("cognitiveservices", "account", "deployment", "show")
    assert call[call.index("--name") + 1] == "synthetic-model"
    assert call[call.index("--deployment-name") + 1] == state.approval["environment"]["LLM_DEPLOYMENT"]
    assert not state.calls


def test_model_capacity_accepts_raw_arm_float_limits_and_nullable_metadata(continued):
    state = continued
    state.model_metadata["properties"]["rateLimits"] = [
        {"count": count, "key": key, "renewalPeriod": 60.0,
         "dynamicThrottlingEnabled": None, "matchPatterns": None, "minCount": None}
        for key, count in (("request", 300.0), ("token", 30000.0))
    ]
    state.model_metadata["properties"]["model"].update(source=None, callRateLimit=None)
    observed = continuation.require_model_capacity(state.config, state.approval)
    assert observed["tokens_per_minute"] == 30000
    assert observed["deployment_sha256"] == continuation.sha(state.model_metadata)
    assert not state.calls


@pytest.mark.parametrize("defect", [
    "missing", "too-small", "requests-not-tokens", "wrong-period", "duplicate",
    "wrong-resource", "model-upgrade", "sku", "not-ready", "nan", "boolean",
])
def test_unknown_or_insufficient_token_capacity_blocks_before_publication(continued, defect):
    state = continued
    metadata = state.model_metadata
    limits = metadata["properties"]["rateLimits"]
    if defect == "missing":
        del metadata["properties"]["rateLimits"]
    elif defect == "too-small":
        limits[1]["count"] = 29999
    elif defect == "requests-not-tokens":
        limits[1]["key"] = "request"
    elif defect == "wrong-period":
        limits[1]["renewalPeriod"] = 120
    elif defect == "duplicate":
        limits.append(copy.deepcopy(limits[1]))
    elif defect == "wrong-resource":
        metadata["id"] += "-different"
    elif defect == "model-upgrade":
        metadata["properties"]["model"]["version"] = "different"
    elif defect == "sku":
        metadata["sku"]["name"] = "DifferentStandard"
    elif defect == "not-ready":
        metadata["properties"]["provisioningState"] = "Updating"
    elif defect == "nan":
        limits[1]["count"] = float("nan")
    else:
        limits[1]["count"] = True
    with pytest.raises(ValueError):
        continuation.publish(state.work, state.config, state.decision)
    assert not state.calls and not state.console_calls
    assert not (state.work / "continuation-attempt.json").exists()


def test_closure_does_not_depend_on_model_capacity_after_activation(continued, monkeypatch):
    state = continued
    deploy_and_configure(state, monkeypatch)
    continuation.enable(state.work, state.config, state.decision)
    state.model_metadata["properties"]["rateLimits"][1]["count"] = 1
    count = len(state.model_metadata_calls)
    with pytest.raises(ValueError, match="30000 tokens"):
        continuation.start(state.work, state.config, state.decision)
    assert len(state.model_metadata_calls) == count + 1
    continuation.close(state.work, state.config, state.decision)
    assert len(state.model_metadata_calls) == count + 1
    assert not (state.work / "continuation-worker-attempt.json").exists()
    assert (state.work / "continuation-closed.json").exists()
