"""Mocked WebIQ contract; no provider requests."""

import json
import hashlib
import socket
import subprocess
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from unittest.mock import Mock

import httpx
import pytest

from backend.core.websearch import ExternalEvidenceError, OriginalPageEvidence
from backend.core.docintel import ParsedDocument
from backend.quality_pdf import CachedPDFOCR, PDF_MAX_BYTES, TEXT_MAX_BYTES, PDFPageError, PDFPageEvidence, pdf_text
from backend.quality_web import BROWSE_ENDPOINT, BROWSE_MAX_LENGTH, ENDPOINT, QualityWeb, original_page
from tests.test_quality_pipeline import Store, manifest


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


def synthetic_pdf(pages=1, text=""):
    """Minimal real PDF; no fixtures, documents, or scratch files are downloaded."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Count {pages} /Kids [{' '.join(f'{4 + 2*i} 0 R' for i in range(pages))}] >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2*index} 0 R >>".encode()
        )
        stream = f"BT /F1 12 Tf 10 170 Td ({text}) Tj ET".encode() if text else b""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    startxref = len(content)
    content.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode())
    return bytes(content)


@pytest.fixture
def original_response(monkeypatch):
    connections = []
    response = None
    address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [address])

    class Connection:
        def __init__(self, host, pinned, timeout):
            assert pinned == address and timeout == 15
            self.host, self.closed, self.requests = host, False, []
            connections.append(self)

        def request(self, method, path, *, headers):
            self.requests.append((method, path, headers))

        def getresponse(self):
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr("backend.quality_web._PinnedHTTPSConnection", Connection)

    def configure(raw, media="application/pdf", status=200, encoding="identity"):
        nonlocal response
        headers = Message()
        headers["Content-Type"] = media
        response = Mock(status=status, headers=headers)
        response.getheader.side_effect = lambda name, default=None: {
            "Content-Type": media, "Content-Encoding": encoding,
        }.get(name, default)
        response.read.side_effect = BytesIO(raw).read
        return response, connections

    return configure


class OCRStore(Store):
    def lease(self, key):
        pytest.fail("OCR must not acquire a lease or create an admission reservation")


def ocr_parser(text="AV11-333W-NL pressure 100 PSI"):
    parser = Mock()

    def extract(content, *, source, page_limit):
        parser.last_page_count = page_limit
        return ParsedDocument(
            source=source, cache_key="sha256:" + hashlib.sha256(content).hexdigest(),
            parsed_at=datetime(2026, 10, 7, tzinfo=timezone.utc), raw_text=text,
        )

    parser.extract_pdf_bytes.side_effect = extract
    return parser


@pytest.mark.parametrize("host", [
    "muellercompany.com", "www.muellercompany.com", "muellerwaterproducts.com",
    "www.muellerwaterproducts.com", "fordmeterbox.com", "www.fordmeterbox.com",
])
def test_text_pdf_manufacturer_allowlist_uses_local_text_no_di(original_response, host):
    raw = synthetic_pdf(text="AV11-333W-NL pressure 100 PSI")
    response, connections = original_response(raw)
    ocr = Mock(side_effect=AssertionError("Text PDFs must never consult DI or its cache"))
    before = datetime.now(timezone.utc)
    result = original_page(f"https://{host}/catalog.pdf", pdf_ocr=ocr)
    assert isinstance(result, PDFPageEvidence)
    assert "AV11-333W-NL pressure 100 PSI" in result.text
    assert result.evidence_kind == "original_page_unverified"
    assert result.text_normalization == "decoded_plain_text"
    assert result.pdf_page_count == 1 and result.pdf_extraction == "pdftotext"
    assert result.content_hash == hashlib.sha256(raw).hexdigest() and result.byte_size == len(raw)
    assert result.final_url == f"https://{host}/catalog.pdf"
    assert before <= result.retrieved_at <= datetime.now(timezone.utc)
    response.read.assert_called_once_with(PDF_MAX_BYTES + 1)
    assert connections[0].host == host and connections[0].closed
    assert connections[0].requests == [("GET", "/catalog.pdf", {
        "Accept-Encoding": "identity", "User-Agent": "DocIntel-quality/1.0",
    })]
    ocr.assert_not_called()


@pytest.mark.parametrize("host", [
    "supplier.example", "fordmeterbox.com.supplier.example", "muellercompany.com.supplier.example",
    "files.fordmeterbox.com", "mueller.example",
])
def test_nonmanufacturer_pdf_is_rejected_before_body_or_di(original_response, host):
    response, connections = original_response(synthetic_pdf())
    ocr = Mock()
    with pytest.raises(ValueError, match="manufacturer domain"):
        original_page(f"https://{host}/catalog.pdf", pdf_ocr=ocr)
    response.read.assert_not_called()
    ocr.assert_not_called()
    assert connections[0].closed


@pytest.mark.parametrize("pages", [1, 5])
def test_textless_pdf_ocr_requires_actual_small_page_count_and_keeps_provenance(original_response, pages):
    raw = synthetic_pdf(pages)
    original_response(raw)
    store, parser = OCRStore(), ocr_parser()
    url = "https://fordmeterbox.com/scanned.pdf"
    result = original_page(url, pdf_ocr=CachedPDFOCR(store, parser))
    parser.extract_pdf_bytes.assert_called_once_with(raw, source=url, page_limit=pages)
    assert result.text == "AV11-333W-NL pressure 100 PSI"
    assert result.pdf_page_count == pages and result.pdf_extraction == "document_intelligence_ocr"
    assert result.pdf_cache_key in store.data and not result.pdf_cache_hit
    provenance = result.provenance()
    assert provenance["source_url"] == provenance["parse_source"] == url
    assert provenance["sha256"] == hashlib.sha256(raw).hexdigest()
    assert provenance["first_retrieved_at"] == provenance["retrieved_at"] == result.retrieved_at.isoformat()
    assert provenance["parsed_at"] == "2026-10-07T00:00:00+00:00"


def test_textless_pdf_default_api_has_no_ocr(original_response):
    original_response(synthetic_pdf())
    with pytest.raises(PDFPageError) as error:
        original_page("https://fordmeterbox.com/scanned.pdf")
    assert "worker OCR" in error.value.provenance["reason"]


@pytest.mark.parametrize("text", ["", "AV11-333W-NL pressure 100 PSI"])
def test_six_actual_pages_never_call_ocr_even_with_blank_pages(original_response, text):
    original_response(synthetic_pdf(6, text))
    ocr = Mock(side_effect=AssertionError("Six-page PDF cannot enter OCR"))
    if text:
        result = original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=ocr)
        assert result.pdf_page_count == 6 and text in result.text
    else:
        with pytest.raises(PDFPageError) as error:
            original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=ocr)
        assert "five actual pages" in error.value.provenance["reason"]
    ocr.assert_not_called()


def test_ocr_cache_reuses_bytes_across_worker_instances_products_and_urls(original_response):
    raw = synthetic_pdf(2)
    store = OCRStore()
    parser = ocr_parser("AV11-333W-NL and AV11-444W-NL: 100 PSI")
    evidence, records = [], []
    for index, mpn in enumerate(["AV11-333W-NL", "AV11-444W-NL"]):
        original_response(raw)
        product = manifest()
        product.product.mpn = mpn
        product.product.item_id = f"item-{index}"
        url = f"https://fordmeterbox.com/copy-{index}.pdf"
        client = QualityWeb(
            search=lambda _, url=url: [url], browse=lambda source: {"url": source, "content": "not evidence"},
            pdf_ocr=CachedPDFOCR(store, parser), usage_callback=records.append,
        )
        evidence.extend(client.load(product, "manufacturer_web", ["Pressure"]))
        assert client.counts[f"item-{index}"] == {"search": 1, "browse": 1, "direct_page": 1}
    assert len(evidence) == 2 and parser.extract_pdf_bytes.call_count == 1
    assert evidence[0].source_version == evidence[1].source_version == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert evidence[0].source_locator != evidence[1].source_locator
    assert evidence[0].provider_retrieved_at <= evidence[1].provider_retrieved_at
    first, second = [record["pdf"] for record in records if record["operation"] == "direct_page"]
    assert first["cache_key"] == second["cache_key"] and not first["cache_hit"] and second["cache_hit"]
    assert second["parse_source"] == first["source_url"] and second["source_url"] == evidence[1].source_locator
    assert second["first_retrieved_at"] == first["retrieved_at"]
    assert "PDF text remains untrusted" in evidence[1].qualification
    assert '"cache_hit": true' in evidence[1].qualification


def test_changed_pdf_bytes_do_not_reuse_ocr_cache(original_response):
    store, parser = OCRStore(), ocr_parser()
    keys = []
    for raw in [synthetic_pdf(), synthetic_pdf() + b"\n% different source bytes"]:
        original_response(raw)
        keys.append(original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=CachedPDFOCR(store, parser)).pdf_cache_key)
    assert keys[0] != keys[1] and parser.extract_pdf_bytes.call_count == 2


@pytest.mark.parametrize("raw,media", [
    (b"<p>AV11-333W-NL</p>", "text/html"),
    (b"AV11-333W-NL", "text/plain"),
])
def test_original_html_and_plain_text_keep_existing_bounds_and_no_ocr(original_response, raw, media):
    response, connections = original_response(raw, media)
    ocr = Mock()
    result = original_page("https://supplier.example/product", pdf_ocr=ocr)
    assert "AV11-333W-NL" in result.text and not isinstance(result, PDFPageEvidence)
    assert result.content_hash == hashlib.sha256(raw).hexdigest()
    response.read.assert_called_once_with(TEXT_MAX_BYTES + 1)
    ocr.assert_not_called()
    assert connections[0].closed


@pytest.mark.parametrize("url", [
    "http://fordmeterbox.com/catalog.pdf", "https://user:password@fordmeterbox.com/catalog.pdf",
    "https://fordmeterbox.com:444/catalog.pdf", "https://fordmeterbox.com/catalog.pdf?token=secret",
    "https://fordmeterbox.com/catalog.pdf#page=1", "https://127.0.0.1/catalog.pdf",
])
def test_pdf_path_preserves_url_safeguards(original_response, url):
    _, connections = original_response(synthetic_pdf())
    with pytest.raises(ExternalEvidenceError):
        original_page(url)
    assert connections == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254"])
def test_pdf_path_rejects_any_private_dns_answer(original_response, monkeypatch, address):
    _, connections = original_response(synthetic_pdf())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
    ])
    with pytest.raises(ExternalEvidenceError):
        original_page("https://fordmeterbox.com/catalog.pdf")
    assert connections == []


@pytest.mark.parametrize("status,encoding,media,raw", [
    (302, "identity", "application/pdf", b"%PDF-redirect"),
    (200, "gzip", "application/pdf", b"%PDF-encoded"),
    (200, "identity", "application/octet-stream", b"%PDF-unknown"),
    (200, "identity", "application/pdf", b""),
    (200, "identity", "application/pdf", b"<html>not a PDF</html>"),
    (200, "identity", "application/pdf", b"%PDF-1.4\nmalformed"),
    (200, "identity", "application/pdf", b"%PDF-" + b"x" * PDF_MAX_BYTES),
    (200, "identity", "text/plain", b"x" * (TEXT_MAX_BYTES + 1)),
])
def test_pdf_retrieval_and_parser_failures_close_without_ocr(original_response, status, encoding, media, raw):
    _, connections = original_response(raw, media, status, encoding)
    ocr = Mock()
    with pytest.raises(ValueError):
        original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=ocr)
    ocr.assert_not_called()
    assert connections[0].closed


@pytest.mark.parametrize("failure", [FileNotFoundError("pdftotext"), subprocess.TimeoutExpired("pdfinfo", 30)])
def test_missing_or_timed_out_pdf_tools_do_not_fall_back_to_ocr(original_response, monkeypatch, failure):
    original_response(synthetic_pdf())
    ocr = Mock()
    monkeypatch.setattr("backend.quality_pdf.subprocess.Popen", Mock(side_effect=failure))
    with pytest.raises(PDFPageError) as error:
        original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=ocr)
    assert error.value.error_type == type(failure).__name__
    ocr.assert_not_called()


@pytest.mark.parametrize("text", ["", "\x00bad", "x" * (TEXT_MAX_BYTES + 1)])
def test_unusable_ocr_results_are_not_cached_and_later_calls_can_retry(original_response, text):
    store, parser = OCRStore(), ocr_parser(text)
    for _ in range(2):
        original_response(synthetic_pdf())
        with pytest.raises(PDFPageError) as error:
            original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=CachedPDFOCR(store, parser))
        assert error.value.error_type == "ValueError"
    assert parser.extract_pdf_bytes.call_count == 2 and not store.keys("parses/")
    assert not store.keys("analysis-attempts/")


def test_ocr_provider_failure_is_explicit_retryable_and_never_promotes_provider_content(original_response):
    store, parser, records = OCRStore(), ocr_parser(), []
    parser.extract_pdf_bytes.side_effect = RuntimeError("provider failure")
    for index in range(2):
        original_response(synthetic_pdf())
        client = QualityWeb(
            search=lambda _: [f"https://fordmeterbox.com/copy-{index}.pdf"],
            browse=lambda url: {"url": url, "content": "AV11-333W-NL invented pressure 900 PSI"},
            pdf_ocr=CachedPDFOCR(store, parser), usage_callback=records.append,
        )
        assert client.load(manifest(), "manufacturer_web", ["Pressure"]) == []
    assert parser.extract_pdf_bytes.call_count == 2
    for record in [record for record in records if record["operation"] == "direct_page"]:
        assert record["status"] == "failed"
        assert record["pdf"]["sha256"] == hashlib.sha256(synthetic_pdf()).hexdigest()
        assert record["pdf"]["source_url"] == record["source_url"]
        assert record["pdf"]["retrieved_at"] and record["pdf"]["page_count"] == 1
    assert "provider failure" not in json.dumps(records)
    assert not store.keys("parses/")
    assert not store.keys("analysis-attempts/")


def test_corrupt_ocr_cache_fails_closed_without_fresh_analysis(original_response):
    from backend.batch_store import read_json, write_json

    store, parser = OCRStore(), ocr_parser()
    original_response(synthetic_pdf())
    result = original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=CachedPDFOCR(store, parser))
    record, version = read_json(store, result.pdf_cache_key)
    record["document"]["raw_text"] = "AV11-333W-NL invented 900 PSI"
    write_json(store, result.pdf_cache_key, record, version)
    original_response(synthetic_pdf())
    with pytest.raises(PDFPageError) as error:
        original_page("https://fordmeterbox.com/copy.pdf", pdf_ocr=CachedPDFOCR(store, parser))
    assert error.value.error_type == "ValueError"
    assert parser.extract_pdf_bytes.call_count == 1


@pytest.mark.parametrize("text", ["AV11-333W-NL-OTHER 100 PSI", "AV11-444W-NL 100 PSI", "XAV11-333W-NL 100 PSI"])
def test_pdf_never_weakens_exact_mpn_matching(original_response, text):
    original_response(synthetic_pdf(text=text))
    client = QualityWeb(
        search=lambda _: ["https://fordmeterbox.com/catalog.pdf"],
        browse=lambda url: {"url": url, "content": "AV11-333W-NL 900 PSI"},
    )
    assert client.load(manifest(), "manufacturer_web", ["Pressure"]) == []


def test_poppler_output_is_bounded_and_page_count_is_not_text_formfeed_count(monkeypatch):
    text, pages = pdf_text(synthetic_pdf(5))
    assert not text.strip() and pages == 5
    monkeypatch.setattr("backend.quality_pdf.TEXT_MAX_BYTES", 8)
    with pytest.raises(ValueError, match="output bound"):
        pdf_text(synthetic_pdf(text="AV11-333W-NL pressure 100 PSI"))


@pytest.mark.parametrize("info", [b"Pages: unknown\n", b"Pages: 0\n", b"Pages: 1\nPages: 6\n"])
def test_unverified_pdf_page_count_cannot_enter_ocr(original_response, monkeypatch, info):
    original_response(synthetic_pdf())
    command = Mock(return_value=info)
    monkeypatch.setattr("backend.quality_pdf._pdf_command", command)
    ocr = Mock()
    with pytest.raises(PDFPageError):
        original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=ocr)
    assert command.call_count == 1
    ocr.assert_not_called()


@pytest.mark.parametrize("field,value", [("source", "https://supplier.example/wrong.pdf"), ("cache_key", "sha256:" + "0" * 64)])
def test_ocr_source_and_content_mismatch_is_not_cached(original_response, field, value):
    raw = synthetic_pdf()
    original_response(raw)
    store, parser = OCRStore(), ocr_parser()
    parsed = ParsedDocument(
        source="https://fordmeterbox.com/catalog.pdf", cache_key="sha256:" + hashlib.sha256(raw).hexdigest(),
        parsed_at=datetime.now(timezone.utc), raw_text="AV11-333W-NL 100 PSI",
    )
    parser.extract_pdf_bytes.side_effect = None
    parser.extract_pdf_bytes.return_value = parsed.model_copy(update={field: value})
    with pytest.raises(PDFPageError):
        original_page(parsed.source, pdf_ocr=CachedPDFOCR(store, parser))
    assert not store.keys("parses/") and parser.extract_pdf_bytes.call_count == 1


def test_unavailable_ocr_cache_never_turns_into_a_new_paid_parse(original_response, monkeypatch):
    original_response(synthetic_pdf())
    store, parser = OCRStore(), ocr_parser()
    monkeypatch.setattr(store, "read_bytes", Mock(side_effect=RuntimeError("storage denied")))
    with pytest.raises(PDFPageError):
        original_page("https://fordmeterbox.com/catalog.pdf", pdf_ocr=CachedPDFOCR(store, parser))
    parser.extract_pdf_bytes.assert_not_called()


def test_ocr_usage_hooks_price_actual_pages_and_record_cached_hits_as_zero():
    store, parser, records, before = OCRStore(), ocr_parser(), [], []
    raw, source, retrieved = synthetic_pdf(2), "https://fordmeterbox.com/catalog.pdf", datetime.now(timezone.utc)

    def price_and_record(entry):
        if entry["new_analysis"] and entry["analyzed_pages"] is not None:
            entry["cost_usd"] = entry["analyzed_pages"] * .01
            entry["pricing_basis"] = "synthetic worker-configured per-page price"
        records.append(entry)

    result = CachedPDFOCR(store, parser, before_call=before.append, usage_callback=price_and_record)(
        raw, source=source, page_count=2, retrieved_at=retrieved,
    )
    cached = CachedPDFOCR(store, parser, before_call=before.append, usage_callback=price_and_record)(
        raw, source="https://fordmeterbox.com/copy.pdf", page_count=2, retrieved_at=retrieved,
    )
    assert not result.cache_hit and cached.cache_hit and result.cache_key == cached.cache_key
    assert len(before) == 1 and before[0]["requested_pages"] == 2
    assert before[0]["operation"] == "document_intelligence"
    first, second = records
    assert first["status"] == "succeeded" and first["new_analysis"]
    assert first["analyzed_pages"] == 2 and first["usage_reported"]
    assert first["cost_usd"] == .02 and first["pricing_basis"].startswith("synthetic")
    assert first["sha256"] == hashlib.sha256(raw).hexdigest() and first["retrieved_at"] == retrieved.isoformat()
    assert second["status"] == "cached" and not second["new_analysis"]
    assert second["analyzed_pages"] == 0 and second["cost_usd"] == 0
    assert second["pricing_basis"] == "cached_parse"
    assert store.keys("") == [result.cache_key]
    parser.extract_pdf_bytes.assert_called_once_with(raw, source=source, page_limit=2)


@pytest.mark.parametrize("reported_pages", [None, 0, True, 6])
def test_missing_or_invalid_di_usage_stays_explicitly_unknown(reported_pages):
    store, parser, records = OCRStore(), ocr_parser(), []
    original_extract = parser.extract_pdf_bytes.side_effect

    def extract(*args, **kwargs):
        document = original_extract(*args, **kwargs)
        parser.last_page_count = reported_pages
        return document

    parser.extract_pdf_bytes.side_effect = extract
    CachedPDFOCR(store, parser, usage_callback=records.append)(
        synthetic_pdf(), source="https://fordmeterbox.com/catalog.pdf", page_count=1,
        retrieved_at=datetime.now(timezone.utc),
    )
    assert records[0]["status"] == "succeeded" and records[0]["new_analysis"]
    assert records[0]["analyzed_pages"] is None and not records[0]["usage_reported"]
    assert records[0]["cost_usd"] is None and records[0]["pricing_basis"] is None


def test_failed_ocr_usage_is_unknown_and_fix_then_rerun_succeeds():
    store, parser, records = OCRStore(), ocr_parser(), []
    failure = RuntimeError("private provider body")
    parser.last_page_count = 5  # Prior service state must not become this failed call's usage.
    original_extract = parser.extract_pdf_bytes.side_effect
    parser.extract_pdf_bytes.side_effect = failure
    raw, source, retrieved = synthetic_pdf(), "https://fordmeterbox.com/catalog.pdf", datetime.now(timezone.utc)
    with pytest.raises(RuntimeError) as error:
        CachedPDFOCR(store, parser, usage_callback=records.append)(
            raw, source=source, page_count=1, retrieved_at=retrieved,
        )
    assert error.value is failure and store.keys("") == []
    record = records[0]
    assert record["status"] == "failed" and record["stage"] == "analysis"
    assert record["new_analysis"] and not record["usage_reported"]
    assert record["cost_usd"] is None and record["analyzed_pages"] is None
    assert record["error_type"] == "RuntimeError" and "private provider body" not in json.dumps(records)
    parser.extract_pdf_bytes.side_effect = original_extract
    result = CachedPDFOCR(store, parser, usage_callback=records.append)(
        raw, source=source, page_count=1, retrieved_at=retrieved,
    )
    assert not result.cache_hit and records[-1]["status"] == "succeeded"
    assert parser.extract_pdf_bytes.call_count == 2 and store.keys("") == [result.cache_key]


def test_existing_before_call_exception_propagates_and_records_no_analysis():
    store, parser, records = OCRStore(), ocr_parser(), []
    failure = RuntimeError("existing meter cap")
    before = Mock(side_effect=failure)
    with pytest.raises(RuntimeError) as error:
        CachedPDFOCR(store, parser, before_call=before, usage_callback=records.append)(
            synthetic_pdf(), source="https://fordmeterbox.com/catalog.pdf", page_count=1,
            retrieved_at=datetime.now(timezone.utc),
        )
    assert error.value is failure
    assert records[0]["stage"] == "before_call" and records[0]["status"] == "failed"
    assert not records[0]["new_analysis"] and records[0]["analyzed_pages"] == records[0]["cost_usd"] == 0
    parser.extract_pdf_bytes.assert_not_called()
    assert store.keys("") == []


def test_usage_callback_failure_does_not_mask_existing_provider_exception():
    parser = ocr_parser()
    failure = RuntimeError("provider failed")
    parser.extract_pdf_bytes.side_effect = failure
    with pytest.raises(RuntimeError) as error:
        CachedPDFOCR(OCRStore(), parser, usage_callback=Mock(side_effect=ValueError("meter failed")))(
            synthetic_pdf(), source="https://fordmeterbox.com/catalog.pdf", page_count=1,
            retrieved_at=datetime.now(timezone.utc),
        )
    assert error.value is failure
    assert failure.__notes__ == ["PDF OCR usage callback failed: ValueError"]


def test_existing_di_service_contract_preserves_worker_credential_and_return_shape(monkeypatch):
    from types import SimpleNamespace
    from backend.core.docintel import DocumentIntelligenceService
    from backend.quality_pdf import PDFOCRResult

    credential, records, store = object(), [], OCRStore()
    parser = DocumentIntelligenceService(endpoint="https://parser.example", credential=credential)
    analyze = Mock(return_value=SimpleNamespace(content="AV11-333W-NL 100 PSI", pages=[object()]))
    monkeypatch.setattr(parser, "_analyze", analyze)
    monkeypatch.setattr(parser, "_get_blob_service", Mock(side_effect=AssertionError("Use the existing worker store")))
    raw, source = synthetic_pdf(), "https://fordmeterbox.com/catalog.pdf"
    result = CachedPDFOCR(store, parser, usage_callback=records.append)(
        raw, source=source, page_count=1, retrieved_at=datetime.now(timezone.utc),
    )
    assert isinstance(result, PDFOCRResult) and isinstance(result.document, ParsedDocument)
    assert result.document.source == source and result.document.cache_key == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert result.first_retrieved_at.tzinfo is not None and records[0]["analyzed_pages"] == 1
    assert parser._get_credential() is credential
    analyze.assert_called_once_with(raw, page_limit=1)


def test_existing_monetary_stop_from_ocr_hook_propagates_through_web_loader(original_response):
    from backend.quality_cost import CostLimitExceeded

    original_response(synthetic_pdf())
    parser, records = ocr_parser(), []
    failure = CostLimitExceeded("existing run meter cap")
    client = QualityWeb(
        search=lambda _: ["https://fordmeterbox.com/catalog.pdf"],
        browse=lambda url: {"url": url, "content": "AV11-333W-NL unverified content"},
        pdf_ocr=CachedPDFOCR(OCRStore(), parser, before_call=Mock(side_effect=failure), usage_callback=records.append),
    )
    with pytest.raises(CostLimitExceeded) as error:
        client.load(manifest(), "manufacturer_web", ["Pressure"])
    assert error.value is failure and records[0]["cost_usd"] == 0
    parser.extract_pdf_bytes.assert_not_called()


def test_web_routes_ocr_to_late_bound_live_callbacks_with_current_product_context(original_response):
    raw, store, parser = synthetic_pdf(2), OCRStore(), ocr_parser()
    old_before, old_usage = Mock(), Mock()
    client = QualityWeb(
        search=lambda _: ["https://fordmeterbox.com/catalog.pdf"],
        browse=lambda url: {"url": url, "content": "AV11-333W-NL not verified"},
        pdf_ocr=CachedPDFOCR(store, parser, before_call=old_before, usage_callback=old_usage),
    )
    all_usage, all_before = [], []
    for index in range(2):
        original_response(raw)
        usage, before = [], []
        client.usage_callback, client.before_call = usage.append, before.append
        product = manifest()
        product.product.item_id = f"product-{index}"
        assert client.load(product, "manufacturer_web", ["Pressure"])
        all_usage.append(usage)
        all_before.append(before)
        record = next(entry for entry in usage if entry["operation"] == "document_intelligence")
        assert record["item_id"] == f"product-{index}" and record["tier"] == "manufacturer_web"
        assert record["phase"] == "ocr" and record["cache_hit"] is bool(index)
        assert record["analysis_attempted"] is (index == 0)
        assert record["analyzed_pages"] == (0 if index else 2)
        if index:
            assert record["cost_usd"] == 0 and not record["new_analysis"]
        assert client.counts[f"product-{index}"] == {"search": 1, "browse": 1, "direct_page": 1}
    assert len(all_usage[0]) == len(all_usage[1]) == 4
    assert [entry["operation"] for entry in all_usage[0]] == [
        "web_search", "web_browse", "document_intelligence", "direct_page",
    ]
    paid_before = [entry for entry in all_before[0] if entry["operation"] == "document_intelligence"]
    assert len(paid_before) == 1 and paid_before[0]["item_id"] == "product-0"
    assert paid_before[0]["tier"] == "manufacturer_web" and paid_before[0]["phase"] == "ocr"
    assert not [entry for entry in all_before[1] if entry["operation"] == "document_intelligence"]
    old_before.assert_not_called()
    old_usage.assert_not_called()
    assert parser.extract_pdf_bytes.call_count == 1
    assert len([entry for entry in client.diagnostics if entry["operation"] == "document_intelligence"]) == 2


def test_live_web_monetary_callback_stops_ocr_and_propagates_without_a_second_guard(original_response):
    from backend.quality_cost import CostLimitExceeded

    original_response(synthetic_pdf())
    parser, store, records, before = ocr_parser(), OCRStore(), [], []
    failure = CostLimitExceeded("existing worker monetary cap")

    def cap(entry):
        before.append(entry)
        if entry["operation"] == "document_intelligence":
            raise failure

    client = QualityWeb(
        search=lambda _: ["https://fordmeterbox.com/catalog.pdf"],
        browse=lambda url: {"url": url, "content": "AV11-333W-NL unverified"},
        pdf_ocr=CachedPDFOCR(store, parser),
    )
    client.before_call, client.usage_callback = cap, records.append
    with pytest.raises(CostLimitExceeded) as error:
        client.load(manifest(), "manufacturer_web", ["Pressure"])
    assert error.value is failure
    record = next(entry for entry in records if entry["operation"] == "document_intelligence")
    assert record["item_id"] == manifest().product.item_id and record["tier"] == "manufacturer_web"
    assert record["phase"] == "ocr" and record["error_type"] == "CostLimitExceeded"
    assert record["analyzed_pages"] == record["cost_usd"] == 0
    assert record["cache_hit"] is False and not record["new_analysis"]
    assert record["analysis_attempted"] is False
    assert records[-1]["operation"] == "direct_page" and records[-1]["status"] == "stopped"
    parser.extract_pdf_bytes.assert_not_called()
    assert store.keys("") == []


def test_live_web_routes_failed_ocr_with_unknown_pages_and_boolean_cache_hit(original_response):
    original_response(synthetic_pdf())
    parser, records = ocr_parser(), []
    parser.extract_pdf_bytes.side_effect = RuntimeError("private provider response")
    client = QualityWeb(
        search=lambda _: ["https://fordmeterbox.com/catalog.pdf"],
        browse=lambda url: {"url": url, "content": "AV11-333W-NL unverified"},
        pdf_ocr=CachedPDFOCR(OCRStore(), parser),
    )
    client.usage_callback = records.append
    assert client.load(manifest(), "manufacturer_web", ["Pressure"]) == []
    record = next(entry for entry in records if entry["operation"] == "document_intelligence")
    assert record["status"] == "failed" and record["error_type"] == "RuntimeError"
    assert record["analyzed_pages"] is None and record["cost_usd"] is None
    assert record["cache_hit"] is False and record["new_analysis"]
    assert record["analysis_attempted"] is True
    assert record["item_id"] == manifest().product.item_id and record["phase"] == "ocr"


@pytest.mark.parametrize("stage,attempted", [
    ("succeeded", True), ("cached", False), ("cache_read", False), ("before_call", False), ("analysis", True),
])
def test_di_analysis_attempted_flag_distinguishes_cache_and_pre_parser_failures(monkeypatch, stage, attempted):
    raw, store, parser, records, before = synthetic_pdf(), OCRStore(), ocr_parser(), [], []
    kwargs = {"source": "https://fordmeterbox.com/catalog.pdf", "page_count": 1,
              "retrieved_at": datetime.now(timezone.utc)}
    failure = RuntimeError("synthetic failure")
    if stage == "cached":
        CachedPDFOCR(store, parser)(raw, **kwargs)
    elif stage == "cache_read":
        monkeypatch.setattr(store, "read_bytes", Mock(side_effect=failure))
    elif stage == "analysis":
        parser.extract_pdf_bytes.side_effect = failure

    def before_call(entry):
        before.append(entry)
        if stage == "before_call":
            raise failure

    prior_calls = parser.extract_pdf_bytes.call_count
    ocr = CachedPDFOCR(store, parser, before_call=before_call, usage_callback=records.append)
    if stage in {"succeeded", "cached"}:
        ocr(raw, **kwargs)
    else:
        with pytest.raises(RuntimeError) as error:
            ocr(raw, **kwargs)
        assert error.value is failure
    assert len(records) == 1 and records[0]["analysis_attempted"] is attempted
    assert all(entry["analysis_attempted"] is False for entry in before)
    assert parser.extract_pdf_bytes.call_count - prior_calls == int(attempted)
