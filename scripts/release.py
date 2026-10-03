"""Guarded release operations; private target configuration stays outside Git."""

import argparse
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib
import uuid


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"Succeeded", "Failed", "Stopped"}
OPERATIONS = {"provision", "publish", "seed", "deploy", "start", "stop", "rollback"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def command(arguments, *, cwd=None, env=None):
    completed = subprocess.run(arguments, cwd=cwd, env=env, capture_output=True)
    require(completed.returncode == 0, f"Command failed: {arguments[0]} {arguments[1]}; output withheld to protect private configuration")
    return completed.stdout


def azure(*arguments):
    output = command(["az", *arguments, "--only-show-errors", "-o", "json"])
    return json.loads(output) if output.strip() else None


def private_path(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(ROOT):
        relative = str(path.relative_to(ROOT))
        ignored = subprocess.run(["git", "check-ignore", "-q", relative], cwd=ROOT).returncode == 0
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", relative], cwd=ROOT, capture_output=True).returncode == 0
        require(ignored and not tracked, "Private state must be outside tracked source, in an ignored directory or outside the repository")
    return path


def save(path, data):
    path = private_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def load_config(path):
    path = private_path(path)
    require(path.stat().st_mode & 0o077 == 0, "Target configuration must be owner-only (mode 600)")
    config = json.loads(path.read_text())
    allowed = {"subscription", "tenant", "group", "environment", "registry", "storage", "frontend", "backend", "job", "container", "frontend_origin", "backend_origin", "api_client_id", "frontend_client_id", "frontend_image", "backend_image", "auth_secret_ref", "entra_secret_ref", "baseline_authenticated"}
    require(set(config) <= allowed, "Unknown/private-secret fields are forbidden in release configuration")
    for field in ("subscription", "tenant"):
        require(str(uuid.UUID(config[field])) == config[field], f"Invalid {field}")
    for field in ("group", "environment", "registry", "storage", "frontend", "backend", "job", "container", "auth_secret_ref", "entra_secret_ref"):
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,89}", config[field]), f"Invalid resource/reference name: {field}")
    for field in ("frontend_origin", "backend_origin"):
        require(re.fullmatch(r"https://[a-z0-9.-]+", config[field]), f"Invalid HTTPS origin: {field}")
    return config


def app(config, name):
    return azure("containerapp", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", name)


def verify_target(config, allow_isolated=False):
    account = azure("account", "show")
    require(account["id"] == config["subscription"] and account["tenantId"] == config["tenant"], "Selected Azure tenant/subscription differs from approved target")
    prefix = f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}/providers/"
    environment = azure("containerapp", "env", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config["environment"])
    require(environment["id"].lower() == (prefix + "Microsoft.App/managedEnvironments/" + config["environment"]).lower(), "Environment target mismatch")
    for kind in ("backend", "frontend"):
        resource = app(config, config[kind])
        require(resource["id"].lower() == (prefix + "Microsoft.App/containerApps/" + config[kind]).lower(), "App target mismatch")
        require(resource["properties"]["environmentId"].lower() == environment["id"].lower(), "App belongs to a different environment")
        require(resource["properties"]["configuration"]["activeRevisionsMode"] == "Single", "Review multiple-revision traffic routing explicitly before release")
        ingress = resource["properties"]["configuration"].get("ingress")
        require((ingress is None and allow_isolated) or (ingress is not None and "https://" + ingress["fqdn"] == config[kind + "_origin"]), "App origin mismatch")
    storage = azure("storage", "account", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config["storage"])
    require(storage["publicNetworkAccess"] == "Disabled", "Private storage network boundary must be retained")
    registry = azure("acr", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config["registry"])
    require(registry["loginServer"] == config["registry"] + ".azurecr.io", "Registry mismatch")


def identity_contract(config):
    for field in ("api_client_id", "frontend_client_id"):
        require(str(uuid.UUID(config[field])) == config[field], f"Verified {field} required")
    api = azure("ad", "app", "show", "--id", config["api_client_id"])
    frontend = azure("ad", "app", "show", "--id", config["frontend_client_id"])
    require(api["signInAudience"] == frontend["signInAudience"] == "AzureADMyOrg", "Single-tenant registrations required")
    require(api["api"]["requestedAccessTokenVersion"] == 2, "API must issue v2 access tokens")
    require("api://" + config["api_client_id"] in api["identifierUris"], "API identifier URI mismatch")
    scopes = [scope for scope in api["api"]["oauth2PermissionScopes"] if scope["value"] == "Batch.Access" and scope["isEnabled"]]
    require(len(scopes) == 1, "Enabled Batch.Access delegated scope required")
    require(config["frontend_origin"] + "/api/auth/callback/microsoft-entra-id" in frontend["web"]["redirectUris"], "Frontend callback mismatch")
    permissions = frontend["requiredResourceAccess"]
    require(permissions == [{"resourceAppId": config["api_client_id"], "resourceAccess": [{"id": scopes[0]["id"], "type": "Scope"}]}], "Use a dedicated frontend registration with only the custom API permission; no Graph/SharePoint permission is needed")
    api_principal = azure("ad", "sp", "show", "--id", config["api_client_id"])
    frontend_principal = azure("ad", "sp", "show", "--id", config["frontend_client_id"])
    grants = azure("rest", "--method", "GET", "--url", f"https://graph.microsoft.com/v1.0/servicePrincipals/{frontend_principal['id']}/oauth2PermissionGrants")
    require(any(grant["resourceId"] == api_principal["id"] and grant["consentType"] == "AllPrincipals" and "Batch.Access" in grant["scope"].split() for grant in grants["value"]), "Verified custom API admin consent required; this tool never grants consent")


def safe_containers(resource):
    containers = copy.deepcopy(resource["properties"]["template"]["containers"])
    require(len(containers) == 1, "Review multi-container apps explicitly")
    for entry in containers[0].get("env", []):
        require(not (re.search(r"SECRET|PASSWORD|TOKEN|(?:^|_)KEY(?:$|_)", entry["name"]) and entry.get("value")), "Literal credential-like app setting found; replace it with a secret reference through an approved secure process")
    return containers


def image(config, kind):
    value = config[kind + "_image"]
    require(re.fullmatch(re.escape(config["registry"]) + r"\.azurecr\.io/docintel/" + kind + r"@sha256:[0-9a-f]{64}", value), "Reviewed immutable image digest required")
    return value


def desired_containers(config, kind, baseline):
    containers = copy.deepcopy(baseline)
    values = {entry["name"]: entry for entry in containers[0].get("env", [])}
    if kind == "backend":
        updates = {"DOCINTEL_BATCH_MODE": "hosted", "DOCINTEL_BATCH_STORAGE_URL": f"https://{config['storage']}.blob.core.windows.net", "DOCINTEL_BATCH_CONTAINER": config["container"], "DOCINTEL_AUTH_TENANT_ID": config["tenant"], "DOCINTEL_AUTH_AUDIENCE": config["api_client_id"], "DOCINTEL_AUTH_CLIENT_ID": config["frontend_client_id"], "DOCINTEL_BATCH_LIVE_ENABLED": "false"}
        values.pop("DOCINTEL_BATCH_HOME", None)
    else:
        updates = {"AUTH_URL": config["frontend_origin"], "DOCINTEL_PORTAL_ORIGIN": config["frontend_origin"], "DOCINTEL_BATCH_API_URL": config["backend_origin"] + "/api/v1/batches", "DOCINTEL_BATCH_DEV": "false", "DOCINTEL_API_SCOPE": f"api://{config['api_client_id']}/Batch.Access", "AUTH_MICROSOFT_ENTRA_ID_ID": config["frontend_client_id"], "AUTH_MICROSOFT_ENTRA_ID_TENANT_ID": f"https://login.microsoftonline.com/{config['tenant']}/v2.0"}
        for name, reference in (("AUTH_SECRET", "auth_secret_ref"), ("AUTH_MICROSOFT_ENTRA_ID_SECRET", "entra_secret_ref")):
            values[name] = {"name": name, "secretRef": config[reference]}
    values.update({name: {"name": name, "value": value} for name, value in updates.items()})
    containers[0]["env"] = list(values.values())
    containers[0]["image"] = image(config, kind)
    return containers


def writable_containers(containers):
    supported = {"name", "image", "command", "args", "env", "resources", "probes", "volumeMounts"}
    result = []
    for container in containers:
        require(not (set(container) - supported - {"imageType"}), "Unknown container fields; review compatibility with API 2024-03-01")
        require(container.get("imageType") in (None, "ContainerImage"), "Unsupported image type for API 2024-03-01")
        projected = {name: copy.deepcopy(value) for name, value in container.items() if name in supported}
        if projected.get("resources") is not None:
            resources = projected["resources"]
            require(not (set(resources) - {"cpu", "memory", "ephemeralStorage"}), "Unknown container resource fields; review write compatibility")
            resources.pop("ephemeralStorage", None)
        result.append(projected)
    return result


def patch_app(config, kind, containers, work):
    body = work / (kind + "-patch.json")
    save(body, {"properties": {"template": {"containers": writable_containers(containers)}}})
    resource = f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}/providers/Microsoft.App/containerApps/{config[kind]}"
    azure("rest", "--method", "PATCH", "--url", "https://management.azure.com" + resource + "?api-version=2024-03-01", "--body", "@" + str(body))


