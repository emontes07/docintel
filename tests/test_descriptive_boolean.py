"""Offline synthetic tests for the sole review-only descriptive Boolean rule."""

import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.batch import BatchService
from backend.batch_store import Missing
from backend.extract import run_enrichment, run_offline, validate_response
from backend.models.enrichment import (
    AttributeDefinition, AttributeResult, Candidate, DESCRIPTIVE_BOOLEAN_FLAG,
    EnrichmentResult, Evidence, EvidenceVerification, ExtractionResponse,
    LEAD_FREE_DESCRIPTION_RULE, LiveBundle, Manifest, OfflineBundle, OfflineSource, ProductKey,
)
from backend.response_validation import ResponseValidationError
from backend.workbooks import read_workbook


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Descriptive Boolean tests must remain offline")

    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.setattr("socket.getaddrinfo", blocked)
    monkeypatch.setattr("backend.extract.LLMClient", blocked)


def evidence(text, index=0, **overrides):
    return Evidence(
        evidence_id=f"synthetic:v1:{index}", source_id="synthetic", source_version="v1",
        source_locator=f"synthetic.pdf#page=1&paragraph={index}",
        source_tier="internal_pdf", content_kind="source_excerpt", text=text,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc), **overrides,
    )


def proposal(text, *, quote=None, attribute="Lead-Free", value=True):
    excerpts = [evidence(text)]
    manifest = Manifest(
        product=ProductKey(item_id="fixture", vendor="Synthetic", mpn="TEST-1", hierarchy_node="Valve"),
        attributes=[AttributeDefinition(attribute_id=attribute, description=attribute, value_type="boolean")],
        source_ids=["synthetic"],
    )
    response = ExtractionResponse(candidates=[Candidate(
        attribute_id=attribute, value=value, evidence_ids=[excerpts[0].evidence_id],
        supporting_quote=text if quote is None else quote,
        qualification="Synthetic exact-product source only.", confidence=0.9,
    )])
    return response, manifest, excerpts


def offline_result(text="Low-lead brass angle valve"):
    response, manifest, excerpts = proposal(text)
    return run_offline(OfflineBundle(
        manifest=manifest, sources=[OfflineSource(
            source_id="synthetic", product=manifest.product, excerpts=excerpts,
        )], generated_response=response,
    ))


@pytest.mark.parametrize("source,quote", [
    ("Low-lead brass angle valve", "low lead brass angle valve"),
    ("LEAD-FREE brass valve", "lead free brass valve"),
    ("No-lead bronze valve", "NO LEAD bronze valve"),
    ("LOW\u2011LEAD brass ANGLE valve", "low lead brass angle valve"),
    ("This valve is lead\u2014free.", "this valve is lead free"),
    ("Product: lead free", "product lead free"),
    ("Low-lead\nbrass angle valve", "Low lead brass angle valve"),
    ("Low-lead brass angle valve", "Angle valve of low-lead brass"),
])
def test_explicit_description_is_a_flagged_review_inference_not_literal_evidence(source, quote):
    response, manifest, excerpts = proposal(source, quote=quote)
    check = validate_response(response, manifest, excerpts)[0]
    candidate = response.candidates[0]
    assert candidate.value is True
    assert candidate.evidence_basis == check.evidence_basis == "inferred_from_description"
    assert candidate.inference_rule == check.inference_rule == LEAD_FREE_DESCRIPTION_RULE
    assert candidate.qualification.startswith(DESCRIPTIVE_BOOLEAN_FLAG)
    assert "not literal Boolean evidence or certification" in candidate.qualification
    assert candidate.confidence is None
    assert candidate.supporting_quote == quote
    assert check.quote.evidence_ids == [excerpts[0].evidence_id]
    assert check.value is None and check.unit is None


