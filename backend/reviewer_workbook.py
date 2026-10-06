"""Offline customer reviewer workbook, separate from private technical exports."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from backend.batch import export_workbook
from backend.models.enrichment import (
    AttributeDefinition, AttributeResult, Candidate, EnrichmentResult, Evidence, Manifest, ProductKey,
)


REVIEW_COLUMNS = [
    "Product ID", "MPN", "Attribute", "Decision", "Correction", "Correction unit", "Reason",
    "Status", "Next action", "Proposed value", "Unit", "Evidence basis",
    "Supporting quote", "Evidence", "Source URL", "Retrieved at", "Applicability",
]
DECISIONS = ("Approve", "Correct", "Reject")


@dataclass(frozen=True)
class ReviewerPackage:
    workbook: bytes
    private_binding: dict
    summary: dict


def reviewer_status(attribute: AttributeResult, result: EnrichmentResult | _PresentationResult) -> tuple[str, str]:
    if attribute.review is not None:
        return {
            "approve": ("Reviewed — accepted", "Accepted by the recorded reviewer."),
            "correct": ("Reviewed — corrected", "Use the recorded reviewer correction."),
            "reject": ("Reviewed — rejected", "Obtain new supporting evidence before proposing a replacement."),
        }[attribute.review.decision]
    if attribute.status == "existing":
        return "Existing value retained", "No replacement proposed; confirm the existing value if needed."
    if attribute.status == "conflict":
        return "Conflicting evidence — review needed", "Resolve the cited conflicting evidence; record a supported correction and reason."
    if attribute.candidates:
        if all(candidate.evidence_basis == "inferred_from_description" for candidate in attribute.candidates):
            return "Descriptive inference — review required", "Confirm the whole-product claim; descriptive wording is not certification."
        return "Proposal ready for review", "Check the supporting quotation and product applicability before deciding."
    if attribute.status == "definition_clarification_needed":
        return "Definition needs clarification", "Confirm the requested definition and unit before evaluating a value."
    if attribute.status == "retrieval_failed":
        return "Source unavailable", "Provide an accessible, applicable source for this attribute."
    if attribute.status == "extraction_failed":
        if result.extraction_error == "invalid_response":
            return "Evidence verification needs attention", "The generated value or citation was not verified; inspect the original source or supply a correction."
        return "Processing unavailable", "No verified proposal was returned; keep the attribute pending and obtain supporting evidence."
    return "Supporting evidence needed", "No supported value was found in the checked sources; provide applicable product evidence."


def _url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
        host = parsed.hostname.casefold()
        if "." not in host or any(host == suffix or host.endswith("." + suffix) for suffix in (
            "sharepoint.com", "sharepoint.cn", "blob.core.windows.net", "dfs.core.windows.net",
            "localhost", "local", "internal",
        )):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        if re.search(r"(?i)(?:[a-f0-9]{64}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})", parsed.path):
            return ""
        if parsed.port not in (None, 443):
            return ""
        return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return ""


def _display(value) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"(?i)\b(?:sha256:)?[a-f0-9]{64}\b", "[private reference]", text)
    text = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "[private reference]", text)
    text = re.sub(r"(?:batchblob|file|az|blob)://\S+", "[internal source]", text)
    text = re.sub(r"(?<![:/\w])/(?:[A-Za-z0-9_.-]+/)+\S+|[A-Za-z]:\\\S+", "[internal source]", text)
    text = re.sub(r"https?://[^\s<>\"']+", "[source link withheld]", text)
    return re.sub(r"(?i)extraction failed", "processing needs attention", text)


def _location(locator: str) -> str:
    try:
        fragment = urlsplit(locator).fragment
    except ValueError:
        return ""
    allowed = {"page", "table", "row", "column", "paragraph"}
    return ", ".join(f"{key} {value}" for key, value in parse_qsl(fragment)
                     if key in allowed and value.isascii() and value.isdecimal())


@dataclass
class _PresentationResult:
    manifest: Manifest
    attributes: list[AttributeResult]
    evidence: list[Evidence]
    extraction_error: str | None
    private_rows: dict[str, dict]
    no_result: bool = False


def _build_package(
    results: Sequence[EnrichmentResult | _PresentationResult], *,
    source_labels: Mapping[str, str] | None = None,
    attempt_metadata: Mapping[str, dict] | None = None,
) -> ReviewerPackage:
    """Build bytes plus a separate private binding; never read/write source files.

    One row per product/attribute; candidate selection remains private. Human input
    columns remain blank unless a real ReviewDecision already exists.
    """
    rows = [list(REVIEW_COLUMNS)]
    evidence_rows = [["Product ID", "MPN", "Attribute", "Proposed value", "Unit", "Source", "Source tier", "Location", "Quote", "Source URL", "Retrieved at", "Applicability", "Evidence basis"]]
    bindings = []
    status_counts = {}
    seen = set()
    source_labels = source_labels or {}
    for result in results:
        product = result.manifest.product
        indexed = {entry.evidence_id: entry for entry in result.evidence}
        for attribute in result.attributes:
            identity = (product.item_id, attribute.attribute_id)
            if identity in seen:
                raise ValueError("Reviewer package requires one selected attempt per product and attribute")
            seen.add(identity)
            status, action = (
                ("No result available", "No result exists for this selected attempt; leave the decision pending or provide independently supported evidence.")
                if isinstance(result, _PresentationResult) and result.no_result
                else reviewer_status(attribute, result)
            )
            status_counts[status] = status_counts.get(status, 0) + 1
            proposals, units, bases, quotes, labels, urls, retrieved, applicability = [], [], [], [], [], [], [], []
            for index, candidate in enumerate(attribute.candidates):
                proposals.append(_display(candidate.value))
                units.append(_display(candidate.unit))
                bases.append(candidate.evidence_basis)
                quotes.append(_display(candidate.supporting_quote))
                for key in candidate.evidence_ids:
                    if key not in indexed:
                        raise ValueError("Reviewer candidate cites unavailable evidence")
                    evidence = indexed[key]
                    label = _display(source_labels.get(evidence.source_id)) or (
                        "Original public page" if evidence.source_tier in {"manufacturer_web", "approved_web"} else
                        "Vendor table" if evidence.source_tier == "vendor_table" else "Internal document"
                    )
                    url = _url(evidence.source_locator) if evidence.source_tier in {"manufacturer_web", "approved_web"} else ""
                    date = evidence.provider_retrieved_at.isoformat() if evidence.provider_retrieved_at else "Not recorded"
                    scope = _display("\n".join(dict.fromkeys(filter(None, [
                        evidence.qualification, candidate.qualification,
                    ])))) or "Confirm exact product applicability."
                    labels.append(label)
                    if url:
                        urls.append(url)
                    retrieved.append(date)
                    applicability.append(scope)
                    evidence_rows.append([
                        product.item_id, product.mpn, attribute.attribute_id,
                        _display(candidate.value), _display(candidate.unit), label, evidence.source_tier, _location(evidence.source_locator),
                        _display(candidate.supporting_quote), url, date, scope, candidate.evidence_basis,
                    ])
            review = attribute.review
            rows.append([
                product.item_id, product.mpn, attribute.attribute_id,
                review.decision.capitalize() if review else "", _display(review.corrected_value) if review else "",
                _display(review.corrected_unit) if review else "", _display(review.reason) if review else "",
                status, action, "\n".join(proposals) or _display(result.manifest.existing_values.get(attribute.attribute_id)),
                "\n".join(dict.fromkeys(units)), "\n".join(dict.fromkeys(bases)), "\n".join(dict.fromkeys(quotes)),
                "\n".join(dict.fromkeys(labels)), "\n".join(dict.fromkeys(urls)),
                "\n".join(dict.fromkeys(retrieved)), "\n".join(dict.fromkeys(applicability)),
            ])
            binding = {
                "Product ID": product.item_id, "MPN": product.mpn, "Attribute": attribute.attribute_id,
                "source_versions": {entry.source_id: entry.source_version for entry in result.evidence},
                "candidate_index": (
                    review.candidate_index if review and review.decision == "approve"
                    else 0 if len(attribute.candidates) == 1 else None
                ),
                "candidate_indexes": list(range(len(attribute.candidates))),
            }
            if isinstance(result, _PresentationResult):
                binding.update(result.private_rows[attribute.attribute_id])
            else:
                binding.update({
                    "machine_sha256": hashlib.sha256(result.model_dump_json().encode()).hexdigest(),
                    "hash_basis": "validated_result_model_dump_json",
                    "attempt_metadata": dict((attempt_metadata or {}).get(product.item_id, {})),
                })
            bindings.append(binding)
    summary = {
        "products": len({(result.manifest.product.item_id, result.manifest.product.mpn) for result in results}),
        "attributes": len(rows) - 1, "status_counts": status_counts,
        "qualification": "Review proposals only; inferred descriptions are not literal evidence or approved master data.",
    }
    instructions = [
        ["Topic", "Guidance"],
        ["Review columns", "Product ID and Attribute identify the row; MPN must match. Enter Decision: Approve, Correct or Reject, with a Reason."],
        ["Correction", "For Correct, enter Correction and Correction unit as defined. For Reject, leave correction fields blank."],
        ["Conflicting proposals", "Resolve conflicting evidence before approving; provide a supported Correction and Reason when appropriate."],
        ["Evidence", "Check source quotations, original public URLs, retrieval time and exact-product applicability."],
        ["Pending", "Blank Decision remains pending. Missing evidence differs from an unverified generated value."],
        ["Descriptive inference", "inferred_from_description requires human review and is not certification or literal Boolean evidence."],
        ["Privacy", "Attempt identifiers, storage paths, source hashes and technical diagnostics are retained only in a separate private binding."],
    ]
    workbook = export_workbook({
        "Review": rows, "Evidence": evidence_rows, "Instructions": instructions,
        "Summary": [["Metric", "Value"], ["Products", summary["products"]], ["Attributes", summary["attributes"]],
                    *[[status, count] for status, count in status_counts.items()]],
    })
    return ReviewerPackage(
        workbook=workbook,
        private_binding={
            "schema_version": 1, "workbook_sha256": hashlib.sha256(workbook).hexdigest(),
            "columns": REVIEW_COLUMNS, "rows": bindings,
        },
        summary=summary,
    )


def build_reviewer_package(
    results: Sequence[EnrichmentResult], *,
    source_labels: Mapping[str, str] | None = None,
    attempt_metadata: Mapping[str, dict] | None = None,
) -> ReviewerPackage:
    """Build a sanitized package for already-selected results without writing files."""
    return _build_package(results, source_labels=source_labels, attempt_metadata=attempt_metadata)


def build_snapshot_reviewer_package(snapshot: dict) -> ReviewerPackage:
    """Render every immutable scoring slot with blank reviewer inputs.

    Snapshot/attempt bindings remain entirely in ``private_binding``; no current
    batch state is read and no result attempt is invented for unavailable slots.
    """
    body = snapshot["body"]
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if snapshot["id"] != hashlib.sha256(canonical).hexdigest():
        raise ValueError("Scoring snapshot integrity check failed")
    views = []
    for slot in body["slots"]:
        product = ProductKey.model_validate(slot["product"])
        definition = AttributeDefinition.model_validate(slot["definition"])
        if definition.attribute_id != slot["attribute_id"]:
            raise ValueError("Snapshot definition does not match its attribute")
        candidates = []
        indexed = {}
        for index, supplied in enumerate(slot["candidates"]):
            if supplied["index"] != index:
                raise ValueError("Snapshot candidate indexes must be contiguous and zero-based")
            candidate = Candidate.model_validate({name: value for name, value in supplied.items() if name in Candidate.model_fields})
            if candidate.attribute_id != slot["attribute_id"]:
                raise ValueError("Snapshot candidate belongs to a different attribute")
            definition.validate_value(candidate.value, candidate.unit)
            for entry in supplied["evidence"]:
                evidence = Evidence.model_validate(entry)
                if evidence.evidence_id in indexed and indexed[evidence.evidence_id] != evidence:
                    raise ValueError("Snapshot evidence identity is inconsistent")
                indexed[evidence.evidence_id] = evidence
            if not set(candidate.evidence_ids) <= indexed.keys():
                raise ValueError("Snapshot candidate has missing evidence")
            candidates.append(candidate)
        no_result = slot["status"] == "no_result"
        if no_result and candidates:
            raise ValueError("Unavailable snapshot slots cannot carry candidates")
        attribute = AttributeResult(
            attribute_id=slot["attribute_id"], status="missing_evidence" if no_result else slot["status"],
            candidates=candidates,
        )
        existing = {slot["attribute_id"]: slot["existing_value"]} if slot["status"] == "existing" else {}
        views.append(_PresentationResult(
            manifest=Manifest(product=product, attributes=[definition], existing_values=existing),
            attributes=[attribute], evidence=list(indexed.values()), extraction_error=slot.get("extraction_error"),
            no_result=no_result,
            private_rows={slot["attribute_id"]: {
                "snapshot_id": snapshot["id"], "slot_key": slot["key"], "item_key": slot["item_key"],
                "product": slot["product"], "attribute_id": slot["attribute_id"],
                "attempt_key": slot["attempt_key"], "result_sha256": slot["result_sha256"],
                "candidate_indexes": list(range(len(candidates))),
            }},
        ))
    if not views:
        raise ValueError("Reviewer snapshot must contain at least one slot")
    package = _build_package(views)
    package.private_binding.update(snapshot_id=snapshot["id"], batch_id=body["batch_id"], owner=body["owner"])
    return package
