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


def test_release_helper_ci_regenerates_and_verifies_pinned_write_contracts():
    contracts = release.write_schema().check_generated()
    assert set(contracts["contracts"]) == {"containerApps", "jobs"}
    assert all(set(methods) == {"PATCH", "PUT"} for methods in contracts["contracts"].values())


@pytest.fixture
def container_read_response():
    return {"location": "westus", "identity": {"type": "SystemAssigned"}, "properties": {
        "configuration": {"registries": [{"server": "synthetic.azurecr.io", "passwordSecretRef": "acr-password"}], "ingress": {
            "additionalPortMappings": None, "allowInsecure": False, "clientCertificateMode": None,
            "corsPolicy": None, "customDomains": None, "exposedPort": 0, "external": True,
            "fqdn": "synthetic.invalid", "ipSecurityRestrictions": None, "stickySessions": None,
            "targetPort": 80, "targetPortHttpScheme": None,
            "traffic": [{"latestRevision": True, "weight": 100}], "transport": "auto",
        }},
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
    monkeypatch.setattr(release, "app", lambda *arguments: copy.deepcopy(container_read_response))
    release.patch_app(config, kind, containers, tmp_path)
    payload = json.loads(next(tmp_path.glob(kind + "-patch-*.json")).read_text())
    expected = copy.deepcopy(containers)
    expected[0].pop("imageType")
    expected[0]["resources"].pop("ephemeralStorage")
    assert payload == {"location": "westus", "properties": {"template": {"containers": expected}}}
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
    baseline = {"target": release.fingerprint(config), "authenticated": False, "apps": {kind: {"containers": original, "ingress": copy.deepcopy(container_read_response["properties"]["configuration"]["ingress"]), "pinned_image": "synthetic.azurecr.io/old@sha256:" + "c" * 64} for kind in ("backend", "frontend")}}
    release.save(tmp_path / "baseline.json", baseline)
    resources = {kind: copy.deepcopy(container_read_response) for kind in ("backend", "frontend")}
    for kind, resource in resources.items():
        resource["properties"].update(provisioningState="Succeeded", latestRevisionName="ready", latestReadyRevisionName="ready")
        resource["properties"]["configuration"]["secrets"] = [{"name": reference} for reference in ("auth-reference", "entra-reference")]
        if action == "rollback":
            intended = release.desired_containers(config, kind, original)
            resource["properties"]["template"]["containers"] = intended
            resource["properties"]["configuration"]["ingress"] = release.desired_ingress(kind, baseline["apps"][kind]["ingress"])
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
            kind = path.name.split("-patch-")[0]
            payload = json.loads(path.read_text())
            payloads[kind] = payload
            assert set(payload) == {"location", "properties"}
            assert payload["location"] == "westus"
            assert set(payload["properties"]) == {"template", "configuration"}
            assert set(payload["properties"]["template"]) == {"containers"}
            resources[kind]["properties"]["template"]["containers"] = payload["properties"]["template"]["containers"]
            resources[kind]["properties"]["configuration"]["ingress"] = payload["properties"]["configuration"]["ingress"]
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
    if action == "rollback":
        # The official 2024 contract does not permit a null ingress write.
        with pytest.raises(ValueError, match="contract reason=type"):
            release.run(Namespace(action=action, approve=action, work=tmp_path, config=None, isolate_legacy_baseline=True))
        assert not payloads and resources == before
        return
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
        assert {key: value for key, value in resources[kind]["properties"]["configuration"].items() if key != "ingress"} == {key: value for key, value in before[kind]["properties"]["configuration"].items() if key != "ingress"}
        expected_ingress = release.desired_ingress(kind, baseline["apps"][kind]["ingress"])
        assert payload["properties"]["configuration"]["ingress"] == {
            key: value for key, value in expected_ingress.items() if value is not None
        }
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


@pytest.fixture
def publication_transport(monkeypatch):
    def install(work, config, status="Succeeded", *, cpu=2, digest_mismatch=False):
        calls, requests, archives = [], {}, []

        def upload(location, archive, timeout):
            import tarfile
            assert 0 < timeout <= 120
            assert archive.stat().st_mode & 0o777 == 0o600
            with tarfile.open(archive) as contents:
                archives.append(contents.getnames())
            return location["relativePath"]

        def azure(*arguments, timeout=None):
            assert 0 < timeout <= 60
            calls.append(arguments)
            if arguments[:2] == ("acr", "repository"):
                return {"digest": "sha256:" + ("d" if digest_mismatch else "c") * 64}
            assert arguments[0] == "rest"
            method = arguments[arguments.index("--method") + 1]
            url = arguments[arguments.index("--url") + 1]
            assert url.endswith("?api-version=2019-04-01")
            assert f"/registries/{config['registry']}/" in url
            if "/listBuildSourceUploadUrl?" in url:
                assert method == "POST"
                assert list(work.glob("*-publication*attempt.json"))
                return {"relativePath": "source/20300203/synthetic.tar.gz", "uploadUrl": "https://registryaccount.blob.core.windows.net/container/source/20300203/synthetic.tar.gz?sig=TEST-ONLY-SAS&se=2030-02-04T13:00:00Z&sp=rw"}
            if "/scheduleRun?" in url:
                assert method == "POST"
                body_path = Path(arguments[arguments.index("--body") + 1].removeprefix("@"))
                body = release.private_json(body_path)
                stem = body_path.name.removesuffix("-request.json")
                assert release.private_json(work / (stem + "-submission.json"))["request_sha256"] == hashlib.sha256(body_path.read_bytes()).hexdigest()
                attempt = release.private_json(work / (stem + "-attempt.json"))
                assert attempt["cpu"] == body["agentConfiguration"]["cpu"] == 2
                assert attempt["timeout_seconds"] == body["timeout"] == 900
                assert arguments[arguments.index("--headers") + 1] == "x-docintel-publication-attempt-id=" + attempt["attempt_id"]
                run_id = "synthetic-" + str(len(requests) + 1)
                requests[run_id] = body
                if status == "Unknown":
                    raise ValueError("Synthetic unknown build outcome")
                return {"properties": {"runId": run_id, "status": "Queued"}}
            assert method == "GET" and "/runs/" in url
            run_id = url.split("/runs/")[1].split("?")[0]
            body = requests[run_id]
            assert list(work.glob("*-publication*queued.json"))
            repository, tag = body["imageNames"][0].split(":")
            return {"properties": {
                "runId": run_id, "status": status, "agentConfiguration": {"cpu": cpu},
                "outputImages": [{"registry": config["registry"] + ".azurecr.io", "repository": repository, "tag": tag, "digest": "sha256:" + "c" * 64}],
            }}
        monkeypatch.setattr(release, "upload_publication_context", upload)
        monkeypatch.setattr(release, "azure", azure)
        return calls, requests, archives
    return install


@pytest.mark.parametrize("defect", [None, "smoke", "mode", "backend", "failed_build", "unknown_build"])
@pytest.mark.parametrize("kind", ["frontend", "backend"])
def test_component_publication_preserves_other_image_and_refuses_retry(tmp_path, monkeypatch, publication_transport, defect, kind):
    retained = "frontend" if kind == "backend" else "backend"
    previous = tmp_path / "previous"
    work = tmp_path / "replacement"
    config = {"registry": "synthetic", "subscription": "synthetic", "group": "synthetic", retained + "_image": "synthetic.azurecr.io/docintel/" + retained + "@sha256:" + "b" * 64}
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
    status = {"failed_build": "Failed", "unknown_build": "Unknown"}.get(defect, "Succeeded")
    calls, requests, archives = publication_transport(work, config, status)
    if defect == "mode":
        (context / kind / "Dockerfile").chmod(0o400)
    if defect == "backend":
        config[retained + "_image"] = config[retained + "_image"].replace("b" * 64, "d" * 64)
    if defect:
        with pytest.raises(ValueError):
            release.publish_component(config, work, previous, kind)
        assert len(requests) == (1 if defect in ("failed_build", "unknown_build") else 0)
    else:
        release.publish_component(config, work, previous, kind)
        receipt = json.loads((work / "images.json").read_text())
        assert receipt[retained] == previous_images[retained]
        assert receipt["preserved_" + retained]["revision"] == "b" * 40
        release.require_release_images({**config, kind + "_image": receipt[kind]}, work)
        assert receipt["tool_revision"] == "a" * 40
        assert len(archives) == 1 and ("backend/Dockerfile" if kind == "backend" else "Dockerfile") in archives[0]
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
def test_publication_uses_verified_contexts_and_bounded_arm_builds(tmp_path, monkeypatch, publication_transport, build_status):
    from argparse import Namespace
    config = {"registry": "synthetic", "subscription": "subscription", "group": "synthetic"}
    source = {"revision": "a" * 40, "files": {}}
    for relative in ("backend/Dockerfile", "frontend/Dockerfile"):
        path = tmp_path / "context" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FROM synthetic")
        source["files"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    release.save(tmp_path / "source.json", source)
    release.save(tmp_path / "context.json", source["files"])
    release.save(tmp_path / "context-modes.json", {str(path.relative_to(tmp_path / "context")): path.stat().st_mode & 0o777 for path in (tmp_path / "context").rglob("*")})
    release.save(tmp_path / "clean-checks.json", {"revision": source["revision"], "lock_sha256": hashlib.sha256(b"lock").hexdigest()})
    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_source", lambda _: source)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "command", lambda args, **kwargs: b"lock" if "show" in args else b"f" * 40)
    calls, builds, archives = publication_transport(tmp_path, config, build_status)
    options = Namespace(action="publish", work=tmp_path, config=None, approve="publish")
    if build_status in ("Failed", "Unknown"):
        with pytest.raises(ValueError, match="did not succeed|unknown"):
            release.run(options)
        assert len(builds) == 1
        assert not (tmp_path / "images.json").exists()
    else:
        release.run(options)
        assert len(builds) == 2
        receipt = json.loads((tmp_path / "images.json").read_text())
        assert receipt["frontend_run_id"] == "synthetic-2"
        assert receipt["revision"] == "a" * 40 and receipt["tool_revision"] == "f" * 40
    for body, archived in zip(builds.values(), archives):
        assert body["dockerFilePath"] in archived
        assert body["timeout"] == 900 and body["agentConfiguration"] == {"cpu": 2}
        assert body["sourceLocation"] == "source/20300203/synthetic.tar.gz"
    assert not list(tmp_path.glob("*-context.tar.gz"))
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


@pytest.fixture
def publication_case(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 2, 3, 10, tzinfo=timezone.utc)

    monkeypatch.setattr(release, "datetime", Clock)
    config = {
        "registry": "synthetic", "subscription": "subscription", "group": "synthetic",
        "pilot_operator_ids": ["11111111-1111-4111-8111-111111111111"],
        "publication_deadline": "2030-02-03T13:00:00Z",
        "frontend_image": "synthetic.azurecr.io/docintel/frontend@sha256:" + "b" * 64,
    }
    context = tmp_path / "context"
    for relative in ("backend/Dockerfile", "backend/main.py", "frontend/Dockerfile", "frontend/public/app.txt", "uv.lock"):
        path = context / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("lock" if relative == "uv.lock" else "synthetic")
    inventory = {str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest() for path in context.rglob("*") if path.is_file()}
    modes = {str(path.relative_to(context)): path.stat().st_mode & 0o777 for path in context.rglob("*")}
    source = {"revision": "a" * 40, "files": inventory}
    release.save(tmp_path / "source.json", source)
    release.save(tmp_path / "context.json", inventory)
    release.save(tmp_path / "context-modes.json", modes)
    release.save(tmp_path / "clean-checks.json", {"revision": source["revision"], "lock_sha256": hashlib.sha256(b"lock").hexdigest()})
    release.save(tmp_path / "backend-smoke.json", {"revision": source["revision"], "passed": True, "context_sha256": release.fingerprint(inventory), "modes_sha256": release.fingerprint(modes)})
    monkeypatch.setattr(release, "command", lambda args, **kwargs: b"lock" if "show" in args else b"f" * 40)
    monkeypatch.setattr(release, "verify_source", lambda path: release.private_json(path / "source.json"))
    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    return config, source


@pytest.fixture
def publication_replacement(tmp_path, publication_case):
    config, source = publication_case
    original = tmp_path / "backend-publication-attempt.json"
    release.save(original, {"revision": source["revision"], "component": "backend", "cpu": 2, "timeout_seconds": 900, "status": "attempted"})
    return {
        "schema_version": 1, "approved": True, "id": "22222222-2222-4222-8222-222222222222",
        "approved_by": config["pilot_operator_ids"][0], "target": release.fingerprint(config),
        "work": str(tmp_path.resolve()), "revision": source["revision"], "component": "backend",
        "original_attempt": str(original.resolve()), "original_attempt_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "original_failure": "unsupported_acr_build_cpu_argument", "additional_attempts": 1,
        "cpu": 2, "timeout_seconds": 900, "expires_at": "2030-02-03T13:00:00Z",
    }


@pytest.fixture
def publication_supplemental(tmp_path, publication_case, publication_replacement):
    config, source = publication_case
    replacement = release.reserve_publication_attempt(tmp_path, "backend", source["revision"], replacement=publication_replacement, config=config)
    return {
        "schema_version": 1, "approved": True, "id": "44444444-4444-4444-8444-444444444444",
        "approved_by": config["pilot_operator_ids"][0], "target": release.fingerprint(config),
        "work": str(tmp_path.resolve()), "revision": source["revision"],
        "original_attempt": publication_replacement["original_attempt"],
        "original_attempt_sha256": publication_replacement["original_attempt_sha256"],
        "replacement_attempt": str(replacement.resolve()),
        "replacement_attempt_sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
        "metadata_requests": 1, "additional_backend_attempts": 1, "cpu": 2,
        "timeout_seconds": 900, "expires_at": "2030-02-03T13:00:00Z",
    }


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("approved", False), ("metadata_requests", 2),
    ("metadata_requests", True), ("additional_backend_attempts", 2), ("cpu", 4),
    ("timeout_seconds", 901), ("id", "invalid"), ("target", "0" * 64),
    ("approved_by", "33333333-3333-4333-8333-333333333333"), ("work", "/different"),
    ("revision", "b" * 40), ("original_attempt", "/different"),
    ("replacement_attempt", "/different"), ("original_attempt_sha256", "0" * 64),
    ("replacement_attempt_sha256", "0" * 64), ("expires_at", "2030-02-03T09:00:00Z"),
    ("expires_at", "2030-02-03T13:00:01Z"), ("extra", "forbidden"),
])
def test_supplemental_publication_scope_rejects_before_preflight(tmp_path, monkeypatch, publication_case, publication_supplemental, field, value):
    config, source = publication_case
    monkeypatch.setattr(release, "azure", lambda *args, **kwargs: pytest.fail("Azure called"))
    with pytest.raises(ValueError):
        release.publication_metadata_preflight(config, tmp_path, source["revision"], {**publication_supplemental, field: value})
    assert not (tmp_path / "metadata-preflight-attempt.json").exists()
    assert not (tmp_path / "backend-publication-supplemental-attempt.json").exists()


@pytest.mark.parametrize("record", ["backend-publication-attempt.json", "backend-publication-replacement-attempt.json"])
def test_supplemental_publication_rejects_prior_receipt_byte_drift(tmp_path, monkeypatch, publication_case, publication_supplemental, record):
    config, source = publication_case
    path = tmp_path / record
    path.write_bytes(path.read_bytes() + b"\n")
    monkeypatch.setattr(release, "azure", lambda *args, **kwargs: pytest.fail("Azure called"))
    with pytest.raises(ValueError, match="prior attempt hash"):
        release.publication_metadata_preflight(config, tmp_path, source["revision"], publication_supplemental)
    assert not (tmp_path / "metadata-preflight-attempt.json").exists()


@pytest.mark.parametrize("stage", ["response_shape", "source_path", "https_blob_destination", "source_blob_binding", "sas_signature", "sas_expiry", "sas_not_before", "sas_write_permission"])
def test_supplemental_preflight_failure_consumes_only_metadata_slot(tmp_path, monkeypatch, publication_case, publication_supplemental, stage):
    config, source = publication_case
    location = {"relativePath": "opaque-blob", "uploadUrl": "https://registryaccount.blob.core.windows.net/container/opaque-blob?sig=TEST-ONLY-SAS&se=2030-02-03T13:00:00Z&sp=rw"}
    if stage == "response_shape":
        location = None
    elif stage == "source_path":
        location["relativePath"] = "../private"
    elif stage == "https_blob_destination":
        location["uploadUrl"] = location["uploadUrl"].replace("https://", "http://")
    elif stage == "source_blob_binding":
        location["relativePath"] = "different"
    elif stage == "sas_signature":
        location["uploadUrl"] = location["uploadUrl"].replace("sig=TEST-ONLY-SAS", "sig=")
    elif stage == "sas_expiry":
        location["uploadUrl"] = location["uploadUrl"].replace("13:00:00", "10:20:59")
    elif stage == "sas_not_before":
        location["uploadUrl"] += "&st=2030-02-03T10:01:00Z"
    elif stage == "sas_write_permission":
        location["uploadUrl"] = location["uploadUrl"].replace("sp=rw", "sp=r")
    calls = []
    monkeypatch.setattr(release, "azure", lambda *args, **kwargs: calls.append(args) or location)
    monkeypatch.setattr(release, "upload_publication_context", lambda *args: pytest.fail("Context uploaded"))
    monkeypatch.setattr(release, "archive_publication_context", lambda *args: pytest.fail("Context archived"))
    with pytest.raises(ValueError, match=stage) as error:
        release.publication_metadata_preflight(config, tmp_path, source["revision"], publication_supplemental)
    assert "TEST-ONLY-SAS" not in str(error.value)
    assert len(calls) == 1 and "/listBuildSourceUploadUrl?" in " ".join(calls[0])
    result = release.private_json(tmp_path / "metadata-preflight-result.json")
    assert result["status"] == "rejected" and result["stage"] == stage
    assert not (tmp_path / "backend-publication-supplemental-attempt.json").exists()
    assert not (tmp_path / "frontend-publication-attempt.json").exists()
    for approval in (publication_supplemental, {**publication_supplemental, "id": str(release.uuid.uuid4())}):
        with pytest.raises(ValueError, match="already attempted"):
            release.publication_metadata_preflight(config, tmp_path, source["revision"], approval)
    for kind in ("backend", "frontend"):
        with pytest.raises(ValueError, match="already attempted"):
            release.reserve_publication_attempt(tmp_path, kind, source["revision"])
    assert len(calls) == 1
    assert all("TEST-ONLY-SAS" not in path.read_text() for path in tmp_path.glob("*.json"))


def test_supplemental_preflight_request_failure_has_no_raw_error_or_retry(tmp_path, monkeypatch, publication_case, publication_supplemental):
    config, source = publication_case
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise ValueError("TEST-ONLY-SAS")

    monkeypatch.setattr(release, "azure", fail)
    with pytest.raises(ValueError, match="failed or is unknown") as error:
        release.publication_metadata_preflight(config, tmp_path, source["revision"], publication_supplemental)
    assert "TEST-ONLY-SAS" not in str(error.value)
    result = release.private_json(tmp_path / "metadata-preflight-result.json")
    assert result["status"] == "failed_or_unknown" and result["stage"] == "metadata_request"
    assert not (tmp_path / "backend-publication-supplemental-attempt.json").exists()
    assert len(calls) == 1


@pytest.mark.parametrize("status", ["Succeeded", "Failed", "Unknown"])
def test_supplemental_full_publish_reuses_metadata_and_preserves_all_counters(tmp_path, monkeypatch, publication_case, publication_supplemental, publication_replacement, publication_transport, status):
    from argparse import Namespace
    config, _ = publication_case
    path = tmp_path / "supplemental-approval.json"
    release.save(path, publication_supplemental)
    originals = {name: (tmp_path / name).read_bytes() for name in ("backend-publication-attempt.json", "backend-publication-replacement-attempt.json")}
    calls, requests, archives = publication_transport(tmp_path, config, status)
    azure, upload = release.azure, release.upload_publication_context
    metadata, uploaded = [], []

    def request(*args, **kwargs):
        result = azure(*args, **kwargs)
        if "/listBuildSourceUploadUrl?" in " ".join(args):
            if not metadata:
                assert (tmp_path / "metadata-preflight-attempt.json").exists()
                assert not (tmp_path / "backend-publication-supplemental-attempt.json").exists()
            metadata.append(result)
        return result

    def upload_context(location, archive, timeout):
        assert location is metadata[len(uploaded)]
        uploaded.append(location)
        return upload(location, archive, timeout)

    monkeypatch.setattr(release, "azure", request)
    monkeypatch.setattr(release, "upload_publication_context", upload_context)
    options = Namespace(action="publish", approve="publish", work=tmp_path, config=None, publication_supplemental_approval=path)
    if status == "Succeeded":
        release.run(options)
        assert len(metadata) == len(requests) == len(archives) == 2
        assert (tmp_path / "images.json").exists()
        assert (tmp_path / "frontend-publication-attempt.json").exists()
    else:
        with pytest.raises(ValueError, match="did not succeed|unknown"):
            release.run(options)
        assert len(metadata) == len(requests) == len(archives) == 1
        assert not (tmp_path / "frontend-publication-attempt.json").exists()
        assert not (tmp_path / "images.json").exists()
    assert sum(body["imageNames"][0].startswith("docintel/backend:") for body in requests.values()) == 1
    assert release.private_json(tmp_path / "metadata-preflight-result.json")["status"] == "validated"
    assert (tmp_path / "backend-publication-supplemental-attempt.json").exists()
    assert all((tmp_path / name).read_bytes() == value for name, value in originals.items())
    assert all("TEST-ONLY-SAS" not in record.read_text() for record in tmp_path.glob("*.json"))
    count = len(calls)
    for extra in (None, "replacement", "supplemental"):
        options.publication_supplemental_approval = path if extra == "supplemental" else None
        replacement_path = tmp_path / "old-replacement-approval.json"
        release.save(replacement_path, publication_replacement)
        options.publication_replacement_approval = replacement_path if extra == "replacement" else None
        with pytest.raises(ValueError, match="already attempted"):
            release.run(options)
    for kind in ("backend", "frontend"):
        with pytest.raises(ValueError, match="already attempted"):
            release.publish_component(config, tmp_path, None, kind)
    assert len(calls) == count


def test_supplemental_preflight_is_atomic(tmp_path, monkeypatch, publication_case, publication_supplemental):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    config, source = publication_case
    barrier = threading.Barrier(4)
    calls = []
    location = {"relativePath": "opaque", "uploadUrl": "https://registryaccount.blob.core.windows.net/container/opaque?sig=TEST-ONLY-SAS&se=2030-02-03T13:00:00Z&sp=rw"}
    monkeypatch.setattr(release, "azure", lambda *args, **kwargs: calls.append(args) or location)

    def preflight(index):
        barrier.wait()
        try:
            return release.publication_metadata_preflight(config, tmp_path, source["revision"], publication_supplemental)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(preflight, range(4)))
    assert sum(value is location for value in outcomes) == 1 and len(calls) == 1
    assert not (tmp_path / "backend-publication-supplemental-attempt.json").exists()


