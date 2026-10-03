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


def test_console_transport_handles_long_code_and_remote_error():
    import sys
    marker = "SYNTHETIC_CONSOLE_VALIDATED"
    code = "value=" + repr("synthetic" * 2000) + "\nassert len(value)==18000\nprint(" + repr(marker) + ")"
    output = release.console_code([sys.executable, "-q"], code, marker, timeout=10)
    assert marker in output.decode().splitlines()
    with pytest.raises(ValueError, match="did not confirm"):
        release.console_code([sys.executable, "-q"], "raise ValueError('synthetic failure')", marker, timeout=2)


@pytest.mark.parametrize("executions", [[], [{"name": "already-used"}]])
def test_worker_image_patch_preserves_configuration_and_never_starts(tmp_path, monkeypatch, executions):
    config = {"subscription": "synthetic", "group": "synthetic", "job": "worker", "registry": "synthetic", "backend_image": "synthetic.azurecr.io/docintel/backend@sha256:" + "a" * 64}
    job = {"id": "/subscriptions/synthetic/resourceGroups/synthetic/providers/Microsoft.App/jobs/worker", "identity": {"type": "UserAssigned"}, "properties": {"configuration": {"triggerType": "Manual", "replicaTimeout": 600, "replicaRetryLimit": 0, "registries": [{"server": "synthetic.azurecr.io", "identity": "preserved"}], "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1}}, "template": {"containers": [{"name": "worker", "image": "old", "command": ["python"], "args": ["-m", "backend.batch_worker"], "env": [{"name": "DOCINTEL_BATCH_LIVE_ENABLED", "value": "false"}], "resources": {"cpu": 1, "memory": "2Gi"}}]}}}
    release.save(tmp_path / "worker-baseline.json", {"target": release.fingerprint(config), "resource": job})
    before = copy.deepcopy(job)
    calls = []
    def azure(*arguments):
        calls.append(arguments)
        if arguments[:3] == ("containerapp", "job", "show"):
            return job
        if arguments[:4] == ("containerapp", "job", "execution", "list"):
            return executions
        assert arguments[:3] == ("rest", "--method", "PATCH")
    monkeypatch.setattr(release, "azure", azure)
    if executions:
        with pytest.raises(ValueError, match="unused"):
            release.update_worker_image(config, tmp_path)
        assert not (tmp_path / "worker-image-patch.json").exists()
    else:
        release.update_worker_image(config, tmp_path)
        expected = copy.deepcopy(job["properties"]["template"]["containers"])
        expected[0]["image"] = config["backend_image"]
        assert json.loads((tmp_path / "worker-image-patch.json").read_text()) == {"properties": {"template": {"containers": expected}}}
        with pytest.raises(ValueError, match="already attempted"):
            release.update_worker_image(config, tmp_path)
        assert sum(arguments[0] == "rest" for arguments in calls) == 1
    assert job == before
    assert not any("start" in arguments for arguments in calls)


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


@pytest.mark.parametrize("defect", [None, "smoke", "mode", "backend", "failed_build", "unknown_build"])
@pytest.mark.parametrize("kind", ["frontend", "backend"])
def test_component_publication_preserves_other_image_and_refuses_retry(tmp_path, monkeypatch, defect, kind):
    retained = "frontend" if kind == "backend" else "backend"
    previous = tmp_path / "previous"
    work = tmp_path / "replacement"
    config = {"registry": "synthetic", "subscription": "synthetic", retained + "_image": "synthetic.azurecr.io/docintel/" + retained + "@sha256:" + "b" * 64}
    previous_images = {"revision": "b" * 40, retained: config[retained + "_image"]}
    release.save(previous / "source.json", {"revision": "b" * 40, "files": {}})
    release.save(previous / "images.json", previous_images)
    context = work / "context"
    (context / kind).mkdir(parents=True)
    (context / kind / "Dockerfile").write_text("FROM synthetic")
    inventory = {kind + "/Dockerfile": hashlib.sha256(b"FROM synthetic").hexdigest()}
    modes = {str(path.relative_to(context)): path.stat().st_mode & 0o777 for path in context.rglob("*")}
    source = {"revision": "a" * 40, "files": inventory}
    release.save(work / "source.json", source)
    release.save(work / "context.json", inventory)
    release.save(work / "context-modes.json", modes)
    release.save(work / "clean-checks.json", {"revision": source["revision"], "lock_sha256": hashlib.sha256(b"lock").hexdigest()})
    release.save(work / (kind + "-smoke.json"), {"revision": source["revision"], "passed": defect != "smoke", "context_sha256": release.fingerprint(inventory), "modes_sha256": release.fingerprint(modes)})
    monkeypatch.setattr(release, "verify_source", lambda path: json.loads((path / "source.json").read_text()))
    monkeypatch.setattr(release, "command", lambda args, **kwargs: b"lock" if "show" in args else b"a" * 40)
    calls = []
    def azure(*arguments):
        calls.append(arguments)
        if arguments[:2] == ("acr", "build"):
            assert arguments[arguments.index("--timeout") + 1] == "900"
            assert arguments[arguments.index("--cpu") + 1] == "2"
            attempt = release.private_json(work / f"{kind}-publication-attempt.json")
            assert attempt["cpu"] == 2 and attempt["timeout_seconds"] == 900
            assert arguments[arguments.index("--image") + 1].startswith("docintel/" + kind + ":")
            assert arguments[arguments.index("--file") + 1] == str(context / kind / "Dockerfile")
            assert arguments[-1] == str(context if kind == "backend" else context / "frontend")
            if defect == "unknown_build":
                raise ValueError("Synthetic unknown build outcome")
            return {"status": "Failed" if defect == "failed_build" else "Succeeded", "runId": "synthetic-run"}
        return {"digest": "sha256:" + "c" * 64}
    monkeypatch.setattr(release, "azure", azure)
    if defect == "mode":
        (context / kind / "Dockerfile").chmod(0o400)
    if defect == "backend":
        config[retained + "_image"] = config[retained + "_image"].replace("b" * 64, "d" * 64)
    if defect:
        with pytest.raises(ValueError):
            release.publish_component(config, work, previous, kind)
        assert len(calls) == (1 if defect in ("failed_build", "unknown_build") else 0)
    else:
        release.publish_component(config, work, previous, kind)
        receipt = json.loads((work / "images.json").read_text())
        assert receipt[retained] == previous_images[retained]
        assert receipt["preserved_" + retained]["revision"] == "b" * 40
        release.require_release_images({**config, kind + "_image": receipt[kind]}, work)
    if defect in (None, "failed_build", "unknown_build"):
        count = len(calls)
        attempt_before = (work / f"{kind}-publication-attempt.json").read_bytes()
        with pytest.raises(ValueError, match="already attempted"):
            release.publish_component(config, work, previous, kind)
        assert len(calls) == count
        from argparse import Namespace
        monkeypatch.setattr(release, "load_config", lambda _: config)
        monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
        with pytest.raises(ValueError, match="already attempted"):
            release.run(Namespace(action="publish", approve="publish", work=work, config=None))
        assert len(calls) == count
        assert (work / f"{kind}-publication-attempt.json").read_bytes() == attempt_before
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


@pytest.mark.parametrize("build_status", ["Succeeded", "Failed", "Unknown"])
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
            kind = "backend" if len(builds) == 1 else "frontend"
            attempt = release.private_json(tmp_path / f"{kind}-publication-attempt.json")
            assert attempt["component"] == kind and attempt["cpu"] == 2 and attempt["timeout_seconds"] == 900
            if build_status == "Unknown":
                raise ValueError("Synthetic unknown build outcome")
            return {"status": build_status, "runId": "run-" + str(len(builds))}
        return {"digest": "sha256:" + "b" * 64}
    monkeypatch.setattr(release, "azure", azure)
    options = Namespace(action="publish", work=tmp_path, config=None, approve="publish")
    if build_status in ("Failed", "Unknown"):
        with pytest.raises(ValueError, match="did not succeed|unknown"):
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
        assert arguments[arguments.index("--cpu") + 1] == "2"
    count = len(builds)
    attempts = {path.name: path.read_bytes() for path in tmp_path.glob("*-publication-attempt.json")}
    with pytest.raises(ValueError, match="already attempted"):
        release.run(options)
    with pytest.raises(ValueError, match="already attempted"):
        release.publish_component(config, tmp_path, None, "backend")
    assert len(builds) == count
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*-publication-attempt.json")} == attempts


def test_publication_attempt_reservation_is_atomic_and_component_scoped(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    barrier = threading.Barrier(4)

    def reserve(kind):
        barrier.wait()
        try:
            release.reserve_publication_attempt(tmp_path, kind, "a" * 40)
            return kind
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(reserve, ("backend", "backend", "frontend", "frontend")))
    assert outcomes.count("backend") == outcomes.count("frontend") == 1
    assert len(list(tmp_path.glob("*-publication-attempt.json"))) == 2
    for kind in ("backend", "frontend"):
        before = (tmp_path / f"{kind}-publication-attempt.json").read_bytes()
        with pytest.raises(ValueError, match="already attempted"):
            release.reserve_publication_attempt(tmp_path, kind, "b" * 40)
        assert (tmp_path / f"{kind}-publication-attempt.json").read_bytes() == before


@pytest.mark.parametrize("legacy", ["backend-publication-attempt.json", "frontend-publication-result.json", "publication-progress.json", "images.json"])
def test_publication_does_not_reset_or_migrate_existing_records(tmp_path, legacy):
    content = {"legacy": "retained"}
    release.save(tmp_path / legacy, content)
    before = (tmp_path / legacy).read_bytes()
    with pytest.raises(ValueError, match="already attempted"):
        release.require_unused_publication(tmp_path, ("backend", "frontend"))
    assert (tmp_path / legacy).read_bytes() == before


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
    monkeypatch.setattr(release, "console_code", lambda *args, **kwargs: b"remote process failed but CLI exited zero")
    with pytest.raises(ValueError, match="did not confirm"):
        release.seed_via_backend(config, fixture, files, tmp_path)
    assert not (tmp_path / "seed.json").exists()
    def console(arguments, code, marker):
        assert arguments[arguments.index("--revision") + 1] == "ready"
        assert len(arguments[arguments.index("--command") + 1]) < 100
        assert "PYTHON_BASIC_REPL=1" in arguments[arguments.index("--command") + 1]
        assert f"print({marker!r})" in code
        assert "overwrite=False" in code
        return (marker + "\r\n").encode()
    monkeypatch.setattr(release, "console_code", console)
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


@pytest.fixture
def pilot_release(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    import socket

    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("Release tests must not contact services"))
    config = {
        "subscription": "11111111-1111-4111-8111-111111111111",
        "tenant": "22222222-2222-4222-8222-222222222222",
        "group": "synthetic-group", "registry": "synthetic", "storage": "synthetic",
        "container": "batches", "backend": "backend", "frontend": "frontend", "job": "worker",
        "api_client_id": "33333333-3333-4333-8333-333333333333",
        "api_principal_id": "77777777-7777-4777-8777-777777777777",
        "pilot_operator_ids": ["66666666-6666-4666-8666-666666666666"],
        "frontend_client_id": "44444444-4444-4444-8444-444444444444",
        "frontend_origin": "https://portal.synthetic.invalid",
        "backend_origin": "https://backend.synthetic.invalid",
        "auth_secret_ref": "auth-ref", "entra_secret_ref": "entra-ref",
        "backend_image": "synthetic.azurecr.io/docintel/backend@sha256:" + "a" * 64,
        "frontend_image": "synthetic.azurecr.io/docintel/frontend@sha256:" + "b" * 64,
    }
    now = datetime.now(timezone.utc)
    approval = {
        "schema_version": 1, "approved": True,
        "id": "55555555-5555-4555-8555-555555555555",
        "approved_by": "66666666-6666-4666-8666-666666666666",
        "not_before": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(seconds=1100)).isoformat(),
        "batch_id": "c" * 64, "owner": "synthetic-owner", "batch_sha256": "d" * 64,
        "customer_processing_approved": True,
        "identities": {
            "api_principal_id": "77777777-7777-4777-8777-777777777777",
            "worker_principal_id": "88888888-8888-4888-8888-888888888888",
        },
        "environment": {
            "AZURE_CLIENT_ID": "",
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://analysis.synthetic.invalid",
            "LLM_ENDPOINT": "https://model.synthetic.invalid",
            "LLM_DEPLOYMENT": "synthetic", "AOAI_API_VERSION": "synthetic-version",
            "WEBSEARCH_PROVIDER": "webiq", "WEBIQ_ENDPOINT": "https://api.microsoft.ai/v3/search/web",
        },
        "limits": {**release.PILOT_LIMITS, "spend_microdollars": 1000000},
        "unit_prices_usd": {name: "0.000001" for name in ("analysis_page", "input_token", "output_token", "search", "web_retrieval")},
    }
    prefix = f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}/providers/Microsoft.App/"
    resources = {}
    for kind in ("backend", "frontend"):
        containers = release.desired_containers(config, kind, [{
            "name": kind, "image": "old", "resources": {"cpu": 1, "memory": "2Gi"},
            "env": [{"name": "PRESERVED", "value": "original"}, {"name": "WEBIQ_API_KEY", "secretRef": "existing-web-key"}],
        }])
        resources[kind] = {
            "id": prefix + "containerApps/" + kind, "etag": "synthetic-etag",
            "identity": {"type": "SystemAssigned", "principalId": approval["identities"]["api_principal_id"]},
            "properties": {
                "provisioningState": "Succeeded", "latestRevisionName": "ready", "latestReadyRevisionName": "ready",
                "configuration": {"ingress": {"external": True}, "secrets": [{"name": "existing-web-key"}]},
                "template": {"containers": containers, "volumes": [{"name": "retained"}]},
            },
        }
    job = {
        "id": prefix + "jobs/worker",
        "identity": {"type": "SystemAssigned", "principalId": approval["identities"]["worker_principal_id"]},
        "properties": {
            "configuration": {"triggerType": "Manual", "replicaRetryLimit": 0, "replicaTimeout": 600, "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1}},
            "template": {"containers": copy.deepcopy(resources["backend"]["properties"]["template"]["containers"]), "volumes": [{"name": "worker-volume"}]},
        },
    }
    work = tmp_path / "pilot"
    source_files = {}
    for name in ("uv.lock", "backend/Dockerfile", "frontend/Dockerfile"):
        content = ("synthetic fixture " + name).encode()
        for tree in ("source", "context"):
            path = work / tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        source_files[name] = hashlib.sha256(content).hexdigest()
    modes = {str(path.relative_to(work / "context")): path.stat().st_mode & 0o777 for path in (work / "context").rglob("*")}
    release.save(work / "source.json", {"revision": "e" * 40, "files": source_files})
    release.save(work / "context.json", source_files)
    release.save(work / "context-modes.json", modes)
    release.save(work / "clean-checks.json", {
        "revision": "e" * 40, "lock_sha256": source_files["uv.lock"],
        "backend": "clean_locked_install_offline_tests", "frontend": "clean_npm_ci_checks_build",
    })
    for kind in ("backend", "frontend"):
        release.save(work / f"{kind}-smoke.json", {
            "revision": "e" * 40, "passed": True, "network": "none",
            "context_sha256": release.fingerprint(source_files), "modes_sha256": release.fingerprint(modes),
            **({"startup": "health_200_anonymous_batch_401", "uid": 10001} if kind == "backend"
               else {"running": True, "permission_failure": False, "probe_exit_code": 0}),
        })
    release.save(work / "images.json", {"revision": "e" * 40, "backend": config["backend_image"], "frontend": config["frontend_image"]})
    release.save(work / "pilot-acceptance.json", {
        "target": release.fingerprint(config), "revision": "e" * 40,
        "backend_image": config["backend_image"], "frontend_image": config["frontend_image"],
        "authenticated": True, "synthetic_accepted": True,
    })
    approval_path = work / "approval.json"
    release.save(approval_path, approval)
    state = SimpleNamespace(
        config=config, approval=approval, work=work, approval_path=approval_path,
        resources=resources, job=job, calls=[], console_calls=[], active=[],
    )
    monkeypatch.setattr(release, "app", lambda _, name: copy.deepcopy(resources[name]))
    monkeypatch.setattr(release, "identity_contract", lambda _: None)
    monkeypatch.setattr(release, "active_executions", lambda _: (copy.deepcopy(job), list(state.active)))

    def azure(*arguments):
        if arguments[:3] == ("containerapp", "revision", "show"):
            name = arguments[arguments.index("-n") + 1]
            return {"properties": {"healthState": "Healthy", "template": copy.deepcopy(resources[name]["properties"]["template"])}}
        state.calls.append(arguments)
        if arguments[:3] == ("containerapp", "job", "start"):
            return {"name": "synthetic-execution-" + str(len(state.calls))}
        assert arguments[:3] == ("rest", "--method", "PATCH")
        assert "--headers" in arguments and arguments[-1] == "If-Match=synthetic-etag"
        payload = json.loads(Path(arguments[arguments.index("--body") + 1][1:]).read_text())
        resources["backend"]["properties"]["template"]["containers"] = payload["properties"]["template"]["containers"]
        return {}

    def console(arguments, code, marker):
        state.console_calls.append((arguments, code, marker))
        return (marker + "\n").encode()

    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(release, "console_code", console)
    return state


def enable_test_pilot(state):
    release.pilot_enable(state.config, state.approval, state.work)


def test_pilot_approval_matches_guard_schema_and_denies_unknown_secrets(pilot_release):
    from backend.real_pilot import HARD_LIMITS, ENVIRONMENT_KEYS
    state = pilot_release
    assert release.PILOT_LIMITS == HARD_LIMITS
    assert release.PILOT_ENVIRONMENT_KEYS == ENVIRONMENT_KEYS
    assert release.load_pilot_approval(state.approval_path, state.approval["batch_id"]) == state.approval
    state.approval["environment"]["WEBIQ_API_KEY"] = "never-allowed"
    release.save(state.approval_path, state.approval)
    with pytest.raises(ValueError, match="Unsupported"):
        release.load_pilot_approval(state.approval_path, state.approval["batch_id"])
    assert not state.calls


@pytest.mark.parametrize("defect", ["draft", "batch", "expired", "limits", "price", "permissions"])
def test_pilot_invalid_approval_never_reaches_azure(pilot_release, monkeypatch, defect):
    from argparse import Namespace
    state = pilot_release
    if defect == "draft":
        state.approval["approved"] = False
    elif defect == "batch":
        state.approval["batch_id"] = "f" * 64
    elif defect == "expired":
        state.approval["expires_at"] = state.approval["not_before"]
    elif defect == "limits":
        state.approval["limits"]["executions"] = 3
    elif defect == "price":
        state.approval["unit_prices_usd"]["search"] = "0"
    release.save(state.approval_path, state.approval)
    if defect == "permissions":
        state.approval_path.chmod(0o644)
    monkeypatch.setattr(release, "load_config", lambda _: state.config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: pytest.fail("Azure before approval validation"))
    with pytest.raises(ValueError):
        release.run(Namespace(action="pilot-start", approve="pilot-start", work=state.work, config=None, batch_id="c" * 64, pilot_approval=state.approval_path))
    assert not state.calls


def test_pilot_enable_changes_only_three_api_settings(pilot_release):
    state = pilot_release
    before = copy.deepcopy(state.resources)
    enable_test_pilot(state)
    containers = state.resources["backend"]["properties"]["template"]["containers"]
    actual = release.environment_entries(containers[0])
    original = release.environment_entries(before["backend"]["properties"]["template"]["containers"][0])
    assert {name: entry for name, entry in actual.items() if name not in release.PILOT_API_SETTINGS} == original
    assert {name: actual[name]["value"] for name in release.PILOT_API_SETTINGS} == release.pilot_values(state.approval)
    assert "AZURE_CLIENT_ID" not in actual
    assert actual["WEBIQ_API_KEY"] == {"name": "WEBIQ_API_KEY", "secretRef": "existing-web-key"}
    assert state.resources["frontend"] == before["frontend"]
    assert state.resources["backend"]["identity"] == before["backend"]["identity"]
    assert state.resources["backend"]["properties"]["configuration"] == before["backend"]["properties"]["configuration"]
    assert len(state.calls) == 1 and state.calls[0][:3] == ("rest", "--method", "PATCH")
    assert len(state.console_calls) == 1
    assert (state.work / "pilot-enabled.json").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="disabled"):
        enable_test_pilot(state)
    assert len(state.calls) == 1


@pytest.mark.parametrize("field", ["authenticated", "synthetic_accepted", "revision", "backend_image", "target"])
def test_pilot_requires_accepted_exact_source_images_and_auth_baseline(pilot_release, field):
    state = pilot_release
    receipt = release.private_json(state.work / "pilot-acceptance.json")
    receipt[field] = False if field in ("authenticated", "synthetic_accepted") else "drifted"
    release.save(state.work / "pilot-acceptance.json", receipt)
    with pytest.raises(ValueError):
        enable_test_pilot(state)
    assert not state.calls and not state.console_calls


@pytest.mark.parametrize("kind", ["backend", "frontend"])
@pytest.mark.parametrize("defect", ["clean", "startup", "context", "modes", "revision", "lock"])
def test_pilot_requires_clean_install_and_exact_runtime_smoke(pilot_release, kind, defect):
    state = pilot_release
    clean = release.private_json(state.work / "clean-checks.json")
    smoke = release.private_json(state.work / f"{kind}-smoke.json")
    if defect == "clean":
        clean[kind] = "existing_interpreter_not_clean_install"
    elif defect == "startup":
        smoke["passed"] = False
    elif defect == "context":
        smoke["context_sha256"] = "wrong"
    elif defect == "modes":
        smoke["modes_sha256"] = "wrong"
    elif defect == "revision":
        smoke["revision"] = "f" * 40
    else:
        clean["lock_sha256"] = "wrong"
    release.save(state.work / "clean-checks.json", clean)
    release.save(state.work / f"{kind}-smoke.json", smoke)
    with pytest.raises(ValueError):
        enable_test_pilot(state)
    assert not state.calls and not state.console_calls


def test_pilot_requires_healthy_actual_published_revision(pilot_release, monkeypatch):
    state = pilot_release
    original = release.azure

    def azure(*arguments):
        result = original(*arguments)
        if arguments[:3] == ("containerapp", "revision", "show"):
            result["properties"]["healthState"] = "Unhealthy"
        return result

    monkeypatch.setattr(release, "azure", azure)
    with pytest.raises(ValueError, match="not healthy"):
        enable_test_pilot(state)
    assert not state.calls and not state.console_calls


@pytest.mark.parametrize("defect", ["active", "retry", "timeout", "replicas", "container", "image", "identity", "storage"])
def test_pilot_worker_safety_guards_before_mutation(pilot_release, defect):
    state = pilot_release
    settings = state.job["properties"]["configuration"]
    container = state.job["properties"]["template"]["containers"][0]
    if defect == "active":
        state.active.append({"name": "running"})
    elif defect == "retry":
        settings["replicaRetryLimit"] = 1
    elif defect == "timeout":
        settings["replicaTimeout"] = 1200
    elif defect == "replicas":
        settings["manualTriggerConfig"]["parallelism"] = 2
    elif defect == "container":
        state.job["properties"]["template"]["containers"].append(copy.deepcopy(container))
    elif defect == "image":
        container["image"] = "unapproved"
    elif defect == "identity":
        state.job["identity"]["principalId"] = state.approval["identities"]["api_principal_id"]
    else:
        release.merge_environment(container, {"DOCINTEL_BATCH_CONTAINER": "other"})
    with pytest.raises(ValueError):
        enable_test_pilot(state)
    assert not state.calls


def test_pilot_requires_matching_private_preseeded_configuration(pilot_release, monkeypatch):
    state = pilot_release
    monkeypatch.setattr(release, "console_code", lambda *args: b"no confirmation")
    with pytest.raises(ValueError, match="preflight"):
        enable_test_pilot(state)
    assert not state.calls
    assert not (state.work / "pilot-enablement.json").exists()
    code, _ = release.remote_pilot_check_code(state.approval)
    assert 'read_json(store, "configuration/real-pilot-approval.json")' in code
    assert "write_json" not in code and "upload_blob" not in code
    assert "checker._validate()" in code
    assert state.approval["owner"] not in code


@pytest.mark.parametrize("defect", [None, "approval", "batch", "ledger", "exhausted"])
def test_pilot_remote_preflight_executes_without_writes(pilot_release, monkeypatch, capsys, defect):
    from types import SimpleNamespace
    from backend.batch_store import Missing
    from backend.real_pilot import binding_digest, _sha256
    state = pilot_release
    batch = {
        "id": state.approval["batch_id"], "owner": state.approval["owner"],
        "valid": True, "product_count": 1, "attribute_reference": "synthetic.xlsx",
        "input_hashes": {"manifest": "a" * 64, "attributes": "b" * 64},
        "original_definitions": [{"attribute_id": "synthetic"}],
        "items": [{
            "item_key": "row-2", "errors": [],
            "manifest": {"product": {"item_id": "synthetic", "vendor": "synthetic", "mpn": "synthetic"}},
            "sources": [],
        }],
    }
    state.approval["batch_sha256"] = binding_digest(batch)
    code, marker = release.remote_pilot_check_code(state.approval)
    records = {
        "configuration/real-pilot-approval.json": copy.deepcopy(state.approval),
        f"batches/{batch['id']}.json": batch,
    }
    if defect == "approval":
        records["configuration/real-pilot-approval.json"]["owner"] = "different"
    elif defect == "batch":
        batch["items"][0]["manifest"]["product"]["mpn"] = "changed"
    elif defect in ("ledger", "exhausted"):
        records["budgets/real-pilot.json"] = {
            "approval_sha256": "changed" if defect == "ledger" else _sha256(state.approval),
            "executions": {"1": {}, "2": {}},
        }

    def read_bytes(key):
        if key not in records:
            raise Missing(key)
        return json.dumps(records[key]).encode(), "version"

    store = SimpleNamespace(read_bytes=read_bytes)
    monkeypatch.setattr("backend.batch_store.configured_store", lambda: store)
    # The generated preflight mutates only its disposable process environment.
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "false")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "")
    if defect:
        with pytest.raises((ValueError, AssertionError)):
            exec(code, {})
        assert marker not in capsys.readouterr().out
    else:
        exec(code, {})
        assert marker in capsys.readouterr().out


