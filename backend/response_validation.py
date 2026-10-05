"""Bounded, text-free response diagnostics and exact citation aliases."""

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import ValidationError

from backend.models.enrichment import (
    Candidate, DiagnosticReference, ExtractionResponse,
    ResponseValidationDiagnostic, ValidationIssue,
)

MAX_CANDIDATES = 64
MAX_REFERENCES = 512
MAX_VALID_REFERENCES = 4096
MAX_ISSUES = 32


class ResponseValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        self.diagnostic: ResponseValidationDiagnostic | None = None
        super().__init__(issues[0].message)


def invalid(field_path: str, message: str) -> ResponseValidationError:
    return ResponseValidationError([ValidationIssue(field_path=field_path, message=message)])


def schema_issues(error: Exception) -> list[ValidationIssue]:
    if not isinstance(error, ValidationError):
        return [ValidationIssue(field_path="$", message="Response is not valid JSON or has no content.")]
    messages = {
        "missing": "Required response field is missing.",
        "extra_forbidden": "Unexpected response field is not allowed.",
        "too_short": "Response field has too few entries; every candidate requires citations.",
        "list_type": "Expected a JSON list.",
        "string_type": "Expected a string.",
        "literal_error": "Response field is not an allowed literal.",
    }
    issues = []
    for detail in error.errors(include_input=False, include_context=False, include_url=False):
        path = ""
        for part in detail["loc"]:
            if type(part) is int:
                path += f"[{part}]"
            else:
                name = part if part in {"candidates", *Candidate.model_fields} else "<redacted-field>"
                path += ("." if path else "") + name
        issues.append(ValidationIssue(
            field_path=path or "$",
            message=messages.get(detail["type"], "Response field does not satisfy the declared schema."),
        ))
    return issues


def map_citations(response: ExtractionResponse, references: dict[str, str]) -> ExtractionResponse:
    originals = set(references.values())
    mapped = response.model_copy(deep=True)
    issues = []
    for index, candidate in enumerate(mapped.candidates):
        citations = []
        for offset, supplied in enumerate(candidate.evidence_ids):
            reference = supplied.strip()
            if re.fullmatch(r"[Ee][1-9][0-9]*", reference) and reference.upper() in references:
                citations.append(references[reference.upper()])
            elif reference in originals:
                citations.append(reference)
            else:
                issues.append(ValidationIssue(
                    field_path=f"candidates[{index}].evidence_ids[{offset}]",
                    message="Unknown citation reference. Use an exact row reference or an exact original evidence ID, not a group or location suffix.",
                ))
        candidate.evidence_ids = list(dict.fromkeys(citations))
    if issues:
        raise ResponseValidationError(issues)
    return mapped


def response_diagnostic(
    *, payload: Any, references: dict[str, str], issues: list[ValidationIssue],
    stage: Literal["structured_response_parsing", "evidence_validation"] = "evidence_validation",
    raw_response_sha256: str | None = None,
) -> ResponseValidationDiagnostic:
    """Only known keys, structural types and allowlisted citation tokens survive."""
    used = []
    truncated = len(references) > MAX_VALID_REFERENCES or len(issues) > MAX_ISSUES
    known = set(references) | set(references.values())
    allowed_keys = {"candidates", *Candidate.model_fields}
    if not isinstance(raw_response_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", raw_response_sha256):
        raw_response_sha256 = None

    def sanitize(value: Any, path: str = "$", field: str = "", depth: int = 0):
        nonlocal truncated
        if depth > 6:
            truncated = True
            return {"redacted": True, "reason": "depth_limit"}
        if isinstance(value, dict):
            output = {}
            for index, (key, child) in enumerate(value.items()):
                if index >= MAX_CANDIDATES:
                    truncated = True
                    break
                name = key if key in allowed_keys else f"<redacted-field-{index}>"
                output[name] = sanitize(child, name if path == "$" else path + "." + name, name, depth + 1)
            return output
        if isinstance(value, list):
            limit = MAX_REFERENCES if field == "evidence_ids" else MAX_CANDIDATES
            truncated |= len(value) > limit
            return [sanitize(child, f"{path}[{index}]", field, depth + 1)
                    for index, child in enumerate(value[:limit])]
        if field == "evidence_ids" and isinstance(value, str):
            safe = len(value) <= 512 and (
                value.strip() in known
                or re.fullmatch(r"\s*(?:[Ee][0-9]{1,7})(?:\s*,\s*[Ee][0-9]{1,7})*\s*", value)
                or re.fullmatch(r"(?:group[ :=-]?)?[0-9]{1,7}", value)
            )
            entry = DiagnosticReference(
                field_path=path, value=value if safe else None,
                sha256=hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest(), redacted=not safe,
            )
            if len(used) < MAX_REFERENCES:
                used.append(entry)
            else:
                truncated = True
            return entry.model_dump(mode="json")
        return {"redacted": True, "type": type(value).__name__}

    structure = sanitize(payload)
    if not isinstance(structure, dict):
        structure = {"root": structure}
    return ResponseValidationDiagnostic(
        stage=stage, issues=issues[:MAX_ISSUES], used_references=used,
        valid_references=list(references)[:MAX_VALID_REFERENCES],
        parsed_response=structure, raw_response_sha256=raw_response_sha256,
        raw_response_hash_basis="provider_content" if raw_response_sha256 else "unavailable",
        truncated=truncated,
    )


def parsed_content(content: str | None):
    if content is None:
        return None
    try:
        return json.loads(content)
    except (ValueError, RecursionError):
        return None
