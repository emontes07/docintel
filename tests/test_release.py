import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_literal_secrets_are_never_captured():
    resource = {"properties": {"template": {"containers": [{"env": [{"name": "AUTH_SECRET", "value": "synthetic-private"}]}]}}}
    with pytest.raises(ValueError, match="secret reference"):
        release.safe_containers(resource)
    resource["properties"]["template"]["containers"][0]["env"] = [{"name": "AUTH_SECRET", "secretRef": "session-secret"}]
    assert release.safe_containers(resource)[0]["env"][0]["secretRef"] == "session-secret"


def test_unapproved_actions_never_reach_azure(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "load_config", lambda _: {})
    monkeypatch.setattr(release, "verify_target", lambda _: pytest.fail("Azure reached without approval"))
    for operation in release.OPERATIONS:
        from argparse import Namespace
        with pytest.raises(ValueError, match="Explicit approval"):
            release.run(Namespace(work=tmp_path, config=None, action=operation, approve=None))


def test_target_checks_tenant_as_well_as_subscription(monkeypatch):
    monkeypatch.setattr(release, "azure", lambda *args: {"id": "expected", "tenantId": "wrong"})
    with pytest.raises(ValueError, match="tenant/subscription"):
        release.verify_target({"subscription": "expected", "tenant": "expected-tenant"})


def test_context_excludes_private_material_and_environments(tmp_path):
    source = tmp_path / "source"
    for relative in ["backend/main.py", "frontend/package.json", "frontend/.env.local", "backend/static/customer.png", "backend/customer.pdf", "frontend/customer.xlsx", "frontend/private.key", "frontend/.clean-venv/bin/python", "frontend/.azure/private.json", "frontend/node_modules/private.json", "pyproject.toml", "uv.lock", ".dockerignore"]:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
    release.save(tmp_path / "source.json", {"revision": "a" * 40, "files": {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}})
    (source / "backend/untracked.py").write_text("private runtime artifact")
    context = release.build_context(tmp_path)
    paths = {path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()}
    assert paths == {"backend/main.py", "frontend/package.json", "pyproject.toml", "uv.lock", ".dockerignore"}


def test_desired_settings_use_references_and_drop_local_store():
    config = {"registry": "synthetic", "backend_image": "synthetic.azurecr.io/docintel/backend@sha256:" + "a" * 64, "frontend_image": "synthetic.azurecr.io/docintel/frontend@sha256:" + "b" * 64, "storage": "synthetic", "container": "batches", "tenant": "tenant", "api_client_id": "api", "frontend_client_id": "frontend", "frontend_origin": "https://portal.invalid", "backend_origin": "https://backend.invalid", "auth_secret_ref": "auth-reference", "entra_secret_ref": "entra-reference"}
    baseline = [{"image": "old", "env": [{"name": "DOCINTEL_BATCH_HOME", "value": "/private/local"}]}]
    backend = release.desired_containers(config, "backend", baseline)[0]
    assert not any(entry["name"] == "DOCINTEL_BATCH_HOME" for entry in backend["env"])
    frontend = release.desired_containers(config, "frontend", baseline)[0]
    assert {entry["name"]: entry["secretRef"] for entry in frontend["env"] if "secretRef" in entry} == {"AUTH_SECRET": "auth-reference", "AUTH_MICROSOFT_ENTRA_ID_SECRET": "entra-reference"}
    assert baseline[0]["image"] == "old"


def test_lock_changes_are_minimal_and_registry_is_unchanged():
    before = {"package": [{"name": "unrelated", "version": "1"}]}
    after = {"package": [*before["package"], {"name": "azure-ai-documentintelligence", "version": "1.0.2", "source": {"registry": "https://pypi.org/simple"}}]}
    release.check_lock(before, after)
    after["package"][0] = {"name": "unrelated", "version": "2"}
    with pytest.raises(ValueError, match="unrelated"):
        release.check_lock(before, after)
    after["package"][0] = before["package"][0]
    after["package"][1]["source"]["registry"] = "https://other.invalid/simple"
    with pytest.raises(ValueError, match="Registry"):
        release.check_lock(before, after)


