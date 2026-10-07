"""Offline release command tests: Azure and HTTP commands are replaced by shell functions."""

from contextlib import nullcontext
import ast
import base64
from io import BytesIO
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
from types import SimpleNamespace
from urllib.error import HTTPError
import uuid
import zipfile

import pytest

from scripts import console_export, container_smoke


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64
IMAGE = "crc4ryis6hullf4.azurecr.io/docintel/backend@" + DIGEST


@pytest.fixture
def invoke(tmp_path):
    calls = tmp_path / "calls"

    def run(*arguments, failure="", environment=None):
        script = """
az() {
  printf 'az' >> "$CALLS"
  printf ' <%s>' "$@" >> "$CALLS"
  printf '\\n' >> "$CALLS"
  if [[ "$1 $2" == "$FAILURE" ]]; then return 23; fi
  if [[ "$1 $2 $3" == 'acr repository show' ]]; then printf '%s\\n' "$DIGEST"; fi
  if [[ "$1 $2 $3" == 'containerapp job start' ]]; then printf 'caj-quality-execution\\n'; fi
  if [[ "$1 $2 $3" == 'containerapp job logs' ]]; then printf '{"known_run_cost_usd":1.25}\\n'; fi
}
curl() {
  printf 'curl' >> "$CALLS"
  printf ' <%s>' "$@" >> "$CALLS"
  printf '\\n' >> "$CALLS"
  cat > "$CALLS.headers"
  local output
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == '--output' ]]; then output="$2"; break; fi
    shift
  done
  if [[ "$output" != /dev/null && "$output" != - ]]; then printf 'SYNTHETIC-XLSX' > "$output"; fi
  if [[ "$FAILURE" == curl || ( "$FAILURE" == curl-export && "$output" == *.part ) ]]; then return 22; fi
  if [[ "$output" == - ]]; then printf '{"usd":0}\\n'; fi
}
uname() { printf '%s\\n' "${TEST_OS:-Darwin}"; }
sleep() { [[ "$1" == 30 ]]; }
script() {
  printf 'script' >> "$CALLS"
  printf ' <%s>' "$@" >> "$CALLS"
  printf '\\n' >> "$CALLS"
  local transcript="$2"
  if [[ "$2" == -e ]]; then transcript="$5"; fi
  printf 'Connected\\nDOCINTEL_WORKBOOK_BEGIN\\n%s\\nDOCINTEL_WORKBOOK_END\\nClosed\\n' "$WORKBOOK_BASE64" > "$transcript"
  if [[ "$FAILURE" == console ]]; then return 23; fi
}
export -f az curl script uname sleep
exec bash ./run.sh "$@"
"""
        result = subprocess.run(
            ["bash", "-c", script, "release-test", *arguments],
            cwd=ROOT,
            env={**os.environ, "API_BASE_URL": "", "AUTH_COOKIE_FILE": "", "DOCINTEL_ACCESS_TOKEN": "",
                 "EXPORT_MODE": "http", "QUALITY_OWNER": "",
                 "CALLS": str(calls), "DIGEST": DIGEST, "FAILURE": failure, **(environment or {})},
            capture_output=True,
            text=True,
            input="",
            timeout=10,
        )
        return result, calls.read_text().splitlines() if calls.exists() else []

    return run


@pytest.fixture
def exports_directory():
    path = ROOT / ".cache" / "plain-release" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path.relative_to(ROOT)
    finally:
        shutil.rmtree(path)


def synthetic_workbook():
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as workbook:
        workbook.writestr("[Content_Types].xml", "<Types/>")
    return content.getvalue()


def test_ten_line_entrypoint_help_has_no_external_operations(invoke):
    assert len((ROOT / "run.sh").read_text().splitlines()) == 10
    result, calls = invoke("--help")
    assert result.returncode == 0
    assert all(name in result.stdout for name in ("build", "deploy", "start", "export", "cost"))
    assert calls == []


def test_unknown_command_fails_without_operations(invoke):
    result, calls = invoke("approval")
    assert result.returncode == 2 and calls == []


