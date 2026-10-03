"""Bounded, version-pinned PDF/XLSX retrieval for approved internal sources.

The default credential is Azure CLI only; hosted callers supply their existing
managed-identity credential. A Graph metadata response does not establish byte
access. Redirected downloads are unauthenticated, single-hop, and restricted to
the exact tenant SharePoint host in the approved source reference.
"""

import hashlib
import io
import json
import re
import urllib.error
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NoReturn
from urllib.parse import urlsplit
from xml.etree import ElementTree

from pydantic import Field

from backend.models.enrichment import Contract


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_XLSX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_XLSX_MEMBERS = 1000


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
    """Compatibility entry point for existing PDF-only pilot callers."""
    return retrieve_document(reference, format="pdf", credential=credential, opener=opener)


def _https_host(url: str) -> str:
    if not isinstance(url, str) or not url or any(character.isspace() or ord(character) < 32 for character in url) or "\\" in url:
        raise ValueError("Invalid HTTPS destination")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment or "#" in url
        or parsed.netloc.lower() != parsed.hostname
        or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)
    ):
        raise ValueError("Invalid HTTPS destination")
    return parsed.hostname


def _valid_xlsx(content: bytes) -> bool:
    """Validate a bounded, macro-free OOXML package without extracting files."""
    if not content.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            required = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
            if (
                len(members) > MAX_XLSX_MEMBERS or len(names) != len(set(names))
                or not required.issubset(names)
                or not any(re.fullmatch(r"xl/worksheets/[^/]+\.xml", name) for name in names)
                or sum(member.file_size for member in members) > MAX_XLSX_EXPANDED_BYTES
            ):
                return False
            expanded = 0
            xml_parts = {}
            for member in members:
                if (
                    member.flag_bits & 1 or member.filename.startswith("/")
                    or "\\" in member.filename or ".." in member.filename.split("/")
                    or member.filename.lower().endswith("/vbaproject.bin")
                ):
                    return False
                with archive.open(member) as stream:
                    body = stream.read(MAX_XLSX_EXPANDED_BYTES - expanded + 1)
                expanded += len(body)
                if expanded > MAX_XLSX_EXPANDED_BYTES:
                    return False
                if member.filename in required or re.fullmatch(r"xl/worksheets/[^/]+\.xml", member.filename):
                    if b"\x00" in body or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                        return False
                    xml_parts[member.filename] = ElementTree.fromstring(body)
            types = xml_parts["[Content_Types].xml"]
            workbook = xml_parts["xl/workbook.xml"]
            package_ns = "{http://schemas.openxmlformats.org/package/2006/content-types}"
            sheet_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            relationship_ns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
            if not (
                types.tag == package_ns + "Types"
                and any(
                    node.get("PartName") == "/xl/workbook.xml"
                    and node.get("ContentType") == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
                    for node in types.findall(package_ns + "Override")
                )
                and workbook.tag == sheet_ns + "workbook"
                and bool(workbook.findall(sheet_ns + "sheets/" + sheet_ns + "sheet"))
                and all(
                    xml_parts[name].tag == relationship_ns + "Relationships"
                    for name in ("_rels/.rels", "xl/_rels/workbook.xml.rels")
                )
            ):
                return False
            office_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            roots = xml_parts["_rels/.rels"].findall(relationship_ns + "Relationship")
            if not any(
                node.get("Type") == office_ns + "/officeDocument"
                and node.get("Target") in ("xl/workbook.xml", "/xl/workbook.xml")
                and node.get("TargetMode", "Internal") == "Internal"
                for node in roots
            ):
                return False
            relationships = xml_parts["xl/_rels/workbook.xml.rels"].findall(relationship_ns + "Relationship")
            by_id = {node.get("Id"): node for node in relationships}
            if len(by_id) != len(relationships):
                return False
            for sheet in workbook.findall(sheet_ns + "sheets/" + sheet_ns + "sheet"):
                relationship = by_id.get(sheet.get("{" + office_ns + "}id"))
                if (
                    relationship is None
                    or relationship.get("Type") != office_ns + "/worksheet"
                    or relationship.get("TargetMode", "Internal") != "Internal"
                ):
                    return False
                target = relationship.get("Target", "")
                target = target.lstrip("/") if target.startswith("/") else "xl/" + target
                if not re.fullmatch(r"xl/worksheets/[^/]+\.xml", target) or target not in xml_parts:
                    return False
                if xml_parts[target].tag != sheet_ns + "worksheet":
                    return False
            return True
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, zlib.error, ElementTree.ParseError):
        return False