def test_packaging_requires_new_access_evidence_before_any_command(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "command", lambda *args, **kwargs: pytest.fail("Download attempted"))
    with pytest.raises(ValueError, match="approved runner"):
        release.package(tmp_path, False)


def test_rollback_restores_configuration_without_deleting_resources(tmp_path, monkeypatch):
    from argparse import Namespace
    config = {"backend": "backend", "frontend": "frontend", "subscription": "subscription", "group": "group"}
    original = [{"image": "old:tag", "env": [{"name": "AUTH_SECRET", "secretRef": "retained-old-reference"}, {"name": "SETTING", "value": "old"}]}]
    current = [{"image": "new@sha256:digest", "env": [{"name": "AUTH_SECRET", "secretRef": "new-reference"}]}]
    baseline = {"target": release.fingerprint(config), "authenticated": False, "apps": {kind: {"containers": original, "pinned_image": "old@sha256:digest"} for kind in ("backend", "frontend")}}
    release.save(tmp_path / "baseline.json", baseline)
    for kind in ("backend", "frontend"):
        release.save(tmp_path / (kind + "-intended.json"), current)
    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "app", lambda *args: {"properties": {"template": {"containers": current}}})
    actions = []
    monkeypatch.setattr(release, "stop", lambda _: actions.append("stop"))
    monkeypatch.setattr(release, "azure", lambda *args: actions.append(args))
    monkeypatch.setattr(release, "patch_app", lambda config, kind, containers, work: actions.append((kind, containers)))
    options = Namespace(work=tmp_path, config=None, action="rollback", approve="rollback", isolate_legacy_baseline=False)
    with pytest.raises(ValueError, match="never restore it publicly"):
        release.run(options)
    assert not actions
    options.isolate_legacy_baseline = True
    release.run(options)
    assert actions[0] == "stop"
    assert all(action[:3] == ("containerapp", "ingress", "disable") for action in actions[1:3])
    for kind, containers in actions[3:]:
        assert kind in ("backend", "frontend")
        assert containers[0]["image"] == "old@sha256:digest"
        assert containers[0]["env"] == original[0]["env"]
    assert "delete" not in json.dumps(actions)


def test_changed_export_cannot_be_used_for_build(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "backend.py").write_text("changed")
    release.save(tmp_path / "source.json", {"files": {"backend.py": "original-hash"}})
    with pytest.raises(ValueError, match="source changed"):
        release.build_context(tmp_path)


@pytest.mark.parametrize("ready", [True, False])
def test_deploy_resumes_without_repatching_ready_backend(tmp_path, monkeypatch, ready):
    from argparse import Namespace
    config = {"backend": "backend", "frontend": "frontend", "subscription": "subscription", "group": "group", "auth_secret_ref": "auth-ref", "entra_secret_ref": "entra-ref"}
    original = [{"image": "old", "env": []}]
    desired = [{"image": "new", "env": []}]
    release.save(tmp_path / "baseline.json", {"target": release.fingerprint(config), "apps": {kind: {"containers": original} for kind in ("backend", "frontend")}})
    state = {"backend": desired, "frontend": original}
    mutations = []
    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "require_release_images", lambda *args: None)
    monkeypatch.setattr(release, "identity_contract", lambda _: None)
    monkeypatch.setattr(release, "desired_containers", lambda *args: desired)
    monkeypatch.setattr(release, "azure", lambda *args: {"properties": {"healthState": "Healthy", "template": {"containers": desired}}})
    def resource(config, kind):
        return {"properties": {"provisioningState": "Succeeded" if ready else "Updating", "template": {"containers": state[kind]}, "configuration": {"secrets": [{"name": "auth-ref"}, {"name": "entra-ref"}]}, "latestRevisionName": "ready", "latestReadyRevisionName": "ready"}}
    def patch(config, kind, containers, work):
        mutations.append(kind)
        state[kind] = containers
    monkeypatch.setattr(release, "app", resource)
    monkeypatch.setattr(release, "patch_app", patch)
    options = Namespace(work=tmp_path, config=None, action="deploy", approve="deploy")
    if ready:
        release.run(options)
        assert mutations == ["frontend"]
    else:
        with pytest.raises(ValueError, match="not ready"):
            release.run(options)
        assert not mutations