@pytest.mark.parametrize("attribute", [
    "Lead-Free", "LEAD FREE", "lead\u2011free", "Lead-Free (No-Lead)", "LEAD FREE (NO LEAD)",
])
def test_rule_accepts_only_case_and_hyphen_equivalent_attribute_names(attribute):
    response, manifest, excerpts = proposal("Low-lead brass valve", attribute=attribute)
    assert validate_response(response, manifest, excerpts)[0].value is None


@pytest.mark.parametrize("value,answer", [(True, "Yes"), (True, "true"), (False, "No"), (False, "FALSE")])
def test_literal_labeled_booleans_keep_the_existing_value_verification(value, answer):
    response, manifest, excerpts = proposal(f"Lead-Free: {answer}", value=value)
    check = validate_response(response, manifest, excerpts)[0]
    assert check.value is not None
    assert check.evidence_basis == response.candidates[0].evidence_basis == "literal"
    assert check.inference_rule is response.candidates[0].inference_rule is None
    assert DESCRIPTIVE_BOOLEAN_FLAG not in response.candidates[0].qualification


@pytest.mark.parametrize("text,value", [
    ("Yes", True), ("True", True), ("No", False), ("False", False),
    ("Low-lead brass valve", False), ("No lead in potable components", False),
    ("Lead-Free", True), ("Lead content 0.25 percent", True),
    ("Lead-Free: unavailable", False), ("This valve contains lead", False),
])
def test_unlabeled_or_unsupported_booleans_remain_unresolved(text, value):
    response, manifest, excerpts = proposal(text, value=value)
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


@pytest.mark.parametrize("text,quote", [
    ("Not a low-lead brass valve", "low-lead brass valve"),
    ("This is not a lead-free valve", "lead-free valve"),
    ("Non lead-free valve", "lead-free valve"),
    ("No low-lead valve is supplied", "low-lead valve"),
    ("No lead-free valve is supplied", "lead-free valve"),
    ("Low-lead valve or standard valve", "Low-lead valve"),
    ("Low-lead valve available as an option", "Low-lead valve"),
    ("Low-lead valve sold separately", "Low-lead valve"),
    ("Use a low-lead brass valve", "low-lead brass valve"),
    ("Another product is lead-free", "product is lead-free"),
    ("Includes a low-lead valve", "low-lead valve"),
    ("This valve is lead-free compatible", "This valve is lead-free"),
    ("Low-lead valve only on alternative variants", "Low-lead valve"),
    ("A low-lead valve is required", "low-lead valve"),
    ("Example: low-lead brass valve", "low-lead brass valve"),
    ("Low-lead brass body", "Low-lead brass body"),
    ("No-lead valve stem", "No-lead valve"),
    ("Lead-free valve coating", "Lead-free valve"),
    ("A lead-free valve replacement kit", "lead-free valve"),
    ("Lead-free valve family", "Lead-free valve"),
    ("NSF certified lead-free valve", "lead-free valve"),
    ("This valve meets lead-free standards", "This valve meets lead-free standards"),
    ("Low-lead brass stem in angle valve", "Low-lead brass angle valve"),
])
def test_negation_alternatives_components_and_certification_cannot_be_cropped_away(text, quote):
    response, manifest, excerpts = proposal(text, quote=quote)
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


@pytest.mark.parametrize("attribute", [
    "Lead-Free Certification", "NSF Certified", "Potable Water", "Lead", "Low Lead", "Certified Lead-Free",
])
def test_description_never_establishes_another_attribute_or_certification(attribute):
    response, manifest, excerpts = proposal("Low-lead brass valve", attribute=attribute)
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


@pytest.mark.parametrize("failure", ["unknown", "missing", "generated", "scope", "quote", "unit", "allowed"])
def test_citations_quote_applicability_units_and_allowed_values_remain_mandatory(failure):
    response, manifest, excerpts = proposal("Low-lead brass valve")
    candidate = response.candidates[0]
    if failure == "unknown":
        candidate.evidence_ids = ["uncited"]
    elif failure == "missing":
        with pytest.raises(ValidationError):
            Candidate.model_validate({**candidate.model_dump(), "evidence_ids": []})
        return
    elif failure == "generated":
        excerpts[0].content_kind = "generated_answer"
    elif failure == "scope":
        excerpts[0].attribute_ids = ["Primary Material"]
    elif failure == "quote":
        candidate.supporting_quote = None
    elif failure == "unit":
        manifest.attributes[0].unit = "percent"
    elif failure == "allowed":
        manifest.attributes[0].allowed_values = [False]
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


