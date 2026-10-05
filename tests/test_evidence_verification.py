"""Synthetic reproductions of verifier comparisons, not recovered provider output."""

import json
from datetime import datetime, timezone

import pytest

from backend.evidence_verification import match_text, windows
from backend.extract import run_enrichment, validate_response
from backend.models.enrichment import (
    AttributeDefinition, Candidate, Evidence, ExtractionResponse, LiveBundle, Manifest,
    OfflineSource, ProductKey,
)
from backend.response_validation import ResponseValidationError, map_citations


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Verifier reproductions must not contact services")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def excerpt(text, index=0, *, page=1, tier="internal_pdf", version="v1", source="synthetic", location=None):
    return Evidence(
        evidence_id=f"{source}:{version}:{index}", source_id=source, source_version=version,
        source_locator=f"{source}#page={page}&" + (location or f"paragraph={index}"),
        source_tier=tier, content_kind="source_excerpt", text=text,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def row(*values):
    return excerpt(json.dumps({"sheet": "Synthetic", "row": 2, "cells": [
        {"cell": f"{chr(65 + index)}2", "column": f"Column {index}", "value": value}
        for index, value in enumerate(values)
    ]}), tier="vendor_table")


def response(value, quote, evidence, *, attribute="Size", kind="string", unit=None):
    manifest = Manifest(
        product=ProductKey(item_id="reproduction", vendor="Synthetic", mpn="TEST", hierarchy_node="Valve"),
        attributes=[AttributeDefinition(attribute_id=attribute, description=attribute, value_type=kind, unit=unit)],
        source_ids=list(dict.fromkeys(entry.source_id for entry in evidence)),
    )
    candidates = ExtractionResponse(candidates=[Candidate(
        attribute_id=attribute, value=value, unit=unit,
        evidence_ids=[entry.evidence_id for entry in evidence],
        supporting_quote=quote, qualification="REPRODUCTION ONLY: synthetic evidence.",
    )])
    return candidates, manifest


@pytest.mark.parametrize("source,quote,value", [
    ("Body Material: BRASS.", "body material brass", "brass"),
    ("Low-lead\n  brass body", "LOW LEAD brass body", "brass"),
    ("Caf\u00e9 finish", "Cafe\u0301 FINISH", "Caf\u00e9"),
    ("Full\u2014port valve", "full port valve", "full port"),
    ("Bronze\tBODY", "bronze body", "Bronze"),
])
def test_normalized_quotes_are_grounded_and_audited(source, quote, value):
    evidence = [excerpt(source)]
    candidates, manifest = response(value, quote, evidence)
    assert quote not in source
    check = validate_response(candidates, manifest, evidence)[0]
    assert check.quote.method == "normalized" and check.quote.normalization
    assert check.value.evidence_ids == [evidence[0].evidence_id]
    assert len(check.quote.normalized_sha256) == 64


def test_quote_spans_only_cited_adjacent_fragments_and_is_order_independent():
    evidence = [excerpt("BODY MATERIAL:"), excerpt("Brass.", 1)]
    candidates, manifest = response("brass", "body material brass", evidence)
    candidates.candidates[0].evidence_ids.reverse()
    check = validate_response(candidates, manifest, evidence)[0]
    assert check.quote.method == "adjacent_fragments"
    assert check.quote.evidence_ids == [entry.evidence_id for entry in evidence]
    candidates.candidates[0].evidence_ids.pop()
    with pytest.raises(ResponseValidationError, match="normalized match"):
        validate_response(candidates, manifest, evidence)


@pytest.mark.parametrize("change", ["page", "source", "version", "gap", "table"])
def test_quote_cannot_bridge_other_regions_or_uncited_gaps(change):
    first = excerpt("BODY MATERIAL:")
    second = excerpt("brass", 1, **{
        "page": {"page": 2}, "source": {"source": "other"}, "version": {"version": "v2"},
        "gap": {"location": "paragraph=2"}, "table": {"location": "table=0&row=0&column=0"},
    }[change])
    candidates, manifest = response("brass", "body material brass", [first, second])
    with pytest.raises(ResponseValidationError, match="normalized match"):
        validate_response(candidates, manifest, [first, second])


def test_pdf_table_cells_require_every_intervening_citation():
    evidence = [excerpt(text, index, location=f"table=0&row=0&column={index}")
                for index, text in enumerate(("Inlet", "size", "5/8"))]
    candidates, manifest = response("5/8", "Inlet size 5/8", evidence)
    assert validate_response(candidates, manifest, evidence)[0].quote.method == "adjacent_fragments"
    candidates.candidates[0].evidence_ids = [evidence[0].evidence_id, evidence[2].evidence_id]
    with pytest.raises(ResponseValidationError):
        validate_response(candidates, manifest, evidence)


@pytest.mark.parametrize("cell,value,quote", [
    ('5/8"', "5/8 in", "5/8 inches"),
    ("0.625 in", '5/8"', "5/8 in"),
    ("\u215d\u2033", "0.625 in", "5/8 in"),
    ('1 1/2"', "1.50 in", "1.5 inch"),
    ("1\u00bd in", "1.5 in", "1.5 in"),
    ("1,000.00", "1000", "1000"),
    ("-0.50", "-.5", "-0.5"),
    ('5 / 8"', "5/8 in", "5/8 inches"),
    ("- 0.50", "-.5", "-0.5"),
])
def test_vendor_value_and_quote_normalize_cells_not_serialized_json(cell, value, quote):
    evidence = [row(cell)]
    candidates, manifest = response(value, quote, evidence)
    check = validate_response(candidates, manifest, evidence)[0]
    assert check.quote.method == "vendor_cells" and check.value.method == "vendor_cells"
    assert check.value.cells == ["A2"]
    assert set(check.value.normalization) & {"inch_unit_alias", "numeric_format", "unicode_nfkc"}


def test_vendor_quote_can_span_cells_but_value_cannot_come_from_wrapper():
    evidence = [row("Inlet", '5/8"', "brass")]
    candidates, manifest = response("5/8 in", "inlet 5/8 inches BRASS", evidence)
    check = validate_response(candidates, manifest, evidence)[0]
    assert check.quote.cells == ["A2", "B2", "C2"] and check.value.cells == ["B2"]
    for wrapper_value in ("Synthetic", "A2", "Column 0", "2", "cells"):
        candidates.candidates[0].value = wrapper_value
        with pytest.raises(ResponseValidationError) as caught:
            validate_response(candidates, manifest, evidence)
        assert caught.value.issues[0].field_path == "candidates[0].value"


@pytest.mark.parametrize("source,quote,value", [
    ("5/8 in", "5 8 in", "5/8 in"),
    ("0.5 in", "5 in", "5 in"),
    ("-5 PSI", "5 PSI", "5"),
    ("- 5 PSI", "5 PSI", "5"),
    ("500 PSI", "50 PSI", "50"),
    ("not brass", "brass", "steel"),
    ("AB100", "AB100", "100"),
    ("00123", "00123", "123"),
    ("body material brass", "body material steel", "steel"),
    ("5/8 in", "5/8 in", "15.875 mm"),
    ('5/8"', "5/8'", "5/8'"),
    ("5/8'", '5/8"', '5/8"'),
])
def test_no_fuzzy_numeric_conversion_or_unsupported_value(source, quote, value):
    evidence = [row(source)]
    candidates, manifest = response(value, quote, evidence)
    with pytest.raises(ResponseValidationError):
        validate_response(candidates, manifest, evidence)


def test_scope_units_and_boolean_safeguards_survive_normalization():
    for value, quote, name, kind, unit in [
        ("EPDM", "epdm o ring", "Primary Material", "string", None),
        (100, "100 PSI working pressure", "Maximum Pressure", "number", "PSI"),
        (False, "No lead in potable components", "Lead-Free", "boolean", None),
    ]:
        evidence = [excerpt(quote.upper())]
        candidates, manifest = response(value, quote, evidence, attribute=name, kind=kind, unit=unit)
        with pytest.raises(ResponseValidationError):
            validate_response(candidates, manifest, evidence)
    evidence = [excerpt("Lead-Free: YES")]
    candidates, manifest = response(True, "lead free yes", evidence, attribute="Lead-Free", kind="boolean")
    assert validate_response(candidates, manifest, evidence)
    evidence[0].attribute_ids = ["Unrelated"]
    with pytest.raises(ResponseValidationError, match="applicability"):
        validate_response(candidates, manifest, evidence)


def test_unknown_and_uncited_values_are_still_rejected():
    evidence = [excerpt("brass")]
    candidates, manifest = response("brass", "BRASS", evidence)
    candidates.candidates[0].evidence_ids = ["E999"]
    with pytest.raises(ResponseValidationError):
        map_citations(candidates, {"E1": evidence[0].evidence_id})
    with pytest.raises(ResponseValidationError):
        validate_response(candidates, manifest, evidence)


def test_known_header_citations_do_not_establish_an_uncited_product_fact():
    evidence = [
        excerpt("No.", 0, location="table=0&row=0&column=0"),
        excerpt("DESCRIPTION", 1, location="table=0&row=0&column=1"),
        excerpt("brass body", 2, location="table=0&row=1&column=1"),
    ]
    candidates, manifest = response("brass", "BRASS BODY", evidence[:2])
    with pytest.raises(ResponseValidationError, match="no normalized match"):
        validate_response(candidates, manifest, evidence)
    candidates.candidates[0].evidence_ids = [evidence[2].evidence_id]
    assert validate_response(candidates, manifest, evidence)[0].value.evidence_ids == [evidence[2].evidence_id]


def test_vendor_invalid_cell_records_fail_explicitly():
    for text in ('{"row":', '{"row":2,"sheet":"test","cells":[]}',
                 '{"row":2,"sheet":"test","cells":[{"cell":"A3","value":"brass"}]}'):
        evidence = [excerpt(text, tier="vendor_table")]
        candidates, manifest = response("brass", "BRASS", evidence)
        with pytest.raises(ResponseValidationError) as caught:
            validate_response(candidates, manifest, evidence)
        assert caught.value.issues[0].field_path == "candidates[0].evidence_ids"


def test_missing_table_positions_and_vendor_row_boundaries_cannot_be_joined():
    evidence = [
        excerpt("Inlet", 0, location="table=0&row=0&column=0"),
        excerpt("brass", 1, location="table=0&row=0&column=2"),
    ]
    assert match_text("Inlet brass", windows(evidence, {part.evidence_id for part in evidence})) is None
    first = row("Inlet")
    second = row("brass").model_copy(update={"evidence_id": "other"})
    data = json.loads(second.text)
    data.update(row=3, cells=[{"cell": "A3", "value": "brass"}])
    second.text = json.dumps(data)
    assert match_text("Inlet brass", windows([first, second], {first.evidence_id, second.evidence_id})) is None


def test_verification_is_server_computed_not_a_model_response_field():
    assert "verification" not in json.dumps(ExtractionResponse.model_json_schema())
    evidence = [row('5/8"')]
    candidates, manifest = response("5/8 in", "5/8 inches", evidence)

    class Reproduction:
        def complete_structured(self, system, user, schema):
            return candidates

    result = run_enrichment(
        LiveBundle(execution_mode="live_inference", manifest=manifest, sources=[OfflineSource(
            source_id="synthetic", product=manifest.product, source_tier="vendor_table", excerpts=evidence,
        )]), execution_mode="live_inference", completion=Reproduction(), qualified=True,
    )
    assert result.attributes[0].status == "proposed"
    assert result.attributes[0].verification[0].value.cells == ["A2"]
    assert result.attributes[0].review is None
    assert "REPRODUCTION ONLY" in result.attributes[0].candidates[0].qualification
    assert not result.validation_diagnostics


def test_verification_survives_production_export_without_model_schema_changes(tmp_path):
    from backend.batch import BatchService
    from backend.batch_store import SQLiteStore, write_json
    from backend.workbooks import read_workbook, write_workbook

    service = BatchService(SQLiteStore(tmp_path / "store"))
    write_json(service.store, "configuration/sources.json", {"sources": []})
    record = service.intake(
        write_workbook({"Products": [
            ["PIMITEM Number", "Vendor Name", "MPN", "Hierarchy Node", "Attributes to Fill"],
            ["reproduction", "Synthetic", "TEST", "Valve", "definitions.xlsx"],
        ]}),
        write_workbook({"Definitions": [
            ["node", "potential_attribute_name", "potential_attribute_data_type"],
            ["Valve", "Size", "String"],
        ]}), "definitions.xlsx", "owner",
    )
    evidence = [row('5/8"')]
    candidates, manifest = response("5/8 in", "5/8 inches", evidence)

    class Reproduction:
        def complete_structured(self, system, user, schema):
            return candidates

    result = run_enrichment(
        LiveBundle(execution_mode="live_inference", manifest=manifest, sources=[OfflineSource(
            source_id="synthetic", product=manifest.product, source_tier="vendor_table", excerpts=evidence,
        )]), execution_mode="live_inference", completion=Reproduction(), qualified=True,
    )
    service.submit(record["id"], "owner", "11111111-1111-4111-8111-111111111111", "evidence_only", False)
    path = f"items/{record['id']}/row-2.json"
    write_json(service.store, path, {"state": "completed"})
    write_json(service.store, f"results/{record['id']}/row-2.json", result.model_dump(mode="json"))
    sheets = read_workbook(service.export(record["id"], "owner"))
    expected = result.attributes[0].verification[0].model_dump(mode="json")
    assert json.loads(sheets["Results"][0]["Evidence verification JSON"]) == expected
    assert sheets["Results"][0]["Review status"] == "pending"
    assert "REPRODUCTION ONLY" in sheets["Results"][0]["Qualifications"]
