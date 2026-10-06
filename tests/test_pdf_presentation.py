"""Synthetic row-presentation reproductions; no recovered model output."""

import copy
import json

import pytest

from backend.batch_worker import compact_inference_prompt
from backend.extract import validate_response
from backend.pdf_presentation import pdf_items
from backend.response_validation import ResponseValidationError, map_citations, response_diagnostic
from tests.test_evidence_verification import excerpt, response


def table(rows):
    return [
        excerpt(text, f"{row}-{column}", location=f"table=0&row={row}&column={column}")
        for row, cells in enumerate(rows) for column, text in enumerate(cells) if text
    ]


def prompt(evidence):
    original = [entry.model_dump(mode="json") for entry in evidence]
    compact, references = compact_inference_prompt(json.dumps({"evidence": original}))
    assert original == [entry.model_dump(mode="json") for entry in evidence]
    return json.loads(compact), references


def test_header_paragraphs_and_duplicate_cells_are_not_disconnected_citations():
    evidence = table([["No.", "DESCRIPTION", "MATERIAL"], ["1", "BODY", "CAST BRONZE"]])
    evidence += [excerpt(text, 100 + index) for index, text in enumerate(
        ("No.", "DESCRIPTION", "MATERIAL", "1", "BODY", "CAST BRONZE", "NOTE: TEST ONLY", "MODEL-X PART NUMBER")
    )]
    before = copy.deepcopy(evidence)
    items = pdf_items(evidence)
    assert evidence == before
    assert len(items) == 3
    row = next(item for item in items if item.kind == "table_row")
    assert row.text == "Row 1: No.=1 | DESCRIPTION=BODY | MATERIAL=CAST BRONZE"
    assert len(row.originals) == 6 and [entry.text for entry in row.values] == ["BODY", "CAST BRONZE"]
    assert {item.text for item in items if item.kind == "paragraph"} == {"NOTE: TEST ONLY", "MODEL-X PART NUMBER"}
    payload, references = prompt(evidence)
    reference = next(record[0] for record in payload["evidence"] if record[4]["kind"] == "table_row")
    assert references[reference] == [entry.evidence_id for entry in row.originals]
    candidates, manifest = response("bronze", row.text.casefold(), evidence, attribute="Body Material")
    candidates.candidates[0].evidence_ids = [" " + reference.lower() + " ", reference]
    mapped = map_citations(candidates, references)
    check = validate_response(mapped, manifest, evidence)[0]
    assert check.quote.method == "reconstructed_row"
    assert check.quote.evidence_ids == [entry.evidence_id for entry in row.originals]
    assert check.quote.source_locations == [entry.source_locator for entry in row.originals]
    assert check.value.source_locations == [evidence[5].source_locator]
    assert "table_row_reconstruction" in check.quote.normalization
    mapped.candidates[0].supporting_quote = row.text.replace("=", ": ")
    assert validate_response(mapped, manifest, evidence)[0].quote.method == "reconstructed_row"
    mapped.candidates[0].supporting_quote = row.text.replace("=", "\uff1d")
    assert "unicode_nfkc" in validate_response(mapped, manifest, evidence)[0].quote.normalization


def test_partial_original_aliases_cannot_authorize_unseen_row_cells():
    evidence = table([["No.", "DESCRIPTION", "MATERIAL"], ["1", "BODY", "BRONZE"]])
    _, references = prompt(evidence)
    item = pdf_items(evidence)[0]
    candidates, manifest = response("BRONZE", item.text, evidence, attribute="Body Material")
    candidates.candidates[0].evidence_ids = [item.anchor.evidence_id]
    with pytest.raises(ResponseValidationError, match="normalized match"):
        validate_response(map_citations(candidates, references), manifest, evidence)
    candidates.candidates[0].evidence_ids = ["E999"]
    with pytest.raises(ResponseValidationError, match="Unknown citation"):
        map_citations(candidates, references)


@pytest.mark.parametrize("value", ["1", "No.", "MATERIAL", "Column", "Row"])
def test_row_labels_headers_and_part_indexes_are_not_values(value):
    evidence = table([["No.", "DESCRIPTION", "MATERIAL"], ["1", "BODY", "BRONZE"]])
    _, references = prompt(evidence)
    candidates, manifest = response(value, pdf_items(evidence)[0].text, evidence)
    candidates.candidates[0].evidence_ids = ["E1"]
    with pytest.raises(ResponseValidationError) as caught:
        validate_response(map_citations(candidates, references), manifest, evidence)
    assert caught.value.issues[0].field_path == "candidates[0].value"