@pytest.mark.parametrize("value", [False, True])
def test_existing_values_and_uncited_literal_answers_are_never_replaced(value):
    response, manifest, excerpts = proposal("Low-lead brass valve")
    manifest.existing_values["Lead-Free"] = value
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)
    manifest.existing_values.clear()
    excerpts.append(evidence(f"Lead-Free: {'Yes' if value else 'No'}", 1))
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


@pytest.mark.parametrize("answer", ["Yes", "No", "True", "False"])
def test_an_uncited_literal_vendor_column_answer_prevents_descriptive_inference(answer):
    response, manifest, excerpts = proposal("Low-lead brass valve")
    literal = evidence(json.dumps({
        "sheet": "Fixture", "row": 2,
        "cells": [{"cell": "A2", "column": "Lead-Free", "value": answer}],
    }), 1)
    literal.source_tier = "vendor_table"
    excerpts.append(literal)
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


def test_adjacent_cited_fragments_can_support_description_but_not_an_uncited_fragment():
    response, manifest, _ = proposal("Low-lead brass angle valve")
    excerpts = [evidence("Low-lead brass"), evidence("angle valve", 1)]
    response.candidates[0].evidence_ids = [entry.evidence_id for entry in excerpts]
    assert validate_response(response, manifest, excerpts)[0].quote.method == "adjacent_fragments"
    response.candidates[0].evidence_ids.pop()
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


@pytest.mark.parametrize("qualification", [
    "Family scope only", "Alternative variant", "Component material description",
])
def test_source_applicability_qualification_cannot_be_ignored(qualification):
    response, manifest, excerpts = proposal("Low-lead brass valve")
    excerpts[0].qualification = qualification
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


def test_explicit_variant_exclusion_and_retrieval_disclaimers_do_not_negate_product_description():
    response, manifest, excerpts = proposal("Low-lead brass valve")
    excerpts[0].qualification = (
        "Exact part-number row in the explicitly associated vendor workbook; other variants excluded. "
        "Discovery passage was not used as product evidence."
    )
    assert validate_response(response, manifest, excerpts)[0].evidence_basis == "inferred_from_description"


def test_vendor_description_is_grounded_in_cell_values_not_serialized_metadata():
    response, manifest, excerpts = proposal("Low-lead brass valve")
    excerpts[0].source_tier = "vendor_table"
    excerpts[0].text = json.dumps({
        "sheet": "Fixture", "row": 2,
        "cells": [{"cell": "A2", "column": "Description", "value": "Low-lead brass valve"}],
    })
    check = validate_response(response, manifest, excerpts)[0]
    assert check.quote.method == "vendor_cells" and check.quote.cells == ["A2"]
    assert check.value is None
    excerpts[0].text = json.dumps({
        "sheet": "Low-lead brass valve", "row": 2,
        "cells": [{"cell": "A2", "column": "Description", "value": "Synthetic valve"}],
    })
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


def vendor_description_proposal(
    value="Synthetic service inlet LOW LEAD BRASS", *, column="DescrGen1",
    attribute="Lead-Free (No-Lead)", extra_cells=None,
):
    response, manifest, excerpts = proposal("LOW LEAD BRASS", attribute=attribute)
    excerpts[0].source_tier = "vendor_table"
    excerpts[0].text = json.dumps({
        "sheet": "Fixture", "row": 2,
        "cells": [
            {"cell": "A2", "column": column, "value": value},
            *(extra_cells or []),
        ],
    })
    return response, manifest, excerpts


