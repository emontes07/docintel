"""Opt-in bounded WebIQ discovery, separate from original-page evidence retrieval.

Contract: https://webiq.microsoft.ai/llms-full.txt (Web Search).
Passages are model-selected query-contextual extracts, not verified quotations.
This adapter performs no extraction, persistence, instrumentation or fallback.
"""

from datetime import datetime, timezone
import json
from typing import List, Optional
from urllib.parse import quote, urlsplit

import httpx

from backend.core.config import settings
from backend.core.websearch import (
    ExternalEvidenceError,
    NotConfiguredError,
    SearchResult,
    WebIQSearchResult,
    WebSearchError,
    exact_https_hosts,
    validate_original_url,
)

ENDPOINT = "https://api.microsoft.ai/v3/search/web"
DEFAULT_TIMEOUT = 15.0
DEFAULT_RESULT_COUNT = 3
MAX_RESPONSE_BYTES = 65536


def _malformed() -> WebSearchError:
    return WebSearchError("WebIQ returned an incompatible response.", code="malformed_response")


def _url(value: object) -> str:
    """Match the standalone diagnostic's required HTTP(S) URL validation."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise _malformed()
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        parsed.port
    except ValueError:
        raise _malformed() from None
    return value


def _timestamp(item: dict, name: str) -> str | None:
    """Keep absent/null/empty provider dates distinct from local retrieval time."""
    value = item.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _malformed()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _malformed() from None
    return value


class WebIQSearchClient:
    """One explicitly authorized public query; no implicit pilot/customer opt-in."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        max_results: int = DEFAULT_RESULT_COUNT,
        max_length: int = 2000,
    ):
        self.endpoint = endpoint if endpoint is not None else settings.WEBIQ_ENDPOINT
        self.api_key = api_key if api_key is not None else settings.WEBIQ_API_KEY
        self.timeout = timeout
        self.max_results = max_results
        self.max_length = max_length

    def _require_config(self) -> None:
        if not self.endpoint or not self.api_key or not self.api_key.strip():
            raise NotConfiguredError("WebIQ endpoint and API key are required.", code="not_configured")
        if self.endpoint != ENDPOINT:
            raise NotConfiguredError("WebIQ requires the approved REST endpoint.", code="invalid_endpoint")
        if (
            not self.api_key.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in self.api_key)
        ):
            raise NotConfiguredError("WebIQ API key is invalid.", code="not_configured")
        if (
            isinstance(self.timeout, bool) or not isinstance(self.timeout, (float, int))
            or not 0 < self.timeout <= 15
            or type(self.max_results) is not int or not 1 <= self.max_results <= 3
            or type(self.max_length) is not int or not 1 <= self.max_length <= 2000
        ):
            raise NotConfiguredError("WebIQ request limits are invalid.", code="invalid_limits")

    def search(
        self, query: str, allowed_domains: Optional[List[str]] = None,
        *, authorized: bool = False,
    ) -> List[SearchResult]:
        """Discover sources using caller-approved public vendor/MPN/attribute terms.

        The caller owns public-term selection, unresolved-only orchestration and
        budget reservation. Authorization is required on every call, even when
        environment configuration is present. Approved domains are exact hosts;
        subdomains must be listed explicitly. No source page is followed here.
        """
        if authorized is not True:
            raise WebSearchError("Explicit WebIQ query authorization is required.", code="authorization_required")
        self._require_config()
        hosts = exact_https_hosts(allowed_domains)
        if (
            not isinstance(query, str) or not query.strip() or len(query) > 1000
            or any(ord(char) < 32 or ord(char) == 127 for char in query)
            or self.api_key in query
        ):
            raise WebSearchError("Supply only authorized public search terms.", code="invalid_query")
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=False,
                trust_env=False,
                transport=httpx.HTTPTransport(retries=0, trust_env=False),
            ) as client:
                with client.stream(
                    "POST", ENDPOINT,
                    headers={"x-apikey": self.api_key, "Accept-Encoding": "identity"},
                    json={
                        "query": query, "maxResults": self.max_results,
                        "contentFormat": "passage", "maxLength": self.max_length,
                    },
                ) as response:
                    code = response.status_code
                    status = {
                        401: "authentication_failed", 403: "permission_denied",
                        429: "throttled", 408: "timeout", 504: "timeout",
                    }.get(code)
                    if status:
                        raise WebSearchError("WebIQ request was unsuccessful.", code=status)
                    if 300 <= code < 400:
                        raise WebSearchError("WebIQ redirect was not followed.", code="redirect_not_followed")
                    if code != 200:
                        raise WebSearchError("WebIQ returned an unsuccessful status.", code="http_error")
                    if (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
                        or response.headers.get("content-encoding", "identity").lower() != "identity"
                    ):
                        raise _malformed()
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=4096):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise WebSearchError("WebIQ response exceeded its byte bound.", code="response_too_large")
                    try:
                        payload = json.loads(body)
                    except (ValueError, UnicodeError, RecursionError):
                        raise _malformed() from None
                    return self._parse(payload, hosts)
        except WebSearchError:
            raise
        except httpx.TimeoutException:
            raise WebSearchError("WebIQ request timed out.", code="timeout") from None
        except httpx.RequestError:
            raise WebSearchError("WebIQ transport failed.", code="transport_error") from None
        except Exception:
            raise WebSearchError("WebIQ response processing failed.", code="response_processing_error") from None

    def _parse(self, body: object, hosts: frozenset[str]) -> List[SearchResult]:
        """Consume the same documented fields as scripts/webiq_diagnostic.py.

        The standalone script is intentionally not a backend runtime dependency.
        Cross-contract tests enforce agreement; the pilot adds stricter byte,
        content-length and exact HTTPS source boundaries.
        """
        if not isinstance(body, dict) or "errorCode" in body:
            raise _malformed()
        items = body.get("webResults")
        if not isinstance(items, list) or len(items) > self.max_results:
            raise _malformed()
        for name in ("instrumentationClickBase", "instrumentationCitationBase"):
            if body.get(name) is not None:
                _url(body[name])
        now = datetime.now(timezone.utc)
        results: List[SearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                raise _malformed()
            if not isinstance(item.get("title"), str) or not isinstance(item.get("content"), str):
                raise _malformed()
            url = _url(item.get("url"))
            if len(item["content"]) > self.max_length or len(item["title"]) > 2000 or len(url) > 2048:
                raise _malformed()
            if any(secret in value for secret in (self.api_key, quote(self.api_key or "", safe=""))
                   if secret for value in (url, item["title"], item["content"])):
                raise _malformed()
            crawled_at = _timestamp(item, "crawledAt")
            last_updated_at = _timestamp(item, "lastUpdatedAt")
            if item.get("clickUrl") is not None:
                _url(item["clickUrl"])
            if item.get("instrumentationSuffix") is not None and not isinstance(item["instrumentationSuffix"], str):
                raise _malformed()
            try:
                safe_url = validate_original_url(url, tuple(hosts))
            except ExternalEvidenceError:
                continue
            # Redirect/instrumentation locations never substitute for source URLs.
            if item.get("clickUrl") == url:
                continue
            results.append(WebIQSearchResult(
                url=safe_url, title=item["title"], snippet="",
                content=item["content"], retrieved_at=now,
                crawled_at=crawled_at, last_updated_at=last_updated_at,
            ))
        return results
