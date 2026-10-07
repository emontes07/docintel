"""Synthetic identity/linkage regressions; no workbook, network or model calls."""

from datetime import datetime, timezone
import json
from urllib.parse import quote

import pytest

from backend.models.enrichment import AttributeDefinition, Candidate, Evidence, Manifest, ProductKey
from backend.quality_applicability import build_applicability_map, candidate_applicability, candidate_confidence


NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def manifest(mpn="MODEL-A", sources=("pdf", "vendor")):
    return Manifest(
        product=ProductKey(item_id="synthetic", vendor="Synthetic Vendor", mpn=mpn, hierarchy_node="Valves"),
        attributes=[AttributeDefinition(attribute_id="Material", description="", value_type="string")],
        source_ids=list(sources),
    )


def excerpt(eid, text, *, source="pdf", locator=None, **kwargs):
    return Evidence(
        evidence_id=eid, source_id=source, source_tier="internal_pdf", source_version="v1",
        source_locator=locator or f"batchblob:///{source}.pdf#page=1&paragraph={eid}",
        text=text, content_kind="source_excerpt", observed_at=NOW, **kwargs,
    )


def table(rows, *, source="pdf", page=1):
    return [
        excerpt(
            f"{source}-p{page}-r{row}-c{column}", value, source=source,
            locator=f"batchblob:///{source}.pdf#page={page}&table=0&row={row}&column={column}",
        )
        for row, values in enumerate(rows) for column, value in enumerate(values) if value
    ]


def vendor(mpn="MODEL-A", *, source="vendor", row=2, key_column="MPN", fields=()):
    values = [(key_column, mpn), ("Material", "Brass"), *fields]
    cells = [
        {"cell": f"{chr(65 + column)}{row}", "column": name, "value": value}
        for column, (name, value) in enumerate(values)
    ]
    return Evidence(
        evidence_id=f"{source}-{row}", source_id=source, source_tier="vendor_table", source_version="v1",
        source_locator=f"batchblob:///{source}.xlsx#sheet={quote('Product Rows')}&row={row}&cells="
                       + ",".join(cell["cell"] for cell in cells),
        text=json.dumps({"sheet": "Product Rows", "row": row, "cells": cells}),
        content_kind="source_excerpt", observed_at=NOW,
    )


def candidate(*evidence_ids, origin="literal", **kwargs):
    return Candidate(
        attribute_id="Material", value="Brass", evidence_ids=list(evidence_ids),
        origin=origin, supporting_quote="Brass", **kwargs,
    )


def classify(m, evidence, c):
    return candidate_applicability(c, build_applicability_map(m, evidence), evidence)


@pytest.mark.parametrize("key", ["MPN", "Part #", "2nd Item Number", "Manufacturer Part Number"])
def test_full_vendor_row_exact_identity_and_decoded_cell_proof(key):
    row = vendor(key_column=key)
    mapping = build_applicability_map(manifest(), [row])
    assert mapping["vendor"].status == "exact"
    proof = next(proof for proof in mapping["vendor"].proofs if proof.role == "product_identity")
    assert proof.quote == "MODEL-A"
    assert proof.location == {"sheet": "Product Rows", "row": 2, "cell": "A2"}
    assert proof.source_locator == row.source_locator
    assert classify(manifest(), [row], candidate(row.evidence_id)).confidence == "High"


def test_original_vendor_chunk_locator_is_supported():
    row = vendor()
    row.source_locator = "vendor#'Product Rows'!A2:B2"
    assert build_applicability_map(manifest(), [row])["vendor"].status == "exact"


@pytest.mark.parametrize("mpn", ["MODEL-AB", "MODEL-A-NL", "MODEL/A", "MODELA"])
def test_identifiers_do_not_use_substring_or_punctuation_aliases(mpn):
    row = vendor(mpn)
    result = classify(manifest(), [row], candidate(row.evidence_id))
    assert (result.status, result.confidence) == ("family-unconfirmed", "Low")
    assert result.reviewer_questions


def test_identifier_whitespace_and_case_are_presentation_only():
    row = vendor("014255 215n")
    assert classify(manifest("014255    215N"), [row], candidate(row.evidence_id)).status == "exact"