def retrieve_document(
    reference: SourceReference, *, format: Literal["pdf", "xlsx"] = "pdf",
    credential=None, opener=None,
) -> RetrievedPDF:
    """Retrieve approved bytes; consumers must require metadata status=success.

    No provider exception text, access token, or preauthenticated download URL
    is recorded. ``opener`` is an injectable transport for offline tests; real
    urllib openers must disable redirects and must not add cookies or auth.
    """
    result = RetrievedPDF(metadata={
        "source_id": reference.source_id, "kind": reference.kind,
        "location": reference.location if reference.kind == "local" else None,
        "format": format, "status": "failed", "stages": [],
        "sha256": None, "etag": None, "version_verified": False,
        "bytes_retrieved": False,
    })
    stage = "local_read" if reference.kind == "local" else "A_metadata"
    download_host = None

    def fail(code: str) -> NoReturn:
        result.metadata["error_code"] = code
        raise ValueError("Source retrieval rejected")

    def request(url: str, *, token: str | None = None, redirect=False, limit=MAX_METADATA_BYTES):
        try:
            host = _https_host(url)
        except ValueError:
            fail("invalid_download_destination" if stage == "C_download" else "invalid_source_reference")
        if host != ("graph.microsoft.com" if token else download_host):
            fail("invalid_download_destination")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = opener.open(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            code = response.code
            result.metadata["stages"].append({"stage": stage, "http_status": code})
            if code != 200 and not (redirect and code == 302):
                fail({401: "authentication_rejected", 403: "access_forbidden", 429: "throttled"}.get(code, "http_rejected"))
            if code == 302:
                return response.headers, None
            declared = response.headers.get("Content-Length")
            if declared is not None:
                if len(str(declared)) > 20 or not re.fullmatch(r"[0-9]+", str(declared)):
                    fail("invalid_content_length")
                if int(declared) > limit:
                    fail("size_limit_exceeded")
            body = response.read(limit + 1)
            if len(body) > limit:
                fail("size_limit_exceeded")
            if declared is not None and int(declared) != len(body):
                fail("invalid_content_length")
            return response.headers, body

    def metadata(body):
        try:
            value = json.loads(body)
        except (TypeError, ValueError):
            fail("invalid_metadata")
        if not isinstance(value, dict):
            fail("invalid_metadata")
        if (
            value.get("id") != reference.item_id or value.get("webUrl") != reference.location
            or not isinstance(value.get("eTag"), str) or not value["eTag"].strip()
        ):
            fail("source_identity_mismatch")
        if type(value.get("size")) is not int or value["size"] <= 0:
            fail("invalid_metadata")
        if value["size"] > MAX_DOCUMENT_BYTES:
            fail("size_limit_exceeded")
        return value

    try:
        if not reference.enabled:
            fail("source_disabled")
        if format not in ("pdf", "xlsx"):
            fail("unsupported_format")
        if reference.kind == "local":
            path = Path(reference.location).expanduser().resolve(strict=True)
            result.metadata["location"] = str(path)
            if path.stat().st_size > MAX_DOCUMENT_BYTES:
                fail("size_limit_exceeded")
            with path.open("rb") as stream:
                content = stream.read(MAX_DOCUMENT_BYTES + 1)
            if len(content) > MAX_DOCUMENT_BYTES:
                fail("size_limit_exceeded")
            result.metadata["stages"].append({"stage": stage, "status": "success"})
        else:
            try:
                download_host = _https_host(reference.location)
            except ValueError:
                fail("invalid_source_reference")
            if (
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.sharepoint\.com", download_host)
                or "?" in reference.location
                or not reference.drive_id or not reference.item_id
                or any(not re.fullmatch(r"[A-Za-z0-9!_-]{1,512}", value) for value in (reference.drive_id, reference.item_id))
            ):
                fail("invalid_source_reference")
            result.metadata["location"] = reference.location
            opener = opener or urllib.request.build_opener(_NoRedirect())
            if isinstance(opener, urllib.request.OpenerDirector) and any(
                isinstance(handler, (urllib.request.HTTPCookieProcessor, urllib.request.AbstractBasicAuthHandler, urllib.request.AbstractDigestAuthHandler))
                or isinstance(handler, urllib.request.HTTPRedirectHandler) and not isinstance(handler, _NoRedirect)
                for handler in opener.handlers
            ):
                fail("unsafe_transport")
            if isinstance(opener, urllib.request.OpenerDirector) and any(
                name.lower() in ("authorization", "proxy-authorization", "cookie")
                for name, _ in opener.addheaders
            ):
                fail("unsafe_transport")
            stage = "credential"
            if credential is None:
                from azure.identity import AzureCliCredential

                credential = AzureCliCredential(tenant_id=reference.tenant_id)
            token = credential.get_token("https://graph.microsoft.com/.default").token
            if not isinstance(token, str) or not token or any(character.isspace() for character in token):
                fail("credential_unavailable")
            base = f"https://graph.microsoft.com/v1.0/drives/{reference.drive_id}/items/{reference.item_id}"
            metadata_url = base + "?$select=id,name,webUrl,size,eTag,cTag,lastModifiedDateTime"
            stage = "A_metadata"
            _, body = request(metadata_url, token=token)
            before = metadata(body)
            result.metadata.update({
                "drive_id": reference.drive_id, "item_id": reference.item_id,
                "etag": before["eTag"], "ctag": before.get("cTag"),
                "modified_at": before.get("lastModifiedDateTime"),
            })
            stage = "B_content"
            headers, content = request(base + "/content", token=token, redirect=True, limit=MAX_DOCUMENT_BYTES)
            if content is None:
                stage = "C_download"
                download_url = headers.get("Location")
                if not download_url:
                    fail("invalid_download_destination")
                _, content = request(download_url, limit=MAX_DOCUMENT_BYTES)
            result.content = content
            result.metadata["bytes_retrieved"] = True
            result.metadata["sha256"] = hashlib.sha256(content).hexdigest()
            stage = "D_metadata"
            _, body = request(metadata_url, token=token)
            after = metadata(body)
            result.metadata["etag_after"] = after.get("eTag")
            if after["eTag"] != before["eTag"] or after["size"] != len(content) or before["size"] != len(content):
                fail("version_changed")
            result.metadata["version_verified"] = True
        result.content = content
        result.metadata["bytes_retrieved"] = True
        result.metadata["sha256"] = hashlib.sha256(content).hexdigest()
        stage = "integrity"
        if format == "pdf" and (not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]):
            fail("invalid_pdf")
        if format == "xlsx" and not _valid_xlsx(content):
            fail("invalid_xlsx")
        if result.metadata["sha256"] != reference.expected_sha256:
            fail("unapproved_content_version")
        result.metadata.update({"status": "success", "size": len(content), "content_version": "sha256:" + result.metadata["sha256"]})
    except Exception as error:
        error_code = result.metadata.get("error_code") or (
            "not_found" if isinstance(error, FileNotFoundError) else
            "local_access_denied" if isinstance(error, PermissionError) else
            "network_failure" if isinstance(error, (TimeoutError, urllib.error.URLError)) else
            "credential_unavailable" if stage == "credential" else
            "retrieval_failed"
        )
        explanations = {
            "source_disabled": "This source is disabled. Resolve the recorded access failure before authorizing another retrieval; no alternate source was used.",
            "not_found": "The configured local document is missing. Restore the approved file at its configured location; the web API does not accept another path.",
            "local_access_denied": "The configured local document is not readable by this OS user. Check local file access without changing cloud permissions.",
            "authentication_rejected": "The source endpoint rejected authentication. Keep SharePoint disabled and give the recorded stage/status to the administrator; a missing permission is not established.",
            "access_forbidden": "The source endpoint denied access. Ask the site administrator to review the recorded stage and applicable download policy; no policy bypass was attempted.",
            "throttled": "The source endpoint throttled this request. No retry was made; inspect the recorded outcome before authorizing another run.",
            "network_failure": "Source transport failed. Check the approved network path before authorizing another run; no automatic retry was made.",
            "version_changed": "Source metadata changed or did not match the downloaded size. Do not use this parse; verify the approved source version before another run.",
            "invalid_pdf": "The retrieved bytes did not validate as a PDF. Check the configured source; no parser or model request was made.",
            "invalid_xlsx": "The retrieved bytes did not validate as a bounded XLSX package. Check the approved workbook; no parser or model request was made.",
            "unapproved_content_version": "The document hash does not match the approved source. Restore the approved document; do not substitute or approve a new version through the web API.",
            "size_limit_exceeded": "Source content or metadata exceeds the allowed size. No oversized content was accepted; no retry was made.",
            "invalid_download_destination": "The content redirect was missing or not an HTTPS URL on the approved tenant SharePoint host. No alternate host was requested.",
            "invalid_source_reference": "The configured source requires a canonical HTTPS tenant SharePoint URL and resolved drive/item IDs. Correct the approved configuration before retrieval.",
            "source_identity_mismatch": "Source metadata did not match the approved item, canonical location, or required version. No source substitution was accepted.",
            "invalid_metadata": "The source did not return valid item metadata with a positive byte size. Metadata access alone does not establish download access.",
            "invalid_content_length": "The source response length was malformed or did not match its declared byte size. No partial document was accepted.",
            "credential_unavailable": "The configured credential could not supply a Graph access token. Local pilots require the approved Azure CLI identity; hosted callers must supply their managed-identity credential.",
            "unsafe_transport": "The supplied transport can redirect automatically or attach cookies/authentication. Use the bounded transport without cookie or credential handlers.",
            "unsupported_format": "Only explicitly selected PDF and XLSX documents are supported.",
        }
        result.metadata.update({
            "failed_stage": stage,
            "error_code": error_code,
            "explanation": explanations.get(error_code, "Source retrieval failed. Inspect the recorded stage/status and configured source. No fallback or retry attempted; provider details withheld."),
        })
    return result