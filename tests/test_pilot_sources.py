import hashlib
import io
import json
import socket
import urllib.error
import urllib.request
import zipfile
from types import SimpleNamespace
from typing import Any, cast

import pytest

from backend.core import pilot_sources
from backend.core.pilot_sources import SourceReference, retrieve_document, retrieve_pdf


PDF = b"%PDF-1.4\nsynthetic test content\n%%EOF"
SIGNED_URL = "https://tenant.sharepoint.com/_layouts/15/download.aspx?signature=a%2Fb%2Bz&other=1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", reject)


def xlsx(**overrides):
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="root" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Attributes" sheetId="1" r:id="sheet1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="sheet1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>',
    }
    parts.update(overrides)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            if content is not None:
                archive.writestr(name, content)
    return output.getvalue()


class Response(io.BytesIO):
    def __init__(self, code, body=b"", headers=None):
        super().__init__(body)
        self.code = code
        self.headers = headers or {}
        self.read_limits = []

    def read(self, size=-1):
        self.read_limits.append(size)
        return super().read(size)


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def reference(**overrides):
    values = dict(source_id="ford-sharepoint", kind="sharepoint", location="https://tenant.sharepoint.com/document.pdf", expected_sha256=hashlib.sha256(PDF).hexdigest(), drive_id="resolved-drive", item_id="resolved-item")
    return SourceReference(**(values | overrides))


def metadata(etag="version-a", **overrides):
    values = {"id": "resolved-item", "webUrl": reference().location, "eTag": etag, "size": len(PDF)}
    return Response(200, json.dumps(values | overrides).encode())


def credential():
    return SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="graph-secret"))


def test_redirect_credentials_and_version_integrity():
    signed_url = SIGNED_URL
    opener = Opener([metadata(), Response(302, headers={"Location": signed_url}), Response(200, PDF), metadata()])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["status"] == "success"
    assert result.metadata["version_verified"]
    assert result.metadata["etag"] != result.metadata["sha256"]
    assert opener.requests[2].full_url == signed_url
    assert opener.requests[2].get_header("Authorization") is None
    assert all(opener.requests[index].get_header("Authorization") == "Bearer graph-secret" for index in (0, 1, 3))
    assert "signature" not in json.dumps(result.metadata)
    assert result.metadata["format"] == "pdf"
    assert all(request.get_header("Cookie") is None for request in opener.requests)


def test_download_failure_stops_without_fallback_or_retry():
    opener = Opener([metadata(), Response(302, headers={"Location": "https://tenant.sharepoint.com/?secret=hidden"}), Response(401)])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["failed_stage"] == "C_download"
    assert not result.metadata["bytes_retrieved"]
    assert len(opener.requests) == 3
    assert "hidden" not in json.dumps(result.metadata)
    assert result.metadata["stages"] == [
        {"stage": "A_metadata", "http_status": 200},
        {"stage": "B_content", "http_status": 302},
        {"stage": "C_download", "http_status": 401},
    ]
    assert result.metadata["sha256"] is None
    assert not result.metadata["version_verified"]
    assert "missing permission is not established" in result.metadata["explanation"]


def test_later_metadata_failure_retains_byte_retrieval_fact():
    opener = Opener([metadata(), Response(200, PDF), Response(401)])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["failed_stage"] == "D_metadata"
    assert result.metadata["bytes_retrieved"]
    assert result.content == PDF
    assert not result.metadata["version_verified"]


def test_changed_etag_blocks_extraction():
    opener = Opener([metadata(), Response(200, PDF), metadata("version-b")])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["status"] == "failed"
    assert result.metadata["error_code"] == "version_changed"


