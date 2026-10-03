import importlib.util
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def container_read_response():
    return {"identity": {"type": "SystemAssigned"}, "properties": {
        "configuration": {"registries": [{"server": "synthetic.azurecr.io", "passwordSecretRef": "acr-password"}]},
        "template": {"volumes": [{"name": "cache", "storageType": "EmptyDir"}], "containers": [{
            "name": "synthetic", "image": "synthetic.azurecr.io/old:tag", "imageType": "ContainerImage",
            "command": ["python"], "args": ["-m", "backend.main"],
            "env": [{"name": "SETTING", "value": "retained"}, {"name": "AUTH_SECRET", "secretRef": "retained-reference"}],
            "resources": {"cpu": 1, "memory": "2Gi", "ephemeralStorage": "4Gi"},
            "probes": [{"type": "Readiness", "httpGet": {"path": "/health", "port": 80, "scheme": "HTTP", "httpHeaders": [{"name": "X-Probe", "value": "synthetic"}]}, "initialDelaySeconds": 1, "periodSeconds": 10, "timeoutSeconds": 2, "failureThreshold": 3, "successThreshold": 1}],
            "volumeMounts": [{"volumeName": "cache", "mountPath": "/cache", "subPath": "retained"}],
        }]}}}


@pytest.mark.parametrize("kind", ["backend", "frontend"])
def test_patch_projects_read_response_without_mutation(tmp_path, monkeypatch, container_read_response, kind):
    original = copy.deepcopy(container_read_response)
    config = {"subscription": "synthetic", "group": "synthetic", kind: kind}
    containers = release.safe_containers(container_read_response)
    containers[0]["image"] = "synthetic.azurecr.io/new@sha256:" + "a" * 64
    containers[0]["env"].append({"name": "AUTH_MICROSOFT_ENTRA_ID_SECRET", "secretRef": "entra-reference"})
    calls = []
    monkeypatch.setattr(release, "azure", lambda *arguments: calls.append(arguments))
    release.patch_app(config, kind, containers, tmp_path)
    payload = json.loads((tmp_path / (kind + "-patch.json")).read_text())
    expected = copy.deepcopy(containers)
    expected[0].pop("imageType")
    expected[0]["resources"].pop("ephemeralStorage")
    assert payload == {"properties": {"template": {"containers": expected}}}
    assert container_read_response == original
    assert containers[0]["imageType"] == "ContainerImage"
    assert "api-version=2024-03-01" in calls[0][calls[0].index("--url") + 1]
    assert calls[0][calls[0].index("--method") + 1] == "PATCH"


@pytest.mark.parametrize("change", [{"imageType": "Artifact"}, {"futureSetting": True}, {"resources": {"gpu": 1}}])
def test_projection_rejects_unknown_configuration(container_read_response, change):
    containers = release.safe_containers(container_read_response)
    containers[0].update(change)
    with pytest.raises(ValueError, match="Unknown|Unsupported"):
        release.writable_containers(containers)


