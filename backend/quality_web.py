"""Paid WebIQ search/browse followed by independent original-page verification.

REST contracts: https://webiq.microsoft.ai/documentation/openapi.json.
Neither search passages nor Browse content alone can become source evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from functools import partial
from typing import Callable
from urllib.parse import unquote, urlsplit

import httpx

from backend.core.config import settings
from backend.core.websearch import (
    OriginalPageEvidence, _PinnedHTTPSConnection, _public_addresses, _visible_html_text,
    validate_original_url,
)
from backend.models.enrichment import Evidence
from backend.quality_cost import CostLimitExceeded
from backend.quality_pdf import (
    OCR_MAX_PAGES, PDF_MAX_BYTES, TEXT_MAX_BYTES, CachedPDFOCR, PDFOCRResult, PDFPageError, PDFPageEvidence, pdf_text,
)

ENDPOINT = "https://api.microsoft.ai/v3/search/web"
BROWSE_ENDPOINT = "https://api.microsoft.ai/v3/browse"
BROWSE_MAX_LENGTH = 10000
MANUFACTURERS = {
    "ford": ("fordmeterbox.com", "www.fordmeterbox.com"),
    "mueller": ("muellercompany.com", "www.muellercompany.com", "muellerwaterproducts.com", "www.muellerwaterproducts.com"),
}


class BrowseUnavailable(ValueError):
    def __init__(self, reason, *, status_code=None, retry_after=None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.retry_after = retry_after


def original_page(url: str, *, pdf_ocr: Callable[..., PDFOCRResult] | None = None) -> OriginalPageEvidence:
    """Pinned public HTTPS; manufacturer PDF OCR requires explicit worker injection."""
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("Original page has no hostname")
    url = validate_original_url(url, [host])
    parsed = urlsplit(url)
    connection = _PinnedHTTPSConnection(host, _public_addresses(host)[0], 15)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"Accept-Encoding": "identity", "User-Agent": "DocIntel-quality/1.0"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            raise ValueError("Original page unavailable; redirects and encoded responses are not followed")
        media = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if media not in {"text/html", "text/plain", "application/pdf"}:
            raise ValueError("Original page is not HTML/text or a manufacturer PDF")
        if media == "application/pdf" and host not in {host for hosts in MANUFACTURERS.values() for host in hosts}:
            raise ValueError("PDF sources require an approved manufacturer domain")
        limit = PDF_MAX_BYTES if media == "application/pdf" else TEXT_MAX_BYTES
        raw = response.read(limit + 1)
        if not raw or len(raw) > limit:
            raise ValueError("Original page empty or exceeds its byte bound")
        retrieved_at = datetime.now(timezone.utc)
        content_hash = hashlib.sha256(raw).hexdigest()
        if media == "application/pdf":
            provenance = {
                "source_url": url, "sha256": content_hash, "retrieved_at": retrieved_at.isoformat(),
                "media_type": media, "byte_size": len(raw), "extraction": "pdftotext",
            }
            try:
                text, page_count = pdf_text(raw)
                provenance["page_count"] = page_count
                result = None
                if not text.strip():
                    if page_count > OCR_MAX_PAGES or pdf_ocr is None:
                        provenance["reason"] = "Textless PDF requires worker OCR and at most five actual pages"
                        raise ValueError("PDF OCR is not authorized")
                    provenance["extraction"] = "document_intelligence_ocr"
                    result = pdf_ocr(raw, source=url, page_count=page_count, retrieved_at=retrieved_at)
                    if result.document.cache_key != "sha256:" + content_hash:
                        raise ValueError("PDF OCR content version mismatch")
                    text = result.document.raw_text
                if not text.strip() or "\x00" in text or len(text.encode("utf-8")) > TEXT_MAX_BYTES:
                    raise ValueError("PDF contains no usable bounded text")
                return PDFPageEvidence(
                    text=text, final_url=url, content_hash=content_hash, retrieved_at=retrieved_at,
                    media_type=media, byte_size=len(raw), pdf_page_count=page_count,
                    pdf_extraction="document_intelligence_ocr" if result else "pdftotext",
                    pdf_cache_key=result.cache_key if result else None,
                    pdf_cache_hit=result.cache_hit if result else False,
                    pdf_parsed_at=result.document.parsed_at if result else None,
                    pdf_parse_source=result.document.source if result else None,
                    pdf_first_retrieved_at=result.first_retrieved_at if result else None,
                )
            except CostLimitExceeded:
                raise
            except Exception as error:
                raise PDFPageError(provenance, type(error).__name__) from None
        charset = (response.headers.get_content_charset() or "utf-8").lower()
        if charset not in {"utf-8", "ascii", "us-ascii", "iso-8859-1", "latin-1", "windows-1252"}:
            raise ValueError("Unsupported original-page encoding")
        text = raw.decode(charset)
        if media == "text/html":
            text = _visible_html_text(text, 262144)
        if not text.strip() or "\x00" in text:
            raise ValueError("Original page contains no usable text")
        return OriginalPageEvidence(
            text=text, final_url=url, content_hash=content_hash,
            retrieved_at=retrieved_at, media_type=media, byte_size=len(raw),
            text_normalization="html_visible_text_v1" if media == "text/html" else "decoded_plain_text",
        )
    finally:
        connection.close()


class QualityWeb:
    def __init__(self, *, api_key=None, search=None, browse=None, page_fetch=None, usage_callback=None, before_call=None, pdf_ocr=None):
        self.api_key = api_key if api_key is not None else os.environ.get("WEBIQ_API_KEY") or settings.WEBIQ_API_KEY
        if search is None and not self.api_key:
            raise ValueError("WEBIQ_API_KEY is required when QUALITY_WEB_ENABLED is true")
        self.search = search or self._search
        self.browse = browse or self._browse
        self.pdf_ocr = pdf_ocr
        self._default_page_fetch = partial(original_page, pdf_ocr=pdf_ocr)
        self.page_fetch = page_fetch or self._default_page_fetch
        self.usage_callback = usage_callback
        self.before_call = before_call
        self.counts = {}
        self.seen = {}
        self.diagnostics = []

    def _ocr_event(self, event, context, hook):
        entry = {**event, "item_id": context["item_id"], "tier": context["tier"], "phase": "ocr"}
        if hook == "usage_callback":
            self.diagnostics.append(entry)
        # The worker assigns run hooks after construction; use those live hooks
        # instead of charging both an adapter callback and the same run meter.
        callback = getattr(self, hook) or getattr(self.pdf_ocr, hook)
        if callback:
            return callback(entry)

    def _request(self, endpoint, body):
        if not self.api_key:
            raise ValueError("WEBIQ_API_KEY is required for enabled WebIQ requests")
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", endpoint, headers={"x-apikey": self.api_key, "Accept-Encoding": "identity"},
                               json=body) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    raise ValueError("WebIQ response must be JSON")
                raw = bytearray()
                for chunk in response.iter_bytes(4096):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError("WebIQ response exceeds its bound")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("WebIQ response must be an object")
                return payload, response.status_code

    def _search(self, query):
        payload, status = self._request(
            ENDPOINT, {"query": query, "maxResults": 3, "contentFormat": "passage", "maxLength": 2000},
        )
        results = payload.get("webResults")
        if status != 200 or "errorCode" in payload or not isinstance(results, list) or len(results) > 3:
            raise ValueError("WebIQ webResults must be a bounded list")
        # Retain only URLs for triage. Never promote generated passages to evidence.
        urls = []
        for entry in results:
            url = entry.get("url") if isinstance(entry, dict) else None
            if (not isinstance(url, str) or len(url) > 2048 or self.api_key in unquote(url)
                    or url == entry.get("clickUrl")):
                continue
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                urls.append(url)
        return urls

    def _browse(self, url):
        payload, status = self._request(BROWSE_ENDPOINT, {
            "url": url, "maxLength": BROWSE_MAX_LENGTH, "contentFormat": "text", "liveCrawl": "fallback",
            "includeWebLinks": False, "renderDynamicPages": False,
        })
        if status == 202 or payload.get("retryAfter"):
            raise BrowseUnavailable(
                "WebIQ Browse live crawl is pending; no automatic retry or verified page evidence.",
                status_code=status, retry_after=str(payload.get("retryAfter", ""))[:100],
            )
        content = payload.get("content")
        if (status != 200 or "errorCode" in payload or not isinstance(content, str)
                or not content.strip() or len(content) > BROWSE_MAX_LENGTH):
            raise BrowseUnavailable("WebIQ Browse returned no usable bounded content.", status_code=status)
        if payload.get("url") != url:
            raise BrowseUnavailable("WebIQ Browse response URL does not match the requested page.", status_code=status)
        return payload

    def _perform(self, operation, manifest, tier, function, *args):
        entry = {"operation": operation, "item_id": manifest.product.item_id, "tier": tier,
                 "phase": {"web_search": "search", "web_browse": "browse", "direct_page": "verify_page"}[operation],
                 "provider": "original_page" if operation == "direct_page" else "webiq"}
        if operation != "web_search":
            entry["source_url"] = args[0]
        if self.before_call:
            self.before_call(entry)
        try:
            if (operation == "direct_page" and function is self._default_page_fetch
                    and isinstance(self.pdf_ocr, CachedPDFOCR)):
                ocr = CachedPDFOCR(
                    self.pdf_ocr.store, self.pdf_ocr.parser,
                    usage_callback=lambda event: self._ocr_event(event, entry, "usage_callback"),
                    before_call=lambda event: self._ocr_event(event, entry, "before_call"),
                )
                value = original_page(*args, pdf_ocr=ocr)
            else:
                value = function(*args)
            entry["status"] = "succeeded"
            if operation == "web_browse":
                entry["content_chars"] = len(value["content"])
                entry["evidence_status"] = "provider_content_unverified"
            elif operation == "direct_page" and isinstance(value, PDFPageEvidence):
                entry["pdf"] = value.provenance()
            return value
        except CostLimitExceeded:
            entry.update(status="stopped", error_type="CostLimitExceeded")
            raise
        except Exception as error:
            entry.update(status="unavailable" if operation == "web_browse" else "failed", error_type=type(error).__name__)
            if isinstance(error, PDFPageError):
                entry.update(pdf=error.provenance, error_type=error.error_type)
            if isinstance(error, BrowseUnavailable):
                entry.update(reason=error.reason, http_status=error.status_code, retry_after=error.retry_after)
            elif isinstance(error, httpx.HTTPStatusError):
                entry["http_status"] = error.response.status_code
            if operation == "web_browse":
                entry.setdefault("reason", "WebIQ Browse API unavailable; no original-page evidence accepted for this request.")
            elif operation == "direct_page":
                entry["reason"] = "Independent original-page verification failed; provider content was not accepted as evidence."
            return None
        finally:
            self.diagnostics.append(entry)
            if self.usage_callback:
                self.usage_callback(entry)

    def load(self, manifest, tier, pending):
        from backend.quality_worker import _identity

        product = manifest.product
        key = product.item_id
        counts = self.counts.setdefault(key, {"search": 0, "browse": 0, "direct_page": 0})
        seen = self.seen.setdefault(key, set())
        family = next((name for name in MANUFACTURERS if name in product.vendor.casefold()), "")
        hosts = MANUFACTURERS.get(family, ())
        base = f'{family or product.vendor} "{product.mpn}"'
        groups = [pending[index:index + 4] for index in range(0, len(pending), 4)]
        queries = [base + " " + " ".join(group) for group in groups] or [base]
        if tier == "manufacturer_web" and hosts:
            queries = [query + " site:" + hosts[0] for query in queries]
        evidence = []
        tier_browse = 0
        for query in queries[:6]:
            if counts["search"] >= 12 or counts["browse"] >= 6:
                break
            counts["search"] += 1
            urls = self._perform("web_search", manifest, tier, self.search, query) or []
            urls = sorted(urls, key=lambda url: urlsplit(url).hostname not in hosts)
            for url in urls:
                if counts["browse"] >= 6 or tier_browse >= 3:
                    break
                host = urlsplit(url).hostname
                if url in seen or tier == "manufacturer_web" and host not in hosts or tier == "approved_web" and host in hosts:
                    continue
                if urlsplit(url).scheme != "https":
                    continue
                seen.add(url)
                counts["browse"] += 1
                tier_browse += 1
                browsed = self._perform("web_browse", manifest, tier, self.browse, url)
                if browsed is None:
                    continue
                counts["direct_page"] += 1
                page = self._perform("direct_page", manifest, tier, self.page_fetch, url)
                if page is None or not _identity(page.text, product.mpn):
                    continue
                source_id = "quality-web-" + hashlib.sha256(page.final_url.encode()).hexdigest()[:16]
                qualification = "WebIQ search and paid Browse followed by independent original-page retrieval; exact MPN present. Verify every attribute quotation against the original page, not provider content."
                if isinstance(page, PDFPageEvidence):
                    qualification += " PDF text remains untrusted; exact-product applicability is required. PDF provenance: " + json.dumps(page.provenance(), sort_keys=True)
                evidence.append(Evidence(
                    evidence_id=source_id + ":" + page.content_hash, source_id=source_id,
                    source_locator=page.final_url, source_version="sha256:" + page.content_hash,
                    source_tier=tier, content_kind="source_excerpt", text=page.text,
                    observed_at=datetime.now(timezone.utc), provider_retrieved_at=page.retrieved_at,
                    discovery_method="webiq",
                    qualification=qualification,
                    attribute_ids=pending,
                ))
            if tier_browse >= 3:
                break
        return evidence
