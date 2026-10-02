"""Verified delegated Entra identity for hosted batch operations."""

import os
import uuid
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


async def restrict_hosted_routes(request, call_next, prefix="/api/v1"):
    development = os.environ.get("DOCINTEL_BATCH_MODE") == "development" and not (os.environ.get("CONTAINER_APP_NAME") or os.environ.get("IDENTITY_ENDPOINT"))
    path = request.url.path
    if not development and path not in {"/", f"{prefix}/health", f"{prefix}/openapi.json", f"{prefix}/batches"} and not path.startswith(f"{prefix}/batches/"):
        return JSONResponse({"detail": "Route unavailable in hosted batch mode"}, status_code=404, headers={"Cache-Control": "no-store"})
    return await call_next(request)


def verify_token(token, key, tenant, audience, client):
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=audience, issuer=f"https://login.microsoftonline.com/{tenant}/v2.0", options={"require": ["exp", "iat", "nbf", "tid", "oid", "azp", "scp"]})
    if claims["tid"] != tenant or claims["azp"] != client or not isinstance(claims["scp"], str) or "Batch.Access" not in claims["scp"].split():
        raise ValueError("Token tenant, client, or scope is not authorized")
    return f"{uuid.UUID(claims['tid'])}/{uuid.UUID(claims['oid'])}"


@lru_cache(maxsize=4)
def keys(tenant):
    return jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", timeout=10)


def actor(request: Request):
    if os.environ.get("DOCINTEL_BATCH_MODE") == "development":
        if os.environ.get("CONTAINER_APP_NAME") or os.environ.get("IDENTITY_ENDPOINT"):
            raise HTTPException(503, "Development authentication is forbidden in Azure")
        if request.client and request.client.host in {"127.0.0.1", "::1"} and request.headers.get("x-docintel-development") == "1" and not request.headers.get("origin"):
            return "development:local-unverified"
        raise HTTPException(403, "Use the explicit loopback development proxy")
    try:
        tenant = str(uuid.UUID(os.environ["DOCINTEL_AUTH_TENANT_ID"]))
        audience = os.environ["DOCINTEL_AUTH_AUDIENCE"]
        client = str(uuid.UUID(os.environ["DOCINTEL_AUTH_CLIENT_ID"]))
        if not audience:
            raise ValueError
    except (KeyError, ValueError):
        raise HTTPException(503, "Hosted batch authentication is not configured") from None
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization) > 16000:
        raise HTTPException(401, "A batch API access token is required")
    try:
        token = authorization[7:]
        return verify_token(token, keys(tenant).get_signing_key_from_jwt(token).key, tenant, audience, client)
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "Batch API token is invalid, expired, or not authorized") from None