@pytest.mark.parametrize("column", ["Description", "Product Description", "General Description", "DescrGen1"])
@pytest.mark.parametrize("attribute", ["Lead-Free", "Lead-Free (No-Lead)"])
def test_short_phrase_in_exact_product_vendor_description_is_review_only(column, attribute):
    response, manifest, excerpts = vendor_description_proposal(
        column=column, attribute=attribute, extra_cells=[
            {"cell": "B2", "column": "Outlet Size", "value": "10 or 12 units"},
            {"cell": "C2", "column": "Feature", "value": "Drilled for wire seal"},
            {"cell": "D2", "column": "Certification", "value": "Unverified"},
        ],
    )
    check = validate_response(response, manifest, excerpts)[0]
    candidate = response.candidates[0]
    assert candidate.attribute_id == attribute and candidate.evidence_basis == "inferred_from_description"
    assert candidate.inference_rule == LEAD_FREE_DESCRIPTION_RULE
    assert check.quote.cells == ["A2"] and check.value is None
    assert DESCRIPTIVE_BOOLEAN_FLAG in candidate.qualification
    assert "not literal Boolean evidence or certification" in candidate.qualification


@pytest.mark.parametrize("column,value,extra_cells", [
    ("Body Material", "LOW LEAD BRASS", []),
    ("Certification", "LOW LEAD BRASS", []),
    ("DescrGen1", "Not LOW LEAD BRASS", []),
    ("DescrGen1", "LOW LEAD BRASS stem only", []),
    ("DescrGen1", "LOW LEAD BRASS or standard brass", []),
    ("DescrGen1", "Certified LOW LEAD BRASS", []),
    ("DescrGen1", "NSF61 LOW LEAD BRASS", []),
    ("DescrGen1", "LOW LEAD BRASS", [{"cell": "B2", "column": "Component", "value": "Stem"}]),
    ("DescrGen1", "LOW LEAD BRASS", [{"cell": "B2", "column": "DescrGen2", "value": "Not lead-free"}]),
    ("DescrGen1", "LOW LEAD BRASS", [{"cell": "B2", "column": "Lead-Free (No-Lead)", "value": "No"}]),
    ("DescrGen1", "Synthetic service inlet LLB", []),
])
def test_vendor_description_context_never_expands_abbreviations_or_bypasses_scope(column, value, extra_cells):
    response, manifest, excerpts = vendor_description_proposal(value, column=column, extra_cells=extra_cells)
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


def test_short_unscoped_paragraph_is_not_a_whole_product_description():
    response, manifest, excerpts = proposal("LOW LEAD BRASS")
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)


def test_inferred_metadata_survives_result_roundtrip_and_cannot_claim_a_literal_value():
    result = offline_result()
    assert result.extraction_error is None and result.model_call_status == "not_attempted"
    restored = EnrichmentResult.model_validate_json(result.model_dump_json())
    attribute = restored.attributes[0]
    assert attribute.status == "proposed" and attribute.review is None
    candidate, check = attribute.candidates[0], attribute.verification[0]
    assert check.version == "grounded-normalization-v3"
    assert candidate.evidence_basis == check.evidence_basis == "inferred_from_description"
    assert check.value is None and check.quote is not None
    with pytest.raises(ValidationError):
        EvidenceVerification.model_validate({**check.model_dump(), "value": check.quote.model_dump()})
    with pytest.raises(ValidationError):
        AttributeResult.model_validate({
            **attribute.model_dump(),
            "verification": [{
                **check.model_dump(), "evidence_basis": "literal",
                "inference_rule": None, "value": check.quote.model_dump(),
            }],
        })


@pytest.mark.parametrize("version", [
    "grounded-normalization-v1", "grounded-normalization-v2", "grounded-normalization-v3",
])
def test_historical_literal_records_remain_schema_compatible(version):
    result = offline_result("Lead-Free: Yes").model_dump()
    attribute = result["attributes"][0]
    attribute["verification"][0]["version"] = version
    for record in (attribute["candidates"][0], attribute["verification"][0]):
        record.pop("evidence_basis")
        record.pop("inference_rule")
    restored = EnrichmentResult.model_validate(result).attributes[0]
    assert restored.candidates[0].evidence_basis == restored.verification[0].evidence_basis == "literal"
    assert restored.verification[0].value is not None
    assert restored.verification[0].version == version


