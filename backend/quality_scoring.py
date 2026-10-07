"""Offline, scoring-only reference ingestion; never evidence or model context.

``normalize_cowork_reference`` produces a JSON-serializable scoring artifact.
Only identifiers, proposed values, and units are retained: draft citations,
confidence, notes, and validation claims cannot become extraction inputs here.
``reference_from_normalized`` restores its slot mapping at scoring time.

``compare_reference(..., comparison_schema="six_class")`` distinguishes exact
agreement from presentation equivalence and retains substantive conflicts.
The default legacy schema preserves existing callers' agree/disagree labels.
Neither schema measures factual accuracy or authorizes a human approval.
"""

from collections import Counter
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
import re
import unicodedata
from typing import Any, Literal, Mapping, Sequence

from backend.answer_key import _equal
from backend.workbooks import read_workbook_cells


COMPARISON_CLASSES = (
    "agree", "format-only difference", "differ", "DocIntel-only", "Cowork-only", "both-not-found",
)
REFERENCE_SCHEMA = "cowork_scoring_reference_v1"


def _text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _comparable(value: object, unit: object = None, value_type: str | None = None) -> tuple:
    if value_type == "boolean":
        if type(value) is bool:
            return "boolean", value, _text(unit or "")
        if _text(value) in {"yes", "true", "no", "false"}:
            return "boolean", _text(value) in {"yes", "true"}, _text(unit or "")
    if type(value) is bool:
        return "boolean", value, _text(unit or "")
    text = _text(value).replace('"', " in").replace("″", " in")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d+)?|\.\d+|\d+/\d+))\s*(inches|inch|in|mm|psi|bar)?", text)
    if match:
        suffix = match.group(2) or _text(unit or "")
        suffix = {"inches": "in", "inch": "in"}.get(suffix, suffix)
        supplied = {"inches": "in", "inch": "in"}.get(_text(unit or ""), _text(unit or ""))
        if supplied and suffix and supplied != suffix:
            return "invalid_unit_pair", text, supplied
        try:
            number = Fraction(match.group(1)) if "/" in match.group(1) else Fraction(Decimal(match.group(1)))
        except (ValueError, ZeroDivisionError, InvalidOperation):
            return "text", text, supplied
        return "number", number, suffix
    return "text", text, _text(unit or "")


def values_agree(left: Mapping, right: Mapping, value_type: str | None = None) -> bool:
    """Normalize presentation only; do not equate materials or infer synonyms."""
    return _comparable(left["value"], left.get("unit"), value_type) == _comparable(
        right["value"], right.get("unit"), value_type,
    )


def _not_found(value: object) -> bool:
    return value is None or _text(value).replace("_", " ") in {
        "", "not found", "not stated", "unknown", "n/a",
    }