def test_build_uses_actual_backend_context_and_returns_digest(invoke):
    result, calls = invoke("build", "quality-1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == IMAGE
    assert len(calls) == 2
    assert calls[0].startswith("az <acr> <build>")
    assert "<--file> <backend/Dockerfile>" in calls[0]
    assert "<--image> <docintel/backend:quality-1> <.>" in calls[0]
    assert "<--query> <digest>" in calls[1]


def test_failed_build_does_not_resolve_or_deploy_an_older_tag(invoke):
    result, calls = invoke("build", "quality-1", failure="acr build")
    assert result.returncode == 23 and len(calls) == 1


@pytest.mark.parametrize("image", [
    "latest", IMAGE.replace("@", ":"), IMAGE.replace("crc4ryis6hullf4", "other"),
    IMAGE.replace(".azurecr.io", "XazurecrXio"),
])
def test_deploy_requires_our_immutable_backend_image(invoke, image):
    result, calls = invoke("deploy", image)
    assert result.returncode == 2 and calls == []


def test_deploy_updates_api_and_new_worker_to_same_digest_without_starting(invoke):
    result, calls = invoke("deploy", IMAGE)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    assert calls[0].startswith("az <containerapp> <update>")
    assert "<ca-backend-docintel-dev-erik3>" in calls[0]
    assert calls[1].startswith("az <containerapp> <job> <update>")
    assert "<caj-docintel-batch-dev-erik3>" in calls[1]
    assert all(f"<--image> <{IMAGE}>" in call for call in calls)
    assert "<--command> </app/.venv/bin/python> <--args=-mbackend.quality_worker>" in calls[1]
    assert "<--replica-retry-limit> <0>" in calls[1]
    assert all("<start>" not in call and "frontend" not in call for call in calls)


def test_failed_api_update_does_not_update_or_start_worker(invoke):
    result, calls = invoke("deploy", IMAGE, failure="containerapp update")
    assert result.returncode == 23 and len(calls) == 1


def test_start_runs_exactly_one_new_worker_execution(invoke):
    result, calls = invoke("start", "quality-001", environment={
        "QUALITY_BATCH_ID": "batch-001", "QUALITY_OWNER": "tenant/owner",
        "QUALITY_WEB_ENABLED": "false", "QUALITY_FORD_IMAGE_BLOB": "rendered/ford.png", "QUALITY_SMOKE_ONLY": "true",
    })
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "caj-quality-execution"
    assert len(calls) == 1 and calls[0].startswith("az <containerapp> <job> <start>")
    assert "<--container-name> <caj-docintel-batch-dev-erik3>" in calls[0]
    assert "<--env-vars> <QUALITY_RUN_ID=quality-001> <QUALITY_BATCH_ID=batch-001> <QUALITY_OWNER=tenant/owner>" in calls[0]
    assert "<QUALITY_WEB_ENABLED=false> <QUALITY_FORD_IMAGE_BLOB=rendered/ford.png>" in calls[0]
    assert "<QUALITY_SMOKE_ONLY=true>" in calls[0]
    assert "<--query> <name> <--output> <tsv>" in calls[0]


@pytest.mark.parametrize("operation", ["start", "export", "cost"])
def test_invalid_run_id_never_reaches_azure_or_http(invoke, operation):
    args = ("../another-owner", ".cache/exports") if operation == "export" else ("../another-owner",)
    result, calls = invoke(operation, *args)
    assert result.returncode == 2 and calls == []


def test_cost_reads_structured_worker_logs_without_starting_another_execution(invoke):
    result, calls = invoke("cost", "caj-quality-execution")
    assert result.returncode == 0 and result.stdout.strip() == '{"known_run_cost_usd":1.25}'
    assert len(calls) == 1
    assert calls[0].startswith("az <containerapp> <job> <logs> <show>")
    assert "<--container> <caj-docintel-batch-dev-erik3>" in calls[0]
    assert "<--execution> <caj-quality-execution> <--tail> <100>" in calls[0]


def test_cost_can_display_existing_downloaded_meter_without_external_calls(invoke, tmp_path):
    meter = tmp_path / "cost.json"
    meter.write_text('{"known_run_cost_usd":1.25}\n')
    result, calls = invoke("cost", "--file", str(meter))
    assert result.returncode == 0 and result.stdout == meter.read_text()
    assert calls == []


def test_owner_cookie_uses_existing_frontend_proxy_without_token_extraction(invoke, exports_directory, tmp_path):
    batch_id = "b" * 64
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\nportal.example.test\tFALSE\t/\tTRUE\t0\tsession\tSYNTHETIC_COOKIE\n")
    result, calls = invoke("export", batch_id, str(exports_directory), environment={
        "API_BASE_URL": "https://portal.example.test",
        "AUTH_COOKIE_FILE": str(jar),
    })
    assert result.returncode == 0, result.stderr
    assert all(f"<--cookie> <{jar}>" in call for call in calls)
    assert all("SYNTHETIC_COOKIE" not in call and "<--config>" not in call for call in calls)
    assert "SYNTHETIC_COOKIE" not in result.stdout + result.stderr
    assert all(f"<https://portal.example.test/api/batches/{batch_id}" in call for call in calls)
    assert f"/{batch_id}/export>" in calls[1]


def test_missing_cookie_file_fails_without_http_or_falling_back_to_bearer(invoke, exports_directory, tmp_path):
    result, calls = invoke("export", "batch-001", str(exports_directory), environment={
        "API_BASE_URL": "https://portal.example.test",
        "AUTH_COOKIE_FILE": str(tmp_path / "absent.txt"),
        "DOCINTEL_ACCESS_TOKEN": "valid.token",
    })
    assert result.returncode == 2 and calls == []


def test_native_console_command_keeps_identifiers_as_data():
    owner = 'owner"); print("not-code'
    arguments = shlex.split(console_export.export_command("batch-001", owner))
    assert arguments[:2] == ["/app/.venv/bin/python", "-c"]
    tree = ast.parse(arguments[2])
    data = next(statement for statement in tree.body if isinstance(statement, ast.Assign))
    assert [ast.literal_eval(argument) for argument in data.value.args] == ["batch-001", owner]


def test_console_decoder_handles_terminal_controls_and_wrapped_base64(tmp_path):
    workbook = synthetic_workbook()
    encoded = base64.b64encode(workbook).decode()
    transcript, destination = tmp_path / "transcript.log", tmp_path / "export.xlsx"
    transcript.write_text("\x1b[32mDOCINTEL_WORKBOOK_BEGIN\x1b[0m\r\n"
                          + encoded[:40] + "\r\n" + encoded[40:] + "\r\nDOCINTEL_WORKBOOK_END\r\n")
    console_export.decode_export(transcript, destination)
    assert destination.read_bytes() == workbook


@pytest.mark.parametrize("transcript", [
    "Connection failed",
    "DOCINTEL_WORKBOOK_BEGIN\nDOCINTEL_WORKBOOK_END",
    "DOCINTEL_WORKBOOK_BEGIN\ninvalid\nDOCINTEL_WORKBOOK_END",
    "DOCINTEL_WORKBOOK_BEGIN\nbm90IGEgd29ya2Jvb2s=\nDOCINTEL_WORKBOOK_END",
])
def test_console_decoder_never_replaces_export_with_incomplete_output(tmp_path, transcript):
    log, output = tmp_path / "console.log", tmp_path / "export.xlsx"
    log.write_text(transcript)
    output.write_bytes(b"previous")
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        console_export.decode_export(log, output)
    assert output.read_bytes() == b"previous"


@pytest.mark.parametrize("platform", ["Darwin", "Linux"])
@pytest.mark.parametrize("failure", ["", "console"])
def test_console_export_uses_native_exec_and_preserves_private_transcript(invoke, exports_directory, platform, failure):
    workbook = synthetic_workbook()
    output = exports_directory / "batch-001.xlsx"
    output.write_bytes(b"previous")
    result, calls = invoke("export", "batch-001", str(exports_directory), failure=failure, environment={
        "EXPORT_MODE": "console", "QUALITY_OWNER": "tenant/owner", "TEST_OS": platform,
        "WORKBOOK_BASE64": base64.b64encode(workbook).decode(),
    })
    assert len(calls) == 1 and calls[0].startswith("script "), result.stderr
    assert "containerapp" in calls[0] and "exec" in calls[0] and "ca-backend-docintel-dev-erik3" in calls[0]
    assert "--only-show-errors" in calls[0]
    if failure:
        assert result.returncode == 23
        assert output.read_bytes() == b"previous"
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_bytes() == workbook
    assert not list(exports_directory.glob("*.part*"))
    transcripts = list(exports_directory.glob("*.console.*.log"))
    assert len(transcripts) == 1
    assert transcripts[0].stat().st_mode & 0o777 == 0o600
    assert "Connected" in transcripts[0].read_text()


@pytest.mark.parametrize("token,origin", [
    ("", "https://api.example.test"),
    ('bad"token', "https://api.example.test"),
    ("valid.token", "http://api.example.test"),
    ("valid.token", "https://api.example.test/other"),
])
def test_invalid_auth_configuration_stops_before_http(invoke, exports_directory, token, origin):
    result, calls = invoke("export", "batch-001", str(exports_directory), environment={
        "DOCINTEL_API_URL": origin, "DOCINTEL_ACCESS_TOKEN": token,
    })
    assert result.returncode != 0 and calls == []


@pytest.mark.parametrize("failure", ["", "curl", "curl-export"])
def test_export_authenticates_first_and_does_not_replace_export_on_failure(invoke, exports_directory, failure):
    output = exports_directory / "exports"
    output.mkdir()
    workbook = output / "quality-001.xlsx"
    workbook.write_bytes(b"PREVIOUS-EXPORT")
    result, calls = invoke("export", "quality-001", str(output), failure=failure, environment={
        "DOCINTEL_API_URL": "https://api.example.test",
        "DOCINTEL_ACCESS_TOKEN": "synthetic.owner.token",
    })
    assert "<https://api.example.test/api/v1/batches/quality-001>" in calls[0]
    assert all("synthetic.owner.token" not in call for call in calls)
    assert "<--config> <->" in calls[0] and "<--max-redirs> <0>" in calls[0]
    if failure:
        assert result.returncode != 0
        assert workbook.read_bytes() == b"PREVIOUS-EXPORT"
    else:
        assert result.returncode == 0, result.stderr
        assert workbook.read_bytes() == b"SYNTHETIC-XLSX"
        assert "/quality-001/export>" in calls[1]
    assert bool(list(output.glob("*.part"))) == (failure == "curl-export")
    if failure == "curl-export":
        assert (output / "quality-001.xlsx.part").read_bytes() == b"SYNTHETIC-XLSX"
        assert "partial output is retained" in result.stderr


def test_context_exclusions_match_native_azure_directory_tar_names():
    rules = {
        line for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line and not line.startswith("#")
    }
    assert not any(rule.endswith("/") for rule in rules)
    assert {
        ".git", ".venv", ".vscode", ".claude", "output", "tools", "scripts",
        "frontend", ".azure", ".env", "**/.env", "*.xlsx", "**/*.xlsx",
        "*.pdf", "**/*.pdf", "node_modules", "**/node_modules",
    } <= rules


def test_live_commands_and_workflows_do_not_load_archived_operators():
    live = "\n".join(path.read_text() for path in [
        ROOT / "run.sh", ROOT / "scripts/operations.sh", ROOT / "scripts/container_smoke.py",
        ROOT / "scripts/console_export.py",
        *sorted((ROOT / ".github/workflows").glob("*")),
    ])
    for old_entrypoint in (
        "scripts.release", "scripts/release.py", "scripts.azure_write_schema",
        "scripts.four_product_continuation", "scripts.backend_smoke",
        "scripts.frontend_smoke", "scripts.release_fixture", "tools.legacy",
        "azd provision", "azd deploy", "what-if",
    ):
        assert old_entrypoint not in live


def test_old_release_cli_is_not_an_operating_path():
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "scripts/release.py"), "deploy"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2 and "./run.sh" in result.stdout


