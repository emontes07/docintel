"""Default-off private copies, not permission to process customer documents.

An operator (never an HTTP request) seeds ``APPROVAL_KEY`` with exactly:
``schema_version: 1``, ``approved: true``, canonical UUID ``id`` and
``approved_by``, ``owner: "tenant-UUID/object-UUID"``, aware ISO ``expires_at``,
``expected_registry_sha256``, ``sources`` (existing SourceBinding contracts),
and ``documents`` (source_id, filename, bytes, sha256). Optional ``input_hashes``
contains both ``manifest`` and ``attributes`` SHA256s. It binds intake metadata
only; this module neither uploads workbooks for intake nor processes documents.

Use ``registry_sha256(record["sources"])`` to calculate the approval's registry
hash. It normalizes SourceBinding defaults and source order, not JSON formatting.
At most six declarations, four non-web sources/documents, two web sources, four
distinct products, and 5 MiB total approved document bytes are allowed. Every
blob declaration has exactly one document; PDF/XLSX extensions match its format.
Blob destinations are operator-approved ``documents/<safe-basename>`` only.

The existing authenticated batch dependency supplies ``actor``; this module
does not verify tokens or accept client-asserted identity. Both the exact owner
and its object UUID as allowlisted approver are required. Enable only with
``DOCINTEL_PILOT_UPLOAD_ENABLED=true``; processing authorization is independent.
The allowlist remains ``DOCINTEL_REAL_PILOT_OPERATOR_IDS``. Checks never write.
Every approved registry contribution inherits the top-level owner, including
web/SharePoint declarations; an explicitly different source owner is rejected.
The first accepted mutation pins the complete approval and exact merged
registry hash in an immutable singleton session. There is no reset/rollover API.
Transport failures during writes raise UploadOutcomeUnknown: do not assume
success or issue an overwrite; an identical retry rechecks durable state.
"""

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Protocol, TypedDict

from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.batch import SourceBinding
from backend.batch_store import Conflict, Missing, read_json, write_json


APPROVAL_KEY = "configuration/pilot-source-upload.json"
SESSION_KEY = "configuration/pilot-source-upload-session.json"
REGISTRY_KEY = "configuration/sources.json"
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_APPROVAL_BYTES = 64 * 1024
SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_WRITE_ERRORS = (OSError, HttpResponseError, ServiceRequestError, ServiceResponseError)
_UNAVAILABLE = "Pilot source upload is unavailable"
_CONFLICT = "Pilot source upload state conflicts with approval"
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,199}")
_SAFE_KEY = re.compile(r"documents/[A-Za-z0-9][A-Za-z0-9._-]{0,199}")


class UploadStore(Protocol):
    def read_bytes(self, key: str, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, str]: ...

    def write_bytes(self, key: str, value: bytes, version: str | None = None) -> str: ...


class UploadOutcomeUnknown(RuntimeError):
    """A conditional write may have committed; inspect/retry identical input."""


class _NoApproval(ValueError):
    pass


class DocumentMetadata(TypedDict):
    source_id: str
    filename: str
    bytes: int
    sha256: str
    status: Literal["pending", "uploaded", "existing", "conflict"]


class FinalizeSummary(TypedDict):
    status: Literal["ready"]
    documents: list[DocumentMetadata]
    document_count: int
    total_bytes: int
    registry_sha256: str


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Document(_Strict):
    source_id: str
    filename: str
    bytes: int = Field(gt=0, le=MAX_DOCUMENT_BYTES)
    sha256: SHA256


class _InputHashes(_Strict):
    manifest: SHA256
    attributes: SHA256


