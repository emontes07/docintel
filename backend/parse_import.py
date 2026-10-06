"""Offline, provenance-preserving preparation of explicitly attested parse imports.

This does not retarget a local analysis to Blob/SharePoint or authorize analysis.
The original cache envelope and all input bytes are retained without rewriting.
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.core.docintel import ParsedDocument
from backend.pilot import PARSER_VERSION


IMPORT_FORMAT = "docintel-parse-import-v1"
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


class ParseImportError(ValueError):
    def __init__(self, codes: list[str]):
        self.codes = codes
        super().__init__("Parse import refused: " + ", ".join(codes))


def _object(content: bytes, code: str) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=unique_pairs)
        canonical(value)
        if not isinstance(value, dict):
            raise ValueError("Expected an object")
        return value
    except (ValueError, TypeError, UnicodeError):
        raise ParseImportError([code]) from None


@dataclass(frozen=True)
class Artifact:
    """An original artifact pinned independently by the caller, not self-attested."""

    path: str
    expected_sha256: str
    content: bytes = field(repr=False)

    def verify(self, name: str) -> None:
        if not self.path or not re.fullmatch(r"[a-f0-9]{64}", self.expected_sha256):
            raise ParseImportError([name + "_invalid_pin"])
        if not self.content or len(self.content) > MAX_ARTIFACT_BYTES:
            raise ParseImportError([name + "_size_limit"])
        if sha256(self.content) != self.expected_sha256:
            raise ParseImportError([name + "_hash_mismatch"])

    def metadata(self) -> dict:
        return {"path": self.path, "sha256": self.expected_sha256, "bytes": len(self.content)}


@dataclass(frozen=True)
class PreparedParseImport:
    cache_key: str
    fingerprint: str
    provenance: dict
    artifacts: dict[str, Artifact] = field(repr=False)

    def records(self) -> dict[str, bytes]:
        prefix = f"parse-imports/{self.fingerprint}/"
        return {
            self.cache_key: self.artifacts["cache"].content,
            prefix + "provenance.json": canonical(self.provenance),
            **{prefix + name: artifact.content for name, artifact in self.artifacts.items()},
        }


def prepare_parse_import(
    *, source: Artifact, parsed: Artifact, cache: Artifact, receipt: Artifact,
    request_options: dict,
) -> PreparedParseImport:
    """Validate prior analysis, returning append-only records for an isolated store.

    The receipt must independently record the actual request contract. A later
    wrapper's parser_version, today's SDK defaults, or represented pages cannot
    establish the historical API version or the pages/features requested.
    """
    artifacts = {"source": source, "parsed": parsed, "cache": cache, "receipt": receipt}
    for name, artifact in artifacts.items():
        artifact.verify(name)
    if not source.content.startswith(b"%PDF-") or b"%%EOF" not in source.content[-1024:]:
        raise ParseImportError(["invalid_source_pdf"])
    if type(request_options) is not dict or request_options not in ({}, {"pages": "1-5"}):
        raise ParseImportError(["request_options_not_representable_by_production_cache"])
    original = _object(parsed.content, "invalid_parsed_json")
    envelope = _object(cache.content, "invalid_cache_json")
    record = _object(receipt.content, "invalid_receipt_json")
    try:
        document = ParsedDocument.model_validate(original)
        cached_document = ParsedDocument.model_validate(envelope["document"])
    except (KeyError, ValueError, TypeError):
        raise ParseImportError(["invalid_parsed_document"]) from None
    if set(original) != set(ParsedDocument.model_fields):
        raise ParseImportError(["unrecognized_parsed_document_shape"])
    if document != cached_document or original != envelope["document"]:
        raise ParseImportError(["cache_does_not_preserve_original_document"])
    if document.source != source.path:
        raise ParseImportError(["original_source_path_mismatch"])
    if document.cache_key != "sha256:" + source.expected_sha256:
        raise ParseImportError(["original_source_hash_mismatch"])
    if document.parsed_at.tzinfo is None:
        raise ParseImportError(["original_analysis_time_not_qualified"])
    document_sha256 = sha256(document.model_dump_json().encode())
    if envelope.get("document_sha256") != document_sha256:
        raise ParseImportError(["cache_document_digest_mismatch"])
    if envelope.get("parser_version") != PARSER_VERSION:
        raise ParseImportError(["incompatible_parser_version"])
    if not isinstance(envelope.get("origin"), str) or not envelope["origin"].strip():
        raise ParseImportError(["missing_original_cache_origin"])
    model, api_version, mapping = PARSER_VERSION.split(":")
    analysis = record.get("analysis")
    if not isinstance(analysis, dict):
        raise ParseImportError(["missing_success_receipt"])
    expected = {
        "outcome": "succeeded", "submissions": 1, "sdk_retries": 0, "model": model,
        "api_version": api_version, "mapping_version": mapping,
        "request_options": request_options, "source": document.source,
        "source_sha256": source.expected_sha256, "parsed_sha256": parsed.expected_sha256,
    }
    codes = []
    for name, value in expected.items():
        if name not in analysis:
            codes.append("receipt_missing_" + name)
        elif type(analysis[name]) is not type(value) or analysis[name] != value:
            codes.append("receipt_mismatch_" + name)
    if codes:
        raise ParseImportError(codes)
    suffix = ":pages=1-5" if request_options else ""
    cache_key = "parses/" + sha256((document.source + source.expected_sha256 + PARSER_VERSION + suffix).encode()) + ".json"
    contract = {
        "format": IMPORT_FORMAT, "source": document.source,
        "source_sha256": source.expected_sha256, "model": model, "api_version": api_version,
        "mapping_version": mapping, "request_options": request_options,
        "parser_version": PARSER_VERSION, "production_cache_key": cache_key,
        "document_sha256": document_sha256,
        "original_artifacts": {name: artifact.metadata() for name, artifact in artifacts.items()},
    }
    fingerprint = sha256(canonical(contract))
    provenance = {
        **contract, "fingerprint": fingerprint,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "original_parsed_at": document.parsed_at.isoformat(),
        "original_cache_origin": envelope["origin"],
        "method": "verified_prior_analysis_import", "freshly_analyzed": False,
        "new_analysis_submissions": 0, "original_analysis_submissions": analysis["submissions"],
        "source_association_rewritten": False, "hosted_ingestion_verified": False,
        "destination_scope": "isolated_offline_store_only",
    }
    return PreparedParseImport(cache_key, fingerprint, provenance, artifacts)


def snapshot_records(content: bytes) -> dict[str, bytes]:
    """Read an exact private snapshot; never drop or regenerate retained records."""
    snapshot = _object(content, "invalid_snapshot")
    if not isinstance(snapshot.get("records"), dict):
        raise ParseImportError(["invalid_snapshot_records"])
    result = {}
    for key, entry in snapshot["records"].items():
        try:
            if not isinstance(key, str) or not key or not isinstance(entry, dict):
                raise ValueError("Invalid record")
            raw = base64.b64decode(entry["base64"], validate=True)
            if sha256(raw) != entry["sha256"]:
                raise ValueError("Invalid record digest")
        except (KeyError, ValueError, TypeError):
            raise ParseImportError(["snapshot_record_integrity_failed"]) from None
        result[key] = raw
    return result


def append_import_records(prior: dict[str, bytes], prepared: PreparedParseImport) -> dict[str, bytes]:
    """Pure append: even an identical existing cache key is never overwritten."""
    additional = prepared.records()
    if prior.keys() & additional.keys():
        raise ParseImportError(["import_record_already_exists"])
    return {**prior, **additional}
