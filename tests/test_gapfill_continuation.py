"""No-cloud operator contract tests; every live boundary is replaced."""

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import gapfill_continuation as gap


@pytest.fixture
def state(tmp_path, monkeypatch):
    work = tmp_path
    work.chmod(0o700)
    monkeypatch.setattr(gap.release, "private_path", lambda value: value)
    anchor = datetime.now(timezone.utc)
    decision = {"target": "synthetic", "source_revision": "b" * 40,
                "credential_origin": "GBBdemo", "credential_source": "existing_job_secret",
                "webiq_secret_ref": "existing-gbbdemo"}
    value = {**gap.binding(decision), "readiness_at": anchor.isoformat(),
             "publication_expires_at": (anchor + timedelta(seconds=2700)).isoformat(),
             "overall_expires_at": (anchor + timedelta(seconds=5400)).isoformat()}
    gap.release.save_once(gap.path(work, "readiness"), value)
    gap.release.save_once(gap.path(work, "readiness-pin"), {**gap.binding(decision), "sha256": gap.sha(value)})
    gap.release.save_once(gap.path(work, "deployed"), {
        **gap.binding(decision), "both_ready_at": anchor.isoformat(),
        "processing_expires_at": (anchor + timedelta(seconds=1200)).isoformat(),
    })
    state = SimpleNamespace(work=work, decision=decision, clock=anchor, calls=[])
    monkeypatch.setattr(gap, "now", lambda: state.clock)
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("No network"))
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: pytest.fail("No network"))
    return state


@pytest.mark.parametrize("phase,seconds,minimum,allowed", [
    ("publication", 1800, 900, True), ("publication", 1801, 900, False),
    ("processing", 600, 600, True), ("processing", 601, 600, False),
    ("overall", 5400, 0, False), ("cleanup", 7200, 0, True),
])
def test_complete_fixed_windows_are_required(state, phase, seconds, minimum, allowed):
    state.clock += timedelta(seconds=seconds)
    if allowed:
        gap.window(state.work, state.decision, phase, minimum)
    else:
        with pytest.raises(ValueError):
            gap.window(state.work, state.decision, phase, minimum)


def test_deadline_rechecked_before_upload_and_build(state, monkeypatch):
    monkeypatch.setattr(gap.release, "upload_publication_context", lambda *a, **k: state.calls.append("upload"))
    monkeypatch.setattr(gap.release, "azure", lambda *a, **k: state.calls.append(a))
    with gap.live(state.work, state.decision, "publication"):
        state.clock += timedelta(seconds=1801)
        with pytest.raises(ValueError):
            gap.release.upload_publication_context({}, Path("never-upload"), 900)
        with pytest.raises(ValueError):
            gap.release.azure("rest", "--method", "POST")
    assert not state.calls


def test_readiness_pin_cannot_be_changed(state):
    value = gap.release.private_json(gap.path(state.work, "readiness"))
    value["overall_expires_at"] = (state.clock + timedelta(seconds=5500)).isoformat()
    gap.release.save(gap.path(state.work, "readiness"), value)
    with pytest.raises(ValueError):
        gap.window(state.work, state.decision, "overall")
    assert gap.window(state.work, state.decision, "cleanup") == {}


@pytest.mark.parametrize("filename", [
    "backend/extract.py", "backend/models/enrichment.py",
    "backend/multisource.py", "frontend/components/enrichment-review.tsx",
])
def test_source_boundary_excludes_track_b_and_frontend(monkeypatch, filename):
    def command(args, **kwargs):
        if args[1] == "diff":
            return ("\n".join(["backend/real_pilot.py", "backend/batch_worker.py",
                              "scripts/gapfill_continuation.py", filename])).encode()
        return b""

    monkeypatch.setattr(gap.release, "command", command)
    with pytest.raises(ValueError, match="business-pipeline"):
        gap.source_boundary("b" * 40)


@pytest.mark.parametrize("extra", [b"", b"\n# Unapproved telemetry change\n"])
def test_worker_allows_only_explicit_gapfill_interface_exception(monkeypatch, extra):
    original = ("before\n" + gap.WORKER_INTERFACE_OLD + "after\n").encode()
    approved = original.replace(gap.WORKER_INTERFACE_OLD.encode(), gap.WORKER_INTERFACE_NEW.encode())

    def command(args, **kwargs):
        if args[1] == "diff":
            return b"backend/real_pilot.py\nbackend/batch_worker.py\nscripts/gapfill_continuation.py\n"
        if args[1] == "show":
            return original if args[2].startswith(gap.BASELINE_REVISION + ":") else approved + extra
        return b""

    monkeypatch.setattr(gap.release, "command", command)
    if extra:
        with pytest.raises(ValueError, match="only the exact gapfill scope exception"):
            gap.source_boundary("b" * 40)
    else:
        gap.source_boundary("b" * 40)


def test_policy_is_exact_backend_only_and_fresh_incremental():
    assert gap.POLICY["frontend_builds"] == 0 and gap.POLICY["backend_builds"] == 1
    assert gap.POLICY["additional_inference"] == 1
    assert gap.POLICY["additional_input_tokens"] == 151646
    assert gap.POLICY["additional_output_tokens"] == 2048
    assert gap.POLICY["incremental_microdollars"] == 5000000