class _Approval(_Strict):
    schema_version: Literal[1]
    approved: Literal[True]
    id: str
    approved_by: str
    owner: str
    expires_at: str
    expected_registry_sha256: SHA256
    sources: list[SourceBinding] = Field(min_length=1, max_length=6)
    documents: list[_Document] = Field(min_length=1, max_length=4)
    input_hashes: _InputHashes | None = None

    @model_validator(mode="before")
    @classmethod
    def exact_flags(cls, value: Any) -> Any:
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("approved") is not True:
            raise ValueError(_UNAVAILABLE)
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> "_Approval":
        if not _canonical_uuid(self.id) or not _canonical_uuid(self.approved_by):
            raise ValueError(_UNAVAILABLE)
        if not _verified_owner(self.owner) or self.owner.split("/")[1] != self.approved_by:
            raise ValueError(_UNAVAILABLE)
        expiry = datetime.fromisoformat(self.expires_at)
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError(_UNAVAILABLE)
        if any(source.owner is not None and source.owner != self.owner for source in self.sources):
            raise ValueError(_UNAVAILABLE)
        self.sources = [source.model_copy(update={"owner": self.owner}) for source in self.sources]
        _normalized_sources([source.model_dump(mode="json") for source in self.sources])
        if any(source.source_id == "finalize" for source in self.sources):
            raise ValueError(_UNAVAILABLE)
        products = {_digest(product.model_dump(mode="json")) for source in self.sources for product in source.products}
        if len(products) > 4 or sum(source.kind == "web" for source in self.sources) > 2 or sum(source.kind != "web" for source in self.sources) > 4:
            raise ValueError(_UNAVAILABLE)
        blobs = {source.source_id: source for source in self.sources if source.kind == "blob"}
        if len(self.documents) != len(blobs) or {doc.source_id for doc in self.documents} != set(blobs):
            raise ValueError(_UNAVAILABLE)
        if sum(doc.bytes for doc in self.documents) > MAX_TOTAL_BYTES:
            raise ValueError(_UNAVAILABLE)
        if len({source.blob for source in blobs.values()}) != len(blobs):
            raise ValueError(_UNAVAILABLE)
        for doc in self.documents:
            source = blobs[doc.source_id]
            if (
                not _SAFE_NAME.fullmatch(doc.filename) or ".." in doc.filename
                or not _SAFE_KEY.fullmatch(source.blob or "") or ".." in (source.blob or "")
                or not doc.filename.lower().endswith("." + source.format)
                or not (source.blob or "").lower().endswith("." + source.format)
                or doc.sha256 != source.sha256
            ):
                raise ValueError(_UNAVAILABLE)
        return self


class _Session(_Strict):
    schema_version: Literal[1]
    approval_id: str
    approval_sha256: SHA256
    target_registry_sha256: SHA256


@dataclass
class _Context:
    approval: _Approval
    fingerprint: str
    session: _Session | None
    registry: dict[str, Any]
    merged: dict[str, Any]
    version: str | None
    target_hash: str


def _canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _verified_owner(value: str) -> bool:
    return isinstance(value, str) and len(parts := value.split("/")) == 2 and all(_canonical_uuid(part) for part in parts)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def validate_upload_approval(approval: Any, owner: str | None = None) -> dict[str, Any]:
    """Validate metadata offline, without reading configuration or enabling work.

    Return a detached JSON object preserving supplied fields/default omissions.
    Check schema, finite scope, expiry, the 64 KiB canonical raw bound, and the
    exact owner if supplied. Operator allowlisting and current registry/session
    checks remain runtime responsibilities. No principal-ID field is required:
    existing authenticated tenant/object ownership is the identity contract.
    """
    try:
        encoded = _canonical_json(approval)
        if len(encoded) > MAX_APPROVAL_BYTES:
            raise ValueError
        parsed = _Approval.model_validate(approval, strict=True)
        if (owner is not None and parsed.owner != owner) or datetime.fromisoformat(parsed.expires_at) <= datetime.now(timezone.utc):
            raise ValueError
        return json.loads(encoded)
    except (ValidationError, ValueError, TypeError):
        raise ValueError(_UNAVAILABLE) from None


