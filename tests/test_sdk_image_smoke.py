"""Synthetic image bindings; real installed native SDKs and request builders."""

import hashlib
from importlib.metadata import version
import json
import socket
import ssl
import subprocess

from azure.identity import AzureCliCredential
import pytest

from backend import sdk_image_smoke as smoke
from backend.batch_worker import prepare_inference_request
from backend.core.llm import preflight_structured_request
from backend.core.websearch import preflight_original_page
from backend.core.websearch_webiq import WebIQSearchClient
from backend.models.enrichment import ExtractionResponse


PRIVATE = "PRIVATE-EVIDENCE-NOT-FOR-RECEIPTS"


@pytest.fixture
def binding(tmp_path, monkeypatch):
    # This is explicitly a synthetic filesystem, not target-image evidence.
    for name in smoke.REQUIRED_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic source binding\n")
    (tmp_path / "uv.lock").write_text("\n".join(
        f'[[package]]\nname = "{package}"\nversion = "{version(package)}"\n'
        for package in smoke._provider_packages()
    ))
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    return {
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "expected_files": {
            name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
            for name in smoke.REQUIRED_FILES
        },
    }


def captures():
    prepared = prepare_inference_request(
        PRIVATE, json.dumps({"evidence": [], "attributes": []}),
        ExtractionResponse, deployment="gpt-5",
    )
    return [
        preflight_structured_request(
            prepared.system, prepared.user, ExtractionResponse,
            endpoint="https://native-image.example", deployment="gpt-5",
            sdk_max_retries=0, **prepared.request_parameters,
        ),
        WebIQSearchClient(
            endpoint="https://api.microsoft.ai/v3/search/web",
            api_key="SYNTHETIC-NO-SEND-KEY",
        ).preflight_search(
            "Public Manufacturer MPN size", allowed_domains=["manufacturer.example"], authorized=True,
        ),
        preflight_original_page(
            "https://manufacturer.example/catalog",
            allowed_hosts=["manufacturer.example"], authorized=True,
        ),
    ]


def test_native_capture_binds_source_lock_versions_without_credentials_or_network(binding):
    original = socket.socket.connect
    result = smoke.collect_image_smoke(**binding, capture_requests=captures)
    assert result["status"] == "validated_no_send"
    assert result["code_sha256"] == binding["expected_files"]
    assert result["lock_sha256"] == binding["expected_files"]["uv.lock"]
    assert result["installed_sdk_versions"]["openai"] == version("openai")
    assert result["blocked_operation_attempts"] == {"network": 0, "credential": 0, "subprocess": 0}
    assert result["worker_execution_started"] is False
    assert socket.socket.connect is original
    serialized = json.dumps(result)
    assert PRIVATE not in serialized
    assert "SYNTHETIC-NO-SEND-KEY" not in serialized
    assert "https://" not in serialized


@pytest.mark.parametrize("operation", [
    lambda: socket.getaddrinfo("forbidden.invalid", 443),
    lambda: socket.create_connection(("forbidden.invalid", 443)),
    lambda: ssl.SSLSocket.write(None, b"forbidden"),
    lambda: subprocess.Popen(["forbidden-command"]),
    lambda: AzureCliCredential().get_token("https://cognitiveservices.azure.com/.default"),
])
def test_forbidden_operation_blocks_even_if_callback_catches_error(binding, operation):
    def attempted():
        with pytest.raises(smoke.ImageSmokeError):
            operation()
        return captures()

    with pytest.raises(smoke.ImageSmokeError, match="forbidden operation was attempted"):
        smoke.collect_image_smoke(**binding, capture_requests=attempted)


@pytest.mark.parametrize("field,value", [
    ("source_revision", "unreviewed"),
    ("image_digest", "registry/backend:mutable"),
])
def test_invalid_bindings_fail_before_capture(binding, field, value):
    binding[field] = value
    with pytest.raises(smoke.ImageSmokeError):
        smoke.collect_image_smoke(**binding, capture_requests=lambda: pytest.fail("must not capture"))


def test_source_mismatch_blocks_before_capture(binding):
    binding["expected_files"]["backend/batch_worker.py"] = "0" * 64
    with pytest.raises(smoke.ImageSmokeError, match="differs from reviewed source"):
        smoke.collect_image_smoke(**binding, capture_requests=lambda: pytest.fail("must not capture"))


@pytest.mark.parametrize("name", ["../uv.lock", "/uv.lock", "scripts/release.py"])
def test_invalid_manifest_path_blocks_before_capture(binding, name):
    binding["expected_files"][name] = "0" * 64
    with pytest.raises(smoke.ImageSmokeError, match="Invalid image source manifest path"):
        smoke.collect_image_smoke(**binding, capture_requests=lambda: pytest.fail("must not capture"))


def test_incomplete_source_manifest_blocks_before_capture(binding):
    del binding["expected_files"]["backend/batch_worker.py"]
    with pytest.raises(smoke.ImageSmokeError, match="Incomplete reviewed image source manifest"):
        smoke.collect_image_smoke(**binding, capture_requests=lambda: pytest.fail("must not capture"))


def test_lock_mismatch_is_not_replaced_with_operator_versions(binding):
    path = smoke.ROOT / "uv.lock"
    path.write_text(path.read_text().replace(f'version = "{version("openai")}"', 'version = "0.0.0"', 1))
    binding["expected_files"]["uv.lock"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(smoke.ImageSmokeError, match="differs from the image dependency lock"):
        smoke.collect_image_smoke(**binding, capture_requests=lambda: pytest.fail("must not capture"))


def test_ambiguous_lock_package_blocks(binding):
    path = smoke.ROOT / "uv.lock"
    path.write_text(path.read_text() + '\n[[package]]\nname = "openai"\nversion = "0.0.0"\n')
    binding["expected_files"]["uv.lock"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(smoke.ImageSmokeError, match="unambiguous locked version"):
        smoke.collect_image_smoke(**binding, capture_requests=lambda: pytest.fail("must not capture"))


@pytest.mark.parametrize("change", ["empty", "missing_provider", "fabricated", "wrong_version"])
def test_incomplete_or_misrepresented_proof_is_rejected(binding, change):
    def changed():
        result = captures()
        if change == "empty":
            return []
        if change == "missing_provider":
            return result[:1]
        result[0]["provider_response_fabricated" if change == "fabricated" else "sdk_version"] = (
            True if change == "fabricated" else "not-the-installed-version"
        )
        return result

    with pytest.raises(smoke.ImageSmokeError):
        smoke.collect_image_smoke(**binding, capture_requests=changed)


def test_source_change_during_capture_is_rejected(binding):
    def changed():
        result = captures()
        (smoke.ROOT / "backend/batch_worker.py").write_text("changed")
        return result

    with pytest.raises(smoke.ImageSmokeError, match="differs from reviewed source"):
        smoke.collect_image_smoke(**binding, capture_requests=changed)
