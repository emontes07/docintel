"""Web search abstraction for the tier-3/tier-4 source cascade (vendor website / other web).

Provides a single :class:`WebSearchClient` interface with swappable backends,
selected by ``settings.WEBSEARCH_PROVIDER``.

DATA HANDLING RULE
------------------
Legacy enrichment must never persist raw content/snippets. A separately authorized
private pilot may retain evidence under its own explicit storage policy; these
utilities themselves never persist anything. Search passages are discovery hints,
not verified quotations or authoritative attribute facts. Independently retrieved
pages still require product-identity and attribute-support validation.
"""

import logging
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import List, Literal, Optional, Protocol, Sequence, runtime_checkable
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from backend.core.config import settings

logger = logging.getLogger(__name__)


class WebSearchError(Exception):
    """Base class for all web search backend failures."""

    def __init__(self, message: str, *, code: str = "websearch_error"):
        super().__init__(message)
        self.code = code


class NotConfiguredError(WebSearchError):
    """The selected backend is missing required configuration."""


class ProviderUnavailableError(NotConfiguredError):
    """The selected provider has no supported integration in this application."""


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


class WebIQSearchResult(SearchResult):
    """Unverified discovery content; not an original-page retrieval."""

    content: str = Field(max_length=2000, repr=False)
    content_kind: Literal["provider_returned_passage_unverified"] = (
        "provider_returned_passage_unverified"
    )
    crawled_at: Optional[str] = None
    last_updated_at: Optional[str] = None
    original_source_verified: Literal[False] = False


@dataclass(frozen=True)
class OriginalPageEvidence:
    """Bounded original response, still untrusted and not an attribute fact.

    ``text`` is source plain text or normalized HTML text, not extracted attributes.
    HTML normalization is not browser rendering, CSS evaluation, or verification.
    ``content_hash`` is SHA-256 of the original response bytes, before decoding.
    """

    text: str = field(repr=False)
    final_url: str
    content_hash: str
    retrieved_at: datetime
    media_type: str
    evidence_kind: Literal["original_page_unverified"] = "original_page_unverified"
    text_normalization: Literal["decoded_plain_text", "html_visible_text_v1"] = "decoded_plain_text"
    byte_size: int = 0


class ExternalEvidenceError(WebSearchError):
    """A sanitized failure to retrieve an authorized original page."""


class _VisibleHTMLText(HTMLParser):
    """Conservative static text normalization; never render or load resources."""

    _DISCARD = frozenset({
        "script", "style", "template", "head", "nav", "noscript",
        "iframe", "object", "embed", "svg", "canvas",
    })
    _VOID = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })
    _BLOCK = frozenset({
        "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
        "section", "table", "tbody", "tfoot", "thead", "tr", "ul",
    })
    _HIDDEN_STYLE = re.compile(
        r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)"
        r"|content-visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)",
        re.IGNORECASE,
    )

    def __init__(self, max_characters: int):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._discarded: list[str] = []
        self._characters = 0
        self._max_characters = max_characters

    def _append(self, text: str) -> None:
        self._characters += len(text)
        if self._characters > self._max_characters:
            raise ExternalEvidenceError("Normalized source exceeds its text bound.", code="response_too_large")
        self._parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._discarded:
            if tag not in self._VOID:
                if len(self._discarded) >= 256:
                    raise ExternalEvidenceError("Source HTML nesting exceeds its bound.", code="invalid_content")
                self._discarded.append(tag)
            return
        hidden = any(
            name == "hidden"
            or (name == "aria-hidden" and (value or "").strip().lower() == "true")
            or (name == "style" and self._HIDDEN_STYLE.search(value or "") is not None)
            for name, value in attrs
        )
        if tag in self._BLOCK or tag == "br":
            self._append("\n")
        elif tag in ("td", "th"):
            self._append(" ")
        if (tag in self._DISCARD or hidden) and tag not in self._VOID:
            self._discarded.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._discarded:
            if tag not in self._discarded:
                return
            index = len(self._discarded) - 1 - self._discarded[::-1].index(tag)
            # Misnested discarded subtrees cannot safely reveal subsequent text.
            if any(nested in self._DISCARD for nested in self._discarded[index + 1:]):
                raise ExternalEvidenceError("Source HTML has inconsistent hidden structure.", code="invalid_content")
            del self._discarded[index:]
            if self._discarded:
                return
        if tag in self._BLOCK:
            self._append("\n")
        elif tag in ("td", "th"):
            self._append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self._discarded:
            self._append(re.sub(r"\s+", " ", data))

    def normalized_text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).splitlines())
        return "\n".join(line for line in lines if line)


