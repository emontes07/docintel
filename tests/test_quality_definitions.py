"""Offline definition tests; no workbook, reference or service IO.

Observed examples below are definition metadata from the retained technical
export's Definitions sheet, not Cowork, product evidence, or reference answers.
Synthetic allowed_values/component fields are explicitly labeled as such.
"""

import copy
import json
from pathlib import Path

import pytest

from backend import quality_definitions as quality

derive_definition = quality.derive_definition
model_instruction = quality.model_instruction
normalize_proposal = quality.normalize_proposal
source_bearing_texts = quality.source_bearing_texts

NODE = "Pipes, Valves & Fittings.Fittings.Brass Service Materials.Angle Valves"
OBSERVED_ROWS = [
    ("Blow-out Proof Stem", "Boolean", "Yes, No", "Boolean"),
    ("Inlet Connection Type", "Enumerated",
     "FPT, Pack Joint (CTS), Flare Copper, MPT, Compression, Grip Joint, Quick Joint", "Enumerated"),
    ("Pipe / Tubing Compatibility", "Multi-Select",
     "CTS Copper (Type K, Type L), PEP, PVC, Iron Pipe", "Multi-Select"),
    ("Pressure Rating", "Numeric", "100 PSI (key/globe), 150 PSI, 300 PSI (ball)", "Number"),
    ("Seal / Softgoods Material", "Enumerated", "EPDM, Buna-N, Nitrile, PTFE", "Enumerated"),
    ("Stem Rotation", "Enumerated", "90°, 360°", "Enumerated"),
]


def explicit(kind, **extra):
    """Only this helper invents definitions: synthetic, explicit approved fields."""
    return derive_definition({"attribute_id": "Synthetic", "type_guidance": kind, **extra})


def normalize(definition, value, **metadata):
    return normalize_proposal(definition, {"value": value, **metadata})


@pytest.mark.parametrize("name,guidance,examples,kind", OBSERVED_ROWS)
def test_observed_original_layout_does_not_promote_examples(name, guidance, examples, kind):
    original = {
        "Category Management": "keep", "discovery_method": "Research",
        "node": NODE, "node_level": "4", "parent_node": NODE.rsplit(".", 1)[0],
        "potential_attribute_name": name, "potential_attribute_data_type": guidance,
        "potential_attribute_example_values": examples,
        "source_basis": "Definition research; not product evidence",
    }
    scalar = {"Boolean": "boolean", "Numeric": "number"}.get(guidance, "string")
    manifest = {
        "attribute_id": name, "description": name, "value_type": scalar,
        "unit": None, "allowed_values": [], "examples": [],
        "definition_context": original["source_basis"], "type_guidance": guidance,
        "definition_node": NODE, "unit_resolved": guidance != "Numeric",
    }
    definition = derive_definition(manifest, original_row=original)
    assert definition.kind == kind
    assert definition.allowed_values == ()
    assert definition.allow_component_detail is False
    assert definition.original_row == original
    assert definition.original_row["potential_attribute_example_values"] == examples
    assert definition.ready
    assert bool(definition.definition_questions) is (guidance != "Boolean")
    assert json.loads(json.dumps(model_instruction(definition)))["expected_type"] == kind
    if kind == "Number":
        assert definition.unit is None and not definition.unit_resolved
    if kind in {"Enumerated", "Multi-Select"}:
        result = normalize(definition, "Other: EPDM O-rings")
        assert result.valid and result.requires_review and result.definition_questions
        assert result.value == "Other: EPDM O-rings"
        assert source_bearing_texts(result) == ("EPDM O-rings",)


def test_actual_retained_result_manifests_without_reading_workbooks():
    path = Path(__file__).resolve().parents[1] / "output/private/gap-20261007b/results.json"
    if not path.is_file():
        pytest.skip("Optional private retained result is not present")
    retained = json.loads(path.read_text())
    assert len(retained) == 4
    counts = {}
    for item in retained.values():
        assert len(item["manifest"]["attributes"]) == 24
        for fields in item["manifest"]["attributes"]:
            definition = derive_definition(fields)
            counts[definition.kind] = counts.get(definition.kind, 0) + 1
            assert definition.allowed_values == ()
            assert not definition.allow_component_detail
            assert definition.ready
            assert bool(definition.definition_questions) is (definition.kind != "Boolean")
            value = {"Boolean": True, "Number": 150}.get(definition.kind, "Other: source value")
            result = normalize(definition, value, unit="PSI" if definition.kind == "Number" else None)
            assert result.valid
            assert result.requires_review is (definition.kind != "Boolean")
            assert result.grounding_status == "not_checked"
    assert set(counts) == {"Boolean", "Enumerated", "Multi-Select", "Number"}