def test_pilot_start_has_exact_cli_settings_and_bounded_immutable_attempts(pilot_release):
    state = pilot_release
    enable_test_pilot(state)
    baseline = copy.deepcopy(state.job)
    for number in (1, 2):
        release.pilot_start(state.config, state.approval, state.work, 2)
        template = release.private_json(state.work / f"pilot-execution-{number}.json")
        container = template["containers"][0]
        assert container["command"] == ["/app/.venv/bin/python"]
        assert container["args"] == ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", state.approval["batch_id"], "--concurrency", "1", "--max-batches", "1", "--item-limit", "2"]
        environment = release.environment_entries(container)
        for name, value in {**state.approval["environment"], **release.pilot_values(state.approval)}.items():
            assert environment[name] == {"name": name, "value": value}
        assert environment["DOCINTEL_BATCH_LIVE_ENABLED"]["value"] == "false"
        assert environment["WEBIQ_API_KEY"]["secretRef"] == "existing-web-key"
        attempt = release.private_json(state.work / f"pilot-execution-attempt-{number}.json")
        assert attempt["attempt"] == number
        assert attempt["execution_sha256"] == release.fingerprint(template)
        assert template["volumes"] == baseline["properties"]["template"]["volumes"]
    assert state.job == baseline
    with pytest.raises(ValueError, match="allowance exhausted"):
        release.pilot_start(state.config, state.approval, state.work, 2)
    assert len([call for call in state.calls if call[:3] == ("containerapp", "job", "start")]) == 2


