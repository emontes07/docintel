"""Mocked WebIQ contract; no provider requests."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.core.websearch import OriginalPageEvidence
from backend.quality_web import BROWSE_ENDPOINT, BROWSE_MAX_LENGTH, ENDPOINT, QualityWeb
from tests.test_quality_pipeline import manifest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("No network is allowed"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("No DNS is allowed"))


def page(url, text):
    return OriginalPageEvidence(text=text, final_url=url, content_hash="b"*64,
                                retrieved_at=datetime.now(timezone.utc), media_type="text/html")


def test_webiq_documented_contract_and_no_search_passage_as_evidence(monkeypatch):
    calls = []
    def handle(request):
        calls.append(request)
        assert str(request.url) == ENDPOINT
        assert request.headers["x-apikey"] == "synthetic"
        assert json.loads(request.content) == {"query": "Ford public product", "maxResults": 3, "contentFormat": "passage", "maxLength": 2000}
        return httpx.Response(200, json={"webResults": [{"url": "https://fordmeterbox.com/product", "title": "Ford", "content": "unverified invented value"}]})
    client_class = httpx.Client
    monkeypatch.setattr("backend.quality_web.httpx.Client", lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs))
    client = QualityWeb(api_key="synthetic")
    assert client._search("Ford public product") == ["https://fordmeterbox.com/product"]
    assert len(calls) == 1


def test_manufacturer_first_independent_page_required_and_combined_caps():
    searches, browses, pages, usage = [], [], [], []
    def search(query):
        searches.append(query)
        return ["https://fordmeterbox.com/product" + str(len(searches)),
                "https://supplier.example/product" + str(len(searches))]
    def browse(url):
        browses.append(url)
        return {"url": url, "content": "Provider-returned content is not source evidence"}
    def fetch(url):
        assert url in browses
        pages.append(url)
        return page(url, manifest().product.mpn + " Original source: 100 PSI")
    client = QualityWeb(search=search, browse=browse, page_fetch=fetch, usage_callback=usage.append)
    first = client.load(manifest(), "manufacturer_web", ["Pressure"])
    assert first and all(e.source_locator.startswith("https://fordmeterbox.com/") for e in first)
    assert first[0].provider_retrieved_at and "Original source" in first[0].text
    for _ in range(20):
        client.load(manifest(), "approved_web", ["Pressure"])
    assert len(searches) <= 12 and len(browses) <= 6 and len(pages) == len(browses)
    assert "site:fordmeterbox.com" in searches[0]
    assert usage and usage[0]["operation"] == "web_search"
    assert [entry["operation"] for entry in usage[:3]] == ["web_search", "web_browse", "direct_page"]
    assert usage[1]["provider"] == "webiq" and usage[2]["provider"] == "original_page"


def test_discovery_without_independent_matching_product_has_no_evidence():
    client = QualityWeb(search=lambda _: ["https://fordmeterbox.com/product"],
                        browse=lambda url: {"url": url, "content": manifest().product.mpn},
                        page_fetch=lambda url: page(url, "Different product AV11-444W-NL, pressure 900 PSI"))
    assert client.load(manifest(), "manufacturer_web", ["Pressure"]) == []


def test_paid_browse_documented_contract_then_separate_verified_page(monkeypatch):
    calls, usage = [], []
    url = "https://fordmeterbox.com/product"
    def handle(request):
        calls.append(request)
        assert request.headers["x-apikey"] == "synthetic"
        if str(request.url) == ENDPOINT:
            return httpx.Response(200, json={"webResults": [{"url": url}]})
        assert str(request.url) == BROWSE_ENDPOINT
        assert json.loads(request.content) == {
            "url": url, "maxLength": BROWSE_MAX_LENGTH, "contentFormat": "text", "liveCrawl": "fallback",
            "includeWebLinks": False, "renderDynamicPages": False,
        }
        return httpx.Response(200, json={"url": url, "content": manifest().product.mpn + " Unverified pressure 900 PSI"})
    client_class = httpx.Client
    monkeypatch.setattr("backend.quality_web.httpx.Client", lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs))
    def fetch(requested_url):
        assert len(calls) == 2 and str(calls[-1].url) == BROWSE_ENDPOINT
        assert requested_url == url
        return page(url, manifest().product.mpn + " Original pressure 100 PSI")
    client = QualityWeb(api_key="synthetic", page_fetch=fetch, usage_callback=usage.append)
    evidence = client.load(manifest(), "manufacturer_web", ["Pressure"])
    assert len(evidence) == 1 and "100 PSI" in evidence[0].text and "900 PSI" not in evidence[0].text
    assert evidence[0].provider_retrieved_at and evidence[0].source_locator == url
    assert [entry["operation"] for entry in usage] == ["web_search", "web_browse", "direct_page"]
    assert usage[1]["evidence_status"] == "provider_content_unverified"
    assert all(entry["status"] == "succeeded" for entry in usage)


@pytest.mark.parametrize("status,payload", [
    (202, {"retryAfter": "10"}),
    (403, {"errorCode": "ServiceNotAllowed"}),
    (404, {"errorCode": "NotFound"}),
    (430, {"errorCode": "TooManyOnDemandCrawls"}),
    (503, {"errorCode": "ServiceUnavailable"}),
    (200, {"url": "https://fordmeterbox.com/product", "content": ""}),
    (200, {"url": "https://fordmeterbox.com/product", "content": "x" * (BROWSE_MAX_LENGTH + 1)}),
    (200, {"url": "https://supplier.example/other", "content": "Other page"}),
])
def test_unavailable_browse_is_recorded_paid_once_without_retry_or_unverified_fallback(monkeypatch, status, payload):
    from backend.quality_cost import QualityCostMeter
    from tests.test_quality_pipeline import Store
    calls, usage = [], []
    url = "https://fordmeterbox.com/product"
    meter = QualityCostMeter(Store(), "batch", "run")
    def handle(request):
        calls.append(request)
        return httpx.Response(status, json=payload)
    client_class = httpx.Client
    monkeypatch.setattr("backend.quality_web.httpx.Client", lambda **kwargs: client_class(transport=httpx.MockTransport(handle), **kwargs))
    def record(entry):
        usage.append(meter.record(entry))
    client = QualityWeb(
        api_key="synthetic", search=lambda _: [url],
        page_fetch=lambda _: pytest.fail("Unavailable Browse must not masquerade as an independent browse success"),
        usage_callback=record, before_call=meter.before_call,
    )
    assert client.load(manifest(), "manufacturer_web", ["Pressure"]) == []
    assert len(calls) == 1 and str(calls[0].url) == BROWSE_ENDPOINT
    assert usage[-1]["operation"] == "web_browse" and usage[-1]["status"] == "unavailable"
    assert usage[-1]["cost_usd"] == .0125 and usage[-1]["reason"]
    assert usage[-1]["http_status"] == status
    assert meter.summary()["searches"] == meter.summary()["browses"] == 1
    assert meter.summary()["web_cost_usd"] == .025


def test_successful_browse_cannot_substitute_for_failed_free_original_page():
    from backend.quality_cost import QualityCostMeter
    from tests.test_quality_pipeline import Store
    usage = []
    meter = QualityCostMeter(Store(), "batch", "run")
    def fetch(_):
        raise ValueError("Original page unavailable")
    def record(entry):
        usage.append(meter.record(entry))
    client = QualityWeb(
        search=lambda _: ["https://fordmeterbox.com/product"],
        browse=lambda url: {"url": url, "content": manifest().product.mpn + " pressure 900 PSI"},
        page_fetch=fetch, usage_callback=record, before_call=meter.before_call,
    )
    assert client.load(manifest(), "manufacturer_web", ["Pressure"]) == []
    assert [entry["cost_usd"] for entry in usage] == [.0125, .0125, 0]
    assert usage[-1]["operation"] == "direct_page" and usage[-1]["status"] == "failed"
