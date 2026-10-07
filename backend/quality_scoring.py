"""Value agreement with an explicit reference, never a claim of factual accuracy."""

from collections import Counter
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import re
import unicodedata
from typing import Any, Mapping, Sequence

from backend.answer_key import _equal
from backend.batch import read_workbook


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


def load_cowork_reference(content: bytes) -> dict[tuple[str, str], list[dict]]:
    """Read an explicit long-form draft; reject unknown layouts rather than guess."""
    sheets = read_workbook(content)
    result: dict[tuple[str, str], list[dict]] = {}
    found_table = False
    for rows in sheets.values():
        if not rows:
            continue
        headers = {_text(name): name for name in rows[0]}
        product = next((headers[name] for name in ("product id", "item id", "pimitem") if name in headers), None)
        attribute = next((headers[name] for name in ("attribute", "attribute name") if name in headers), None)
        value = next((headers[name] for name in ("value", "proposed value", "draft value") if name in headers), None)
        if not all((product, attribute, value)):
            continue
        found_table = True
        for row in rows:
            item, name = row[product].strip(), row[attribute].strip()
            if not item and not name:
                continue
            if not item or not name:
                raise ValueError("Cowork reference contains a row without its product or attribute")
            key = (item, name)
            candidates = result.setdefault(key, [])
            proposed = row[value].strip()
            if proposed and _text(proposed) not in {"not found", "not stated", "unknown", "n/a"}:
                candidate = {"value": proposed, "unit": row.get(headers.get("unit", ""), "").strip() or None}
                if candidate not in candidates:
                    candidates.append(candidate)
    if not found_table:
        raise ValueError("Cowork draft has no recognized product/attribute/value table; specify its layout before scoring")
    return result


def compare_reference(
    definitions: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]],
    reference: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]] | None,
) -> dict:
    """One comparison per product/attribute, retaining all competing proposals."""
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
        if baseline is None:
            comparison = "reference_unavailable"
        elif generated and baseline:
            kind = slots.get(key, {}).get("value_type")
            # Equal sets, not "one matching candidate", so conflicts cannot inflate agreement.
            comparison = "agree" if (
                all(any(values_agree(candidate, target, kind) for target in baseline) for candidate in generated)
                and all(any(values_agree(candidate, target, kind) for candidate in generated) for target in baseline)
            ) else "disagree"
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
        })
    return {
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
