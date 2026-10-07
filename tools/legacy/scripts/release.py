"""Guarded release operations; private target configuration stays outside Git."""

import argparse
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import tomllib
from urllib.parse import parse_qs, unquote, urlsplit
import uuid
import zlib


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"Succeeded", "Failed", "Stopped"}
OPERATIONS = {"provision", "publish", "publish-frontend", "publish-backend", "update-worker", "seed", "deploy", "start", "stop", "rollback", "pilot-enable", "pilot-start", "pilot-disable", "pilot-upload-enable", "pilot-upload-disable", "pilot-configure"}
PILOT_API_SETTINGS = {"DOCINTEL_REAL_PILOT_ENABLED", "DOCINTEL_REAL_PILOT_OPERATOR_IDS", "DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID"}
PILOT_UPLOAD_SETTINGS = {"DOCINTEL_PILOT_UPLOAD_ENABLED", "DOCINTEL_REAL_PILOT_OPERATOR_IDS"}
PILOT_CONFIGURATION_KEYS = {"upload": "configuration/pilot-source-upload.json", "real": "configuration/real-pilot-approval.json"}
PILOT_ENVIRONMENT_KEYS = {"AZURE_CLIENT_ID", "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "LLM_ENDPOINT", "LLM_DEPLOYMENT", "AOAI_API_VERSION", "WEBSEARCH_PROVIDER", "WEBIQ_ENDPOINT", "AI_FOUNDRY_PROJECT_ENDPOINT", "BING_CONNECTION_ID", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME"}
PILOT_EXTERNAL_ENVIRONMENT_KEYS = {"WEBSEARCH_PROVIDER", "WEBIQ_ENDPOINT", "AI_FOUNDRY_PROJECT_ENDPOINT", "BING_CONNECTION_ID", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME"}
PILOT_LIMITS = {"products": 4, "executions": 2, "analysis": 2, "inference": 16, "search": 8, "web_retrieval": 12, "retrieval": 4, "analysis_pages": 10, "input_tokens": 200000, "output_tokens": 32768}
PUBLICATION_API_VERSION = "2019-04-01"
SUPPLEMENTAL_PUBLICATION_RECORDS = (
    "metadata-preflight-attempt.json", "metadata-preflight-result.json",
    "backend-publication-supplemental-attempt.json", "backend-publication-supplemental-result.json",
    "backend-publication-supplemental-request.json", "backend-publication-supplemental-submission.json",
    "backend-publication-supplemental-queued.json", "backend-publication-supplemental-upload-metadata.json",
)
BACKEND_COMMAND = ["/app/.venv/bin/fastapi"]
BACKEND_ARGUMENTS = ["run", "backend/main.py", "--port", "8080", "--host", "0.0.0.0"]
UNCHANGED_INGRESS = object()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def command(arguments, *, cwd=None, env=None, timeout=None):
    try:
        completed = subprocess.run(arguments, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError("Command timed out; outcome unknown, do not repeat a submitted operation") from None
    require(completed.returncode == 0, f"Command failed: {arguments[0]} {arguments[1]}; output withheld to protect private configuration")
    return completed.stdout


def _emit_preflight(callback, receipt, stage):
    if callback is None:
        return
    require(callable(callback), "Native preflight receipt callback must be callable")
    try:
        callback({**copy.deepcopy(receipt), "operation_stage": stage})
    except Exception:
        raise ValueError("Native preflight receipt persistence failed; no request sent") from None


def azure(*arguments, timeout=None, resource_snapshot=None, before_send=None, on_preflight=None):
    arguments = canonical_job_stop(arguments)
    write_schema().validate_azure_arguments(arguments)
    if azure_write(arguments):
        proof = preflight_azure(arguments, resource_snapshot=resource_snapshot)
        _emit_preflight(on_preflight, proof, "azure_write")
    if before_send is not None:
        before_send()
    output = command(["az", *arguments, "--only-show-errors", "-o", "json"], timeout=timeout)
    return json.loads(output) if output.strip() else None


def azure_write(arguments):
    return (tuple(arguments[:3]) == ("containerapp", "job", "start")
            or bool(write_schema().validate_azure_arguments(arguments)))


def cli_options(arguments, start):
    require(len(arguments[start:]) % 2 == 0, "Exact native CLI option/value pairs required")
    options = {}
    for index in range(start, len(arguments), 2):
        key = arguments[index]
        require(key not in options, "Duplicate native CLI option")
        options[key] = arguments[index + 1]
    return options


def canonical_job_stop(arguments):
    if tuple(arguments[:3]) != ("containerapp", "job", "stop"):
        return tuple(arguments)
    options = cli_options(arguments, 3)
    require(set(options) == {"--subscription", "-g", "-n", "--job-execution-name"},
            "Only the exact reserved execution may be stopped")
    url = (f"https://management.azure.com/subscriptions/{options['--subscription']}"
           f"/resourceGroups/{options['-g']}/providers/Microsoft.App/jobs/{options['-n']}"
           f"/executions/{options['--job-execution-name']}/stop?api-version=2025-01-01")
    write_schema().validate_request("POST", url, None)
    return ("rest", "--method", "POST", "--url", url)


def native_cli_python():
    executable = shutil.which("az")
    require(executable, "Installed Azure CLI is required for no-send construction")
    launcher = Path(executable).resolve()
    text = launcher.read_text()
    match = re.search(r"(/[^ \n\"']+/bin/python[\d.]*)[\"']? -Im azure\.cli", text)
    if match:
        interpreter = Path(match[1])
    else:
        interpreter = Path(text.splitlines()[0].removeprefix("#!").strip())
    require(interpreter.is_absolute() and interpreter.is_file()
            and re.fullmatch(r"python[\d.]*", interpreter.name), "Unsupported installed Azure CLI launcher")
    return interpreter


def preflight_azure(arguments, *, resource_snapshot=None):
    """Construct the exact installed CLI request, without owner state or transport."""
    arguments = canonical_job_stop(tuple(arguments))
    descriptor = write_schema().validate_azure_arguments(arguments)
    require(azure_write(arguments), "A modeled deployment/publication write is required")
    if arguments[0] == "rest":
        expected = descriptor
        payload = expected["body"]
        if "--headers" in arguments:
            key, value = arguments[arguments.index("--headers") + 1].split("=", 1)
            expected["headers"] = {key: value}
        subscription = expected["url"].split("/subscriptions/")[1].split("/")[0]
    else:
        options = cli_options(arguments, 3)
        require(set(options) == {"--subscription", "-g", "-n", "--yaml"},
                "Exact start command with a JSON execution template required")
        payload = json.loads(Path(options["--yaml"]).read_text())
        resource_id = (f"/subscriptions/{options['--subscription']}/resourceGroups/{options['-g']}"
                       f"/providers/Microsoft.App/jobs/{options['-n']}")
        require(resource_snapshot and resource_snapshot.get("id", "").lower() == resource_id.lower(),
                "Native no-send start requires the already captured current job")
        expected = {"method": "POST", "url": "https://management.azure.com" + resource_id
                    + "/start?api-version=2025-01-01", "body": payload}
        subscription = options["--subscription"]
    write_schema().validate_request(expected["method"], expected["url"], payload)
    root = ROOT / (".native-write-preflight-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    try:
        native_arguments = list(arguments)
        if arguments[0] != "rest":
            template = root / "template.json"
            template.write_text(json.dumps(payload))
            template.chmod(0o600)
            native_arguments[native_arguments.index("--yaml") + 1] = str(template)
        packet = {"state_directory": str(root), "arguments": [*native_arguments, "--only-show-errors", "-o", "json"],
                  "expected": expected, "subscription": subscription, "resource_snapshot": resource_snapshot}
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        result = subprocess.run(
            [str(native_cli_python()), "-B", str(ROOT / "scripts" / "azure_write_schema.py"), "--native-cli-no-send"],
            input=json.dumps(packet).encode(), capture_output=True, timeout=60, env=environment, cwd=ROOT,
        )
        receipt = json.loads(result.stdout) if result.stdout.strip() else {}
        if result.returncode != 0 and isinstance(receipt.get("diagnostic"), dict):
            diagnostic = write_schema().native_cli_diagnostic(receipt["diagnostic"])
            raise ValueError("Native deployment request construction rejected; no write sent; diagnostic="
                             + json.dumps(diagnostic, sort_keys=True))
        require(result.returncode == 0 and receipt.get("status") == "validated" and receipt.get("no_send") is True,
                "Native deployment request construction rejected; no write sent")
        return receipt
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise ValueError("Native deployment request construction failed; no write sent") from None
    finally:
        shutil.rmtree(root)


def native_http(packet, *, send=False):
    """No generic URL transport: the child accepts only the two explicit contracts."""
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    mode = "--native-http-send" if send else "--native-http-no-send"
    try:
        result = subprocess.run(
            [str(native_cli_python()), "-B", str(ROOT / "scripts" / "azure_write_schema.py"), mode],
            input=json.dumps(packet).encode(), capture_output=True,
            timeout=packet["timeout"], env=environment, cwd=ROOT,
        )
        receipt = json.loads(result.stdout) if result.returncode == 0 else {}
        require(receipt.get("status") == "validated" and receipt.get("no_send") is (not send),
                "Native HTTP construction/transport failed; details withheld, no automatic retry")
        return receipt
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise ValueError("Native HTTP construction/transport failed; details withheld, no automatic retry") from None


def preflight_deployment(config, backend, worker, execution_template, *,
                         api_payload=None, worker_payload=None, publication=None):
    """Readiness check from supplied shapes only; no credentials, service reads or receipts."""
    results = []
    for resource, supplied, job in ((backend, api_payload, False), (worker, worker_payload, True)):
        payload = supplied if supplied is not None else build_update_payload(
            resource, safe_containers(resource), job=job,
        )
        url = "https://management.azure.com" + resource["id"] + "?api-version=2024-03-01"
        results.append(preflight_azure(("rest", "--method", "PATCH", "--url", url, "--body", json.dumps(payload))))
    root = ROOT / (".native-deployment-template-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    try:
        template = root / "template.json"
        template.write_text(json.dumps(execution_template))
        template.chmod(0o600)
        results.append(preflight_azure((
            "containerapp", "job", "start", "--subscription", config["subscription"],
            "-g", config["group"], "-n", config["job"], "--yaml", str(template),
        ), resource_snapshot=worker))
    finally:
        shutil.rmtree(root)
    registry = (f"https://management.azure.com/subscriptions/{config['subscription']}/resourceGroups/"
                f"{config['group']}/providers/Microsoft.ContainerRegistry/registries/{config['registry']}")
    results.append(preflight_azure(("rest", "--method", "POST", "--url",
                                   registry + "/listBuildSourceUploadUrl?api-version=" + PUBLICATION_API_VERSION)))
    if publication is not None:
        results.append(preflight_azure(("rest", "--method", "POST", "--url",
                                       registry + "/scheduleRun?api-version=" + PUBLICATION_API_VERSION,
                                       "--body", json.dumps(publication))))
    return {"status": "validated", "no_send": True, "requests": results,
            "pending_dynamic_boundary": ["actual_source_upload_metadata", "actual_binary_archive",
                                         "actual_schedule_request"] if publication is None else ["actual_binary_archive"]}


def write_schema():
    # Staging runs before dependency installation, including direct-script mode.
    try:
        from scripts import azure_write_schema
    except ModuleNotFoundError:
        import azure_write_schema
    return azure_write_schema


def build_update_payload(resource, containers, *, job=False, configuration=UNCHANGED_INGRESS):
    kind = "jobs" if job else "containerApps"
    projected = write_schema().project_component(
        kind, "PATCH", ("properties", "template", "containers"), writable_containers(containers),
    )
    properties = {"template": {"containers": projected}}
    if configuration is not UNCHANGED_INGRESS:
        properties["configuration"] = copy.deepcopy(configuration)
    members = {"properties": properties}
    if not job:
        require(isinstance(resource, dict) and isinstance(resource.get("location"), str) and resource["location"],
                "The pinned Container Apps PATCH contract requires the observed resource location")
        members["location"] = resource["location"]
    return write_schema().build_payload(kind, "PATCH", **members)


def writable_execution_template(template):
    value = copy.deepcopy(template)
    # Execution overrides cannot change the existing job's volume definitions.
    value.pop("volumes", None)
    for name in ("containers", "initContainers"):
        if value.get(name) is not None:
            value[name] = writable_containers(value[name])
    return write_schema().project_action_payload("jobStart", value)


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


def private_json(path, max_bytes=262144):
    require(path is not None, "An explicit private file is required")
    path = private_path(path)
    require(path.is_file() and path.stat().st_mode & 0o077 == 0, "Private receipt/approval must be an owner-only file")
    require(path.stat().st_size <= max_bytes, "Private receipt/approval exceeds the read bound")
    return json.loads(path.read_text())


def bounded_metadata(approval):
    raw = json.dumps(approval, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    require(len(raw) <= 65536, "Approval metadata exceeds the 64 KiB raw bound; source bytes never belong in console metadata")
    encoded = base64.b64encode(zlib.compress(raw, level=9)).decode()
    require(len(encoded) <= 8192, "Compressed approval metadata exceeds the 8 KiB console bound")
    return encoded


def require_real_pilot_off(resource):
    entries = environment_entries(safe_containers(resource)[0])
    flag = entries.get("DOCINTEL_REAL_PILOT_ENABLED")
    require(flag is None or flag == {"name": "DOCINTEL_REAL_PILOT_ENABLED", "value": "false"}, "Real-pilot processing must remain disabled during source/configuration bootstrap")


def run_pilot_console(config, backend, code, marker):
    require(len(base64.b64encode(code.encode())) + 80 <= 16384, "Complete approval console frame exceeds the 16 KiB transport bound")
    output = console_code([
        "az", "containerapp", "exec", "--subscription", config["subscription"],
        "-g", config["group"], "-n", config["backend"],
        "--revision", backend["properties"]["latestReadyRevisionName"],
        "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors",
    ], code, marker)
    require(marker in output.decode().splitlines(), "Pilot metadata operation did not confirm; inspect private state before proceeding")


def save_once(path, data):
    path = private_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("Pilot action already attempted; inspect its immutable receipt, never blindly retry") from None
    with os.fdopen(descriptor, "w") as output:
        json.dump(data, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())


@contextmanager
def pilot_lock(work):
    import fcntl

    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(work / "pilot.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another pilot release action is in progress") from None
        yield


def pilot_window(approval):
    start = datetime.fromisoformat(approval["not_before"])
    end = datetime.fromisoformat(approval["expires_at"])
    require(start.tzinfo is not None and end.tzinfo is not None, "Pilot timestamps require timezones")
    require(0 < (end - start).total_seconds() <= 1200 and start <= datetime.now(timezone.utc) < end, "Pilot approval is expired, not active, or exceeds twenty minutes")


def load_pilot_approval(path, batch_id):
    approval = private_json(path, max_bytes=65536)
    fields = {"schema_version", "approved", "id", "approved_by", "not_before", "expires_at", "batch_id", "owner", "batch_sha256", "customer_processing_approved", "identities", "environment", "limits", "unit_prices_usd"}
    require(isinstance(approval, dict) and fields <= set(approval) <= fields | {"execution_scope"}, "Pilot approval schema mismatch")
    scope = approval.get("execution_scope", "full")
    require(scope in ("full", "internal_only"), "Pilot execution_scope must be full or internal_only")
    require(type(approval["schema_version"]) is int and approval["schema_version"] == 1 and approval["approved"] is True, "An explicitly approved real-pilot packet is required")
    require(re.fullmatch(r"[a-f0-9]{64}", batch_id or "") and approval["batch_id"] == batch_id, "Exact approved batch ID required")
    require(isinstance(approval["owner"], str) and approval["owner"] and re.fullmatch(r"[a-f0-9]{64}", approval["batch_sha256"]), "Pilot owner and input binding required")
    require(set(approval["identities"]) == {"api_principal_id", "worker_principal_id"}, "Separate API/worker principal bindings required")
    for value in (approval["id"], approval["approved_by"], *approval["identities"].values()):
        require(isinstance(value, str) and str(uuid.UUID(value)) == value, "Canonical approval/operator/principal UUID required")
    pilot_window(approval)
    require(approval["customer_processing_approved"] is True, "Explicit customer processing approval is required in every scope")
    require(set(approval["limits"]) == set(PILOT_LIMITS) | {"spend_microdollars"}, "Pilot limit schema mismatch")
    for name, maximum in PILOT_LIMITS.items():
        value = approval["limits"][name]
        require(type(value) is int and 0 <= value <= maximum, "Pilot limit exceeds its finite ceiling")
    for name in ("products", "executions", "spend_microdollars"):
        require(type(approval["limits"][name]) is int and approval["limits"][name] > 0, "Positive explicit pilot allowance required")
    if scope == "internal_only":
        require(all(approval["limits"][name] == 0 for name in ("search", "web_retrieval", "retrieval")), "Internal-only approval requires zero external operation budgets")
    prices = approval["unit_prices_usd"]
    price_keys = {"analysis_page", "input_token", "output_token"}
    if scope == "full":
        price_keys.update(("search", "web_retrieval"))
    require(set(prices) == price_keys, "Complete per-unit upper-bound prices required")
    for value in prices.values():
        require(isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,18})?", value) and Decimal(value) > 0, "Positive explicit decimal-string unit prices required")
    environment = approval["environment"]
    require(set(environment) <= PILOT_ENVIRONMENT_KEYS and "AZURE_CLIENT_ID" in environment, "Unsupported or missing worker environment binding")
    if scope == "internal_only":
        require(not set(environment) & PILOT_EXTERNAL_ENVIRONMENT_KEYS, "Internal-only approval must omit external service environment settings")
    require(all(isinstance(value, str) for value in environment.values()), "Worker settings must be strings, never credential objects")
    if environment["AZURE_CLIENT_ID"]:
        require(str(uuid.UUID(environment["AZURE_CLIENT_ID"])) == environment["AZURE_CLIENT_ID"], "Canonical managed identity client ID required")
    return approval


def load_upload_approval(path, config):
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    approval = private_json(path, max_bytes=65536)
    try:
        from backend.pilot_upload import validate_upload_approval
        validate_upload_approval(approval)
    except (ImportError, AttributeError, ValueError, TypeError):
        raise ValueError("Source upload approval schema is invalid or unavailable") from None
    require(datetime.fromisoformat(approval["expires_at"]) > datetime.now(timezone.utc), "Source upload approval is expired")
    require(approval["owner"].split("/")[0] == config["tenant"], "Source upload owner belongs to another tenant")
    approved_operator(config, approval)
    bounded_metadata(approval)
    return approval


def approved_operator(config, approval):
    operators = config.get("pilot_operator_ids")
    require(isinstance(operators, list) and 1 <= len(operators) <= 4 and len(set(operators)) == len(operators), "Independently approved pilot_operator_ids are required in the private target")
    for operator in operators:
        require(isinstance(operator, str) and str(uuid.UUID(operator)) == operator, "Trusted pilot operator IDs must be canonical UUIDs")
    require(approval["approved_by"] in operators, "Approval operator is not in the trusted target allowlist")
    return approval["approved_by"]


def pilot_values(approval):
    return {
        "DOCINTEL_REAL_PILOT_ENABLED": "true",
        "DOCINTEL_REAL_PILOT_OPERATOR_IDS": approval["approved_by"],
        "DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID": approval["identities"]["worker_principal_id"],
    }


def upload_values(approval):
    return {"DOCINTEL_PILOT_UPLOAD_ENABLED": "true", "DOCINTEL_REAL_PILOT_OPERATOR_IDS": approval["approved_by"]}


def metadata_backend(config):
    require(isinstance(config.get("api_principal_id"), str) and str(uuid.UUID(config["api_principal_id"])) == config["api_principal_id"], "Verified private target api_principal_id is required for metadata/upload actions")
    backend = app(config, config["backend"])
    require_pilot_identity(backend, config["api_principal_id"])
    require_real_pilot_off(backend)
    return backend


def remote_metadata_code(approval, kind, tenant, *, write):
    require(kind in PILOT_CONFIGURATION_KEYS, "Only the two fixed pilot approval keys may be configured")
    encoded = bounded_metadata(approval)
    marker = "DOCINTEL_PILOT_METADATA_OK:" + kind + ":" + fingerprint(approval)
    code = f'''import base64, hashlib, json, os, zlib
from datetime import datetime, timezone
from backend.batch_store import Conflict, Missing, configured_store, read_json, write_json
decoder = zlib.decompressobj()
raw = decoder.decompress(base64.b64decode({encoded!r}, validate=True), 65537)
assert len(raw) <= 65536 and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
candidate = json.loads(raw)
assert hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest() == {fingerprint(approval)!r}
store = configured_store()
key = {PILOT_CONFIGURATION_KEYS[kind]!r}
os.environ["DOCINTEL_REAL_PILOT_OPERATOR_IDS"] = candidate["approved_by"]
'''
    if kind == "upload":
        code += f'''from backend.pilot_upload import PilotSourceUpload, validate_upload_approval
validate_upload_approval(candidate)
assert candidate["owner"].split("/")[0] == {tenant!r}
os.environ["DOCINTEL_PILOT_UPLOAD_ENABLED"] = "true"
os.environ["DOCINTEL_REAL_PILOT_ENABLED"] = "false"
class ApprovedView:
    def read_bytes(self, name, **kwargs):
        return (raw, "candidate") if name == key else store.read_bytes(name, **kwargs)
PilotSourceUpload(ApprovedView())._prepare(candidate["owner"])
'''
    else:
        code += '''from backend.real_pilot import BUDGET_KEY, RealPilotGuard, _sha256
batch, _ = read_json(store, "batches/" + candidate["batch_id"] + ".json")
checker = RealPilotGuard.__new__(RealPilotGuard)
checker.store, checker.batch = store, batch
checker._validate_approval(candidate, verify_runtime=False)
try:
    ledger, _ = read_json(store, BUDGET_KEY)
except Missing:
    pass
else:
    assert ledger["approval_sha256"] == _sha256(candidate) and not ledger.get("invalidated")
'''
    if write:
        code += '''try:
    existing, _ = read_json(store, key)
except Missing:
    try:
        write_json(store, key, candidate)
    except Conflict:
        existing, _ = read_json(store, key)
        assert existing == candidate
else:
    assert existing == candidate
'''
    code += f'''verified, _ = read_json(store, key)
assert verified == candidate
print({marker!r})
'''
    require(len(base64.b64encode(code.encode())) + 80 <= 16384, "Complete approval console frame exceeds the 16 KiB transport bound")
    return code, marker


def pilot_configure(config, approval, work, kind):
    approved_operator(config, approval)
    with pilot_lock(work):
        require_pilot_acceptance(config, work)
        backend = metadata_backend(config)
        if kind == "real":
            require(approval["identities"]["api_principal_id"] == config["api_principal_id"], "Final approval API principal differs from verified target")
        code, marker = remote_metadata_code(approval, kind, config["tenant"], write=True)
        receipt = {"target": fingerprint(config), "key": PILOT_CONFIGURATION_KEYS[kind], "approval_sha256": fingerprint(approval)}
        attempt = work / f"pilot-configure-{kind}-attempt.json"
        if attempt.exists():
            require(private_json(attempt) == receipt, "Different metadata cannot replace the pinned configuration attempt")
        else:
            save_once(attempt, receipt)
        run_pilot_console(config, backend, code, marker)
        result = work / f"pilot-configured-{kind}.json"
        if result.exists():
            require(private_json(result) == receipt, "Configured metadata receipt drift")
        else:
            save_once(result, receipt)


def pilot_upload_enable(config, approval, work):
    approved_operator(config, approval)
    with pilot_lock(work):
        require_pilot_acceptance(config, work)
        backend = metadata_backend(config)
        require(not active_executions(config)[1], "Worker must be idle during source upload activation")
        code, marker = remote_metadata_code(approval, "upload", config["tenant"], write=False)
        run_pilot_console(config, backend, code, marker)
        containers = safe_containers(backend)
        entries = environment_entries(containers[0])
        flag = entries.get("DOCINTEL_PILOT_UPLOAD_ENABLED")
        require(flag is None or flag == {"name": "DOCINTEL_PILOT_UPLOAD_ENABLED", "value": "false"}, "Pilot upload API must initially be disabled")
        values = upload_values(approval)
        baseline = {name: entries.get(name) for name in PILOT_UPLOAD_SETTINGS}
        intended = {name: {"name": name, "value": value} for name, value in values.items()}
        merge_environment(containers[0], values)
        receipt = {"target": fingerprint(config), "approval_sha256": fingerprint(approval), "baseline": baseline, "intended": intended}
        save_once(work / "pilot-upload-enablement.json", receipt)
        patch_pilot_backend(config, backend, containers, work)
        save_once(work / "pilot-upload-enabled.json", {"target": receipt["target"], "approval_sha256": receipt["approval_sha256"]})


def pilot_upload_disable(config, work):
    with pilot_lock(work):
        baseline = private_json(work / "pilot-upload-enablement.json")
        require(baseline["target"] == fingerprint(config), "Pilot upload disable target mismatch")
        backend = metadata_backend(config)
        containers = safe_containers(backend)
        entries = environment_entries(containers[0])
        actual = {name: entries.get(name) for name in PILOT_UPLOAD_SETTINGS}
        require(actual in (baseline["baseline"], baseline["intended"]), "Upload-managed settings drift; refusing unsafe rollback")
        if actual != baseline["baseline"]:
            for name, entry in baseline["baseline"].items():
                if entry is None:
                    entries.pop(name, None)
                else:
                    entries[name] = entry
            containers[0]["env"] = list(entries.values())
            save_once(work / "pilot-upload-disable-attempt.json", {"target": fingerprint(config)})
            patch_pilot_backend(config, backend, containers, work)
        if not (work / "pilot-upload-disabled.json").exists():
            save_once(work / "pilot-upload-disabled.json", {"target": fingerprint(config)})


def environment_entries(container):
    entries = container.get("env", [])
    require(len({entry["name"] for entry in entries}) == len(entries), "Duplicate environment entries require explicit review")
    return {entry["name"]: copy.deepcopy(entry) for entry in entries}


def merge_environment(container, updates):
    entries = environment_entries(container)
    entries.update({name: {"name": name, "value": value} for name, value in updates.items()})
    container["env"] = list(entries.values())


def require_pilot_identity(resource, principal, client_id=None):
    identity = resource.get("identity", {})
    if client_id:
        require(any(value.get("clientId") == client_id and value.get("principalId") == principal for value in identity.get("userAssignedIdentities", {}).values()), "Approved user-assigned worker identity is not attached")
    else:
        require("SystemAssigned" in identity.get("type", "") and identity.get("principalId") == principal, "Approved system-assigned principal differs from resource identity")


def require_pilot_job(config, approval):
    job, running = active_executions(config)
    require(not running, "A worker execution is already active")
    expected = f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}/providers/Microsoft.App/jobs/{config['job']}"
    require(job["id"].lower() == expected.lower(), "Pilot worker resource mismatch")
    settings = job["properties"]["configuration"]
    require(settings["replicaRetryLimit"] == 0 and settings["replicaTimeout"] == 600, "Pilot worker requires zero retries and a 600-second timeout")
    require(settings.get("manualTriggerConfig") == {"parallelism": 1, "replicaCompletionCount": 1}, "Pilot worker requires exactly one replica")
    containers = safe_containers(job)
    require(containers[0]["image"] == image(config, "backend"), "Pilot worker image mismatch")
    environment = environment_entries(containers[0])
    flag = environment.get("DOCINTEL_REAL_PILOT_ENABLED")
    require(flag is None or flag == {"name": "DOCINTEL_REAL_PILOT_ENABLED", "value": "false"}, "Persistent worker real-pilot mode must remain default-off")
    for name, value in {
        "DOCINTEL_BATCH_MODE": "hosted",
        "DOCINTEL_BATCH_STORAGE_URL": f"https://{config['storage']}.blob.core.windows.net",
        "DOCINTEL_BATCH_CONTAINER": config["container"],
        "DOCINTEL_BATCH_LIVE_ENABLED": "false",
    }.items():
        require(environment.get(name) == {"name": name, "value": value}, "Pilot worker storage/mode/synthetic guard drift")
    require_pilot_identity(job, approval["identities"]["worker_principal_id"], approval["environment"]["AZURE_CLIENT_ID"])
    return job


def require_pilot_build_receipts(config, work):
    for kind in ("backend", "frontend"):
        component_work = work
        seen = set()
        while True:
            require(component_work not in seen and len(seen) < 8, "Cyclic or excessive preserved publication chain")
            seen.add(component_work)
            source = verify_source(component_work)
            published = private_json(component_work / "images.json")
            require(published["revision"] == source["revision"] and published[kind] == image(config, kind), "Pilot component publication source/image mismatch")
            preserved = published.get("preserved_" + kind)
            if not preserved:
                break
            component_work = private_path(preserved["work"])
            require(fingerprint(private_json(component_work / "images.json")) == preserved["receipt_sha256"], "Preserved pilot publication receipt changed")
        require_component_source_receipts(component_work, kind)


def require_component_source_receipts(work, kind):
    """The same exact-source evidence is required before and after publication."""
    require(kind in {"backend", "frontend"}, "Unsupported source component")
    source = verify_source(work)
    clean = private_json(work / "clean-checks.json")
    smoke = private_json(work / f"{kind}-smoke.json")
    inventory = private_json(work / "context.json")
    modes = private_json(work / "context-modes.json")
    context = work / "context"
    entries = list(context.rglob("*"))
    require(all(not path.is_symlink() and (path.is_file() or path.is_dir()) for path in entries),
            "Build context links/special files are forbidden")
    require(inventory == {str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest() for path in entries if path.is_file()}, "Pilot smoke context content drift")
    require(modes == {str(path.relative_to(context)): path.stat().st_mode & 0o777 for path in entries}, "Pilot smoke context permission drift")
    require(all(source["files"].get(name) == digest for name, digest in inventory.items()), "Pilot tested context differs from published source")
    require(clean["revision"] == smoke["revision"] == source["revision"], "Pilot clean/runtime receipt revision mismatch")
    expected_clean = "clean_locked_install_offline_tests" if kind == "backend" else "clean_npm_ci_checks_build"
    require(clean.get(kind) == expected_clean, "Pilot requires actual clean component installation/build checks")
    require(clean["lock_sha256"] == source["files"].get("uv.lock"), "Pilot clean checks do not bind the published lock")
    require(smoke.get("passed") is True and smoke.get("network") == "none", "Pilot offline startup smoke did not pass")
    require(smoke["context_sha256"] == fingerprint(inventory) and smoke["modes_sha256"] == fingerprint(modes), "Pilot startup smoke did not test this exact build context")
    if kind == "backend":
        require(smoke.get("startup") == "health_200_anonymous_batch_401" and smoke.get("uid") == 10001, "Pilot backend startup/auth/nonroot smoke is missing")
    else:
        require(smoke.get("running") is True and smoke.get("permission_failure") is False and smoke.get("probe_exit_code") == 0, "Pilot frontend nonroot asset/startup smoke is missing")


def require_pilot_acceptance(config, work):
    require_release_images(config, work)
    require_pilot_build_receipts(config, work)
    receipt = private_json(work / "pilot-acceptance.json")
    require(set(receipt) == {"target", "revision", "backend_image", "frontend_image", "authenticated", "synthetic_accepted"}, "Pilot acceptance receipt schema mismatch")
    published = private_json(work / "images.json")
    require(receipt.get("target") == fingerprint(config) and receipt.get("revision") == published["revision"], "Pilot acceptance source/target mismatch")
    require(receipt.get("authenticated") is True and receipt.get("synthetic_accepted") is True, "Accepted hosted authentication and synthetic baseline required")
    for kind in ("backend", "frontend"):
        require(receipt.get(kind + "_image") == image(config, kind), "Pilot accepted image mismatch")
        resource = app(config, config[kind])
        properties = resource["properties"]
        require(properties["provisioningState"] == "Succeeded" and properties["latestRevisionName"] == properties["latestReadyRevisionName"], "Pilot app revision is not ready")
        containers = safe_containers(resource)
        require(containers[0]["image"] == image(config, kind), "Pilot app differs from accepted release image")
        require(writable_containers(desired_containers(config, kind, containers)) == writable_containers(containers), "Pilot hosted authentication/storage baseline drift")
        if kind == "backend":
            require_backend_runtime(containers, writable_ingress(properties["configuration"].get("ingress")))
        revision = azure("containerapp", "revision", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config[kind], "--revision", properties["latestReadyRevisionName"])
        require(revision["properties"]["healthState"] == "Healthy", "Pilot deployed revision is not healthy")
        require([entry["image"] for entry in revision["properties"]["template"]["containers"]] == [image(config, kind)], "Pilot healthy revision image differs from accepted digest")
        if kind == "backend":
            require_backend_runtime(safe_containers(revision), writable_ingress(properties["configuration"].get("ingress")))
    identity_contract(config)


def bind_pilot(config, approval, work):
    binding = {"target": fingerprint(config), "approval_sha256": fingerprint(approval), "approval_id": approval["id"], "batch_id": approval["batch_id"]}
    path = work / "pilot-binding.json"
    if path.exists():
        require(private_json(path) == binding, "Pilot approval/target drift cannot reset local allowance")
    else:
        save_once(path, binding)
    return binding


def remote_pilot_check_code(approval):
    marker = "DOCINTEL_REAL_PILOT_CONFIG_OK:" + fingerprint(approval)
    code = f'''import hashlib, json, os
from backend.batch_store import Missing, configured_store, read_json
from backend.real_pilot import BUDGET_KEY, RealPilotGuard, _sha256
store = configured_store()
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
assert hashlib.sha256(json.dumps(approval, sort_keys=True).encode()).hexdigest() == {fingerprint(approval)!r}
batch, _ = read_json(store, "batches/" + {approval['batch_id']!r} + ".json")
os.environ["DOCINTEL_REAL_PILOT_ENABLED"] = "true"
os.environ["DOCINTEL_REAL_PILOT_OPERATOR_IDS"] = {approval['approved_by']!r}
checker = RealPilotGuard.__new__(RealPilotGuard)
checker.store, checker.batch = store, batch
checker._validate()
try:
    ledger, _ = read_json(store, BUDGET_KEY)
except Missing:
    pass
else:
    assert ledger["approval_sha256"] == _sha256(approval) and not ledger.get("invalidated")
    assert len(ledger["executions"]) < approval["limits"]["executions"]
print({marker!r})
'''
    return code, marker


def verify_pilot_seed(config, approval):
    backend = app(config, config["backend"])
    require_pilot_identity(backend, approval["identities"]["api_principal_id"])
    code, marker = remote_pilot_check_code(approval)
    result = console_code([
        "az", "containerapp", "exec", "--subscription", config["subscription"],
        "-g", config["group"], "-n", config["backend"],
        "--revision", backend["properties"]["latestReadyRevisionName"],
        "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors",
    ], code, marker)
    require(marker in result.decode().splitlines(), "Private pilot approval/batch preflight did not confirm; no activation")


def patch_pilot_backend(config, expected, containers, work):
    require(app(config, config["backend"]) == expected, "Backend changed during pilot activation; refusing to overwrite drift")
    body = work / "pilot-backend-patch.json"
    save(body, build_update_payload(expected, containers))
    resource = expected["id"]
    arguments = ["rest", "--method", "PATCH", "--url", "https://management.azure.com" + resource + "?api-version=2024-03-01", "--body", "@" + str(body)]
    if expected.get("etag"):
        arguments.extend(("--headers", "If-Match=" + expected["etag"]))
    azure(*arguments)
    actual = app(config, config["backend"])
    require(writable_containers(safe_containers(actual)) == writable_containers(containers), "Pilot backend settings not confirmed; inspect before retrying")
    require(actual.get("identity") == expected.get("identity") and actual["properties"]["configuration"] == expected["properties"]["configuration"], "Pilot backend identity/auth configuration changed")


def pilot_enable(config, approval, work):
    approved_operator(config, approval)
    with pilot_lock(work):
        require_pilot_acceptance(config, work)
        require_pilot_job(config, approval)
        verify_pilot_seed(config, approval)
        binding = bind_pilot(config, approval, work)
        backend = app(config, config["backend"])
        containers = safe_containers(backend)
        entries = environment_entries(containers[0])
        upload_flag = entries.get("DOCINTEL_PILOT_UPLOAD_ENABLED")
        require(upload_flag is None or upload_flag == {"name": "DOCINTEL_PILOT_UPLOAD_ENABLED", "value": "false"}, "Disable pilot source upload before enabling real-pilot processing")
        flag = entries.get("DOCINTEL_REAL_PILOT_ENABLED")
        require(flag is None or flag == {"name": "DOCINTEL_REAL_PILOT_ENABLED", "value": "false"}, "Pilot API must initially be disabled")
        baseline = {name: entries.get(name) for name in PILOT_API_SETTINGS}
        intended = {name: {"name": name, "value": value} for name, value in pilot_values(approval).items()}
        merge_environment(containers[0], pilot_values(approval))
        pilot_window(approval)
        save_once(work / "pilot-enablement.json", {**binding, "baseline": baseline, "intended": intended, "backend_image": image(config, "backend")})
        patch_pilot_backend(config, backend, containers, work)
        save_once(work / "pilot-enabled.json", binding)


def pilot_start(config, approval, work, item_limit):
    approved_operator(config, approval)
    require(type(item_limit) is int and 1 <= item_limit <= min(4, approval["limits"]["products"]), "Pilot slice exceeds approved product count")
    with pilot_lock(work):
        require_pilot_acceptance(config, work)
        binding = bind_pilot(config, approval, work)
        require(private_json(work / "pilot-enabled.json") == binding, "Confirmed pilot API enablement receipt required")
        require(not (work / "pilot-disabled.json").exists(), "Pilot was disabled; no automatic reactivation")
        backend = app(config, config["backend"])
        values = environment_entries(safe_containers(backend)[0])
        require(all(values.get(name) == {"name": name, "value": value} for name, value in pilot_values(approval).items()), "Pilot API authorization settings drift")
        verify_pilot_seed(config, approval)
        job = require_pilot_job(config, approval)
        attempts = sorted(work.glob("pilot-execution-attempt-*.json"))
        for path in attempts:
            receipt = private_json(path)
            require(receipt["binding"] == binding, "Pilot attempt approval binding drift")
            require((work / f"pilot-execution-result-{receipt['attempt']}.json").exists(), "Prior worker start outcome is unknown; inspect before any continuation")
        require(len(attempts) < approval["limits"]["executions"], "Approved pilot execution allowance exhausted")
        number = len(attempts) + 1
        template = copy.deepcopy(job["properties"]["template"])
        container = template["containers"][0]
        container["command"] = ["/app/.venv/bin/python"]
        container["args"] = ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", approval["batch_id"], "--concurrency", "1", "--max-batches", "1", "--item-limit", str(item_limit)]
        scope = approval.get("execution_scope", "full")
        if scope == "internal_only":
            container["env"] = [entry for entry in container.get("env", [])
                                if entry["name"] not in PILOT_EXTERNAL_ENVIRONMENT_KEYS | {"WEBIQ_API_KEY"}]
        merge_environment(container, {
            **approval["environment"], **pilot_values(approval),
            "DOCINTEL_REAL_PILOT_EXECUTION_SCOPE": scope,
        })
        template = writable_execution_template(template)
        pilot_window(approval)
        fresh_job, running = active_executions(config)
        require(not running and fresh_job == job, "Worker changed before pilot start; inspect rather than overwrite drift")
        execution = work / f"pilot-execution-{number}.json"
        save_once(execution, template)
        save_once(work / f"pilot-execution-attempt-{number}.json", {"binding": binding, "attempt": number, "execution_sha256": fingerprint(template), "state": "start_attempted_completion_unknown"})
        result = azure("containerapp", "job", "start", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"],
                       "--yaml", str(execution), resource_snapshot=fresh_job, before_send=lambda: pilot_window(approval))
        require(isinstance(result, dict) and isinstance(result.get("name"), str) and result["name"], "Worker start outcome unknown; no automatic retry")
        save_once(work / f"pilot-execution-result-{number}.json", {"binding": binding, "attempt": number, "execution_name": result["name"]})


def pilot_disable(config, work):
    with pilot_lock(work):
        baseline = private_json(work / "pilot-enablement.json")
        require(baseline["target"] == fingerprint(config), "Pilot disable target mismatch")
        require(not active_executions(config)[1], "Stop the active worker explicitly before disabling pilot API")
        backend = app(config, config["backend"])
        containers = safe_containers(backend)
        entries = environment_entries(containers[0])
        actual = {name: entries.get(name) for name in PILOT_API_SETTINGS}
        require(actual in (baseline["intended"], baseline["baseline"]), "Pilot-managed settings drift; refusing unsafe rollback")
        if actual != baseline["baseline"]:
            for name, entry in baseline["baseline"].items():
                if entry is None:
                    entries.pop(name, None)
                else:
                    entries[name] = entry
            containers[0]["env"] = list(entries.values())
            save_once(work / "pilot-disable-attempt.json", {"target": fingerprint(config), "approval_sha256": baseline["approval_sha256"]})
            patch_pilot_backend(config, backend, containers, work)
        if not (work / "pilot-disabled.json").exists():
            save_once(work / "pilot-disabled.json", {"target": fingerprint(config), "approval_sha256": baseline["approval_sha256"]})


def load_config(path):
    path = private_path(path)
    require(path.stat().st_mode & 0o077 == 0, "Target configuration must be owner-only (mode 600)")
    config = json.loads(path.read_text())
    allowed = {"subscription", "tenant", "group", "environment", "registry", "storage", "frontend", "backend", "job", "container", "frontend_origin", "backend_origin", "api_client_id", "api_principal_id", "pilot_operator_ids", "publication_deadline", "frontend_client_id", "frontend_image", "backend_image", "auth_secret_ref", "entra_secret_ref", "baseline_authenticated"}
    require(set(config) <= allowed, "Unknown/private-secret fields are forbidden in release configuration")
    for field in ("subscription", "tenant"):
        require(str(uuid.UUID(config[field])) == config[field], f"Invalid {field}")
    if "api_principal_id" in config:
        require(str(uuid.UUID(config["api_principal_id"])) == config["api_principal_id"], "Invalid API managed-identity principal ID")
    if "publication_deadline" in config:
        publication_deadline(config)
    for field in ("group", "environment", "registry", "storage", "frontend", "backend", "job", "container", "auth_secret_ref", "entra_secret_ref"):
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,89}", config[field]), f"Invalid resource/reference name: {field}")
    for field in ("frontend_origin", "backend_origin"):
        require(re.fullmatch(r"https://[a-z0-9.-]+", config[field]), f"Invalid HTTPS origin: {field}")
    return config


def app(config, name, *, timeout=None):
    options = {} if timeout is None else {"timeout": timeout}
    return azure("containerapp", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", name, **options)


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


def desired_containers(config, kind, baseline, *, runtime=True):
    containers = copy.deepcopy(baseline)
    values = {entry["name"]: entry for entry in containers[0].get("env", [])}
    if kind == "backend":
        updates = {"DOCINTEL_BATCH_MODE": "hosted", "DOCINTEL_BATCH_STORAGE_URL": f"https://{config['storage']}.blob.core.windows.net", "DOCINTEL_BATCH_CONTAINER": config["container"], "DOCINTEL_AUTH_TENANT_ID": config["tenant"], "DOCINTEL_AUTH_AUDIENCE": config["api_client_id"], "DOCINTEL_AUTH_CLIENT_ID": config["frontend_client_id"], "DOCINTEL_BATCH_LIVE_ENABLED": "false"}
        values.pop("DOCINTEL_BATCH_HOME", None)
        if runtime:
            containers[0]["command"] = list(BACKEND_COMMAND)
            containers[0]["args"] = list(BACKEND_ARGUMENTS)
            updates.update({"API_PORT": "8080", "NEXT_PUBLIC_API_PORT": "8080"})
            probes = containers[0].get("probes")
            require(probes is None or isinstance(probes, list), "Invalid backend probe configuration")
            for probe in probes or []:
                handlers = [probe[name] for name in ("httpGet", "tcpSocket") if probe.get(name) is not None]
                require(len(handlers) == 1 and isinstance(handlers[0], dict) and handlers[0].get("port") in (80, 8080), "Backend probe must target the approved API port")
                handlers[0]["port"] = 8080
    else:
        updates = {"AUTH_URL": config["frontend_origin"], "DOCINTEL_PORTAL_ORIGIN": config["frontend_origin"], "DOCINTEL_BATCH_API_URL": config["backend_origin"] + "/api/v1/batches", "DOCINTEL_BATCH_DEV": "false", "DOCINTEL_API_SCOPE": f"api://{config['api_client_id']}/Batch.Access", "AUTH_MICROSOFT_ENTRA_ID_ID": config["frontend_client_id"], "AUTH_MICROSOFT_ENTRA_ID_TENANT_ID": f"https://login.microsoftonline.com/{config['tenant']}/v2.0"}
        for name, reference in (("AUTH_SECRET", "auth_secret_ref"), ("AUTH_MICROSOFT_ENTRA_ID_SECRET", "entra_secret_ref")):
            values[name] = {"name": name, "secretRef": config[reference]}
    values.update({name: {"name": name, "value": value} for name, value in updates.items()})
    containers[0]["env"] = list(values.values())
    containers[0]["image"] = image(config, kind)
    return containers


def writable_containers(containers):
    result = copy.deepcopy(containers)
    for container in result:
        # A newer GET-only discriminator has no 2024-03-01 wire equivalent.
        require(container.get("imageType") in (None, "ContainerImage"), "Unsupported image type for API 2024-03-01")
        container.pop("imageType", None)
    return write_schema().project_component("containerApps", "PATCH", ("properties", "template", "containers"), result)


def writable_ingress(ingress):
    if ingress is None:
        return None
    require(isinstance(ingress, dict), "Ingress must be an object")
    projected = copy.deepcopy(ingress)
    projected.pop("targetPortHttpScheme", None)
    return write_schema().project_component("containerApps", "PATCH", ("properties", "configuration", "ingress"), projected)


def desired_ingress(kind, baseline):
    ingress = writable_ingress(baseline)
    if kind == "backend":
        require(ingress is not None and ingress.get("targetPort") in (80, 8080) and ingress.get("allowInsecure") is False, "Backend requires existing HTTPS-only ingress on the approved API port")
        ingress["targetPort"] = 8080
    return ingress


def require_backend_runtime(containers, ingress):
    container = containers[0]
    require(container.get("command") == BACKEND_COMMAND and container.get("args") == BACKEND_ARGUMENTS, "Backend requires the nonroot-compatible explicit port-8080 FastAPI command")
    values = environment_entries(container)
    require(all(values.get(name) == {"name": name, "value": "8080"} for name in ("API_PORT", "NEXT_PUBLIC_API_PORT")), "Backend internal API port settings drift")
    require(ingress is not None and ingress.get("targetPort") == 8080 and ingress.get("allowInsecure") is False, "Backend ingress must remain HTTPS-only and target port 8080")
    probes = container.get("probes")
    require(probes is None or isinstance(probes, list), "Invalid backend probe configuration")
    for probe in probes or []:
        handlers = [probe[name] for name in ("httpGet", "tcpSocket") if probe.get(name) is not None]
        require(len(handlers) == 1 and isinstance(handlers[0], dict) and handlers[0].get("port") == 8080, "Backend probes must target port 8080")


def preserve_json(path, value):
    if path.exists():
        require(private_json(path) == value, "Existing immutable runtime receipt differs; inspect instead of overwriting")
    else:
        save_once(path, value)


def runtime_baseline(config, work, baseline, resources):
    path = work / "runtime-baseline.json"
    baseline_hash = hashlib.sha256((work / "baseline.json").read_bytes()).hexdigest()
    if path.exists():
        record = private_json(path)
        require(record["target"] == fingerprint(config) and record["baseline_sha256"] == baseline_hash and record["baseline"] == baseline, "Runtime rollback snapshot drift")
    else:
        record = {"target": fingerprint(config), "baseline_sha256": baseline_hash, "baseline": baseline, "apps": {}}
        for kind in ("backend", "frontend"):
            original = baseline["apps"][kind]
            current = safe_containers(resources[kind])
            intended_path = work / (kind + "-intended.json")
            prior = private_json(intended_path) if intended_path.exists() else None
            expected = desired_containers(config, kind, original["containers"], runtime=False)
            if prior is not None:
                require(writable_containers(prior) in (writable_containers(expected), writable_containers(desired_containers(config, kind, original["containers"]))), "Prior recorded intent is not the exact release intent")
            if "ingress" in original:
                ingress = original["ingress"]
            else:
                require(writable_containers(current) in (writable_containers(original["containers"]), writable_containers(prior) if prior is not None else None), "Cannot capture missing ingress from unrelated or unrecorded state")
                ingress = resources[kind]["properties"]["configuration"].get("ingress")
                require(ingress is not None and (kind != "backend" or ingress.get("targetPort") == 80), "Original ingress is missing; refuse to invent rollback port/settings")
            writable_ingress(ingress)
            record["apps"][kind] = {
                "ingress": copy.deepcopy(ingress), "prior_intended": prior,
                "prior_intended_sha256": hashlib.sha256(intended_path.read_bytes()).hexdigest() if prior is not None else None,
            }
        preserve_json(path, record)
    for kind, original in record["apps"].items():
        intended_path = work / (kind + "-intended.json")
        actual = hashlib.sha256(intended_path.read_bytes()).hexdigest() if intended_path.exists() else None
        require(actual == original["prior_intended_sha256"], "Prior recorded intent changed since runtime snapshot")
    return record


def runtime_targets(config, baseline, snapshot, resources, *, allow_isolated=False):
    targets = {}
    for kind in ("backend", "frontend"):
        original = baseline["apps"][kind]["containers"]
        ingress = writable_ingress(snapshot["apps"][kind]["ingress"])
        desired = desired_containers(config, kind, original)
        target_ingress = desired_ingress(kind, ingress)
        restored = copy.deepcopy(original)
        restored[0]["image"] = baseline["apps"][kind].get("pinned_image", original[0]["image"])
        states = [(writable_containers(original), ingress), (writable_containers(restored), ingress), (writable_containers(desired), target_ingress)]
        prior = snapshot["apps"][kind]["prior_intended"]
        if prior is not None:
            prior_ingress = target_ingress if writable_containers(prior) == writable_containers(desired) else ingress
            states.append((writable_containers(prior), prior_ingress))
        if allow_isolated:
            states.extend((containers, None) for containers, _ in list(states))
        resource = resources[kind]
        actual = (writable_containers(safe_containers(resource)), writable_ingress(resource["properties"]["configuration"].get("ingress")))
        require(actual in states, "App or ingress drift since baseline/recorded intent; stop rather than overwrite")
        targets[kind] = {"containers": desired, "ingress": target_ingress}
    return targets


def patch_app(config, kind, containers, work, *, ingress=UNCHANGED_INGRESS):
    observed = app(config, config[kind])
    if ingress is not UNCHANGED_INGRESS and ingress is not None:
        ingress = write_schema().project_component(
            "containerApps", "PATCH", ("properties", "configuration", "ingress"), writable_ingress(ingress),
        )
    configuration = UNCHANGED_INGRESS if ingress is UNCHANGED_INGRESS else {"ingress": writable_ingress(ingress)}
    payload = build_update_payload(observed, containers, configuration=configuration)
    body = work / (kind + "-patch-" + fingerprint(payload) + ".json")
    preserve_json(body, payload)
    resource = f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}/providers/Microsoft.App/containerApps/{config[kind]}"
    azure("rest", "--method", "PATCH", "--url", "https://management.azure.com" + resource + "?api-version=2024-03-01", "--body", "@" + str(body))


def active_executions(config, *, timeout=None):
    options = {} if timeout is None else {"timeout": timeout}
    job = azure("containerapp", "job", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"], **options)
    require(job["properties"]["configuration"]["triggerType"] == "Manual", "Refusing a scheduled or event-driven job")
    executions = azure("containerapp", "job", "execution", "list", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"], **options)
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
    for path in source.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() or path.stat().st_mode & 0o111 else 0o600)
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
    save(work / "context-modes.json", {str(path.relative_to(target)): path.stat().st_mode & 0o777 for path in target.rglob("*")})
    return target


def check_lock(before, after):
    original = {entry["name"]: entry["version"] for entry in before["package"]}
    resolved = {entry["name"]: entry["version"] for entry in after["package"]}
    require(all(resolved.get(name) == version for name, version in original.items() if name not in {"azure-ai-documentintelligence", "pyjwt"}), "Resolver changed unrelated package versions; stop for review")
    require(all(entry.get("source", {}).get("registry", "https://pypi.org/simple") == "https://pypi.org/simple" for entry in after["package"]), "Registry differs from committed public PyPI choice")
    require(resolved.get("azure-ai-documentintelligence") == "1.0.2", "Committed Document Intelligence constraint was not satisfied")


def package(work, approved, backend_only=False):
    require(approved, "An existing approved runner with verified package/artifact access is required; do not retry the known failing host")
    require(not any(os.environ.get(name) for name in ("UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL")), "Review inherited registry overrides before using the committed registry; no automatic substitution")
    source = work / "source"
    verify_source(work)
    baseline = work / "baseline.lock"
    require(not baseline.exists(), "Prior packaging attempt exists; inspect it rather than retrying a failed download loop")
    require(command(["uv", "--version"]).decode().startswith("uv 0.11.31"), "Use the pinned uv 0.11.31 toolchain")
    if not backend_only:
        require(int(command(["node", "--version"]).decode().strip().lstrip("v").split(".")[0]) == 22, "Clean frontend checks require the Dockerfile's Node 22 toolchain")
    baseline.write_bytes((source / "uv.lock").read_bytes())
    environment = {**os.environ, "UV_HTTP_RETRIES": "0", "UV_PYTHON_DOWNLOADS": "never", "UV_PROJECT_ENVIRONMENT": str(work / ".clean-venv"), "PYTHONDONTWRITEBYTECODE": "1", "NEXT_TELEMETRY_DISABLED": "1"}
    command(["uv", "lock", "--directory", str(source)], env=environment)
    check_lock(tomllib.loads(baseline.read_text()), tomllib.loads((source / "uv.lock").read_text()))
    command(["uv", "sync", "--locked", "--python", "3.13", "--directory", str(source)], env=environment)
    command([str(work / ".clean-venv/bin/python"), "-m", "pytest", "tests/test_batch.py", "tests/test_batch_api.py", "tests/test_workbooks.py", "tests/test_release.py", "tests/test_release_continuation.py", "tests/test_pilot.py", "tests/test_pilot_api.py", "tests/test_pilot_server.py", "-q"], cwd=source, env=environment)
    if not backend_only:
        frontend = source / "frontend"
        command(["npm", "ci", "--no-audit", "--no-fund"], cwd=frontend, env=environment)
        for arguments in (["node_modules/.bin/eslint", "."], ["node_modules/.bin/tsc", "--noEmit", "--incremental", "false"], ["node", "--test", *[str(path) for path in (frontend / "tests").glob("*.test.mjs")]], ["npm", "run", "build"]):
            command(arguments, cwd=frontend, env=environment)
    verify_source(work, allow_lock_change=True)
    revision = json.loads((work / "source.json").read_text())["revision"]
    save(work / "clean-checks.json", {"revision": revision, "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest(), "frontend": "unchanged_not_rebuilt" if backend_only else "clean_npm_ci_checks_build", "backend": "clean_locked_install_offline_tests"})
    print("Isolated clean checks passed. Review/commit the generated lock before release; no image was built or published.")


def require_release_images(config, work):
    verify_source(work)
    receipt = json.loads((work / "images.json").read_text())
    source = json.loads((work / "source.json").read_text())
    require(receipt["revision"] == source["revision"], "Published source mismatch")
    for kind in ("backend", "frontend"):
        preserved = receipt.get("preserved_" + kind)
        if preserved is None:
            continue
        previous = private_path(preserved["work"])
        previous_source = verify_source(previous)
        previous_images = json.loads((previous / "images.json").read_text())
        require(previous_images["revision"] == previous_source["revision"], "Preserved source mismatch")
        require(fingerprint(previous_images) == preserved["receipt_sha256"], "Preserved publication receipt changed")
        require(previous_images[kind] == receipt[kind], "Preserved digest mismatch")
    for kind in ("backend", "frontend"):
        require(receipt[kind] == image(config, kind), "Configured digest differs from the release publication receipt")


def publish_frontend(config, work, previous_work):
    publish_component(config, work, previous_work, "frontend")


def publication_deadline(config):
    value = config.get("publication_deadline")
    require(isinstance(value, str), "Private target publication_deadline is required for replacement")
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Private target publication_deadline must be a timezone-aware ISO timestamp") from None
    require(deadline.tzinfo is not None, "Private target publication_deadline must be a timezone-aware ISO timestamp")
    return deadline


def validate_publication_replacement(approval, config, work, revision):
    fields = {"schema_version", "approved", "id", "approved_by", "target", "work", "revision", "component", "original_attempt", "original_attempt_sha256", "original_failure", "additional_attempts", "cpu", "timeout_seconds", "expires_at"}
    require(isinstance(approval, dict) and set(approval) == fields, "Publication replacement approval schema mismatch")
    for name, value in (("schema_version", 1), ("additional_attempts", 1), ("cpu", 2), ("timeout_seconds", 900)):
        require(type(approval[name]) is int and approval[name] == value, "Replacement approval may authorize only one two-CPU, 900-second attempt")
    require(approval["approved"] is True and approval["component"] == "backend", "Only an explicitly approved backend replacement is supported")
    for name in ("id", "approved_by"):
        require(isinstance(approval[name], str) and str(uuid.UUID(approval[name])) == approval[name], "Canonical replacement approval/operator UUID required")
    approved_operator(config, approval)
    require(approval["target"] == fingerprint(config) and approval["work"] == str(work.resolve()), "Replacement target/work mismatch")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) and approval["revision"] == revision, "Replacement source revision mismatch")
    end = datetime.fromisoformat(approval["expires_at"])
    deadline = publication_deadline(config)
    now = datetime.now(timezone.utc)
    require(deadline > now and end.tzinfo is not None and now < end <= deadline, "Replacement approval expired or exceeds the private target publication_deadline")
    original = work / "backend-publication-attempt.json"
    require(approval["original_attempt"] == str(original.resolve()), "Replacement original attempt path mismatch")
    record = private_json(original)
    require(approval["original_attempt_sha256"] == hashlib.sha256(original.read_bytes()).hexdigest(), "Replacement original attempt hash mismatch")
    require(record == {"revision": revision, "component": "backend", "cpu": 2, "timeout_seconds": 900, "status": "attempted"}, "Only the original CLI-rejected attempt may be replaced")
    require(approval["original_failure"] == "unsupported_acr_build_cpu_argument", "Explicit pre-submission CLI failure attestation required")
    require(not any((work / name).exists() for name in ("backend-publication-result.json", "backend-publication-queued.json", "backend-publication-request.json", "backend-publication-submission.json")), "Original attempt has remote-submission evidence; replacement refused")
    return approval


def validate_publication_supplemental(approval, config, work, revision):
    fields = {"schema_version", "approved", "id", "approved_by", "target", "work", "revision", "original_attempt", "original_attempt_sha256", "replacement_attempt", "replacement_attempt_sha256", "metadata_requests", "additional_backend_attempts", "cpu", "timeout_seconds", "expires_at"}
    require(isinstance(approval, dict) and set(approval) == fields, "Publication supplemental approval schema mismatch")
    for name, value in (("schema_version", 1), ("metadata_requests", 1), ("additional_backend_attempts", 1), ("cpu", 2), ("timeout_seconds", 900)):
        require(type(approval[name]) is int and approval[name] == value, "Supplemental approval permits one metadata preflight and one two-CPU, 900-second backend attempt")
    require(approval["approved"] is True, "Explicit supplemental publication approval required")
    for name in ("id", "approved_by"):
        require(isinstance(approval[name], str) and str(uuid.UUID(approval[name])) == approval[name], "Canonical supplemental approval/operator UUID required")
    approved_operator(config, approval)
    require(approval["target"] == fingerprint(config) and approval["work"] == str(work.resolve()), "Supplemental target/work mismatch")
    require(re.fullmatch(r"[0-9a-f]{40}", revision) and approval["revision"] == revision, "Supplemental source revision mismatch")
    end = datetime.fromisoformat(approval["expires_at"])
    require(end.tzinfo is not None and datetime.now(timezone.utc) < end <= publication_deadline(config), "Supplemental approval expired or exceeds private target deadline")
    for field, stem in (("original_attempt", "backend-publication"), ("replacement_attempt", "backend-publication-replacement")):
        path = work / (stem + "-attempt.json")
        require(approval[field] == str(path.resolve()), "Supplemental prior attempt path mismatch")
        record = private_json(path)
        require(approval[field + "_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest(), "Supplemental prior attempt hash mismatch")
        require(all(record.get(name) == value for name, value in {"revision": revision, "component": "backend", "cpu": 2, "timeout_seconds": 900, "status": "attempted"}.items()), "Supplemental prior attempt source/component/bounds mismatch")
        require(not any((work / (stem + suffix)).exists() for suffix in ("-request.json", "-submission.json", "-queued.json", "-result.json")), "Prior attempt has upload/submission evidence; supplemental scope mismatch")
    return approval


def require_unused_publication(work, kinds, replacement=None, supplemental=None):
    require_standard_release_work(work)
    require(replacement is None or supplemental is None, "Replacement and supplemental approvals are mutually exclusive")
    require(not (work / "images.json").exists() and not (work / "publication-progress.json").exists(), "Publication already attempted or accepted; no automatic retry")
    require(not any((work / name).exists() for name in SUPPLEMENTAL_PUBLICATION_RECORDS), "Supplemental publication already attempted; no automatic retry")
    for kind in kinds:
        require(kind in {"backend", "frontend"}, "Unsupported publication component")
        replacing = kind == "backend" and replacement is not None
        supplementing = kind == "backend" and supplemental is not None
        stem = kind + "-publication" + ("-supplemental" if supplementing else "-replacement" if replacing else "")
        if kind == "backend" and not supplementing:
            require(not (work / "backend-publication-replacement-attempt.json").exists(), "Component publication already attempted; no automatic retry")
        require(not (work / f"{stem}-attempt.json").exists() and not (work / f"{stem}-result.json").exists(), "Component publication already attempted; no automatic retry")


def require_standard_release_work(work):
    require(not any((directory / "continuation-baseline.json").exists()
                    for directory in (work, *work.parents)),
            "Pinned continuation history requires pilot_continuation.py; a context child is not a fresh allowance")


def reserve_publication_attempt(work, kind, revision, *, replacement=None, config=None, supplemental=None):
    require_standard_release_work(work)
    require(kind in {"backend", "frontend"}, "Unsupported publication component")
    require(replacement is None or supplemental is None, "Replacement and supplemental approvals are mutually exclusive")
    replacing = kind == "backend" and replacement is not None
    supplementing = kind == "backend" and supplemental is not None
    stem = kind + "-publication" + ("-supplemental" if supplementing else "-replacement" if replacing else "")
    if supplemental is None:
        require(not any((work / name).exists() for name in SUPPLEMENTAL_PUBLICATION_RECORDS), "Supplemental publication already attempted; no automatic retry")
    else:
        validate_publication_supplemental(supplemental, config, work, revision)
        preflight_attempt = private_json(work / "metadata-preflight-attempt.json")
        preflight = private_json(work / "metadata-preflight-result.json")
        require(preflight_attempt["approval_sha256"] == preflight["approval_sha256"] == fingerprint(supplemental) and preflight["status"] == "validated", "A successful bound metadata preflight is required")
        if kind == "frontend":
            progress = private_json(work / "publication-progress.json")
            require(progress["revision"] == revision and "backend_run_id" in progress, "Supplemental backend must succeed before original frontend publication")
    if replacement is not None:
        validate_publication_replacement(replacement, config, work, revision)
    if kind == "backend" and not replacing and not supplementing:
        require(not (work / "backend-publication-replacement-attempt.json").exists(), "Component publication already attempted; no automatic retry")
    require(not (work / f"{stem}-result.json").exists(), "Component publication already attempted; no automatic retry")
    record = {
        "revision": revision, "component": kind, "cpu": 2,
        "timeout_seconds": 900, "status": "attempted",
        "attempt_id": str(uuid.uuid4()),
    }
    if replacement is not None:
        record.update({"replacement_approval": replacement, "replacement_approval_sha256": fingerprint(replacement)})
    if supplemental is not None:
        record.update({"supplemental_approval": supplemental, "supplemental_approval_sha256": fingerprint(supplemental)})
    path = work / f"{stem}-attempt.json"
    save_once(path, record)
    return path


def publication_source_path(value):
    require(isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 4096, "Invalid registry build-source relative path")
    require(not any(char in value for char in "\\?#") and not re.search(r"%(?![0-9A-Fa-f]{2})", value), "Invalid registry build-source relative path")
    segments = tuple(unquote(segment, encoding="utf-8", errors="strict") for segment in value.split("/"))
    require(all(segment not in {"", ".", ".."} and not any(char in segment for char in "/\\%") and not any(ord(char) < 32 or ord(char) == 127 for char in segment) for segment in segments) and ":" not in segments[0], "Invalid registry build-source relative path")
    return segments


def publication_upload_metadata(upload):
    def describe(value):
        result = {"type": type(value).__name__}
        if isinstance(value, str):
            raw = value.encode("utf-8", errors="surrogatepass")
            result.update({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "segments": value.count("/") + 1,
                           "leading_slash": value.startswith("/"), "has_backslash": "\\" in value,
                           "has_percent": "%" in value, "has_query_or_fragment": "?" in value or "#" in value,
                           "has_controls": any(ord(char) < 32 or ord(char) == 127 for char in value)})
        return result

    source = upload.get("relativePath") if isinstance(upload, dict) else None
    url = upload.get("uploadUrl") if isinstance(upload, dict) else None
    result = {"response_type": type(upload).__name__, "relative_path": describe(source), "upload_url": {"type": type(url).__name__}}
    if isinstance(url, str):
        result["upload_url"]["bytes"] = len(url.encode("utf-8", errors="surrogatepass"))
        try:
            parts = urlsplit(url)
            result["upload_url"].update({
                "https": parts.scheme == "https", "has_query": bool(parts.query),
                "trusted_blob_host": bool(re.fullmatch(r"[a-z0-9]+\.blob\.core\.windows\.net", parts.hostname or "")),
                "path": describe(parts.path),
            })
        except ValueError:
            result["upload_url"]["parseable"] = False
    return result


def publication_request(kind, revision, source_location):
    require(kind in {"backend", "frontend"} and re.fullmatch(r"[0-9a-f]{40}", revision), "Exact publication component/source required")
    publication_source_path(source_location)
    return {
        "type": "DockerBuildRequest", "imageNames": [f"docintel/{kind}:{revision}"],
        "isPushEnabled": True, "isArchiveEnabled": False,
        "sourceLocation": source_location,
        "dockerFilePath": "backend/Dockerfile" if kind == "backend" else "Dockerfile",
        "platform": {"os": "Linux", "architecture": "amd64"},
        "agentConfiguration": {"cpu": 2}, "timeout": 900,
    }


def archive_publication_context(work, kind, destination):
    context = work / "context"
    inventory = private_json(work / "context.json", max_bytes=4 * 1024 * 1024)
    modes = private_json(work / "context-modes.json", max_bytes=4 * 1024 * 1024)
    entries = sorted(context.rglob("*"))
    require(all(not path.is_symlink() and (path.is_file() or path.is_dir()) for path in entries), "Build context links/special files are forbidden")
    require(inventory == {str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest() for path in entries if path.is_file()}, "Build context drift")
    require(modes == {str(path.relative_to(context)): path.stat().st_mode & 0o777 for path in entries}, "Build context permission drift")
    build_root = context if kind == "backend" else context / "frontend"
    dockerfile = build_root / ("backend/Dockerfile" if kind == "backend" else "Dockerfile")
    require(dockerfile.is_file(), "Verified Dockerfile is missing")
    for path in entries:
        relative = path.relative_to(context)
        require(relative.parts[0] in {"backend", "frontend", "pyproject.toml", "uv.lock", ".dockerignore"}, "Uncurated build context")
        require(not any(part in {".git", ".azure", ".venv", "node_modules", "__pycache__"} or part.startswith(".env") for part in relative.parts), "Private/runtime files are forbidden in build context")
        require(path.suffix.lower() not in {".pdf", ".xlsx", ".xlsm", ".db", ".sqlite", ".pem", ".key", ".pfx", ".p12", ".log"}, "Evidence/secrets are forbidden in build context")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                for path in entries:
                    if path != build_root and path.is_relative_to(build_root):
                        archive.add(path, arcname=path.relative_to(build_root).as_posix(), recursive=False)
    except BaseException:
        destination.unlink()
        raise
    return fingerprint(inventory)


class PublicationUploadError(ValueError):
    def __init__(self, stage, upload):
        self.stage = stage
        super().__init__("Invalid registry upload destination or relative path; validation_stage=" + stage + "; safe metadata: " + json.dumps(publication_upload_metadata(upload), sort_keys=True))


def validate_publication_upload(upload, *, now=None, valid_until=None):
    stage = "response_shape"
    try:
        require(isinstance(upload, dict), "Invalid registry upload response")
        source, url = upload.get("relativePath"), upload.get("uploadUrl")
        stage = "source_path"
        source_segments = publication_source_path(source)
        stage = "upload_url"
        require(isinstance(url, str) and len(url.encode("utf-8")) <= 16384 and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url), "Invalid registry upload URL")
        parts = urlsplit(url)
        stage = "https_blob_destination"
        require(parts.scheme == "https" and parts.hostname is not None and re.fullmatch(r"[a-z0-9]+\.blob\.core\.windows\.net", parts.hostname) and parts.port in (None, 443) and not parts.username and not parts.password and not parts.fragment and parts.path.startswith("/"), "Invalid registry upload destination")
        stage = "blob_path"
        blob_segments = publication_source_path(parts.path[1:])
        stage = "source_blob_binding"
        require(len(blob_segments) >= 2 and blob_segments[-len(source_segments):] == source_segments, "Invalid registry upload destination binding")
        stage = "sas_signature"
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=32)
        signatures = query.get("sig", [])
        require(len(signatures) == 1 and bool(signatures[0]), "Invalid registry upload signature")
        if valid_until is not None:
            stage = "sas_expiry"
            require(now is not None and now.tzinfo is not None and valid_until.tzinfo is not None and valid_until > now, "Invalid SAS validation window")
            expiry = query.get("se", [])
            require(len(expiry) == 1, "SAS expiry is required")
            end = datetime.fromisoformat(expiry[0])
            require(end.tzinfo is not None and end >= valid_until, "SAS expiry does not cover the bounded upload/queue window")
            stage = "sas_not_before"
            starts = query.get("st")
            if starts is not None:
                require(len(starts) == 1, "Invalid SAS start")
                start = datetime.fromisoformat(starts[0])
                require(start.tzinfo is not None and start <= now < end, "SAS is not yet active")
            stage = "sas_write_permission"
            permissions = query.get("sp", [])
            require(len(permissions) == 1 and bool(set(permissions[0]) & {"w", "c"}), "SAS requires write or create permission")
    except (ValueError, TypeError):
        raise PublicationUploadError(stage, upload) from None
    return source


def upload_publication_context(upload, archive, timeout, *, before_send=None, on_preflight=None):
    source = validate_publication_upload(upload)
    require(Path(archive).is_file() and type(timeout) in (int, float) and 0 < timeout < float("inf"),
            "Binary Blob PUT requires an existing archive and a finite positive timeout")
    packet = {"contract": "binary_blob", "method": "PUT", "url": upload["uploadUrl"],
              "archive": str(Path(archive).resolve()), "timeout": timeout,
              "headers": {"x-ms-blob-type": "BlockBlob", "x-ms-version": "2024-08-04",
                          "Content-Type": "application/octet-stream", "Content-Length": Path(archive).stat().st_size}}
    _emit_preflight(on_preflight, native_http(packet), "binary_source_upload")
    if before_send is not None:
        before_send()
    result = native_http(packet, send=True)
    require(result["http_status"] == 201, "Private build-source upload failed; details withheld, no automatic retry")
    return source


def supplemental_upload_window(approval):
    now = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(approval["expires_at"])
    require((expires - now).total_seconds() >= 900, "Insufficient approved time remains for a 900-second build; no next upload/queue action")
    return now, min(expires, now + timedelta(seconds=1200)) + timedelta(seconds=60)


def publication_metadata_preflight(config, work, revision, approval):
    validate_publication_supplemental(approval, config, work, revision)
    require_unused_publication(work, ("backend", "frontend"), supplemental=approval)
    supplemental_upload_window(approval)
    binding = {"approval_sha256": fingerprint(approval), "target": fingerprint(config), "source_revision": revision}
    save_once(work / "metadata-preflight-attempt.json", {**binding, "approval": approval, "stage": "metadata_request", "status": "attempted"})
    resource = f"https://management.azure.com/subscriptions/{config['subscription']}/resourceGroups/{config['group']}/providers/Microsoft.ContainerRegistry/registries/{config['registry']}"
    try:
        supplemental_upload_window(approval)
        upload = azure("rest", "--method", "POST", "--url", resource + "/listBuildSourceUploadUrl?api-version=" + PUBLICATION_API_VERSION,
                       timeout=60, before_send=lambda: supplemental_upload_window(approval))
    except (ValueError, OSError, TypeError):
        save_once(work / "metadata-preflight-result.json", {**binding, "stage": "metadata_request", "status": "failed_or_unknown"})
        raise ValueError("Supplemental metadata request failed or is unknown; no upload/build reserved, no retry") from None
    shape = publication_upload_metadata(upload)
    try:
        now, valid_until = supplemental_upload_window(approval)
        validate_publication_upload(upload, now=now, valid_until=valid_until)
    except ValueError as error:
        stage = error.stage if isinstance(error, PublicationUploadError) else "approval_window"
        save_once(work / "metadata-preflight-result.json", {**binding, "stage": stage, "status": "rejected", "shape": shape})
        raise ValueError("Supplemental metadata preflight rejected at " + stage + "; safe metadata: " + json.dumps(shape, sort_keys=True) + "; no upload/build reserved, no retry") from None
    save_once(work / "metadata-preflight-result.json", {**binding, "stage": "metadata_validation", "status": "validated", "shape": shape})
    return upload


def build_publication(config, work, kind, revision, *, replacement=None, supplemental=None, upload_metadata=None):
    require(replacement is None or supplemental is None, "Replacement and supplemental approvals are mutually exclusive")
    require(upload_metadata is None or (supplemental is not None and kind == "backend"), "Only supplemental backend may reuse preflight metadata")
    if supplemental is not None and kind == "backend":
        require(upload_metadata is not None, "Supplemental backend requires the in-memory preflight response")
        now, valid_until = supplemental_upload_window(supplemental)
        validate_publication_upload(upload_metadata, now=now, valid_until=valid_until)
        require(private_json(work / "metadata-preflight-result.json")["shape"] == publication_upload_metadata(upload_metadata), "Preflight metadata binding changed")
    attempt = reserve_publication_attempt(work, kind, revision, replacement=replacement, config=config, supplemental=supplemental)
    authorization = supplemental if supplemental is not None else replacement
    expires = datetime.fromisoformat(authorization["expires_at"]) if authorization is not None else None
    return execute_publication(
        config, work, kind, revision, attempt, expires=expires,
        upload_metadata=upload_metadata, validate_upload_window=supplemental is not None,
    )


def execute_publication(config, work, kind, revision, attempt, *, expires=None,
                        upload_metadata=None, validate_upload_window=False, on_preflight=None):
    """Execute an already reserved attempt; callers own the immutable authority."""
    require(attempt.is_file(), "Publication must be reserved before requesting upload metadata")
    stem = attempt.name.removesuffix("-attempt.json")
    deadline = time.monotonic() + min(1200, (expires - datetime.now(timezone.utc)).total_seconds() if expires else 1200)
    resource = f"https://management.azure.com/subscriptions/{config['subscription']}/resourceGroups/{config['group']}/providers/Microsoft.ContainerRegistry/registries/{config['registry']}"
    archive = work / (stem + "-context.tar.gz")
    archive_created = False

    def remaining(maximum=60):
        seconds = deadline - time.monotonic()
        if expires is not None:
            seconds = min(seconds, (expires - datetime.now(timezone.utc)).total_seconds())
        require(seconds > 0, "Publication observation deadline reached; outcome unknown, never resubmit")
        return min(maximum, seconds)

    def build_window():
        require(remaining(1200) >= 900, "Insufficient approved time remains for a 900-second build; no next upload/queue action")

    def rest(method, suffix, *arguments):
        hooks = {}
        if method == "POST":
            build_window()
            if on_preflight is not None:
                stage = ("publication_upload_metadata" if suffix == "/listBuildSourceUploadUrl"
                         else "publication_schedule_run")
                hooks["on_preflight"] = lambda proof: _emit_preflight(on_preflight, proof, stage)
        return azure("rest", "--method", method, "--url", resource + suffix + "?api-version=" + PUBLICATION_API_VERSION,
                     *arguments, timeout=remaining(), before_send=build_window if method == "POST" else None, **hooks)

    try:
        context_hash = archive_publication_context(work, kind, archive)
        archive_created = True
        tool_revision = command(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
        require(re.fullmatch(r"[0-9a-f]{40}", tool_revision), "Publication helper revision is invalid")
        upload = upload_metadata if upload_metadata is not None else rest("POST", "/listBuildSourceUploadUrl")
        save_once(work / (stem + "-upload-metadata.json"), publication_upload_metadata(upload))
        if expires is not None:
            require(remaining(1200) >= 900, "Insufficient approved time remains for a 900-second build; no next upload/queue action")
        if validate_upload_window:
            now, valid_until = supplemental_upload_window({"expires_at": expires.isoformat()})
            validate_publication_upload(upload, now=now, valid_until=valid_until)
        hooks = {"on_preflight": on_preflight} if on_preflight is not None else {}
        source = upload_publication_context(upload, archive, remaining(120), before_send=build_window, **hooks)
        body = publication_request(kind, revision, source)
        body_path = work / (stem + "-request.json")
        save_once(body_path, body)
        submission = {
            "target": fingerprint(config), "source_revision": revision,
            "tool_revision": tool_revision, "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "attempt_sha256": hashlib.sha256(attempt.read_bytes()).hexdigest(),
            "request_sha256": hashlib.sha256(body_path.read_bytes()).hexdigest(),
            "context_sha256": context_hash, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "api_version": PUBLICATION_API_VERSION, "cpu": 2, "timeout_seconds": 900,
            "expires_at": expires.isoformat() if expires else None,
        }
        require(remaining(1200) >= 900, "Insufficient approved time remains for a 900-second build; no run submitted")
        if validate_upload_window:
            now, valid_until = supplemental_upload_window({"expires_at": expires.isoformat()})
            validate_publication_upload(upload, now=now, valid_until=valid_until)
        save_once(work / (stem + "-submission.json"), submission)
        attempt_id = private_json(attempt)["attempt_id"]
        queued = rest("POST", "/scheduleRun", "--body", "@" + str(body_path), "--headers", "x-docintel-publication-attempt-id=" + attempt_id)
        properties = queued.get("properties", {}) if isinstance(queued, dict) else {}
        run_id = properties.get("runId")
        require(isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id), "ACR submission outcome unknown (no run ID); inspect existing runs, never resubmit")
        save_once(work / (stem + "-queued.json"), {**submission, "runId": run_id, "attempt_id": attempt_id})
        while True:
            observed = rest("GET", "/runs/" + run_id)
            properties = observed.get("properties", {}) if isinstance(observed, dict) else {}
            require(properties.get("runId") == run_id, "ACR run identity mismatch; never resubmit")
            actual_cpu = properties.get("agentConfiguration", {}).get("cpu")
            require(type(actual_cpu) is int and actual_cpu == 2, "ACR actual CPU is not two; stop and inspect the consumed run")
            status = properties.get("status")
            require(status in {"Queued", "Started", "Running", "Succeeded", "Failed", "Canceled", "Error", "Timeout"}, "Unknown ACR run status; never resubmit")
            if status not in {"Queued", "Started", "Running"}:
                result = {name: properties.get(name) for name in ("runId", "status", "startTime", "finishTime", "agentConfiguration", "outputImages")}
                save_once(work / (stem + "-result.json"), result)
                require(status == "Succeeded", "ACR build did not succeed; no automatic retry")
                outputs = properties.get("outputImages", [])
                require(isinstance(outputs, list) and len(outputs) == 1, "ACR output-image evidence missing or unexpected")
                output = outputs[0]
                require(output.get("registry") == config["registry"] + ".azurecr.io" and output.get("repository") == "docintel/" + kind and output.get("tag") == revision and re.fullmatch(r"sha256:[a-f0-9]{64}", output.get("digest", "")), "ACR run output-image binding mismatch")
                digest = azure("acr", "repository", "show", "--subscription", config["subscription"], "--name", config["registry"], "--image", f"docintel/{kind}:{revision}", timeout=remaining())["digest"]
                require(digest == output["digest"], "Published image digest differs from the successful run")
                return {**result, "digest": digest, "tool_revision": tool_revision, "tool_sha256": submission["tool_sha256"]}
            time.sleep(min(5, remaining()))
    finally:
        if archive_created:
            archive.unlink()


def publish_component(config, work, previous_work, kind, *, replacement=None):
    require(kind in {"backend", "frontend"}, "Unsupported publication component")
    require_unused_publication(work, [kind], replacement)
    retained = "frontend" if kind == "backend" else "backend"
    source = verify_source(work)
    previous = private_path(previous_work)
    previous_source = verify_source(previous)
    previous_images = json.loads((previous / "images.json").read_text())
    require(previous_images["revision"] == previous_source["revision"], "Preserved source mismatch")
    require(previous_images[retained] == image(config, retained), "Unchanged component must retain its original published digest")
    protected = lambda name: name.startswith(retained + "/") or name in {"pyproject.toml", "uv.lock"}
    require({name: digest for name, digest in source["files"].items() if protected(name)} == {name: digest for name, digest in previous_source["files"].items() if protected(name)}, "Repair changed preserved source or dependencies")
    checks = json.loads((work / "clean-checks.json").read_text())
    smoke = json.loads((work / (kind + "-smoke.json")).read_text())
    inventory = json.loads((work / "context.json").read_text())
    modes = json.loads((work / "context-modes.json").read_text())
    context = work / "context"
    require(inventory == {str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest() for path in context.rglob("*") if path.is_file()}, "Build context drift")
    require(modes == {str(path.relative_to(context)): path.stat().st_mode & 0o777 for path in context.rglob("*")}, "Build context permission drift")
    require(all(source["files"].get(name) == digest for name, digest in inventory.items()), "Context differs from reviewed source")
    require(checks["revision"] == smoke["revision"] == source["revision"], "Validation source mismatch")
    require(checks["lock_sha256"] == hashlib.sha256(command(["git", "show", source["revision"] + ":uv.lock"], cwd=ROOT)).hexdigest(), "Committed lock mismatch")
    require(smoke["passed"] is True and smoke["context_sha256"] == fingerprint(inventory) and smoke["modes_sha256"] == fingerprint(modes), "Runtime/context validation required")
    require_unused_publication(work, [kind], replacement)
    result = build_publication(config, work, kind, source["revision"], replacement=replacement)
    digest = result["digest"]
    published = {"revision": source["revision"], kind: config["registry"] + ".azurecr.io/docintel/" + kind + "@" + digest, kind + "_run_id": result["runId"], retained: previous_images[retained], "preserved_" + retained: {"work": str(previous), "revision": previous_source["revision"], "receipt_sha256": fingerprint(previous_images)}, "tool_revision": result["tool_revision"], "tool_sha256": result["tool_sha256"]}
    image({**config, kind + "_image": published[kind]}, kind)
    save(work / "images.json", published)


def update_worker_image(config, work):
    baseline = json.loads((work / "worker-baseline.json").read_text())
    require(baseline["target"] == fingerprint(config), "Worker snapshot target mismatch")
    job = azure("containerapp", "job", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"])
    require(job == baseline["resource"], "Worker drift since reviewed snapshot")
    resource_id = f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}/providers/Microsoft.App/jobs/{config['job']}"
    require(job["id"].lower() == resource_id.lower(), "Worker resource target mismatch")
    settings = job["properties"]["configuration"]
    require(settings["triggerType"] == "Manual" and settings["replicaTimeout"] == 600 and settings["replicaRetryLimit"] == 0, "Worker execution bounds changed")
    executions = azure("containerapp", "job", "execution", "list", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"])
    require(not executions, "Worker executions must remain unused before image repair")
    containers = safe_containers(job)
    require(any(entry["name"] == "DOCINTEL_BATCH_LIVE_ENABLED" and entry.get("value") == "false" for entry in containers[0]["env"]), "Worker live AI must remain disabled")
    containers[0]["image"] = image(config, "backend")
    body = work / "worker-image-patch.json"
    require(not body.exists(), "Worker image update already attempted; inspect state before proceeding")
    save(body, build_update_payload(job, containers, job=True))
    azure("rest", "--method", "PATCH", "--url", "https://management.azure.com" + resource_id + "?api-version=2024-03-01", "--body", "@" + str(body))


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


def console_code(arguments, code, marker, timeout=120):
    import pty
    import selectors
    import time
    master, slave = pty.openpty()
    process = subprocess.Popen(arguments, stdin=slave, stdout=slave, stderr=slave, env={**os.environ, "PYTHON_BASIC_REPL": "1"})
    os.close(slave)
    os.set_blocking(master, False)
    output = b""
    sent = False
    offset = 0
    deadline = time.monotonic() + timeout
    encoded = base64.b64encode(code.encode()).decode()
    payload = f"exec(__import__('base64').b64decode('{encoded}'));exit()\n".encode()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(master, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                events = selector.select(max(0, deadline - time.monotonic()))
                if not events:
                    break
                for _, mask in events:
                    if mask & selectors.EVENT_WRITE:
                        try:
                            offset += os.write(master, payload[offset:offset + 1024])
                        except BlockingIOError:
                            pass
                        if offset == len(payload):
                            selector.modify(master, selectors.EVENT_READ)
                    if mask & selectors.EVENT_READ:
                        try:
                            chunk = os.read(master, 65536)
                        except BlockingIOError:
                            continue
                        except OSError:
                            raise ValueError("Remote console did not confirm the expected marker") from None
                        if not chunk:
                            raise ValueError("Remote console did not confirm the expected marker")
                        output = (output + chunk)[-262144:]
                        if not sent and b">>> " in output:
                            selector.modify(master, selectors.EVENT_READ | selectors.EVENT_WRITE)
                            sent = True
                        if marker in output.decode(errors="replace").splitlines():
                            return output
        raise ValueError("Remote console did not confirm the expected marker; inspect state before retrying")
    finally:
        os.close(master)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def seed_via_backend(config, fixture, files, work):
    backend = app(config, config["backend"])
    properties = backend["properties"]
    require(properties["provisioningState"] == "Succeeded" and properties["latestRevisionName"] == properties["latestReadyRevisionName"], "Backend must be ready before private seeding")
    require([container["image"] for container in safe_containers(backend)] == [image(config, "backend")], "Seed backend differs from published release")
    payload = {name.removeprefix("seed/"): {"sha256": digest, "content": base64.b64encode((fixture / name).read_bytes()).decode()} for name, digest in files.items() if name.startswith("seed/")}
    require(sum(len(entry["content"]) for entry in payload.values()) <= 16384, "Synthetic seed exceeds remote execution bound")
    marker = "DOCINTEL_SYNTHETIC_SEED_OK:" + fingerprint(payload)
    output = console_code(["az", "containerapp", "exec", "--subscription", config["subscription"], "-g", config["group"], "-n", config["backend"], "--revision", properties["latestReadyRevisionName"], "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors"], remote_seed_code(config, payload), marker)
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
    if options.action in {"package", "package-backend"}:
        package(work, options.package_access_approved, backend_only=options.action == "package-backend")
        return
    if options.action in (OPERATIONS - {"stop"}) | {"capture"}:
        require_standard_release_work(work)
    config = load_config(options.config)
    if options.action == "plan":
        print("No Azure operation performed. Gates: reviewed private source; consistent committed lock; clean checks; approved digests; Entra scope/consent; secret references; rollback policy.")
        return
    if options.action in OPERATIONS:
        require(options.approve == options.action, "Explicit approval for this operation is required")
    replacement = None
    replacement_path = getattr(options, "publication_replacement_approval", None)
    supplemental = None
    supplemental_path = getattr(options, "publication_supplemental_approval", None)
    require(not (replacement_path is not None and supplemental_path is not None), "Replacement and supplemental approvals are mutually exclusive")
    if supplemental_path is not None:
        require(options.action == "publish", "Supplemental approval is valid only for full publish")
        supplemental = validate_publication_supplemental(private_json(supplemental_path, max_bytes=16384), config, work, verify_source(work)["revision"])
    if replacement_path is not None:
        require(options.action in {"publish", "publish-backend", "publish-frontend"}, "Publication replacement approval is valid only for publication")
        replacement = validate_publication_replacement(private_json(replacement_path, max_bytes=16384), config, work, verify_source(work)["revision"])
    approval = None
    if options.action in {"pilot-start", "pilot-enable"}:
        approval = load_pilot_approval(getattr(options, "pilot_approval", None), options.batch_id)
        approved_operator(config, approval)
    if options.action in {"pilot-upload-enable", "pilot-configure"}:
        upload_path = getattr(options, "upload_approval", None)
        real_path = getattr(options, "pilot_approval", None)
        require(bool(upload_path) != bool(real_path), "Supply exactly one upload-approval or pilot-approval metadata file")
        require(options.action == "pilot-configure" or upload_path, "Upload activation requires upload approval, not processing approval")
        approval = load_upload_approval(upload_path, config) if upload_path else load_pilot_approval(real_path, options.batch_id)
        approved_operator(config, approval)
        bounded_metadata(approval)
    verify_target(config, allow_isolated=options.action == "rollback" and options.isolate_legacy_baseline)
    if options.action in {"provision", "seed", "deploy", "start", "update-worker"}:
        require_release_images(config, work)
    if options.action == "identity-check":
        identity_contract(config)
    elif options.action == "pilot-configure":
        pilot_configure(config, approval, work, "upload" if getattr(options, "upload_approval", None) else "real")
    elif options.action == "pilot-upload-enable":
        pilot_upload_enable(config, approval, work)
    elif options.action == "pilot-upload-disable":
        pilot_upload_disable(config, work)
    elif options.action == "pilot-enable":
        pilot_enable(config, approval, work)
    elif options.action == "pilot-start":
        pilot_start(config, approval, work, options.item_limit)
    elif options.action == "pilot-disable":
        pilot_disable(config, work)
    elif options.action == "publish-frontend":
        publish_component(config, work, options.previous_work, "frontend", replacement=replacement)
    elif options.action == "publish-backend":
        publish_component(config, work, options.previous_work, "backend", replacement=replacement)
    elif options.action == "update-worker":
        update_worker_image(config, work)
    elif options.action == "capture":
        require(not (work / "baseline.json").exists(), "Refusing to replace rollback baseline")
        resources = {kind: app(config, config[kind]) for kind in ("backend", "frontend")}
        baseline = {kind: {"containers": safe_containers(resource), "ingress": copy.deepcopy(resource["properties"]["configuration"].get("ingress"))} for kind, resource in resources.items()}
        for record in baseline.values():
            writable_ingress(record["ingress"])
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
        require_unused_publication(work, ("backend", "frontend"), replacement, supplemental)
        upload_metadata = publication_metadata_preflight(config, work, source["revision"], supplemental) if supplemental is not None else None
        published = {"revision": source["revision"]}
        for kind in ("backend", "frontend"):
            result = build_publication(config, work, kind, source["revision"], replacement=replacement, supplemental=supplemental, upload_metadata=upload_metadata if kind == "backend" else None)
            if kind == "backend":
                upload_metadata = None
            digest = result["digest"]
            published[kind] = config["registry"] + f".azurecr.io/docintel/{kind}@" + digest
            published[kind + "_run_id"] = result["runId"]
            published["tool_revision"] = result["tool_revision"]
            published["tool_sha256"] = result["tool_sha256"]
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
        resources = {kind: app(config, config[kind]) for kind in ("backend", "frontend")}
        snapshot = runtime_baseline(config, work, baseline, resources)
        targets = runtime_targets(config, baseline, snapshot, resources)
        for kind in ("backend", "frontend"):
            desired = targets[kind]["containers"]
            ingress = targets[kind]["ingress"]
            preserve_json(work / (kind + "-runtime-intended.json"), {"target": fingerprint(config), "baseline_sha256": snapshot["baseline_sha256"], **targets[kind]})
            current = app(config, config[kind])
            if writable_containers(safe_containers(current)) != writable_containers(desired) or writable_ingress(current["properties"]["configuration"].get("ingress")) != ingress:
                patch_app(config, kind, desired, work, ingress=ingress)
            resource = app(config, config[kind])
            require(writable_containers(safe_containers(resource)) == writable_containers(desired) and writable_ingress(resource["properties"]["configuration"].get("ingress")) == ingress, f"{kind} runtime/ingress did not converge")
            require(resource["properties"]["provisioningState"] == "Succeeded" and resource["properties"]["latestRevisionName"] == resource["properties"]["latestReadyRevisionName"], f"{kind} revision is not ready; stop and inspect before proceeding")
            revision = azure("containerapp", "revision", "show", "--subscription", config["subscription"], "-g", config["group"], "-n", config[kind], "--revision", resource["properties"]["latestReadyRevisionName"])
            require(revision["properties"]["healthState"] == "Healthy", f"{kind} revision is not healthy; deployment stopped")
            require([container["image"] for container in revision["properties"]["template"]["containers"]] == [container["image"] for container in desired], f"{kind} ready revision does not contain the intended image")
            if kind == "backend":
                require_backend_runtime(safe_containers(resource), ingress)
                require_backend_runtime(safe_containers(revision), ingress)
    elif options.action == "start":
        require(re.fullmatch(r"[a-f0-9]{64}", options.batch_id or ""), "Explicit validated synthetic batch ID required")
        require(options.item_limit in (1, 2), "Acceptance slice must contain at most two items")
        job, running = active_executions(config)
        require(not running, "A worker execution is already active")
        settings = job["properties"]["configuration"]
        require(settings["replicaRetryLimit"] == 0 and settings["replicaTimeout"] == 600, "Unexpected worker retry/timeout configuration")
        template = copy.deepcopy(job["properties"]["template"])
        require(len(template["containers"]) == 1, "Unexpected worker containers")
        container = template["containers"][0]
        require(container["image"] == image(config, "backend"), "Worker image mismatch")
        require(any(entry["name"] == "DOCINTEL_BATCH_LIVE_ENABLED" and entry.get("value") == "false" for entry in container["env"]), "Worker live AI must be disabled")
        container["args"] = ["-m", "backend.batch_worker", "--concurrency", "2", "--max-batches", "1", "--item-limit", str(options.item_limit), "--synthetic-acceptance", "--batch-id", options.batch_id]
        template = writable_execution_template(template)
        save(work / "execution.json", template)
        result = azure("containerapp", "job", "start", "--subscription", config["subscription"], "-g", config["group"], "-n", config["job"], "--yaml", str(work / "execution.json"), resource_snapshot=job)
        save(work / "last-execution.json", result)
    elif options.action == "stop":
        stop(config)
    elif options.action == "rollback":
        baseline = json.loads((work / "baseline.json").read_text())
        require(baseline["target"] == fingerprint(config), "Rollback target mismatch")
        require(baseline["authenticated"] or options.isolate_legacy_baseline, "Baseline has no verified hosted authentication; approve ingress isolation or a secure rollback baseline, never restore it publicly")
        resources = {kind: app(config, config[kind]) for kind in ("backend", "frontend")}
        snapshot = runtime_baseline(config, work, baseline, resources)
        runtime_targets(config, baseline, snapshot, resources, allow_isolated=not baseline["authenticated"])
        stop(config)
        if not baseline["authenticated"]:
            for kind in ("frontend", "backend"):
                azure("containerapp", "ingress", "disable", "--subscription", config["subscription"], "-g", config["group"], "-n", config[kind])
        for kind in ("backend", "frontend"):
            containers = copy.deepcopy(baseline["apps"][kind]["containers"])
            containers[0]["image"] = baseline["apps"][kind]["pinned_image"]
            ingress = snapshot["apps"][kind]["ingress"] if baseline["authenticated"] else None
            patch_app(config, kind, containers, work, ingress=ingress)
            resource = app(config, config[kind])
            require(writable_containers(safe_containers(resource)) == writable_containers(containers) and writable_ingress(resource["properties"]["configuration"].get("ingress")) == writable_ingress(ingress), f"{kind} rollback runtime/ingress did not converge")
        print("Restored baseline images and exact environment/reference settings. Job, identities, grants, container, results and reviews retained. Ingress remains disabled for an unsafe legacy baseline.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "stage", "package", "package-backend", "context", "identity-check", "capture", "publish", "publish-frontend", "publish-backend", "update-worker", "what-if", "provision", "seed", "deploy", "start", "stop", "rollback", "pilot-enable", "pilot-start", "pilot-disable", "pilot-upload-enable", "pilot-upload-disable", "pilot-configure"])
    parser.add_argument("--previous-work", type=Path)
    publication_approval = parser.add_mutually_exclusive_group()
    publication_approval.add_argument("--publication-replacement-approval", type=Path, help="Owner-only, single-use authorization replacing the exact CLI-rejected backend attempt; never resets original records")
    publication_approval.add_argument("--publication-supplemental-approval", type=Path, help="Full publish only: one metadata preflight and, only on validation success, one supplemental backend attempt")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--revision")
    parser.add_argument("--package-access-approved", action="store_true")
    parser.add_argument("--approve", choices=sorted(OPERATIONS))
    parser.add_argument("--batch-id")
    metadata = parser.add_mutually_exclusive_group()
    metadata.add_argument("--pilot-approval", type=Path, help="Owner-only approved real-pilot processing packet")
    metadata.add_argument("--upload-approval", type=Path, help="Owner-only approved source-upload metadata; never document bytes")
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