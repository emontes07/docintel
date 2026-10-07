"""Capture provider compatibility in the deployed API image without starting a job."""

import base64
import hashlib
import json
import lzma
from pathlib import Path

from scripts import release


MARKER = "DOCINTEL_NATIVE_IMAGE_SMOKE_OK"
RESULT = "DOCINTEL_NATIVE_IMAGE_SMOKE_RESULT:"


def image_program(*, source_revision, image_digest, expected_files, measurement):
    """Pure program construction; inputs remain private and only hashes return."""
    payload = {
        "source_revision": source_revision, "image_digest": image_digest,
        "expected_files": expected_files,
        "endpoint": measurement["model_endpoint"], "deployment": measurement["model_deployment"],
        "model": [
            {key: entry[key] for key in ("system", "user", "request_parameters")}
            for entry in measurement["prepared_requests"]
        ],
        "search": [
            {key: entry[key] for key in ("query", "allowed_hosts")}
            for entry in measurement["search_requests"]
        ],
        "page": [
            {key: entry[key] for key in ("url", "allowed_hosts")}
            for entry in measurement["page_requests"]
        ],
    }
    release.require(
        len(payload["model"]) == 12 and len(payload["search"]) == 4 and len(payload["page"]) == 6,
        "All complete measured requests must be present for image inspection",
    )
    encoded = base64.b64encode(lzma.compress(json.dumps(payload, separators=(",", ":")).encode())).decode()
    return f'''import base64, json, lzma
from backend.sdk_image_smoke import collect_image_smoke
from backend.core.llm import preflight_structured_request
from backend.core.websearch_webiq import WebIQSearchClient
from backend.core.websearch import preflight_original_page
from backend.models.enrichment import ExtractionResponse
payload = json.loads(lzma.decompress(base64.b64decode({encoded!r}, validate=True)))
def capture_requests():
    receipts = [
        preflight_structured_request(
            entry["system"], entry["user"], ExtractionResponse,
            endpoint=payload["endpoint"], deployment=payload["deployment"],
            sdk_max_retries=0, **entry["request_parameters"],
        ) for entry in payload["model"]
    ]
    client = WebIQSearchClient(
        endpoint="https://api.microsoft.ai/v3/search/web", api_key="SYNTHETIC-IMAGE-NO-SEND-KEY",
    )
    receipts.extend(
        client.preflight_search(entry["query"], allowed_domains=entry["allowed_hosts"], authorized=True)
        for entry in payload["search"]
    )
    receipts.extend(
        preflight_original_page(entry["url"], allowed_hosts=entry["allowed_hosts"], authorized=True)
        for entry in payload["page"]
    )
    return receipts
proof = collect_image_smoke(
    source_revision=payload["source_revision"], image_digest=payload["image_digest"],
    expected_files=payload["expected_files"], capture_requests=capture_requests,
)
print({RESULT!r} + base64.b64encode(json.dumps(proof).encode()).decode())
print({MARKER!r})
'''


def reviewed_files(revision):
    names = release.command([
        "git", "ls-tree", "-r", "--name-only", revision, "--", "backend", "uv.lock", "pyproject.toml",
    ], cwd=release.ROOT).decode().splitlines()
    return {
        name: hashlib.sha256(release.command(["git", "show", revision + ":" + name], cwd=release.ROOT)).hexdigest()
        for name in names
    }


def capture(work: Path, config: dict, decision: dict, measurement: dict) -> dict:
    """Explicit read-only API console inspection after deployment, before activation."""
    from scripts import four_product_continuation as four

    destination = four.path(work, "image-sdk-smoke")
    release.require(not destination.exists(), "Image proof already exists; do not overwrite it")
    published = four.receipt(work, "published", decision)
    expected = published["backend_image"]
    release.require("@sha256:" in expected, "Published immutable backend digest required")
    backend = release.app(config, config["backend"])
    worker, active = release.active_executions(config)
    release.require(not active, "Image smoke cannot run alongside an active worker")
    release.require_real_pilot_off(backend)
    release.require_real_pilot_off(worker)
    properties = backend["properties"]
    release.require(
        properties["provisioningState"] == "Succeeded"
        and properties["latestRevisionName"] == properties["latestReadyRevisionName"]
        and [entry["image"] for entry in release.safe_containers(backend)] == [expected]
        and [entry["image"] for entry in release.safe_containers(worker)] == [expected],
        "Both deployed resources must be ready and reference the published digest",
    )
    code = image_program(
        source_revision=decision["source_revision"], image_digest=expected.partition("@")[2],
        expected_files=reviewed_files(decision["source_revision"]), measurement=measurement,
    )
    release.save_once(four.path(work, "image-sdk-smoke-attempt"), {
        **four.binding(decision), "observed_api_revision": properties["latestReadyRevisionName"],
        "backend_image": expected, "program_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "measurement_sha256": four.sha(measurement), "purpose": "no_send_image_inspection_not_worker_execution",
    })
    output = release.console_code([
        "az", "containerapp", "exec", "--subscription", config["subscription"],
        "-g", config["group"], "-n", config["backend"],
        "--revision", properties["latestReadyRevisionName"],
        "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors",
    ], code, MARKER, timeout=120)
    lines = [line[len(RESULT):] for line in output.decode().splitlines() if line.startswith(RESULT)]
    release.require(len(lines) == 1, "Image native capture did not return exactly one proof")
    proof = json.loads(base64.b64decode(lines[0], validate=True))
    envelope = {
        "schema_version": 1, **four.binding(decision),
        "observed_api_revision": properties["latestReadyRevisionName"],
        "observed_api_image": expected, "observed_worker_image": expected, "proof": proof,
    }
    release.save_once(destination, envelope)
    four.validate_image_smoke(work, decision)
    return envelope
