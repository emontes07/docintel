"""Explicit, bounded PDF retrieval for the configured single-product pilot."""

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from backend.models.enrichment import Contract


class SourceReference(Contract):
    source_id: str = Field(pattern=r"^[a-z0-9-]+$")
    kind: Literal["local", "sharepoint"]
    location: str
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    drive_id: str | None = None
    item_id: str | None = None
    tenant_id: str | None = None
    availability: str = "configured"
    enabled: bool = True


@dataclass
class RetrievedPDF:
    metadata: dict
    content: bytes = field(default=b"", repr=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def retrieve_pdf(reference: SourceReference, *, credential=None, opener=None) -> RetrievedPDF:
    result = RetrievedPDF(metadata={
        "source_id": reference.source_id, "kind": reference.kind,
        "location": reference.location, "status": "failed", "stages": [],
        "sha256": None, "etag": None, "version_verified": False,
        "bytes_retrieved": False,
    })
    stage = "local_read" if reference.kind == "local" else "A_metadata"
    opener = opener or urllib.request.build_opener(_NoRedirect())

    def request(url: str, *, token: str | None = None, redirect=False):
        parsed_url = urlsplit(url)
        if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.fragment:
            raise ValueError("Invalid HTTPS destination")
        if token and parsed_url.netloc != "graph.microsoft.com":
            raise ValueError("Invalid Graph destination")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = opener.open(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            code = response.code
            result.metadata["stages"].append({"stage": stage, "http_status": code})
            if code != 200 and not (redirect and code == 302):
                result.metadata["error_code"] = {401: "authentication_rejected", 403: "access_forbidden", 429: "throttled"}.get(code, "http_rejected")
                raise ValueError("Request rejected")
            return response.headers, response.read(10 * 1024 * 1024 + 1) if code == 200 else None

    try:
        if not reference.enabled:
            result.metadata["error_code"] = "source_disabled"
            raise ValueError("Configured source disabled")
        if reference.kind == "local":
            path = Path(reference.location).expanduser().resolve(strict=True)
            result.metadata["location"] = str(path)
            if path.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("PDF exceeds pilot size limit")
            content = path.read_bytes()
            result.metadata["stages"].append({"stage": stage, "status": "success"})
        else:
            if not reference.drive_id or not reference.item_id:
                raise ValueError("Missing resolved source IDs")
            if any(character in reference.drive_id + reference.item_id for character in "/?#%"):
                raise ValueError("Invalid resolved source IDs")
            if credential is None:
                from azure.identity import AzureCliCredential

                credential = AzureCliCredential(tenant_id=reference.tenant_id)
            token = credential.get_token("https://graph.microsoft.com/.default").token
            base = f"https://graph.microsoft.com/v1.0/drives/{reference.drive_id}/items/{reference.item_id}"
            metadata_url = base + "?$select=id,name,webUrl,size,eTag,cTag,lastModifiedDateTime"
            _, body = request(metadata_url, token=token)
            before = json.loads(body)
            if before.get("id") != reference.item_id or before.get("webUrl") != reference.location or not before.get("eTag"):
                raise ValueError("Source identity mismatch")
            result.metadata.update({
                "drive_id": reference.drive_id, "item_id": reference.item_id,
                "etag": before["eTag"], "ctag": before.get("cTag"),
                "modified_at": before.get("lastModifiedDateTime"),
            })
            stage = "B_content"
            headers, content = request(base + "/content", token=token, redirect=True)
            if content is None:
                stage = "C_download"
                download_url = headers.get("Location")
                if not download_url:
                    raise ValueError("Missing download location")
                _, content = request(download_url)
            result.content = content
            result.metadata["bytes_retrieved"] = True
            result.metadata["sha256"] = hashlib.sha256(content).hexdigest()
            stage = "D_metadata"
            _, body = request(metadata_url, token=token)
            after = json.loads(body)
            result.metadata["etag_after"] = after.get("eTag")
            if after.get("id") != reference.item_id or after.get("eTag") != before["eTag"] or after.get("size") != len(content) or before.get("size") != len(content):
                result.metadata["error_code"] = "version_changed"
                raise ValueError("Source version changed")
            result.metadata["version_verified"] = True
        result.content = content
        result.metadata["bytes_retrieved"] = True
        result.metadata["sha256"] = hashlib.sha256(content).hexdigest()
        stage = "integrity"
        if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
            result.metadata["error_code"] = "invalid_pdf"
            raise ValueError("Invalid PDF")
        if result.metadata["sha256"] != reference.expected_sha256:
            result.metadata["error_code"] = "unapproved_content_version"
            raise ValueError("PDF does not match approved source")
        result.metadata.update({"status": "success", "size": len(content), "content_version": "sha256:" + result.metadata["sha256"]})
    except Exception as error:
        error_code = result.metadata.get("error_code") or (
            "not_found" if isinstance(error, FileNotFoundError) else
            "local_access_denied" if isinstance(error, PermissionError) else
            "network_failure" if isinstance(error, (TimeoutError, urllib.error.URLError)) else
            "retrieval_failed"
        )
        explanations = {
            "source_disabled": "This source is disabled. Resolve the recorded access failure before authorizing another retrieval; no alternate source was used.",
            "not_found": "The configured local PDF is missing. Restore the approved file at its configured location; the web API does not accept another path.",
            "local_access_denied": "The configured local PDF is not readable by this OS user. Check local file access without changing cloud permissions.",
            "authentication_rejected": "The source endpoint rejected authentication. Keep SharePoint disabled and give the recorded stage/status to the administrator; a missing permission is not established.",
            "access_forbidden": "The source endpoint denied access. Ask the site administrator to review the recorded stage and applicable download policy; no policy bypass was attempted.",
            "throttled": "The source endpoint throttled this request. No retry was made; inspect the recorded outcome before authorizing another run.",
            "network_failure": "Source transport failed. Check the approved network path before authorizing another run; no automatic retry was made.",
            "version_changed": "Source metadata changed or did not match the downloaded size. Do not use this parse; verify the approved source version before another run.",
            "invalid_pdf": "The retrieved bytes did not validate as a PDF. Check the configured source; no parser or model request was made.",
            "unapproved_content_version": "The PDF hash does not match the approved pilot. Restore the approved PDF; do not substitute or approve a new version through the web API.",
        }
        result.metadata.update({
            "failed_stage": stage,
            "error_code": error_code,
            "explanation": explanations.get(error_code, "Source retrieval failed. Inspect the recorded stage/status and configured source. No fallback or retry attempted; provider details withheld."),
        })
    return result