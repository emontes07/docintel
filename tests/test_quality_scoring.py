from copy import deepcopy
import json
from typing import Any, cast

import pytest

from backend.batch import export_workbook
from backend.quality_scoring import (
    COMPARISON_CLASSES, compare_answer_key, compare_reference, load_cowork_reference,
    normalize_cowork_reference, reference_from_normalized, values_agree,
)


DEFINITIONS = [{"product_id": "P1", "attribute_id": "Size", "value_type": "string"}]


def candidate(value, **kwargs):
    return {"product_id": "P1", "attribute_id": "Size", "value": value, "unit": None, **kwargs}


@pytest.mark.parametrize(("left", "right"), [
    ({"value": '3/4"'}, {"value": 0.75, "unit": "in"}),
    ({"value": " LockWing "}, {"value": "lockwing"}),
    ({"value": "3/4 in"}, {"value": "3/4", "unit": "inches"}),
])
def test_presentation_equivalence(left, right):
    assert values_agree(left, right)


def test_no_material_or_boolean_guess():
    assert not values_agree({"value": "brass"}, {"value": "low lead brass"})
    assert not values_agree({"value": True}, {"value": 1})
    assert values_agree({"value": True}, {"value": "Yes"}, "boolean")
    assert not values_agree({"value": '3/4 in', "unit": "mm"}, {"value": '3/4 in'})


@pytest.mark.parametrize(("values", "baseline", "expected"), [
    (["3/4 in"], [{"value": "3/4", "unit": "in"}], "agree"),
    (["1 in"], [{"value": "3/4", "unit": "in"}], "disagree"),
    (["1 in"], [], "DocIntel-only"),
    ([], [{"value": "3/4 in"}], "Cowork-only"),
    ([], [], "both-not-found"),
])
def test_five_requested_comparison_categories(values, baseline, expected):
    result = compare_reference(DEFINITIONS, [candidate(value) for value in values], {("P1", "Size"): baseline})
    assert result["rows"][0]["comparison"] == expected


def test_missing_reference_is_not_empty_and_does_not_claim_agreement():
    result = compare_reference(DEFINITIONS, [candidate("3/4 in", judge_status="accepted")], None)
    assert result["counts"] == {"reference_unavailable": 1}
    assert result["per_product"]["P1"]["judge_accepted"] == 1


def test_conflict_with_one_match_is_disagreement():
    result = compare_reference(
        DEFINITIONS, [candidate("3/4 in"), candidate("1 in")],
        {("P1", "Size"): [{"value": "3/4 in"}]},
    )
    assert result["counts"] == {"disagree": 1}


def test_reference_only_attribute_is_retained():
    result = compare_reference(DEFINITIONS, [], {("P1", "Material"): [{"value": "Brass"}]})
    assert result["counts"] == {"Cowork-only": 1, "both-not-found": 1}


def test_human_scoring_preserves_track_b_typed_equality():
    result = compare_answer_key([candidate(True)], {("P1", "Size"): {"decision": "Approve", "value": 1, "unit": None}})
    assert result["counts"] == {"disagree": 1}
    assert compare_answer_key([], {})["human_judgments"] == 0


def test_load_explicit_reference_and_reject_unrecognized_layout():
    data = export_workbook({"Draft": [["Product ID", "Attribute", "Value", "Unit"], ["P1", "Size", "3/4", "in"]]})
    assert load_cowork_reference(data) == {("P1", "Size"): [{"value": "3/4", "unit": "in"}]}
    with pytest.raises(ValueError, match="no recognized"):
        load_cowork_reference(export_workbook({"Draft": [["Unknown"], ["Not a scored table"]]}))


DRAFT_HEADERS = [
    "item_id", "vendor", "attribute_name", "expected_data_type", "proposed_value", "unit",
    "source_tier", "source_locator", "system_confidence", "notes", "human_validation_status",
]


def draft_row(product="P1", attribute="Size", value="3/4", unit="in"):
    return [
        product, "Synthetic vendor", attribute, "Enumerated", value, unit,
        "DO_NOT_IMPORT_TIER", "DO_NOT_IMPORT_CITATION", "DO_NOT_IMPORT_CONFIDENCE",
        "DO_NOT_IMPORT_NOTES", "DO_NOT_IMPORT_APPROVAL",
    ]


