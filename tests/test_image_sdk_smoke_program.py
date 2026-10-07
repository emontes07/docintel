import base64
import contextlib
import copy
import io
import json

import pytest

from scripts import image_sdk_smoke as image
from scripts import four_product_continuation as four, release
from tests.test_sdk_image_smoke import binding
from backend.batch_worker import prepare_inference_request
from backend.models.enrichment import ExtractionResponse


def measurement():
    request = prepare_inference_request(
        "PRIVATE-COMPLETE-INPUT", json.dumps({"evidence": [], "attributes": []}),
        ExtractionResponse, deployment="gpt-5",
    )
    return {
        "model_endpoint": "https://native-image.example", "model_deployment": "gpt-5",
        "prepared_requests": [
            {"system": request.system, "user": request.user, "request_parameters": request.request_parameters}
            for _ in range(12)
        ],
        "search_requests": [
            {"query": "Manufacturer MPN material", "allowed_hosts": ["manufacturer.example"]}
            for _ in range(4)
        ],
        "page_requests": [
            {"url": "https://manufacturer.example/catalog", "allowed_hosts": ["manufacturer.example"]}
            for _ in range(6)
        ],
    }


def test_console_program_captures_all_full_requests_without_printing_private_input(binding):
    code = image.image_program(**binding, measurement=measurement())
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(code, "<synthetic-image-smoke>", "exec"), {})
    lines = output.getvalue().splitlines()
    assert lines[-1] == image.MARKER and len(lines) == 2
    proof = json.loads(base64.b64decode(lines[0][len(image.RESULT):], validate=True))
    assert len(proof["native_requests"]) == 22
    assert sum(entry["provider"] == "model" for entry in proof["native_requests"]) == 12
    assert sum(entry["provider"] == "webiq" for entry in proof["native_requests"]) == 4
    assert sum(entry["provider"] == "web_retrieval" for entry in proof["native_requests"]) == 6
    assert proof["blocked_operation_attempts"] == {"network": 0, "credential": 0, "subprocess": 0}
    assert "PRIVATE-COMPLETE-INPUT" not in json.dumps(proof)
    assert "https://" not in json.dumps(proof)


@pytest.mark.parametrize("field", ["prepared_requests", "search_requests", "page_requests"])
def test_incomplete_measurement_is_not_smoked(binding, field):
    inputs = measurement()
    inputs[field].pop()
    with pytest.raises(ValueError, match="All complete measured requests"):
        image.image_program(**binding, measurement=inputs)


@pytest.mark.parametrize("drift", [None, "active", "worker_image", "api_unready", "processing"])
def test_operator_bridge_only_inspects_ready_closed_matching_images(tmp_path, monkeypatch, binding, drift):
    digest = "registry.example/backend@" + binding["image_digest"]
    decision = {"source_revision": binding["source_revision"], "target": "synthetic"}
    config = {"subscription": "synthetic", "group": "synthetic", "backend": "api", "job": "worker"}
    api = {"properties": {
        "provisioningState": "Succeeded", "latestRevisionName": "api--1", "latestReadyRevisionName": "api--1",
        "template": {"containers": [{"image": digest, "env": []}]},
    }}
    job = copy.deepcopy(api)
    if drift == "worker_image":
        job["properties"]["template"]["containers"][0]["image"] = "registry.example/backend:mutable"
    if drift == "api_unready":
        api["properties"]["latestReadyRevisionName"] = "api--0"
    if drift == "processing":
        api["properties"]["template"]["containers"][0]["env"] = [{"name": "DOCINTEL_REAL_PILOT_ENABLED", "value": "true"}]
    calls = []
    monkeypatch.setattr(four, "receipt", lambda *args: {"backend_image": digest})
    monkeypatch.setattr(release, "app", lambda *args: api)
    monkeypatch.setattr(release, "active_executions", lambda *args: (job, ["active"] if drift == "active" else []))
    monkeypatch.setattr(image, "reviewed_files", lambda revision: binding["expected_files"])

    def console(arguments, code, marker, timeout):
        calls.append(arguments)
        assert arguments[:3] == ["az", "containerapp", "exec"] and "start" not in arguments
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(compile(code, "<synthetic-image-console>", "exec"), {})
        return output.getvalue().encode()

    monkeypatch.setattr(release, "console_code", console)
    validated = []
    monkeypatch.setattr(four, "validate_image_smoke", lambda *args: validated.append(args))
    if drift is not None:
        with pytest.raises(ValueError):
            image.capture(tmp_path, config, decision, measurement())
        assert not calls and not validated and not list(tmp_path.glob("*.json"))
    else:
        envelope = image.capture(tmp_path, config, decision, measurement())
        assert len(calls) == len(validated) == 1
        assert envelope["observed_api_image"] == envelope["observed_worker_image"] == digest
        assert envelope["proof"]["worker_execution_started"] is False
        assert len(envelope["proof"]["native_requests"]) == 22
        assert (tmp_path / "four-product-image-sdk-smoke.json").stat().st_mode & 0o077 == 0
