from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.batch_api import development_app
from backend.batch_auth import verify_token
from tests.test_batch import service, workbooks


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