@pytest.mark.parametrize("action,with_replacement", [("publish-backend", False), ("publish-frontend", False), ("publish", True)])
def test_supplemental_flag_rejects_other_actions_and_coexisting_approval(tmp_path, monkeypatch, publication_case, publication_supplemental, action, with_replacement):
    from argparse import Namespace
    path = tmp_path / "supplemental-approval.json"
    release.save(path, publication_supplemental)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: pytest.fail("Azure target read"))
    with pytest.raises(ValueError, match="full publish|mutually exclusive"):
        release.run(Namespace(action=action, approve=action, work=tmp_path, config=None, publication_supplemental_approval=path, publication_replacement_approval=path if with_replacement else None))


@pytest.mark.parametrize("record", release.SUPPLEMENTAL_PUBLICATION_RECORDS)
def test_supplemental_residual_receipts_never_reopen_preflight(tmp_path, monkeypatch, publication_case, publication_supplemental, record):
    config, source = publication_case
    release.save(tmp_path / record, {})
    monkeypatch.setattr(release, "azure", lambda *args, **kwargs: pytest.fail("Azure called"))
    with pytest.raises(ValueError, match="already attempted"):
        release.publication_metadata_preflight(config, tmp_path, source["revision"], publication_supplemental)
    for kind in ("backend", "frontend"):
        with pytest.raises(ValueError, match="already attempted"):
            release.reserve_publication_attempt(tmp_path, kind, source["revision"])