@pytest.mark.parametrize("limit", [0, 5, True])
def test_pilot_start_rejects_unbounded_item_limits(pilot_release, limit):
    state = pilot_release
    with pytest.raises(ValueError, match="slice"):
        release.pilot_start(state.config, state.approval, state.work, limit)
    assert not state.calls


def test_pilot_start_unknown_outcome_is_not_retried_or_refunded(pilot_release, monkeypatch):
    state = pilot_release
    enable_test_pilot(state)
    calls = []
    original = release.azure

    def fail(*arguments):
        if arguments[:3] != ("containerapp", "job", "start"):
            return original(*arguments)
        calls.append(arguments)
        raise ValueError("Synthetic lost response")

    monkeypatch.setattr(release, "azure", fail)
    with pytest.raises(ValueError, match="lost response"):
        release.pilot_start(state.config, state.approval, state.work, 1)
    assert (state.work / "pilot-execution-attempt-1.json").exists()
    with pytest.raises(ValueError, match="outcome is unknown"):
        release.pilot_start(state.config, state.approval, state.work, 1)
    assert len(calls) == 1


def test_pilot_approval_edit_cannot_reset_local_allowance(pilot_release):
    state = pilot_release
    enable_test_pilot(state)
    state.approval["id"] = "99999999-9999-4999-8999-999999999999"
    with pytest.raises(ValueError, match="cannot reset"):
        release.pilot_start(state.config, state.approval, state.work, 1)
    assert len(state.calls) == 1