def test_local_hash_is_not_an_etag(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(PDF)
    result = retrieve_pdf(reference(kind="local", location=str(path)))
    assert result.metadata["status"] == "success"
    assert result.metadata["etag"] is None
    assert result.metadata["content_version"] == "sha256:" + hashlib.sha256(PDF).hexdigest()
    path.write_bytes(PDF + b"changed")
    assert retrieve_pdf(reference(kind="local", location=str(path))).metadata["error_code"] == "unapproved_content_version"


def test_non_https_redirect_is_never_requested():
    opener = Opener([metadata(), Response(302, headers={"Location": "http://download.example/?secret=hidden"})])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["failed_stage"] == "C_download"
    assert len(opener.requests) == 2


def test_disabled_source_never_requests_content():
    opener = Opener([])
    result = retrieve_pdf(reference(enabled=False), opener=opener)
    assert result.metadata["error_code"] == "source_disabled"
    assert opener.requests == []


def test_missing_local_file_has_actionable_sanitized_failure(tmp_path):
    result = retrieve_pdf(reference(kind="local", location=str(tmp_path / "private-name.pdf")))
    assert result.metadata["failed_stage"] == "local_read"
    assert result.metadata["error_code"] == "not_found"
    assert "Restore the approved file" in result.metadata["explanation"]
    assert "private-name" not in result.metadata["explanation"]


@pytest.mark.parametrize("destination", [
    "http://tenant.sharepoint.com/document",
    "https://other.sharepoint.com/document",
    "https://tenant.sharepoint.com.evil.example/document",
    "https://tenant.sharepoint.com@evil.example/document",
    "https://user:password@tenant.sharepoint.com/document",
    "https://tenant.sharepoint.com:443/document",
    "https://tenant.sharepoint.com./document",
    "https://127.0.0.1/document",
    "https://169.254.169.254/document",
    "https://tenant.sharepoint.com/document#fragment",
    "https://tenant.sharepoint.com/document#",
    "https://tenant.sharepoint.com\\@evil.example/document",
    "https://tenant.sharepoint.com/\r\nX-Test: secret",
    "//tenant.sharepoint.com/document",
    "/document",
    "https://[invalid/document",
    "",
])
def test_unapproved_redirect_destination_never_requested(destination):
    opener = Opener([metadata(), Response(302, headers={"Location": destination})])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "invalid_download_destination"
    assert result.metadata["failed_stage"] == "C_download"
    assert len(opener.requests) == 2
    assert "secret" not in json.dumps(result.metadata)


@pytest.mark.parametrize("location", [
    "http://tenant.sharepoint.com/document.pdf",
    "https://other.example/document.pdf",
    "https://tenant.sharepoint.com.evil.example/document.pdf",
    "https://tenant.sharepoint.com:443/document.pdf",
    "https://user:private-secret@tenant.sharepoint.com/document.pdf",
    "https://tenant.sharepoint.com/document.pdf?token=private-secret",
    "https://tenant.sharepoint.com/document.pdf#private-secret",
    "https://[invalid/",
])
def test_invalid_reference_never_requests_credentials_or_persists_url(location):
    opener = Opener([])
    result = retrieve_pdf(reference(location=location), opener=opener)
    assert result.metadata["error_code"] == "invalid_source_reference"
    assert result.metadata["location"] is None
    assert opener.requests == []
    assert "private-secret" not in json.dumps(result.metadata)


@pytest.mark.parametrize("field", ["drive_id", "item_id"])
@pytest.mark.parametrize("value", [None, "", ".", "..", "../item", "item?query", "item%2Fother", "item#fragment", "item\nheader", "item other"])
def test_invalid_resolved_ids_stop_before_authentication(field, value):
    opener = Opener([])
    result = retrieve_pdf(reference(**{field: value}), opener=opener)
    assert result.metadata["error_code"] == "invalid_source_reference"
    assert opener.requests == []


def test_second_redirect_is_not_followed():
    opener = Opener([
        metadata(), Response(302, headers={"Location": SIGNED_URL}),
        Response(302, headers={"Location": "https://tenant.sharepoint.com/?signature=second-secret"}),
    ])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["failed_stage"] == "C_download"
    assert result.metadata["error_code"] == "http_rejected"
    assert len(opener.requests) == 3
    assert "second-secret" not in json.dumps(result.metadata)


@pytest.mark.parametrize("handler", [
    urllib.request.HTTPRedirectHandler,
    urllib.request.HTTPCookieProcessor,
    urllib.request.HTTPBasicAuthHandler,
    urllib.request.HTTPDigestAuthHandler,
])
def test_real_opener_cannot_add_auth_cookies_or_automatic_redirects(handler):
    opener = urllib.request.build_opener(pilot_sources._NoRedirect(), handler())
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "unsafe_transport"
    assert result.metadata["stages"] == []


@pytest.mark.parametrize("name", ["Authorization", "Proxy-Authorization", "Cookie"])
def test_real_opener_must_not_supply_implicit_credentials(name):
    opener = urllib.request.build_opener(pilot_sources._NoRedirect())
    opener.addheaders.append((name, "private-secret"))
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "unsafe_transport"
    assert result.metadata["stages"] == []
    assert "private-secret" not in json.dumps(result.metadata)


@pytest.mark.parametrize("stage,responses", [
    ("A_metadata", [Response(401)]),
    ("B_content", [metadata(), Response(401)]),
    ("C_download", [metadata(), Response(302, headers={"Location": SIGNED_URL}), Response(401)]),
    ("D_metadata", [metadata(), Response(200, PDF), Response(401)]),
])
def test_authentication_failure_reports_exact_stage_without_inventing_cause(stage, responses):
    result = retrieve_pdf(reference(), credential=credential(), opener=Opener(responses))
    assert result.metadata["failed_stage"] == stage
    assert result.metadata["error_code"] == "authentication_rejected"
    assert result.metadata["bytes_retrieved"] is (stage == "D_metadata")
    assert result.metadata["status"] == "failed"
    assert "missing permission is not established" in result.metadata["explanation"]


def test_http_error_redirect_is_handled_without_forwarding_credentials():
    redirect = urllib.error.HTTPError(
        "https://graph.microsoft.com/content", 302, "Found",
        {"Location": SIGNED_URL}, io.BytesIO(),
    )
    opener = Opener([metadata(), redirect, Response(200, PDF), metadata()])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["status"] == "success"
    assert opener.requests[2].get_header("Authorization") is None


def test_provider_exception_never_persists_signed_url_or_token():
    opener = Opener([metadata(), Response(302, headers={"Location": SIGNED_URL}), urllib.error.URLError(SIGNED_URL + "&token=private-secret")])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "network_failure"
    assert result.metadata["failed_stage"] == "C_download"
    assert "private-secret" not in json.dumps(result.metadata)
    assert "signature" not in json.dumps(result.metadata)


@pytest.mark.parametrize("status,error_code", [(401, "authentication_rejected"), (403, "access_forbidden"), (429, "throttled")])
def test_real_http_error_at_download_stops_and_sanitizes(status, error_code):
    rejection = urllib.error.HTTPError(SIGNED_URL, status, "private-secret", {}, io.BytesIO(b"private provider body"))
    opener = Opener([metadata(), Response(302, headers={"Location": SIGNED_URL}), rejection])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == error_code
    assert result.metadata["failed_stage"] == "C_download"
    assert not result.metadata["bytes_retrieved"]
    assert len(opener.requests) == 3
    serialized = json.dumps(result.metadata)
    assert all(secret not in serialized for secret in ("signature", "private-secret", "private provider body", "graph-secret"))


@pytest.mark.parametrize("format,body", [("pdf", PDF), ("xlsx", xlsx())])
def test_document_formats_share_local_hash_integrity(tmp_path, format, body):
    path = tmp_path / ("approved." + format)
    path.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()
    source = reference(kind="local", location=str(path), expected_sha256=expected)
    result = retrieve_document(source, format=format)
    assert result.content == body
    assert result.metadata["status"] == "success"
    assert result.metadata["format"] == format
    assert result.metadata["kind"] == "local"
    assert result.metadata["source_id"] == source.source_id
    assert result.metadata["sha256"] == expected
    assert result.metadata["content_version"] == "sha256:" + expected
    assert result.metadata["etag"] is None
    assert retrieve_document(source.model_copy(update={"expected_sha256": "0" * 64}), format=format).metadata["error_code"] == "unapproved_content_version"


def test_xlsx_sharepoint_uses_same_version_pinned_byte_flow():
    body = xlsx()
    source = reference(expected_sha256=hashlib.sha256(body).hexdigest(), location="https://tenant.sharepoint.com/attributes.xlsx")
    item_metadata = lambda: metadata(size=len(body), webUrl=source.location)
    opener = Opener([item_metadata(), Response(302, headers={"Location": SIGNED_URL}), Response(200, body), item_metadata()])
    result = retrieve_document(source, format="xlsx", credential=credential(), opener=opener)
    assert result.metadata["status"] == "success"
    assert result.metadata["version_verified"]
    assert result.metadata["kind"] == "sharepoint"
    assert result.content == body
    assert opener.requests[2].get_header("Authorization") is None


@pytest.mark.parametrize("body", [
    b"PK not a zip",
    b"not-an-xlsx" + xlsx(),
    xlsx(**{"xl/workbook.xml": None}),
    xlsx(**{"[Content_Types].xml": "<Types/>"}),
    xlsx(**{"xl/workbook.xml": "<invalid"}),
    xlsx(**{"xl/workbook.xml": '<!DOCTYPE workbook [<!ENTITY secret "value">]><workbook>&secret;</workbook>'}),
    xlsx(**{"xl/worksheets/sheet1.xml": "<invalid"}),
    xlsx(**{"xl/worksheets/sheet1.xml": "<not-a-worksheet/>"}),
    xlsx(**{"xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'}),
    xlsx(**{"_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'}),
    xlsx(**{"../outside.xml": "<outside/>"}),
    xlsx(**{"xl/vbaProject.bin": b"macros"}),
])
def test_invalid_xlsx_structure_is_not_accepted(tmp_path, body):
    path = tmp_path / "invalid.xlsx"
    path.write_bytes(body)
    result = retrieve_document(reference(kind="local", location=str(path), expected_sha256=hashlib.sha256(body).hexdigest()), format="xlsx")
    assert result.metadata["error_code"] == "invalid_xlsx"
    assert result.metadata["failed_stage"] == "integrity"


def test_xlsx_expanded_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot_sources, "MAX_XLSX_EXPANDED_BYTES", 1024)
    path = tmp_path / "oversized.xlsx"
    body = xlsx(**{"xl/worksheets/sheet1.xml": " " * 2048})
    path.write_bytes(body)
    result = retrieve_document(reference(kind="local", location=str(path), expected_sha256=hashlib.sha256(body).hexdigest()), format="xlsx")
    assert result.metadata["error_code"] == "invalid_xlsx"


def test_xlsx_member_count_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot_sources, "MAX_XLSX_MEMBERS", 4)
    path = tmp_path / "oversized.xlsx"
    body = xlsx()
    path.write_bytes(body)
    result = retrieve_document(reference(kind="local", location=str(path), expected_sha256=hashlib.sha256(body).hexdigest()), format="xlsx")
    assert result.metadata["error_code"] == "invalid_xlsx"


def test_xlsx_duplicate_member_is_rejected(tmp_path):
    buffer = io.BytesIO(xlsx())
    with zipfile.ZipFile(buffer, "a") as archive, pytest.warns(UserWarning, match="Duplicate"):
        archive.writestr("xl/workbook.xml", "<workbook/>")
    body = buffer.getvalue()
    path = tmp_path / "duplicate.xlsx"
    path.write_bytes(body)
    result = retrieve_document(reference(kind="local", location=str(path), expected_sha256=hashlib.sha256(body).hexdigest()), format="xlsx")
    assert result.metadata["error_code"] == "invalid_xlsx"


def test_xlsx_corrupt_archive_is_rejected(tmp_path):
    body = xlsx()[:-40]
    path = tmp_path / "corrupt.xlsx"
    path.write_bytes(body)
    result = retrieve_document(reference(kind="local", location=str(path), expected_sha256=hashlib.sha256(body).hexdigest()), format="xlsx")
    assert result.metadata["error_code"] == "invalid_xlsx"


@pytest.mark.parametrize("body", [b"not a PDF", b"%PDF-1.4\nno trailer"])
def test_pdf_structure_is_required_even_with_approved_hash(tmp_path, body):
    path = tmp_path / "invalid.pdf"
    path.write_bytes(body)
    result = retrieve_pdf(reference(kind="local", location=str(path), expected_sha256=hashlib.sha256(body).hexdigest()))
    assert result.metadata["error_code"] == "invalid_pdf"


def test_local_document_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot_sources, "MAX_DOCUMENT_BYTES", len(PDF) - 1)
    path = tmp_path / "source.pdf"
    path.write_bytes(PDF)
    result = retrieve_pdf(reference(kind="local", location=str(path)))
    assert result.metadata["error_code"] == "size_limit_exceeded"
    assert not result.metadata["bytes_retrieved"]


def test_oversized_remote_metadata_stops_before_content(monkeypatch):
    monkeypatch.setattr(pilot_sources, "MAX_DOCUMENT_BYTES", len(PDF) - 1)
    opener = Opener([metadata()])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "size_limit_exceeded"
    assert result.metadata["failed_stage"] == "A_metadata"
    assert len(opener.requests) == 1


@pytest.mark.parametrize("redirected", [False, True])
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "34"}, {"Content-Length": "1000"}])
def test_remote_byte_limit_is_enforced_even_when_metadata_underreports(monkeypatch, redirected, headers):
    monkeypatch.setattr(pilot_sources, "MAX_DOCUMENT_BYTES", len(PDF))
    download = Response(200, PDF + b"oversized", headers)
    responses = [metadata()]
    if redirected:
        responses.append(Response(302, headers={"Location": SIGNED_URL}))
    responses.append(download)
    opener = Opener(responses)
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "size_limit_exceeded"
    assert result.metadata["failed_stage"] == ("C_download" if redirected else "B_content")
    assert not result.metadata["bytes_retrieved"]
    assert result.content == b""
    assert all(size == len(PDF) + 1 for size in download.read_limits)