def test_supplemental_expiring_cached_sas_never_refreshes_metadata(tmp_path, monkeypatch, publication_case, publication_supplemental, publication_transport):
    from argparse import Namespace
    from datetime import datetime, timezone
    config, _ = publication_case
    approval_path = tmp_path / "supplemental-approval.json"
    release.save(approval_path, publication_supplemental)
    calls, requests, uploads = publication_transport(tmp_path, config)
    azure, archive = release.azure, release.archive_publication_context
    now = [datetime(2030, 2, 3, 10, tzinfo=timezone.utc)]
    monkeypatch.setattr(release.datetime, "now", classmethod(lambda cls, tz=None: now[0]))

    def request(*args, **kwargs):
        result = azure(*args, **kwargs)
        if "/listBuildSourceUploadUrl?" in " ".join(args):
            result["uploadUrl"] = result["uploadUrl"].replace("2030-02-04T13:00:00Z", "2030-02-03T10:22:00Z")
        return result

    def archive_context(*args):
        result = archive(*args)
        now[0] = datetime(2030, 2, 3, 10, 5, tzinfo=timezone.utc)
        return result

    monkeypatch.setattr(release, "azure", request)
    monkeypatch.setattr(release, "archive_publication_context", archive_context)
    with pytest.raises(release.PublicationUploadError, match="sas_expiry"):
        release.run(Namespace(action="publish", approve="publish", work=tmp_path, config=None, publication_supplemental_approval=approval_path))
    assert len(calls) == 1 and not requests and not uploads
    assert (tmp_path / "backend-publication-supplemental-attempt.json").exists()
    assert not (tmp_path / "frontend-publication-attempt.json").exists()


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("approved", False), ("component", "frontend"),
    ("additional_attempts", 2), ("additional_attempts", True), ("cpu", 4), ("timeout_seconds", 901),
    ("approved_by", "33333333-3333-4333-8333-333333333333"), ("id", "not-a-uuid"),
    ("target", "0" * 64), ("work", "/another/work"), ("revision", "b" * 40),
    ("original_attempt", "/another/attempt.json"), ("original_attempt_sha256", "0" * 64),
    ("original_failure", "unknown_remote_run"), ("expires_at", "2030-02-03T09:59:00Z"),
    ("expires_at", "2030-02-03T13:00:01Z"), ("expires_at", "2030-02-03T12:00:00"),
    ("extra", "forbidden"),
])
def test_publication_replacement_rejects_scope_drift(tmp_path, publication_case, publication_replacement, field, value):
    config, source = publication_case
    changed = {**publication_replacement, field: value}
    before = (tmp_path / "backend-publication-attempt.json").read_bytes()
    with pytest.raises(ValueError):
        release.reserve_publication_attempt(tmp_path, "backend", source["revision"], replacement=changed, config=config)
    assert (tmp_path / "backend-publication-attempt.json").read_bytes() == before
    assert not (tmp_path / "backend-publication-replacement-attempt.json").exists()