def test_history_pins_existing_records_without_freezing_sibling_additions(state):
    original = state.work / "retained-row-receipt.json"
    gap.release.save_once(original, {"owner": "synthetic", "nested": {"one": 1, "two": 2}})
    retained = gap.history(state.work)
    gap.release.save_once(state.work / "track-b-later-private-gate.json", {"scope": "unrelated"})
    gap.verify_history(state.work, retained)
    original.write_text(json.dumps({"nested": {"two": 2, "one": 1}, "owner": "synthetic"}, indent=3))
    gap.verify_history(state.work, retained)
    original.write_text(json.dumps({"owner": "changed", "nested": {"one": 1, "two": 2}}))
    with pytest.raises(ValueError, match="pinned historical record"):
        gap.verify_history(state.work, retained)


def test_history_rejects_removed_or_nonprivate_pinned_records(state):
    original = state.work / "retained-row-receipt.json"
    gap.release.save_once(original, {"preserve": True})
    retained = gap.history(state.work)
    original.chmod(0o644)
    with pytest.raises(ValueError, match="pinned historical record"):
        gap.verify_history(state.work, retained)
    original.unlink()
    with pytest.raises(ValueError, match="pinned historical record"):
        gap.verify_history(state.work, retained)


def test_decision_factory_is_complete_canonical_and_does_not_start_a_clock(state, monkeypatch):
    approval = {"approved_by": "synthetic-operator"}
    monkeypatch.setattr(gap, "original", lambda *a: (approval, {}))
    gap.release.save_once(gap.path(state.work, "baseline"), {"source_revision": "b" * 40})
    gap.release.save_once(state.work / "gapfill-private-gate.json", {"snapshot_sha256": "c" * 64, "scope_sha256": "d" * 64})
    existing = {candidate.name: candidate.read_bytes() for candidate in state.work.glob("*.json")}
    decision = gap.decision_from_gate(state.work, {"target": "synthetic"})
    assert set(decision) == gap.DECISION_FIELDS
    assert decision["credential_source"] == "owner_env_file" and decision["webiq_secret_ref"] == "webiq-gbbdemo"
    assert decision["policy"] == gap.POLICY and decision["policy"] is not gap.POLICY
    assert decision["gate_sha256"] == gap.digest_file(state.work / "gapfill-private-gate.json")
    assert {candidate.name: candidate.read_bytes() for candidate in state.work.glob("*.json")} == existing


def test_legacy_web_configuration_omits_new_item_page_defaults_only(monkeypatch):
    from backend.core.websearch_policy import OptionalWebPolicy
    from backend.real_pilot import RealPilotGuard

    monkeypatch.setattr(RealPilotGuard, "_cost", lambda *args: 185920)
    selected = ["row-2", "row-3"]
    records = {
        "configuration/real-pilot-approval.json": {"batch_id": "synthetic", "batch_sha256": "a" * 64},
        "configuration/real-pilot-row-rerun.json": {"selected_item_keys": selected},
        "batches/synthetic.json": {"items": [
            {"item_key": key, "manifest": {"product": {"mpn": "PUBLIC-" + key}},
             "sources": [{"kind": "web", "source_tier": "manufacturer_web",
                          "source_id": "web-" + key, "url": "https://www.muellercompany.com"}]}
            for key in selected
        ]},
    }
    configuration = gap.web_configuration(records, {key: {"material": "Material"} for key in selected})
    policy = configuration["public_web_policy"]
    assert policy["max_direct_page_attempts"] == 4
    assert policy["max_search_calls"] == policy["max_inference_calls"] == 2
    assert all("max_direct_page_attempts" not in item for item in policy["items"].values())
    parsed = OptionalWebPolicy.model_validate(policy)
    assert all(getattr(item, "max_direct_page_attempts", 2) == 2 for item in parsed.items.values())


@pytest.mark.parametrize("failure", ["configure", "enable", "start", "observe"])
def test_activation_always_closes_without_retry(state, monkeypatch, failure):
    config = {"synthetic": True}
    monkeypatch.setattr(gap, "validate", lambda *a: None)
    monkeypatch.setattr(gap, "current_config", lambda *a: config)
    monkeypatch.setattr(gap, "active_target", lambda *a: config)
    for name in ("configure", "enable", "start", "observe", "close"):
        def call(*args, _name=name):
            state.calls.append(_name)
            if _name == failure:
                raise ValueError("synthetic failure")
        monkeypatch.setattr(gap, name, call)
    with pytest.raises(ValueError, match="synthetic failure"):
        gap.activate(state.work, config, state.decision)
    assert state.calls[-1] == "close"
    assert all(state.calls.count(name) <= 1 for name in state.calls)
    with pytest.raises(ValueError):
        gap.activate(state.work, config, state.decision)