def test_pilot_disable_restores_only_managed_values_after_expiry(pilot_release):
    state = pilot_release
    baseline = copy.deepcopy(state.resources["backend"])
    enable_test_pilot(state)
    container = state.resources["backend"]["properties"]["template"]["containers"][0]
    release.merge_environment(container, {"UNRELATED_NEW_SETTING": "retained"})
    state.approval["expires_at"] = state.approval["not_before"]
    release.pilot_disable(state.config, state.work)
    actual = release.environment_entries(state.resources["backend"]["properties"]["template"]["containers"][0])
    original = release.environment_entries(baseline["properties"]["template"]["containers"][0])
    assert actual == {**original, "UNRELATED_NEW_SETTING": {"name": "UNRELATED_NEW_SETTING", "value": "retained"}}
    assert (state.work / "pilot-disabled.json").exists()
    count = len(state.calls)
    release.pilot_disable(state.config, state.work)
    assert len(state.calls) == count


def test_pilot_disable_refuses_managed_setting_drift(pilot_release):
    state = pilot_release
    enable_test_pilot(state)
    container = state.resources["backend"]["properties"]["template"]["containers"][0]
    release.merge_environment(container, {"DOCINTEL_REAL_PILOT_OPERATOR_IDS": "unrelated-operator"})
    with pytest.raises(ValueError, match="unsafe rollback"):
        release.pilot_disable(state.config, state.work)
    assert len(state.calls) == 1