def test_original_row_is_accepted_directly_and_supported_fields_are_explicit():
    row = {
        "potential_attribute_name": "Synthetic", "potential_attribute_data_type": "Multi-Select",
        "allowed_values": '["CTS Copper (Type K, Type L)", "PEP"]',
    }
    definition = derive_definition(row)
    assert definition.ready
    assert definition.allowed_values == ("CTS Copper (Type K, Type L)", "PEP")
    assert definition.provenance["allowed_values"] == "fields.allowed_values"


def test_explicit_original_enrichment_is_allowed_but_conflicts_fail_closed():
    fields = {"attribute_id": "Synthetic", "type_guidance": "Enumerated", "allowed_values": [], "definition_node": NODE}
    row = {"potential_attribute_name": "Synthetic", "node": NODE, "allowed_values": '["Ball", "Ground Key"]'}
    definition = derive_definition(fields, original_row=row)
    assert definition.ready and definition.allowed_values == ("Ball", "Ground Key")
    assert definition.provenance["allowed_values"] == "original_row.allowed_values"
    for change in [
        {"potential_attribute_name": "Different"}, {"node": "Wrong.Node"},
        {"potential_attribute_data_type": "Boolean"},
    ]:
        bad = derive_definition(fields, original_row={**row, **change})
        assert not bad.ready
    bad = derive_definition({**fields, "allowed_values": ["Compression"]}, original_row=row)
    assert not bad.ready


@pytest.mark.parametrize("fields", [
    {"description": "Boolean or Numeric with PSI"},
    {"potential_attribute_example_values": "Yes, No"},
    {"type_guidance": "GuessFromEvidence", "value_type": "string"},
    {"type_guidance": "Enumerated", "value_type": "boolean", "allowed_values": ["Ball"]},
    {"type_guidance": "Numeric (PSI)"},
    {"type_guidance": "Enumerated", "allowed_values": "Ball, Ground Key"},
    {"type_guidance": "Enumerated", "allowed_values": [True, 1]},
    {"type_guidance": "Enumerated", "allowed_values": [""]},
    {"type_guidance": "Enumerated", "allowed_values": [" Ball "]},
    {"type_guidance": "Enumerated", "allowed_values": ["Other: TBD"]},
    {"type_guidance": "Multi-Select", "allowed_values": ["Ball; Ground Key"]},
    {"type_guidance": "Boolean", "allowed_values": [float("nan")]},
])
def test_no_type_options_or_units_inferred_from_prose(fields):
    assert not derive_definition({"attribute_id": "Synthetic", **fields}).ready


@pytest.mark.parametrize("value_type,kind", [
    ("boolean", "Boolean"), ("string", "Text"), ("number", "Number"), ("integer", "Number"),
])
def test_explicit_scalar_fallback(value_type, kind):
    definition = derive_definition({"attribute_id": "Synthetic", "value_type": value_type, "unit": None})
    assert definition.ready and definition.kind == kind


@pytest.mark.parametrize("value,expected,display", [
    (True, True, "Yes"), (False, False, "No"), ("Yes", True, "Yes"), ("No", False, "No"),
    (" true ", True, "Yes"), ("FALSE", False, "No"),
])
def test_boolean_is_literal_internal_bool_display_yes_no(value, expected, display):
    result = normalize(explicit("Boolean"), value)
    assert result.valid and type(result.value) is bool and result.value is expected
    assert result.display_value == display
    assert result.derivation_rule == "boolean_literal_display_yes_no_v1"
    assert result.requires_quote_grounding and result.grounding_status == "not_checked"


