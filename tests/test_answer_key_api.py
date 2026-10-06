from fastapi.testclient import TestClient

from backend.batch import BatchService
from backend.batch_api import development_app
from backend.batch_auth import actor
from tests.test_answer_key import OWNER, no_network, public_sheets, public_workbook, seed, synthetic_baseline


def test_api_template_ingest_score_delta_and_owner_isolation(monkeypatch):
    service, batch_id = seed()
    monkeypatch.setattr("backend.batch_api.service", lambda: BatchService(service.store))
    development_app.dependency_overrides[actor] = lambda: OWNER
    try:
        with TestClient(development_app) as client:
            prefix = "/api/v1/batches"
            snapshot = client.post(f"{prefix}/{batch_id}/scoring/snapshots", json={})
            assert snapshot.status_code == 200, snapshot.text
            snapshot_id = snapshot.json()["id"]
            base = f"{prefix}/scoring/snapshots/{snapshot_id}"
            assert client.get(base).status_code == 200
            baseline = synthetic_baseline(service, snapshot_id)
            assert client.get(base + "/template").status_code == 422
            assert client.post(base + "/reviewer-packages", files={"baseline": ("synthetic.xlsx", baseline)}).status_code == 422
            package = client.post(base + "/reviewer-packages", data={
                "confirm_trusted_baseline": "true",
                "candidate_bindings": '[{"product_id":"002","attribute_id":"Pressure","candidate_index":0}]',
            }, files={"baseline": ("synthetic.xlsx", baseline)})
            assert package.status_code == 200, package.text
            assert client.get(base + "/template").headers["cache-control"] == "no-store"
            assert client.get(base + "/score").json()["overall"]["accuracy"] is None
            content = public_workbook(public_sheets(service, snapshot_id))
            answer = client.post(base + "/answer-keys", files={"workbook": ("synthetic.xlsx", content)})
            assert answer.status_code == 200, answer.text
            version_id = answer.json()["id"]
            metrics = client.get(base + "/score", params={"answer_key_id": version_id})
            assert metrics.status_code == 200 and metrics.json()["overall"]["accuracy"] == 15 / 17
            delta = client.get(base + "/delta", params={"previous_snapshot_id": snapshot_id, "answer_key_id": version_id})
            assert delta.status_code == 200 and delta.json()["counts"]["unchanged"] == 24
            assert len(client.get(prefix + "/scoring/records").json()["versions"]) == 1
            sheets = public_sheets(service, snapshot_id)
            rows = sheets["Review"]
            invalid_row = next(index for index, row in enumerate(rows, 2) if row["Decision"])
            rows[invalid_row - 2]["Reason"] = ""
            invalid = client.post(base + "/answer-keys", files={"workbook": ("synthetic.xlsx", public_workbook(sheets))})
            assert invalid.status_code == 422 and f"Review row {invalid_row}" in invalid.json()["detail"]
            development_app.dependency_overrides[actor] = lambda: "other"
            for path in (base, base + "/score", base + "/template", prefix + "/scoring/answer-keys/" + version_id):
                assert client.get(path).status_code == 404
            assert client.post(base + "/answer-keys", files={"workbook": ("synthetic.xlsx", content)}).status_code == 404
            assert client.get(prefix + "/scoring/records").json() == {"snapshots": [], "versions": [], "packages": []}
    finally:
        development_app.dependency_overrides.pop(actor, None)


def test_api_requires_existing_authentication(monkeypatch):
    monkeypatch.setenv("DOCINTEL_BATCH_MODE", "development")
    with TestClient(development_app, client=("127.0.0.1", 9999)) as client:
        assert client.get("/api/v1/batches/scoring/records").status_code == 403
        assert client.post("/api/v1/batches/synthetic/scoring/snapshots", json={}).status_code == 403
