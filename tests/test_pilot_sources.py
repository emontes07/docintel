import hashlib
import io
import json
from types import SimpleNamespace

from backend.core.pilot_sources import SourceReference, retrieve_pdf


PDF = b"%PDF-1.4\nsynthetic test content\n%%EOF"


class Response(io.BytesIO):
    def __init__(self, code, body=b"", headers=None):
        super().__init__(body)
        self.code = code
        self.headers = headers or {}


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return next(self.responses)


def reference(**overrides):
    values = dict(source_id="ford-sharepoint", kind="sharepoint", location="https://tenant.sharepoint.com/document.pdf", expected_sha256=hashlib.sha256(PDF).hexdigest(), drive_id="resolved-drive", item_id="resolved-item")
    return SourceReference(**(values | overrides))


def metadata(etag="version-a"):
    return Response(200, json.dumps({"id": "resolved-item", "webUrl": reference().location, "eTag": etag, "size": len(PDF)}).encode())


def credential():
    return SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="graph-secret"))


def test_redirect_credentials_and_version_integrity():
    signed_url = "https://download.example/document?signature=a%2Fb%2Bz&other=1"
    opener = Opener([metadata(), Response(302, headers={"Location": signed_url}), Response(200, PDF), metadata()])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["status"] == "success"
    assert result.metadata["version_verified"]
    assert result.metadata["etag"] != result.metadata["sha256"]
    assert opener.requests[2].full_url == signed_url
    assert opener.requests[2].get_header("Authorization") is None
    assert all(opener.requests[index].get_header("Authorization") == "Bearer graph-secret" for index in (0, 1, 3))
    assert "signature" not in json.dumps(result.metadata)


def test_download_failure_stops_without_fallback_or_retry():
    opener = Opener([metadata(), Response(302, headers={"Location": "https://download.example/?secret=hidden"}), Response(401)])
    result = retrieve_pdf(reference(), credential=credential(), opener=opener)
    assert result.metadata["failed_stage"] == "C_download"
    assert not result.metadata["bytes_retrieved"]
    assert len(opener.requests) == 3
    assert "hidden" not in json.dumps(result.metadata)


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