def _visible_html_text(html: str, max_characters: int) -> str:
    try:
        parser = _VisibleHTMLText(max_characters)
        parser.feed(html)
        parser.close()
        return parser.normalized_text()
    except ExternalEvidenceError:
        raise
    except Exception:
        raise ExternalEvidenceError("Source HTML normalization failed.", code="invalid_content") from None


def exact_https_hosts(hosts: Sequence[str] | None) -> frozenset[str]:
    """Validate an explicit exact-host allowlist; wildcards/subdomains are not implied."""
    if not hosts or isinstance(hosts, (str, bytes)):
        raise ExternalEvidenceError("An exact HTTPS host allowlist is required.", code="invalid_allowed_hosts")
    normalized = set()
    for host in hosts:
        if not isinstance(host, str):
            raise ExternalEvidenceError("Invalid allowed host.", code="invalid_allowed_hosts")
        name = host.lower()
        if (
            len(name) > 253
            or "." not in name
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", name)
            or any(len(label) > 63 for label in name.split("."))
            or name.endswith((".localhost", ".local", ".internal"))
        ):
            raise ExternalEvidenceError("Invalid allowed host.", code="invalid_allowed_hosts")
        try:
            ipaddress.ip_address(name)
        except ValueError:
            normalized.add(name)
        else:
            raise ExternalEvidenceError("IP-literal hosts are not allowed.", code="invalid_allowed_hosts")
    return frozenset(normalized)


def validate_original_url(url: str, allowed_hosts: Sequence[str]) -> str:
    """Reject credentials, queries (including signed URLs), fragments and non-HTTPS URLs."""
    hosts = exact_https_hosts(allowed_hosts)
    try:
        if not isinstance(url, str) or len(url) > 2048 or not url.isascii():
            raise ValueError()
        decoded = unquote(url)
        if any(ord(char) <= 32 or ord(char) == 127 for char in decoded) or "\\" in decoded:
            raise ValueError()
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or "?" in url
            or "#" in url
            or "%" in parsed.netloc
            or parsed.netloc.lower() not in (parsed.hostname, f"{parsed.hostname}:443")
        ):
            raise ValueError()
        return urlunsplit(("https", parsed.hostname, parsed.path or "/", "", ""))
    except (ValueError, TypeError):
        raise ExternalEvidenceError("URL is outside the approved HTTPS source boundary.", code="unsafe_url") from None


