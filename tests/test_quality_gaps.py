import json
from datetime import datetime, timezone

import pytest

from backend.core.docintel import ParsedDocument, ParsedParagraph, ParsedTable
from backend.evidence_verification import normalized
from backend.models.enrichment import AttributeDefinition, Evidence, Manifest, ProductKey
from backend.quality_pipeline import QualityProposal, ground_candidate, product_packet
from backend.quality_worker import CachedEvidenceLoader, scope_document
from backend.reviewer_workbook import _proposed_display

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def vendor():
    return Evidence(
        evidence_id="vendor", source_id="vendor-source", source_tier="vendor_table",
        source_locator="https://example.com/vendor.xlsx#sheet=Vendor&row=2201", source_version="v1",
        content_kind="source_excerpt", observed_at=NOW,
        text=json.dumps({"sheet": "Vendor", "row": 2201, "cells": [
            {"cell": "C2201", "column": "Descr 1", "value": '5/8X3/4" ANGLE METER STOP LLB'},
            {"cell": "Q2201", "column": "DescrGen1", "value": '3/4" COPPER SERVICE INLET  LLB'},
            {"cell": "R2201", "column": "DescrGen2", "value": '5/8" SADDLE METER SWIVEL NUT OUTLET'},
        ]}),
    )


@pytest.mark.parametrize("attribute,value,quote,cell", [
    ("Compatible Meter Size", '5/8"', '5/8\\" SADDLE METER SWIVEL NUT OUTLET', "R2201"),
    ("Outlet Size", '5/8"', '5/8\\" SADDLE METER SWIVEL NUT OUTLET', "R2201"),
    ("Nominal Size", '5/8X3/4"', '5/8X3/4\\" ANGLE METER STOP LLB', "C2201"),
])
def test_retained_row_2201_escaped_unit_failures(attribute, value, quote, cell):
    candidate = ground_candidate(QualityProposal(
        attribute_id=attribute, value=value, supporting_quote=quote, evidence_ids=["vendor"],
    ), AttributeDefinition(attribute_id=attribute, description="", value_type="string"), [vendor()])
    assert candidate.grounding["quote"]["cells"] == [cell]
    assert "json_escaped_unit_mark" in candidate.grounding["quote"]["normalization"]
    assert candidate.supporting_quote == quote


def test_size_normalization_preserves_alternatives_units_and_all_numbers():
    assert normalized('5/8X3/4\\"', vendor=True)[0] == normalized("5/8 x 3/4 in", vendor=True)[0]
    assert normalized('5/8  X  3/4 OR 3/4IN', vendor=True)[0] == normalized("5/8 x 3/4 or 3/4 in", vendor=True)[0]
    assert normalized("5/8 or 3/4", vendor=True)[0] != normalized("5/8 and 3/4", vendor=True)[0]
    assert normalized('5/8"', vendor=True)[0] != normalized("5/8", vendor=True)[0]


def test_mixed_title_block_survives_dimension_filter_only_for_manufacturer(monkeypatch):
    product = ProductKey(item_id="P1", vendor="Mueller", mpn="014255 203N", hierarchy_node="Valves")
    title = "CONFIGURATION FOR 5/8x3/4x1 DRFTR JG CHKR Mueller Co. ENGR MGR THIRD ANGLE PROJECTION"
    document = ParsedDocument(
        source="batchblob:///drawing.pdf", cache_key="sha256:" + "a" * 64, parsed_at=NOW,
        paragraphs=[ParsedParagraph(text=title, page_number=1)],
        tables=[ParsedTable(page_number=1, row_count=3, column_count=1,
                            cells=[["METER CONNX SIZE"], [title], ["H14255N PART NUMBER"]])],
    )
    binding = {"source_id": "drawing", "kind": "blob", "format": "pdf", "blob": "drawing.pdf",
               "sha256": "a" * 64, "products": [product.model_dump()]}
    scoped = scope_document(document, product, binding)
    assert scoped.paragraphs[0].text == title
    assert scoped.paragraphs[0].role == "manufacturerTitleBlock"
    assert scoped.tables[0].cells[1] == [""]
    definitions = [AttributeDefinition(attribute_id=name, description="", value_type="string")
                   for name in ("Manufacturer", "Inlet Size")]
    manifest = Manifest(product=product, attributes=definitions)
    loader = CachedEvidenceLoader(None)
    monkeypatch.setattr(loader, "cached_document", lambda _: document)
    evidence, _, _ = loader.load({"manifest": manifest.model_dump(mode="json"), "sources": [binding]})
    header = next(e for e in evidence if title == e.text)
    assert header.attribute_ids == ["Manufacturer"]
    candidate = ground_candidate(QualityProposal(
        attribute_id="Manufacturer", value="Mueller Co.", supporting_quote="Mueller Co.",
        evidence_ids=[header.evidence_id],
    ), definitions[0], evidence)
    assert candidate.value == "Mueller Co."
    packet = product_packet(manifest, evidence, "internal_pdf", ["Manufacturer"])
    title_entry = next(entry for entry in packet["evidence"] if entry["evidence_id"] == header.evidence_id)
    assert title_entry["presentation"]["kind"] == "title_block"
    assert title_entry["presentation"]["identity_only"] is True
    assert title_entry["presentation"]["context_only"] is False
    with pytest.raises(ValueError, match="not applicable"):
        ground_candidate(QualityProposal(
            attribute_id="Inlet Size", value="1", supporting_quote=title,
            evidence_ids=[header.evidence_id],
        ), definitions[1], evidence)