def test_advisory_probe_failure_never_stops_or_retries(state, monkeypatch):
    gap.release.save_once(gap.path(state.work, "worker-result"), {
        **gap.binding(state.decision), "execution_name": "synthetic-fifth",
    })
    candidate = {"expires_at": (state.clock + timedelta(seconds=120)).isoformat()}
    monkeypatch.setattr(gap, "amendment", lambda *a: candidate)
    monkeypatch.setattr(gap, "usage", lambda *a, **k: None)
    monkeypatch.setattr(gap, "sleep", lambda seconds: setattr(state, "clock", state.clock + timedelta(seconds=seconds)))

    def azure(*args, **kwargs):
        state.calls.append(args)
        if len(state.calls) == 1:
            raise OSError("usage probe unavailable")
        return [{"name": "synthetic-fifth", "properties": {"status": "Succeeded"}}]

    monkeypatch.setattr(gap.release, "azure", azure)
    gap.observe(state.work, {"subscription": "s", "group": "g", "job": "j"}, state.decision)
    assert len(list(state.work.glob("gapfill-warning-*.json"))) == 1
    assert all("stop" not in call and "start" not in call for call in state.calls)


def test_authenticated_external_receipt_required(state):
    gap.release.save_once(gap.path(state.work, "auth-preworker"), {
        **gap.binding(state.decision), "verified": True, "authenticated": False,
    })
    with pytest.raises(ValueError, match="authenticated"):
        gap.verify_auth(state.work, state.decision, {})


def test_one_backend_build_has_preexisting_capacity_receipt(state, monkeypatch):
    config = {"registry": "synthetic"}
    approval, previous = {}, {**config, "frontend_image": "unchanged-frontend"}
    monkeypatch.setattr(gap, "validate", lambda *a: (approval, previous))
    for name in ("verify_target", "identity_contract", "require_real_pilot_off"):
        monkeypatch.setattr(gap.release, name, lambda *a: None)
    monkeypatch.setattr(gap.prior, "ready", lambda *a: {})
    monkeypatch.setattr(gap.prior, "require_worker", lambda *a: {})
    monkeypatch.setattr(gap, "credential_binding", lambda *a, **k: {"name": "WEBIQ_API_KEY", "secretRef": "existing-gbbdemo"})
    monkeypatch.setattr(gap, "remote_snapshot", lambda *a: None)
    monkeypatch.setattr(gap.prior, "require_model_capacity", lambda *a: {"tokens_per_minute": 30000})

    def build(config, work, kind, revision, attempt, **kwargs):
        assert kind == "backend"
        assert gap.path(state.work, "capacity").exists()
        assert gap.release.private_json(attempt)["capacity_sha256"] == gap.digest_file(gap.path(state.work, "capacity"))
        assert kwargs["validate_upload_window"] is True
        state.calls.append(kind)
        return {"status": "Succeeded", "digest": "sha256:" + "a" * 64, "runId": "one-backend"}

    monkeypatch.setattr(gap.release, "execute_publication", build)
    gap.publish(state.work, config, state.decision)
    assert state.calls == ["backend"]
    with pytest.raises(ValueError):
        gap.publish(state.work, config, state.decision)
    assert state.calls == ["backend"]
    assert gap.receipt(state.work, "published", state.decision)["frontend_image"] == "unchanged-frontend"


def test_deployment_never_patches_frontend(state, monkeypatch):
    gap.path(state.work, "deployed").unlink()
    config = {"backend_image": "old-backend", "frontend_image": "unchanged-frontend"}
    current = {**config, "backend_image": "new-backend"}
    resources = {
        name: {"id": name, "properties": {"configuration": {}, "template": {
            "containers": [{"name": name, "image": config["frontend_image" if name == "frontend" else "backend_image"]}],
        }}} for name in ("backend", "frontend", "worker")
    }
    frontend = copy.deepcopy(resources["frontend"])
    monkeypatch.setattr(gap, "validate", lambda *a: ({}, config))
    monkeypatch.setattr(gap, "current_config", lambda *a: current)
    monkeypatch.setattr(gap.prior, "ready", lambda config, kind: resources[kind])
    monkeypatch.setattr(gap.prior, "require_worker", lambda *a: resources["worker"])
    monkeypatch.setattr(gap, "credential_binding", lambda *a, **k: {"name": "WEBIQ_API_KEY", "secretRef": "existing-gbbdemo"})
    monkeypatch.setattr(gap.release, "require_real_pilot_off", lambda *a: None)
    monkeypatch.setattr(gap, "remote_snapshot", lambda *a: None)

    def patch(work, config, decision, label, resource, containers, **kwargs):
        assert resource["id"] != "frontend"
        state.calls.append(label)
        resource["properties"]["template"]["containers"] = containers

    monkeypatch.setattr(gap, "patch", patch)
    gap.deploy(state.work, config, state.decision)
    assert state.calls == ["deploy-backend", "deploy-worker"]
    assert resources["frontend"] == frontend
    assert resources["worker"]["properties"]["template"]["containers"][0]["image"] == "new-backend"


def test_usage_failure_logs_and_never_kills(state, monkeypatch):
    gap.release.save_once(gap.path(state.work, "worker-result"), {
        **gap.binding(state.decision), "execution_name": "synthetic-fifth",
    })
    monkeypatch.setattr(gap, "amendment", lambda *a: {"expires_at": (state.clock + timedelta(seconds=120)).isoformat()})
    monkeypatch.setattr(gap.release, "azure", lambda *a, **k: [
        {"name": "synthetic-fifth", "properties": {"status": "Succeeded"}},
    ])
    def unavailable(*args, **kwargs):
        raise OSError("usage unavailable")
    monkeypatch.setattr(gap, "usage", unavailable)
    gap.observe(state.work, {"subscription": "s", "group": "g", "job": "j"}, state.decision)
    warning = list(state.work.glob("gapfill-warning-*.json"))
    assert len(warning) == 1
    assert gap.release.private_json(warning[0])["reason"] == "usage_unavailable"
    assert not gap.path(state.work, "expiry-stop-attempt").exists()


