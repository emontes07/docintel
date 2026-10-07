import pytest

from backend.batch import export_workbook
from backend.quality_scoring import compare_answer_key, compare_reference, load_cowork_reference, values_agree


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
