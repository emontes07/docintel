"""Azure AI Document Intelligence client for the ``prebuilt-layout`` model.

Authenticated with ``DefaultAzureCredential`` (managed identity), matching the
pattern in ``backend/core/azure_storage.py``. No API keys.

Blob-source results are cached to Blob Storage to avoid repeat parsing. The
cache key is the **source blob path plus its ETag**, so a hit costs one
lightweight metadata read rather than downloading the document. Renaming or
re-uploading a document produces a new key; there is no cross-copy dedup, which
is the accepted trade-off for keeping cache checks cheap.

PDF byte input bypasses Blob Storage and uses a SHA-256 content version.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, List, Optional
from urllib.parse import urlparse, unquote

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from pydantic import BaseModel, Field

from backend.core.config import settings

logger = logging.getLogger(__name__)

LAYOUT_MODEL_ID = "prebuilt-layout"


class DocumentIntelligenceError(Exception):
    """Base class for Document Intelligence failures."""


class NotConfiguredError(DocumentIntelligenceError):
    """Required Document Intelligence configuration is missing."""


class ParsedTable(BaseModel):
    """A table extracted from the document."""

    page_number: Optional[int] = Field(default=None)
    row_count: int = 0
    column_count: int = 0
    cells: List[List[str]] = Field(
        default_factory=list, description="Row-major grid of cell text"
    )


class ParsedParagraph(BaseModel):
    """A block of text and the page it appeared on."""

    text: str
    page_number: Optional[int] = Field(default=None)
    role: Optional[str] = Field(
        default=None, description="Layout role, e.g. title / sectionHeading"
    )


class ParsedDocument(BaseModel):
    """Structured layout result for one source document."""

    source: str = Field(description="Original document location that was parsed")
    cache_key: str = Field(description="Blob path + ETag cache key, or SHA-256 content version for byte input")
    parsed_at: datetime
    tables: List[ParsedTable] = Field(default_factory=list)
    paragraphs: List[ParsedParagraph] = Field(default_factory=list)
    raw_text: str = ""


class DocumentIntelligenceService:
    """Parses PDF bytes directly or Blob sources with a Blob-backed layout cache."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        credential: Optional[Any] = None,
        cache_container: Optional[str] = None,
    ):
        self.endpoint = endpoint or settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT
        self.cache_container = (
            cache_container or settings.AZURE_BLOB_PARSE_CACHE_CONTAINER
        )
        self._credential = credential
        self._blob_service: Optional[BlobServiceClient] = None
        self.last_page_count: int | None = None

    # ── configuration / clients ──────────────────────────────────────────

    def _require_config(self) -> None:
        if not self.endpoint:
            raise NotConfiguredError(
                "Document Intelligence is not configured; missing: "
                "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
            )

    def _get_credential(self) -> Any:
        if self._credential is None:
            self._credential = DefaultAzureCredential()
        return self._credential

    def _get_blob_service(self) -> BlobServiceClient:
        if self._blob_service is None:
            account_url = settings.AZURE_BLOB_SERVICE_URL
            if not account_url and settings.AZURE_STORAGE_ACCOUNT_NAME:
                account_url = (
                    f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}"
                    ".blob.core.windows.net/"
                )
            if not account_url:
                raise NotConfiguredError(
                    "Blob Storage is not configured; missing: "
                    "AZURE_BLOB_SERVICE_URL or AZURE_STORAGE_ACCOUNT_NAME"
                )
            self._blob_service = BlobServiceClient(
                account_url=account_url, credential=self._get_credential()
            )
        return self._blob_service

    # ── cache ────────────────────────────────────────────────────────────

    @staticmethod
    def _split_source(blob_url_or_path: str) -> tuple[str, str]:
        """Return ``(container, blob_name)`` for a full blob URL or ``container/blob``."""
        if blob_url_or_path.startswith(("http://", "https://")):
            path = unquote(urlparse(blob_url_or_path).path).lstrip("/")
        else:
            path = blob_url_or_path.lstrip("/")
        container, _, blob_name = path.partition("/")
        if not container or not blob_name:
            raise DocumentIntelligenceError(
                f"Cannot derive container and blob name from {blob_url_or_path!r}"
            )
        return container, blob_name

    def _cache_key(self, container: str, blob_name: str, etag: str) -> str:
        """Stable cache key from source location + ETag."""
        digest = hashlib.sha256(
            f"{container}/{blob_name}@{etag}".encode("utf-8")
        ).hexdigest()
        return f"{digest}.json"

    def _read_cache(self, key: str) -> Optional[ParsedDocument]:
        blob = self._get_blob_service().get_blob_client(self.cache_container, key)
        try:
            payload = blob.download_blob().readall()
        except ResourceNotFoundError:
            return None
        try:
            return ParsedDocument.model_validate_json(payload)
        except Exception:
            logger.warning("Discarding unreadable parse cache entry %s", key)
            return None

    def _write_cache(self, key: str, parsed: ParsedDocument) -> None:
        container = self._get_blob_service().get_container_client(self.cache_container)
        try:
            container.create_container()
        except Exception:
            pass  # already exists, or insufficient rights — upload will report
        try:
            container.upload_blob(
                name=key,
                data=parsed.model_dump_json().encode("utf-8"),
                overwrite=True,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except Exception as exc:  # a cache write must never fail the parse
            logger.warning("Failed to write parse cache entry %s: %s", key, exc)

    # ── parsing ──────────────────────────────────────────────────────────

    def extract_document(self, blob_url_or_path: str) -> ParsedDocument:
        """Parse a document with ``prebuilt-layout``, using the cache when possible."""
        self._require_config()
        container, blob_name = self._split_source(blob_url_or_path)

        source_blob = self._get_blob_service().get_blob_client(container, blob_name)
        try:
            properties = source_blob.get_blob_properties()
        except ResourceNotFoundError as exc:
            raise DocumentIntelligenceError(
                f"Source document not found: {container}/{blob_name}"
            ) from exc

        etag = str(properties.etag or "").strip('"')
        key = self._cache_key(container, blob_name, etag)

        cached = self._read_cache(key)
        if cached is not None:
            logger.info("Parse cache hit for %s/%s", container, blob_name)
            return cached

        logger.info("Parse cache miss for %s/%s; calling prebuilt-layout", container, blob_name)
        result = self._analyze(source_blob.url)
        parsed = self._to_parsed_document(
            result, source=f"{container}/{blob_name}", cache_key=key
        )
        self._write_cache(key, parsed)
        return parsed

    def extract_pdf_bytes(self, content: bytes, *, source: str, page_limit: int | None = None) -> ParsedDocument:
        """Analyze PDF bytes once without Blob access; retain a content-based version."""
        self._require_config()
        if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
            raise ValueError("Expected nonempty PDF bytes")
        if not source.strip():
            raise ValueError("An original source location is required")
        version = f"sha256:{hashlib.sha256(content).hexdigest()}"
        self.last_page_count = None
        if page_limit is not None and (type(page_limit) is not int or not 1 <= page_limit <= 5):
            raise ValueError("Bounded PDF analysis supports one to five pages")
        result = self._analyze(content, page_limit=page_limit) if page_limit is not None else self._analyze(content)
        pages = getattr(result, "pages", None)
        if isinstance(pages, list):
            self.last_page_count = len(pages)
        return self._to_parsed_document(result, source=source, cache_key=version)

    def _analyze(self, source_url: str | bytes, *, page_limit: int | None = None) -> Any:
        # Imported lazily to keep application import independent of the SDK.
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        except ImportError as exc:
            raise DocumentIntelligenceError(
                "Document Intelligence requires the azure-ai-documentintelligence "
                "package, which is not installed."
            ) from exc

        try:
            body = (
                BytesIO(source_url)
                if isinstance(source_url, bytes)
                else AnalyzeDocumentRequest(url_source=source_url)
            )
            with DocumentIntelligenceClient(
                endpoint=self.endpoint, credential=self._get_credential(),
                retry_total=0,
            ) as client:
                options = {"pages": f"1-{page_limit}"} if page_limit is not None else {}
                poller = client.begin_analyze_document(LAYOUT_MODEL_ID, body=body, **options)
                return poller.result(timeout=120) if page_limit is not None else poller.result()
        except Exception as exc:
            raise DocumentIntelligenceError("Layout analysis failed") from exc

    # ── result mapping ───────────────────────────────────────────────────

    @staticmethod
    def _page_of(element: Any) -> Optional[int]:
        for region in getattr(element, "bounding_regions", None) or []:
            page = getattr(region, "page_number", None)
            if page is not None:
                return int(page)
        return None

    @classmethod
    def _to_parsed_document(
        cls, result: Any, source: str, cache_key: str
    ) -> ParsedDocument:
        paragraphs = [
            ParsedParagraph(
                text=getattr(p, "content", "") or "",
                page_number=cls._page_of(p),
                role=getattr(p, "role", None),
            )
            for p in getattr(result, "paragraphs", None) or []
            if getattr(p, "content", None)
        ]

        tables: List[ParsedTable] = []
        for table in getattr(result, "tables", None) or []:
            rows = int(getattr(table, "row_count", 0) or 0)
            cols = int(getattr(table, "column_count", 0) or 0)
            grid = [["" for _ in range(cols)] for _ in range(rows)]
            for cell in getattr(table, "cells", None) or []:
                r = int(getattr(cell, "row_index", 0) or 0)
                c = int(getattr(cell, "column_index", 0) or 0)
                if 0 <= r < rows and 0 <= c < cols:
                    grid[r][c] = getattr(cell, "content", "") or ""
            tables.append(
                ParsedTable(
                    page_number=cls._page_of(table),
                    row_count=rows,
                    column_count=cols,
                    cells=grid,
                )
            )

        raw_text = getattr(result, "content", "") or ""
        if not raw_text:
            raw_text = "\n".join(p.text for p in paragraphs)

        return ParsedDocument(
            source=source,
            cache_key=cache_key,
            parsed_at=datetime.now(timezone.utc),
            tables=tables,
            paragraphs=paragraphs,
            raw_text=raw_text,
        )


_service: Optional[DocumentIntelligenceService] = None


def extract_document(blob_url_or_path: str) -> ParsedDocument:
    """Parse a document with ``prebuilt-layout``, caching the result in Blob Storage."""
    global _service
    if _service is None:
        _service = DocumentIntelligenceService()
    return _service.extract_document(blob_url_or_path)
