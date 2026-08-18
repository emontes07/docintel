"""Web search abstraction for the tier-3/tier-4 source cascade (vendor website / other web).

Provides a single :class:`WebSearchClient` interface with swappable backends,
selected by ``settings.WEBSEARCH_PROVIDER``.

DATA HANDLING RULE
------------------
Callers must never persist the raw Content/snippet returned here into Cosmos or
any other store — only derived, validated attribute values with a source URL for
provenance. This applies to all backends.

:class:`SearchResult` deliberately carries no field for full page or raw content:
it holds only enough to locate and cite a source.
"""

import logging
from datetime import datetime
from typing import List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from backend.core.config import settings

logger = logging.getLogger(__name__)


class WebSearchError(Exception):
    """Base class for all web search backend failures."""


class NotConfiguredError(WebSearchError):
    """The selected backend is missing required configuration."""


class SearchResult(BaseModel):
    """A single citable web source.

    Intentionally minimal: enough to locate and attribute a source, and nothing
    that could be mistaken for storable page content.
    """

    url: str = Field(description="Canonical URL of the source")
    title: str = Field(default="", description="Human-readable title, may be empty")
    snippet: str = Field(
        default="",
        description="Short excerpt for relevance triage only. Never persist this.",
    )
    retrieved_at: datetime = Field(description="When this result was retrieved")


@runtime_checkable
class WebSearchClient(Protocol):
    """Interface every web search backend implements."""

    def search(
        self, query: str, allowed_domains: Optional[List[str]] = None
    ) -> List[SearchResult]:
        """Run a web search and return citable results.

        Args:
            query: Free-text search query.
            allowed_domains: If provided, restrict results to these domains.
                Backends that cannot filter server-side must filter client-side.

        Raises:
            NotConfiguredError: Required configuration or dependencies are missing.
            WebSearchError: The backend failed to complete the search.
        """
        ...


def get_websearch_client() -> WebSearchClient:
    """Return the backend named by ``settings.WEBSEARCH_PROVIDER``.

    Backend modules are imported lazily so that an unconfigured or
    uninstallable provider does not break application import.
    """
    provider = (settings.WEBSEARCH_PROVIDER or "").strip().lower()

    if provider == "bing":
        from backend.core.websearch_bing import FoundryWebSearchClient

        return FoundryWebSearchClient()
    if provider == "webiq":
        from backend.core.websearch_webiq import WebIQSearchClient

        return WebIQSearchClient()

    raise NotConfiguredError(
        f"Unknown WEBSEARCH_PROVIDER {provider!r}; expected 'bing' or 'webiq'"
    )


def filter_by_domain(
    results: List[SearchResult], allowed_domains: Optional[List[str]]
) -> List[SearchResult]:
    """Client-side domain filter, for backends without server-side support."""
    if not allowed_domains:
        return results

    from urllib.parse import urlparse

    wanted = {d.strip().lower().lstrip(".") for d in allowed_domains if d.strip()}
    if not wanted:
        return results

    kept: List[SearchResult] = []
    for result in results:
        host = (urlparse(result.url).hostname or "").lower()
        if any(host == d or host.endswith(f".{d}") for d in wanted):
            kept.append(result)
    return kept