@pytest.mark.parametrize("deadline", [
    "missing", None, False, 123, "", "invalid",
    "2030-02-03T13:00:00", "2030-02-03T09:59:59Z", "2030-02-03T12:59:59Z",
])
def test_publication_replacement_requires_valid_active_private_target_deadline(tmp_path, publication_case, publication_replacement, deadline):
    config, source = publication_case
    target = {**config, "publication_deadline": deadline}
    if deadline == "missing":
        target.pop("publication_deadline")
    approval = {**publication_replacement, "target": release.fingerprint(target)}
    with pytest.raises(ValueError, match="publication_deadline"):
        release.reserve_publication_attempt(tmp_path, "backend", source["revision"], replacement=approval, config=target)
    assert not (tmp_path / "backend-publication-replacement-attempt.json").exists()


@pytest.mark.parametrize("deadline", ["missing", "2030-02-03T13:00:00Z", "2030-02-03T08:00:00-05:00", None, False, "", "invalid", "2030-02-03T13:00:00"])
def test_load_config_validates_optional_private_publication_deadline(tmp_path, deadline):
    config = {field: "synthetic" for field in ("group", "environment", "registry", "storage", "frontend", "backend", "job", "container", "auth_secret_ref", "entra_secret_ref")}
    config.update({
        "subscription": "11111111-1111-4111-8111-111111111111",
        "tenant": "22222222-2222-4222-8222-222222222222",
        "frontend_origin": "https://frontend.invalid", "backend_origin": "https://backend.invalid",
    })
    if deadline != "missing":
        config["publication_deadline"] = deadline
    path = tmp_path / "target.json"
    release.save(path, config)
    if deadline in ("missing", "2030-02-03T13:00:00Z", "2030-02-03T08:00:00-05:00"):
        assert release.load_config(path) == config
    else:
        with pytest.raises(ValueError, match="publication_deadline"):
            release.load_config(path)


@pytest.mark.parametrize("evidence", ["backend-publication-result.json", "backend-publication-queued.json", "backend-publication-request.json", "backend-publication-submission.json"])
def test_publication_replacement_rejects_remote_evidence(tmp_path, publication_case, publication_replacement, evidence):
    config, source = publication_case
    release.save(tmp_path / evidence, {})
    with pytest.raises(ValueError, match="remote-submission"):
        release.reserve_publication_attempt(tmp_path, "backend", source["revision"], replacement=publication_replacement, config=config)


def test_publication_replacement_is_atomic_and_id_changes_do_not_renew(tmp_path, publication_case, publication_replacement):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    config, source = publication_case
    barrier = threading.Barrier(4)
    original = (tmp_path / "backend-publication-attempt.json").read_bytes()

    def reserve(index):
        candidate = {**publication_replacement, "id": str(release.uuid.uuid4())}
        barrier.wait()
        try:
            return release.reserve_publication_attempt(tmp_path, "backend", source["revision"], replacement=candidate, config=config)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(path is not None for path in pool.map(reserve, range(4))) == 1
    receipt = (tmp_path / "backend-publication-replacement-attempt.json").read_bytes()
    for replacement in (None, publication_replacement, {**publication_replacement, "id": str(release.uuid.uuid4())}):
        with pytest.raises(ValueError, match="already attempted"):
            release.reserve_publication_attempt(tmp_path, "backend", source["revision"], replacement=replacement, config=config)
    release.reserve_publication_attempt(tmp_path, "frontend", source["revision"])
    assert (tmp_path / "backend-publication-attempt.json").read_bytes() == original
    assert (tmp_path / "backend-publication-replacement-attempt.json").read_bytes() == receipt


@pytest.mark.parametrize("first_path", ["publish", "publish-backend"])
@pytest.mark.parametrize("status", ["Succeeded", "Failed", "Unknown"])
def test_publication_replacement_shared_by_full_and_component_paths(tmp_path, publication_case, publication_replacement, publication_transport, first_path, status):
    from argparse import Namespace
    config, source = publication_case
    previous = tmp_path / "previous"
    release.save(previous / "source.json", {**source, "revision": "b" * 40})
    release.save(previous / "images.json", {"revision": "b" * 40, "frontend": config["frontend_image"]})
    path = tmp_path / "replacement-approval.json"
    release.save(path, publication_replacement)
    calls, requests, _ = publication_transport(tmp_path, config, status)
    options = Namespace(action=first_path, approve=first_path, work=tmp_path, config=None, previous_work=previous, publication_replacement_approval=path)
    original = (tmp_path / "backend-publication-attempt.json").read_bytes()
    if status == "Succeeded":
        release.run(options)
        assert len(requests) == (2 if first_path == "publish" else 1)
    else:
        with pytest.raises(ValueError, match="did not succeed|unknown"):
            release.run(options)
        assert len(requests) == 1
    count = len(calls)
    for next_action in ("publish", "publish-backend"):
        options.action = options.approve = next_action
        with pytest.raises(ValueError, match="already attempted"):
            release.run(options)
    assert len(calls) == count
    assert (tmp_path / "backend-publication-attempt.json").read_bytes() == original
    assert not (tmp_path / "backend-publication-result.json").exists()
    assert (tmp_path / "backend-publication-replacement-attempt.json").exists()
    if first_path == "publish" and status == "Succeeded":
        assert (tmp_path / "frontend-publication-attempt.json").exists()


