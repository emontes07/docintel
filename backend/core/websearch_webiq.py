"""WebIQ web search backend.

PENDING APPROVAL — the WebIQ API is not yet approved for use by this
application; access is awaiting Core & Main's sign-off. The request/response
shape below is scaffolded from the expected contract and must be re-checked
against the real API before first use.

Configuration comes from ``WEBIQ_ENDPOINT`` and ``WEBIQ_API_KEY``. Both are blank
by default, so :meth:`WebIQSearchClient.search` raises
:class:`~backend.core.websearch.NotConfiguredError` rather than failing silently
or issuing an unauthenticated request.

The rule in ``websearch.py`` applies here too: never persist the returned
snippet — only derived, validated attribute values plus the source URL.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from backend.core.config import settings
from backend.core.websearch import (
    NotConfiguredError,
    SearchResult,
    WebSearchError,
    filter_by_domain,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_RESULT_COUNT = 5


class WebIQSearchClient:
    """Web search backed by the WebIQ API."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.endpoint = endpoint or settings.WEBIQ_ENDPOINT
        self.api_key = api_key or settings.WEBIQ_API_KEY
        self.timeout = timeout

    def _require_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("WEBIQ_ENDPOINT", self.endpoint),
                ("WEBIQ_API_KEY", self.api_key),
            )
            if not value
        ]
        if missing:
            raise NotConfiguredError(
                "WebIQ is not configured (pending Core & Main access approval); "
                "missing: " + ", ".join(missing)
            )

    def search(
        self, query: str, allowed_domains: Optional[List[str]] = None
    ) -> List[SearchResult]:
        self._require_config()

        # TODO: confirm against the real WebIQ contract once access is approved —
        # request field names, auth header, and response envelope are provisional.
        payload: Dict[str, Any] = {
            "query": query,
            "count": DEFAULT_RESULT_COUNT,
        }
        if allowed_domains:
            payload["domains"] = allowed_domains

        try:
            response = httpx.post(
                str(self.endpoint).rstrip("/") + "/search",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise WebSearchError(f"WebIQ search failed: {exc}") from exc

        results = self._parse(body)

        # Server-side filtering is requested above; re-apply client-side so the
        # contract holds even if the API ignores the `domains` field.
        return filter_by_domain(results, allowed_domains)

    @staticmethod
    def _parse(body: Dict[str, Any]) -> List[SearchResult]:
        retrieved_at = datetime.now(timezone.utc)
        results: List[SearchResult] = []

        for item in body.get("results", []) or []:
            url = item.get("url") or ""
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title") or "",
                    snippet=item.get("snippet") or "",
                    retrieved_at=retrieved_at,
                )
            )

        return results