def test_pilot_disable_restores_existing_entries_exactly(pilot_release):
    state = pilot_release
    container = state.resources["backend"]["properties"]["template"]["containers"][0]
    prior = {
        "DOCINTEL_REAL_PILOT_ENABLED": "false",
        "DOCINTEL_REAL_PILOT_OPERATOR_IDS": "prior-approved-operator",
        "DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID": "prior-worker-binding",
    }
    release.merge_environment(container, prior)
    enable_test_pilot(state)
    release.pilot_disable(state.config, state.work)
    current = release.environment_entries(state.resources["backend"]["properties"]["template"]["containers"][0])
    assert {name: current[name]["value"] for name in release.PILOT_API_SETTINGS} == prior


def test_pilot_enable_rejects_unknown_flag_secret_reference(pilot_release):
    state = pilot_release
    container = state.resources["backend"]["properties"]["template"]["containers"][0]
    container["env"].append({"name": "DOCINTEL_REAL_PILOT_ENABLED", "secretRef": "unknown-flag"})
    with pytest.raises(ValueError, match="disabled"):
        enable_test_pilot(state)
    assert not state.calls


def test_pilot_local_lock_prevents_concurrent_release_actions(pilot_release):
    state = pilot_release
    with release.pilot_lock(state.work):
        with pytest.raises(ValueError, match="in progress"):
            enable_test_pilot(state)
    assert not state.calls