def test_identity_must_be_an_identity_column_not_description():
    row = vendor(key_column="Description")
    assert build_applicability_map(manifest(), [row])["vendor"].status == "family-unconfirmed"


@pytest.mark.parametrize("mutation", ["json", "row", "sheet", "address", "duplicate", "missing_column"])
def test_malformed_vendor_evidence_fails_closed(mutation):
    row = vendor()
    data = json.loads(row.text)
    if mutation == "json":
        row.text = "{not json"
    elif mutation == "row":
        row.source_locator = row.source_locator.replace("row=2", "row=3")
    elif mutation == "sheet":
        data["sheet"] = "Other"
    elif mutation == "address":
        data["cells"][0]["cell"] = "A3"
    elif mutation == "duplicate":
        data["cells"].append(data["cells"][0])
    else:
        del data["cells"][0]["column"]
    if mutation != "json":
        row.text = json.dumps(data)
    assert classify(manifest(), [row], candidate(row.evidence_id)).confidence == "Low"


def test_generic_vendor_family_text_is_not_exact_because_source_is_bound():
    row = vendor()
    generic = row.model_copy(update={
        "evidence_id": "generic", "text": "The MODEL-A family uses brass.",
        "source_locator": "batchblob:///vendor.xlsx#sheet=Product%20Rows&row=9",
        "qualification": "Exact product association.",
    })
    mapping = build_applicability_map(manifest(), [row, generic])
    assert mapping["vendor"].status == "exact"
    assert candidate_applicability(candidate("generic"), mapping, [row, generic]).confidence == "Low"


def test_exact_pdf_row_proves_values_not_other_rows_or_paragraphs():
    ev = table([["Part Number", "Material"], ["MODEL-A", "Brass"], ["MODEL-B", "Steel"]])
    ev.append(excerpt("generic", "All models may have a brass body.", qualification="Bound to MODEL-A"))
    mapping = build_applicability_map(manifest(), ev)
    assert mapping["pdf"].status == "exact"
    exact = candidate_applicability(candidate("pdf-p1-r1-c1"), mapping, ev)
    assert (exact.status, exact.confidence) == ("exact", "High")
    assert any(proof.quote == "MODEL-A" and proof.location["row"] == 1 for proof in exact.proofs)
    for eid in ["generic", "pdf-p1-r2-c1"]:
        assert candidate_applicability(candidate(eid), mapping, ev).confidence == "Low"


def test_header_expansion_does_not_downgrade_exact_row_but_header_alone_is_low():
    ev = table([["Part Number", "Material"], ["MODEL-A", "Brass"]])
    result = classify(manifest(), ev, candidate("pdf-p1-r0-c0", "pdf-p1-r1-c0", "pdf-p1-r1-c1"))
    assert result.confidence == "High"
    assert classify(manifest(), ev, candidate("pdf-p1-r0-c0")).confidence == "Low"


def test_complete_pdf_row_citations_remain_exact_including_all_column_headers():
    ev = table([["Part Number", "Material"], ["MODEL-A", "Brass"]])
    ev += [vendor(fields=[("Drawing", "D-42")]), excerpt("heading", "Drawing: D-42")]
    result = classify(manifest(), ev, candidate(
        "pdf-p1-r0-c0", "pdf-p1-r0-c1", "pdf-p1-r1-c0", "pdf-p1-r1-c1",
    ))
    assert (result.status, result.confidence) == ("exact", "High")


def test_pdf_identity_column_does_not_require_a_fixed_attribute_header_vocabulary():
    ev = table([["MPN", "Operating Pressure Limit"], ["MODEL-A", "100 psi"]])
    assert classify(manifest(), ev, candidate("pdf-p1-r1-c1")).confidence == "High"


@pytest.mark.parametrize("mpn", ["MODEL-A", "MODEL-42"])
def test_dedicated_exact_product_page_is_high(mpn):
    ev = [
        excerpt("title", f"Part Number: {mpn}", locator="batchblob:///pdf.pdf#page=1&paragraph=0&role=title"),
        excerpt("material", "Material: Brass"),
        excerpt("generic", "All models in this series use a common service manual."),
    ]
    assert classify(manifest(mpn), ev, candidate("material")).confidence == "High"
    assert classify(manifest(mpn), ev, candidate("generic")).confidence == "Low"


