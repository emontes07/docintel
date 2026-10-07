"""WebIQ discovery followed by independent, bounded original-page browsing.

Search uses the repository-documented WebIQ POST /v3/search/web contract.
Browse here means an independent original-page GET, not an undocumented paid
WebIQ /browse endpoint. Provider search passages can never become evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

import httpx

from backend.core.config import settings
from backend.core.websearch import (
    OriginalPageEvidence, _PinnedHTTPSConnection, _public_addresses, _visible_html_text,
    validate_original_url,
)
from backend.models.enrichment import Evidence

ENDPOINT = "https://api.microsoft.ai/v3/search/web"
MANUFACTURERS = {
    "ford": ("fordmeterbox.com", "www.fordmeterbox.com"),
    "mueller": ("muellercompany.com", "www.muellercompany.com", "muellerwaterproducts.com", "www.muellerwaterproducts.com"),
}


def original_page(url: str) -> OriginalPageEvidence:
    """Public DNS and a pinned socket, no proxy, redirects, credentials or DI."""
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
        media = response.getheader("Content-Type", "").split(";", 1)[0].lower()
        if media not in {"text/html", "text/plain"}:
            raise ValueError("Original page is not HTML/text; provide cached text for PDF sources (DI disabled)")
        raw = response.read(262145)
        if not raw or len(raw) > 262144:
            raise ValueError("Original page empty or exceeds 256 KiB")
        charset = (response.headers.get_content_charset() or "utf-8").lower()
        if charset not in {"utf-8", "ascii", "us-ascii", "iso-8859-1", "latin-1", "windows-1252"}:
            raise ValueError("Unsupported original-page encoding")
        text = raw.decode(charset)
        if media == "text/html":
            text = _visible_html_text(text, 262144)
        if not text.strip() or "\x00" in text:
            raise ValueError("Original page contains no usable text")
        return OriginalPageEvidence(
            text=text, final_url=url, content_hash=hashlib.sha256(raw).hexdigest(),
            retrieved_at=datetime.now(timezone.utc), media_type=media, byte_size=len(raw),
            text_normalization="html_visible_text_v1" if media == "text/html" else "decoded_plain_text",
        )
    finally:
        connection.close()


class QualityWeb:
    def __init__(self, *, api_key=None, search=None, browse=None, usage_callback=None, before_call=None):
        self.api_key = api_key if api_key is not None else os.environ.get("WEBIQ_API_KEY") or settings.WEBIQ_API_KEY
        if search is None and not self.api_key:
            raise ValueError("WEBIQ_API_KEY is required when QUALITY_WEB_ENABLED is true")
        self.search = search or self._search
        self.browse = browse or original_page
        self.usage_callback = usage_callback
        self.before_call = before_call
        self.counts = {}
        self.seen = {}
        self.diagnostics = []

    def _search(self, query):
        if not self.api_key:
            raise ValueError("WEBIQ_API_KEY is required for enabled WebIQ discovery")
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", ENDPOINT, headers={"x-apikey": self.api_key, "Accept-Encoding": "identity"},
                               json={"query": query, "maxResults": 3, "contentFormat": "passage", "maxLength": 2000}) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    raise ValueError("WebIQ response must be JSON")
                raw = bytearray()
                for chunk in response.iter_bytes(4096):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError("WebIQ response exceeds its bound")
                payload = json.loads(raw)
        results = payload.get("webResults")
        if "errorCode" in payload or not isinstance(results, list) or len(results) > 3:
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

    def _perform(self, operation, manifest, tier, function, *args):
        entry = {"operation": operation, "item_id": manifest.product.item_id, "tier": tier,
                 "phase": "search" if operation == "web_search" else "browse", "provider": "webiq" if operation == "web_search" else "original_page"}
        if self.before_call:
            self.before_call(entry)
        try:
            value = function(*args)
            entry["status"] = "succeeded"
            return value
        except Exception as error:
            entry.update(status="failed", error=type(error).__name__)
            return None
        finally:
            self.diagnostics.append(entry)
            if self.usage_callback:
                self.usage_callback(entry)

    def load(self, manifest, tier, pending):
        from backend.quality_worker import _identity

        product = manifest.product
        key = product.item_id
        counts = self.counts.setdefault(key, {"search": 0, "browse": 0})
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
                page = self._perform("web_browse", manifest, tier, self.browse, url)
                if page is None or not _identity(page.text, product.mpn):
                    continue
                source_id = "quality-web-" + hashlib.sha256(page.final_url.encode()).hexdigest()[:16]
                evidence.append(Evidence(
                    evidence_id=source_id + ":" + page.content_hash, source_id=source_id,
                    source_locator=page.final_url, source_version="sha256:" + page.content_hash,
                    source_tier=tier, content_kind="source_excerpt", text=page.text,
                    observed_at=datetime.now(timezone.utc), provider_retrieved_at=page.retrieved_at,
                    discovery_method="webiq", qualification="Independently retrieved original page; exact MPN present. Verify every attribute quotation.",
                    attribute_ids=pending,
                ))
            if tier_browse >= 3:
                break
        return evidence
