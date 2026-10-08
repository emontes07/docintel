"""Pure structured definition shaping from approved definition metadata.

API:
    derive_definition(manifest_fields, original_row=approved_row)
    model_instruction(definition)
    compact_instruction(definition)  # model-facing; rules in DEFINITION_RULES
    normalize_proposal(definition, candidate.model_dump())
    source_bearing_texts(normalization_result)

Pass mappings, not workbook paths. No product evidence is read here. An original
row must match the attribute and, when present, the manifest's definition node.
The module never interprets examples or source_basis as constraints/evidence.

Observed retained input, gap-20261007b:
* 24 rows use Boolean, Enumerated, Multi-Select, or Numeric guidance.
* potential_attribute_example_values contains examples, not an approved enum.
* No row specifies allowed_values, unit, or component-detail permission.
* validate_batch keeps examples in original_definitions, not active value
  constraints. The worker supplies the matching original row separately;
  never manufacture a whitelist from example text.

Supported explicit additions (not claimed present in those observed rows):
allowed_values is a scalar list or JSON array; unit is exact text or an explicit
empty dimensionless cell. allow_component_detail is a bool/JSON boolean and
component_labels is a string list/JSON array. The latter declares exact prefix
or suffix labels, e.g. "Wetted", "non-wetted", "O-rings", "rubber gasket".
Component detail is never inferred from an attribute name or example.

Rules preserve scalar AttributeValue: multiselect uses "; "; Boolean is bool
internally and Yes/No for display. Enumerated members are exact approved strings
or explicit "Other: <source text>". Missing options retain source-bearing text
under Other, with review/questions, never an invented whitelist. Missing numeric
unit guidance retains the supplied numeric value/unit (including None); units
are never guessed from examples or an untyped suffix. Unknown component
permissions retain recognizable label/value or approved-value/suffix forms
unchanged for review, without claiming an approved component mapping.

issues denotes invalid/conflicting definitions; definition_questions denotes
missing guidance. ready/valid mean structural validation can proceed/succeeded,
not definition completeness, approval, or grounding. No synonyms, case-folded
enum matching, sorting, deduplication, unit conversion, or feature inference
occurs. Component labels remain grounding obligations. Review derivation rules:
unlisted_as_other_pending_definition_v1, literal_number_unit_pending_definition_v1,
and unresolved_component_text_preserved_v1. All preserve original_proposal.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

Scalar = str | int | float | bool
Kind = Literal["Boolean", "Enumerated", "Multi-Select", "Text", "Number", "Unresolved"]

_GUIDANCE: dict[str, Kind] = {
    "Boolean": "Boolean",
    "Enumerated": "Enumerated",
    "Multi-Select": "Multi-Select",
    "String": "Text",
    "Text": "Text",
    "Numeric": "Number",
    "Number": "Number",
    "Number+unit": "Number",
}
_SCALAR_KIND: dict[str, Kind] = {
    "boolean": "Boolean", "string": "Text", "number": "Number", "integer": "Number",
}
_RULES = {
    "Boolean": "boolean_literal_display_yes_no_v1",
    "Enumerated": "exact_enum_or_explicit_other_v1",
    "Multi-Select": "exact_members_semicolon_space_v1",
    "Text": "literal_text_preserved_v1",
    "Number": "literal_number_exact_unit_v1",
    "Unresolved": "definition_clarification_required_v1",
}
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_OTHER = "Other: "


@dataclass(frozen=True)
class StructuredDefinition:
    attribute_id: str
    kind: Kind
    value_type: str | None
    allowed_values: tuple[Scalar, ...]
    unit: str | None
    unit_resolved: bool
    allow_component_detail: bool
    component_labels: tuple[str, ...]
    issues: tuple[str, ...]
    provenance: dict[str, str]
    original_fields: dict[str, Any]
    original_row: dict[str, Any] | None
    component_permission_resolved: bool = False
    definition_questions: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Safe to attempt normalization, possibly with definition questions."""
        return not self.issues