@pytest.mark.parametrize("component", ["O-RING", "BODY SEAL", "STEM", "BODY BUSHING"])
def test_parallel_component_groups_cannot_transfer_seal_material_to_body(component):
    evidence = table([
        ["No.", "DESCRIPTION", "MATERIAL", "No.", "DESCRIPTION", "MATERIAL"],
        ["1", "BODY", "BRONZE", "2", component, "EPDM"],
    ])
    _, references = prompt(evidence)
    item = pdf_items(evidence)[0]
    candidates, manifest = response("EPDM", item.text, evidence, attribute="Primary Material")
    candidates.candidates[0].evidence_ids = ["E1"]
    with pytest.raises(ResponseValidationError, match="component material"):
        validate_response(map_citations(candidates, references), manifest, evidence)
    candidates.candidates[0].value = "BRONZE"
    assert validate_response(map_citations(candidates, references), manifest, evidence)


def test_new_header_section_and_blank_row_gap_prevent_stale_header_assignment():
    evidence = table([
        ["No.", "DESCRIPTION", "MATERIAL"], ["1", "BODY", "BRONZE"],
        ["SIZE", "A", "B"], ["1", '1-1/2"', '2-1/4"'],
        [], ["MODEL-X PART NUMBER", "DRAWING-123"],
    ])
    items = pdf_items(evidence)
    assert next(item for item in items if item.location.get("row") == 3).text == 'Row 3: SIZE=1 | A=1-1/2" | B=2-1/4"'
    last = next(item for item in items if item.kind == "table_row" and item.location["row"] == 5)
    assert last.text == "Row 5: Column 1=DRAWING-123"
    assert any(item.kind == "paragraph" and item.text == "MODEL-X PART NUMBER" for item in items)


def test_sparse_drawing_callouts_are_not_materials_or_dimension_suffixes():
    evidence = table([
        ["No.", "DESCRIPTION", "MATERIAL", "No.", "DESCRIPTION", "MATERIAL"],
        ["1", "BODY", "BRONZE", "", "", "1 3"],
        ["SIZE", "A", "B"],
        ["1/2", '1-1/2"', '2-1/4"', "10", "", "B"],
    ])
    rows = pdf_items(evidence)
    assert rows[0].text == "Row 1: No.=1 | DESCRIPTION=BODY | MATERIAL=BRONZE"
    assert rows[1].text == 'Row 3: SIZE=1/2 | A=1-1/2" | B=2-1/4"'
    assert not pdf_items([excerpt("REV\nECR")])


def test_quotes_cannot_span_different_physical_table_rows():
    evidence = table([["BRONZE"], ["BODY"]])
    candidates, manifest = response("BRONZE", "BRONZE BODY", evidence)
    with pytest.raises(ResponseValidationError, match="normalized match"):
        validate_response(candidates, manifest, evidence)


def test_row_reference_diagnostics_allowlist_all_originals_without_leaking_values():
    evidence = table([["DESCRIPTION", "MATERIAL"], ["BODY", "SECRET-FICTIONAL-ALLOY"]])
    _, references = prompt(evidence)
    candidates, _ = response("SECRET-FICTIONAL-ALLOY", "secret quote", evidence)
    diagnostic = response_diagnostic(payload=candidates.model_dump(), references=references, issues=[])
    assert all(not item.redacted for item in diagnostic.used_references)
    assert "SECRET-FICTIONAL-ALLOY" not in diagnostic.model_dump_json()
    assert diagnostic.valid_references == ["E1"]


def test_duplicate_coordinates_fail_explicitly_and_scopes_remain_separate():
    evidence = table([["DESCRIPTION", "MATERIAL"], ["BODY", "BRONZE"]])
    with pytest.raises(ValueError, match="duplicate table coordinates"):
        pdf_items([*evidence, evidence[-1].model_copy(update={"evidence_id": "different"})])
    other = [entry.model_copy(update={
        "evidence_id": entry.evidence_id + "-other", "attribute_ids": ["Other"],
    }) for entry in evidence]
    assert len(pdf_items(evidence + other)) == 2
    restricted = [entry.model_copy(update={
        "evidence_id": entry.evidence_id + "-restricted", "attribute_ids": [],
    }) for entry in evidence]
    assert len(pdf_items(evidence + restricted)) == 2