@pytest.mark.parametrize("separator", [". ", "; ", " | ", "\n"])
@pytest.mark.parametrize("mpn", ["AV11-333W-NL", "AV11.333W-NL"])
def test_explicit_product_record_can_share_a_paragraph_with_attribute_fields(separator, mpn):
    text = f"Part number: {mpn}{separator}Body: BRASS."
    ev = [excerpt("body", text, locator="https://example.com/catalog#page=1&paragraph=0")]
    result = classify(manifest(mpn), ev, candidate("body"))
    assert (result.status, result.confidence) == ("exact", "High")
    assert any(proof.quote == text and proof.location["page"] == 1 for proof in result.proofs)


@pytest.mark.parametrize("text", [
    "AV11-333W-NL Body: BRASS.",
    "Compatible part number: AV11-333W-NL. Body: BRASS.",
    "Part number: AV11-333W-NLX. Body: BRASS.",
    "Part number: AV11-333W-NL-NL. Body: BRASS.",
    "Part number: AV11-333W-NL Body: BRASS.",
    "Part number: AV11-333W-NL or AV11-444W-NL. Body: BRASS.",
    "Part number: AV11-333W-NL. Body: BRASS. Part number: AV11-444W-NL.",
    "Part number: AV11-333W-NL\nBody: BRASS.\nPart number: AV11-444W-NL",
])
def test_inline_identity_parser_does_not_accept_aliases_bare_mentions_or_mixed_products(text):
    ev = [excerpt("body", text, locator="https://example.com/catalog#page=1&paragraph=0")]
    assert classify(manifest("AV11-333W-NL"), ev, candidate("body")).confidence == "Low"


def test_same_mpn_mentioned_in_body_is_not_an_exact_product_page():
    ev = [excerpt("body", "A general family paragraph mentions MODEL-A. Brass is optional.")]
    assert classify(manifest(), ev, candidate("body")).confidence == "Low"


def test_exact_page_requires_non_conflicting_product_identity():
    ev = [
        excerpt("title", "Model: MODEL-A"), excerpt("other", "Model: MODEL-B"),
        excerpt("material", "Material: Brass"),
    ]
    assert classify(manifest(), ev, candidate("material")).confidence == "Low"


def test_exact_page_identity_does_not_cross_pages_or_source_versions():
    ev = [
        excerpt("title", "Model: MODEL-A"),
        excerpt("page2", "Material: Brass", locator="batchblob:///pdf.pdf#page=2&paragraph=0"),
        excerpt("version2", "Material: Brass").model_copy(update={"source_version": "v2"}),
    ]
    for eid in ["page2", "version2"]:
        assert classify(manifest(), ev, candidate(eid)).confidence == "Low"


@pytest.mark.parametrize("identifier", ["D-42", "ALPHA", "Alpha Series 7"])
def test_matching_row_and_source_identifier_confirm_family(identifier):
    row = vendor(fields=[("Drawing", identifier)])
    ev = [row, excerpt("heading", f"Drawing: {identifier}"), excerpt("material", "Material: Brass")]
    result = classify(manifest(), ev, candidate("material"))
    assert (result.status, result.confidence) == ("family-confirmed", "Medium")
    assert not result.reviewer_questions
    assert {proof.role for proof in result.proofs} >= {"product_identity", "family_reference", "source_identity"}
    assert {proof.evidence_id for proof in result.proofs} >= {row.evidence_id, "heading"}


def test_pdf_exact_row_can_explicitly_name_a_family_in_another_source():
    rows = table([["Part Number", "Family"], ["MODEL-A", "FAM-7"]], source="catalog")
    ev = rows + [excerpt("heading", "FAM-7 family"), excerpt("material", "Material: Brass")]
    assert classify(manifest(), ev, candidate("material")).status == "family-confirmed"


def test_a_labeled_drawing_identifier_in_pdf_cells_can_confirm_the_family():
    ev = [vendor(fields=[("Drawing", "D-42")])]
    ev += table([["Drawing", "Material"], ["D-42", "Brass"]])
    result = classify(manifest(), ev, candidate("pdf-p1-r1-c1"))
    assert (result.status, result.confidence) == ("family-confirmed", "Medium")
    assert any(proof.role == "source_identity" and proof.location.get("column") == 0 for proof in result.proofs)