def test_existing_credential_reference_does_not_require_old_template_environment():
    job = {"properties": {"configuration": {"secrets": [{"name": "existing-gbbdemo"}]},
                          "template": {"containers": [{"name": "worker", "image": "synthetic", "env": []}]}}}
    before = copy.deepcopy(job)
    assert gap.credential_binding(job, {"webiq_secret_ref": "existing-gbbdemo"}) == {
        "name": "WEBIQ_API_KEY", "secretRef": "existing-gbbdemo",
    }
    assert job == before


def test_absent_key_is_explicit_but_cannot_start_readiness(state, monkeypatch):
    decision = {**state.decision, "webiq_secret_ref": None, "credential_source": "unavailable"}
    assert gap.credential_binding({}, decision) is None
    gap.path(state.work, "readiness").unlink()
    gap.path(state.work, "readiness-pin").unlink()
    monkeypatch.setattr(gap, "validate", lambda *a: None)
    with pytest.raises(ValueError, match="obtain owner input"):
        gap.ready(state.work, {}, decision)
    assert not gap.path(state.work, "readiness").exists()
    assert not gap.path(state.work, "readiness-pin").exists()


@pytest.mark.parametrize("secrets", [[], [{"name": "different"}], [{"name": "existing-gbbdemo", "value": "synthetic-forbidden"}]])
def test_missing_or_literal_credentials_are_not_created_or_serialized(secrets):
    job = {"properties": {"configuration": {"secrets": secrets},
                          "template": {"containers": [{"name": "worker", "image": "synthetic", "env": []}]}}}
    with pytest.raises(ValueError, match="Existing GBBdemo"):
        gap.credential_binding(job, {"webiq_secret_ref": "existing-gbbdemo"})


@pytest.mark.parametrize("reference", ["existing-gbbdemo", None])
def test_start_adds_only_existing_secret_reference_to_one_execution(state, monkeypatch, reference):
    config = {"subscription": "synthetic", "group": "g", "job": "j"}
    approval = {"batch_id": "b" * 64, "approved_by": "operator", "environment": {},
                "identities": {"worker_principal_id": "worker"}}
    job = {"properties": {"configuration": {"secrets": [{"name": "existing-gbbdemo"}]},
                          "template": {"containers": [{"name": "worker", "image": "synthetic", "env": []}]}}}
    candidate = {"web_environment": {"WEBSEARCH_PROVIDER": "webiq",
                                     "WEBIQ_ENDPOINT": "https://api.microsoft.ai/v3/search/web"},
                 "public_web_policy": {"items": {}}}
    monkeypatch.setattr(gap, "original", lambda *a: (approval, config))
    monkeypatch.setattr(gap, "active_target", lambda *a: config)
    monkeypatch.setattr(gap, "amendment", lambda *a: candidate)
    monkeypatch.setattr(gap, "verify_auth", lambda *a: None)
    monkeypatch.setattr(gap.prior, "require_worker", lambda *a: job)
    monkeypatch.setattr(gap.prior, "require_model_capacity", lambda *a: {"tokens_per_minute": 30000})
    monkeypatch.setattr(gap.release, "active_executions", lambda *a: (job, []))
    monkeypatch.setattr(gap.release, "azure", lambda *a, **k: {"name": "synthetic-fifth"})
    if reference is None:
        monkeypatch.setattr(gap, "credential_binding", lambda *a: None)
    gap.release.save_once(gap.path(state.work, "configured"), gap.binding(state.decision))
    gap.start(state.work, config, state.decision)
    template = gap.release.private_json(gap.path(state.work, "worker-template"))
    entries = gap.release.environment_entries(template["containers"][0])
    if reference is None:
        assert "WEBIQ_API_KEY" not in entries and "WEBIQ_SUBSCRIPTION_KEY" not in entries
    else:
        assert entries["WEBIQ_API_KEY"] == {"name": "WEBIQ_API_KEY", "secretRef": "existing-gbbdemo"}
    assert entries["WEBSEARCH_PROVIDER"]["value"] == "webiq"
    assert entries["WEBIQ_ENDPOINT"]["value"] == candidate["web_environment"]["WEBIQ_ENDPOINT"]
    assert job["properties"]["template"]["containers"][0]["env"] == []
    with pytest.raises(ValueError, match="consumed"):
        gap.start(state.work, config, state.decision)


def test_owner_key_file_is_existing_private_and_never_interpolated(state, monkeypatch):
    monkeypatch.setattr(gap.release, "ROOT", state.work)
    candidate = state.work / ".env.local"
    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write('WEBIQ_API_KEY="SYNTHETIC-EXISTING-KEY"\n')
    before = candidate.read_bytes()
    assert gap.existing_webiq_key() == "SYNTHETIC-EXISTING-KEY"
    assert candidate.read_bytes() == before
    candidate.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        gap.existing_webiq_key()