def test_synthetic_start_remains_separate_from_real_pilot(pilot_release, monkeypatch):
    from argparse import Namespace
    state = pilot_release
    monkeypatch.setattr(release, "load_config", lambda _: state.config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    release.run(Namespace(action="start", approve="start", work=state.work, config=None, batch_id="f" * 64, item_limit=1))
    execution = release.private_json(state.work / "execution.json")
    assert "--synthetic-acceptance" in execution["containers"][0]["args"]
    assert "--real-pilot" not in execution["containers"][0]["args"]
    assert not any(entry["name"] == "DOCINTEL_REAL_PILOT_ENABLED" for entry in execution["containers"][0]["env"])
    assert not state.console_calls


@pytest.fixture
def upload_release(pilot_release):
    from backend.pilot_upload import registry_sha256
    state = pilot_release
    operator = state.approval["approved_by"]
    source = {
        "reference": "synthetic.pdf", "source_id": "synthetic-source",
        "kind": "blob", "format": "pdf", "source_tier": "internal_pdf",
        "enabled": True, "blob": "documents/synthetic.pdf", "sha256": "f" * 64,
        "products": [{"item_id": "001", "vendor": "Synthetic", "mpn": "Part-1", "hierarchy_node": "Valve"}],
    }
    state.upload = {
        "schema_version": 1, "approved": True,
        "id": "99999999-9999-4999-8999-999999999999",
        "approved_by": operator, "owner": state.config["tenant"] + "/" + operator,
        "expires_at": state.approval["expires_at"],
        "expected_registry_sha256": registry_sha256([]),
        "sources": [source],
        "documents": [{"source_id": source["source_id"], "filename": "synthetic.pdf", "bytes": 100, "sha256": source["sha256"]}],
    }
    state.upload_path = state.work / "upload-approval.json"
    release.save(state.upload_path, state.upload)
    return state


def test_pilot_upload_metadata_uses_actual_schema_and_exact_owner(upload_release):
    state = upload_release
    assert release.load_upload_approval(state.upload_path, state.config) == state.upload
    state.upload["owner"] = state.config["tenant"] + "/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    release.save(state.upload_path, state.upload)
    with pytest.raises(ValueError, match="schema is invalid") as error:
        release.load_upload_approval(state.upload_path, state.config)
    assert "synthetic.pdf" not in str(error.value)
    assert not state.calls


@pytest.mark.parametrize("operators", [None, [], ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]])
def test_pilot_approval_cannot_self_authorize_operator_allowlist(upload_release, operators):
    state = upload_release
    state.config["pilot_operator_ids"] = operators
    for action in (release.pilot_upload_enable, release.pilot_enable):
        with pytest.raises(ValueError, match="pilot_operator_ids|trusted target allowlist"):
            action(state.config, state.upload if action == release.pilot_upload_enable else state.approval, state.work)
    with pytest.raises(ValueError, match="pilot_operator_ids|trusted target allowlist"):
        release.pilot_configure(state.config, state.upload, state.work, "upload")
    assert not state.calls and not state.console_calls


