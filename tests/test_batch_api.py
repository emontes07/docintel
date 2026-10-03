from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.batch_api import development_app
from backend.batch_auth import verify_token
from tests.test_batch import service, workbooks
from tests.test_pilot_upload import OWNER, PDF, no_network, pilot


def test_routes_are_authenticated_and_do_not_accept_reviewer_spoofing(service, monkeypatch):
    monkeypatch.setenv("DOCINTEL_BATCH_MODE", "development")
    monkeypatch.setattr("backend.batch_api.service", lambda: service)
    client = TestClient(development_app, client=("127.0.0.1", 9999))
    assert client.get("/api/v1/batches").status_code == 403
    assert client.post("/api/v1/batches/validate", headers={"content-length": str(22 * 1024 * 1024)}).status_code == 413
    manifest, attributes = workbooks()
    headers = {"x-docintel-development": "1"}
    result = client.post("/api/v1/batches/validate", headers=headers, data={"attribute_reference": "definitions.xlsx"}, files={"manifest": ("manifest.xlsx", manifest), "attributes": ("attributes.xlsx", attributes)})
    assert result.status_code == 200
    batch_id = result.json()["id"]
    assert client.post(f"/api/v1/batches/{batch_id}/items/row-2/reviews", headers=headers, json={"attribute_id": "Pressure Rating", "decision": "reject", "reason": "test", "reviewer": "spoofed"}).status_code == 422
    monkeypatch.setenv("DOCINTEL_BATCH_MODE", "hosted")
    assert client.get("/api/v1/batches", headers=headers).status_code == 503