@dataclass(frozen=True)
class NormalizationResult:
    valid: bool
    value: Scalar | None
    unit: str | None
    display_value: str | None
    derivation_rule: str
    errors: tuple[str, ...]
    original_proposal: dict[str, Any]
    grounding_texts: tuple[str, ...] = ()
    requires_quote_grounding: bool = True
    grounding_status: Literal["not_checked"] = "not_checked"
    requires_review: bool = False
    definition_questions: tuple[str, ...] = ()


def _same(left: Any, right: Any) -> bool:
    """Avoid Python's True == 1 and 1 == 1.0 when comparing approved fields."""
    if type(left) is not type(right):
        return False
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _array(value: Any, field: str, issues: list[str]) -> tuple[Any, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            issues.append(f"{field}: expected a list or JSON array, not delimited prose")
            return ()
    if not isinstance(value, (list, tuple)):
        issues.append(f"{field}: expected a list or JSON array")
        return ()
    return tuple(value)


def _permission(value: Any, issues: list[str]) -> bool:
    if value in ("true", "false") and isinstance(value, str):
        value = value == "true"
    if type(value) is not bool:
        issues.append("allow_component_detail: expected an explicit boolean")
        return False
    return value


def derive_definition(
    fields: Mapping[str, Any],
    *,
    original_row: Mapping[str, Any] | None = None,
) -> StructuredDefinition:
    """Merge retained explicit fields; reject conflicts and keep raw provenance.

    ``fields`` may itself be an original definition row. Empty manifest option
    lists and unresolved units can be enriched only from matching explicit row
    fields, never from potential_attribute_example_values or source_basis.
    """
    saved = copy.deepcopy(dict(fields))
    row = copy.deepcopy(dict(original_row)) if original_row is not None else None
    issues: list[str] = []
    questions: list[str] = []
    provenance: dict[str, str] = {}
    attribute_id = saved.get("attribute_id", saved.get("potential_attribute_name", ""))
    if not isinstance(attribute_id, str) or not attribute_id.strip():
        attribute_id = ""
        issues.append("attribute_id: missing exact attribute name")
    sources = [("fields", saved)]
    if row is not None:
        row_name = row.get("potential_attribute_name", row.get("attribute_id"))
        node = saved.get("definition_node", saved.get("node"))
        row_node = row.get("node", row.get("definition_node"))
        if row_name != attribute_id or (node and row_node and node != row_node):
            issues.append("original_row: attribute or definition-node mismatch")
        else:
            sources.append(("original_row", row))

    def values(*keys: str) -> list[tuple[str, Any]]:
        return [
            (f"{label}.{key}", source[key])
            for label, source in sources
            for key in keys if key in source and source[key] not in (None, "")
        ]

    guidance = values("type_guidance", "potential_attribute_data_type")
    kinds = []
    for location, value in guidance:
        kind = _GUIDANCE.get(value.strip()) if isinstance(value, str) else None
        if kind is None:
            issues.append(f"{location}: unsupported type guidance; explicit mapping required")
        else:
            kinds.append(kind)
            provenance.setdefault("kind", location)
    scalar_fields = values("value_type")
    scalar_types = [value for _, value in scalar_fields]
    for location, value in scalar_fields:
        if not isinstance(value, str) or value not in _SCALAR_KIND:
            issues.append(f"{location}: unsupported scalar value_type")
    if scalar_types and any(not _same(value, scalar_types[0]) for value in scalar_types):
        issues.append("value_type: conflicting retained and original fields")
    if kinds and any(kind != kinds[0] for kind in kinds):
        issues.append("kind: conflicting retained and original guidance")
    kind: Kind = kinds[0] if kinds else "Unresolved"
    scalar_type = scalar_types[0] if scalar_types and isinstance(scalar_types[0], str) else None
    if kind == "Unresolved" and not guidance and scalar_type in _SCALAR_KIND:
        kind = _SCALAR_KIND[scalar_type]
        provenance["kind"] = scalar_fields[0][0]
    expected_scalar = {
        "Boolean": "boolean", "Enumerated": "string", "Multi-Select": "string",
        "Text": "string", "Number": "number",
    }.get(kind)
    if scalar_type is not None and scalar_type != expected_scalar:
        if not (kind == "Number" and scalar_type == "integer"):
            issues.append("value_type: incompatible with type guidance")
    scalar_type = scalar_type or expected_scalar
    if kind == "Unresolved":
        issues.append("kind: no supported explicit type guidance")

    options: tuple[Scalar, ...] = ()
    for location, raw in values("allowed_values"):
        parsed = _array(raw, "allowed_values", issues)
        if not parsed:
            continue
        if any(
            type(value) not in (str, int, float, bool)
            or (isinstance(value, str) and (not value.strip() or value != value.strip()))
            or (type(value) is float and not math.isfinite(value))
            for value in parsed
        ):
            issues.append("allowed_values: must contain nonblank exact finite scalars")
            continue
        if options and not _same(options, parsed):
            issues.append("allowed_values: conflicting retained and original lists")
        else:
            options = parsed
            provenance["allowed_values"] = location
    if kind in {"Enumerated", "Multi-Select"}:
        if not options:
            questions.append("allowed_values: confirm approved options; examples are not a whitelist")
        if any(not isinstance(option, str) for option in options):
            issues.append("allowed_values: Enumerated/Multi-Select requires string options")
        if any(isinstance(option, str) and option.startswith("Other:") for option in options):
            issues.append("allowed_values: Other: is reserved for explicit source text")
    if kind == "Multi-Select" and any(isinstance(option, str) and ";" in option for option in options):
        issues.append("allowed_values: semicolon in a member conflicts with scalar list encoding")

    unit: str | None = None
    resolved = kind != "Number"
    explicit_units = []
    for label, source in sources:
        flag = source.get("unit_resolved", True)
        if type(flag) is not bool:
            issues.append(f"{label}.unit_resolved: expected boolean")
        if "unit" in source and flag is True:
            raw_unit = source["unit"]
            if raw_unit is not None and (
                not isinstance(raw_unit, str)
                or (raw_unit != "" and (not raw_unit.strip() or raw_unit != raw_unit.strip()))
            ):
                issues.append(f"{label}.unit: expected exact text or explicit dimensionless cell")
                continue
            explicit_units.append((f"{label}.unit", raw_unit or None))
    if explicit_units:
        unit = explicit_units[0][1]
        resolved = True
        provenance["unit"] = explicit_units[0][0]
        if any(value != unit for _, value in explicit_units):
            issues.append("unit: conflicting retained and original fields")
    elif kind == "Number" or any(source.get("unit_resolved") is False for _, source in sources):
        resolved = False
        questions.append("unit: confirm the expected unit or an explicit dimensionless definition")
    if any(raw == "Number+unit" for _, raw in guidance) and unit is None and resolved:
        issues.append("unit: Number+unit requires an explicit nonempty unit")

    permissions = values("allow_component_detail")
    allowed_detail = False
    parsed_permissions = [_permission(raw, issues) for _, raw in permissions]
    if parsed_permissions:
        allowed_detail = parsed_permissions[0]
        provenance["allow_component_detail"] = permissions[0][0]
        if any(permission != allowed_detail for permission in parsed_permissions):
            issues.append("allow_component_detail: conflicting explicit permissions")
    labels: tuple[str, ...] = ()
    for location, raw in values("component_labels"):
        parsed = _array(raw, "component_labels", issues)
        if any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            or ":" in value or ";" in value for value in parsed
        ):
            issues.append("component_labels: expected exact nonempty labels without ':' or ';'")
            continue
        if labels and labels != parsed:
            issues.append("component_labels: conflicting retained and original lists")
        elif parsed:
            labels = parsed
            provenance["component_labels"] = location
    permission_resolved = bool(permissions)
    if labels and permission_resolved and not allowed_detail:
        issues.append("component_labels: requires explicit allow_component_detail=true")
    elif labels and not permission_resolved:
        questions.append("component_detail: confirm permission before approving component mapping")
    if allowed_detail and kind not in {"Enumerated", "Multi-Select", "Text"}:
        issues.append("allow_component_detail: requires a text kind")
    if allowed_detail and not labels:
        questions.append("component_labels: supply approved labels before approving component mapping")
    return StructuredDefinition(
        attribute_id, kind, scalar_type, options, unit, resolved, allowed_detail,
        labels, tuple(issues), provenance, saved, row,
        component_permission_resolved=permission_resolved,
        definition_questions=tuple(questions),
    )