def test_actual_layout_synthetic_ingestion_keeps_conflicts_and_not_found_slots():
    content = export_workbook({"Draft": [
        DRAFT_HEADERS, draft_row(value="3/4"), draft_row(value="1"), draft_row(value="1"),
        draft_row(attribute="Material", value="NOT_FOUND", unit=""),
        draft_row(product="P2", value=" not_found ", unit=""),
        draft_row(product="P2", value=" 1 ", unit=" in "),
    ], "Instructions": [["Read me"], ["Not an attribute table"]]})
    artifact = normalize_cowork_reference(content)
    reference = reference_from_normalized(json.loads(json.dumps(artifact)))
    assert artifact["purpose"] == "scoring_only"
    assert len(artifact["source_sha256"]) == 64
    assert reference == {
        ("P1", "Size"): [{"value": "3/4", "unit": "in"}, {"value": "1", "unit": "in"}],
        ("P1", "Material"): [],
        ("P2", "Size"): [{"value": " 1 ", "unit": " in "}],
    }
    assert "DO_NOT_IMPORT" not in json.dumps(artifact)
    assert artifact["diagnostics"] == {
        "sheets": [
            {"sheet_index": 1, "layout": "long_form", "header_row": 1,
             "columns": {"product_id": 1, "attribute_id": 3, "value": 5, "unit": 6},
             "column_count": 11, "data_rows": 6, "value_rows": 4, "not_found_rows": 2},
            {"sheet_index": 2, "layout": "ignored", "data_rows": 1},
        ],
        "recognized_sheets": 1, "ignored_sheets": 1, "data_rows": 6, "value_rows": 4, "not_found_rows": 2,
        "products": 2, "attributes": 2, "slots": 3, "populated_slots": 2, "empty_slots": 1,
        "duplicate_slots": 2, "multiple_value_slots": 1, "mixed_presence_slots": 1,
    }
    size = next(row for row in artifact["rows"] if (row["product_id"], row["attribute_id"]) == ("P1", "Size"))
    assert [row["row"] for row in size["source_rows"]] == [2, 3, 4]
    assert load_cowork_reference(content)[("P2", "Size")] == [{"value": "1", "unit": "in"}]


def test_headers_only_and_empty_data_rows_are_supported():
    assert load_cowork_reference(export_workbook({"Draft": [DRAFT_HEADERS]})) == {}
    data = export_workbook({"Draft": [DRAFT_HEADERS, [""] * 11, draft_row(value="0")]})
    assert normalize_cowork_reference(data)["diagnostics"]["data_rows"] == 1
    assert load_cowork_reference(data)[("P1", "Size")][0]["value"] == "0"


def test_source_rows_preserve_value_associations_across_sheets_and_json_roundtrip():
    content = export_workbook({
        "First": [
            DRAFT_HEADERS, draft_row(value=" 1 ", unit=" in "), [""] * len(DRAFT_HEADERS),
            draft_row(value="2", unit="in"), draft_row(value="2", unit="in"),
        ],
        "Second": [
            DRAFT_HEADERS, draft_row(value=" NOT_FOUND ", unit=""),
            draft_row(value=" 1 ", unit=" in "), draft_row(value="", unit=" "),
        ],
    })
    artifact = json.loads(json.dumps(normalize_cowork_reference(content)))
    assert artifact["diagnostics"]["slots"] == 1
    assert artifact["diagnostics"]["data_rows"] == 6
    slot = artifact["rows"][0]
    assert slot["source_rows"] == [
        {"sheet_index": 1, "row": 2, "not_found": False, "value": " 1 ", "unit": " in ", "candidate_index": 0},
        {"sheet_index": 1, "row": 4, "not_found": False, "value": "2", "unit": "in", "candidate_index": 1},
        {"sheet_index": 1, "row": 5, "not_found": False, "value": "2", "unit": "in", "candidate_index": 1},
        {"sheet_index": 2, "row": 2, "not_found": True, "value": " NOT_FOUND ", "unit": "", "candidate_index": None},
        {"sheet_index": 2, "row": 3, "not_found": False, "value": " 1 ", "unit": " in ", "candidate_index": 0},
        {"sheet_index": 2, "row": 4, "not_found": True, "value": "", "unit": " ", "candidate_index": None},
    ]
    for source in slot["source_rows"]:
        if source["candidate_index"] is not None:
            assert slot["candidates"][source["candidate_index"]] == {
                "value": source["value"], "unit": source["unit"],
            }
    assert reference_from_normalized(artifact) == {
        ("P1", "Size"): [{"value": " 1 ", "unit": " in "}, {"value": "2", "unit": "in"}],
    }
    flattened = [
        {"product_id": slot["product_id"], "attribute_id": slot["attribute_id"], **source}
        for slot in artifact["rows"] for source in slot["source_rows"]
    ]
    assert len(flattened) == 6
    assert len({(row["sheet_index"], row["row"]) for row in flattened}) == 6


