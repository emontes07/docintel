"""Offline source applicability from printed identities, never product bindings.

``build_applicability_map`` returns a source summary and evidence-level decisions.
The summary is the strongest classification in a source, NOT permission to apply
every paragraph in it. Use ``candidate_applicability`` for cited values; its
confidence is a review label, not a probability or a substitute for grounding.
Identifiers normalize case and whitespace only. Explicit file references may
lose their path/extension, but prefixes, suffixes and variant codes never alias.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import Field

from backend.models.enrichment import Candidate, Contract, Evidence, Manifest

Applicability = Literal["exact", "family-confirmed", "family-unconfirmed"]
Confidence = Literal["High", "Medium", "Low"]
_RANK: dict[Applicability, int] = {"exact": 0, "family-confirmed": 1, "family-unconfirmed": 2}


class ApplicabilityProof(Contract):
    evidence_id: str
    source_id: str
    source_version: str
    source_locator: str
    quote: str
    location: dict[str, str | int] = Field(default_factory=dict)
    role: Literal["product_identity", "family_reference", "source_identity", "header", "context"]
    identifier: str | None = None


class EvidenceApplicability(Contract):
    evidence_id: str
    status: Applicability
    reason: str
    proofs: list[ApplicabilityProof] = Field(default_factory=list)
    reviewer_question: str | None = None
    context_only: bool = False
    evidence_signature: str


class SourceApplicability(Contract):
    source_id: str
    product_mpn: str
    status: Applicability
    reason: str
    proofs: list[ApplicabilityProof] = Field(default_factory=list)
    reviewer_question: str | None = None
    evidence: dict[str, EvidenceApplicability] = Field(default_factory=dict)


ApplicabilityMap = dict[str, SourceApplicability]


class CandidateApplicability(Contract):
    status: Applicability
    confidence: Confidence
    source_ids: list[str]
    evidence_ids: list[str]
    proofs: list[ApplicabilityProof] = Field(default_factory=list)
    reviewer_questions: list[str] = Field(default_factory=list)
    reason: str


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _label(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _text(value)))


_PRODUCT_LABELS = {
    "mpn", "manufacturer part number", "manufacturer part no", "part", "part number", "part no",
    "model", "model number", "model no", "catalog number", "catalog no", "catalogue number",
    "product", "product number", "item number", "2nd item number",
}
_FAMILY_LABELS = {
    "family", "product family", "series", "style", "drawing", "drawing number", "drawing no",
    "dwg", "dwg no", "submittal", "submittal file", "submittal id", "submittal id path",
    "photo id", "photo id path",
}
_HEADER_LABELS = _PRODUCT_LABELS | _FAMILY_LABELS | {
    "material", "description", "size", "valve size", "inlet size", "outlet size", "length",
    "height", "approx wt lbs", "quantity", "qty", "no", "item", "component",
    "selected submitted item s", "selected submitted items",
}
_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)*"
_NAMED = re.compile(
    rf"\b(?:family|series|style|drawing(?:\s+(?:number|no\.?))?|dwg(?:\s+no\.?)?|"
    rf"part\s+(?:number|no\.?)|(?:ref(?:erence)?|see)\s+HS)\s*[:#=]?\s+(?P<id>{_ID})"
    rf"|(?P<before>{_ID})\s+(?:style|family|series|PART\s+NUMBER)\b",
    re.I,
)
_FAMILY_PROSE = re.compile(r"\b(?:family|series|(?:all|other)\s+(?:models|products|valves)|general)\b", re.I)
_UNCERTAIN_LINK = re.compile(
    r"\b(?:not|never|without|except|excluding|alternative|optional|may|might|could|either|or|"
    r"other|unrelated|obsolete|superseded|compatible)\b", re.I,
)


def _records(text: str) -> list[str]:
    """Split labeled fields, not punctuation within a model identifier."""
    return [
        part.strip() for part in re.split(
            r"\n|(?:[;|]\s*|\.\s+)(?=[A-Za-z][^.;|\n]*[:#=])", text,
        ) if part.strip()
    ]


def _identifier(value: str) -> str:
    value = value.strip()
    if re.search(r"\.(?:pdf|png|jpe?g|tiff?)$", urlsplit(value).path, re.I):
        value = unquote(urlsplit(value).path.replace("\\", "/").rsplit("/", 1)[-1])
        value = value.rsplit(".", 1)[0]
    return _text(value)


def _named(text: str) -> list[str]:
    result = []
    for line in _records(text):
        if _UNCERTAIN_LINK.search(line):
            continue
        explicit = re.fullmatch(
            r"(?:family|series|style|drawing(?:\s+(?:number|no\.?))?|dwg)\s*[:#=]\s*([^|;]+)",
            line.strip(), re.I,
        )
        if explicit:
            result.append(_identifier(explicit[1]))
        else:
            result.extend(
                _identifier(match["id"] or match["before"])
                for match in _NAMED.finditer(line)
                if any(char.isdigit() for char in match["id"] or match["before"])
            )
    return list(dict.fromkeys(result))


def _fields(entry: Evidence) -> dict[str, str]:
    return {
        key: values[0] for key, values in parse_qs(urlsplit(entry.source_locator).fragment).items()
        if len(values) == 1
    }


def _location(entry: Evidence) -> dict[str, str | int]:
    return {
        key: int(value) if key in {"page", "table", "row", "column", "paragraph"} and value.isdigit() else value
        for key, value in _fields(entry).items()
        if key in {"page", "table", "row", "column", "paragraph", "sheet", "role"}
    }


def _scope(entry: Evidence) -> tuple[str, str, str, str | None]:
    return (entry.source_id, entry.source_version, entry.source_locator.split("#")[0], _fields(entry).get("page"))


def _signature(entry: Evidence) -> str:
    return hashlib.sha256(entry.model_dump_json().encode()).hexdigest()


def _proof(
    entry: Evidence, role: Literal["product_identity", "family_reference", "source_identity", "header", "context"],
    *, quote: str | None = None, identifier: str | None = None, cell: str | None = None,
) -> ApplicabilityProof:
    location = _location(entry)
    if cell:
        location["cell"] = cell
    return ApplicabilityProof(
        evidence_id=entry.evidence_id, source_id=entry.source_id, source_version=entry.source_version,
        source_locator=entry.source_locator, quote=entry.text if quote is None else quote,
        location=location, role=role, identifier=identifier,
    )


def _unique(proofs: list[ApplicabilityProof]) -> list[ApplicabilityProof]:
    return list({proof.model_dump_json(): proof for proof in proofs}.values())


@dataclass(frozen=True)
class _Cell:
    entry: Evidence
    value: str
    label: str = ""
    address: str | None = None
    header: Evidence | None = None

    def proof(
        self, role: Literal["product_identity", "family_reference", "source_identity"], identifier: str,
    ) -> list[ApplicabilityProof]:
        result = [_proof(self.entry, role, quote=self.value, identifier=identifier, cell=self.address)]
        if self.header:
            result.append(_proof(self.header, "header"))
        return result


def _vendor_cells(entry: Evidence) -> list[_Cell]:
    try:
        row = json.loads(entry.text)
    except (ValueError, TypeError):
        return []
    if (not isinstance(row, dict) or not isinstance(row.get("sheet"), str)
            or not row["sheet"] or type(row.get("row")) is not int or row["row"] < 1
            or not isinstance(row.get("cells"), list) or not row["cells"]):
        return []
    cells: list[_Cell] = []
    addresses: set[str] = set()
    for cell in row["cells"]:
        if (not isinstance(cell, dict) or not all(isinstance(cell.get(k), str) for k in ("cell", "column", "value"))
                or not cell["column"].strip()):
            return []
        match = re.fullmatch(r"[A-Z]+([1-9]\d*)", cell["cell"])
        if not match or int(match[1]) != row["row"] or cell["cell"] in addresses:
            return []
        addresses.add(cell["cell"])
        cells.append(_Cell(entry, cell["value"], _label(cell["column"]), cell["cell"]))
    fields = _fields(entry)
    if "sheet" in fields or "row" in fields:
        if fields.get("sheet") != row["sheet"] or fields.get("row") != str(row["row"]):
            return []
        if "cells" in fields and set(fields["cells"].split(",")) != addresses:
            return []
    else:
        # Original VendorTableChunk locators predate the worker's query form.
        fragment = unquote(urlsplit(entry.source_locator).fragment)
        match = re.fullmatch(r"'((?:[^']|'')+)'!([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)", fragment)
        if (not match or match[1].replace("''", "'") != row["sheet"]
                or int(match[3]) != row["row"] or int(match[5]) != row["row"]):
            return []
    return cells


def _pdf_rows(evidence: list[Evidence]) -> tuple[list[list[_Cell]], set[str]]:
    tables: dict[tuple, dict[int, dict[int, Evidence]]] = defaultdict(lambda: defaultdict(dict))
    for entry in evidence:
        if entry.source_tier == "vendor_table":
            continue
        fields = _fields(entry)
        if not all(fields.get(key, "").isdigit() for key in ("page", "table", "row", "column")):
            continue
        if int(fields["page"]) < 1:
            continue
        key = (_scope(entry), fields["table"], tuple(entry.attribute_ids or ()), entry.qualification)
        cells = tables[key][int(fields["row"])]
        column = int(fields["column"])
        if column in cells:
            raise ValueError("Duplicate PDF coordinates cannot prove source applicability")
        cells[column] = entry
    rows: list[list[_Cell]] = []
    header_ids: set[str] = set()
    for table in tables.values():
        headers: dict[int, Evidence] = {}
        for _, cells in sorted(table.items()):
            labels = {_label(entry.text) for entry in cells.values()}
            if (labels & (_PRODUCT_LABELS | _FAMILY_LABELS)
                    or len(cells) > 1 and labels <= _HEADER_LABELS):
                headers = cells
                header_ids.update(entry.evidence_id for entry in cells.values())
                continue
            rows.append([
                _Cell(entry, entry.text, _label(headers[column].text) if column in headers else "",
                      header=headers.get(column))
                for column, entry in sorted(cells.items())
            ])
    return rows, header_ids


def _row_identity(cells: list[_Cell], mpn: str) -> list[_Cell]:
    keys = [cell for cell in cells if cell.label in _PRODUCT_LABELS]
    if not keys:
        keys = [cell for cell in cells if not cell.label and cell.entry.source_tier != "vendor_table"]
    # Two conflicting identity fields do not prove the whole row is this product.
    return keys if keys and all(_text(cell.value) == _text(mpn) for cell in keys) else []


def _references(cells: list[_Cell]) -> list[tuple[str, list[ApplicabilityProof]]]:
    result = []
    for cell in cells:
        if _UNCERTAIN_LINK.search(cell.value):
            continue
        identifiers = _named(cell.value)
        if cell.label in _FAMILY_LABELS:
            identifiers = [_identifier(cell.value)]
        for identifier in identifiers:
            if identifier:
                result.append((identifier, cell.proof("family_reference", identifier)))
    return result


def _source_identifiers(entry: Evidence, known_identifiers: set[str]) -> list[str]:
    if entry.source_tier == "vendor_table":
        return []
    # A citation to another document is not the identity of this page.
    if re.search(r"\b(?:see|ref|reference|refer)\b", entry.text, re.I):
        return []
    if (_fields(entry).get("role") in {"title", "sectionHeading"} and re.fullmatch(_ID, entry.text.strip())
            and (_text(entry.text) in known_identifiers or any(char.isdigit() for char in entry.text))):
        return [_identifier(entry.text)]
    return _named(entry.text)


def _page_identifiers(entry: Evidence, known_identifiers: set[str]) -> set[str]:
    fields = _fields(entry)
    if (entry.source_tier == "vendor_table" or not fields.get("page", "").isdigit()
            or int(fields["page"]) < 1 or "table" in fields or fields.get("role") == "manufacturerTitleBlock"):
        return set()
    records = _records(entry.text) or [""]
    first = records[0]
    label = re.compile(
        r"(?:product|mpn|model(?:\s+(?:number|no\.?))?|part\s+(?:number|no\.?)|catalog\s+number)\s*[:#=]\s*(.+)",
        re.I,
    )
    if label.fullmatch(first):
        return {_text(match[1]) for record in records if (match := label.fullmatch(record))}
    if (fields.get("role") in {"title", "sectionHeading"} and re.fullmatch(_ID, first)
            and (_text(first) in known_identifiers or any(char.isdigit() for char in first))):
        return {_text(first)}
    return set()


def _question(mpn: str, identifiers: list[str], references: list[str]) -> str:
    def display(values: list[str]) -> str:
        return ", ".join(value.upper() if re.fullmatch(_ID, value) else value for value in values)

    subject = f"the {display(identifiers)} drawing or family" if identifiers else "this source"
    question = f"Does {subject} apply to product {mpn}?"
    if references:
        question += f" Its exact product row refers to {display(references)}; please confirm the matching drawing or family."
    else:
        question += " Which exact product row or page confirms the connection?"
    return question


def build_applicability_map(manifest: Manifest, evidence: list[Evidence]) -> ApplicabilityMap:
    """Classify observed rows/pages, retaining the proof and unresolved questions.

    A family link requires a family/drawing reference in an exact product row
    AND that same printed identifier in the target source. A source binding,
    filename, MPN prefix, or prose mentioning the product is insufficient.
    Missing manifest sources and generated answers remain unconfirmed.
    """
    ids = [entry.evidence_id for entry in evidence]
    if len(set(ids)) != len(ids):
        raise ValueError("Evidence IDs must be unique for applicability mapping")
    usable = [entry for entry in evidence if entry.content_kind == "source_excerpt"]
    rows, header_ids = _pdf_rows(usable)
    rows.extend(cells for entry in usable if entry.source_tier == "vendor_table"
                if (cells := _vendor_cells(entry)))
    mpn = manifest.product.mpn
    exact: dict[str, list[ApplicabilityProof]] = {}
    references: dict[str, list[ApplicabilityProof]] = defaultdict(list)
    foreign_rows: set[str] = set()
    for cells in rows:
        identity = _row_identity(cells, mpn)
        if not identity:
            if any(cell.label in _PRODUCT_LABELS for cell in cells):
                foreign_rows.update(cell.entry.evidence_id for cell in cells)
            continue
        proofs = [proof for cell in identity for proof in cell.proof("product_identity", mpn)]
        proofs = _unique(proofs + [_proof(cell.header, "header") for cell in cells if cell.header])
        for cell in cells:
            exact[cell.entry.evidence_id] = proofs
        for identifier, named_proofs in _references(cells):
            references[identifier].extend(proofs + named_proofs)

    scoped: dict[tuple, list[Evidence]] = defaultdict(list)
    for entry in usable:
        scoped[_scope(entry)].append(entry)
    anchors: dict[tuple, dict[str, list[ApplicabilityProof]]] = {}
    page_exact: dict[str, list[ApplicabilityProof]] = {}
    foreign_pages: set[tuple] = set()
    known_identifiers = {_text(mpn)} | references.keys()
    table_anchors: dict[tuple, dict[str, list[ApplicabilityProof]]] = defaultdict(lambda: defaultdict(list))
    for cells in rows:
        for cell in cells:
            if (cell.entry.source_tier != "vendor_table" and cell.entry.evidence_id not in foreign_rows
                    and cell.entry.evidence_id not in exact
                    and cell.label in _FAMILY_LABELS and not _UNCERTAIN_LINK.search(cell.value)):
                identifier = _identifier(cell.value)
                table_anchors[_scope(cell.entry)][identifier].extend(cell.proof("source_identity", identifier))
    for scope, entries in scoped.items():
        named: dict[str, list[ApplicabilityProof]] = defaultdict(list, table_anchors[scope])
        for entry in entries:
            if entry.source_tier != "vendor_table" and entry.evidence_id not in foreign_rows:
                for identifier in _source_identifiers(entry, known_identifiers):
                    named[identifier].append(_proof(entry, "source_identity", identifier=identifier))
        anchors[scope] = named
        page_identifiers = {entry.evidence_id: _page_identifiers(entry, known_identifiers) for entry in entries}
        titles = [entry for entry in entries if page_identifiers[entry.evidence_id] == {_text(mpn)}]
        page_ids = {identifier for identifiers in page_identifiers.values() for identifier in identifiers}
        if page_ids - {_text(mpn)} - references.keys():
            foreign_pages.add(scope)
        # Dedicated product pages are not multiproduct catalogs or family pages.
        if (titles and page_ids == {_text(mpn)} and not set(named) - {_text(mpn)}
                and not any(entry.evidence_id in foreign_rows for entry in entries)):
            proof = [_proof(entry, "product_identity", identifier=mpn) for entry in titles]
            for entry in entries:
                fields = _fields(entry)
                if (not _FAMILY_PROSE.search(entry.text)
                        and fields.get("role") != "manufacturerTitleBlock"):
                    page_exact[entry.evidence_id] = proof

    result: ApplicabilityMap = {
        source_id: SourceApplicability(
            source_id=source_id, product_mpn=mpn, status="family-unconfirmed",
            reason="No source excerpt establishes an exact product row/page or an explicit family link.",
            reviewer_question=_question(mpn, [], sorted(references)),
        )
        for source_id in sorted(set(manifest.source_ids) | {entry.source_id for entry in evidence})
    }
    for entry in evidence:
        named = anchors.get(_scope(entry), {})
        question = _question(mpn, sorted(named), sorted(references))
        status: Applicability = "family-unconfirmed"
        reason = "No exact product identity or matching row-to-family reference proves applicability."
        proofs = [proof for group in named.values() for proof in group]
        proofs += [proof for group in references.values() for proof in group]
        if not proofs and entry.content_kind == "source_excerpt":
            proofs = [_proof(entry, "context", quote=entry.text[:280])]
        if entry.content_kind != "source_excerpt":
            reason = "Generated answers are not source identity evidence."
            proofs = []
        elif entry.evidence_id in exact or entry.evidence_id in page_exact:
            status = "exact"
            proofs = exact.get(entry.evidence_id, page_exact.get(entry.evidence_id, []))
            reason = "The printed MPN matches the exact product row or dedicated product page."
        elif (named and set(named) <= references.keys() and entry.evidence_id not in foreign_rows
              and _scope(entry) not in foreign_pages):
            status = "family-confirmed"
            proofs = [proof for identifier in named for proof in named[identifier] + references[identifier]]
            reason = "An exact product row explicitly names the same family/drawing printed in this source."
        if status != "family-unconfirmed":
            question = None
        result[entry.source_id].evidence[entry.evidence_id] = EvidenceApplicability(
            evidence_id=entry.evidence_id, status=status, reason=reason, proofs=_unique(proofs),
            reviewer_question=question, context_only=entry.evidence_id in header_ids,
            evidence_signature=_signature(entry),
        )
    for source in result.values():
        decisions = list(source.evidence.values())
        if decisions:
            strongest = min(decisions, key=lambda decision: _RANK[decision.status])
            source.status, source.reason = strongest.status, strongest.reason
            source.proofs = _unique([
                proof for decision in decisions if decision.status == strongest.status for proof in decision.proofs
            ])
            source.reviewer_question = strongest.reviewer_question
    return result


def candidate_confidence(candidate: Candidate, applicability: CandidateApplicability) -> Confidence:
    """Conservative review label; numeric/model confidence never overrides proof."""
    if (applicability.status == "family-unconfirmed" or candidate.origin in {"inferred", "model_generated"}
            or candidate.evidence_basis != "literal" or candidate.inference_rule is not None):
        return "Low"
    if candidate.origin == "derived" or applicability.status == "family-confirmed" or candidate.normalization_rule:
        return "Medium"
    return "High" if candidate.supporting_quote and candidate.supporting_quote.strip() else "Low"


def candidate_applicability(
    candidate: Candidate, applicability_map: Mapping[str, SourceApplicability], evidence: list[Evidence],
) -> CandidateApplicability:
    """Aggregate only cited evidence, taking the weakest required applicability.

    Headers co-cited with the row whose identity they prove are context, not an
    extra unsupported assertion. Missing, changed, generated or out-of-scope
    citations fail closed, even if another citation has exact applicability.
    Call after grounding; this function does not re-prove the proposed value.
    """
    by_id = {entry.evidence_id: entry for entry in evidence}
    if len(by_id) != len(evidence):
        raise ValueError("Evidence IDs must be unique for candidate applicability")
    decisions: list[EvidenceApplicability] = []
    questions: list[str] = []
    source_ids: set[str] = set()
    invalid = False
    for evidence_id in dict.fromkeys(candidate.evidence_ids):
        entry = by_id.get(evidence_id)
        source = applicability_map.get(entry.source_id) if entry else None
        decision = source.evidence.get(evidence_id) if source else None
        if entry:
            source_ids.add(entry.source_id)
        proof_available = True
        if decision and decision.status != "family-unconfirmed":
            for proof in decision.proofs:
                proof_entry = by_id.get(proof.evidence_id)
                proof_source = applicability_map.get(proof.source_id)
                proof_decision = proof_source.evidence.get(proof.evidence_id) if proof_source else None
                if (proof_entry is None or proof_decision is None
                        or proof_decision.evidence_signature != _signature(proof_entry)):
                    proof_available = False
                    break
        if (entry is None or decision is None or decision.evidence_signature != _signature(entry)
                or not proof_available
                or entry.content_kind != "source_excerpt"
                or entry.attribute_ids is not None and candidate.attribute_id not in entry.attribute_ids):
            invalid = True
            questions.append(f"Which unchanged source row or page supports {candidate.attribute_id} for this product? "
                             f"Citation {evidence_id} is missing, changed, or outside this attribute's scope.")
        else:
            decisions.append(decision)
    context = {
        proof.evidence_id for decision in decisions if not decision.context_only
        for proof in decision.proofs if proof.role == "header"
    }
    decisions = [decision for decision in decisions if not (decision.context_only and decision.evidence_id in context)]
    status: Applicability = "family-unconfirmed"
    if decisions and any(not decision.context_only for decision in decisions) and not invalid:
        status = max((decision.status for decision in decisions), key=lambda value: _RANK[value])
    questions.extend(decision.reviewer_question for decision in decisions if decision.reviewer_question)
    if status == "family-unconfirmed" and not questions:
        questions.append("Which exact product row or page supports this value?")
    result = CandidateApplicability(
        status=status, confidence="Low", source_ids=sorted(source_ids),
        evidence_ids=list(dict.fromkeys(candidate.evidence_ids)),
        proofs=_unique([proof for decision in decisions for proof in decision.proofs]),
        reviewer_questions=list(dict.fromkeys(questions)),
        reason="The least-specific cited value evidence controls applicability; source-level summaries do not upgrade it.",
    )
    result.confidence = candidate_confidence(candidate, result)
    return result
