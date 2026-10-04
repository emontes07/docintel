"""Authenticated batch routes shared by the deployed backend and development app."""

from functools import lru_cache
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.batch import BatchService
from backend.batch_auth import actor
from backend.batch_store import Conflict, Missing, configured_store
from backend.pilot_upload import PilotSourceUpload
from backend.workbooks import MAX_BYTES

router = APIRouter(prefix="/batches", tags=["batches"])


@lru_cache(maxsize=1)
def service():
    try:
        return BatchService(configured_store())
    except Exception:
        raise HTTPException(503, "Private batch storage is unavailable. Check approved container, identity, and network configuration") from None


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    mode: Literal["evidence_only", "live_inference", "real_pilot"] = "evidence_only"
    confirm_live: bool = False


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attribute_id: str
    decision: Literal["approve", "correct", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
    candidate_index: int | None = Field(default=None, ge=0)
    corrected_value: str | int | float | bool | None = None
    corrected_unit: str | None = None


def call(operation):
    try:
        return operation()
    except Missing:
        raise HTTPException(404, "Batch or item not found") from None
    except Conflict as error:
        raise HTTPException(409, str(error)) from None
    except ValueError:
        raise HTTPException(422, "Invalid workbook, batch selection, or review contract. Check data-only XLSX, required columns, explicit bindings, and typed values") from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Batch storage or processing unavailable; no sensitive provider details exposed") from None


@router.get("")
def list_batches(identity=Depends(actor)):
    return call(lambda: service().list(identity))


@router.get("/catalog")
def catalog(identity=Depends(actor)):
    return call(lambda: {"identity": identity, "sources": [{**{key: source.get(key) for key in ["source_id", "reference", "kind"]}, "product_ids": [product["item_id"] for product in source["products"]]} for source in service().catalog(identity)], "source_upload": PilotSourceUpload(service().store).catalog(identity)})


@router.post("/pilot-sources/finalize")
def finalize_pilot_sources(identity=Depends(actor)):
    return call(lambda: PilotSourceUpload(service().store).finalize(identity))


@router.post("/pilot-sources/{source_id}")
async def upload_pilot_source(source_id: str, document: UploadFile = File(), identity=Depends(actor)):
    uploader = call(lambda: PilotSourceUpload(service().store))
    approved = call(lambda: uploader.catalog(identity))
    source = next((source for source in approved or [] if source["source_id"] == source_id), None)
    if source is None:
        raise HTTPException(404, "Approved source not found")
    if document.filename != source["filename"]:
        raise HTTPException(422, "Filename differs from the approved source")
    content = await document.read(MAX_BYTES + 1)
    return call(lambda: uploader.upload(source_id, content, identity))


@router.post("/validate")
async def validate(manifest: UploadFile = File(), attributes: UploadFile = File(), attribute_reference: str = Form(min_length=1, max_length=255), identity=Depends(actor)):
    manifest_bytes = await manifest.read(MAX_BYTES + 1)
    attribute_bytes = await attributes.read(MAX_BYTES + 1)
    return call(lambda: service().summary(service().intake(manifest_bytes, attribute_bytes, attribute_reference, identity)))


@router.get("/{batch_id}")
def batch(batch_id: str, identity=Depends(actor)):
    return call(lambda: service().summary(service().get(batch_id, identity)))


@router.post("/{batch_id}/submit")
def submit(batch_id: str, request: Submit, identity=Depends(actor)):
    return call(lambda: service().summary(service().submit(batch_id, identity, str(request.request_id), request.mode, request.confirm_live)))


@router.get("/{batch_id}/items")
def items(batch_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100), view: Literal["all", "failed", "unresolved", "pending"] = "all", identity=Depends(actor)):
    return call(lambda: service().items(batch_id, identity, offset, limit, view))


@router.get("/{batch_id}/items/{item_key}")
def item(batch_id: str, item_key: str, identity=Depends(actor)):
    return call(lambda: service().detail(batch_id, item_key, identity))


@router.post("/{batch_id}/items/{item_key}/reviews")
def review(batch_id: str, item_key: str, request: Review, identity=Depends(actor)):
    return call(lambda: service().review(batch_id, item_key, identity, request.model_dump()))


@router.get("/{batch_id}/export")
def export(batch_id: str, identity=Depends(actor)):
    content = call(lambda: service().export(batch_id, identity))
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="docintel-batch.xlsx"', "Cache-Control": "no-store"})


development_app = FastAPI(title="DocIntel batch development")
development_app.include_router(router, prefix="/api/v1")


async def batch_response_headers(request, call_next):
    if request.method == "POST":
        length = request.headers.get("content-length", "")
        if not length.isdecimal() or request.headers.get("transfer-encoding"):
            return JSONResponse({"detail": "A bounded Content-Length is required"}, status_code=411, headers={"Cache-Control": "no-store"})
        if int(length) > 21 * 1024 * 1024:
            return JSONResponse({"detail": "Batch request exceeds 21 MiB"}, status_code=413, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


development_app.middleware("http")(batch_response_headers)