DISPLAY_RULE = "Boolean: Yes/No; other scalars unchanged; numeric unit appended with one space."
NORMALIZATION_RULE = (
    "Use literal bool internally. Enumerated: exact listed value or Other: <verbatim source text>. "
    "Multi-Select: exact listed/Other members separated by '; ', in source order; never split "
    "commas, slashes, 'and', or 'or'. Text is verbatim, including whitespace. "
    "With no approved options, retain unknown source text as Other and request definition review. "
    "Numbers with resolved unit guidance require an exact matching supplied unit; with missing "
    "guidance retain the supplied numeric value/unit, including absent unit, for clarification. "
    "No synonym guessing, unit conversion, rounding, dropped qualifiers, or inferred booleans."
)
COMPONENT_RULE = (
    "Only when explicitly allowed: '<declared label>: <member>' or "
    "'<exact listed value> <declared label>'; keep labels and material text intact. "
    "Multiple component-qualified members may use '; ' even for an Enumerated definition."
    " Missing permission/label guidance retains source-bearing component text unchanged for "
    "review, without claiming approved mapping; explicit prohibitions/violations still fail."
)
GROUNDING_RULE = (
    "Definition/options/examples are not product evidence. Every value, Other payload, "
    "number, unit, negation, alternative and component label must remain quote-grounded "
    "to applicable source evidence. Structural validation never approves grounding."
)
UNRESOLVED_RULE = (
    "definition_ready=false means invalid/conflicting structure; do not normalize. "
    "Missing guidance is not absence of evidence: retain found candidates with requires_review "
    "and definition_questions. Do not invent constraints or discard review candidates."
)
# Stated once in the shared model instructions instead of once per attribute.
DEFINITION_RULES = (
    "Structured definition rules (apply to every definition):\n"
    f"- display: {DISPLAY_RULE}\n- normalization: {NORMALIZATION_RULE}\n"
    f"- components: {COMPONENT_RULE}\n- grounding: {GROUNDING_RULE}\n- unresolved: {UNRESOLVED_RULE}\n"
)