@pytest.mark.parametrize("defect", ["cpu", "digest", "empty_202", "output", "run_id", "expired_poll", "request_error"])
def test_publication_never_reschedules_on_unknown_or_invalid_run(tmp_path, monkeypatch, publication_case, publication_transport, defect):
    config, source = publication_case
    calls, requests, _ = publication_transport(tmp_path, config, "Running" if defect == "expired_poll" else "Succeeded", cpu=4 if defect == "cpu" else 2, digest_mismatch=defect == "digest")
    transport = release.azure

    def azure(*args, **kwargs):
        response = transport(*args, **kwargs)
        if "/scheduleRun?" in " ".join(args):
            if defect == "empty_202":
                return None
            if defect == "request_error":
                raise ValueError("Unknown submission outcome")
        if "/runs/" in " ".join(args):
            if defect == "output":
                response["properties"]["outputImages"][0]["tag"] = "different"
            if defect == "run_id":
                response["properties"]["runId"] = "different"
        return response

    monkeypatch.setattr(release, "azure", azure)
    clock = [0]
    monkeypatch.setattr(release.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(release.time, "sleep", lambda _: clock.__setitem__(0, 1300))
    with pytest.raises(ValueError):
        release.build_publication(config, tmp_path, "backend", source["revision"])
    assert len(requests) == 1
    count = len(calls)
    with pytest.raises(ValueError, match="already attempted"):
        release.build_publication(config, tmp_path, "backend", source["revision"])
    assert len(calls) == count
    assert not list(tmp_path.glob("*-context.tar.gz"))
    assert (tmp_path / "backend-publication-submission.json").exists()
    assert (tmp_path / "backend-publication-queued.json").exists() == (defect not in {"empty_202", "request_error"})


@pytest.mark.parametrize("kind", ["backend", "frontend"])
def test_publication_replacement_requires_full_remaining_build_window(tmp_path, publication_case, publication_replacement, publication_transport, kind):
    config, source = publication_case
    publication_replacement["expires_at"] = "2030-02-03T10:14:59Z"
    calls, requests, _ = publication_transport(tmp_path, config)
    with pytest.raises(ValueError, match="Insufficient approved time"):
        release.build_publication(config, tmp_path, kind, source["revision"], replacement=publication_replacement)
    assert not calls and not requests
    stem = "backend-publication-replacement" if kind == "backend" else "frontend-publication"
    assert (tmp_path / (stem + "-attempt.json")).exists()


@pytest.mark.parametrize("kind", ["backend", "frontend"])
@pytest.mark.parametrize("stage", ["list", "upload", "queue", "final_queue"])
def test_publication_rechecks_full_window_before_every_upload_and_queue(tmp_path, monkeypatch, publication_case, publication_replacement, publication_transport, kind, stage):
    from datetime import datetime, timezone

    config, source = publication_case
    publication_replacement["expires_at"] = "2030-02-03T10:15:01Z"
    now = [datetime(2030, 2, 3, 10, tzinfo=timezone.utc)]
    monkeypatch.setattr(release.datetime, "now", classmethod(lambda cls, tz=None: now[0]))
    calls, requests, _ = publication_transport(tmp_path, config)
    archive, azure, upload, save_once = release.archive_publication_context, release.azure, release.upload_publication_context, release.save_once
    uploads = []

    def advance():
        now[0] = datetime(2030, 2, 3, 10, 0, 2, tzinfo=timezone.utc)

    def archive_context(*args):
        result = archive(*args)
        if stage == "list":
            advance()
        return result

    def request(*args, **kwargs):
        result = azure(*args, **kwargs)
        if stage == "upload" and "/listBuildSourceUploadUrl?" in " ".join(args):
            advance()
        return result

    def upload_context(*args):
        uploads.append(args)
        result = upload(*args)
        if stage == "queue":
            advance()
        return result

    def save_receipt(path, data):
        save_once(path, data)
        if stage == "final_queue" and path.name.endswith("-submission.json"):
            advance()

    monkeypatch.setattr(release, "archive_publication_context", archive_context)
    monkeypatch.setattr(release, "azure", request)
    monkeypatch.setattr(release, "upload_publication_context", upload_context)
    monkeypatch.setattr(release, "save_once", save_receipt)
    with pytest.raises(ValueError, match="Insufficient approved time"):
        release.build_publication(config, tmp_path, kind, source["revision"], replacement=publication_replacement)
    assert not requests
    assert len(calls) == (0 if stage == "list" else 1)
    assert len(uploads) == (1 if stage in {"queue", "final_queue"} else 0)


def test_publication_frontend_cannot_upload_after_backend_uses_remaining_window(tmp_path, monkeypatch, publication_case, publication_replacement, publication_transport):
    from argparse import Namespace
    from datetime import datetime, timezone

    config, _ = publication_case
    publication_replacement["expires_at"] = "2030-02-03T10:15:01Z"
    now = [datetime(2030, 2, 3, 10, tzinfo=timezone.utc)]
    monkeypatch.setattr(release.datetime, "now", classmethod(lambda cls, tz=None: now[0]))
    path = tmp_path / "replacement-approval.json"
    release.save(path, publication_replacement)
    calls, requests, archives = publication_transport(tmp_path, config)
    azure = release.azure

    def request(*args, **kwargs):
        result = azure(*args, **kwargs)
        if args[:2] == ("acr", "repository"):
            now[0] = datetime(2030, 2, 3, 10, 0, 2, tzinfo=timezone.utc)
        return result

    monkeypatch.setattr(release, "azure", request)
    with pytest.raises(ValueError, match="Insufficient approved time"):
        release.run(Namespace(action="publish", approve="publish", work=tmp_path, config=None, publication_replacement_approval=path))
    assert len(requests) == len(archives) == 1
    assert sum("/listBuildSourceUploadUrl?" in " ".join(call) for call in calls) == 1
    assert not (tmp_path / "frontend-publication-queued.json").exists()
    assert (tmp_path / "backend-publication-replacement-result.json").exists()


@pytest.mark.parametrize("defect", ["bytes", "permissions", "symlink", "evidence"])
def test_publication_archive_rejects_drift_and_private_evidence(tmp_path, publication_case, publication_transport, defect):
    config, source = publication_case
    context = tmp_path / "context"
    target = context / "backend/main.py"
    if defect == "bytes":
        target.write_text("changed")
    elif defect == "permissions":
        target.chmod(0o400)
    elif defect == "symlink":
        target.unlink()
        target.symlink_to(context / "backend/Dockerfile")
    else:
        target = context / "backend/private.pdf"
        target.write_bytes(b"private evidence")
        release.save(tmp_path / "context.json", {str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest() for path in context.rglob("*") if path.is_file()})
        release.save(tmp_path / "context-modes.json", {str(path.relative_to(context)): path.stat().st_mode & 0o777 for path in context.rglob("*")})
    calls, _, _ = publication_transport(tmp_path, config)
    with pytest.raises(ValueError):
        release.build_publication(config, tmp_path, "backend", source["revision"])
    assert not calls


@pytest.mark.parametrize("failure", [False, True])
def test_publication_blob_put_never_exposes_sas_or_retries(tmp_path, monkeypatch, failure, capsys):
    from types import SimpleNamespace
    url = "https://registryaccount.blob.core.windows.net/context/source/20300203/source.tar.gz?sig=PRIVATE-SAS"
    calls = []
    (tmp_path / "source.tar.gz").write_bytes(b"SYNTHETIC-ARCHIVE")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert "PRIVATE-SAS" not in " ".join(args)
        assert b"PRIVATE-SAS" in kwargs["input"]
        assert args[:2] == ["curl", "--disable"]
        assert args[args.index("--retry") + 1] == "0"
        assert args[args.index("--max-redirs") + 1] == "0"
        assert args[args.index("--header") + 1] == "x-ms-blob-type: BlockBlob"
        assert kwargs["timeout"] == 30
        return SimpleNamespace(returncode=1 if failure else 0, stdout=b"403 PRIVATE-SAS" if failure else b"201", stderr=b"PRIVATE-SAS")

    monkeypatch.setattr(release.subprocess, "run", run)
    location = {"relativePath": "source/20300203/source.tar.gz", "uploadUrl": url}
    if failure:
        with pytest.raises(ValueError, match="details withheld") as error:
            release.upload_publication_context(location, tmp_path / "source.tar.gz", 30)
        assert "PRIVATE-SAS" not in str(error.value)
    else:
        assert release.upload_publication_context(location, tmp_path / "source.tar.gz", 30) == location["relativePath"]
    assert len(calls) == 1 and not capsys.readouterr().out


@pytest.mark.parametrize("source", [
    "opaque-object", "context.zip", "folder/build.tgz", "another/container/blob.bundle",
    "e84a913d-3b50-41df-8564-271662992195", "names.with.dots/archive..part",
    "unicode/évidence package", "escaped/context%20package",
])
def test_publication_accepts_safe_opaque_relative_paths_without_prefix_or_extension(tmp_path, monkeypatch, source):
    """Representative synthetic strings, not recovered provider response values."""
    from types import SimpleNamespace
    from urllib.parse import quote
    calls = []
    (tmp_path / "context.tar.gz").write_bytes(b"SYNTHETIC-ARCHIVE")
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout=b"201"))
    blob_path = quote(source, safe="/%")
    location = {"relativePath": source, "uploadUrl": "https://registryaccount.blob.core.windows.net/container/" + blob_path + "?sig=SYNTHETIC"}
    assert release.upload_publication_context(location, tmp_path / "context.tar.gz", 30) == source
    assert len(calls) == 1
    for kind in ("backend", "frontend"):
        assert release.publication_request(kind, "a" * 40, source)["sourceLocation"] == source


@pytest.mark.parametrize("source", [
    None, 123, [], "", "/absolute", "//host/path", "https://host.invalid/blob",
    "../blob", "path/../blob", "path/./blob", "path//blob", "path/", "path\\blob",
    "path?sig=PRIVATE", "path#fragment", "path\nblob", "path\x7fblob", "x" * 4097,
    "%2e%2e/blob", "path/%2fblob", "path/%5cblob", "%252e%252e/blob", "path/%GG",
    "https%3a/host/blob", "path/%0ablob",
])
def test_publication_rejects_unsafe_relative_paths_without_disclosing_values(tmp_path, monkeypatch, source):
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: pytest.fail("Upload attempted"))
    location = {"relativePath": source, "uploadUrl": "https://registryaccount.blob.core.windows.net/container/blob?sig=PRIVATE-SAS"}
    with pytest.raises(ValueError, match="safe metadata") as error:
        release.upload_publication_context(location, tmp_path / "archive", 30)
    assert "PRIVATE" not in str(error.value)
    with pytest.raises(ValueError):
        release.publication_request("backend", "a" * 40, source)


@pytest.mark.parametrize("suffix", [
    "/container/prefixblob?sig=PRIVATE-SAS", "/container/blob/extra?sig=PRIVATE-SAS",
    "/container/%2e%2e/blob?sig=PRIVATE-SAS", "/container/path%2fblob?sig=PRIVATE-SAS",
    "/container/blob?sig=", "/container/blob?sig=one&sig=PRIVATE-SAS",
    "/container/blob?other=PRIVATE-SAS",
])
def test_publication_requires_exact_blob_path_binding_and_sas(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: pytest.fail("Upload attempted"))
    with pytest.raises(ValueError, match="destination") as error:
        release.upload_publication_context({"relativePath": "blob", "uploadUrl": "https://registryaccount.blob.core.windows.net" + suffix}, tmp_path / "archive", 30)
    assert "PRIVATE-SAS" not in str(error.value)


@pytest.mark.parametrize("upload", [
    None, [], {"relativePath": 123, "uploadUrl": False},
    {"relativePath": "confidential-marker/../blob", "uploadUrl": "https://registryaccount.blob.core.windows.net/container/blob?sig=PRIVATE-SAS"},
    {"relativePath": "blob", "uploadUrl": "https://registryaccount.blob.core.windows.net:PRIVATE-SAS/container/blob?sig=PRIVATE-SAS"},
    {"relativePath": "blob", "uploadUrl": "https://[PRIVATE-SAS/container/blob?sig=PRIVATE-SAS"},
])
def test_publication_provider_diagnostics_are_safe_and_persist_before_rejection(tmp_path, monkeypatch, publication_case, upload):
    config, source = publication_case
    calls = []
    run = release.subprocess.run

    def offline_run(arguments, **kwargs):
        assert arguments[0] != "curl", "Upload attempted"
        return run(arguments, **kwargs)

    def azure(*arguments, **kwargs):
        assert "/listBuildSourceUploadUrl?" in " ".join(arguments)
        calls.append(arguments)
        return upload

    monkeypatch.setattr(release.subprocess, "run", offline_run)
    monkeypatch.setattr(release, "azure", azure)
    with pytest.raises(ValueError, match="safe metadata") as error:
        release.build_publication(config, tmp_path, "backend", source["revision"])
    record = release.private_json(tmp_path / "backend-publication-upload-metadata.json")
    assert record == release.publication_upload_metadata(upload)
    diagnostics = json.dumps(record) + str(error.value)
    assert "PRIVATE-SAS" not in diagnostics and "confidential-marker" not in diagnostics
    assert "https://" not in diagnostics and "?sig=" not in diagnostics
    assert record["response_type"] == type(upload).__name__
    if isinstance(upload, dict) and isinstance(upload["relativePath"], str):
        assert record["relative_path"]["sha256"] == hashlib.sha256(upload["relativePath"].encode()).hexdigest()
        assert record["relative_path"]["bytes"] == len(upload["relativePath"].encode())
    assert len(calls) == 1
    assert (tmp_path / "backend-publication-attempt.json").exists()
    assert not (tmp_path / "backend-publication-submission.json").exists()
    with pytest.raises(ValueError, match="already attempted"):
        release.build_publication(config, tmp_path, "backend", source["revision"])
    assert len(calls) == 1