@pytest.mark.parametrize("value", ["", "Not Found", "NOT_FOUND", "not stated", "unknown", "N/A"])
def test_explicit_missing_tokens(value):
    data = export_workbook({"Draft": [DRAFT_HEADERS, draft_row(value=value)]})
    assert load_cowork_reference(data) == {("P1", "Size"): []}


@pytest.mark.parametrize("value", ["0", "False", "No", "Not Foundry brass", "not applicable", "none"])
def test_missing_detection_never_uses_substrings_or_boolean_truthiness(value):
    data = export_workbook({"Draft": [DRAFT_HEADERS, draft_row(value=value)]})
    assert load_cowork_reference(data)[("P1", "Size")][0]["value"] == value


@pytest.mark.parametrize(("product", "attribute"), [("", "Size"), ("P1", ""), ("", "")])
def test_unidentified_value_rows_fail_closed(product, attribute):
    data = export_workbook({"Draft": [DRAFT_HEADERS, draft_row(product=product, attribute=attribute)]})
    with pytest.raises(ValueError, match="without its product or attribute"):
        normalize_cowork_reference(data)


def test_ambiguous_headers_are_rejected_instead_of_picking_a_value():
    data = export_workbook({"Draft": [
        ["item_id", "attribute_name", "proposed_value", "Value"], ["P1", "Size", "1", "2"],
    ]})
    with pytest.raises(ValueError, match="ambiguous"):
        normalize_cowork_reference(data)


def test_reference_identifiers_are_not_aliased_or_casefolded():
    data = export_workbook({"Draft": [
        DRAFT_HEADERS, draft_row(product="PIMITEM-1"), draft_row(product="1"),
        draft_row(product="pimitem-1"),
    ]})
    assert len(load_cowork_reference(data)) == 3


def test_normalized_artifact_requires_scoring_purpose_and_unique_slots():
    artifact = normalize_cowork_reference(export_workbook({"Draft": [DRAFT_HEADERS, draft_row()]}))
    for overrides in ({"purpose": "evidence"}, {"schema_version": "unknown"}):
        with pytest.raises(ValueError, match="scoring-only"):
            reference_from_normalized({**artifact, **overrides})
    with pytest.raises(ValueError, match="Duplicate"):
        reference_from_normalized({**artifact, "rows": artifact["rows"] * 2})


@pytest.mark.parametrize(("generated", "reference", "expected"), [
    ([{"value": "Brass"}], [{"value": "Brass"}], "agree"),
    ([{"value": " BRASS "}], [{"value": "Brass"}], "format-only difference"),
    ([{"value": '3/4"'}], [{"value": 0.75, "unit": "in"}], "format-only difference"),
    ([{"value": "brass"}], [{"value": "low lead brass"}], "differ"),
    ([{"value": "Brass"}], [], "DocIntel-only"),
    ([], [{"value": "Brass"}], "Cowork-only"),
    ([], [], "both-not-found"),
])
def test_exact_six_class_schema(generated, reference, expected):
    result = compare_reference(
        DEFINITIONS, [candidate(**entry) for entry in generated], {("P1", "Size"): reference},
        comparison_schema="six_class",
    )
    assert result["counts"] == {expected: 1}
    assert set(result["counts"]) <= set(COMPARISON_CLASSES)
    assert len(COMPARISON_CLASSES) == 6