def model_instruction(definition: StructuredDefinition) -> dict[str, Any]:
    """JSON-compatible model contract. Source fields/examples are not evidence."""
    return {
        "attribute_id": definition.attribute_id,
        "expected_type": definition.kind,
        "internal_value_type": definition.value_type,
        "allowed_values": list(definition.allowed_values),
        "unit": definition.unit,
        "unit_resolved": definition.unit_resolved,
        "allow_component_detail": definition.allow_component_detail,
        "component_permission_resolved": definition.component_permission_resolved,
        "component_labels": list(definition.component_labels),
        "definition_ready": definition.ready,
        "definition_issues": list(definition.issues),
        "definition_questions": list(definition.definition_questions),
        "derivation_rule": _RULES[definition.kind],
        "display_rule": DISPLAY_RULE,
        "normalization_rule": NORMALIZATION_RULE,
        "component_rule": COMPONENT_RULE,
        "grounding_rule": GROUNDING_RULE,
        "unresolved_rule": UNRESOLVED_RULE,
        "field_provenance": dict(definition.provenance),
    }


def compact_instruction(definition: StructuredDefinition) -> dict[str, Any]:
    """Model-facing definition: per-attribute facts only; shared rules live in DEFINITION_RULES."""
    entry: dict[str, Any] = {
        "attribute_id": definition.attribute_id,
        "expected_type": definition.kind,
        "derivation_rule": _RULES[definition.kind],
    }
    if definition.allowed_values:
        entry["allowed_values"] = list(definition.allowed_values)
    if definition.unit is not None:
        entry["unit"] = definition.unit
    if not definition.unit_resolved:
        entry["unit_resolved"] = False
    if definition.allow_component_detail or definition.component_permission_resolved:
        entry["allow_component_detail"] = definition.allow_component_detail
    if definition.component_labels:
        entry["component_labels"] = list(definition.component_labels)
    if not definition.ready:
        entry["definition_ready"] = False
    if definition.issues:
        entry["definition_issues"] = list(definition.issues)
    if definition.definition_questions:
        entry["definition_questions"] = list(definition.definition_questions)
    return entry