def normalize_cowork_reference(content: bytes) -> dict:
    """Read bounded data-only long-form XLSX, including the approved snake_case layout.

    No filesystem discovery, network access, identifier aliasing, type coercion,
    synonym mapping, or conflict resolution is performed. Empty/not-found rows
    remain explicit slots. Duplicate values are de-duplicated only by exact
    value/unit equality. Every ``source_rows`` entry retains one-based worksheet
    coordinates, the original value/unit text (including missing markers), and
    its zero-based ``candidate_index`` within that slot, or None if not found.
    Flatten ``source_rows`` for a row-complete report; score ``candidates`` for
    slot-level comparisons without losing duplicate or conflicting source rows.
    """
    sheets = read_workbook_cells(content)
    slots: dict[tuple[str, str], dict] = {}
    layouts = []
    for sheet_index, rows in enumerate(sheets.values(), 1):
        if not rows:
            layouts.append({"sheet_index": sheet_index, "layout": "ignored", "data_rows": 0})
            continue
        headers = [(_text(cell.text).replace("_", " "), column) for column, cell in rows[0].cells.items()
                   if cell.text.strip()]

        def column_for(names):
            matches = [column for name, column in headers if name in names]
            if len(matches) > 1:
                raise ValueError("Cowork reference has ambiguous duplicate scoring columns")
            return matches[0] if matches else None

        product = column_for(("product id", "item id", "pimitem"))
        attribute = column_for(("attribute", "attribute name", "attribute id"))
        value = column_for(("value", "proposed value", "draft value"))
        unit = column_for(("unit",))
        if product is None or attribute is None or value is None:
            layouts.append({"sheet_index": sheet_index, "layout": "ignored", "data_rows": len(rows) - 1})
            continue
        layout = {
            "sheet_index": sheet_index, "layout": "long_form", "header_row": rows[0].number,
            "columns": {"product_id": product, "attribute_id": attribute, "value": value, "unit": unit},
            "column_count": len(headers), "data_rows": 0, "value_rows": 0, "not_found_rows": 0,
        }
        layouts.append(layout)
        for row in rows[1:]:
            cells = {column: cell.text for column, cell in row.cells.items()}
            if not any(text.strip() for text in cells.values()):
                continue
            item, name = cells.get(product, "").strip(), cells.get(attribute, "").strip()
            if not item or not name:
                raise ValueError("Cowork reference contains a row without its product or attribute")
            key = (item, name)
            slot = slots.setdefault(key, {
                "product_id": item, "attribute_id": name, "candidates": [], "source_rows": [],
            })
            proposed = cells.get(value, "")
            supplied_unit = cells.get(unit, "") if unit is not None else ""
            missing = _not_found(proposed)
            layout["data_rows"] += 1
            layout["not_found_rows" if missing else "value_rows"] += 1
            candidate_index = None
            if not missing:
                candidate = {"value": proposed, "unit": supplied_unit if supplied_unit.strip() else None}
                if candidate not in slot["candidates"]:
                    slot["candidates"].append(candidate)
                candidate_index = slot["candidates"].index(candidate)
            slot["source_rows"].append({
                "sheet_index": sheet_index, "row": row.number, "not_found": missing,
                "value": proposed, "unit": supplied_unit, "candidate_index": candidate_index,
            })
    recognized = [layout for layout in layouts if layout["layout"] == "long_form"]
    if not recognized:
        raise ValueError("Cowork draft has no recognized product/attribute/value table; specify its layout before scoring")
    return {
        "schema_version": REFERENCE_SCHEMA, "purpose": "scoring_only", "source_sha256": sha256(content).hexdigest(),
        "qualification": (
            "Reference values are not evidence, prompts, citations, model decisions, or human approvals. "
            "Compare only after extraction and judging. Identifiers and conflicting values are not reconciled."
        ),
        "rows": [slots[key] for key in sorted(slots)],
        "diagnostics": {
            "sheets": layouts, "recognized_sheets": len(recognized), "ignored_sheets": len(layouts) - len(recognized),
            "data_rows": sum(layout["data_rows"] for layout in recognized),
            "value_rows": sum(layout["value_rows"] for layout in recognized),
            "not_found_rows": sum(layout["not_found_rows"] for layout in recognized),
            "products": len({key[0] for key in slots}), "attributes": len({key[1] for key in slots}),
            "slots": len(slots), "populated_slots": sum(bool(slot["candidates"]) for slot in slots.values()),
            "empty_slots": sum(not slot["candidates"] for slot in slots.values()),
            "duplicate_slots": sum(len(slot["source_rows"]) > 1 for slot in slots.values()),
            "multiple_value_slots": sum(len(slot["candidates"]) > 1 for slot in slots.values()),
            "mixed_presence_slots": sum(
                bool(slot["candidates"]) and any(row["not_found"] for row in slot["source_rows"])
                for slot in slots.values()
            ),
        },
    }


def reference_from_normalized(document: Mapping[str, Any]) -> dict[tuple[str, str], list[dict]]:
    """Restore an explicit scoring-only JSON artifact; never wire it into model inputs."""
    if document.get("schema_version") != REFERENCE_SCHEMA or document.get("purpose") != "scoring_only":
        raise ValueError("Expected a scoring-only normalized Cowork reference")
    reference = {}
    for row in document["rows"]:
        key = (row["product_id"], row["attribute_id"])
        if key in reference:
            raise ValueError("Duplicate normalized reference slot")
        reference[key] = [dict(candidate) for candidate in row["candidates"]]
    return reference