@pytest.mark.parametrize("url", ["http://registryaccount.blob.core.windows.net/source/20300203/source.tar.gz?sig=private", "https://untrusted.invalid/source/20300203/source.tar.gz?sig=private", "https://registryaccount.blob.core.windows.net/different.tar.gz?sig=private"])
def test_publication_upload_rejects_redirect_or_unbound_destination(tmp_path, monkeypatch, url):
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: pytest.fail("Upload attempted"))
    with pytest.raises(ValueError, match="destination"):
        release.upload_publication_context({"relativePath": "source/20300203/source.tar.gz", "uploadUrl": url}, tmp_path / "archive", 30)


@pytest.fixture
def isolated_azure_cli(tmp_path, monkeypatch):
    import os
    import shutil

    executable = shutil.which("az")
    if executable is None:
        pytest.skip("The installed Azure CLI is required for the release-host contract test")
    for name in list(os.environ):
        if name.upper().startswith(("AZURE_", "ARM_", "MSI_", "IDENTITY_", "IMDS_")):
            monkeypatch.delenv(name)
    for name, directory in (("HOME", "home"), ("AZURE_CONFIG_DIR", "azure-config"), ("AZURE_EXTENSION_DIR", "azure-extensions")):
        path = tmp_path / directory
        path.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(path))
    for name, value in {
        "AZURE_CORE_COLLECT_TELEMETRY": "no", "AZURE_CORE_CHECK_VERSION": "false",
        "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no", "AZURE_CORE_NO_COLOR": "true",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    return executable


def test_publication_contract_with_installed_real_azure_cli(isolated_azure_cli):
    """Offline contract check, not a mock of the unsupported `acr build --cpu`."""
    import ast
    import shutil
    import subprocess
    import sys

    site = Path("/usr/local/Cellar/azure-cli/2.77.0/libexec/lib/python3.13/site-packages")
    build = site / "azure/cli/command_modules/acr/build.py"
    if not build.is_file() or not shutil.which("az"):
        pytest.skip("Azure CLI 2.77.0 contract is checked on the installed release host")
    tree = ast.parse(build.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "acr_build")
    assert "cpu" not in {argument.arg for argument in function.args.args}
    constructor = next(node for node in ast.walk(function) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DockerBuildRequest")
    assert "agent_configuration" not in {keyword.arg for keyword in constructor.keywords}
    help_result = subprocess.run(["az", "rest", "--help"], capture_output=True, timeout=60, check=True)
    for argument in ("--method", "--url", "--body", "--headers", "--only-show-errors", "--output"):
        assert argument.encode() in help_result.stdout
    raw = ast.parse((site / "azure/cli/core/util.py").read_text())
    send = next(node for node in raw.body if isinstance(node, ast.FunctionDef) and node.name == "send_raw_request")
    assert sum(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "send" for node in ast.walk(send)) == 1
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "mount" for node in ast.walk(send))
    archive_tree = ast.parse((site / "azure/cli/command_modules/acr/_archive_utils.py").read_text())
    upload = next(node for node in archive_tree.body if isinstance(node, ast.FunctionDef) and node.name == "upload_source_code")
    assignments = [node for node in ast.walk(upload) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "relative_path" for target in node.targets)]
    assert len(assignments) == 2
    assert any(isinstance(node.value, ast.Attribute) and node.value.attr == "relative_path" for node in assignments)
    assert any(isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id == "relative_path" for node in ast.walk(upload))
    body = release.publication_request("backend", "a" * 40, "opaque-object")
    code = (
        "import json,sys; sys.path.insert(0,sys.argv[1]); "
        "from azure.mgmt.containerregistry.v2019_06_01_preview.models import DockerBuildRequest, SourceUploadDefinition; "
        "assert SourceUploadDefinition._attribute_map == {'upload_url': {'key': 'uploadUrl', 'type': 'str'}, 'relative_path': {'key': 'relativePath', 'type': 'str'}}; "
        "assert not SourceUploadDefinition._validation.get('relative_path'); "
        "upload=SourceUploadDefinition.deserialize({'relativePath':'opaque-object','uploadUrl':'https://synthetic.invalid/blob'}); "
        "assert upload.relative_path=='opaque-object' and upload.serialize()['relativePath']=='opaque-object'; "
        "body=json.loads(sys.argv[2]); model=DockerBuildRequest.deserialize(body); "
        "actual=model.serialize(); assert all(actual[k]==v for k,v in body.items()); "
        "assert model.agent_configuration.cpu==2 and model.timeout==900; print('verified')"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code, str(site), json.dumps(body)], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.strip() == b"verified"