@pytest.mark.parametrize(("left", "right", "kind", "expected"), [
    ({"value": True}, {"value": "Yes"}, "boolean", "format-only difference"),
    ({"value": True}, {"value": 1}, "boolean", "differ"),
    ({"value": True}, {"value": "Yes"}, "string", "differ"),
    ({"value": "FIP"}, {"value": "Female Iron Pipe"}, "string", "differ"),
    ({"value": "low-lead brass"}, {"value": "no-lead brass"}, "string", "differ"),
    ({"value": "Copper; Iron"}, {"value": "Iron; Copper"}, "string", "differ"),
    ({"value": "3/4 in", "unit": "mm"}, {"value": "3/4 in"}, "string", "differ"),
    ({"value": "1 in"}, {"value": "25.4 mm"}, "string", "differ"),
    ({"value": 1}, {"value": 1.0}, "number", "format-only difference"),
    ({"value": "No"}, {"value": "NOT_FOUND"}, "boolean", "DocIntel-only"),
    ({"value": None}, {"value": "NOT_FOUND"}, "string", "both-not-found"),
])
def test_six_classes_do_not_guess_synonyms_or_resolve_units(left, right, kind, expected):
    result = compare_reference(
        [{**DEFINITIONS[0], "value_type": kind}], [candidate(**left)], {("P1", "Size"): [right]},
        comparison_schema="six_class",
    )
    assert result["counts"] == {expected: 1}


def test_six_class_format_detection_retains_reference_whitespace():
    reference = reference_from_normalized(normalize_cowork_reference(
        export_workbook({"Draft": [DRAFT_HEADERS, draft_row(value=" 3/4 ", unit=" in ")]}),
    ))
    result = compare_reference(
        DEFINITIONS, [candidate("3/4", unit="in")], reference, comparison_schema="six_class",
    )
    assert result["counts"] == {"format-only difference": 1}


@pytest.mark.parametrize("both_conflicted", [False, True])
def test_six_class_conflicts_are_visible_even_when_both_sides_share_them(both_conflicted):
    baseline = [{"value": "brass"}, {"value": "bronze"}]
    proposals = [candidate("brass"), candidate("bronze")] if both_conflicted else [candidate("brass")]
    result = compare_reference(DEFINITIONS, proposals, {("P1", "Size"): baseline}, comparison_schema="six_class")
    assert result["counts"] == {"differ": 1}
    assert result["rows"][0]["cowork"] == baseline
    assert result["rows"][0]["cowork_conflict"] is True
    assert result["rows"][0]["docintel_conflict"] is both_conflicted


def test_exact_duplicate_proposals_do_not_create_a_conflict():
    result = compare_reference(
        DEFINITIONS, [candidate("brass"), candidate("brass")], {("P1", "Size"): [{"value": "brass"}]},
        comparison_schema="six_class",
    )
    assert result["counts"] == {"agree": 1}
    assert result["rows"][0]["docintel_conflict"] is False
    assert len(result["rows"][0]["docintel"]) == 2


def test_six_class_reference_only_slots_and_unavailable_reference():
    result = compare_reference(
        DEFINITIONS, [], {("P1", "Material"): [{"value": "Brass"}]}, comparison_schema="six_class",
    )
    assert result["counts"] == {"Cowork-only": 1, "both-not-found": 1}
    with pytest.raises(ValueError, match="available reference"):
        compare_reference(DEFINITIONS, [], None, comparison_schema="six_class")
    with pytest.raises(ValueError, match="Unknown"):
        compare_reference(DEFINITIONS, [], {}, comparison_schema=cast(Any, "typo"))


def other_label_candidate(parts, *, kind="Enumerated", rule="unlisted_as_other_pending_definition_v1"):
    return candidate(
        "; ".join("Other: " + part for part in parts), origin="derived", normalization_rule=rule,
        grounding={"definition_normalization": {
            "expected_type": kind, "rule": rule, "source_bearing_texts": parts,
            "questions": ["Confirm approved options."],
            "original_proposal": {"attribute_id": "Size", "value": "; ".join(parts), "unit": None},
        }},
    )


def six_class_against(proposal, value, **reference_updates):
    return compare_reference(
        DEFINITIONS, [proposal], {("P1", "Size"): [{"value": value, **reference_updates}]},
        comparison_schema="six_class",
    )


@pytest.mark.parametrize(("kind", "parts", "rule"), [
    ("Enumerated", ["Brass"], "unlisted_as_other_pending_definition_v1"),
    ("Enumerated", ["Copper; Iron"], "unlisted_as_other_pending_definition_v1"),
    ("Enumerated", ["Brass"], "exact_enum_or_explicit_other_v1"),
    ("Multi-Select", ["Copper", "Iron"], "unlisted_as_other_pending_definition_v1"),
    ("Multi-Select", ["Copper", "Iron"], "exact_members_semicolon_space_v1"),
])
def test_validated_other_labels_are_format_only(kind, parts, rule):
    proposal = other_label_candidate(parts, kind=kind, rule=rule)
    proposal["normalization_rule"] = "earlier_grounding_rule_v1; " + rule
    original = deepcopy(proposal)
    result = six_class_against(proposal, "; ".join(parts))
    assert result["counts"] == {"format-only difference": 1}
    assert result["rows"][0]["docintel"] == [original]
    assert proposal == original