@pytest.mark.parametrize("value", [
    1, 0, 1.0, "1", "0", "not false", "No lead", "Yes / No",
    "lockable", "without padlock", "", None, ["Yes"],
])
def test_boolean_does_not_guess_features_negation_or_alternatives(value):
    assert not normalize(explicit("Boolean"), value).valid


def test_bool_is_not_integer_in_approved_scalar_options():
    assert not normalize(explicit("Boolean", allowed_values=[1]), True).valid
    assert not normalize(explicit("Numeric", unit=None, allowed_values=[True]), 1).valid


@pytest.mark.parametrize("value,valid", [
    ("Ball", True), ("Ground Key (Inverted Key)", True), (" ball ", False),
    ("Ground Key", False), ("BALL", False), ("Compression Globe", False),
    ("Ball or Ground Key (Inverted Key)", False), ("Ball; Ground Key (Inverted Key)", False),
    ("Other: Compression Globe", True), ("Other: not Ball; Ground Key or globe", True),
    ("Other:", False), ("Other:   ", False), ("other: Compression Globe", False),
    ("Other:Compression Globe", False), (["Ball"], False),
])
def test_enum_requires_exact_value_or_explicit_other(value, valid):
    definition = explicit("Enumerated", allowed_values=["Ball", "Ground Key (Inverted Key)"])
    result = normalize(definition, value)
    assert result.valid is valid
    if valid:
        assert result.value == value


def test_other_keeps_numbers_negation_alternatives_and_unmodified_metadata():
    definition = explicit("Enumerated", allowed_values=["Ball"])
    proposal = {
        "attribute_id": "Synthetic", "value": 'Other: not rated 150 PSI; 100 or 300 PSI, 5/8"x3/4"',
        "supporting_quote": '  RAW "not rated 150 PSI; 100 or 300 PSI, 5/8"x3/4""\n',
        "origin": "model_generated", "evidence_ids": ["e1"],
        "machine": {"proposal": ["not rated", 150], "raw_response": '{"unchanged":true}'},
    }
    before = copy.deepcopy(proposal)
    result = normalize_proposal(definition, proposal)
    assert result.valid and result.original_proposal == before and proposal == before
    assert source_bearing_texts(result) == ('not rated 150 PSI; 100 or 300 PSI, 5/8"x3/4"',)
    result.original_proposal["machine"]["proposal"].append("separate copy")
    assert proposal == before


@pytest.mark.parametrize("value,expected", [
    ("PEP;PVC", "PEP; PVC"), ("PEP ; PVC", "PEP; PVC"), (["PEP", "PVC"], "PEP; PVC"),
    ("CTS Copper (Type K, Type L); PEP", "CTS Copper (Type K, Type L); PEP"),
    (["PEP", "PEP"], "PEP; PEP"),
    ("PEP; Other: not PVC or PEX", "PEP; Other: not PVC or PEX"),
])
def test_multiselect_is_scalar_canonical_preserves_order_and_duplicates(value, expected):
    definition = explicit("Multi-Select", allowed_values=["CTS Copper (Type K, Type L)", "PEP", "PVC"])
    result = normalize(definition, value)
    assert result.valid and type(result.value) is str and result.value == expected
    assert result.display_value == expected
    assert result.derivation_rule == "exact_members_semicolon_space_v1"


@pytest.mark.parametrize("value", [
    "PEP, PVC", "PEP/PVC", "PEP or PVC", "PEP and PVC", "PEP; PEX",
    "PEP;", ";PEP", "PEP;;PVC", ["PEP", ""], ["PEP", True], [], "pep",
    ["PEP; PVC"], "Other: PEX; bad unlisted", {"PEP": True},
])
def test_multiselect_validates_every_member_without_guessing_delimiters(value):
    assert not normalize(explicit("Multi-Select", allowed_values=["PEP", "PVC"]), value).valid


def test_other_multiselect_has_separate_grounding_obligations():
    definition = explicit("Multi-Select", allowed_values=["NSF/ANSI 61", "NSF/ANSI 372"])
    result = normalize(definition, "NSF/ANSI 61; Other: not NSF/ANSI 372; Other: 100 PSI or 150 PSI")
    assert source_bearing_texts(result) == ("NSF/ANSI 61", "not NSF/ANSI 372", "100 PSI or 150 PSI")
    assert result.requires_quote_grounding


