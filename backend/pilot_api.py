"""Loopback-only entry point for the existing FastAPI pilot workflow."""

import os
from functools import lru_cache
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.pilot import PilotError, PilotService, ReviewInput, RunRequest


os.umask(0o077)
app = FastAPI(title="DocIntel Local Review", docs_url=None, redoc_url=None, openapi_url=None)


@lru_cache
def service() -> PilotService:
    return PilotService()


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    host = urlsplit("http://" + request.headers.get("host", "")).hostname
    if host not in ("127.0.0.1", "localhost", "::1") or not request.client or request.client.host not in ("127.0.0.1", "::1"):
        return JSONResponse({"detail": "Loopback access only"}, status_code=403)
    if request.headers.get("origin") or request.headers.get("x-docintel-local") != "1":
        return JSONResponse({"detail": "Use the local same-origin application"}, status_code=403)
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse({"detail": "Local pilot unavailable; sensitive details withheld."}, status_code=503)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(PilotError)
async def pilot_error(request: Request, error: PilotError):
    return JSONResponse({"detail": str(error)}, status_code=409)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    return JSONResponse({"detail": "Invalid configured source, execution confirmation, or review input"}, status_code=422)


@app.exception_handler(Exception)
async def unavailable(request: Request, error: Exception):
    return JSONResponse({"detail": "Local pilot unavailable; check private configuration. Sensitive details withheld."}, status_code=503)


@app.get("/catalog")
def catalog():
    return service().catalog()


@app.get("/runs")
def runs():
    return service().list_runs()


@app.post("/runs", status_code=202)
def start(request: RunRequest, tasks: BackgroundTasks):
    instance = service()
    run_id, created = instance.begin(request)
    if created:
        tasks.add_task(instance.execute, run_id)
    return {"id": run_id, "created": created}


@app.get("/runs/{run_id}")
def run(run_id: str):
    return service().get(run_id)


@app.post("/runs/{run_id}/reviews")
def review(run_id: str, decision: ReviewInput):
    return service().review(run_id, decision)


@app.get("/runs/{run_id}/export")
def export(run_id: str):
    return JSONResponse(service().get(run_id), headers={"Content-Disposition": 'attachment; filename="docintel-review-v1.json"'})


@app.post("/runs/{run_id}/export")
def save_export(run_id: str):
    return service().export_snapshot(run_id)