def _other(text: str) -> str | None:
    if text.startswith(_OTHER) and text[len(_OTHER):].strip():
        return text[len(_OTHER):]
    return None


def _member(
    definition: StructuredDefinition, text: str,
) -> tuple[str, str, bool] | None:
    """Return retained text, source-bearing text, component-qualified flag."""
    if any(_same(text, option) for option in definition.allowed_values):
        return text, text, False
    payload = _other(text)
    if payload is not None:
        return text, payload, False
    if not definition.allow_component_detail or ";" in text:
        return None
    for label in definition.component_labels:
        prefix = label + ": "
        if text.startswith(prefix):
            value = text[len(prefix):]
            payload = _other(value)
            if payload is not None or any(_same(value, option) for option in definition.allowed_values):
                return text, prefix + (payload if payload is not None else value), True
        suffix = " " + label
        if text.endswith(suffix):
            value = text[:-len(suffix)]
            if any(_same(value, option) for option in definition.allowed_values):
                return text, text, True
    return None


def _component_candidate(
    definition: StructuredDefinition, text: str,
) -> tuple[str, bool] | None:
    """Recognize syntax, not meaning: retain labels without approving a mapping.

    The bool only checks explicit option/label constraints. An unknown label is
    not guessed to mean wetted/non-wetted or any component role.
    """
    if ":" in text:
        label, _, body = text.partition(":")
        if label.casefold().strip() == "other":
            return None
        body_text = body.lstrip()
        payload = _other(body_text)
        valid = bool(label.strip() and body_text.strip())
        if definition.component_labels:
            valid = valid and label in definition.component_labels
        if body_text.startswith("Other:") and payload is None:
            valid = False
        if definition.allowed_values:
            valid = valid and (
                payload is not None
                or any(_same(body_text, option) for option in definition.allowed_values)
            )
        ground = text if payload is None else label + ":" + body[:len(body) - len(body_text)] + payload
        return ground, valid
    for label in definition.component_labels:
        suffix = " " + label
        if text.endswith(suffix):
            value = text[:-len(suffix)]
            valid = bool(value) and (
                not definition.allowed_values
                or any(_same(value, option) for option in definition.allowed_values)
            )
            return text, valid
    for option in definition.allowed_values:
        if isinstance(option, str) and text.startswith(option + " "):
            suffix = text[len(option) + 1:]
            if re.search(r"\b(?:and|or|not|no|without)\b", suffix, re.IGNORECASE):
                return None
            return text, bool(suffix) and (
                not definition.component_labels or suffix in definition.component_labels
            )
    return None


def _number(
    definition: StructuredDefinition, value: Any, proposed_unit: Any,
) -> tuple[Scalar, str | None, str] | None:
    unit = proposed_unit
    if unit is not None and (
        not isinstance(unit, str) or not unit.strip() or unit != unit.strip()
    ):
        return None
    if isinstance(value, str):
        token = value.strip()
        suffix_unit = definition.unit if definition.unit_resolved else unit
        if suffix_unit is not None and token.endswith(" " + suffix_unit):
            if unit not in (None, suffix_unit):
                return None
            token = token[:-(len(suffix_unit) + 1)]
            unit = suffix_unit
        if not _NUMBER.fullmatch(token):
            return None
        try:
            decimal = Decimal(token)
            if definition.value_type == "integer":
                if decimal != decimal.to_integral_value():
                    return None
                value = int(decimal)
            else:
                value = int(token) if re.fullmatch(r"[+-]?[0-9]+", token) else float(token)
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if Decimal(str(value)) != decimal:
                return None
        except (ValueError, OverflowError, InvalidOperation):
            return None
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        return None
    if definition.value_type == "integer" and type(value) is not int:
        return None
    if definition.unit_resolved and unit != definition.unit:
        return None
    return value, unit, str(value) + (f" {unit}" if unit is not None else "")