def components(kind="Enumerated"):
    return explicit(
        kind, allowed_values=["EPDM", "nitrile", "PTFE"],
        allow_component_detail=True, component_labels=["O-rings", "rubber gasket", "Wetted", "non-wetted"],
    )


@pytest.mark.parametrize("value", [
    "EPDM O-rings; nitrile rubber gasket",
    "Wetted: EPDM; non-wetted: nitrile",
    "Wetted: Other: not PTFE; non-wetted: nitrile",
])
def test_explicit_component_details_stay_intact_and_labels_must_be_grounded(value):
    result = normalize(components(), value)
    assert result.valid and result.value == value
    obligations = source_bearing_texts(result)
    assert obligations == tuple(part.replace("Other: ", "", 1) for part in value.split("; "))
    pending = normalize(explicit("Enumerated", allowed_values=["EPDM", "nitrile", "PTFE"]), value)
    assert pending.valid and pending.requires_review and pending.definition_questions
    assert pending.value == value and source_bearing_texts(pending) == obligations
    assert not normalize(explicit(
        "Enumerated", allowed_values=["EPDM", "nitrile", "PTFE"], allow_component_detail=False,
    ), value).valid


@pytest.mark.parametrize("value", [
    "EPDM O-rings; Nitrile rubber gasket",
    "EPDM or nitrile O-rings", "not EPDM O-rings", "EPDM asbestos O-rings",
    "Wetted: EPDM; non-wetted: NBR", "Wetted: Other: not PTFE; non-wetted: NBR",
    "Wetted: PTFE; nitrile",
    "Unknown: EPDM", "Otherness: EPDM", "Wetted: EPDM; unlisted",
    "Wetted: Other: ", "non-wetted: Nitrile", "EPDM no O-rings",
])
def test_components_cannot_smuggle_synonyms_negation_or_unlisted_values(value):
    assert not normalize(components(), value).valid


@pytest.mark.parametrize("extra", [
    {"allow_component_detail": "yes", "component_labels": ["Wetted"]},
    {"allow_component_detail": True, "component_labels": ["non-wetted: "]},
    {"allow_component_detail": True, "component_labels": "Wetted, non-wetted"},
    {"allow_component_detail": False, "component_labels": ["Wetted"]},
])
def test_components_need_explicit_permission_and_unambiguous_labels(extra):
    assert not explicit("Enumerated", allowed_values=["EPDM"], **extra).ready


def test_workbook_explicit_component_json_fields_supported_without_inference():
    definition = explicit(
        "Multi-Select", allowed_values='["EPDM", "nitrile"]',
        allow_component_detail="true", component_labels='["Wetted", "non-wetted"]',
    )
    assert definition.ready
    assert normalize(definition, "Wetted: EPDM; non-wetted: nitrile").valid


@pytest.mark.parametrize("kind", ["Text", "String"])
def test_text_preserves_all_original_text_and_other_is_not_a_wrapper(kind):
    value = '  Other: not 150 PSI; 100 or 300 PSI\nWetted: EPDM; non-wetted: nitrile rubber gasket  '
    result = normalize(explicit(kind), value)
    assert result.valid and result.value == result.display_value == value
    assert source_bearing_texts(result) == (value,)


@pytest.mark.parametrize("value,unit,expected", [
    (150, "PSI", 150), ("150 PSI", None, 150), ("150", "PSI", 150),
    ("-15.5 PSI", None, -15.5), ("+1.5e2", "PSI", 150.0),
    (0.1, "PSI", 0.1),
])
def test_number_unit_normalization_is_literal_and_no_unit_conversion(value, unit, expected):
    definition = explicit("Number+unit", unit="PSI")
    result = normalize(definition, value, unit=unit)
    assert result.valid and result.value == expected and result.unit == "PSI"
    source = str(value)
    assert source_bearing_texts(result) == (source if source.endswith(" PSI") else source + " PSI",)