def test_publication_real_azure_cli_sends_exact_arm_requests_over_loopback(tmp_path, monkeypatch, isolated_azure_cli):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    requests = []
    path = "/subscriptions/00000000-0000-4000-8000-000000000000/resourceGroups/synthetic/providers/Microsoft.ContainerRegistry/registries/synthetic/scheduleRun?api-version=2019-04-01"
    queued = {"properties": {"runId": "local-contract-only", "status": "Queued", "agentConfiguration": {"cpu": 2}}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.path, dict(self.headers), body))
            payload = json.dumps(queued).encode()
            self.send_response(200 if self.path == path else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(name, url)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for kind in ("backend", "frontend"):
                body = release.publication_request(kind, "a" * 40, "opaque-object" if kind == "backend" else "nested path/frontend.bundle")
                request_path = tmp_path / "private request files" / (kind + "-request.json")
                release.save(request_path, body)
                request_id = str(release.uuid.uuid4())
                # Real production subprocess/parser, with only a loopback URL and
                # explicit no-auth flag substituted; no CLI/transport mocking.
                result = release.azure(
                    "rest", "--method", "POST", "--url", url + path,
                    "--body", "@" + str(request_path),
                    "--headers", "x-docintel-publication-attempt-id=" + request_id,
                    "--skip-authorization-header", timeout=60,
                )
                assert result == queued
                assert len(requests) == (1 if kind == "backend" else 2)
                received_path, headers, received_body = requests[-1]
                assert received_path == path
                normalized = {key.lower(): value for key, value in headers.items()}
                assert "authorization" not in normalized
                assert normalized["x-docintel-publication-attempt-id"] == request_id
                assert str(release.uuid.UUID(normalized["x-ms-client-request-id"])) == normalized["x-ms-client-request-id"]
                assert normalized["content-type"].startswith("application/json")
                assert json.loads(received_body) == body
                assert json.loads(received_body)["agentConfiguration"] == {"cpu": 2}
                assert json.loads(received_body)["timeout"] == 900
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert not list((tmp_path / "azure-config").glob("*token*"))


def test_publication_paths_do_not_construct_acr_build_commands():
    import ast

    tree = ast.parse(Path(release.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "azure":
            leading = [argument.value if isinstance(argument, ast.Constant) else None for argument in node.args[:2]]
            assert leading != ["acr", "build"]


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
    ingress = {"targetPort": 80, "allowInsecure": False}
    baseline = {"target": release.fingerprint(config), "authenticated": False, "apps": {kind: {"containers": original, "ingress": ingress, "pinned_image": "old@sha256:digest"} for kind in ("backend", "frontend")}}
    release.save(tmp_path / "baseline.json", baseline)
    for kind in ("backend", "frontend"):
        release.save(tmp_path / (kind + "-intended.json"), current)
    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "desired_containers", lambda *args, **kwargs: current)
    state = {kind: {"properties": {"template": {"containers": copy.deepcopy(current)}, "configuration": {"ingress": release.desired_ingress(kind, ingress)}}} for kind in ("backend", "frontend")}
    monkeypatch.setattr(release, "app", lambda _, kind: state[kind])
    actions = []
    monkeypatch.setattr(release, "stop", lambda _: actions.append("stop"))
    monkeypatch.setattr(release, "azure", lambda *args: actions.append(args))
    def patch(config, kind, containers, work, *, ingress):
        actions.append((kind, containers))
        state[kind]["properties"]["template"]["containers"] = copy.deepcopy(containers)
        state[kind]["properties"]["configuration"]["ingress"] = ingress
    monkeypatch.setattr(release, "patch_app", patch)
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
    desired = [{"image": "new", "command": release.BACKEND_COMMAND, "args": release.BACKEND_ARGUMENTS, "env": [{"name": name, "value": "8080"} for name in ("API_PORT", "NEXT_PUBLIC_API_PORT")]}]
    ingress = {"targetPort": 80, "allowInsecure": False}
    release.save(tmp_path / "baseline.json", {"target": release.fingerprint(config), "apps": {kind: {"containers": original, "ingress": ingress} for kind in ("backend", "frontend")}})
    state = {"backend": desired, "frontend": original}
    ingress_state = {kind: release.desired_ingress(kind, ingress) for kind in ("backend", "frontend")}
    mutations = []
    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "require_release_images", lambda *args: None)
    monkeypatch.setattr(release, "identity_contract", lambda _: None)
    monkeypatch.setattr(release, "desired_containers", lambda *args, **kwargs: desired)
    monkeypatch.setattr(release, "azure", lambda *args: {"properties": {"healthState": "Healthy", "template": {"containers": desired}}})
    def resource(config, kind):
        return {"properties": {"provisioningState": "Succeeded" if ready else "Updating", "template": {"containers": state[kind]}, "configuration": {"secrets": [{"name": "auth-ref"}, {"name": "entra-ref"}], "ingress": ingress_state[kind]}, "latestRevisionName": "ready", "latestReadyRevisionName": "ready"}}
    def patch(config, kind, containers, work, *, ingress):
        mutations.append(kind)
        state[kind] = containers
        ingress_state[kind] = ingress
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
def runtime_release(tmp_path, monkeypatch, container_read_response, pilot_release):
    from types import SimpleNamespace
    config = pilot_release.config
    work = tmp_path / "runtime"
    resources = {kind: copy.deepcopy(container_read_response) for kind in ("backend", "frontend")}
    originals = {}
    for kind, resource in resources.items():
        container = resource["properties"]["template"]["containers"][0]
        container["image"] = "synthetic.azurecr.io/docintel/" + kind + "@sha256:" + "c" * 64
        if kind == "backend":
            container.pop("command")
            container.pop("args")
            container["env"].extend({"name": name, "value": value} for name, value in {
                "API_PORT": "80", "NEXT_PUBLIC_API_PORT": "80",
                "DOCINTEL_REAL_PILOT_ENABLED": "false", "DOCINTEL_PILOT_UPLOAD_ENABLED": "false",
            }.items())
            container["probes"].append({"type": "Startup", "httpGet": None, "tcpSocket": {"port": 80}, "failureThreshold": 30})
        else:
            container["probes"][0]["httpGet"]["port"] = 3000
            resource["properties"]["configuration"]["ingress"]["targetPort"] = 3000
        originals[kind] = copy.deepcopy(resource["properties"]["template"]["containers"])
        resource["properties"].update(provisioningState="Succeeded", latestRevisionName="ready", latestReadyRevisionName="ready")
        resource["properties"]["configuration"]["secrets"] = [{"name": config[name]} for name in ("auth_secret_ref", "entra_secret_ref")]
    baseline = {"target": release.fingerprint(config), "authenticated": True, "apps": {kind: {"containers": original, "pinned_image": original[0]["image"]} for kind, original in originals.items()}}
    release.save(work / "baseline.json", baseline)
    prior = release.desired_containers(config, "backend", originals["backend"], runtime=False)
    release.save(work / "backend-intended.json", prior)
    release.save(work / "backend-patch.json", {"properties": {"template": {"containers": release.writable_containers(prior)}}})
    resources["backend"]["properties"]["template"]["containers"] = copy.deepcopy(prior)
    resources["backend"]["properties"]["latestRevisionName"] = "failed-low-port"
    state = SimpleNamespace(config=config, work=work, resources=resources, originals=originals, baseline=baseline, calls=[], healthy=True)

    def azure(*arguments, **kwargs):
        if arguments[:3] == ("containerapp", "revision", "show"):
            kind = arguments[arguments.index("-n") + 1]
            return {"properties": {"healthState": "Healthy" if state.healthy else "Unhealthy", "template": copy.deepcopy(resources[kind]["properties"]["template"])}}
        assert arguments[:3] == ("rest", "--method", "PATCH")
        kind = arguments[arguments.index("--url") + 1].split("/containerApps/")[1].split("?")[0]
        path = Path(arguments[arguments.index("--body") + 1][1:])
        payload = release.private_json(path)
        state.calls.append((kind, payload, path))
        resources[kind]["properties"]["template"]["containers"] = copy.deepcopy(payload["properties"]["template"]["containers"])
        ingress = payload["properties"]["configuration"]["ingress"]
        resources[kind]["properties"]["configuration"]["ingress"] = {**container_read_response["properties"]["configuration"]["ingress"], **ingress} if ingress is not None else None
        resources[kind]["properties"].update(latestRevisionName="high-port-ready", latestReadyRevisionName="high-port-ready")
        return resources[kind]

    monkeypatch.setattr(release, "load_config", lambda _: config)
    monkeypatch.setattr(release, "verify_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(release, "require_release_images", lambda *args: None)
    monkeypatch.setattr(release, "identity_contract", lambda _: None)
    monkeypatch.setattr(release, "app", lambda _, kind: copy.deepcopy(resources[kind]))
    monkeypatch.setattr(release, "azure", azure)
    monkeypatch.setattr(release, "stop", lambda _: None)
    return state


def test_runtime_port_correction_is_atomic_preserves_receipts_and_rolls_back(runtime_release):
    from argparse import Namespace
    state = runtime_release
    original_bytes = {name: (state.work / name).read_bytes() for name in ("baseline.json", "backend-intended.json", "backend-patch.json")}
    original_ingress = {kind: copy.deepcopy(resource["properties"]["configuration"]["ingress"]) for kind, resource in state.resources.items()}
    options = Namespace(action="deploy", approve="deploy", work=state.work, config=None)
    release.run(options)
    assert [kind for kind, _, _ in state.calls] == ["backend", "frontend"]
    backend = state.calls[0][1]
    container = backend["properties"]["template"]["containers"][0]
    assert container["command"] == ["/app/.venv/bin/fastapi"]
    assert container["args"] == ["run", "backend/main.py", "--port", "8080", "--host", "0.0.0.0"]
    assert {name: release.environment_entries(container)[name]["value"] for name in ("API_PORT", "NEXT_PUBLIC_API_PORT")} == {"API_PORT": "8080", "NEXT_PUBLIC_API_PORT": "8080"}
    assert container["probes"][0]["httpGet"]["port"] == container["probes"][1]["tcpSocket"]["port"] == 8080
    assert container["probes"][0]["httpGet"]["httpHeaders"] == state.originals["backend"][0]["probes"][0]["httpGet"]["httpHeaders"]
    assert container["probes"][1]["failureThreshold"] == 30
    assert release.environment_entries(container)["DOCINTEL_REAL_PILOT_ENABLED"]["value"] == "false"
    assert release.environment_entries(container)["DOCINTEL_PILOT_UPLOAD_ENABLED"]["value"] == "false"
    expected_ingress = release.writable_ingress(original_ingress["backend"])
    expected_ingress["targetPort"] = 8080
    assert backend["properties"]["configuration"]["ingress"] == expected_ingress
    assert "fqdn" not in expected_ingress and "targetPortHttpScheme" not in expected_ingress
    assert state.calls[1][1]["properties"]["configuration"]["ingress"] == release.writable_ingress(original_ingress["frontend"])
    snapshot = release.private_json(state.work / "runtime-baseline.json")
    assert snapshot["baseline"] == state.baseline
    assert snapshot["baseline_sha256"] == hashlib.sha256(original_bytes["baseline.json"]).hexdigest()
    assert snapshot["apps"]["backend"]["ingress"] == original_ingress["backend"]
    assert all((state.work / name).read_bytes() == value for name, value in original_bytes.items())
    snapshot_bytes = (state.work / "runtime-baseline.json").read_bytes()
    release.run(options)
    assert len(state.calls) == 2
    assert (state.work / "runtime-baseline.json").read_bytes() == snapshot_bytes
    options.action = options.approve = "rollback"
    options.isolate_legacy_baseline = False
    release.run(options)
    restored = state.calls[2][1]
    assert restored["properties"]["template"]["containers"] == release.writable_containers(state.originals["backend"])
    assert restored["properties"]["configuration"]["ingress"] == release.writable_ingress(original_ingress["backend"])
    assert restored["properties"]["configuration"]["ingress"]["targetPort"] == 80
    assert state.calls[0][2] != state.calls[2][2]
    assert all((state.work / name).read_bytes() == value for name, value in original_bytes.items())


@pytest.mark.parametrize("defect", ["unrecorded", "changed_env", "changed_command", "changed_intent", "unknown_ingress"])
def test_runtime_continuation_rejects_unrelated_drift(runtime_release, defect):
    from argparse import Namespace
    state = runtime_release
    container = state.resources["backend"]["properties"]["template"]["containers"][0]
    if defect == "unrecorded":
        (state.work / "backend-intended.json").unlink()
    elif defect == "changed_env":
        container["env"].append({"name": "UNRELATED", "value": "drift"})
    elif defect == "changed_command":
        container["command"] = ["/unrelated/command"]
    elif defect == "changed_intent":
        release.save(state.work / "backend-intended.json", [{"image": "unrelated"}])
    else:
        state.resources["backend"]["properties"]["configuration"]["ingress"]["futureUnknown"] = True
    with pytest.raises(ValueError):
        release.run(Namespace(action="deploy", approve="deploy", work=state.work, config=None))
    assert not state.calls


@pytest.mark.parametrize("field", ["allowInsecure", "transport", "targetPort"])
def test_runtime_resume_rejects_ingress_drift_after_snapshot(runtime_release, field):
    from argparse import Namespace
    state = runtime_release
    options = Namespace(action="deploy", approve="deploy", work=state.work, config=None)
    release.run(options)
    count = len(state.calls)
    state.resources["backend"]["properties"]["configuration"]["ingress"][field] = {"allowInsecure": True, "transport": "tcp", "targetPort": 80}[field]
    with pytest.raises(ValueError, match="drift"):
        release.run(options)
    assert len(state.calls) == count


def test_runtime_unhealthy_backend_does_not_patch_frontend(runtime_release):
    from argparse import Namespace
    state = runtime_release
    state.healthy = False
    with pytest.raises(ValueError, match="not healthy"):
        release.run(Namespace(action="deploy", approve="deploy", work=state.work, config=None))
    assert [kind for kind, _, _ in state.calls] == ["backend"]


def test_fresh_capture_includes_exact_ingress(runtime_release):
    from argparse import Namespace
    state = runtime_release
    (state.work / "baseline.json").unlink()
    release.run(Namespace(action="capture", work=state.work, config=None))
    captured = release.private_json(state.work / "baseline.json")
    for kind in ("backend", "frontend"):
        assert captured["apps"][kind]["ingress"] == state.resources[kind]["properties"]["configuration"]["ingress"]
    assert not state.calls


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
            "id": prefix + "containerApps/" + kind, "etag": "synthetic-etag", "location": "westus",
            "identity": {"type": "SystemAssigned", "principalId": approval["identities"]["api_principal_id"]},
            "properties": {
                "provisioningState": "Succeeded", "latestRevisionName": "ready", "latestReadyRevisionName": "ready",
                "configuration": {"ingress": {"external": True, "allowInsecure": False, "targetPort": 8080 if kind == "backend" else 3000}, "secrets": [{"name": "existing-web-key"}]},
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


def internal_release_approval(state):
    state.approval["execution_scope"] = "internal_only"
    state.approval["customer_processing_approved"] = True
    for name in ("search", "web_retrieval", "retrieval"):
        state.approval["limits"][name] = 0
    state.approval["environment"] = {
        name: value for name, value in state.approval["environment"].items()
        if name not in release.PILOT_EXTERNAL_ENVIRONMENT_KEYS
    }
    state.approval["unit_prices_usd"] = {
        name: value for name, value in state.approval["unit_prices_usd"].items()
        if name in {"analysis_page", "input_token", "output_token"}
    }
    release.save(state.approval_path, state.approval)


def test_pilot_approval_matches_guard_schema_and_denies_unknown_secrets(pilot_release):
    from backend.real_pilot import HARD_LIMITS, ENVIRONMENT_KEYS, EXTERNAL_ENVIRONMENT_KEYS
    state = pilot_release
    assert release.PILOT_LIMITS == HARD_LIMITS
    assert release.PILOT_ENVIRONMENT_KEYS == ENVIRONMENT_KEYS
    assert release.PILOT_EXTERNAL_ENVIRONMENT_KEYS == EXTERNAL_ENVIRONMENT_KEYS
    assert release.load_pilot_approval(state.approval_path, state.approval["batch_id"]) == state.approval
    state.approval["environment"]["WEBIQ_API_KEY"] = "never-allowed"
    release.save(state.approval_path, state.approval)
    with pytest.raises(ValueError, match="Unsupported"):
        release.load_pilot_approval(state.approval_path, state.approval["batch_id"])
    assert not state.calls


def test_pilot_internal_only_approval_requires_no_web_prices_or_environment(pilot_release):
    state = pilot_release
    internal_release_approval(state)
    assert release.load_pilot_approval(state.approval_path, state.approval["batch_id"]) == state.approval
    assert state.approval["customer_processing_approved"] is True
    assert set(state.approval["unit_prices_usd"]) == {"analysis_page", "input_token", "output_token"}
    assert not set(state.approval["environment"]) & release.PILOT_EXTERNAL_ENVIRONMENT_KEYS


@pytest.mark.parametrize("defect", ["search", "web_retrieval", "retrieval", "scope", "environment", "price"])
def test_pilot_internal_only_packet_rejects_external_authority(pilot_release, defect):
    state = pilot_release
    internal_release_approval(state)
    if defect in ("search", "web_retrieval", "retrieval"):
        state.approval["limits"][defect] = 1
    elif defect == "scope":
        state.approval["execution_scope"] = "internal"
    elif defect == "environment":
        state.approval["environment"]["WEBIQ_ENDPOINT"] = "https://unverified.invalid"
    else:
        del state.approval["unit_prices_usd"]["analysis_page"]
    release.save(state.approval_path, state.approval)
    with pytest.raises(ValueError):
        release.load_pilot_approval(state.approval_path, state.approval["batch_id"])
    assert not state.calls


@pytest.mark.parametrize("scope", ["full", "internal_only"])
def test_pilot_packet_requires_customer_consent_in_every_scope(pilot_release, scope):
    state = pilot_release
    if scope == "internal_only":
        internal_release_approval(state)
    state.approval["customer_processing_approved"] = False
    release.save(state.approval_path, state.approval)
    with pytest.raises(ValueError, match="customer processing"):
        release.load_pilot_approval(state.approval_path, state.approval["batch_id"])
    assert not state.calls and not state.console_calls


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


@pytest.mark.parametrize("surface", ["app", "revision"])
@pytest.mark.parametrize("defect", ["command", "args", "probe", "port_env"])
def test_pilot_acceptance_requires_effective_nonroot_high_port_runtime(pilot_release, monkeypatch, surface, defect):
    state = pilot_release

    def change(container):
        if defect == "command":
            container.pop("command")
        elif defect == "args":
            container["args"] = ["run", "backend/main.py", "--port", "80"]
        elif defect == "probe":
            container["probes"] = [{"type": "Readiness", "httpGet": {"path": "/health", "port": 80}}]
        else:
            next(entry for entry in container["env"] if entry["name"] == "API_PORT")["value"] = "80"

    if surface == "app":
        change(state.resources["backend"]["properties"]["template"]["containers"][0])
    else:
        azure = release.azure

        def altered(*args):
            result = azure(*args)
            if args[:3] == ("containerapp", "revision", "show") and args[args.index("-n") + 1] == "backend":
                change(result["properties"]["template"]["containers"][0])
            return result

        monkeypatch.setattr(release, "azure", altered)
    with pytest.raises(ValueError):
        release.require_pilot_acceptance(state.config, state.work)
    assert not state.calls and not state.console_calls


@pytest.mark.parametrize("field,value", [("targetPort", 80), ("allowInsecure", True), ("unknownField", "drift")])
def test_pilot_acceptance_requires_matching_https_ingress(pilot_release, field, value):
    state = pilot_release
    state.resources["backend"]["properties"]["configuration"]["ingress"][field] = value
    with pytest.raises(ValueError):
        release.require_pilot_acceptance(state.config, state.work)
    assert not state.calls and not state.console_calls


def test_backend_runtime_command_is_accepted_by_installed_fastapi_cli():
    from fastapi_cli.cli import app
    from rich.text import Text
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, [*release.BACKEND_ARGUMENTS, "--help"])
    assert result.exit_code == 0, result.output
    output = Text.from_ansi(result.output).plain
    assert "--port" in output and "--host" in output


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


@pytest.mark.parametrize("execution_scope", ["full", "internal_only"])
def test_pilot_start_has_exact_cli_settings_and_bounded_immutable_attempts(pilot_release, execution_scope):
    state = pilot_release
    if execution_scope == "internal_only":
        internal_release_approval(state)
        release.merge_environment(state.job["properties"]["template"]["containers"][0], {
            "WEBIQ_ENDPOINT": "https://unverified.invalid", "WEBSEARCH_PROVIDER": "unverified",
            "AZURE_SEARCH_ENDPOINT": "https://unverified.invalid",
            "DOCINTEL_REAL_PILOT_EXECUTION_SCOPE": "full",
        })
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
        assert environment["DOCINTEL_REAL_PILOT_EXECUTION_SCOPE"]["value"] == execution_scope
        if execution_scope == "full":
            assert environment["WEBIQ_API_KEY"]["secretRef"] == "existing-web-key"
        else:
            assert not set(environment) & (release.PILOT_EXTERNAL_ENVIRONMENT_KEYS | {"WEBIQ_API_KEY"})
        attempt = release.private_json(state.work / f"pilot-execution-attempt-{number}.json")
        assert attempt["attempt"] == number
        assert attempt["execution_sha256"] == release.fingerprint(template)
        assert template["volumes"] == baseline["properties"]["template"]["volumes"]
    assert state.job == baseline
    with pytest.raises(ValueError, match="allowance exhausted"):
        release.pilot_start(state.config, state.approval, state.work, 2)
    assert len([call for call in state.calls if call[:3] == ("containerapp", "job", "start")]) == 2


def test_release_scope_change_cannot_reset_bound_approval(pilot_release):
    state = pilot_release
    enable_test_pilot(state)
    internal_release_approval(state)
    with pytest.raises(ValueError, match="cannot reset"):
        release.pilot_start(state.config, state.approval, state.work, 1)
    assert not any(call[:3] == ("containerapp", "job", "start") for call in state.calls)


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
@pytest.mark.parametrize("execution_scope", ["full", "internal_only"])
def test_pilot_configure_final_metadata_validates_intake_before_append(pilot_release, monkeypatch, capsys, batch_exists, execution_scope):
    from backend.batch_store import Missing
    from backend.real_pilot import binding_digest
    state = pilot_release
    if execution_scope == "internal_only":
        internal_release_approval(state)
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