def test_owner_key_binding_is_planned_only_for_existing_empty_job_store(state, monkeypatch):
    decision = {**state.decision, "credential_source": "owner_env_file"}
    job = {"properties": {"configuration": {"secrets": None},
                          "template": {"containers": [{"name": "worker", "image": "synthetic", "env": []}]}}}
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: "SYNTHETIC-EXISTING-KEY")
    before = copy.deepcopy(job)
    assert gap.credential_binding(job, decision, allow_install=True) == {
        "name": "WEBIQ_API_KEY", "secretRef": "existing-gbbdemo",
    }
    assert job == before
    with pytest.raises(ValueError, match="unavailable"):
        gap.credential_binding(job, decision)
    job["properties"]["configuration"]["secrets"] = [{"name": "other-secret"}]
    with pytest.raises(ValueError, match="cannot overwrite"):
        gap.credential_binding(job, decision, allow_install=True)


def test_credential_transport_uses_stdin_only_and_does_not_change_redacted_payload(state, monkeypatch):
    config = {"subscription": "subscription", "tenant": "tenant", "group": "group", "job": "worker"}
    resource = {"id": "/subscriptions/subscription/resourceGroups/group/providers/Microsoft.App/jobs/worker",
                "etag": "synthetic-etag"}
    payload = {"properties": {"configuration": {"replicaTimeout": 600, "triggerType": "Manual",
                                               "secrets": [{"name": "existing-gbbdemo"}]},
                              "template": {"containers": [{"name": "worker", "image": "digest"}]}}}
    before = copy.deepcopy(payload)
    key, token = "SYNTHETIC-EXISTING-KEY", "SYNTHETIC-ACCESS-TOKEN"
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: key)
    monkeypatch.setenv("WEBIQ_API_KEY", key)
    monkeypatch.setattr(gap.release, "azure", lambda *a, **k: {
        "accessToken": token, "subscription": "subscription", "tenant": "tenant",
    })

    def transport(arguments, **kwargs):
        assert key not in repr(arguments) and token not in repr(arguments)
        assert key.encode() in kwargs["input"] and token.encode() in kwargs["input"]
        assert "WEBIQ_API_KEY" not in kwargs["env"]
        assert arguments[arguments.index("--retry") + 1] == "0"
        assert arguments[arguments.index("--config") + 1] == "-"
        return SimpleNamespace(returncode=0, stdout=b"200", stderr=b"")

    monkeypatch.setattr(gap.subprocess, "run", transport)
    gap.secure_worker_patch(state.work, config, state.decision, resource, payload)
    assert payload == before
    for candidate in state.work.glob("*.json"):
        assert key.encode() not in candidate.read_bytes() and token.encode() not in candidate.read_bytes()


@pytest.mark.parametrize("returncode,stdout,expected_status", [
    (22, b"400", 400), (22, b"503", 503), (7, b"", None), (22, b"000", None),
    (28, b"000", None),
    (22, b"400 SYNTHETIC-RESPONSE-BODY", None), (0, b"204", 204),
])
def test_credential_transport_failure_is_sanitized_without_retry(
    state, monkeypatch, returncode, stdout, expected_status,
):
    config = {"subscription": "subscription", "tenant": "tenant", "group": "group", "job": "worker"}
    resource = {"id": "/subscriptions/subscription/resourceGroups/group/providers/Microsoft.App/jobs/worker"}
    payload = {"properties": {"configuration": {"replicaTimeout": 600, "triggerType": "Manual",
                                               "secrets": [{"name": "existing-gbbdemo"}]}}}
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: "SYNTHETIC-KEY")
    monkeypatch.setattr(gap.release, "azure", lambda *a, **k: {"accessToken": "SYNTHETIC-TOKEN"})
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"SYNTHETIC-KEY SYNTHETIC-TOKEN")

    monkeypatch.setattr(gap.subprocess, "run", transport)
    with pytest.raises(gap.WorkerPatchFailure, match="details withheld, no retry") as error:
        gap.secure_worker_patch(state.work, config, state.decision, resource, payload)
    assert error.value.metadata() == {
        "phase": "worker_patch", "http_status": expected_status,
        "curl_returncode": returncode, "retry_permitted": False,
    }
    assert "SYNTHETIC" not in str(error.value) and calls == [1]