@pytest.mark.parametrize("value,unit", [
    (True, "PSI"), (float("inf"), "PSI"), (float("nan"), "PSI"), ("1e999", "PSI"),
    ("0.1234567890123456789 PSI", None), ("not 150 PSI", None),
    ("150 or 300 PSI", None), ("100-150 PSI", None), ("<=150 PSI", None),
    ("150 PSI at 70 F", None), ("150 psi", None), ("150 kPa", "PSI"),
    ("150 PSI", "kPa"), (150, None), ("150", None), ([150], "PSI"),
])
def test_numeric_qualifiers_negation_alternatives_and_wrong_units_are_not_dropped(value, unit):
    proposal = {"value": value, "unit": unit, "supporting_quote": "unchanged"}
    result = normalize_proposal(explicit("Numeric", unit="PSI"), proposal)
    assert not result.valid and result.value is None
    assert result.original_proposal["supporting_quote"] == "unchanged"
    with pytest.raises(ValueError, match="Rejected"):
        source_bearing_texts(result)


def test_numeric_units_need_explicit_declaration_and_original_unit_can_restore_it():
    fields = {
        "attribute_id": "Pressure", "value_type": "number", "type_guidance": "Numeric",
        "unit": None, "unit_resolved": False,
    }
    pending = derive_definition(fields)
    assert pending.ready and pending.definition_questions and not pending.unit_resolved
    for unit in ["PSI", ""]:
        definition = derive_definition(fields, original_row={"potential_attribute_name": "Pressure", "unit": unit})
        assert definition.ready and definition.unit_resolved
        assert normalize(definition, 150, unit=unit or None).valid
    assert not explicit("Number+unit", unit=None).ready
    assert not explicit("Numeric", unit=" ").ready


def test_integer_contract_remains_distinct_from_number_and_bool():
    definition = explicit("Numeric", value_type="integer", unit="")
    assert normalize(definition, "15.0").value == 15
    assert normalize(definition, -15).valid
    assert not normalize(definition, "1.5").valid
    assert not normalize(definition, 15.0).valid
    assert not normalize(definition, True).valid


def test_instruction_and_result_explicitly_do_not_claim_grounding():
    definition = components("Multi-Select")
    instruction = model_instruction(definition)
    assert instruction["allowed_values"] == ["EPDM", "nitrile", "PTFE"]
    assert instruction["definition_ready"]
    assert "never approves grounding" in instruction["grounding_rule"]
    result = normalize(definition, "EPDM")
    assert result.valid and result.grounding_status == "not_checked"
    assert "supporting_quote" not in result.original_proposal
    assert result.requires_quote_grounding


def test_rejected_metadata_is_retained_and_wrong_attribute_never_normalizes():
    definition = explicit("Boolean")
    proposal = {"value": True, "attribute_id": "Different", "supporting_quote": '"raw"', "machine": {"x": [1]}}
    result = normalize_proposal(definition, proposal)
    assert not result.valid and result.original_proposal == proposal
    proposal["machine"]["x"].append(2)
    assert result.original_proposal["machine"]["x"] == [1]
    assert not normalize_proposal(definition, {}).valid


@pytest.mark.parametrize("value,expected,source", [
    ("Compression Globe", "Other: Compression Globe", "Compression Globe"),
    ("Other: Compression Globe", "Other: Compression Globe", "Compression Globe"),
    ('not 150 PSI; 100 or 300 PSI, 5/8"x3/4"',
     'Other: not 150 PSI; 100 or 300 PSI, 5/8"x3/4"', 'not 150 PSI; 100 or 300 PSI, 5/8"x3/4"'),
    ("EPDM O-rings; nitrile rubber gasket",
     "Other: EPDM O-rings; nitrile rubber gasket", "EPDM O-rings; nitrile rubber gasket"),
    ("Other: EPDM O-rings; nitrile rubber gasket",
     "Other: EPDM O-rings; nitrile rubber gasket", "EPDM O-rings; nitrile rubber gasket"),
])
def test_missing_whitelist_retains_other_instead_of_losing_candidate(value, expected, source):
    definition = explicit("Enumerated")
    assert definition.ready and not definition.issues
    assert not definition.allowed_values and definition.definition_questions
    result = normalize(definition, value, supporting_quote=source, evidence_ids=["e1"])
    assert result.valid and result.value == result.display_value == expected
    assert result.requires_review and result.definition_questions == definition.definition_questions
    assert not result.errors and source_bearing_texts(result) == (source,)
    assert result.original_proposal["value"] == value
    assert result.requires_quote_grounding and result.grounding_status == "not_checked"
    second = normalize(definition, result.value)
    assert second.valid and second.value == result.value
    assert source_bearing_texts(second) == source_bearing_texts(result)