def _public_addresses(host: str) -> list[tuple]:
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ExternalEvidenceError("No public source address was found.", code="unsafe_address")
    for family, _socktype, _protocol, _canonname, sockaddr in addresses:
        address = ipaddress.ip_address(sockaddr[0])
        if (
            family not in (socket.AF_INET, socket.AF_INET6)
            or not address.is_global
            or address.is_multicast
            or address.is_reserved
            or (isinstance(address, ipaddress.IPv6Address) and (
                address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None
            ))
        ):
            raise ExternalEvidenceError("Source DNS includes a non-public address.", code="unsafe_address")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to one validated numeric IP without a second DNS lookup."""

    def __init__(self, host: str, address: tuple, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        family, socktype, protocol, _canonname, sockaddr = self._address
        raw = socket.socket(family, socktype, protocol)
        try:
            raw.settimeout(self.timeout)
            raw.connect(sockaddr)
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(sockaddr[0]):
                raise ExternalEvidenceError("Source peer address changed.", code="unsafe_address")
            # Preserve the original hostname for SNI and certificate verification.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch_original_page(
    url: str,
    *,
    allowed_hosts: Sequence[str],
    authorized: bool = False,
    timeout: float = 15.0,
    max_bytes: int = 262144,
) -> OriginalPageEvidence:
    """Retrieve one approved page, with no retries, redirects, proxies or credentials.

    All DNS answers must be public and the single connection is IP-pinned. Socket
    phase timeouts are not a total deadline; system DNS resolution has its own
    resolver timeout. Only identity-encoded UTF-8/ASCII/Latin-1/CP1252 text or HTML
    is accepted. Oversize bodies fail rather than becoming partial evidence.
    HTML is normalized to static text with paragraph/row boundaries; it is not
    browser-rendered, and normalized line ordinals are not original coordinates.
    """
    if authorized is not True:
        raise ExternalEvidenceError("Explicit source retrieval authorization is required.", code="authorization_required")
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not 0 < timeout <= 15
        or type(max_bytes) is not int or not 1 <= max_bytes <= 262144
    ):
        raise ExternalEvidenceError("Invalid source retrieval limits.", code="invalid_limits")
    final_url = validate_original_url(url, allowed_hosts)
    parsed = urlsplit(final_url)
    connection = None
    response = None
    try:
        host = parsed.hostname
        assert host is not None
        address = _public_addresses(host)[0]
        connection = _PinnedHTTPSConnection(host, address, timeout)
        connection.request("GET", parsed.path or "/", headers={
            "Accept": "text/plain, text/html",
            "Accept-Encoding": "identity",
            "User-Agent": "DocIntel-Authorized-Evidence/1.0",
        })
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ExternalEvidenceError("Source redirect was not followed.", code="redirect_not_followed")
        if response.status != 200:
            raise ExternalEvidenceError("Source returned an unsuccessful status.", code="http_error")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ExternalEvidenceError("Encoded source bodies are unsupported.", code="unsupported_content_encoding")
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in ("text/plain", "text/html"):
            raise ExternalEvidenceError("Only original text or HTML is supported.", code="unsupported_content_type")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > max_bytes):
            raise ExternalEvidenceError("Source exceeds its byte bound or has an invalid length.", code="response_too_large")
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ExternalEvidenceError("Source exceeds its byte bound.", code="response_too_large")
        if not raw or (length is not None and len(raw) != int(length)):
            raise ExternalEvidenceError("Source body is empty or incomplete.", code="invalid_content")
        charset = (response.headers.get_content_charset() or "utf-8").lower()
        if charset not in ("utf-8", "us-ascii", "ascii", "iso-8859-1", "latin-1", "windows-1252"):
            raise ExternalEvidenceError("Unsupported source text encoding.", code="invalid_content")
        text = raw.decode(charset, errors="strict")
        if "\x00" in text:
            raise ExternalEvidenceError("Source body is not usable text.", code="invalid_content")
        if content_type == "text/html":
            text = _visible_html_text(text, max_bytes)
        if "\x00" in text or not text.strip():
            raise ExternalEvidenceError("Source body is not usable text.", code="invalid_content")
        return OriginalPageEvidence(
            text=text, final_url=final_url, content_hash=hashlib.sha256(raw).hexdigest(),
            retrieved_at=datetime.now(timezone.utc), media_type=content_type,
            text_normalization="html_visible_text_v1" if content_type == "text/html" else "decoded_plain_text",
            byte_size=len(raw),
        )
    except ExternalEvidenceError:
        raise
    except (TimeoutError, socket.timeout):
        raise ExternalEvidenceError("Source retrieval timed out.", code="timeout") from None
    except UnicodeError:
        raise ExternalEvidenceError("Invalid source text encoding.", code="invalid_content") from None
    except Exception:
        raise ExternalEvidenceError("Source retrieval failed.", code="transport_error") from None
    finally:
        for resource in (response, connection):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


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