def load_cowork_reference(content: bytes) -> dict[tuple[str, str], list[dict]]:
    """Backward-compatible trimmed mapping, extended to snake_case draft headers.

    Use the normalized artifact API to retain original value/unit whitespace for
    six-class format-only scoring. NOT_FOUND and the legacy missing markers are
    absent values, not model proposals.
    """
    reference = reference_from_normalized(normalize_cowork_reference(content))
    for key, candidates in reference.items():
        trimmed = []
        for candidate in candidates:
            candidate = {"value": candidate["value"].strip(), "unit": (candidate.get("unit") or "").strip() or None}
            if candidate not in trimmed:
                trimmed.append(candidate)
        reference[key] = trimmed
    return reference


def _sets_agree(left, right, equal) -> bool:
    return all(any(equal(candidate, target) for target in right) for candidate in left) and all(
        any(equal(candidate, target) for candidate in left) for target in right
    )


def _conflicted(candidates, value_type) -> bool:
    return bool(candidates) and any(
        not values_agree(candidates[0], candidate, value_type) for candidate in candidates[1:]
    )


def _exact(left: Mapping, right: Mapping) -> bool:
    return type(left["value"]) is type(right["value"]) and _equal(left, right)


def _definition_source_candidate(candidate: Mapping) -> Mapping:
    """Project validated enum labels for scoring without changing retained values."""
    grounding = candidate.get("grounding")
    metadata = grounding.get("definition_normalization") if isinstance(grounding, Mapping) else None
    if not isinstance(metadata, Mapping):
        return candidate
    kind, rule = metadata.get("expected_type"), metadata.get("rule")
    rules = {
        "Enumerated": {"unlisted_as_other_pending_definition_v1", "exact_enum_or_explicit_other_v1"},
        "Multi-Select": {"unlisted_as_other_pending_definition_v1", "exact_members_semicolon_space_v1"},
    }
    if not isinstance(kind, str) or kind not in rules or not isinstance(rule, str) or rule not in rules[kind]:
        return candidate
    recorded_rule = candidate.get("normalization_rule")
    original = metadata.get("original_proposal")
    if (
        not isinstance(recorded_rule, str) or rule not in {part.strip() for part in recorded_rule.split(";")}
        or not isinstance(original, Mapping) or original.get("attribute_id") != candidate.get("attribute_id")
        or not isinstance(original.get("value"), str) or not original["value"].strip()
    ):
        return candidate
    parts, value = metadata.get("source_bearing_texts"), candidate.get("value")
    if (
        not isinstance(value, str) or not isinstance(parts, list) or not parts
        or any(not isinstance(part, str) or not part.strip() for part in parts)
    ):
        return candidate
    if kind == "Enumerated":
        if len(parts) != 1 or value != "Other: " + parts[0]:
            return candidate
    else:
        members = value.split("; ")
        if (
            len(members) != len(parts) or any(";" in part for part in parts)
            or not any(member == "Other: " + part for member, part in zip(members, parts))
            or any(member not in (part, "Other: " + part) for member, part in zip(members, parts))
        ):
            return candidate
    return {**candidate, "value": "; ".join(parts)}