@pytest.mark.parametrize("value,expected,parts", [
    ("PEP; PVC", "Other: PEP; Other: PVC", ("PEP", "PVC")),
    (["PEP", "Other: not PVC or PEX"], "Other: PEP; Other: not PVC or PEX", ("PEP", "not PVC or PEX")),
    ("CTS Copper (Type K, Type L)", "Other: CTS Copper (Type K, Type L)", ("CTS Copper (Type K, Type L)",)),
])
def test_missing_multiselect_options_retains_every_member_as_other(value, expected, parts):
    result = normalize(explicit("Multi-Select"), value)
    assert result.valid and type(result.value) is str and result.value == expected
    assert result.requires_review and result.definition_questions
    assert source_bearing_texts(result) == parts


@pytest.mark.parametrize("kind,value", [
    ("Enumerated", ""), ("Enumerated", "Other:"), ("Enumerated", "Other:   "),
    ("Enumerated", "Other:no space"), ("Enumerated", "other: value"),
    ("Enumerated", True), ("Enumerated", ["one"]),
    ("Multi-Select", [""]), ("Multi-Select", [False]), ("Multi-Select", "one;;two"),
    ("Multi-Select", "one;Other:"), ("Multi-Select", {"value": "one"}),
])
def test_missing_options_does_not_make_invalid_structures_valid(kind, value):
    result = normalize(explicit(kind), value)
    assert not result.valid and result.errors and result.original_proposal["value"] == value


@pytest.mark.parametrize("value,unit,expected", [
    (150, None, 150), (150, "PSI", 150), ("150", "PSI", 150),
    ("150 PSI", "PSI", 150), ("-1.50e2", "bar", -150.0), (0, None, 0),
])
def test_missing_numeric_unit_preserves_typed_value_and_supplied_unit(value, unit, expected):
    definition = explicit("Numeric", potential_attribute_example_values="150 PSI, 300 PSI")
    result = normalize(definition, value, unit=unit)
    assert definition.unit is None and not definition.unit_resolved
    assert result.valid and result.requires_review and result.definition_questions
    assert result.value == expected and result.unit == unit
    source = str(value)
    if unit is not None and not source.endswith(" " + unit):
        source += " " + unit
    assert source_bearing_texts(result) == (source,)
    assert result.original_proposal["value"] == value
    assert result.derivation_rule == "literal_number_unit_pending_definition_v1"


@pytest.mark.parametrize("value,unit", [
    (True, None), (False, "PSI"), (float("inf"), None), ("1e9999", None),
    ("150 PSI", None), ("150 PSI", "bar"), ("not 150", "PSI"),
    ("150 or 300", "PSI"), ("150 PSI at 70 F", "PSI"),
    ("100-150", None), ("<=150", "PSI"), ("", None), (150, ""),
    (150, " PSI "), (150, False), (150, []), ("0.1234567890123456789", "PSI"),
])
def test_missing_numeric_guidance_never_salvages_invalid_number_or_guesses_unit(value, unit):
    result = normalize(explicit("Numeric"), value, unit=unit, supporting_quote='"not 150 PSI"')
    assert not result.valid and result.value is None and result.definition_questions
    assert result.original_proposal["supporting_quote"] == '"not 150 PSI"'
    with pytest.raises(ValueError):
        source_bearing_texts(result)


@pytest.mark.parametrize("value", [1, "not false", "Yes or No", "lead free", None])
def test_boolean_stays_strict_when_optional_guidance_is_absent(value):
    assert not normalize(explicit("Boolean"), value).valid