def normalize_proposal(
    definition: StructuredDefinition, proposal: Mapping[str, Any],
) -> NormalizationResult:
    """Validate and normalize a candidate-shaped mapping without mutating it.

    All quote/evidence/origin/qualification/machine fields survive deep-copied in
    original_proposal, including on rejection. Only value/unit/display are
    derived. Invalid results have no normalized value, never a best-effort guess.
    Missing definition guidance is not invalid content: preserve the candidate
    with requires_review and definition_questions. Units must be supplied in the
    proposal or matched literally to a resolved unit; untyped suffixes are not
    guessed to be units. valid never means approved, grounded, or review-free.
    A missing quote is not invented: the caller must still perform its existing
    quote-to-source and value-to-quote grounding checks.
    """
    original = copy.deepcopy(dict(proposal))
    rule = _RULES[definition.kind]
    questions = definition.definition_questions

    def failure(message: str) -> NormalizationResult:
        return NormalizationResult(
            False, None, None, None, rule, (message,), original,
            requires_review=bool(questions), definition_questions=questions,
        )

    def success(
        value: Scalar,
        unit: str | None,
        display: str,
        grounding: tuple[str, ...],
        *,
        review: bool = False,
        extra_questions: tuple[str, ...] = (),
        derivation_rule: str = rule,
    ) -> NormalizationResult:
        combined = tuple(dict.fromkeys((*questions, *extra_questions)))
        return NormalizationResult(
            True, value, unit, display, derivation_rule, (), original, grounding,
            requires_review=review or bool(combined), definition_questions=combined,
        )

    if not definition.ready:
        return NormalizationResult(
            False, None, None, None, rule, definition.issues, original,
            requires_review=bool(questions), definition_questions=questions,
        )
    if "value" not in proposal:
        return failure("value: missing")
    if "attribute_id" in proposal and proposal["attribute_id"] != definition.attribute_id:
        return failure("attribute_id: does not match definition")
    value, unit = proposal["value"], proposal.get("unit")
    if definition.kind == "Number":
        parsed = _number(definition, value, unit)
        if parsed is None:
            return failure(
                "value: requires a finite literal number; no ranges, qualifiers, guessed units, "
                "or violation of explicit unit guidance"
            )
        normalized, unit, display = parsed
        if definition.allowed_values and not any(_same(normalized, option) for option in definition.allowed_values):
            return failure("value: number is not an exact allowed scalar")
        source_text = value.strip() if isinstance(value, str) else str(value)
        if unit is not None and not source_text.endswith(" " + unit):
            source_text += " " + unit
        return success(
            normalized, unit, display, (source_text,),
            derivation_rule=rule if definition.unit_resolved else "literal_number_unit_pending_definition_v1",
        )
    if unit != definition.unit:
        return failure("unit: does not match the explicit definition")
    if definition.kind == "Boolean":
        if type(value) is bool:
            normalized = value
        elif isinstance(value, str) and value.strip().casefold() in {"true", "false", "yes", "no"}:
            normalized = value.strip().casefold() in {"true", "yes"}
        else:
            return failure("value: Boolean requires literal bool or Yes/No/true/false, not feature inference")
        if definition.allowed_values and not any(_same(normalized, option) for option in definition.allowed_values):
            return failure("value: Boolean is not an exact allowed scalar")
        return success(
            normalized, unit, "Yes" if normalized else "No",
            (value if isinstance(value, str) else str(value).lower(),),
        )
    if definition.kind == "Text":
        if not isinstance(value, str) or not value.strip():
            return failure("value: Text requires a nonblank string")
        if definition.allowed_values and not any(_same(value, option) for option in definition.allowed_values):
            return failure("value: Text is not an exact allowed scalar")
        return success(value, unit, value, (value,))
    if definition.kind not in {"Enumerated", "Multi-Select"}:
        return failure("value: unsupported definition kind")
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if definition.kind == "Enumerated":
            exact = _member(definition, text)
            if exact is not None:
                return success(
                    exact[0], unit, exact[0], (exact[1],), review=exact[0] != exact[1],
                )
        parts = [part.strip() for part in text.split(";")]
    elif definition.kind == "Multi-Select" and isinstance(value, (list, tuple)) and value:
        if any(not isinstance(part, str) or not part.strip() or ";" in part for part in value):
            return failure("value: Multi-Select list must contain nonempty, unambiguous string members")
        parts = [part.strip() for part in value]
    else:
        return failure("value: requires nonblank scalar text or a Multi-Select string list")
    if any(not part or (part.casefold().startswith("other:") and _other(part) is None) for part in parts):
        return failure("value: empty member or malformed Other: source text")
    members = [_member(definition, part) for part in parts]
    components = [
        _component_candidate(definition, part) if member is None else None
        for part, member in zip(parts, members)
    ]
    if any(component is not None for component in components):
        if definition.component_permission_resolved and not definition.allow_component_detail:
            return failure("value: component formatting violates explicit allow_component_detail=false")
        if definition.component_permission_resolved and definition.component_labels and definition.allowed_values:
            return failure("value: component member violates the explicit option/label constraints")
        grounded = []
        for part, member, component in zip(parts, members, components):
            if member is not None:
                grounded.append(member[1])
            elif component is not None:
                if not component[1]:
                    return failure("value: invalid component structure or explicit option/label violation")
                grounded.append(component[0])
            elif not definition.allowed_values:
                grounded.append(part)
            else:
                return failure("value: unlisted member in unresolved component candidate")
        retained = value if isinstance(value, str) else "; ".join(parts)
        component_questions = ()
        if not definition.component_permission_resolved or not definition.component_labels:
            component_questions = ("component_detail: confirm permission and labels; mapping is not approved",)
        return success(
            retained, unit, retained, tuple(grounded), review=True,
            extra_questions=component_questions,
            derivation_rule="unresolved_component_text_preserved_v1",
        )
    if not definition.allowed_values:
        if definition.kind == "Enumerated":
            retained = _OTHER + text
            return success(
                retained, unit, retained, (text,), review=True,
                derivation_rule="unlisted_as_other_pending_definition_v1",
            )
        members = [
            member if member is not None else (_OTHER + part, part, False)
            for part, member in zip(parts, members)
        ]
    if any(member is None for member in members):
        return failure("value: unlisted member; use exact allowed value or explicit Other: source text")
    accepted = [member for member in members if member is not None]
    if definition.kind == "Enumerated" and (
        not definition.allow_component_detail or not all(member[2] for member in accepted)
    ):
        return failure("value: Enumerated accepts one member unless every member is explicitly component-qualified")
    normalized = "; ".join(member[0] for member in accepted)
    return success(
        normalized, unit, normalized, tuple(member[1] for member in accepted),
        review=any(member[0] != member[1] for member in accepted),
        derivation_rule=rule if definition.allowed_values else "unlisted_as_other_pending_definition_v1",
    )


def source_bearing_texts(result: NormalizationResult) -> tuple[str, ...]:
    """Return ALL grounding obligations, not evidence of their satisfaction.

    Removes only a recognized enum wrapper "Other: "; never strips qualifiers,
    component labels, negation, alternatives, numbers or supplied units. Retained
    review candidates also return ALL obligations; requires_review is not a
    grounding exemption. Unknown component labels remain in the text, not an
    approved mapping. An absent numeric unit remains absent. Text definitions
    keep literal "Other:" text. Every returned member must still be grounded
    against the original supporting_quote AND applicable retrieved source;
    membership in an approved enum is not grounding. Boolean callers retain
    their existing literal-Boolean grounding rules, not free feature inference.
    """
    if not result.valid:
        raise ValueError("Rejected proposals cannot supply normalized grounding text")
    return result.grounding_texts