def active_executions(config):
    job = azure("containerapp", "job", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"])
    require(job["properties"]["configuration"]["triggerType"] == "Manual", "Refusing a scheduled or event-driven job")
    executions = azure("containerapp", "job", "execution", "list", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"])
    return job, [entry for entry in executions if entry["properties"]["status"] not in TERMINAL]


def stop(config):
    _, running = active_executions(config)
    for execution in running:
        azure("containerapp", "job", "stop", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"], "--job-execution-name", execution["name"])
    require(not active_executions(config)[1], "Worker has not stopped yet; inspect execution state before continuing")


def stage(revision, work):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Full reviewed source commit required")
    source = work / "source"
    require(not source.exists(), "Use a fresh private build directory")
    archive = command(["git", "archive", revision], cwd=ROOT)
    source.mkdir(parents=True, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        contents.extractall(source, filter="data")
    require(not list(source.rglob(".env.local")), "Source commit still tracks a local environment file")
    files = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}
    save(work / "source.json", {"revision": revision, "files": files})
    print("Committed source exported privately; no dependency downloads or cloud operations.")


def verify_source(work, allow_lock_change=False):
    record = json.loads((work / "source.json").read_text())
    for relative, expected in record["files"].items():
        if allow_lock_change and relative == "uv.lock":
            continue
        require(hashlib.sha256((work / "source" / relative).read_bytes()).hexdigest() == expected, "Exported committed source changed; review and re-export instead of publishing drift")
    return record


def build_context(work):
    import shutil
    source = work / "source"
    record = verify_source(work)
    target = work / "context"
    require(not target.exists(), "Build context already exists; use a fresh private directory")
    ignore = shutil.ignore_patterns(".env", ".env.*", ".git", ".azure", "activation", "release-prep*", ".venv", ".clean-venv", "venv", "env", "node_modules", ".next", "__pycache__", "*.pyc", "*.pdf", "*.xlsx", "*.xlsm", "*.sqlite*", "*.db", "*.pem", "*.key", "*.pfx", "*.p12", "*.log", ".DS_Store")
    target.mkdir(mode=0o700)
    for name in ("backend", "frontend"):
        shutil.copytree(source / name, target / name, ignore=ignore)
    if (target / "backend/static").exists():
        shutil.rmtree(target / "backend/static")
    if (target / "frontend/tests").exists():
        shutil.rmtree(target / "frontend/tests")
    for name in ("pyproject.toml", "uv.lock", ".dockerignore"):
        shutil.copy2(source / name, target / name)
    for path in target.rglob("*"):
        if path.is_file() and path.relative_to(target).as_posix() not in record["files"]:
            path.unlink()
    inventory = {str(path.relative_to(target)): hashlib.sha256(path.read_bytes()).hexdigest() for path in target.rglob("*") if path.is_file()}
    save(work / "context.json", inventory)
    return target


def check_lock(before, after):
    original = {entry["name"]: entry["version"] for entry in before["package"]}
    resolved = {entry["name"]: entry["version"] for entry in after["package"]}
    require(all(resolved.get(name) == version for name, version in original.items() if name not in {"azure-ai-documentintelligence", "pyjwt"}), "Resolver changed unrelated package versions; stop for review")
    require(all(entry.get("source", {}).get("registry", "https://pypi.org/simple") == "https://pypi.org/simple" for entry in after["package"]), "Registry differs from committed public PyPI choice")
    require(resolved.get("azure-ai-documentintelligence") == "1.0.2", "Committed Document Intelligence constraint was not satisfied")


def package(work, approved):
    require(approved, "An existing approved runner with verified package/artifact access is required; do not retry the known failing host")
    require(not any(os.environ.get(name) for name in ("UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")), "Review inherited registry overrides before using the committed registry; no automatic substitution")
    source = work / "source"
    verify_source(work)
    baseline = work / "baseline.lock"
    require(not baseline.exists(), "Prior packaging attempt exists; inspect it rather than retrying a failed download loop")
    require(command(["uv", "--version"]).decode().startswith("uv 0.11.31"), "Use the pinned uv 0.11.31 toolchain")
    require(int(command(["node", "--version"]).decode().strip().lstrip("v").split(".")[0]) == 22, "Clean frontend checks require the Dockerfile's Node 22 toolchain")
    baseline.write_bytes((source / "uv.lock").read_bytes())
    environment = {**os.environ, "UV_HTTP_RETRIES": "0", "UV_PYTHON_DOWNLOADS": "never", "UV_PROJECT_ENVIRONMENT": str(work / ".clean-venv"), "PYTHONDONTWRITEBYTECODE": "1", "NEXT_TELEMETRY_DISABLED": "1"}
    command(["uv", "lock", "--directory", str(source)], env=environment)
    check_lock(tomllib.loads(baseline.read_text()), tomllib.loads((source / "uv.lock").read_text()))
    command(["uv", "sync", "--locked", "--python", "3.13", "--directory", str(source)], env=environment)
    command([str(work / ".clean-venv/bin/python"), "-m", "pytest", "tests/test_batch.py", "tests/test_batch_api.py", "tests/test_workbooks.py", "tests/test_release.py", "tests/test_pilot.py", "tests/test_pilot_api.py", "tests/test_pilot_server.py", "-q"], cwd=source, env=environment)
    frontend = source / "frontend"
    command(["npm", "ci", "--no-audit", "--no-fund"], cwd=frontend, env=environment)
    for arguments in (["node_modules/.bin/eslint", "."], ["node_modules/.bin/tsc", "--noEmit", "--incremental", "false"], ["node", "--test", *[str(path) for path in (frontend / "tests").glob("*.test.mjs")]], ["npm", "run", "build"]):
        command(arguments, cwd=frontend, env=environment)
    verify_source(work, allow_lock_change=True)
    revision = json.loads((work / "source.json").read_text())["revision"]
    save(work / "clean-checks.json", {"revision": revision, "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest(), "frontend": "clean_npm_ci_checks_build", "backend": "clean_locked_install_offline_tests"})
    print("Isolated clean checks passed. Review/commit the generated lock before release; no image was built or published.")


def require_release_images(config, work):
    verify_source(work)
    receipt = json.loads((work / "images.json").read_text())
    source = json.loads((work / "source.json").read_text())
    require(receipt["revision"] == source["revision"], "Published source mismatch")
    for kind in ("backend", "frontend"):
        require(receipt[kind] == image(config, kind), "Configured digest differs from the release publication receipt")


def remote_seed_code(config, payload):
    encoded = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    url = f"https://{config['storage']}.blob.core.windows.net"
    marker = "DOCINTEL_SYNTHETIC_SEED_OK:" + fingerprint(payload)
    return f'''import base64, hashlib, json, os
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
assert os.environ.get("DOCINTEL_BATCH_MODE") == "hosted"
assert os.environ.get("DOCINTEL_BATCH_LIVE_ENABLED") == "false"
assert os.environ.get("DOCINTEL_BATCH_STORAGE_URL") == {url!r}
assert os.environ.get("DOCINTEL_BATCH_CONTAINER") == {config['container']!r}
payload = json.loads(base64.b64decode({encoded!r}))
content = {{name: base64.b64decode(entry["content"], validate=True) for name, entry in payload.items()}}
assert len(content) == 3
assert all(hashlib.sha256(value).hexdigest() == payload[name]["sha256"] for name, value in content.items())
client = BlobServiceClient({url!r}, credential=ManagedIdentityCredential(), retry_total=0, connection_timeout=10, read_timeout=30)
container = client.get_container_client({config['container']!r})
assert next(iter(container.list_blobs()), None) is None, "Refusing a nonempty seed container"
for name, value in content.items():
    container.upload_blob(name, value, overwrite=False)
assert {{blob.name for blob in container.list_blobs()}} == set(content)
for name, value in content.items():
    assert container.download_blob(name).readall() == value
print({marker!r})
'''


def seed_via_backend(config, fixture, files, work):
    backend = app(config, config["backend"])
    properties = backend["properties"]
    require(properties["provisioningState"] == "Succeeded" and properties["latestRevisionName"] == properties["latestReadyRevisionName"], "Backend must be ready before private seeding")
    require([container["image"] for container in safe_containers(backend)] == [image(config, "backend")], "Seed backend differs from published release")
    payload = {name.removeprefix("seed/"): {"sha256": digest, "content": base64.b64encode((fixture / name).read_bytes()).decode()} for name, digest in files.items() if name.startswith("seed/")}
    require(sum(len(entry["content"]) for entry in payload.values()) <= 16384, "Synthetic seed exceeds remote execution bound")
    encoded = base64.b64encode(remote_seed_code(config, payload).encode()).decode()
    output = command(["az", "containerapp", "exec", "--subscription", config["subscription"], "-g", config["group"], "-n", config["backend"], "--revision", properties["latestReadyRevisionName"], "--command", f"/app/.venv/bin/python -c exec(__import__('base64').b64decode('{encoded}'))"])
    marker = "DOCINTEL_SYNTHETIC_SEED_OK:" + fingerprint(payload)
    require(marker in output.decode().splitlines(), "Private seed did not confirm all hashes; inspect partial state, never blindly retry")
    save(work / "seed.json", {"backend_image": image(config, "backend"), "revision": properties["latestReadyRevisionName"], "files": {name: entry["sha256"] for name, entry in payload.items()}})


def run(options):
    work = private_path(options.work)
    if options.action == "stage":
        stage(options.revision, work)
        return
    if options.action == "context":
        build_context(work)
        return
    if options.action == "package":
        package(work, options.package_access_approved)
        return
    config = load_config(options.config)
    if options.action == "plan":
        print("No Azure operation performed. Gates: reviewed private source; consistent committed lock; clean checks; approved digests; Entra scope/consent; secret references; rollback policy.")
        return
    if options.action in OPERATIONS:
        require(options.approve == options.action, "Explicit approval for this operation is required")
    verify_target(config, allow_isolated=options.action == "rollback" and options.isolate_legacy_baseline)
    if options.action in {"provision", "seed", "deploy", "start"}:
        require_release_images(config, work)
    if options.action == "identity-check":
        identity_contract(config)
    elif options.action == "capture":
        require(not (work / "baseline.json").exists(), "Refusing to replace rollback baseline")
        baseline = {kind: {"containers": safe_containers(app(config, config[kind]))} for kind in ("backend", "frontend")}
        for record in baseline.values():
            for container in record["containers"]:
                current = container["image"]
                require(current.startswith(config["registry"] + ".azurecr.io/"), "Baseline image is outside approved registry")
                if "@sha256:" not in current:
                    repository = current.split("/", 1)[1]
                    digest = azure("acr", "repository", "show", "--name", config["registry"], "--image", repository)["digest"]
                    record["pinned_image"] = current.rsplit(":", 1)[0] + "@" + digest
                else:
                    record["pinned_image"] = current
        save(work / "baseline.json", {"target": fingerprint(config), "authenticated": config.get("baseline_authenticated") is True, "apps": baseline})
    elif options.action == "publish":
        verify_source(work)
        receipt = json.loads((work / "clean-checks.json").read_text())
        source = json.loads((work / "source.json").read_text())
        committed_lock = command(["git", "show", source["revision"] + ":uv.lock"], cwd=ROOT)
        require(receipt["lock_sha256"] == hashlib.sha256(committed_lock).hexdigest(), "Resolved lock is not the reviewed committed lock; commit it and re-export first")
        require(receipt["revision"] == source["revision"], "Clean-check source mismatch")
        inventory = json.loads((work / "context.json").read_text())
        actual = {str(path.relative_to(work / "context")): hashlib.sha256(path.read_bytes()).hexdigest() for path in (work / "context").rglob("*") if path.is_file()}
        require(inventory == actual, "Build context drift")
        require(all(source["files"].get(relative) == digest for relative, digest in actual.items()), "Context differs from reviewed committed source")
        published = {"revision": source["revision"]}
        for kind in ("backend", "frontend"):
            context = work / "context" if kind == "backend" else work / "context/frontend"
            dockerfile = context / ("backend/Dockerfile" if kind == "backend" else "Dockerfile")
            tag = f"docintel/{kind}:{source['revision']}"
            result = azure("acr", "build", "--subscription", config["subscription"], "--registry", config["registry"], "--platform", "linux/amd64", "--timeout", "900", "--no-logs", "--file", str(dockerfile), "--image", tag, str(context))
            require(result.get("status") == "Succeeded", "ACR build did not succeed; inspect the run before another attempt")
            digest = azure("acr", "repository", "show", "--name", config["registry"], "--image", tag)["digest"]
            published[kind] = config["registry"] + f".azurecr.io/docintel/{kind}@" + digest
            published[kind + "_run_id"] = result["runId"]
            save(work / "publication-progress.json", published)
        save(work / "images.json", published)
    elif options.action in {"what-if", "provision"}:
        verify_source(work)
        image(config, "backend")
        command_name = "what-if" if options.action == "what-if" else "create"
        if options.action == "provision":
            preview = json.loads((work / "what-if.json").read_text())
            require(preview["target"] == fingerprint(config), "Review a what-if for this exact target before provisioning")
        result = azure("deployment", "group", command_name, "--subscription", config["subscription"], "-g", config["group"], "--template-file", str(work / "source/infra/batch.bicep"), "--parameters", f"storageAccountName={config['storage']}", f"registryName={config['registry']}", f"environmentName={config['environment']}", f"jobName={config['job']}", f"containerName={config['container']}", f"backendImage={config['backend_image']}")
        save(work / ("what-if.json" if options.action == "what-if" else "provision.json"), {"target": fingerprint(config), "result": result})
    elif options.action == "seed":
        fixture = private_path(options.fixture)
        hashes = json.loads((fixture / "sha256.json").read_text())
        files = {path.relative_to(fixture).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in fixture.rglob("*") if path.is_file() and path.name != "sha256.json"}
        require(files == hashes, "Synthetic fixture hash mismatch or unexpected files")
        seed_files = {name.removeprefix("seed/") for name in files if name.startswith("seed/")}
        require(len(seed_files) == 3 and {"documents/release-synthetic.pdf", "configuration/sources.json"} <= seed_files and sum(bool(re.fullmatch(r"parses/[a-f0-9]{64}\.json", name)) for name in seed_files) == 1, "Unexpected synthetic seed layout")
        if options.seed_via_backend:
            seed_via_backend(config, fixture, files, work)
            return
        existing = azure("storage", "blob", "list", "--account-name", config["storage"], "--container-name", config["container"], "--auth-mode", "login")
        require(not existing, "Refusing to overwrite a nonempty container; inspect partial seeding or existing data")
        azure("storage", "blob", "upload-batch", "--account-name", config["storage"], "--destination", config["container"], "--source", str(fixture / "seed"), "--auth-mode", "login", "--overwrite", "false")
    elif options.action == "deploy":
        identity_contract(config)
        baseline = json.loads((work / "baseline.json").read_text())
        require(baseline["target"] == fingerprint(config), "Target changed since baseline capture")
        frontend = app(config, config["frontend"])
        refs = {entry["name"] for entry in frontend["properties"]["configuration"].get("secrets", [])}
        require({config["auth_secret_ref"], config["entra_secret_ref"]} <= refs, "Approved frontend secret references are missing")
        for kind in ("backend", "frontend"):
            current = safe_containers(app(config, config[kind]))
            desired = desired_containers(config, kind, baseline["apps"][kind]["containers"])
            require(current in (baseline["apps"][kind]["containers"], desired), "App drift since baseline capture; stop rather than overwrite")
        for kind in ("backend", "frontend"):
            desired = desired_containers(config, kind, baseline["apps"][kind]["containers"])
            save(work / (kind + "-intended.json"), desired)
            if safe_containers(app(config, config[kind])) != desired:
                patch_app(config, kind, desired, work)
            resource = app(config, config[kind])
            require(resource["properties"]["provisioningState"] == "Succeeded" and resource["properties"]["latestRevisionName"] == resource["properties"]["latestReadyRevisionName"], f"{kind} revision is not ready; stop and inspect before proceeding")
            revision = azure("containerapp", "revision", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config[kind], "--revision", resource["properties"]["latestReadyRevisionName"])
            require(revision["properties"]["healthState"] == "Healthy", f"{kind} revision is not healthy; deployment stopped")
            require([container["image"] for container in revision["properties"]["template"]["containers"]] == [container["image"] for container in desired], f"{kind} ready revision does not contain the intended image")
    elif options.action == "start":
        require(re.fullmatch(r"[a-f0-9]{64}", options.batch_id or ""), "Explicit validated synthetic batch ID required")
        require(options.item_limit in (1, 2), "Acceptance slice must contain at most two items")
        job, running = active_executions(config)
        require(not running, "A worker execution is already active")
        settings = job["properties"]["configuration"]
        require(settings["replicaRetryLimit"] == 0 and settings["replicaTimeout"] == 600, "Unexpected worker retry/timeout configuration")
        template = job["properties"]["template"]
        require(len(template["containers"]) == 1, "Unexpected worker containers")
        container = template["containers"][0]
        require(container["image"] == image(config, "backend"), "Worker image mismatch")
        require(any(entry["name"] == "DOCINTEL_BATCH_LIVE_ENABLED" and entry.get("value") == "false" for entry in container["env"]), "Worker live AI must be disabled")
        container["args"] = ["-m", "backend.batch_worker", "--concurrency", "2", "--max-batches", "1", "--item-limit", str(options.item_limit), "--synthetic-acceptance", "--batch-id", options.batch_id]
        save(work / "execution.json", template)
        result = azure("containerapp", "job", "start", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"], "--yaml", str(work / "execution.json"))
        save(work / "last-execution.json", result)
    elif options.action == "stop":
        stop(config)
    elif options.action == "rollback":
        baseline = json.loads((work / "baseline.json").read_text())
        require(baseline["target"] == fingerprint(config), "Rollback target mismatch")
        require(baseline["authenticated"] or options.isolate_legacy_baseline, "Baseline has no verified hosted authentication; approve ingress isolation or a secure rollback baseline, never restore it publicly")
        for kind in ("backend", "frontend"):
            current = safe_containers(app(config, config[kind]))
            intended_path = work / (kind + "-intended.json")
            intended = json.loads(intended_path.read_text()) if intended_path.exists() else None
            restored = copy.deepcopy(baseline["apps"][kind]["containers"])
            restored[0]["image"] = baseline["apps"][kind]["pinned_image"]
            require(current in (baseline["apps"][kind]["containers"], intended, restored), "Rollback would overwrite unrelated app changes")
        stop(config)
        if not baseline["authenticated"]:
            for kind in ("frontend", "backend"):
                azure("containerapp", "ingress", "disable", "--subscription", config["subscription"], "-g", config["group"], "-n", config[kind])
        for kind in ("backend", "frontend"):
            containers = copy.deepcopy(baseline["apps"][kind]["containers"])
            containers[0]["image"] = baseline["apps"][kind]["pinned_image"]
            patch_app(config, kind, containers, work)
        print("Restored baseline images and exact environment/reference settings. Job, identities, grants, container, results and reviews retained. Ingress remains disabled for an unsafe legacy baseline.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "stage", "package", "context", "identity-check", "capture", "publish", "what-if", "provision", "seed", "deploy", "start", "stop", "rollback"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--revision")
    parser.add_argument("--package-access-approved", action="store_true")
    parser.add_argument("--approve", choices=sorted(OPERATIONS))
    parser.add_argument("--batch-id")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--seed-via-backend", action="store_true", help="Seed only the verified synthetic fixture through the ready published backend managed identity; no public storage access or operator data grant")
    parser.add_argument("--item-limit", type=int, default=2)
    parser.add_argument("--isolate-legacy-baseline", action="store_true", help="Explicitly approve ingress isolation before restoring an unauthenticated legacy image; this causes an outage")
    options = parser.parse_args()
    try:
        run(options)
    except (ValueError, KeyError, OSError, TypeError) as error:
        print(f"Release stopped: {error}" if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "Release stopped: missing or invalid private configuration/state")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())