def _normalized_sources(sources: Any) -> list[dict[str, Any]]:
    try:
        if not isinstance(sources, list):
            raise ValueError
        result = [SourceBinding.model_validate(source).model_dump(mode="json") for source in sources]
        if len({source["source_id"] for source in result}) != len(result) or len({source["reference"] for source in result}) != len(result):
            raise ValueError
        return sorted(result, key=lambda source: (source["source_id"], source["reference"]))
    except (ValidationError, ValueError, TypeError):
        raise ValueError(_UNAVAILABLE) from None


def registry_sha256(sources: list[dict[str, Any]]) -> str:
    """Canonical semantic source-list hash used by the operator's approval."""
    return _digest(_normalized_sources(sources))


def _merge(registry: dict[str, Any], approved: list[SourceBinding]) -> dict[str, Any]:
    existing = _normalized_sources(registry.get("sources"))
    added = []
    for binding in approved:
        source = binding.model_dump(mode="json")
        collisions = [old for old in existing if old["source_id"] == source["source_id"] or old["reference"] == source["reference"]]
        if collisions:
            if len(collisions) != 1 or collisions[0] != source:
                raise Conflict(_CONFLICT)
        else:
            added.append(source)
    # Preserve legacy entries byte-for-byte at the object level, including omissions.
    return {**registry, "sources": [*registry["sources"], *added]}


def _metadata(document: _Document, status: Literal["pending", "uploaded", "existing", "conflict"]) -> DocumentMetadata:
    return {"source_id": document.source_id, "filename": document.filename, "bytes": document.bytes, "sha256": document.sha256, "status": status}