def pdf(text, key="p1", paragraph=1):
    return Evidence(
        evidence_id=key, source_id="catalog", source_tier="internal_pdf",
        source_locator=f"https://example.com/catalog.pdf#page=1&paragraph={paragraph}",
        source_version="v1", content_kind="source_excerpt", text=text, observed_at=NOW,
        qualification="Exact product source association.",
    )


@pytest.mark.parametrize("text", [
    '5/8" SADDLE METER SWIVEL NUT OUTLET',
    "FEMALE IRON PIPE THREAD INLET BY FEMALE IRON PIPE THREAD OUTLET",
    "Outlet: FIP",
])
@pytest.mark.parametrize("origin", ["literal", "derived", "inferred"])
def test_explicit_nonflanged_outlet_mechanism_is_always_review_only(text, origin):
    candidate = ground_candidate(QualityProposal(
        attribute_id="Flanged Outlet", value=False, supporting_quote=text, evidence_ids=["p1"], origin=origin,
    ), AttributeDefinition(attribute_id="Flanged Outlet", description="", value_type="boolean"), [pdf(text)])
    assert candidate.value is False and candidate.origin == "inferred"
    assert candidate.inference_rule == "nonflanged_outlet_mechanism_v1"
    assert "requires review" in candidate.qualification.lower()
    assert "not from silence" in candidate.justification


@pytest.mark.parametrize("text", [
    "FIP inlet by flanged outlet", "Female iron pipe thread inlet",
    "Optional FIP outlet", "FIP outlet or flanged outlet",
    "FIP outlet with flange adapter", "No FIP outlet", "Brass angle valve",
])
def test_flange_absence_is_not_inferred_from_silence_other_roles_or_options(text):
    with pytest.raises(ValueError):
        ground_candidate(QualityProposal(
            attribute_id="Flanged Outlet", value=False, supporting_quote=text, evidence_ids=["p1"],
            origin="inferred", justification="A guess.",
        ), AttributeDefinition(attribute_id="Flanged Outlet", description="", value_type="boolean"), [pdf(text)])


@pytest.mark.parametrize("text,value", [
    ("Female Iron Pipe Thread", "Iron pipe"), ("Male Iron Pipe Thread", "Iron pipe"),
    ('3/4" COPPER SERVICE INLET  LLB', "Copper"),
    ("Flare connection", "Copper"), ("Compression inlet", "Copper"),
    ("Flare", "Copper"), ("Compression", "Copper"),
])
def test_connection_material_derivation_is_named_and_justified(text, value):
    candidate = ground_candidate(QualityProposal(
        attribute_id="Pipe / Tubing Compatibility", value=value, supporting_quote=text, evidence_ids=["p1"],
    ), AttributeDefinition(attribute_id="Pipe / Tubing Compatibility", description="", value_type="string"), [pdf(text)])
    assert candidate.origin == "derived" and candidate.normalization_rule == "connection_material_v1"
    assert f"-> {value}" in candidate.justification


@pytest.mark.parametrize("text", ["PEX compression inlet", "Not a copper service inlet", "Compression stem packing"])
def test_connection_derivation_rejects_material_contradictions_and_component_roles(text):
    with pytest.raises(ValueError):
        ground_candidate(QualityProposal(
            attribute_id="Pipe / Tubing Compatibility", value="Copper", supporting_quote=text, evidence_ids=["p1"],
            origin="derived", normalization_rule="connection_material_v1",
        ), AttributeDefinition(attribute_id="Pipe / Tubing Compatibility", description="", value_type="string"), [pdf(text)])


BRASS = ". All brass that comes in contact with potable water conforms to AWWA Standard C800 (ASTM B584, UNS C89833)"
NL = '. The product has the letters "NL" cast into the main body for lead-free identification'


def test_ford_brass_and_main_body_nl_paragraphs_support_derived_no_lead_brass():
    candidate = ground_candidate(QualityProposal(
        attribute_id="Primary Material", value="No-lead brass", supporting_quote=BRASS + "; " + NL,
        evidence_ids=["p1", "p2"],
    ), AttributeDefinition(attribute_id="Primary Material", description="", value_type="string"),
        [pdf(BRASS, paragraph=38), pdf(NL, key="p2", paragraph=39)])
    assert candidate.origin == "derived"
    assert candidate.normalization_rule == "brass_plus_nl_identification_v1"
    assert set(candidate.grounding["quote"]["evidence_ids"]) == {"p1", "p2"}
    assert candidate.justification


@pytest.mark.parametrize("text", [BRASS, NL, BRASS + " NL is an optional component mark."])
def test_no_lead_brass_requires_both_product_material_and_body_identification(text):
    with pytest.raises(ValueError):
        ground_candidate(QualityProposal(
            attribute_id="Primary Material", value="No-lead brass", supporting_quote=text, evidence_ids=["p1"],
            origin="derived", normalization_rule="brass_plus_nl_identification_v1",
        ), AttributeDefinition(attribute_id="Primary Material", description="", value_type="string"), [pdf(text)])


@pytest.mark.parametrize("value,vendor_text,expected", [
    (True, False, "Yes"), (False, True, "No"),
    ("LOW LEAD BRASS", True, "Low lead brass"),
    ("FEMALE IRON PIPE THREAD", True, "Female iron pipe thread"),
    ("NSF61", True, "NSF 61"), ("NSF61", False, "NSF 61"),
    ("EPDM", True, "EPDM"), ("LOW LEAD BRASS", False, "LOW LEAD BRASS"),
])
def test_review_value_formatting_is_presentation_only(value, vendor_text, expected):
    assert _proposed_display(value, vendor=vendor_text) == expected