def test_metadata_response_has_its_own_small_byte_limit():
    response = Response(200, b" " * (pilot_sources.MAX_METADATA_BYTES + 1))
    opener = Opener([response])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "size_limit_exceeded"
    assert response.read_limits == [pilot_sources.MAX_METADATA_BYTES + 1]
    assert not result.metadata["bytes_retrieved"]


@pytest.mark.parametrize("length", ["-1", "invalid", "9" * 100, str(len(PDF) - 1), str(len(PDF) + 1)])
def test_malformed_or_inconsistent_content_length_is_rejected(length):
    opener = Opener([metadata(), Response(200, PDF, {"Content-Length": length})])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "invalid_content_length"
    assert result.metadata["failed_stage"] == "B_content"
    assert not result.metadata["bytes_retrieved"]


@pytest.mark.parametrize("body", [b"[]", b"null", b"not JSON"])
def test_malformed_metadata_never_establishes_access(body):
    opener = Opener([Response(200, body)])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "invalid_metadata"
    assert not result.metadata["bytes_retrieved"]
    assert len(opener.requests) == 1


@pytest.mark.parametrize("size", [None, True, "34", -1, 0])
def test_metadata_requires_a_positive_integer_size(size):
    opener = Opener([metadata(size=size)])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "invalid_metadata"
    assert not result.metadata["bytes_retrieved"]
    assert len(opener.requests) == 1


