"""Mocked WebIQ contract; no provider requests."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.core.websearch import OriginalPageEvidence
from backend.quality_web import ENDPOINT, QualityWeb
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
    searches, browses, usage = [], [], []
    def search(query):
        searches.append(query)
        return ["https://fordmeterbox.com/product" + str(len(searches)),
                "https://supplier.example/product" + str(len(searches))]
    def browse(url):
        browses.append(url)
        return page(url, manifest().product.mpn + " Original source: 100 PSI")
    client = QualityWeb(search=search, browse=browse, usage_callback=usage.append)
    first = client.load(manifest(), "manufacturer_web", ["Pressure"])
    assert first and all(e.source_locator.startswith("https://fordmeterbox.com/") for e in first)
    assert first[0].provider_retrieved_at and "Original source" in first[0].text
    for _ in range(20):
        client.load(manifest(), "approved_web", ["Pressure"])
    assert len(searches) <= 12 and len(browses) <= 6
    assert "site:fordmeterbox.com" in searches[0]
    assert usage and usage[0]["operation"] == "web_search"


def test_discovery_without_independent_matching_product_has_no_evidence():
    client = QualityWeb(search=lambda _: ["https://fordmeterbox.com/product"],
                        browse=lambda url: page(url, "Different product AV11-444W-NL, pressure 900 PSI"))
    assert client.load(manifest(), "manufacturer_web", ["Pressure"]) == []