def test_container_smoke_rejects_root_before_startup(monkeypatch):
    monkeypatch.setattr(container_smoke.os, "getuid", lambda: 0)
    with pytest.raises(RuntimeError, match="non-root"):
        container_smoke.main()


def test_container_smoke_requires_cached_pdf_renderer(monkeypatch):
    monkeypatch.setattr(container_smoke.os, "getuid", lambda: 10001)
    monkeypatch.setattr(container_smoke.os, "getgid", lambda: 10001)
    monkeypatch.setattr(container_smoke.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="pdftoppm"):
        container_smoke.main()


@pytest.mark.parametrize("anonymous_status", [200, 401, 403])
def test_container_smoke_checks_authentication_and_stops_its_server(monkeypatch, anonymous_status):
    monkeypatch.setattr(container_smoke.os, "getuid", lambda: 10001)
    monkeypatch.setattr(container_smoke.os, "getgid", lambda: 10001)
    monkeypatch.setattr(container_smoke.shutil, "which", lambda _: "/usr/bin/pdftoppm")
    stopped = []
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append(True), wait=lambda **_: 0)
    monkeypatch.setattr(container_smoke.subprocess, "Popen", lambda *_, **__: process)

    def open_url(url, **_):
        assert url.startswith("http://127.0.0.1:80/")
        if url.endswith("/batches") and anonymous_status != 200:
            raise HTTPError(url, anonymous_status, "mock response", None, None)
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(container_smoke.urllib.request, "urlopen", open_url)
    if anonymous_status == 401:
        container_smoke.main()
    else:
        with pytest.raises(RuntimeError, match="Anonymous"):
            container_smoke.main()
    assert stopped == [True]