@pytest.mark.parametrize("changes", [
    {"id": "different-item"}, {"webUrl": "https://other.sharepoint.com/document.pdf"},
    {"eTag": None}, {"eTag": ""}, {"eTag": 12},
])
@pytest.mark.parametrize("after_download", [False, True])
def test_both_metadata_snapshots_must_match_approved_identity(changes, after_download):
    responses = [metadata(), Response(200, PDF)] if after_download else []
    responses.append(metadata(**changes))
    result = retrieve_pdf(reference(), credential=credential(), opener=Opener(responses))
    assert result.metadata["error_code"] == "source_identity_mismatch"
    assert result.metadata["failed_stage"] == ("D_metadata" if after_download else "A_metadata")
    assert result.metadata["bytes_retrieved"] is after_download
    assert not result.metadata["version_verified"]


def test_metadata_size_change_blocks_document():
    opener = Opener([metadata(), Response(200, PDF), metadata(size=len(PDF) + 1)])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["error_code"] == "version_changed"
    assert result.metadata["bytes_retrieved"]
    assert not result.metadata["version_verified"]


def test_unsupported_format_does_not_read_or_request():
    opener = Opener([])
    result = retrieve_document(reference(), format=cast(Any, "docx"), opener=opener)
    assert result.metadata["error_code"] == "unsupported_format"
    assert opener.requests == []