def compare_reference(
    definitions: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]],
    reference: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]] | None,
    *,
    comparison_schema: Literal["legacy", "six_class"] = "legacy",
) -> dict:
    """One comparison per slot, with no substring/synonym or unit-conversion guesses.

    ``six_class`` requires an available reference and emits only the six
    COMPARISON_CLASSES. Exact value/unit equality is ``agree``; supported case,
    whitespace, numeric, inch-unit and typed Boolean presentation changes are
    ``format-only difference``. Validated enum ``Other: `` labels may also be
    projected to their recorded source-bearing texts, only with a recognized
    definition rule, a matching candidate rule token, and an exact label/text
    correspondence. Literal labels and reference values are never stripped.
    A substantive conflict on either side is always ``differ`` when both sides
    have values, even if the same conflict is shared.
    Legacy callers retain their existing five classes and unavailable state.
    """
    if comparison_schema not in {"legacy", "six_class"}:
        raise ValueError("Unknown reference comparison schema")
    six_class = comparison_schema == "six_class"
    if six_class and reference is None:
        raise ValueError("Six-class scoring requires an available reference; unavailable is not empty")
    slots = {(row["product_id"], row["attribute_id"]): row for row in definitions}
    if len(slots) != len(definitions):
        raise ValueError("Duplicate requested product/attribute")
    grouped: dict[tuple[str, str], list[dict]] = {key: [] for key in slots}
    for candidate in proposals:
        key = (candidate["product_id"], candidate["attribute_id"])
        if key not in slots:
            raise ValueError(f"Proposal references an unrequested product/attribute: {key}")
        grouped[key].append(dict(candidate))
    rows = []
    for product, attribute in sorted(slots.keys() | (reference.keys() if reference is not None else set())):
        key = (product, attribute)
        generated = grouped.get(key, [])
        baseline = list(reference.get(key, [])) if reference is not None else None
        kind = slots.get(key, {}).get("value_type")
        if six_class:
            generated = [entry for entry in generated if not _not_found(entry["value"])]
            baseline = [entry for entry in baseline or [] if not _not_found(entry["value"])]
        compared = [_definition_source_candidate(entry) for entry in generated] if six_class and kind == "string" else generated
        docintel_conflict = _conflicted(compared, kind)
        cowork_conflict = _conflicted(baseline or [], kind)
        if baseline is None:
            comparison = "reference_unavailable"
        elif generated and baseline:
            # Equal sets, not "one matching candidate", so conflicts cannot inflate agreement.
            equivalent = _sets_agree(compared, baseline, lambda left, right: values_agree(left, right, kind))
            if six_class:
                comparison = (
                    "differ" if docintel_conflict or cowork_conflict else
                    "agree" if _sets_agree(generated, baseline, _exact) else
                    "format-only difference" if equivalent else "differ"
                )
            else:
                comparison = "agree" if equivalent else "disagree"
        elif generated:
            comparison = "DocIntel-only"
        elif baseline:
            comparison = "Cowork-only"
        else:
            comparison = "both-not-found"
        rows.append({
            "product_id": product, "attribute_id": attribute,
            "docintel": generated, "cowork": baseline, "comparison": comparison,
            "origins": sorted({str(entry.get("origin_label", entry.get("evidence_basis", "literal"))) for entry in generated}),
            "tiers": sorted({tier for entry in generated for tier in entry.get("tiers", [])}),
            "judge_accepted": any(entry.get("judge_status") == "accepted" for entry in generated),
            **({"docintel_conflict": docintel_conflict, "cowork_conflict": cowork_conflict} if six_class else {}),
        })
    return {
        **({"comparison_schema": "six_class", "comparison_classes": list(COMPARISON_CLASSES)} if six_class else {}),
        "reference_available": reference is not None,
        "qualification": "Draft value agreement is not measured accuracy or a human approval. Missing reference is not an empty reference.",
        "counts": dict(Counter(row["comparison"] for row in rows)),
        "rows": rows,
        "per_product": {
            product: {
                "requested": sum(key[0] == product for key in slots),
                "with_proposals": sum(bool(row["docintel"]) for row in rows if row["product_id"] == product),
                "judge_accepted": sum(row["judge_accepted"] for row in rows if row["product_id"] == product),
                "comparison": dict(Counter(row["comparison"] for row in rows if row["product_id"] == product)),
            }
            for product in sorted({key[0] for key in slots})
        },
    }


def compare_answer_key(proposals: Sequence[Mapping], judgments: Mapping[tuple[str, str], Mapping]) -> dict:
    """Retain the Track B exact typed equality rule and explicit human decisions."""
    by_slot: dict[tuple[str, str], list[Mapping]] = {}
    for candidate in proposals:
        by_slot.setdefault((candidate["product_id"], candidate["attribute_id"]), []).append(candidate)
    rows = []
    for key, judgment in sorted(judgments.items()):
        if judgment["decision"] == "Reject":
            rows.append({"product_id": key[0], "attribute_id": key[1], "status": "rejected_reference_no_gold_value"})
            continue
        candidates = by_slot.get(key, [])
        rows.append({
            "product_id": key[0], "attribute_id": key[1],
            "status": "no_proposal" if not candidates else (
                "agree" if all(_equal(candidate, judgment) for candidate in candidates) else "disagree"
            ),
        })
    return {"human_judgments": len(judgments), "counts": dict(Counter(row["status"] for row in rows)), "rows": rows}