def test_entra_signature_audience_tenant_client_scope_and_expiry():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tenant = "11111111-1111-4111-8111-111111111111"
    client = "22222222-2222-4222-8222-222222222222"
    claims = {"tid": tenant, "oid": "33333333-3333-4333-8333-333333333333", "azp": client, "scp": "Batch.Access", "aud": "batch-api", "iss": f"https://login.microsoftonline.com/{tenant}/v2.0", "iat": datetime.now(timezone.utc), "nbf": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    token = jwt.encode(claims, key, algorithm="RS256")
    assert verify_token(token, key.public_key(), tenant, "batch-api", client).startswith(tenant)
    for changes in [{"aud": "other"}, {"iss": "https://other.invalid"}, {"tid": client}, {"azp": tenant}, {"scp": "User.Read"}, {"scp": ["Batch.Access"]}, {"exp": 1}]:
        with pytest.raises((jwt.PyJWTError, ValueError)):
            verify_token(jwt.encode({**claims, **changes}, key, algorithm="RS256"), key.public_key(), tenant, "batch-api", client)


def test_hosted_legacy_routes_and_local_header_are_closed(monkeypatch):
    from fastapi import FastAPI
    from backend.batch_auth import restrict_hosted_routes
    app = FastAPI()
    app.middleware("http")(restrict_hosted_routes)
    for path in ["/api/v1/metadata", "/api/v1/gallery", "/static/private.png", "/api/v1/env"]:
        app.add_api_route(path, lambda: {"private": True})
    monkeypatch.setenv("DOCINTEL_BATCH_MODE", "development")
    monkeypatch.setenv("CONTAINER_APP_NAME", "hosted-test")
    client = TestClient(app, client=("127.0.0.1", 9999))
    for path in ["/api/v1/metadata", "/api/v1/gallery", "/static/private.png", "/api/v1/env", "/docs"]:
        assert client.get(path, headers={"x-docintel-development": "1"}).status_code == 404
    assert TestClient(development_app, client=("127.0.0.1", 9999)).get("/api/v1/batches", headers={"x-docintel-development": "1"}).status_code == 503


@pytest.fixture
def upload_api(pilot, monkeypatch):
    from types import SimpleNamespace
    from backend.batch import BatchService

    tenant, object_id = OWNER.split("/")
    frontend = "99999999-9999-4999-8999-999999999999"
    for name, value in {"DOCINTEL_BATCH_MODE": "hosted", "DOCINTEL_AUTH_TENANT_ID": tenant,
                        "DOCINTEL_AUTH_AUDIENCE": "batch-api", "DOCINTEL_AUTH_CLIENT_ID": frontend,
                        "DOCINTEL_REAL_PILOT_ENABLED": "false"}.items():
        monkeypatch.setenv(name, value)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {"tid": tenant, "oid": object_id, "azp": frontend, "scp": "Batch.Access",
              "aud": "batch-api", "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
              "iat": datetime.now(timezone.utc), "nbf": datetime.now(timezone.utc),
              "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    monkeypatch.setattr("backend.batch_auth.keys", lambda _: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())))
    monkeypatch.setattr("backend.batch_api.service", lambda: BatchService(pilot.store))
    headers = {"authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256")}
    other = {"authorization": "Bearer " + jwt.encode({**claims, "oid": frontend}, key, algorithm="RS256")}
    return TestClient(development_app), headers, other, pilot


def test_approved_upload_routes_require_exact_owner_and_filename(upload_api, monkeypatch):
    client, headers, other, pilot = upload_api
    base = "/api/v1/batches/pilot-sources"
    files = {"document": ("manual.pdf", PDF)}
    assert client.post(base + "/manual", files=files).status_code == 401
    assert client.get("/api/v1/batches/catalog", headers=other).json()["source_upload"] is None
    assert client.post(base + "/manual", headers=other, files=files).status_code == 404
    assert client.post(base + "/finalize", headers=other, json={}).status_code == 404
    assert client.post(base + "/unapproved", headers=headers, files=files).status_code == 404
    assert client.post(base + "/manual", headers=headers, files={"document": ("different.pdf", PDF)}).status_code == 422
    assert client.post(base + "/manual", headers=headers, files={"document": ("manual.pdf", PDF + b"changed")}).status_code == 422
    assert client.post(base + "/manual", headers=headers, files=files).status_code == 200
    assert client.post(base + "/manual", headers=headers, files=files).status_code == 200
    monkeypatch.setenv("DOCINTEL_PILOT_UPLOAD_ENABLED", "false")
    assert client.get("/api/v1/batches/catalog", headers=headers).json()["source_upload"] is None
    assert client.post(base + "/manual", headers=headers, files=files).status_code == 404


def test_source_finalize_route_verifies_all_documents_and_preserves_registry(upload_api, monkeypatch):
    from tests.test_pilot_upload import XLSX

    client, headers, other, pilot = upload_api
    base = "/api/v1/batches/pilot-sources"
    catalog = client.get("/api/v1/batches/catalog", headers=headers).json()
    assert client.post(base + "/finalize", headers=headers, json={}).status_code == 409
    for document in catalog["source_upload"]:
        content = XLSX if document["filename"].endswith(".xlsx") else PDF
        response = client.post(base + "/" + document["source_id"], headers=headers, files={"document": (document["filename"], content)})
        assert response.status_code == 200, response.json()
    finalized = client.post(base + "/finalize", headers=headers, json={})
    assert finalized.status_code == 200 and finalized.json()["status"] == "ready"
    assert client.post(base + "/finalize", headers=headers, json={}).json() == finalized.json()
    updated = client.get("/api/v1/batches/catalog", headers=headers).json()["sources"]
    assert {source["source_id"] for source in updated} >= {source["source_id"] for source in catalog["sources"]}
    private_ids = {document["source_id"] for document in catalog["source_upload"]}
    assert private_ids <= {source["source_id"] for source in updated}
    monkeypatch.setenv("DOCINTEL_PILOT_UPLOAD_ENABLED", "false")
    other_sources = client.get("/api/v1/batches/catalog", headers=other).json()["sources"]
    assert not private_ids & {source["source_id"] for source in other_sources}


def test_intake_cannot_bind_another_owners_private_sources(service):
    from backend.batch_store import read_json, write_json

    registry, etag = read_json(service.store, "configuration/sources.json")
    for source in registry["sources"]:
        source["owner"] = OWNER
    write_json(service.store, "configuration/sources.json", registry, version=etag)
    manifest, attributes = workbooks()
    assert service.intake(manifest, attributes, "definitions.xlsx", OWNER)["valid"]
    other = OWNER.split("/")[0] + "/99999999-9999-4999-8999-999999999999"
    denied = service.intake(manifest, attributes, "definitions.xlsx", other)
    assert not denied["valid"]
    assert all(not item["sources"] for item in denied["items"])


@pytest.mark.parametrize("blob_reads", [False, True])
def test_hosted_synthetic_continuation_review_and_export(tmp_path, monkeypatch, blob_reads):
    import socket
    from types import SimpleNamespace
    from backend.batch import BatchService
    from backend.batch_store import BlobStore, SQLiteStore
    from backend.batch_worker import run_batch
    from scripts.release_fixture import prepare, verify_export

    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("Network forbidden"))
    monkeypatch.setenv("DOCINTEL_BATCH_MODE", "hosted")
    monkeypatch.setenv("DOCINTEL_BATCH_LIVE_ENABLED", "false")
    tenant = "11111111-1111-4111-8111-111111111111"
    frontend = "22222222-2222-4222-8222-222222222222"
    object_id = "33333333-3333-4333-8333-333333333333"
    owner = f"{tenant}/{object_id}"
    for name, value in {"DOCINTEL_AUTH_TENANT_ID": tenant, "DOCINTEL_AUTH_AUDIENCE": "batch-api", "DOCINTEL_AUTH_CLIENT_ID": frontend}.items():
        monkeypatch.setenv(name, value)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {"tid": tenant, "oid": object_id, "azp": frontend, "scp": "Batch.Access", "aud": "batch-api", "iss": f"https://login.microsoftonline.com/{tenant}/v2.0", "iat": datetime.now(timezone.utc), "nbf": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    monkeypatch.setattr("backend.batch_auth.keys", lambda _: SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=key.public_key())))
    headers = {"authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256")}
    other = {"authorization": "Bearer " + jwt.encode({**claims, "oid": frontend}, key, algorithm="RS256")}
    fixture = tmp_path / "fixture"
    prepare(fixture)
    def open_store():
        backing = SQLiteStore(tmp_path / "state")
        if blob_reads:
            original_read = backing.read_bytes
            def blob_client(key):
                def download_blob(*, offset, length):
                    assert offset == 0 and length > 0
                    content, etag = original_read(key, max_bytes=length)
                    return SimpleNamespace(readall=lambda: content[offset:offset + length], properties=SimpleNamespace(etag=etag))
                return SimpleNamespace(download_blob=download_blob)
            adapter = object.__new__(BlobStore)
            adapter.container = SimpleNamespace(get_blob_client=blob_client)
            backing.read_bytes = adapter.read_bytes
        return backing
    store = open_store()
    for path in (fixture / "seed").rglob("*"):
        if path.is_file():
            store.write_bytes(path.relative_to(fixture / "seed").as_posix(), path.read_bytes())
    batch_service = BatchService(store)
    monkeypatch.setattr("backend.batch_api.service", lambda: batch_service)
    client = TestClient(development_app, client=("127.0.0.1", 9999))
    assert client.get("/api/v1/batches").status_code == 401
    assert client.get("/api/v1/batches", headers={"x-docintel-development": "1"}).status_code == 401
    assert client.get("/api/v1/batches", headers={"authorization": "Bearer invalid"}).status_code == 401
    for change in [{"scp": "User.Read"}, {"aud": "wrong"}, {"tid": frontend}, {"azp": tenant}, {"exp": 1}]:
        invalid = {"authorization": "Bearer " + jwt.encode({**claims, **change}, key, algorithm="RS256")}
        assert client.get("/api/v1/batches", headers=invalid).status_code == 401
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert client.get("/api/v1/batches", headers={"authorization": "Bearer " + jwt.encode(claims, wrong_key, algorithm="RS256")}).status_code == 401
    validated = client.post("/api/v1/batches/validate", headers=headers, data={"attribute_reference": "definitions.xlsx"}, files={"manifest": ("manifest.xlsx", (fixture / "manifest.xlsx").read_bytes()), "attributes": ("definitions.xlsx", (fixture / "definitions.xlsx").read_bytes())})
    assert validated.status_code == 200 and validated.json()["valid"] and validated.json()["product_count"] == 2
    batch_id = validated.json()["id"]
    base = f"/api/v1/batches/{batch_id}"
    submission = {"request_id": "44444444-4444-4444-8444-444444444444", "mode": "evidence_only", "confirm_live": False}
    first = client.post(base + "/submit", headers=headers, json=submission)
    repeated = client.post(base + "/submit", headers=headers, json=submission)
    assert first.status_code == repeated.status_code == 200 and first.json() == repeated.json()
    run_batch(store, batch_id, item_limit=1, concurrency=1)
    assert client.get(base, headers=headers).json()["state"] == "queued"
    first_result = store.read_bytes(f"results/{batch_id}/row-2.json")
    batch_service = BatchService(open_store())
    run_batch(batch_service.store, batch_id, item_limit=1, concurrency=1)
    assert client.get(base, headers=headers).json()["state"] == "completed"
    hashes = {}
    originals = {}
    for row in (2, 3):
        detail = client.get(base + f"/items/row-{row}", headers=headers).json()
        assert detail["machine_result"]["model_call_status"] == "not_attempted"
        assert detail["reviewer_identity"] == "verified_entra"
        hashes[str(row)] = detail["machine_sha256"]
        originals[row] = batch_service.store.read_bytes(f"results/{batch_id}/row-{row}.json")
    assert originals[2] == first_result
    for path in (base, base + "/items/row-2", base + "/items/row-3", base + "/export"):
        assert client.get(path, headers=other).status_code == 404
    review = {"attribute_id": "Pressure Rating", "decision": "reject", "reason": "Synthetic acceptance only: no candidate; not a product decision"}
    reviews = base + "/items/row-2/reviews"
    assert client.post(reviews, headers=other, json=review).status_code == 404
    assert client.post(reviews, headers=headers, json={**review, "reviewer": "spoofed"}).status_code == 422
    assert client.post(reviews, headers=headers, json=review).status_code == 200
    assert client.post(reviews, headers=headers, json=review).status_code == 422
    run_batch(batch_service.store, batch_id, processor=lambda *_: pytest.fail("Completed work repeated"))
    batch_service = BatchService(open_store())
    assert all(batch_service.store.read_bytes(f"results/{batch_id}/row-{row}.json") == original for row, original in originals.items())
    assert not batch_service.store.keys("analysis-attempts/") and not batch_service.store.keys("budgets/")
    exported = client.get(base + "/export", headers=headers)
    assert exported.status_code == 200 and "no-store" in exported.headers["cache-control"]
    assert verify_export(exported.content, batch_id, owner, hashes)