@pytest.mark.parametrize("extra", [
    {}, {"allow_component_detail": True}, {"component_labels": ["Wetted", "non-wetted"]},
])
def test_missing_component_permission_or_labels_retains_opaque_candidate(extra):
    definition = explicit("Enumerated", allowed_values=["EPDM", "nitrile"], **extra)
    value = "  Wetted: Other: not EPDM; non-wetted: nitrile  "
    result = normalize(definition, value)
    assert result.valid and result.requires_review
    assert any("component" in question for question in result.definition_questions)
    assert result.value == result.display_value == value
    assert result.derivation_rule == "unresolved_component_text_preserved_v1"
    assert source_bearing_texts(result) == ("Wetted: not EPDM", "non-wetted: nitrile")
    assert result.original_proposal["value"] == value


@pytest.mark.parametrize("kind", ["Enumerated", "Multi-Select"])
def test_missing_options_and_component_permissions_retains_unapproved_labels(kind):
    definition = explicit(kind)
    value = "Wetted: EPDM; non-wetted: nitrile rubber gasket"
    result = normalize(definition, value)
    assert result.valid and result.value == value and result.requires_review
    assert any("allowed_values" in question for question in result.definition_questions)
    assert any("component_detail" in question for question in result.definition_questions)
    assert source_bearing_texts(result) == ("Wetted: EPDM", "non-wetted: nitrile rubber gasket")
    assert not definition.component_permission_resolved
    assert definition.component_labels == () and definition.allowed_values == ()


def test_component_permission_without_options_keeps_found_material_for_review():
    definition = explicit("Enumerated", allow_component_detail=True, component_labels=["Wetted"])
    result = normalize(definition, "Wetted: EPDM")
    assert result.valid and result.requires_review and result.value == "Wetted: EPDM"
    assert source_bearing_texts(result) == ("Wetted: EPDM",)
    assert result.definition_questions == definition.definition_questions
    assert not normalize(definition, "Unknown: EPDM").valid


@pytest.mark.parametrize("value", [
    "NBR", "Wetted: NBR", "Wetted: EPDM; non-wetted: NBR", "Wetted:",
    "Wetted: Other:", ": EPDM", "EPDM or nitrile", "not EPDM O-rings",
])
def test_missing_component_permission_cannot_bypass_explicit_options(value):
    result = normalize(explicit("Enumerated", allowed_values=["EPDM", "nitrile"]), value)
    assert not result.valid and result.errors


def test_explicit_prohibition_and_numeric_options_still_reject_with_missing_guidance():
    definition = explicit("Enumerated", allow_component_detail=False)
    result = normalize(definition, "Wetted: EPDM")
    assert not result.valid
    assert any("explicit allow_component_detail=false" in error for error in result.errors)
    assert not normalize(explicit("Numeric", allowed_values=[150]), 200, unit="PSI").valid
    assert not normalize(explicit("Numeric", unit="PSI"), 150, unit="bar").valid
    assert not normalize(explicit("Enumerated", allowed_values=["EPDM"]), "Nitrile").valid


def test_model_contract_distinguishes_questions_from_invalid_definition():
    definition = explicit("Enumerated")
    instruction = model_instruction(definition)
    assert instruction["definition_ready"] and not instruction["definition_issues"]
    assert instruction["definition_questions"]
    assert instruction["component_permission_resolved"] is False
    assert "retain found candidates" in instruction["unresolved_rule"]
    assert "never approves grounding" in instruction["grounding_rule"]
    assert json.loads(json.dumps(instruction)) == instruction


def test_review_candidate_retains_raw_quotes_and_machine_metadata_without_mutation():
    definition = explicit("Enumerated")
    proposal = {
        "attribute_id": "Synthetic", "value": "Other: not 150 PSI; 100 or 300 PSI",
        "supporting_quote": '  "not 150 PSI; 100 or 300 PSI"\n',
        "qualification": "pending definition review", "machine_proposal": {"values": [150, 100, 300]},
    }
    before = copy.deepcopy(proposal)
    result = normalize_proposal(definition, proposal)
    assert result.valid and result.requires_review and result.original_proposal == before
    assert source_bearing_texts(result) == ("not 150 PSI; 100 or 300 PSI",)
    assert proposal == before
    result.original_proposal["machine_proposal"]["values"].append(900)
    assert proposal == before
    assert definition.definition_questions == result.definition_questions
