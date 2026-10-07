"""Bounded local PDF text and opt-in worker OCR using existing parse contracts.

Worker wiring: ``QualityWeb(pdf_ocr=CachedPDFOCR(store, parser))``, where parser
is the existing DocumentIntelligenceService with the worker credential. Never
construct that adapter in the API. Text PDFs do not consult this adapter.
The optional before_call/usage_callback hooks reuse the worker's existing meter.
Usage records use operation=document_intelligence and actual analyzed_pages when
reported by the parser. A worker pricing callback supplies cost_usd/pricing_basis;
otherwise paid attempts remain explicitly unpriced, never implicitly free.

Existing service constructor: DocumentIntelligenceService(endpoint=None,
credential=None, cache_container=None). Pass the worker's ManagedIdentityCredential
explicitly; omitted credentials use the service's DefaultAzureCredential lazily.
extract_pdf_bytes(content, *, source, page_limit=None) returns ParsedDocument
(source, cache_key, parsed_at, tables, paragraphs, raw_text); it does not use Blob
cache. This adapter calls it with page_limit=actual page_count and persists only
successful parses in the supplied worker store. It creates no credentials,
reservations, leases, price schedules, or additional monetary guard.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import subprocess
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Callable

from backend.batch_store import Missing, read_json, write_json
from backend.core.docintel import ParsedDocument
from backend.core.websearch import OriginalPageEvidence
from backend.pilot import PARSER_VERSION

if TYPE_CHECKING:
    from backend.core.docintel import DocumentIntelligenceService

PDF_MAX_BYTES = 10 * 1024 * 1024
TEXT_MAX_BYTES = 262144
OCR_MAX_PAGES = 5


class PDFPageError(ValueError):
    def __init__(self, provenance: dict, error_type: str):
        super().__init__("PDF original-page processing failed; no provider content accepted")
        self.provenance = provenance
        self.error_type = error_type


def _pdf_command(command: list[str], content: bytes, limit: int) -> bytes:
    """Pipe bytes through Poppler without files, with time and output bounds."""
    output = bytearray()
    deadline = monotonic() + 30
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdin.fileno(), False)
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
                selector.register(process.stdout, selectors.EVENT_READ)
                offset = 0
                while selector.get_map():
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise ValueError("PDF text processing timed out")
                    for key, _ in selector.select(remaining):
                        if key.fileobj is process.stdin:
                            try:
                                offset += os.write(process.stdin.fileno(), content[offset:offset + 65536])
                            except BrokenPipeError:
                                offset = len(content)
                            if offset == len(content):
                                selector.unregister(process.stdin)
                                process.stdin.close()
                        else:
                            chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
                            if not chunk:
                                selector.unregister(process.stdout)
                            output.extend(chunk)
                            if len(output) > limit:
                                raise ValueError("PDF text processing exceeds its output bound")
                if process.wait(timeout=max(.01, deadline - monotonic())) != 0:
                    raise ValueError("PDF text processing failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return bytes(output)


def pdf_text(content: bytes) -> tuple[str, int]:
    if not content.startswith(b"%PDF-") or len(content) > PDF_MAX_BYTES:
        raise ValueError("Expected a bounded PDF response")
    info = _pdf_command(["pdfinfo", "-"], content, 65536).decode("utf-8")
    counts = re.findall(r"^Pages:\s+([0-9]+)\s*$", info, re.MULTILINE)
    if len(counts) != 1 or int(counts[0]) < 1:
        raise ValueError("PDF actual page count is unavailable")
    text = _pdf_command(["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"], content, TEXT_MAX_BYTES).decode("utf-8")
    if "\x00" in text:
        raise ValueError("PDF contains unusable text")
    return text, int(counts[0])


@dataclass(frozen=True)
class PDFOCRResult:
    document: ParsedDocument
    cache_key: str
    cache_hit: bool
    first_retrieved_at: datetime


@dataclass(frozen=True)
class PDFPageEvidence(OriginalPageEvidence):
    pdf_page_count: int = 0
    pdf_extraction: str = "pdftotext"
    pdf_cache_key: str | None = None
    pdf_cache_hit: bool = False
    pdf_parsed_at: datetime | None = None
    pdf_parse_source: str | None = None
    pdf_first_retrieved_at: datetime | None = None

    def provenance(self) -> dict:
        return {
            "source_url": self.final_url, "sha256": self.content_hash,
            "retrieved_at": self.retrieved_at.isoformat(), "media_type": self.media_type,
            "byte_size": self.byte_size, "page_count": self.pdf_page_count,
            "extraction": self.pdf_extraction, "cache_key": self.pdf_cache_key,
            "cache_hit": self.pdf_cache_hit,
            "parsed_at": self.pdf_parsed_at.isoformat() if self.pdf_parsed_at else None,
            "parse_source": self.pdf_parse_source,
            "first_retrieved_at": self.pdf_first_retrieved_at.isoformat() if self.pdf_first_retrieved_at else None,
        }


class CachedPDFOCR:
    """Cache successful worker OCR by bytes; failed calls can be rerun normally."""

    def __init__(
        self, store, parser: DocumentIntelligenceService, *,
        usage_callback: Callable[[dict], object] | None = None,
        before_call: Callable[[dict], object] | None = None,
    ):
        self.store = store
        self.parser = parser
        self.usage_callback = usage_callback
        self.before_call = before_call

    def __call__(self, content: bytes, *, source: str, page_count: int, retrieved_at: datetime) -> PDFOCRResult:
        if not isinstance(content, bytes) or not content.startswith(b"%PDF-") or len(content) > PDF_MAX_BYTES:
            raise ValueError("Expected a bounded PDF response")
        if type(page_count) is not int or not 1 <= page_count <= OCR_MAX_PAGES:
            raise ValueError("OCR requires an actual PDF page count of one to five")
        version = "sha256:" + hashlib.sha256(content).hexdigest()
        key = "parses/quality-web-pdf/" + hashlib.sha256((version + PARSER_VERSION).encode()).hexdigest() + ".json"
        entry = {
            "operation": "document_intelligence", "provider": "azure_document_intelligence", "phase": "pdf_ocr",
            "source_url": source, "sha256": version.removeprefix("sha256:"), "cache_key": key,
            "retrieved_at": retrieved_at.isoformat(), "requested_pages": page_count,
            "cache_hit": False, "new_analysis": False, "analysis_attempted": False, "analyzed_pages": 0,
            "usage_reported": True, "cost_usd": 0, "pricing_basis": "no_new_analysis",
            "status": "not_started", "stage": "cache_read",
        }
        failure = None
        try:
            try:
                result = self._cached(key, version, page_count)
            except Missing:
                pass
            else:
                entry.update(status="cached", cache_hit=True, pricing_basis="cached_parse")
                return result
            entry["stage"] = "before_call"
            if self.before_call:
                self.before_call(dict(entry))
            entry.update(
                stage="analysis", new_analysis=True, analysis_attempted=True, analyzed_pages=None, usage_reported=False,
                cost_usd=None, pricing_basis=None,
            )
            document = self.parser.extract_pdf_bytes(content, source=source, page_limit=page_count)
            analyzed_pages = self.parser.last_page_count
            if type(analyzed_pages) is int and 1 <= analyzed_pages <= page_count:
                entry.update(analyzed_pages=analyzed_pages, usage_reported=True)
            entry["stage"] = "result_validation"
            if document.source != source or document.cache_key != version:
                raise ValueError("PDF OCR source association mismatch")
            self._validate_text(document)
            entry["stage"] = "cache_write"
            write_json(self.store, key, {
                "parser_version": PARSER_VERSION, "origin": "quality_web_pdf_ocr",
                "document": document.model_dump(mode="json"),
                "document_sha256": hashlib.sha256(document.model_dump_json().encode()).hexdigest(),
                "page_count": page_count, "first_retrieved_at": retrieved_at.isoformat(),
            })
            entry.update(status="succeeded", stage="complete")
            return PDFOCRResult(document, key, False, retrieved_at)
        except Exception as error:
            failure = error
            entry.update(status="failed", error_type=type(error).__name__)
            raise
        finally:
            if self.usage_callback:
                try:
                    self.usage_callback(dict(entry))
                except Exception as callback_error:
                    if failure is None:
                        raise
                    failure.add_note("PDF OCR usage callback failed: " + type(callback_error).__name__)

    @staticmethod
    def _validate_text(document: ParsedDocument) -> None:
        if (not document.raw_text.strip() or "\x00" in document.raw_text
                or len(document.raw_text.encode("utf-8")) > TEXT_MAX_BYTES):
            raise ValueError("PDF OCR returned no usable bounded text")

    def _cached(self, key: str, version: str, page_count: int) -> PDFOCRResult:
        cached, _ = read_json(self.store, key)
        document = ParsedDocument.model_validate(cached["document"])
        if (cached["parser_version"] != PARSER_VERSION or document.cache_key != version
                or cached["page_count"] != page_count or cached["origin"] != "quality_web_pdf_ocr"
                or hashlib.sha256(document.model_dump_json().encode()).hexdigest() != cached["document_sha256"]):
            raise ValueError("PDF OCR cache integrity mismatch")
        self._validate_text(document)
        return PDFOCRResult(document, key, True, datetime.fromisoformat(cached["first_retrieved_at"]))
