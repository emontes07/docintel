"""Synthetic paraphrase reproductions; never recovered provider responses."""

from collections import Counter

import pytest

from backend.evidence_verification import match_quote, normalized
from backend.extract import validate_response
from backend.response_validation import ResponseValidationError
from tests.test_evidence_verification import excerpt, response, row
from tests.test_pdf_presentation import table


@pytest.mark.parametrize("source,quote", [
    ("BODY MATERIAL CAST LOW LEAD BRASS ALLOY", "Cast low lead brass alloy is the body material"),
    ("BODY MATERIAL CAST LOW LEAD BRASS ALLOY", "The material of the body is cast low lead brass alloy"),
    ("RING MATERIAL EPDM ASTM D2000", "EPDM ASTM D2000 is the ring material"),
    ("ALL WETTED PARTS ARE LOW LEAD BRASS ALLOY", "Low lead brass alloy: all wetted parts"),
    ("ALL WETTED PARTS ARE LOW LEAD BRASS ALLOY", "All wetted parts are of low lead brass alloy"),
])
def test_reordered_paragraph_is_audited(source, quote):
    evidence = [excerpt(source)]
    match = match_quote(quote, evidence, {evidence[0].evidence_id})
    assert match is not None
    assert "order_tolerant_multiset" in match.normalization
    assert "no_unsupported_content_tokens" in match.normalization
    assert match.evidence_ids == [evidence[0].evidence_id]


def test_multi_cell_paraphrase_is_bound_to_each_source_cell():
    evidence = [row("3/4 IN COPPER SERVICE INLET", "FLAT HEAD INDICATING ARROW DRILLED FOR WIRE SEAL")]
    quote = "The copper service inlet is 3/4 in; the flat head is drilled for wire seal with indicating arrow"
    match = match_quote(quote, evidence, {evidence[0].evidence_id})
    assert match is not None
    assert match.cells == ["A2", "B2"]
    assert "clause_to_cell" in match.normalization
    candidate, manifest = response("3/4 in", quote, evidence)
    assert validate_response(candidate, manifest, evidence)[0].value.cells == ["A2"]


@pytest.mark.parametrize("source,quote", [
    ("BODY MATERIAL CAST LOW LEAD BRASS ALLOY", "The body material is cast low lead titanium alloy"),
    ("NOT LOW LEAD BRASS ALLOY", "The alloy is low lead brass"),
    ("BRASS OR STEEL BODY", "The body is brass"),
    ("BRASS AND STEEL BODY", "The body is brass or steel"),
    ("ONLY BRASS BODY", "The body is brass"),
    ("MAXIMUM PRESSURE 100 PSI", "100 PSI is the pressure"),
    ("WORKING PRESSURE 100 PSI", "The maximum pressure is 100 PSI"),
    ("INLET 1 IN OUTLET 2 IN", "The outlet is 1 in; the inlet is 2 in"),
    ("INLET 1 IN OUTLET 2 IN", "The outlet is 1 in and the inlet is 2 in"),
    ("MINIMUM 10 MAXIMUM 20 PSI", "20 minimum 10 maximum PSI"),
    ("BODY BRASS STEM STEEL", "Steel body brass stem"),
    ("RING MATERIAL EPDM ASTM D2000", "EPDM ASTM D2001 is the ring material"),
    ("RING MATERIAL EPDM ASTM D2000", "EPDM ASTM D2000 is the body material"),
    ("PRESSURE -100 PSI", "100 PSI is the pressure"),
    ("PRESSURE 100 PSI", "100 bar is the pressure"),
    ("BODY MATERIAL LOW LEAD BRASS", "The body material is lead reduced copper alloy"),
    ("BODY NOT BRASS", "Brass not body"),
    ("PRESSURE 100 PSI 2 BAR", "The pressure is 2 PSI 100 bar"),
])
def test_role_qualifier_numeric_and_unsupported_tokens_are_rejected(source, quote):
    evidence = [excerpt(source)]
    assert match_quote(quote, evidence, {evidence[0].evidence_id}) is None


def test_overlap_threshold_alone_cannot_admit_invented_material():
    source = ("BODY MATERIAL CAST LOW LEAD BRASS ALLOY POTABLE WATER SERVICE CONNECTION "
              "MANUFACTURER PRODUCT DESCRIPTION VALVE CASTING STANDARD FINISH TEST REPORT QUALITY")
    quote = source + " titanium"
    source_tokens, _ = normalized(source)
    tokens, _ = normalized(quote)
    assert sum((Counter(source_tokens) & Counter(tokens)).values()) / len(tokens) >= 0.95
    evidence = [excerpt(source)]
    assert match_quote(quote, evidence, {evidence[0].evidence_id}) is None


def test_swapped_inlet_outlet_same_row_never_passes_cell_containment():
    evidence = [row("INLET 1 IN COPPER", "OUTLET 2 IN BRASS")]
    assert match_quote("The outlet is 1 in copper; the inlet is 2 in brass",
                       evidence, {evidence[0].evidence_id}) is None
    assert match_quote("The outlet is 2 in brass; the inlet is 1 in copper",
                       evidence, {evidence[0].evidence_id}) is not None


def test_new_matching_still_requires_citations_and_grounded_value():
    evidence = [excerpt("BODY MATERIAL CAST LOW LEAD BRASS ALLOY")]
    quote = "Cast low lead brass alloy is the body material"
    assert match_quote(quote, evidence, set()) is None
    assert match_quote(quote, evidence, {"unknown"}) is None
    candidate, manifest = response("STEEL", quote, evidence, attribute="Primary Material")
    with pytest.raises(ResponseValidationError):
        validate_response(candidate, manifest, evidence)


def test_paraphrase_cannot_pool_paragraphs_or_vendor_rows():
    evidence = [excerpt("BODY MATERIAL"), excerpt("BRASS ALLOY", 1)]
    assert match_quote("The material of the body is brass alloy", evidence,
                       {entry.evidence_id for entry in evidence}) is None
    first = row("COPPER INLET")
    second = row("OUTLET BRASS").model_copy(update={"evidence_id": "another-row"})
    assert match_quote("The inlet is copper; the outlet is brass", [first, second],
                       {first.evidence_id, second.evidence_id}) is None


def test_independently_retrieved_web_paragraph_uses_same_containment():
    evidence = [excerpt("BODY MATERIAL CAST LOW LEAD BRASS ALLOY", tier="manufacturer_web")]
    quote = "Cast low lead brass alloy is the body material"
    match = match_quote(quote, evidence, {evidence[0].evidence_id})
    assert match is not None and "order_tolerant_multiset" in match.normalization


def test_reconstructed_component_quote_preserves_citations_and_associations():
    evidence = table([
        ["No.", "DESCRIPTION", "MATERIAL", "No.", "DESCRIPTION", "MATERIAL"],
        ["1", "BODY", "CAST BRONZE", "2", "RING", "EPDM ASTM D2000"],
    ])
    cited = {entry.evidence_id for entry in evidence}
    match = match_quote("Cast bronze is the body material", evidence, cited)
    assert match is not None and set(match.evidence_ids) == cited
    assert match_quote("EPDM ASTM D2000 is the body material", evidence, cited) is None
    assert match_quote("Cast bronze is the ring material", evidence, cited) is None
    assert match_quote("Cast bronze is the body material", evidence, {evidence[-1].evidence_id}) is None


def test_drawing_context_is_not_a_new_paraphrase_scope():
    evidence = [excerpt("TOLERANCES BODY MATERIAL CAST BRONZE")]
    assert match_quote("Cast bronze is the body material", evidence, {evidence[0].evidence_id}) is None