@pytest.mark.parametrize("phase", ["configuration", "credential", "management_token", "worker_patch"])
def test_credential_phase_failures_and_timeout_never_expose_raw_details(state, monkeypatch, phase):
    config = {"subscription": "subscription", "tenant": "tenant", "group": "group", "job": "worker"}
    resource = {"id": "/subscriptions/subscription/resourceGroups/group/providers/Microsoft.App/jobs/worker"}
    payload = {"properties": {"configuration": {"replicaTimeout": 600, "triggerType": "Manual",
                                               "secrets": [{"name": "existing-gbbdemo"}]}}}
    calls = []

    def credential():
        calls.append("credential")
        if phase == "credential":
            raise OSError("SYNTHETIC-KEY")
        return "SYNTHETIC-KEY"

    def token(*args, **kwargs):
        calls.append("management_token")
        if phase == "management_token":
            raise ValueError("SYNTHETIC-TOKEN")
        return {"accessToken": "SYNTHETIC-TOKEN"}

    def transport(*args, **kwargs):
        calls.append("worker_patch")
        raise gap.subprocess.TimeoutExpired(["curl"], 70, output=b"", stderr=b"SYNTHETIC-KEY SYNTHETIC-TOKEN")

    monkeypatch.setattr(gap, "existing_webiq_key", credential)
    monkeypatch.setattr(gap.release, "azure", token)
    monkeypatch.setattr(gap.subprocess, "run", transport)
    if phase == "configuration":
        payload["properties"]["configuration"]["secrets"] = [{"name": "SYNTHETIC-INVALID"}]
    with pytest.raises(gap.WorkerPatchFailure, match="details withheld, no retry") as error:
        gap.secure_worker_patch(state.work, config, state.decision, resource, payload)
    assert error.value.metadata() == {
        "phase": phase, "http_status": None, "curl_returncode": None, "retry_permitted": False,
    }
    assert error.value.__suppress_context__ and "SYNTHETIC" not in str(error.value)
    phases = ["credential", "management_token", "worker_patch"]
    assert calls == ([] if phase == "configuration" else phases[:phases.index(phase) + 1])


@pytest.mark.parametrize("trigger,field,settings", [
    ("Manual", "manualTriggerConfig", {"parallelism": 1, "replicaCompletionCount": 1}),
    ("Schedule", "scheduleTriggerConfig", {"cronExpression": "0 0 * * *", "parallelism": 1, "replicaCompletionCount": 1}),
    ("Event", "eventTriggerConfig", {
        "parallelism": 1, "replicaCompletionCount": 1,
        "scale": {"minExecutions": 0, "maxExecutions": 1, "pollingInterval": 30, "rules": []},
    }),
])
def test_job_projection_preserves_all_supported_settings_without_mutating_get(trigger, field, settings):
    configuration = {
        "triggerType": trigger, "replicaTimeout": 600, "replicaRetryLimit": 0,
        "manualTriggerConfig": None, "scheduleTriggerConfig": None, "eventTriggerConfig": None,
        "secrets": [{"name": "existing-reference"}],
        "registries": [{"server": "registry.invalid", "identity": "system",
                        "username": None, "passwordSecretRef": None}],
        "identitySettings": [], "dapr": None,
        field: settings,
    }
    before = copy.deepcopy(configuration)
    projected = gap.writable_job_configuration(configuration)
    expected = {key: value for key, value in before.items()
                if key not in {"identitySettings", "dapr"} and value is not None}
    expected["registries"] = [{"server": "registry.invalid", "identity": "system"}]
    assert projected == expected
    projected[field]["parallelism"] = 2
    projected["registries"][0]["identity"] = "changed"
    projected["secrets"].clear()
    assert configuration == before


@pytest.mark.parametrize("unsupported", [
    {"identitySettings": [{"identity": "system", "lifecycle": "Main"}]},
    {"dapr": {"enabled": True}},
    {"unknownWritableSetting": {"enabled": True}},
])
def test_job_projection_does_not_silently_drop_nonempty_unsupported_settings(unsupported):
    before = copy.deepcopy(unsupported)
    with pytest.raises(ValueError, match="2024-03-01"):
        gap.writable_job_configuration(unsupported)
    assert unsupported == before