def test_an_exact_rows_family_field_does_not_name_every_paragraph_on_the_page():
    ev = table([["Part Number", "Family"], ["MODEL-A", "FAM-7"]])
    ev.append(excerpt("generic", "Material: Brass"))
    assert classify(manifest(), ev, candidate("generic")).confidence == "Low"


@pytest.mark.parametrize("wrong", ["D-43", "D-42N", "D42"])
def test_different_drawing_identifier_stays_unconfirmed(wrong):
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", f"Drawing: {wrong}"), excerpt("material", "Material: Brass")]
    result = classify(manifest(), ev, candidate("material"))
    assert result.confidence == "Low"
    question = " ".join(result.reviewer_questions).casefold()
    assert "model-a" in question and "d-42" in question and wrong.casefold() in question
    assert {proof.role for proof in result.proofs} >= {"product_identity", "family_reference", "source_identity"}


def test_matching_reference_in_a_different_products_row_is_not_a_link():
    row = vendor("MODEL-B", fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", "Drawing: D-42"), excerpt("material", "Material: Brass")]
    assert classify(manifest(), ev, candidate("material")).status == "family-unconfirmed"


def test_filename_or_manifest_binding_is_not_identity_evidence():
    ev = [excerpt(
        "generic", "Material: Brass", locator="batchblob:///MODEL-A-D-42.pdf#page=1&paragraph=0",
        qualification="Exact product association; use drawing D-42.",
    )]
    assert classify(manifest(), ev, candidate("generic")).confidence == "Low"


def test_family_anchor_does_not_cross_pages():
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", "Drawing: D-42"),
          excerpt("material", "Brass", locator="batchblob:///pdf.pdf#page=2&paragraph=0")]
    assert classify(manifest(), ev, candidate("material")).confidence == "Low"


@pytest.mark.parametrize("reference", [
    "Do not use drawing D-42", "Drawing D-42 is optional", "Either drawing D-42 or D-43",
])
def test_negative_or_conditional_row_references_cannot_confirm_family(reference):
    row = vendor(fields=[("Description", reference)])
    ev = [row, excerpt("heading", "Drawing: D-42"), excerpt("material", "Material: Brass")]
    assert classify(manifest(), ev, candidate("material")).confidence == "Low"


@pytest.mark.parametrize("heading", ["See drawing D-42", "Drawing D-42 is not applicable"])
def test_source_reference_or_negation_is_not_its_identity(heading):
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", heading), excerpt("material", "Material: Brass")]
    assert classify(manifest(), ev, candidate("material")).confidence == "Low"


def test_family_link_does_not_cover_an_explicit_foreign_product_row():
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", "Drawing: D-42")]
    ev += table([["Part Number", "Material"], ["MODEL-B", "Steel"]])
    assert classify(manifest(), ev, candidate("pdf-p1-r1-c1")).confidence == "Low"


def test_family_link_does_not_cover_a_page_explicitly_for_a_different_product():
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", "Drawing: D-42"), excerpt("product", "Model: MODEL-B"),
          excerpt("material", "Material: Brass")]
    assert classify(manifest(), ev, candidate("material")).confidence == "Low"


def test_standalone_named_drawing_title_can_match_an_explicit_row_reference():
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", "D-42", locator="batchblob:///pdf.pdf#page=1&paragraph=0&role=title"),
          excerpt("material", "Material: Brass")]
    assert classify(manifest(), ev, candidate("material")).confidence == "Medium"


def test_dedicated_page_covers_a_table_without_repeating_the_product_mpn():
    ev = [excerpt("title", "Model: MODEL-A")]
    ev += table([["Component", "Material"], ["Body", "Brass"]])
    assert classify(manifest(), ev, candidate("pdf-p1-r1-c1")).confidence == "High"
    assert classify(manifest(), ev, candidate("pdf-p1-r0-c1")).confidence == "Low"