def test_pilot_upload_schema_unavailability_fails_closed(upload_release, monkeypatch):
    state = upload_release

    def unavailable(*args):
        raise AttributeError("synthetic private schema mismatch")

    monkeypatch.setattr("backend.pilot_upload.validate_upload_approval", unavailable)
    with pytest.raises(ValueError, match="invalid or unavailable") as error:
        release.load_upload_approval(state.upload_path, state.config)
    assert "private schema" not in str(error.value)
    assert not state.calls and not state.console_calls


def test_pilot_upload_enable_needs_no_real_approval_or_batch_and_preserves_flags(upload_release):
    state = upload_release
    before = copy.deepcopy(state.resources["backend"])
    release.pilot_upload_enable(state.config, state.upload, state.work)
    current = release.environment_entries(state.resources["backend"]["properties"]["template"]["containers"][0])
    original = release.environment_entries(before["properties"]["template"]["containers"][0])
    assert {name: entry for name, entry in current.items() if name not in release.PILOT_UPLOAD_SETTINGS} == original
    assert {name: current[name]["value"] for name in release.PILOT_UPLOAD_SETTINGS} == release.upload_values(state.upload)
    assert "DOCINTEL_REAL_PILOT_ENABLED" not in current
    assert "batches/" not in state.console_calls[0][1]
    assert "write_json(store, key, candidate)" not in state.console_calls[0][1]
    assert (state.work / "pilot-upload-enabled.json").exists()
    assert not (state.work / "pilot-binding.json").exists()
    assert not (state.work / "pilot-enablement.json").exists()


def test_pilot_upload_disable_restores_only_upload_settings(upload_release):
    state = upload_release
    before = copy.deepcopy(state.resources["backend"]["properties"]["template"]["containers"][0])
    release.pilot_upload_enable(state.config, state.upload, state.work)
    container = state.resources["backend"]["properties"]["template"]["containers"][0]
    release.merge_environment(container, {"UNRELATED": "retained"})
    release.pilot_upload_disable(state.config, state.work)
    current = release.environment_entries(state.resources["backend"]["properties"]["template"]["containers"][0])
    assert current == {**release.environment_entries(before), "UNRELATED": {"name": "UNRELATED", "value": "retained"}}
    assert (state.work / "pilot-upload-disabled.json").exists()


@pytest.mark.parametrize("defect", ["real-enabled", "principal", "missing-principal", "server-mismatch"])
def test_pilot_upload_activation_fails_before_mutation(upload_release, monkeypatch, defect):
    state = upload_release
    if defect == "real-enabled":
        release.merge_environment(state.resources["backend"]["properties"]["template"]["containers"][0], {"DOCINTEL_REAL_PILOT_ENABLED": "true"})
    elif defect == "principal":
        state.resources["backend"]["identity"]["principalId"] = "different"
    elif defect == "missing-principal":
        del state.config["api_principal_id"]
    else:
        monkeypatch.setattr(release, "console_code", lambda *args: b"no match")
    with pytest.raises(ValueError):
        release.pilot_upload_enable(state.config, state.upload, state.work)
    assert not state.calls


@pytest.mark.parametrize("payload", [{"value": "x" * 65537}, {"value": "test" * 20000}])
def test_pilot_configure_rejects_large_raw_metadata(payload):
    with pytest.raises(ValueError, match="64 KiB"):
        release.bounded_metadata(payload)


def test_pilot_configure_rejects_large_compressed_metadata():
    import random
    import string
    randomizer = random.Random(1)
    payload = {"value": "".join(randomizer.choices(string.ascii_letters + string.digits, k=20000))}
    with pytest.raises(ValueError, match="8 KiB"):
        release.bounded_metadata(payload)