@pytest.mark.parametrize("status,failed_receipt_write", [(200, False), (202, False), (400, False), (400, True)])
def test_worker_patch_preserves_configuration_and_records_no_key(state, monkeypatch, status, failed_receipt_write):
    decision = {**state.decision, "credential_source": "owner_env_file"}
    # Supply new matching local clock receipts for this synthetic decision.
    for name in ("readiness", "readiness-pin"):
        value = gap.release.private_json(gap.path(state.work, name))
        value.update(gap.binding(decision))
        gap.release.save(gap.path(state.work, name), value)
    pin = gap.release.private_json(gap.path(state.work, "readiness-pin"))
    pin["sha256"] = gap.digest_file(gap.path(state.work, "readiness"))
    gap.release.save(gap.path(state.work, "readiness-pin"), pin)
    config = {"subscription": "subscription", "tenant": "tenant", "group": "group", "job": "worker"}
    resource = {
        "id": "/subscriptions/subscription/resourceGroups/group/providers/Microsoft.App/jobs/worker",
        "etag": "synthetic-etag", "identity": {"type": "SystemAssigned"}, "properties": {
        "configuration": {"secrets": None, "replicaTimeout": 600, "replicaRetryLimit": 0,
                          "triggerType": "Manual", "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                          "eventTriggerConfig": None, "scheduleTriggerConfig": None,
                          "registries": [{"server": "registry.invalid", "identity": "system",
                                          "username": None, "passwordSecretRef": None}],
                          "dapr": None, "identitySettings": []},
        "template": {"containers": [{"name": "worker", "image": "old"}]},
    }}
    original = copy.deepcopy(resource)
    actual = copy.deepcopy(resource)
    containers = [{"name": "worker", "image": "new"}]
    key, token = "SYNTHETIC-KEY", "SYNTHETIC-TOKEN"
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: key)
    monkeypatch.setattr(gap.release, "azure", lambda *a, **k: {"accessToken": token})
    monkeypatch.setattr(gap.release, "active_executions", lambda *a: (copy.deepcopy(actual), []))
    requests = []
    if failed_receipt_write:
        save_once = gap.release.save_once

        def save(candidate, value):
            if candidate == gap.path(state.work, "deploy-worker-failure"):
                raise OSError("SYNTHETIC-PRIVATE-ERROR")
            return save_once(candidate, value)

        monkeypatch.setattr(gap.release, "save_once", save)

    def transport(arguments, **kwargs):
        encoded = next(line.removeprefix("data = ") for line in kwargs["input"].decode().splitlines()
                       if line.startswith("data = "))
        payload = json.loads(json.loads(encoded))
        requests.append(copy.deepcopy(payload))
        assert key not in repr(arguments) and token not in repr(arguments)
        assert "--retry" in arguments and arguments[arguments.index("--retry") + 1] == "0"
        submitted = payload["properties"]["configuration"]
        # The 2024 API rejects a newer GET shape; GET-only metadata survives successful PATCHes.
        response_status = 400 if {"identitySettings", "dapr"} & submitted.keys() else status
        if response_status in (200, 202):
            assert submitted["secrets"] == [{"name": "existing-gbbdemo", "value": key}]
            actual["properties"]["configuration"].update(copy.deepcopy(submitted))
            actual["properties"]["configuration"]["secrets"] = [{"name": "existing-gbbdemo"}]
            for registry in actual["properties"]["configuration"]["registries"]:
                registry.update(username=None, passwordSecretRef=None)
            actual["properties"]["template"]["containers"] = copy.deepcopy(containers)
        return SimpleNamespace(returncode=22 if response_status == 400 else 0,
                               stdout=str(response_status).encode(), stderr=b"SYNTHETIC-PRIVATE-ERROR")

    monkeypatch.setattr(gap.subprocess, "run", transport)
    if status == 400:
        with pytest.raises(gap.WorkerPatchFailure) as error:
            gap.patch(state.work, config, decision, "deploy-worker", resource, containers,
                      job=True, bind_existing_secret="existing-gbbdemo")
        assert error.value.metadata() == {
            "http_status": 400, "curl_returncode": 22, "phase": "worker_patch", "retry_permitted": False,
        }
        if failed_receipt_write:
            assert not gap.path(state.work, "deploy-worker-failure").exists()
            assert "SYNTHETIC" not in str(error.value) and error.value.__suppress_context__
        else:
            assert gap.receipt(state.work, "deploy-worker-failure", decision) == {
                **gap.binding(decision), **error.value.metadata(), "observed_at": state.clock.isoformat(),
            }
        retained = {candidate.name: candidate.read_bytes() for candidate in state.work.glob("*.json")}
        with pytest.raises(ValueError):
            gap.patch(state.work, config, decision, "deploy-worker", resource, containers,
                      job=True, bind_existing_secret="existing-gbbdemo")
        assert {candidate.name: candidate.read_bytes() for candidate in state.work.glob("*.json")} == retained
        assert actual == original
    else:
        gap.patch(state.work, config, decision, "deploy-worker", resource, containers,
                  job=True, bind_existing_secret="existing-gbbdemo")
        assert actual["properties"]["configuration"] == {
            **original["properties"]["configuration"], "secrets": [{"name": "existing-gbbdemo"}],
        }
        assert actual["properties"]["template"]["containers"] == containers
    assert resource == original and len(requests) == 1
    redacted = gap.release.private_json(gap.path(state.work, "deploy-worker-patch"))
    assert redacted["properties"]["configuration"] == {
        **{key: value for key, value in original["properties"]["configuration"].items()
           if key not in {"identitySettings", "dapr"} and value is not None},
        "registries": [{"server": "registry.invalid", "identity": "system"}],
        "secrets": [{"name": "existing-gbbdemo"}],
    }
    intent = gap.receipt(state.work, "credential-binding", decision)
    assert intent["value_persisted_locally"] is False and intent["credential_created"] is False
    assert all(candidate.stat().st_mode & 0o777 == 0o600 for candidate in state.work.glob("*.json"))
    assert all(b"SYNTHETIC-KEY" not in candidate.read_bytes()
               and b"SYNTHETIC-TOKEN" not in candidate.read_bytes()
               and b"SYNTHETIC-PRIVATE-ERROR" not in candidate.read_bytes()
               for candidate in state.work.glob("*.json"))