def test_family_header_alone_is_context_not_a_supported_value():
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", "Drawing: D-42")]
    ev += table([["Part Number", "Material"], ["MODEL-A", "Brass"]])
    result = classify(manifest(), ev, candidate("pdf-p1-r0-c1"))
    assert result.confidence == "Low" and result.reviewer_questions


def test_ford_ocr_selected_column_and_sparse_filtered_rows_preserve_mpn_header():
    ev = table([
        ["VALVE SIZE", "PART NUMBER", ":selected: / SUBMITTED ITEM(S)"],
        ["", "", ""],
        ["3/4\"", "AV11-444W-NL", ""],
    ])
    assert classify(manifest("AV11-444W-NL"), ev, candidate("pdf-p1-r2-c0")).confidence == "High"


@pytest.mark.parametrize("mpn", ["AV11-333W-NL", "AV11-444W-NL", "ZX92-777P-NL"])
def test_ford_style_reference_and_exact_catalog_row_use_source_facts(mpn):
    prefix, _, suffix = mpn.partition("-")
    style = prefix + "-xxx" + suffix[3:]
    row = vendor(mpn, key_column="2nd Item Number", fields=[
        ("Photo ID", style), ("Submittal File", style + ".pdf"),
        ("Submittal ID Path", "https://example.invalid/submittals/" + style + ".pdf"),
    ])
    ev = [row, excerpt("heading", f"Angle Service Valves - ({style} style)"),
          excerpt("features", "FEATURES", locator="batchblob:///pdf.pdf#page=1&paragraph=37&role=sectionHeading"),
          excerpt("family", "Material: Brass")]
    ev += table([["Part Number", "Material"], [mpn, "Brass"]])
    mapping = build_applicability_map(manifest(mpn), ev)
    assert mapping["pdf"].status == "exact"
    assert candidate_applicability(candidate("pdf-p1-r1-c1"), mapping, ev).confidence == "High"
    assert candidate_applicability(candidate("family"), mapping, ev).confidence == "Medium"


def test_ford_header_and_exact_product_row_do_not_invent_a_prefix_alias():
    ev = [excerpt("heading", "Angle Service Valves - (AV11-xxxW-NL style)"),
          excerpt("family", "Material: Brass")]
    ev += table([["Part Number", "Material"], ["AV11-333W-NL", "Brass"]])
    assert classify(manifest("AV11-333W-NL"), ev, candidate("family")).confidence == "Low"


@pytest.mark.parametrize(("mpn", "reference"), [
    ("014255    215N", "REF HS H14250"), ("014255    203N", "SEE HS H14250"),
])
def test_mueller_h14250_is_not_h14255n(mpn, reference):
    row = vendor(mpn, key_column="Part #", fields=[("Descr Mat 1", "   " + reference)])
    ev = [row, excerpt("heading", "H14255N PART NUMBER"), excerpt("material", "Material: Brass")]
    result = classify(manifest(mpn), ev, candidate("material", confidence=0.99))
    assert (result.status, result.confidence) == ("family-unconfirmed", "Low")
    question = " ".join(result.reviewer_questions).upper()
    assert "H14250" in question and "H14255N" in question and mpn in question
    assert classify(manifest(mpn), ev, candidate(row.evidence_id)).confidence == "High"


def test_mueller_variants_are_not_aliased_even_when_the_drawing_matches():
    row = vendor("014255    203N", key_column="Part #", fields=[("Descr Mat 1", "REF HS H14255N")])
    ev = [row, excerpt("heading", "H14255N PART NUMBER"), excerpt("material", "Material: Brass")]
    assert classify(manifest("014255    215N"), ev, candidate("material")).confidence == "Low"


def test_explicit_matching_mueller_reference_would_confirm_without_an_alias():
    row = vendor("014255    215N", key_column="Part #", fields=[("Descr Mat 1", "REF HS H14255N")])
    ev = [row, excerpt("heading", "H14255N PART NUMBER"), excerpt("material", "Material: Brass")]
    assert classify(manifest("014255    215N"), ev, candidate("material")).confidence == "Medium"


@pytest.mark.parametrize(("origin", "expected"), [
    ("literal", "High"), ("derived", "Medium"), ("inferred", "Low"), ("model_generated", "Low"),
])
def test_confidence_comes_from_origin_and_provenance_not_model_score(origin, expected):
    row = vendor()
    c = candidate(row.evidence_id, origin=origin, confidence=1.0)
    result = classify(manifest(), [row], c)
    assert result.confidence == expected
    assert candidate_confidence(c, result) == expected