@pytest.mark.parametrize("action", ["deploy", "rollback"])
def test_deploy_and_rollback_use_write_projection(tmp_path, monkeypatch, container_read_response, action):
    from argparse import Namespace
    config = {"backend": "backend", "frontend": "frontend", "subscription": "synthetic", "group": "synthetic", "registry": "synthetic", "backend_image": "synthetic.azurecr.io/docintel/backend@sha256:" + "a" * 64, "frontend_image": "synthetic.azurecr.io/docintel/frontend@sha256:" + "b" * 64, "storage": "synthetic", "container": "batches", "tenant": "tenant", "api_client_id": "api", "frontend_client_id": "frontend", "frontend_origin": "https://portal.invalid", "backend_origin": "https://backend.invalid", "auth_secret_ref": "auth-reference", "entra_secret_ref": "entra-reference"}
    original = release.safe_containers(container_read_response)
    baseline = {"target": release.fingerprint(config), "authenticated": False, "apps": {kind: {"containers": original, "pinned_image": "synthetic.azurecr.io/old@sha256:" + "c" * 64} for kind in ("backend", "frontend")}}
    release.save(tmp_path / "baseline.json", baseline)
    resources = {kind: copy.deepcopy(container_read_response) for kind in ("backend", "frontend")}
    for kind, resource in resources.items():
        resource["properties"].update(provisioningState="Succeeded", latestRevisionName="ready", latestReadyRevisionName="ready")
        resource["properties"]["configuration"]["secrets"] = [{"name": reference} for reference in ("auth-reference", "entra-reference")]
        if action == "rollback":
            intended = release.desired_containers(config, kind, original)
            resource["properties"]["template"]["containers"] = intended
            release.save(tmp_path / (kind + "-intended.json"), intended)
    before = copy.deepcopy(resources)
    calls = []
    payloads = {}
    def azure(*arguments):
        calls.append(arguments)
        if arguments[:1] == ("rest",):
            assert arguments[arguments.index("--method") + 1] == "PATCH"
            assert arguments[arguments.index("--url") + 1].endswith("?api-version=2024-03-01")
            path = Path(arguments[arguments.index("--body") + 1][1:])
            kind = path.name.removesuffix("-patch.json")
            payload = json.loads(path.read_text())
            payloads[kind] = payload
            assert set(payload) == {"properties"}
            assert set(payload["properties"]) == {"template"}
            assert set(payload["properties"]["template"]) == {"containers"}
            resources[kind]["properties"]["template"]["containers"] = payload["properties"]["template"]["containers"]
        elif arguments[:3] == ("containerapp", "revision", "show"):
            kind = arguments[arguments.index("-n") + 1]
            return {"properties": {"healthState": "Healthy", "template": resources[kind]["properties"]["template"]}}
    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "require_release_images", lambda *args: None)
    monkeypatch.setattr(release, "identity_contract", lambda _: None)
    monkeypatch.setattr(release, "app", lambda _, kind: resources[kind])
    monkeypatch.setattr(release, "stop", lambda _: calls.append(("stop",)))
    release.run(Namespace(action=action, approve=action, work=tmp_path, config=None, isolate_legacy_baseline=True))
    assert set(payloads) == {"backend", "frontend"}
    for kind, payload in payloads.items():
        expected = release.desired_containers(config, kind, original) if action == "deploy" else copy.deepcopy(original)
        if action == "rollback":
            expected[0]["image"] = baseline["apps"][kind]["pinned_image"]
        expected[0].pop("imageType")
        expected[0]["resources"].pop("ephemeralStorage")
        assert payload["properties"]["template"]["containers"] == expected
        assert resources[kind]["identity"] == before[kind]["identity"]
        assert resources[kind]["properties"]["configuration"] == before[kind]["properties"]["configuration"]
        assert resources[kind]["properties"]["template"]["volumes"] == before[kind]["properties"]["template"]["volumes"]
    if action == "rollback":
        assert calls[0] == ("stop",)
        assert all(call[:3] == ("containerapp", "ingress", "disable") for call in calls[1:3])


def test_literal_secrets_are_never_captured():
    resource = {"properties": {"template": {"containers": [{"env": [{"name": "AUTH_SECRET", "value": "synthetic-private"}]}]}}}
    with pytest.raises(ValueError, match="secret reference"):
        release.safe_containers(resource)
    resource["properties"]["template"]["containers"][0]["env"] = [{"name": "AUTH_SECRET", "secretRef": "session-secret"}]
    assert release.safe_containers(resource)[0]["env"][0]["secretRef"] == "session-secret"


def test_synthetic_fixture_checks_both_products_and_export(tmp_path, monkeypatch):
    from scripts import release_fixture
    monkeypatch.setenv("DOCINTEL_BATCH_LIVE_ENABLED", "false")
    output = tmp_path / "fixture"
    release_fixture.prepare(output)
    content, batch_id, owner, hashes = release_fixture.verify(output)
    assert release_fixture.verify_export(content, batch_id, owner, hashes)
    with pytest.raises(AssertionError):
        release_fixture.verify_export(content, batch_id, "other-owner", hashes)
    with pytest.raises(AssertionError):
        release_fixture.verify_export(content, batch_id, owner, {"2": hashes["2"], "3": "changed"})


