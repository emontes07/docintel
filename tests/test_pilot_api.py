import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.pilot_api import app
from tests.test_pilot import service


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setattr("backend.pilot_api.service", lambda: service)
    return TestClient(app, base_url="http://127.0.0.1:8011", client=("127.0.0.1", 12345), headers={"X-DocIntel-Local": "1"})


def test_api_replay_review_export(client):
    payload = {"source_id": "ford-local", "request_id": str(uuid.uuid4())}
    response = client.post("/runs", json=payload)
    assert response.status_code == 202
    run_id = response.json()["id"]
    machine = client.get(f"/runs/{run_id}").json()["machine_result"]
    review = client.post(f"/runs/{run_id}/reviews", json={"attribute_id": "Seal / Softgoods Material", "decision": "approve", "reviewer": "Demo tester", "reason": "Synthetic evidence reviewed", "candidate_index": 0})
    assert review.status_code == 200
    exported = client.get(f"/runs/{run_id}/export")
    assert exported.headers["cache-control"] == "no-store"
    assert exported.json()["machine_result"] == machine
    assert exported.json()["reviewed_result"]["attributes"][1]["review_annotations"]
    assert exported.json()["reviewer_identity"] == "locally_entered_unverified"
    saved = client.post(f"/runs/{run_id}/export")
    assert saved.status_code == 200
    snapshot = Path(saved.json()["location"])
    assert json.loads(snapshot.read_text()) == exported.json()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert client.post("/runs", json=payload).json()["created"] is False


def test_api_rejects_paths_urls_and_implicit_live(client):
    for extra in ({"mode": "live_inference"}, {"path": "/etc/passwd"}, {"url": "https://external.example"}):
        response = client.post("/runs", json={"source_id": "ford-local", "request_id": str(uuid.uuid4()), **extra})
        assert response.status_code == 422
        assert "external.example" not in response.text


def test_api_loopback_and_cross_origin_guards(client):
    assert client.get("/catalog", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/catalog", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/catalog", headers={"X-DocIntel-Local": ""}).status_code == 403
    external = TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.1", 12345), headers={"X-DocIntel-Local": "1"})
    assert external.get("/catalog").status_code == 403


def test_private_artifacts_are_not_static_routes(client):
    for path in ("/static/config.json", "/static/runs.sqlite3", "/config.json", "/.env", "/pdf?path=/etc/passwd", "/download?url=https://example.invalid"):
        assert client.get(path).status_code == 404


def test_restart_preserves_machine_reviews_and_qualifications(service):
    from backend.pilot import PilotService, ReviewInput
    from tests.test_pilot import run

    original = run(service)
    reviewed = service.review(original["id"], ReviewInput(attribute_id="Pressure Rating", decision="correct", corrected_value=41, corrected_unit="PSI", reviewer="Synthetic test only (unverified)", reason="Synthetic restart regression"))
    restarted = PilotService(service.home)
    assert restarted.get(original["id"]) == reviewed
    assert restarted.get(original["id"])["machine_result"] == original["machine_result"]
    assert restarted.catalog()["budget"] == service.catalog()["budget"]