@pytest.mark.parametrize("existing", ["missing", "identical", "different"])
def test_pilot_configure_remote_append_only_upload_metadata(upload_release, monkeypatch, capsys, existing):
    from backend.batch_store import Missing
    state = upload_release
    key = release.PILOT_CONFIGURATION_KEYS["upload"]
    records = {}
    if existing != "missing":
        records[key] = copy.deepcopy(state.upload)
    if existing == "different":
        records[key]["id"] = state.approval["id"]
    writes = []

    class Store:
        def read_bytes(self, name):
            if name not in records:
                raise Missing(name)
            return json.dumps(records[name]).encode(), "v1"

        def write_bytes(self, name, value, version=None):
            assert name == key and version is None
            writes.append(name)
            records[name] = json.loads(value)
            return "v1"

    monkeypatch.setattr("backend.batch_store.configured_store", Store)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "false")
    monkeypatch.setenv("DOCINTEL_PILOT_UPLOAD_ENABLED", "false")
    code, marker = release.remote_metadata_code(state.upload, "upload", state.config["tenant"], write=True)
    assert len(base64.b64encode(code.encode())) + 80 <= 16384
    assert "documents/" not in code
    if existing == "different":
        with pytest.raises(AssertionError):
            exec(code, {})
        assert not writes and marker not in capsys.readouterr().out
    else:
        exec(code, {})
        assert marker in capsys.readouterr().out
        assert len(writes) == (1 if existing == "missing" else 0)
        assert records[key] == state.upload


def test_pilot_configure_pins_metadata_but_allows_identical_confirmation_retry(upload_release):
    state = upload_release
    for _ in range(2):
        release.pilot_configure(state.config, state.upload, state.work, "upload")
    assert len(state.console_calls) == 2
    assert (state.work / "pilot-configure-upload-attempt.json").exists()
    assert (state.work / "pilot-configured-upload.json").exists()
    assert not state.calls
    state.upload["documents"][0]["bytes"] += 1
    with pytest.raises(ValueError, match="pinned"):
        release.pilot_configure(state.config, state.upload, state.work, "upload")
    assert len(state.console_calls) == 2


def test_pilot_configure_fixed_keys_and_exclusive_metadata_files(upload_release, monkeypatch):
    from argparse import Namespace
    state = upload_release
    with pytest.raises(ValueError, match="fixed"):
        release.remote_metadata_code(state.upload, "configuration/other.json", state.config["tenant"], write=True)
    monkeypatch.setattr(release, "load_config", lambda _: state.config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: pytest.fail("No cloud check before metadata selection"))
    with pytest.raises(ValueError, match="exactly one"):
        release.run(Namespace(action="pilot-configure", approve="pilot-configure", work=state.work, config=None, pilot_approval=state.approval_path, upload_approval=state.upload_path))


def test_pilot_configure_final_approval_uses_only_real_fixed_key(pilot_release):
    state = pilot_release
    release.pilot_configure(state.config, state.approval, state.work, "real")
    code = state.console_calls[0][1]
    assert 'key = "configuration/real-pilot-approval.json"' in code or "key = 'configuration/real-pilot-approval.json'" in code
    assert "checker._validate_approval(candidate, verify_runtime=False)" in code
    assert (state.work / "pilot-configured-real.json").exists()
    assert not state.calls


def test_pilot_configure_registry_drift_fails_before_any_metadata_write(upload_release, monkeypatch):
    from backend.batch_store import Missing
    state = upload_release
    state.upload["expected_registry_sha256"] = "a" * 64

    class ReadOnlyStore:
        def read_bytes(self, name, **kwargs):
            raise Missing(name)

    monkeypatch.setattr("backend.batch_store.configured_store", ReadOnlyStore)
    for name in ("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "DOCINTEL_REAL_PILOT_ENABLED", "DOCINTEL_PILOT_UPLOAD_ENABLED"):
        monkeypatch.setenv(name, "")
    code, _ = release.remote_metadata_code(state.upload, "upload", state.config["tenant"], write=True)
    with pytest.raises(ValueError):
        exec(code, {})


@pytest.mark.parametrize("batch_exists", [True, False])
def test_pilot_configure_final_metadata_validates_intake_before_append(pilot_release, monkeypatch, capsys, batch_exists):
    from backend.batch_store import Missing
    from backend.real_pilot import binding_digest
    state = pilot_release
    batch = {
        "id": state.approval["batch_id"], "owner": state.approval["owner"],
        "valid": True, "product_count": 1, "attribute_reference": "synthetic.xlsx",
        "input_hashes": {"manifest": "a" * 64, "attributes": "b" * 64},
        "original_definitions": [{"attribute_id": "synthetic"}],
        "items": [{"item_key": "row-2", "errors": [], "manifest": {
            "product": {"item_id": "synthetic", "vendor": "synthetic", "mpn": "synthetic"},
        }, "sources": []}],
    }
    state.approval["batch_sha256"] = binding_digest(batch)
    records = {f"batches/{batch['id']}.json": batch} if batch_exists else {}
    writes = []

    class Store:
        def read_bytes(self, name):
            if name not in records:
                raise Missing(name)
            return json.dumps(records[name]).encode(), "v1"

        def write_bytes(self, name, content, version=None):
            assert name == release.PILOT_CONFIGURATION_KEYS["real"] and version is None
            writes.append(name)
            records[name] = json.loads(content)
            return "v1"

    monkeypatch.setattr("backend.batch_store.configured_store", Store)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "")
    code, marker = release.remote_metadata_code(state.approval, "real", state.config["tenant"], write=True)
    if batch_exists:
        exec(code, {})
        assert len(writes) == 1 and marker in capsys.readouterr().out
    else:
        with pytest.raises(Missing):
            exec(code, {})
        assert not writes and marker not in capsys.readouterr().out