@pytest.mark.parametrize("invocation", ["api", "cli"])
def test_close_resolves_published_images_after_partial_deployment_without_reopening(state, monkeypatch, invocation):
    config = {"backend": "backend", "frontend": "frontend",
              "backend_image": "original-target-backend", "frontend_image": "original-target-frontend"}
    images = {"backend_image": "published-backend", "frontend_image": "retained-current-frontend"}
    original_config = copy.deepcopy(config)
    gap.path(state.work, "deployed").unlink()
    gap.release.save_once(gap.path(state.work, "published"), {**gap.binding(state.decision), **images})
    resources = {
        name: {"id": name, "properties": {"configuration": {}, "template": {
            "containers": [{"name": name, "image": images[name + "_image"], "env": []}],
        }}} for name in ("backend", "frontend")
    }
    resources["frontend"]["properties"]["template"]["containers"][0]["env"] = [{"name": "OTHER", "value": "retained"}]
    captured_frontend = gap.prior.shape(resources["frontend"])
    captured_frontend["template"]["containers"][0]["env"][0]["secretRef"] = None
    gap.release.save_once(gap.path(state.work, "deploy-attempt"), {
        **gap.binding(state.decision), "frontend": captured_frontend,
    })
    state.clock += timedelta(seconds=7200)

    def ready(current, kind):
        assert current == {**config, **images}
        state.calls.append(kind)
        return copy.deepcopy(resources[kind])

    monkeypatch.setattr(gap.release, "app", ready)
    monkeypatch.setattr(gap.prior, "ready", ready)
    monkeypatch.setattr(gap.release, "azure", lambda *a, **k: pytest.fail("No Azure transport"))
    monkeypatch.setattr(gap.release, "active_executions", lambda *a: pytest.fail("No worker action during closure"))
    monkeypatch.setattr(gap, "existing_webiq_key", lambda: pytest.fail("Closure must not read credentials"))
    monkeypatch.setattr(gap, "validate", lambda *a: pytest.fail("Closure must not need a gate or readiness window"))
    monkeypatch.setattr(gap, "patch", lambda *a, **k: pytest.fail("Already-closed processing must not be changed"))
    monkeypatch.setattr(gap.prior, "root", lambda *a: state.work)
    monkeypatch.setattr(gap.release, "load_config", lambda *a: copy.deepcopy(config))
    decision_file = state.work / "synthetic-decision.json"
    gap.release.save_once(decision_file, state.decision)
    monkeypatch.setattr(sys, "argv", [
        "scripts.gapfill_continuation", "close", "--work", str(state.work),
        "--decision", str(decision_file), "--approve", "close",
    ])
    retained = {candidate.name: candidate.read_bytes() for candidate in state.work.glob("*.json")}
    for attempt in range(2):
        if invocation == "api":
            gap.close(state.work, config, state.decision)
        else:
            gap.main()
        if attempt == 0:
            closed = gap.path(state.work, "closed").read_bytes()
        assert gap.path(state.work, "closed").read_bytes() == closed
    assert all((state.work / name).read_bytes() == value for name, value in retained.items())
    assert config == original_config and state.calls == ["backend", "backend", "frontend"] * 2
    assert not gap.path(state.work, "activation-attempt").exists()
    assert not gap.path(state.work, "deployed").exists()


def test_module_cli_help_requires_no_owner_configuration_or_credentials():
    result = gap.subprocess.run(
        [sys.executable, "-m", "scripts.gapfill_continuation", "--help"],
        cwd=gap.release.ROOT, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0 and b"--work" in result.stdout and not result.stderr


@pytest.mark.parametrize("expired_check", [None, 1, 2])
@pytest.mark.parametrize("entrypoint", ["wrapper", "generic"])
def test_secure_binding_reuses_explicit_owner_clock_without_old_namespace_or_receipt_writes(
    state, monkeypatch, expired_check, entrypoint,
):
    config = {"subscription": "subscription", "tenant": "tenant", "group": "group", "job": "worker"}
    resource = {"id": "/subscriptions/subscription/resourceGroups/group/providers/Microsoft.App/jobs/worker"}
    payload = {"properties": {"configuration": {"replicaTimeout": 600, "triggerType": "Manual",
                                               "secrets": [{"name": "existing-gbbdemo"}]}}}
    original = copy.deepcopy(payload)
    retained = {candidate.name: candidate.read_bytes() for candidate in state.work.glob("*.json")}
    events = []

    def check_window():
        events.append("window")
        if events.count("window") == expired_check:
            raise ValueError("New scope expired")

    def key():
        events.append("key")
        return "SYNTHETIC-KEY"

    def token(*args, **kwargs):
        events.append("token")
        return {"accessToken": "SYNTHETIC-TOKEN"}

    def transport(*args, **kwargs):
        events.append("patch")
        return SimpleNamespace(returncode=0, stdout=b"200", stderr=b"")

    monkeypatch.setattr(gap, "window", lambda *a, **k: pytest.fail("Old gapfill namespace must not be read"))
    monkeypatch.setattr(gap, "existing_webiq_key", key)
    monkeypatch.setattr(gap.release, "azure", token)
    monkeypatch.setattr(gap.subprocess, "run", transport)
    def send():
        if entrypoint == "generic":
            return gap.send_existing_secret_patch(
                config, resource, payload, secret_ref=state.decision["webiq_secret_ref"], check_window=check_window,
            )
        return gap.secure_worker_patch(state.work, config, state.decision, resource, payload, check_window=check_window)
    if expired_check is not None:
        with pytest.raises(ValueError):
            send()
        assert events == (["window"] if expired_check == 1 else ["window", "key", "token", "window"])
    else:
        send()
        assert events == ["window", "key", "token", "window", "patch"]
    assert payload == original
    assert {candidate.name: candidate.read_bytes() for candidate in state.work.glob("*.json")} == retained
