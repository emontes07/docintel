"""Mocked byte-input parsing tests; no customer documents or network access."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.core.docintel import DocumentIntelligenceError, DocumentIntelligenceService


@pytest.fixture
def parser(monkeypatch):
    import socket
    import azure.ai.documentintelligence

    blocked = Mock(side_effect=AssertionError("Unexpected network or Blob access"))
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(DocumentIntelligenceService, "_get_blob_service", blocked)
    monkeypatch.setattr("backend.core.docintel.BlobServiceClient", blocked)
    credential = object()
    service = DocumentIntelligenceService(endpoint="https://parser.example", credential=credential)
    client = Mock()
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(azure.ai.documentintelligence, "DocumentIntelligenceClient", factory)
    region = SimpleNamespace(page_number=3)
    result = SimpleNamespace(
        content="Original text",
        paragraphs=[SimpleNamespace(content="Original text", bounding_regions=[region], role="title")],
        tables=[SimpleNamespace(row_count=1, column_count=2, bounding_regions=[region], cells=[
            SimpleNamespace(row_index=0, column_index=1, content="Cell text"),
        ])],
    )
    client.begin_analyze_document.return_value.result.return_value = result
    return service, client, factory, credential


def test_pdf_bytes_preserve_mapping_and_content_version_without_blob(parser):
    service, client, factory, credential = parser
    content = b"%PDF-1.7\nsynthetic test bytes"
    parsed = service.extract_pdf_bytes(content, source="/local/original.pdf")
    assert parsed.source == "/local/original.pdf"
    assert parsed.cache_key == "sha256:" + hashlib.sha256(content).hexdigest()
    assert parsed.paragraphs[0].page_number == 3
    assert parsed.paragraphs[0].text == "Original text"
    assert parsed.paragraphs[0].role == "title"
    assert parsed.tables[0].page_number == 3
    assert parsed.tables[0].cells == [["", "Cell text"]]
    assert parsed.raw_text == "Original text"
    factory.assert_called_once_with(endpoint="https://parser.example", credential=credential, retry_total=0)
    client.begin_analyze_document.assert_called_once()
    assert client.begin_analyze_document.call_args.args == ("prebuilt-layout",)
    assert client.begin_analyze_document.call_args.kwargs["body"].getvalue() == content
    client.begin_analyze_document.return_value.result.assert_called_once_with()
    factory.return_value.__exit__.assert_called_once()
    assert service._blob_service is None


def test_byte_version_changes_with_content_not_location(parser):
    service, *_ = parser
    first = service.extract_pdf_bytes(b"%PDF-1.7 first", source="first.pdf")
    renamed = service.extract_pdf_bytes(b"%PDF-1.7 first", source="renamed.pdf")
    changed = service.extract_pdf_bytes(b"%PDF-1.7 changed", source="first.pdf")
    assert first.cache_key == renamed.cache_key
    assert first.cache_key != changed.cache_key


@pytest.mark.parametrize("content,source", [(b"", "test.pdf"), (b"not a PDF", "test.pdf"), (b"%PDF-1.7", " ")])
def test_invalid_byte_input_never_submits(parser, content, source):
    service, client, factory, _ = parser
    with pytest.raises(ValueError):
        service.extract_pdf_bytes(content, source=source)
    factory.assert_not_called()
    client.begin_analyze_document.assert_not_called()


def test_analysis_failure_is_sanitized_and_not_retried(parser):
    service, client, _, _ = parser
    client.begin_analyze_document.side_effect = RuntimeError("sensitive provider response")
    with pytest.raises(DocumentIntelligenceError, match="^Layout analysis failed$"):
        service.extract_pdf_bytes(b"%PDF-1.7 test", source="test.pdf")
    client.begin_analyze_document.assert_called_once()


def test_url_analysis_still_uses_url_request(parser):
    service, client, _, _ = parser
    service._analyze("https://example.test/document.pdf")
    assert client.begin_analyze_document.call_args.kwargs["body"].url_source == "https://example.test/document.pdf"