@pytest.mark.parametrize("sheet,column,value", [("Inputs", "MPN", "WRONG"), ("Results", "Proposed value", "invented"), ("Provenance", "Execution method", "live")])
def test_synthetic_export_rejects_second_product_corruption(tmp_path, monkeypatch, sheet, column, value):
    from scripts import release_fixture
    monkeypatch.setenv("DOCINTEL_BATCH_LIVE_ENABLED", "false")
    output = tmp_path / "fixture"
    release_fixture.prepare(output)
    content, batch_id, owner, hashes = release_fixture.verify(output)
    workbook = release_fixture.read_workbook(content)
    workbook[sheet][1][column] = value
    monkeypatch.setattr(release_fixture, "read_workbook", lambda _: workbook)
    with pytest.raises(AssertionError):
        release_fixture.verify_export(content, batch_id, owner, hashes)


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


def test_stage_protects_files_without_following_dangling_tool_links(tmp_path, monkeypatch):
    import io
    import tarfile
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as contents:
        file = tarfile.TarInfo("application.txt")
        file.mode = 0o644
        file.size = len(b"synthetic")
        contents.addfile(file, io.BytesIO(b"synthetic"))
        link = tarfile.TarInfo("tool-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "absent-tool"
        contents.addfile(link)
    monkeypatch.setattr(release, "command", lambda *args, **kwargs: archive.getvalue())
    release.stage("a" * 40, tmp_path)
    assert (tmp_path / "source/tool-link").is_symlink()
    assert (tmp_path / "source/application.txt").stat().st_mode & 0o777 == 0o600
    assert set(json.loads((tmp_path / "source.json").read_text())["files"]) == {"application.txt"}


def test_restrictive_context_preserves_private_permissions(tmp_path):
    source = tmp_path / "source"
    for relative in ("backend/main.py", "frontend/public/mock-gens/synthetic.txt", "frontend/Dockerfile", "pyproject.toml", "uv.lock", ".dockerignore"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
    for path in source.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    release.save(tmp_path / "source.json", {"revision": "a" * 40, "files": {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}})
    context = release.build_context(tmp_path)
    assert (context / "frontend/public/mock-gens").stat().st_mode & 0o777 == 0o700
    assert (context / "frontend/public/mock-gens/synthetic.txt").stat().st_mode & 0o777 == 0o600
    modes = json.loads((tmp_path / "context-modes.json").read_text())
    assert modes["frontend/public/mock-gens"] == 0o700
    assert (tmp_path / "context-modes.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("defect", [None, "smoke", "mode", "backend", "failed_build"])
def test_frontend_only_publication_preserves_backend_and_refuses_retry(tmp_path, monkeypatch, defect):
    previous = tmp_path / "previous"
    work = tmp_path / "replacement"
    config = {"registry": "synthetic", "subscription": "synthetic", "backend_image": "synthetic.azurecr.io/docintel/backend@sha256:" + "b" * 64}
    previous_images = {"revision": "b" * 40, "backend": config["backend_image"]}
    release.save(previous / "source.json", {"revision": "b" * 40, "files": {}})
    release.save(previous / "images.json", previous_images)
    context = work / "context"
    (context / "frontend").mkdir(parents=True)
    (context / "frontend/Dockerfile").write_text("FROM synthetic")
    inventory = {"frontend/Dockerfile": hashlib.sha256(b"FROM synthetic").hexdigest()}
    modes = {str(path.relative_to(context)): path.stat().st_mode & 0o777 for path in context.rglob("*")}
    source = {"revision": "a" * 40, "files": inventory}
    release.save(work / "source.json", source)
    release.save(work / "context.json", inventory)
    release.save(work / "context-modes.json", modes)
    release.save(work / "clean-checks.json", {"revision": source["revision"], "lock_sha256": hashlib.sha256(b"lock").hexdigest()})
    release.save(work / "frontend-smoke.json", {"revision": source["revision"], "passed": defect != "smoke", "context_sha256": release.fingerprint(inventory), "modes_sha256": release.fingerprint(modes)})
    monkeypatch.setattr(release, "verify_source", lambda path: json.loads((path / "source.json").read_text()))
    monkeypatch.setattr(release, "command", lambda args, **kwargs: b"lock" if "show" in args else b"a" * 40)
    calls = []
    def azure(*arguments):
        calls.append(arguments)
        if arguments[:2] == ("acr", "build"):
            assert arguments[arguments.index("--timeout") + 1] == "900"
            assert arguments[arguments.index("--image") + 1].startswith("docintel/frontend:")
            return {"status": "Failed" if defect == "failed_build" else "Succeeded", "runId": "synthetic-run"}
        return {"digest": "sha256:" + "c" * 64}
    monkeypatch.setattr(release, "azure", azure)
    if defect == "mode":
        (context / "frontend/Dockerfile").chmod(0o400)
    if defect == "backend":
        config["backend_image"] = config["backend_image"].replace("b" * 64, "d" * 64)
    if defect:
        with pytest.raises(ValueError):
            release.publish_frontend(config, work, previous)
        assert len(calls) == (1 if defect == "failed_build" else 0)
    else:
        release.publish_frontend(config, work, previous)
        receipt = json.loads((work / "images.json").read_text())
        assert receipt["backend"] == previous_images["backend"]
        assert receipt["preserved_backend"]["revision"] == "b" * 40
        release.require_release_images({**config, "frontend_image": receipt["frontend"]}, work)
    if defect in (None, "failed_build"):
        count = len(calls)
        with pytest.raises(ValueError, match="already attempted"):
            release.publish_frontend(config, work, previous)
        assert len(calls) == count
    assert json.loads((previous / "images.json").read_text()) == previous_images


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


@pytest.mark.parametrize("build_status", ["Succeeded", "Failed"])
def test_publication_uses_verified_absolute_dockerfiles_and_bounded_builds(tmp_path, monkeypatch, build_status):
    from argparse import Namespace
    config = {"registry": "synthetic", "subscription": "subscription"}
    source = {"revision": "a" * 40, "files": {}}
    for relative in ("backend/Dockerfile", "frontend/Dockerfile"):
        path = tmp_path / "context" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FROM synthetic")
        source["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    release.save(tmp_path / "source.json", source)
    release.save(tmp_path / "context.json", source["files"])
    release.save(tmp_path / "clean-checks.json", {"revision": source["revision"], "lock_sha256": hashlib.sha256(b"lock").hexdigest()})
    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_source", lambda _: source)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "command", lambda *args, **kwargs: b"lock")
    builds = []
    def azure(*args):
        if args[:2] == ("acr", "build"):
            builds.append(args)
            return {"status": build_status, "runId": "run-" + str(len(builds))}
        return {"digest": "sha256:" + "b" * 64}
    monkeypatch.setattr(release, "azure", azure)
    options = Namespace(action="publish", work=tmp_path, config=None, approve="publish")
    if build_status == "Failed":
        with pytest.raises(ValueError, match="did not succeed"):
            release.run(options)
        assert len(builds) == 1
        assert not (tmp_path / "images.json").exists()
    else:
        release.run(options)
        assert len(builds) == 2
        assert json.loads((tmp_path / "images.json").read_text())["frontend_run_id"] == "run-2"
    for arguments in builds:
        dockerfile = Path(arguments[arguments.index("--file") + 1])
        assert dockerfile.is_absolute() and dockerfile.is_file()
        assert dockerfile.is_relative_to(Path(arguments[-1]))
        assert arguments[arguments.index("--timeout") + 1] == "900"


@pytest.mark.parametrize("nonempty", [False, True])
def test_private_seed_checks_environment_empty_container_and_hashes(monkeypatch, nonempty):
    from types import SimpleNamespace
    import azure.identity
    import azure.storage.blob
    content = b"synthetic"
    payload = {name: {"content": base64.b64encode(content).decode(), "sha256": hashlib.sha256(content).hexdigest()} for name in ("documents/release-synthetic.pdf", "configuration/sources.json", "parses/" + "a" * 64 + ".json")}
    blobs = {"existing": b"retained"} if nonempty else {}
    writes = []
    class Container:
        def list_blobs(self):
            return [SimpleNamespace(name=name) for name in blobs]
        def upload_blob(self, name, value, overwrite):
            assert overwrite is False and name not in blobs
            writes.append(name)
            blobs[name] = value
        def download_blob(self, name):
            return SimpleNamespace(readall=lambda: blobs[name])
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", lambda: "managed")
    monkeypatch.setattr(azure.storage.blob, "BlobServiceClient", lambda *args, **kwargs: SimpleNamespace(get_container_client=lambda _: Container()))
    monkeypatch.setenv("DOCINTEL_BATCH_MODE", "hosted")
    monkeypatch.setenv("DOCINTEL_BATCH_LIVE_ENABLED", "false")
    monkeypatch.setenv("DOCINTEL_BATCH_STORAGE_URL", "https://synthetic.blob.core.windows.net")
    monkeypatch.setenv("DOCINTEL_BATCH_CONTAINER", "batches")
    code = release.remote_seed_code({"storage": "synthetic", "container": "batches"}, payload)
    if nonempty:
        with pytest.raises(AssertionError, match="nonempty"):
            exec(code, {})
        assert not writes
    else:
        exec(code, {})
        assert len(writes) == 3
    monkeypatch.setenv("DOCINTEL_BATCH_LIVE_ENABLED", "true")
    with pytest.raises(AssertionError):
        exec(code, {})


def test_private_seed_requires_explicit_remote_confirmation(tmp_path, monkeypatch):
    config = {"backend": "backend", "backend_image": "synthetic.azurecr.io/docintel/backend@sha256:" + "a" * 64, "registry": "synthetic", "storage": "synthetic", "container": "batches", "subscription": "subscription", "group": "group"}
    resource = {"properties": {"provisioningState": "Succeeded", "latestRevisionName": "ready", "latestReadyRevisionName": "ready", "template": {"containers": [{"image": config["backend_image"]}]}}}
    monkeypatch.setattr(release, "app", lambda *args: resource)
    fixture = tmp_path / "fixture"
    files = {}
    for name in ("documents/release-synthetic.pdf", "configuration/sources.json", "parses/" + "a" * 64 + ".json"):
        path = fixture / "seed" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
        files["seed/" + name] = hashlib.sha256(b"synthetic").hexdigest()
    monkeypatch.setattr(release, "command", lambda *args, **kwargs: b"remote process failed but CLI exited zero")
    with pytest.raises(ValueError, match="did not confirm"):
        release.seed_via_backend(config, fixture, files, tmp_path)
    assert not (tmp_path / "seed.json").exists()
    def command(arguments):
        assert arguments[arguments.index("--revision") + 1] == "ready"
        remote = arguments[-1].split(" -c ", 1)[1]
        encoded = remote.split("b64decode('", 1)[1].split("'", 1)[0]
        code = base64.b64decode(encoded).decode()
        marker = code.split("print('", 1)[1].split("'", 1)[0]
        return (marker + "\r\n").encode()
    monkeypatch.setattr(release, "command", command)
    release.seed_via_backend(config, fixture, files, tmp_path)
    assert len(json.loads((tmp_path / "seed.json").read_text())["files"]) == 3
    resource["properties"]["template"]["containers"][0]["image"] = "wrong"
    with pytest.raises(ValueError, match="differs"):
        release.seed_via_backend(config, fixture, files, tmp_path)


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