def test_installed_azure_cli_credential_is_the_only_default(monkeypatch):
    import azure.identity
    from azure.core.credentials import AccessToken

    cli_class = azure.identity.AzureCliCredential
    calls = []
    constructors = []

    def token(self, *scopes, **kwargs):
        calls.append(scopes)
        return AccessToken("installed-sdk-offline-token", 2**31)

    def construct(**kwargs):
        constructors.append(kwargs)
        return cli_class(**kwargs)

    def no_chain(*args, **kwargs):
        raise AssertionError("Retrieval must not discover another identity")

    monkeypatch.setattr(cli_class, "get_token", token)
    monkeypatch.setattr(azure.identity, "AzureCliCredential", construct)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", no_chain)
    opener = Opener([metadata(), Response(200, PDF), metadata()])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    result = retrieve_pdf(reference(tenant_id="00000000-0000-0000-0000-000000000001"))
    assert result.metadata["status"] == "success"
    assert calls == [("https://graph.microsoft.com/.default",)]
    assert constructors == [{"tenant_id": "00000000-0000-0000-0000-000000000001"}]


def test_hosted_managed_identity_is_supplied_not_discovered(monkeypatch):
    import azure.identity
    from azure.core.credentials import AccessToken

    supplied = azure.identity.ManagedIdentityCredential()
    calls = []

    def token(*scopes, **kwargs):
        calls.append(scopes)
        return AccessToken("installed-sdk-offline-token", 2**31)

    def no_cli(*args, **kwargs):
        raise AssertionError("Hosted retrieval must not construct an Azure CLI credential")

    monkeypatch.setattr(supplied, "get_token", token)
    monkeypatch.setattr(azure.identity, "AzureCliCredential", no_cli)
    opener = Opener([metadata(), Response(302, headers={"Location": SIGNED_URL}), Response(200, PDF), metadata()])
    result = retrieve_pdf(reference(), credential=supplied, opener=opener)
    assert result.metadata["status"] == "success"
    assert calls == [("https://graph.microsoft.com/.default",)]
    assert opener.requests[2].get_header("Authorization") is None
    supplied.close()


def test_credential_failure_is_sanitized_without_fallback():
    def reject(scope):
        raise RuntimeError("private-secret " + SIGNED_URL)

    opener = Opener([])
    result = retrieve_pdf(reference(), credential=SimpleNamespace(get_token=reject), opener=opener)
    assert result.metadata["error_code"] == "credential_unavailable"
    assert result.metadata["failed_stage"] == "credential"
    assert opener.requests == []
    assert "private-secret" not in json.dumps(result.metadata)


@pytest.mark.parametrize("token", [None, "", "bad token", "bad\r\ntoken"])
def test_invalid_credential_token_never_reaches_transport(token):
    opener = Opener([])
    supplied = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token=token))
    result = retrieve_pdf(reference(), credential=supplied, opener=opener)
    assert result.metadata["error_code"] == "credential_unavailable"
    assert opener.requests == []