class PilotSourceUpload:
    def __init__(self, store: UploadStore):
        self.store = store

    def _prepare(self, actor: str) -> _Context:
        if not _verified_owner(actor):
            raise Missing(_UNAVAILABLE)
        if os.environ.get("DOCINTEL_PILOT_UPLOAD_ENABLED") != "true":
            raise ValueError(_UNAVAILABLE)
        try:
            content, _ = self.store.read_bytes(APPROVAL_KEY, max_bytes=MAX_APPROVAL_BYTES)
            raw = json.loads(content)
        except Missing:
            raise _NoApproval(_UNAVAILABLE) from None
        except (ValueError, UnicodeError):
            raise ValueError(_UNAVAILABLE) from None
        if isinstance(raw, dict) and raw.get("owner") != actor:
            raise Missing(_UNAVAILABLE)
        validated = validate_upload_approval(raw, owner=actor)
        approval = _Approval.model_validate(validated, strict=True)
        fingerprint = _digest(validated)
        operators = os.environ.get("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "").split(",")
        if approval.approved_by not in {operator.strip() for operator in operators}:
            raise ValueError(_UNAVAILABLE)
        try:
            raw_session, _ = read_json(self.store, SESSION_KEY)
        except Missing:
            session = None
        except (ValueError, UnicodeError):
            raise Conflict(_CONFLICT) from None
        else:
            try:
                session = _Session.model_validate(raw_session, strict=True)
            except ValidationError:
                raise Conflict(_CONFLICT) from None
            if session.approval_id != approval.id or session.approval_sha256 != fingerprint:
                raise Conflict(_CONFLICT)
        try:
            registry, version = read_json(self.store, REGISTRY_KEY)
        except Missing:
            registry, version = {"sources": []}, None
        except (ValueError, UnicodeError):
            raise ValueError(_UNAVAILABLE) from None
        if not isinstance(registry, dict):
            raise ValueError(_UNAVAILABLE)
        current_hash = registry_sha256(registry.get("sources"))
        merged = _merge(registry, approval.sources)
        target_hash = registry_sha256(merged["sources"])
        if session is None:
            if current_hash != approval.expected_registry_sha256:
                raise Conflict(_CONFLICT)
        elif target_hash != session.target_registry_sha256 or current_hash not in {approval.expected_registry_sha256, session.target_registry_sha256}:
            raise Conflict(_CONFLICT)
        return _Context(approval, fingerprint, session, registry, merged, version, target_hash)

    def _pin(self, context: _Context, actor: str) -> _Context:
        if context.session is None:
            session = _Session(schema_version=1, approval_id=context.approval.id, approval_sha256=context.fingerprint, target_registry_sha256=context.target_hash)
            try:
                write_json(self.store, SESSION_KEY, session.model_dump(mode="json"))
            except Conflict:
                pass  # The subsequent read must prove this is the same session.
            except _WRITE_ERRORS:
                raise UploadOutcomeUnknown("Pilot upload session write outcome is unknown") from None
        current = self._prepare(actor)
        if current.session is None or current.fingerprint != context.fingerprint or current.target_hash != context.target_hash:
            raise Conflict(_CONFLICT)
        return current

    def _document_status(self, source: SourceBinding, document: _Document) -> Literal["pending", "uploaded", "conflict"]:
        assert source.blob is not None
        try:
            content, _ = self.store.read_bytes(source.blob, max_bytes=MAX_DOCUMENT_BYTES)
        except Missing:
            return "pending"
        except ValueError:
            return "conflict"
        return "uploaded" if len(content) == document.bytes and hashlib.sha256(content).hexdigest() == document.sha256 else "conflict"

    def catalog(self, actor: str) -> list[DocumentMetadata] | None:
        """Read-only owner catalog; absent/disabled/unauthorized is hidden."""
        if not _verified_owner(actor) or os.environ.get("DOCINTEL_PILOT_UPLOAD_ENABLED") != "true":
            return None
        try:
            context = self._prepare(actor)
        except (Missing, _NoApproval):
            return None
        sources = {source.source_id: source for source in context.approval.sources}
        return [_metadata(doc, self._document_status(sources[doc.source_id], doc)) for doc in context.approval.documents]

    def upload(self, source_id: str, content: bytes, actor: str) -> DocumentMetadata:
        """Write only exact approved bytes; an existing object is never replaced."""
        if source_id == "finalize":
            raise Missing(_UNAVAILABLE)
        context = self._prepare(actor)
        document = next((doc for doc in context.approval.documents if doc.source_id == source_id), None)
        if document is None:
            raise Missing(_UNAVAILABLE)
        if not isinstance(content, bytes) or len(content) != document.bytes or hashlib.sha256(content).hexdigest() != document.sha256:
            raise ValueError("Document does not match approved bytes")
        source = next(source for source in context.approval.sources if source.source_id == source_id)
        status = self._document_status(source, document)
        if status == "conflict":
            raise Conflict(_CONFLICT)
        self._pin(context, actor)
        if status == "uploaded":
            return _metadata(document, "existing")
        assert source.blob is not None
        try:
            self.store.write_bytes(source.blob, content)
        except Conflict:
            if self._document_status(source, document) != "uploaded":
                raise Conflict(_CONFLICT) from None
            return _metadata(document, "existing")
        except _WRITE_ERRORS:
            raise UploadOutcomeUnknown("Pilot document write outcome is unknown") from None
        return _metadata(document, "uploaded")

    def finalize(self, actor: str) -> FinalizeSummary:
        """Publish declarations only after every approved blob is byte-verified."""
        context = self._prepare(actor)
        sources = {source.source_id: source for source in context.approval.sources}
        for document in context.approval.documents:
            if self._document_status(sources[document.source_id], document) != "uploaded":
                raise Conflict("Approved documents are not all available")
        context = self._pin(context, actor)
        if registry_sha256(context.registry["sources"]) != context.target_hash:
            try:
                write_json(self.store, REGISTRY_KEY, context.merged, context.version)
            except Conflict:
                raise Conflict("Registry changed; retry finalization after checking state") from None
            except _WRITE_ERRORS:
                raise UploadOutcomeUnknown("Pilot registry write outcome is unknown") from None
        return {
            "status": "ready",
            "documents": [_metadata(doc, "uploaded") for doc in context.approval.documents],
            "document_count": len(context.approval.documents),
            "total_bytes": sum(doc.bytes for doc in context.approval.documents),
            "registry_sha256": context.target_hash,
        }