@pytest.mark.parametrize(("field", "invalid"), [
    ("expected_type", "Text"),
    ("expected_type", "Boolean"),
    ("rule", "invented_other_stripping_rule"),
    ("rule", "exact_members_semicolon_space_v1"),
    ("source_bearing_texts", ["Bronze"]),
    ("source_bearing_texts", ["Brass", "Iron"]),
    ("source_bearing_texts", []),
    ("source_bearing_texts", [""]),
    ("source_bearing_texts", "Brass"),
    ("source_bearing_texts", [True]),
    ("original_proposal", None),
])
def test_inconsistent_definition_metadata_cannot_authorize_label_removal(field, invalid):
    proposal = other_label_candidate(["Brass"])
    proposal["grounding"]["definition_normalization"][field] = invalid
    assert six_class_against(proposal, "Brass")["counts"] == {"differ": 1}


@pytest.mark.parametrize("rule", [None, "", "another_rule", "prefix_unlisted_as_other_pending_definition_v1_suffix"])
def test_definition_rule_must_be_recorded_as_a_complete_normalization_rule(rule):
    proposal = other_label_candidate(["Brass"])
    proposal["normalization_rule"] = rule
    assert six_class_against(proposal, "Brass")["counts"] == {"differ": 1}


@pytest.mark.parametrize("grounding", [None, {}, {"definition_normalization": None}, "untrusted text"])
def test_literal_other_is_not_stripped_without_normalization_provenance(grounding):
    proposal = candidate("Other: Brass", grounding=grounding)
    assert six_class_against(proposal, "Brass")["counts"] == {"differ": 1}
    assert six_class_against(proposal, "Other: Brass")["counts"] == {"agree": 1}


@pytest.mark.parametrize(("parts", "reference"), [
    (["low lead brass"], "brass"),
    (["FIP"], "Female Iron Pipe"),
    (["Copper", "Iron"], "Iron; Copper"),
    (["Copper", "Copper"], "Copper"),
    (["Other: Brass"], "Brass"),
])
def test_presentation_metadata_does_not_infer_synonyms_sort_deduplicate_or_strip_twice(parts, reference):
    proposal = other_label_candidate(parts, kind="Multi-Select" if len(parts) > 1 else "Enumerated")
    assert six_class_against(proposal, reference)["counts"] == {"differ": 1}


def test_other_labels_preserve_units_and_do_not_normalize_reference_metadata():
    proposal = other_label_candidate(["1"])
    proposal["unit"] = "in"
    assert six_class_against(proposal, "1", unit="mm")["counts"] == {"differ": 1}
    reference = other_label_candidate(["Brass"])
    result = compare_reference(
        DEFINITIONS, [candidate("Brass")], {("P1", "Size"): [reference]}, comparison_schema="six_class",
    )
    assert result["counts"] == {"differ": 1}


def test_only_label_changes_not_shared_source_values_affect_conflict_detection():
    labelled = other_label_candidate(["Brass"])
    reference = {("P1", "Size"): [{"value": "Brass"}]}
    matching = compare_reference(
        DEFINITIONS, [labelled, candidate("Brass")], reference, comparison_schema="six_class",
    )
    assert matching["counts"] == {"format-only difference": 1}
    assert matching["rows"][0]["docintel_conflict"] is False
    conflicting = compare_reference(
        DEFINITIONS, [labelled, candidate("Bronze")], reference, comparison_schema="six_class",
    )
    assert conflicting["counts"] == {"differ": 1}
    assert conflicting["rows"][0]["docintel_conflict"] is True
    assert len(conflicting["rows"][0]["docintel"]) == 2


def test_other_label_normalization_is_six_class_only():
    proposal = other_label_candidate(["Brass"])
    assert not values_agree(proposal, {"value": "Brass"})
    assert compare_reference(
        DEFINITIONS, [proposal], {("P1", "Size"): [{"value": "Brass"}]},
    )["counts"] == {"disagree": 1}