def test_verification_is_server_computed_and_absent_from_model_response_schema():
    schema = ExtractionResponse.model_json_schema()
    assert "EvidenceVerification" not in schema["$defs"]
    assert "verification" not in schema["$defs"]["Candidate"]["properties"]


def test_inference_metadata_is_not_a_bypass_for_literal_validation():
    response, manifest, excerpts = proposal("Low-lead brass valve")
    candidate = Candidate.model_validate({
        **response.candidates[0].model_dump(), "evidence_basis": "inferred_from_description",
        "inference_rule": LEAD_FREE_DESCRIPTION_RULE,
    })
    response.candidates[0] = candidate
    excerpts[0].text = candidate.supporting_quote = "Lead-Free: Yes"
    with pytest.raises(ResponseValidationError):
        validate_response(response, manifest, excerpts)
    for change in [
        {"value": False}, {"attribute_id": "NSF Certified"}, {"inference_rule": None},
        {"evidence_basis": "literal"}, {"unit": "percent"}, {"supporting_quote": None},
    ]:
        with pytest.raises(ValidationError):
            Candidate.model_validate({**candidate.model_dump(), **change})


def test_qualified_validation_retains_the_reclassified_candidate_without_model_calls():
    response, manifest, excerpts = proposal("Low-lead brass valve")
    completion = Mock()
    completion.complete_structured.return_value = response
    result = run_enrichment(
        LiveBundle(
            execution_mode="live_inference", manifest=manifest,
            sources=[OfflineSource(source_id="synthetic", product=manifest.product, excerpts=excerpts)],
        ), execution_mode="live_inference", completion=completion, qualified=True,
    )
    assert result.extraction_error is None
    assert result.attributes[0].candidates[0].evidence_basis == "inferred_from_description"
    assert result.attributes[0].verification[0].value is None


def test_a_different_product_source_is_ineligible_for_automatic_inference():
    response, manifest, excerpts = proposal("Low-lead brass valve")
    other_product = manifest.product.model_copy(update={"mpn": "OTHER-1"})
    result = run_offline(OfflineBundle(
        manifest=manifest, generated_response=response,
        sources=[OfflineSource(source_id="synthetic", product=other_product, excerpts=excerpts)],
    ))
    assert result.attributes[0].candidates == []
    assert result.skip_reason == "no_eligible_evidence"


def test_export_keeps_flag_rule_quote_and_null_literal_value_verification():
    result = offline_result()
    store = Mock()
    store.read_bytes.side_effect = Missing("fixture")
    service = BatchService(store)
    original = {"PIMITEM Number": "fixture", "Vendor Name": "Synthetic", "MPN": "TEST-1"}
    service.get = Mock(return_value={
        "items": [{"row": 2, "item_key": "row-2", "original": original, "warnings": [], "errors": []}],
        "state": "unresolved", "input_hashes": {}, "attribute_reference": "synthetic.xlsx",
        "original_definitions": [{"Attribute": "Lead-Free"}],
    })
    service.detail = Mock(return_value={
        "state": "unresolved", "reviewed_result": result.model_dump(mode="json"),
    })
    exported = read_workbook(service.export("synthetic-batch", "synthetic-owner"))["Results"][0]
    assert exported["Evidence basis"] == "inferred_from_description"
    assert exported["Inference rule"] == LEAD_FREE_DESCRIPTION_RULE
    assert exported["Supporting quote"] == "Low-lead brass angle valve"
    assert exported["Qualifications"].startswith(DESCRIPTIVE_BOOLEAN_FLAG)
    assert exported["Review status"] == "pending"
    verification = json.loads(exported["Evidence verification JSON"])
    assert verification["value"] is None and verification["quote"] is not None
