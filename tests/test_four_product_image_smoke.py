"""Synthetic image-envelope consumer tests, never actual image smoke evidence."""

import copy
from contextlib import nullcontext
import hashlib
import json
import socket

import pytest

from scripts import four_product_continuation as helper


FILES = {
    "uv.lock", "pyproject.toml", "backend/sdk_image_smoke.py", "backend/sdk_preflight.py",
    "backend/batch_worker.py", "backend/core/llm.py", "backend/core/websearch.py",
    "backend/core/websearch_webiq.py", "backend/models/enrichment.py",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("Image admission must not call a provider or network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


def image_case(tmp_path, monkeypatch, model_transport="httpx"):
    monkeypatch.setattr(helper.release, "private_path", lambda value: value)
    decision = {"target": "synthetic-target", "source_revision": "a" * 40}
    digest = "sha256:" + "b" * 64
    image = "synthetic.azurecr.io/docintel/backend@" + digest
    installed = {
        "openai": "1.91.0", "httpx": "0.28.1", "azure-core": "1.34.0",
        "azure-identity": "1.23.0", "azure-ai-documentintelligence": "1.0.2",
        "azure-storage-blob": "12.25.1", "pydantic": "2.11.7",
    }
    if model_transport == "httpx2":
        installed.update(openai="3.3.1", httpx2="2.12.0")
    files = {name: ("REPRODUCTION-ONLY SOURCE " + name).encode() for name in FILES}
    files["uv.lock"] = (
        'version = 1\nrequires-python = ">=3.13"\n'
        + "".join(f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
                  for name, version in installed.items())
    ).encode()
    code = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    common = {
        "status": "validated_no_send", "method": "POST", "content_type": "application/json",
        "authentication_performed": False, "provider_send_performed": False, "provider_response_fabricated": False,
        "endpoint_sha256": "1" * 64, "body_sha256": "2" * 64, "request_sha256": "3" * 64, "body_bytes": 1234,
    }
    model = {
        **common, "provider": "model", "sdk_package": "openai", "sdk_version": installed["openai"],
        "transport_package": model_transport, "transport_version": installed[model_transport],
        "api_version": "2025-01-01-preview",
        "body_fields": ["max_completion_tokens", "messages", "model", "reasoning_effort", "response_format"],
        "body_field_count": 5,
    }
    web = {
        **common, "provider": "webiq", "sdk_package": "httpx", "sdk_version": installed["httpx"],
        "transport_package": "httpx", "transport_version": installed["httpx"], "api_version": None,
        "body_fields": ["contentFormat", "maxLength", "maxResults", "query"], "body_field_count": 4,
        "allowed_domains_sha256": "4" * 64, "allowed_domains_count": 1, "discovery_only_not_evidence": True,
    }
    page = {
        **{key: value for key, value in common.items() if key != "content_type"},
        "provider": "web_retrieval", "sdk_package": "http.client", "sdk_version": "3.13.4",
        "transport_package": "ssl", "transport_version": "OpenSSL 3.1.9 SYNTHETIC-IMAGE",
        "client": "_PinnedHTTPSConnection", "method": "GET", "api_version": None,
        "body_bytes": 0, "body_sha256": hashlib.sha256(b"").hexdigest(),
        "path_sha256": "5" * 64, "wire_request_sha256": "6" * 64, "wire_request_bytes": 200,
        "headers_sha256": "7" * 64, "header_names": ["accept", "accept-encoding", "host", "user-agent"],
        "allowed_hosts_sha256": "8" * 64, "allowed_hosts_count": 1, "timeout_seconds": 15.0,
        "response_max_bytes": 262144, "transport": "in_memory_no_send", "captured_requests": 1,
        **{key: 0 for key in ("transport_real_calls", "network_calls", "dns_calls",
                             "socket_connect_calls", "tls_handshakes", "credential_real_calls")},
        "dns_address_safety_verified": False, "source_content_verified": False,
    }
    proof = {
        "schema_version": 1, "status": "validated_no_send", "source_revision": decision["source_revision"],
        "image_digest": digest, "image_identity_basis": "operator_verified_deployment_and_build_not_self_attested",
        "code_sha256": code, "lock_sha256": code["uv.lock"], "installed_sdk_versions": installed,
        "python_version": "3.13.4", "network": "python_dns_socket_process_and_real_credential_operations_denied",
        "blocked_operation_attempts": {"network": 0, "credential": 0, "subprocess": 0},
        "native_requests": [model, web, page],
        "request_basis": "complete_prepared_planning_inputs_actual_runtime_requests_self_gate",
        "authentication_performed": False, "provider_send_performed": False, "worker_execution_started": False,
    }
    envelope = {
        "schema_version": 1, **helper.binding(decision),
        "observed_api_revision": "synthetic-backend--reviewed-revision",
        "observed_api_image": image, "observed_worker_image": image, "proof": proof,
    }
    helper.release.save_once(helper.path(tmp_path, "published"), {
        **helper.binding(decision), "backend_image": image, "digest": digest,
    })
    helper.release.save_once(helper.path(tmp_path, "image-sdk-smoke"), envelope)
    inspected = []

    def reviewed_file(arguments, **kwargs):
        assert arguments[:2] == ["git", "show"]
        revision, name = arguments[2].split(":", 1)
        assert revision == decision["source_revision"]
        inspected.append(name)
        return files[name]

    monkeypatch.setattr(helper.release, "command", reviewed_file)
    monkeypatch.setattr(helper, "installed_version", lambda *args: pytest.fail("Do not compare image to host packages"))
    monkeypatch.setattr(helper.platform, "python_version", lambda: pytest.fail("Do not compare image to host Python"))
    monkeypatch.setattr(helper, "validate_provider_preflights", lambda *args: pytest.fail("Image is not host evidence"))
    return decision, envelope, files, inspected


@pytest.mark.parametrize("transport", ["httpx", "httpx2"])
def test_image_smoke_uses_its_reviewed_lock_not_operator_versions(tmp_path, monkeypatch, transport):
    decision, expected, _, inspected = image_case(tmp_path, monkeypatch, transport)
    before = copy.deepcopy(expected)
    assert helper.validate_image_smoke(tmp_path, decision) == expected
    assert set(inspected) == FILES
    assert helper.release.private_json(helper.path(tmp_path, "image-sdk-smoke")) == before


@pytest.mark.parametrize("fault", [
    "missing", "decision", "api_revision", "api_image", "worker_image", "source_revision", "image_digest",
    "source_hash", "missing_source", "unsafe_source", "lock_hash", "locked_version", "missing_package",
    "missing_model", "missing_web", "missing_page", "native_version", "image_python",
    "provider_send", "authentication", "worker_execution", "network_namespace_claim",
    "network_attempt", "credential_attempt", "subprocess_attempt", "bool_counter", "page_network",
    "symlink", "permissions", "fabricated_response", "missing_schema", "image_ssl_drift", "raw_request",
])
def test_image_mismatch_blocks_activation_before_any_side_effect(tmp_path, monkeypatch, fault):
    decision, envelope, _, _ = image_case(tmp_path, monkeypatch)
    proof = envelope["proof"]
    if fault == "decision":
        envelope["decision_sha256"] = "0" * 64
    elif fault == "api_revision":
        envelope["observed_api_revision"] = ""
    elif fault in {"api_image", "worker_image"}:
        envelope["observed_" + fault] = envelope["observed_api_image"].replace("b" * 64, "c" * 64)
    elif fault == "source_revision":
        proof["source_revision"] = "c" * 40
    elif fault == "image_digest":
        proof["image_digest"] = "sha256:" + "c" * 64
    elif fault == "source_hash":
        proof["code_sha256"]["backend/core/llm.py"] = "0" * 64
    elif fault == "missing_source":
        proof["code_sha256"].pop("backend/core/llm.py")
    elif fault == "unsafe_source":
        proof["code_sha256"]["backend/../outside.py"] = "0" * 64
    elif fault == "lock_hash":
        proof["lock_sha256"] = "0" * 64
    elif fault == "locked_version":
        proof["installed_sdk_versions"]["openai"] = proof["native_requests"][0]["sdk_version"] = "0.0.0"
    elif fault == "missing_package":
        proof["installed_sdk_versions"].pop("azure-core")
    elif fault in {"missing_model", "missing_web", "missing_page"}:
        proof["native_requests"].pop({"missing_model": 0, "missing_web": 1, "missing_page": 2}[fault])
    elif fault == "native_version":
        proof["native_requests"][0]["sdk_version"] = "0.0.0"
    elif fault == "image_python":
        proof["native_requests"][2]["sdk_version"] = "0.0.0"
    elif fault == "provider_send":
        proof["native_requests"][0]["provider_send_performed"] = True
    elif fault == "authentication":
        proof["authentication_performed"] = True
    elif fault == "worker_execution":
        proof["worker_execution_started"] = True
    elif fault == "network_namespace_claim":
        proof["network"] = "none"
    elif fault in {"network_attempt", "credential_attempt", "subprocess_attempt"}:
        proof["blocked_operation_attempts"][fault.removesuffix("_attempt")] = 1
    elif fault == "bool_counter":
        proof["blocked_operation_attempts"]["network"] = False
    elif fault == "page_network":
        proof["native_requests"][2]["dns_calls"] = 1
    elif fault == "fabricated_response":
        proof["native_requests"][0]["provider_response_fabricated"] = True
    elif fault == "missing_schema":
        proof["native_requests"][0]["body_fields"].remove("response_format")
        proof["native_requests"][0]["body_field_count"] -= 1
    elif fault == "image_ssl_drift":
        second = copy.deepcopy(proof["native_requests"][2])
        second["transport_version"] = "another image SSL runtime"
        proof["native_requests"].append(second)
    elif fault == "raw_request":
        proof["native_requests"][0]["raw_request"] = "REPRODUCTION-ONLY unexpected raw content"
    source = helper.path(tmp_path, "image-sdk-smoke")
    if fault == "missing":
        source.unlink()
    elif fault == "symlink":
        target = tmp_path / "synthetic-symlink-target.json"
        source.rename(target)
        source.symlink_to(target)
    else:
        source.write_text(json.dumps(envelope))
        if fault == "permissions":
            source.chmod(0o644)
    monkeypatch.setattr(helper.release, "pilot_lock", lambda *args: nullcontext())
    monkeypatch.setattr(helper, "validate", lambda *args: ({}, {}))
    effects = []
    monkeypatch.setattr(helper, "window", lambda *args: effects.append("clock"))
    monkeypatch.setattr(helper.release, "save_once", lambda *args: effects.append("write"))
    for name in ("active_target", "configure", "enable", "start", "observe", "close"):
        monkeypatch.setattr(helper, name, lambda *args, name=name: effects.append(name))
    with pytest.raises(ValueError):
        helper.activate(tmp_path, {}, decision)
    assert effects == []
    assert not helper.path(tmp_path, "activation-attempt").exists()


def test_activation_pins_valid_image_receipt_before_any_configuration(tmp_path, monkeypatch):
    decision, envelope, _, _ = image_case(tmp_path, monkeypatch)
    monkeypatch.setattr(helper.release, "pilot_lock", lambda *args: nullcontext())
    monkeypatch.setattr(helper, "validate", lambda *args: ({}, {}))
    monkeypatch.setattr(helper, "window", lambda *args: None)
    monkeypatch.setattr(helper, "live", lambda *args: nullcontext())
    steps = []

    def side_effect(name):
        attempt = helper.receipt(tmp_path, "activation-attempt", decision)
        assert attempt["image_sdk_smoke_sha256"] == helper.sha(envelope)
        steps.append(name)

    for name in ("active_target", "configure", "enable", "start", "observe", "close"):
        monkeypatch.setattr(helper, name, lambda *args, name=name: side_effect(name))
    helper.activate(tmp_path, {}, decision)
    assert steps == ["active_target", "configure", "enable", "start", "observe", "close"]