def test_family_confirmed_inference_cannot_be_high():
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", "Drawing: D-42"), excerpt("material", "Material: Brass")]
    assert classify(manifest(), ev, candidate("material", origin="inferred", confidence=1.0)).confidence == "Low"


def test_normalization_is_medium_and_literal_without_quote_cannot_be_high():
    row = vendor()
    assert classify(manifest(), [row], candidate(row.evidence_id, normalization_rule="case_v1")).confidence == "Medium"
    c = candidate(row.evidence_id).model_copy(update={"supporting_quote": None})
    assert classify(manifest(), [row], c).confidence == "Low"


def test_missing_sources_and_missing_candidate_evidence_have_review_questions():
    mapping = build_applicability_map(manifest(), [])
    assert set(mapping) == {"pdf", "vendor"}
    assert all(source.status == "family-unconfirmed" and source.reviewer_question for source in mapping.values())
    result = candidate_applicability(candidate("missing"), mapping, [])
    assert result.confidence == "Low" and result.reviewer_questions and not result.proofs


def test_generated_answer_does_not_prove_identity():
    row = vendor().model_copy(update={"content_kind": "generated_answer"})
    assert classify(manifest(), [row], candidate(row.evidence_id)).confidence == "Low"


def test_a_good_citation_does_not_hide_a_missing_citation():
    row = vendor()
    assert classify(manifest(), [row], candidate(row.evidence_id, "missing")).confidence == "Low"


def test_mixed_exact_and_unconfirmed_citations_use_lowest_applicability():
    row = vendor()
    ev = [row, excerpt("generic", "Material: Brass")]
    assert classify(manifest(), ev, candidate(row.evidence_id, "generic")).confidence == "Low"


@pytest.mark.parametrize("attribute_ids", [[], ["Manufacturer"]])
def test_attribute_scope_is_not_overridden_by_exact_identity(attribute_ids):
    row = vendor().model_copy(update={"attribute_ids": attribute_ids})
    assert classify(manifest(), [row], candidate(row.evidence_id)).confidence == "Low"


def test_source_map_cannot_be_reused_for_changed_evidence():
    row = vendor()
    mapping = build_applicability_map(manifest(), [row])
    changed = vendor("MODEL-B")
    assert candidate_applicability(candidate(row.evidence_id), mapping, [changed]).confidence == "Low"


def test_family_proof_requires_the_exact_row_to_remain_available_and_unchanged():
    row = vendor(fields=[("Drawing", "D-42")])
    ev = [row, excerpt("heading", "Drawing: D-42"), excerpt("material", "Material: Brass")]
    mapping = build_applicability_map(manifest(), ev)
    assert candidate_applicability(candidate("material"), mapping, ev[1:]).confidence == "Low"
    changed = [row.model_copy(update={"text": row.text.replace("D-42", "D-43")}), *ev[1:]]
    assert candidate_applicability(candidate("material"), mapping, changed).confidence == "Low"


def test_functions_are_pure_and_results_are_json_serializable():
    m = manifest()
    ev = [vendor(), *table([["Part Number", "Material"], ["MODEL-A", "Brass"]])]
    before = [entry.model_dump_json() for entry in ev]
    m_before = m.model_dump_json()
    mapping = build_applicability_map(m, ev)
    c = candidate("pdf-p1-r1-c1")
    c_before = c.model_dump_json()
    result = candidate_applicability(c, mapping, ev)
    assert json.loads(result.model_dump_json())["confidence"] == "High"
    assert json.loads(mapping["pdf"].model_dump_json())["status"] == "exact"
    assert before == [entry.model_dump_json() for entry in ev]
    assert m_before == m.model_dump_json() and c_before == c.model_dump_json()


def test_duplicate_evidence_ids_are_rejected():
    row = vendor()
    with pytest.raises(ValueError, match="unique"):
        build_applicability_map(manifest(), [row, row])
    with pytest.raises(ValueError, match="unique"):
        candidate_applicability(candidate(row.evidence_id), {}, [row, row])
