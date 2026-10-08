"""No-network quality extraction and append-only worker contracts."""

import json
from datetime import datetime, timezone
from io import BytesIO
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import pytest

from backend.batch import BatchService, digest
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.core.docintel import ParsedDocument, ParsedParagraph, ParsedTable
from backend.models.enrichment import AttributeDefinition, Candidate, EnrichmentResult, Evidence, Manifest, ProductKey
from backend.pilot import PARSER_VERSION
from backend.quality_pipeline import (
    QualityExtraction, QualityJudgment, QualityProposal, QualityUsageStop, ground_candidate, product_packet, run_product,
)
from backend.quality_worker import FORD_PDF_SHA256, CachedEvidenceLoader, QualitySmokeError, _image, run_quality_batch, scope_document
from backend.reviewer_workbook import build_reviewer_package, reviewer_status
from backend.workbooks import read_workbook, write_workbook

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def png_bytes():
    from PIL import Image
    output = BytesIO()
    with Image.new("RGB", (2, 2), color="white") as image:
        image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("No network is allowed"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("No DNS is allowed"))


def manifest(attributes=None):
    return Manifest(
        product=ProductKey(item_id="PIMITEM-225830", vendor="The Ford Meter Box Company,Inc", mpn="AV11-333W-NL", hierarchy_node="Angle Valves"),
        attributes=attributes or [AttributeDefinition(attribute_id="Primary Material", description="Body material", value_type="string")],
    )


def evidence(text="Part number: AV11-333W-NL. Body: BRASS.", tier="internal_pdf", key="e1"):
    return Evidence(
        evidence_id=key, source_id="source", source_locator="https://example.com/catalog#page=1&paragraph=0",
        source_version="sha256:" + "a" * 64, source_tier=tier, content_kind="source_excerpt", text=text,
        observed_at=NOW, provider_retrieved_at=NOW,
    )


def model_packet(user, kwargs):
    packet = json.loads(user)
    if kwargs.get("cache_prefix"):
        packet["definitions"] = json.loads(kwargs["cache_prefix"])["definitions"]
    return packet


class Completion:
    effort = "medium"

    def __init__(self, responses=None, *, disputed=False):
        self.responses = list(responses or [])
        self.calls = []
        self.last_usage = {}
        self.disputed = disputed

    def complete_structured(self, system, user, schema, **kwargs):
        packet = model_packet(user, kwargs)
        self.calls.append((schema, packet, kwargs))
        self.last_usage = {"model": "quality-model", "input_tokens": 100, "output_tokens": 30,
                           "reasoning_tokens": 10, "cached_input_tokens": 0, "cost_usd": 0.001}
        if schema == QualityJudgment:
            return QualityJudgment(decisions=[
                {"candidate_id": c["candidate_id"], "decision": "judge_disputed" if self.disputed else "accepted",
                 "reason": "Check applicability." if self.disputed else "Supported by the cited product source."}
                for c in packet["candidates"]
            ])
        return QualityExtraction(candidates=self.responses.pop(0) if self.responses else [])


def proposal(**updates):
    return {"attribute_id": "Primary Material", "value": "brass", "supporting_quote": "body brass",
            "evidence_ids": ["e1"], "origin": "literal", **updates}


def test_missing_pressure_candidate_is_not_described_as_a_found_value():
    result = run_product(manifest([
        AttributeDefinition(attribute_id="Pressure Rating", description="", value_type="number", unit_resolved=False),
    ]), [], Completion(), run_id="pressure-question")
    status, action = reviewer_status(result.attributes[0], result)
    assert status == "Definition needs clarification"
    assert "no verified candidate was found" in action.lower()


def test_reviewer_confidence_is_separate_from_model_probability():
    exact = evidence("Part number: AV11-333W-NL. Body: BRASS.")
    result = run_product(manifest(), [exact], Completion([[proposal(confidence=0.01)]]), run_id="confidence")
    sheets = read_workbook(build_reviewer_package([result]).workbook)
    assert sheets["Review"][0]["Confidence"] == "High"
    assert sheets["Evidence"][0]["Confidence"] == "High"
    assert result.attributes[0].candidates[0].confidence == 0.01
    assert sheets["Review"][0]["Decision"] == ""


def test_structured_enum_other_is_display_only_and_still_needs_quoted_value():
    from backend.quality_definitions import derive_definition
    from backend.quality_pipeline import ground_structured_candidate

    definition = AttributeDefinition(
        attribute_id="Primary Material", description="Body material", value_type="string",
        type_guidance="Enumerated", allowed_values=["bronze"],
    )
    specification = derive_definition(definition.model_dump())
    packet = product_packet(manifest([definition]), [evidence()], "internal_pdf", ["Primary Material"])
    assert packet["structured_definitions"][0]["expected_type"] == "Enumerated"
    assert packet["structured_definitions"][0]["allowed_values"] == ["bronze"]
    assert packet["citation_applicability"]["E1"] == "exact"
    candidate = ground_structured_candidate(QualityProposal(**proposal(value="Other: brass")), definition, [evidence()], specification)
    assert candidate.value == "Other: brass"
    assert candidate.supporting_quote == "body brass"
    assert candidate.grounding["definition_normalization"]["source_bearing_texts"] == ["brass"]
    with pytest.raises(ValueError, match="not grounded"):
        ground_structured_candidate(QualityProposal(**proposal(value="Other: polycarbonate")), definition, [evidence()], specification)


def test_structured_multiselect_preserves_members_and_rejects_an_unquoted_member():
    from backend.quality_definitions import derive_definition
    from backend.quality_pipeline import ground_structured_candidate

    definition = AttributeDefinition(
        attribute_id="Primary Material", description="Materials explicitly present", value_type="string",
        type_guidance="Multi-Select", allowed_values=["BRASS", "EPDM"],
    )
    source = evidence("Part number: AV11-333W-NL. Body: BRASS. Seal: EPDM.")
    specification = derive_definition(definition.model_dump())
    candidate = ground_structured_candidate(QualityProposal(**proposal(
        value="BRASS; EPDM", supporting_quote="Body: BRASS. Seal: EPDM.",
    )), definition, [source], specification)
    assert candidate.value == "BRASS; EPDM"
    assert len(candidate.grounding["multiselect_members"]) == 2
    with pytest.raises(ValueError, match="not grounded"):
        ground_structured_candidate(QualityProposal(**proposal(
            value="BRASS; Other: titanium", supporting_quote="Body: BRASS. Seal: EPDM.",
        )), definition, [source], specification)


def test_unconfirmed_drawing_remains_visible_with_low_confidence_and_question():
    product = manifest().product.model_copy(update={"vendor": "Mueller", "mpn": "014255 215N"})
    current = manifest().model_copy(update={"product": product})
    drawing = evidence("Part number: H14255N. Body: BRASS.")
    vendor = evidence(json.dumps({"sheet": "Vendor", "row": 2, "cells": [
        {"cell": "A2", "column": "MPN", "value": "014255 215N"},
        {"cell": "B2", "column": "Drawing", "value": "H14250"},
    ]}), "vendor_table", "vendor")
    vendor.source_id = "vendor"
    vendor.source_locator = "https://example.com/vendor.xlsx#sheet=Vendor&row=2"
    result = run_product(current, [drawing, vendor], Completion([[proposal()]]), run_id="map")
    candidate = result.attributes[0].candidates[0]
    assert candidate.judge_status == "accepted"
    assert candidate.grounding["applicability"]["status"] == "family-unconfirmed"
    sheets = read_workbook(build_reviewer_package([result]).workbook)
    assert sheets["Review"][0]["Confidence"] == "Low"
    assert "family-unconfirmed" in sheets["Review"][0]["Applicability"]
    questions = " ".join(row["Question"] for row in sheets["Questions"])
    assert "H14250" in questions and "H14255N" in questions


def test_fallback_preserves_disputes_without_steering_new_calls_with_prior_verdicts():
    definitions = manifest().attributes + [
        AttributeDefinition(attribute_id="Valve Type", description="", value_type="string"),
    ]
    vendor = evidence(json.dumps({"sheet": "Vendor", "row": 2, "cells": [
        {"cell": "B2", "column": "Body material", "value": "BRASS"},
    ]}), tier="vendor_table", key="vendor")
    vendor.source_locator = "https://example.com/vendor.xlsx#sheet=Vendor&row=2"

    class Feedback(Completion):
        def complete_structured(self, system, user, schema, **kwargs):
            result = super().complete_structured(system, user, schema, **kwargs)
            if schema == QualityJudgment and json.loads(user)["active_tier"] == "internal_pdf":
                result.decisions[0].decision = "judge_disputed"
                result.decisions[0].reason = "Use target-specific vendor evidence instead of this family drawing."
            return result

    client = Feedback([
        [proposal(), proposal(attribute_id="Valve Type", value="angle valve", supporting_quote="angle valve")],
        [proposal(evidence_ids=["vendor"], supporting_quote="BRASS")],
    ])
    result = run_product(manifest(definitions), [evidence("Part number: AV11-333W-NL. Body: BRASS. Angle valve."), vendor],
                         client, run_id="fallback")
    packet = next(packet for schema, packet, _ in client.calls
                  if schema == QualityExtraction and packet["active_tier"] == "vendor_table")
    assert packet["unresolved_attributes"] == ["Primary Material"]
    assert "prior_tier_review" not in packet
    assert [entry["kind"] for entry in packet["evidence"]] == ["vendor_row"]  # tier-scoped
    assert len(client.calls) == 5  # a 2/2 dispute needs no third vote
    assert [candidate.judge_status for candidate in result.attributes[0].candidates] == [
        "judge_disputed", "accepted",
    ]
    assert all("prior_tier_review" not in packet for schema, packet, _ in client.calls
               if schema == QualityJudgment)


def test_agreeing_disputes_stop_after_two_votes_after_both_extraction_passes():
    definitions = manifest().attributes + [AttributeDefinition(attribute_id="Valve Type", description="", value_type="string")]
    client = Completion([[proposal()], [proposal(attribute_id="Valve Type", value="angle valve", supporting_quote="angle valve")]], disputed=True)
    result = run_product(manifest(definitions), [evidence("Body: BRASS. Angle valve.")], client, run_id="r")
    assert [schema for schema, _, _ in client.calls] == [QualityExtraction, QualityExtraction, *[QualityJudgment] * 2]
    assert client.calls[-1][2]["reasoning_effort"] == "low"
    assert len(client.calls[-1][1]["candidates"]) == 2
    assert all(a.candidates[0].judge_status == "judge_disputed" for a in result.attributes)
    assert all(a.candidates[0].judge_reason.startswith("Majority 2/2") for a in result.attributes)
    assert all(a.reviewer_explanation for a in result.attributes)
    model_calls = [call for call in result.quality_diagnostics if call["operation"] == "model"]
    assert len(model_calls) == 4
    assert all(call["run_id"] == "r" and call["call_id"] for call in model_calls)


@pytest.mark.parametrize("third,status", [("accepted", "accepted"), ("judge_disputed", "judge_disputed")])
def test_third_vote_only_breaks_a_one_one_split(third, status):
    class Split(Completion):
        votes = iter(["judge_disputed", "accepted", third])

        def complete_structured(self, system, user, schema, **kwargs):
            result = super().complete_structured(system, user, schema, **kwargs)
            if schema == QualityJudgment:
                for decision in result.decisions:
                    decision.decision = next(self.votes)
            return result

    client = Split([[proposal()]])
    result = run_product(manifest(), [evidence()], client, run_id="split")
    assert [schema for schema, _, _ in client.calls].count(QualityJudgment) == 3
    candidate = result.attributes[0].candidates[0]
    assert candidate.judge_status == status and candidate.judge_reason.startswith("Majority 2/3")


def test_second_pass_reasks_material_using_already_cited_brass_and_nl_paragraphs():
    brass = ". All brass that comes in contact with potable water conforms to AWWA Standard C800 (ASTM B584, UNS C89833)"
    nl = '. The product has the letters "NL" cast into the main body for lead-free identification'
    first, second = evidence(brass), evidence(nl, key="e2")
    second.source_locator = "https://example.com/catalog#page=1&paragraph=1"
    definitions = [
        AttributeDefinition(attribute_id="Material Standard (Brass Alloy)", description="", value_type="string"),
        AttributeDefinition(attribute_id="Lead-Free", description="", value_type="boolean"),
        AttributeDefinition(attribute_id="Primary Material", description="", value_type="string"),
    ]
    client = Completion([
        [proposal(attribute_id=definitions[0].attribute_id, value="ASTM B584, UNS C89833", supporting_quote=brass),
         proposal(attribute_id="Lead-Free", value=True, supporting_quote=nl, evidence_ids=["e2"])],
        [proposal(value="No-lead brass", supporting_quote=brass + "; " + nl, evidence_ids=["e1", "e2"])],
    ])
    result = run_product(manifest(definitions), [first, second], client, run_id="material-reask")
    assert len(client.calls) == 3
    refinement = client.calls[1][1]
    assert refinement["unresolved_attributes"] == ["Primary Material"]
    assert refinement["already_cited_citation_ids"] == ["E1", "E2"]
    assert {c["attribute_id"] for c in refinement["first_pass_candidates"]} == {"Material Standard (Brass Alloy)", "Lead-Free"}
    assert all(set(c) == {"attribute_id", "value", "unit", "citation_ids", "quote"} for c in refinement["first_pass_candidates"])
    material = next(attribute for attribute in result.attributes if attribute.attribute_id == "Primary Material")
    assert material.candidates[0].value == "No-lead brass"
    assert material.candidates[0].origin == "derived"
    assert material.candidates[0].judge_status == "accepted"


def test_reviewer_proposals_are_readable_without_changing_quotes_or_machine_values():
    quote = "FLANGED OUTLET: NO"
    result = run_product(manifest([
        AttributeDefinition(attribute_id="Flanged Outlet", description="", value_type="boolean"),
    ]), [evidence(quote)], Completion([[proposal(
        attribute_id="Flanged Outlet", value=False, supporting_quote=quote,
    )]]), run_id="readable")
    before = result.model_dump(mode="json")
    public = read_workbook(build_reviewer_package([result]).workbook)
    assert public["Review"][0]["Proposed value"] == public["Evidence"][0]["Proposed value"] == "No"
    assert public["Review"][0]["Supporting quote"] == public["Evidence"][0]["Quote"] == quote
    assert result.model_dump(mode="json") == before
    assert result.attributes[0].candidates[0].value is False


def test_only_unresolved_advances_and_no_unnecessary_refinement():
    client = Completion([[proposal()]])
    class Web:
        def load(self, *args):
            pytest.fail("Resolved product must not search web")
    result = run_product(manifest(), [evidence()], client, run_id="r", web=Web())
    assert len(client.calls) == 2
    assert result.attributes[0].candidates[0].judge_status == "accepted"


def test_conflicts_keep_both_values_quotes_and_citations():
    definition = AttributeDefinition(attribute_id="Pressure", description="", value_type="number", unit="PSI")
    client = Completion([[
        proposal(attribute_id="Pressure", value=100, unit="PSI", supporting_quote="100 PSI"),
        proposal(attribute_id="Pressure", value=150, unit="PSI", supporting_quote="150 PSI", evidence_ids=["e2"]),
    ]])
    result = run_product(manifest([definition]), [evidence("100 PSI"), evidence("150 PSI", key="e2")], client, run_id="r")
    attribute = result.attributes[0]
    assert attribute.status == "conflict"
    assert [(c.value, c.supporting_quote, c.evidence_ids) for c in attribute.candidates] == [
        (100, "100 PSI", ["e1"]), (150, "150 PSI", ["e2"]),
    ]
    assert all(c.judge_status == "accepted" for c in attribute.candidates)


def test_only_unresolved_attribute_is_requested_from_vendor():
    definitions = manifest().attributes + [AttributeDefinition(attribute_id="Valve Type", description="", value_type="string")]
    vendor = evidence(json.dumps({"sheet": "Vendor", "row": 5, "cells": [
        {"cell": "A5", "column": "Description", "value": "angle valve"},
    ]}), "vendor_table", "v1").model_copy(update={"source_locator": "vendor.xlsx#row=5"})
    client = Completion([[proposal()], [], [
        proposal(attribute_id="Valve Type", value="angle valve", supporting_quote="angle valve", evidence_ids=["v1"]),
    ]])
    result = run_product(manifest(definitions), [evidence(), vendor], client, run_id="r")
    vendor_calls = [packet for _, packet, _ in client.calls if packet["active_tier"] == "vendor_table"]
    assert vendor_calls and all(packet["unresolved_attributes"] == ["Valve Type"] for packet in vendor_calls)
    assert all(a.candidates and a.candidates[0].judge_status == "accepted" for a in result.attributes)
    assert len(client.calls) == 5


@pytest.mark.parametrize("updates,reason", [
    ({"evidence_ids": []}, "existing evidence_id"),
    ({"evidence_ids": ["unknown"]}, "unknown"),
    ({"supporting_quote": "The material is bronze"}, "normalized substring"),
    ({"value": "bronze"}, "value is not grounded"),
])
def test_ungrounded_rejection_has_actionable_reason(updates, reason):
    with pytest.raises(ValueError, match=reason):
        ground_candidate(QualityProposal(**proposal(**updates)), manifest().attributes[0], [evidence()])


def test_quote_is_substring_not_word_bag_and_wrong_page_is_rejected():
    with pytest.raises(ValueError, match="normalized substring"):
        ground_candidate(QualityProposal(**proposal(supporting_quote="brass body")), manifest().attributes[0], [evidence()])
    bad = evidence().model_copy(update={"source_locator": "catalog.pdf"})
    with pytest.raises(ValueError, match="page/row"):
        ground_candidate(QualityProposal(**proposal()), manifest().attributes[0], [bad])


@pytest.mark.parametrize("marker", ["LLB", "NL", "-NL", "NSF/ANSI 372", "NSF/ANSI Standard 372", "AB1953", "LOW-LEAD BRASS"])
def test_narrow_lead_inference_is_grounded_and_always_review(marker):
    definition = AttributeDefinition(attribute_id="Lead-Free (No-Lead)", description="", value_type="boolean")
    candidate = ground_candidate(QualityProposal(
        attribute_id=definition.attribute_id, value=True, origin="inferred", supporting_quote=marker,
        evidence_ids=["e1"], justification="Descriptive marker supports a review-only whole-product proposal.",
    ), definition, [evidence(marker)])
    assert candidate.origin == "inferred"
    assert candidate.evidence_basis == "inferred_from_description"
    assert "requires review" in candidate.qualification
    with pytest.raises(ValueError):
        ground_candidate(QualityProposal(
            attribute_id=definition.attribute_id, value=True, origin="inferred", supporting_quote="not lead free",
            evidence_ids=["e1"], justification="unsupported",
        ), definition, [evidence("not lead free")])


def test_pressure_candidate_survives_definition_clarification_and_unit_normalization():
    definition = AttributeDefinition(attribute_id="Pressure Rating", description="", value_type="number", unit_resolved=False)
    client = Completion([[proposal(attribute_id=definition.attribute_id, value="100 PSI", unit=None,
                                  supporting_quote="100 PSI working pressure")]])
    result = run_product(manifest([definition]), [evidence("Meets 100 PSI working pressure requirement.")], client, run_id="r")
    attribute = result.attributes[0]
    assert attribute.status == "definition_clarification_needed"
    assert attribute.candidates[0].value == 100
    assert attribute.candidates[0].unit == "PSI"
    assert attribute.candidates[0].origin == "derived"
    assert "clarify" in attribute.reviewer_explanation.lower()


def test_unit_alias_and_fraction_normalize_without_numeric_drift():
    definition = AttributeDefinition(attribute_id="Size", description="", value_type="number", unit="in")
    candidate = ground_candidate(QualityProposal(attribute_id="Size", value='3/4"', evidence_ids=["e1"],
                                                supporting_quote='Inlet 3/4"'), definition, [evidence('Inlet 3/4"')])
    assert candidate.value == .75 and candidate.unit == "in"
    with pytest.raises(ValueError, match="disagree"):
        ground_candidate(QualityProposal(attribute_id="Size", value="3/4 mm", unit="in", evidence_ids=["e1"],
                                        supporting_quote='Inlet 3/4"'), definition, [evidence('Inlet 3/4"')])


def test_full_product_packet_contains_all_definitions_vendor_cells_and_only_scoped_pdf():
    product = manifest().product
    other = product.model_copy(update={"item_id": "PIMITEM-221315", "mpn": "AV11-444W-NL"})
    document = ParsedDocument(source="batchblob:///documents/ford.pdf", cache_key="sha256:" + "a"*64, parsed_at=NOW,
                              tables=[ParsedTable(page_number=1, cells=[
                                  ["INLET SIZE", "PART NUMBER"], ['3/4"', product.mpn], ['1"', other.mpn]])],
                              paragraphs=[ParsedParagraph(page_number=1, text=value) for value in ["Shared 90° motion", "3/4\"", '1"', other.mpn]])
    scoped = scope_document(document, product, {"products": [product.model_dump(), other.model_dump()]})
    assert scoped.tables[0].cells[2] == ["", ""]
    assert [p.text for p in scoped.paragraphs] == ["Shared 90° motion", "", "", ""]
    assert document.tables[0].cells[2] != ["", ""]
    from backend.extract import source_evidence
    from backend.models.enrichment import OfflineSource
    entries = source_evidence(OfflineSource(source_id="ford", product=product, document=scoped), NOW)
    vendor = evidence(json.dumps({"sheet": "Vendor", "row": 1096, "cells": [
        {"cell": "A1096", "column": "MPN", "value": product.mpn},
        {"cell": "B1096", "column": "Description", "value": "FULL ROW VALUE"},
    ]}), "vendor_table", "vendor")
    packet = product_packet(manifest(), [*entries, vendor], "internal_pdf", ["Primary Material"])
    payload = json.dumps(packet)
    assert other.mpn not in payload and "FULL ROW VALUE" in payload
    assert len(packet["definitions"]) == len(manifest().attributes)
    assert packet["target_mpn"] == product.mpn
    assert product.mpn in packet["image_instruction"] and "ignore neighboring" in packet["image_instruction"]
    assert any(e.get("presentation", {}).get("kind") == "table_row" for e in packet["evidence"])


def test_ford_catalog_headers_and_row_references_restore_all_cited_cells():
    from backend.extract import source_evidence
    from backend.models.enrichment import OfflineSource
    from backend.pdf_presentation import pure_header
    assert pure_header(":selected: / SUBMITTED ITEM(S)")
    definition = AttributeDefinition(attribute_id="Inlet Size", description="", value_type="string")
    document = ParsedDocument(source="catalog.pdf", cache_key="v1", parsed_at=NOW, tables=[
        ParsedTable(page_number=1, cells=[["INLET SIZE", "PART NUMBER", ":selected: / SUBMITTED ITEM(S)"],
                                         ['3/4"', manifest().product.mpn, ""]]),
    ])
    entries = source_evidence(OfflineSource(source_id="ford", product=manifest().product, document=document), NOW)
    client = Completion([[proposal(attribute_id="Inlet Size", value="3/4", supporting_quote='INLET SIZE=3/4"', evidence_ids=["E1"])]])
    result = run_product(manifest([definition]), entries, client, run_id="r")
    candidate = result.attributes[0].candidates[0]
    assert len(candidate.evidence_ids) >= 4
    assert candidate.judge_status == "accepted"


def test_scoped_second_catalog_row_keeps_original_headers_without_neighbor_values():
    from backend.extract import source_evidence
    from backend.models.enrichment import OfflineSource
    product = manifest().product
    other = product.model_copy(update={"item_id": "other", "mpn": "AV11-444W-NL"})
    document = ParsedDocument(source="catalog.pdf", cache_key="v1", parsed_at=NOW, tables=[
        ParsedTable(page_number=1, cells=[["INLET SIZE", "PART NUMBER"], ['1"', other.mpn], ['3/4"', product.mpn]]),
    ])
    document = scope_document(document, product, {"products": [product.model_dump(), other.model_dump()]})
    entries = source_evidence(OfflineSource(source_id="ford", product=product, document=document), NOW)
    packet = product_packet(manifest(), entries, "internal_pdf", ["Primary Material"])
    row = next(e for e in packet["evidence"] if e.get("presentation", {}).get("kind") == "table_row")
    assert row["text"].startswith('Row 2: INLET SIZE=3/4"')
    assert other.mpn not in json.dumps(packet)


def test_vendor_loader_keeps_entire_matching_row_and_not_other_product():
    store, items = seed_store(1)
    item = items[0]
    current = Manifest.model_validate(item["manifest"])
    workbook = write_workbook({"Vendor": [
        ["MPN", "Description", "Other Field", "Zero"],
        ["OTHER", "Wrong body", "Wrong part", "1"],
        [current.product.mpn, "LLB angle valve", "Keep this full row", "0"],
    ]})
    store.write_bytes("documents/vendor.xlsx", workbook)
    item["sources"] = [{"source_id": "vendor", "kind": "blob", "format": "xlsx", "source_tier": "vendor_table",
                        "blob": "documents/vendor.xlsx", "sha256": digest(workbook), "products": [current.product.model_dump()],
                        "table": {"sheet": "Vendor", "mpn_column": "MPN", "expected_vendor": current.product.vendor}}]
    entries, retrieval, provenance = CachedEvidenceLoader(store).load(item)
    assert retrieval[0].status == "success" and provenance[0]["rows"] == [3]
    assert len(entries) == 1 and "Wrong part" not in entries[0].text
    assert [cell["value"] for cell in json.loads(entries[0].text)["cells"]] == [current.product.mpn, "LLB angle valve", "Keep this full row", "0"]


class Store:
    def __init__(self):
        self.data = {}
        self.revision = 0

    def read_bytes(self, key, max_bytes=64 * 1024 * 1024):
        if key not in self.data:
            raise Missing(key)
        value, version = self.data[key]
        assert len(value) <= max_bytes
        return value, version

    def write_bytes(self, key, value, version=None):
        if (key in self.data and self.data[key][1] != version) or (key not in self.data and version is not None):
            raise Conflict("Conditional write failed")
        self.revision += 1
        self.data[key] = (bytes(value), str(self.revision))
        return str(self.revision)

    def keys(self, prefix):
        return sorted(key for key in self.data if key.startswith(prefix))


def seed_store(count=4):
    store = Store()
    items = []
    for index in range(count):
        current = manifest()
        current.product.item_id = f"product-{index}"
        binding = {"source_id": "pdf", "kind": "blob", "format": "pdf", "source_tier": "internal_pdf",
                   "blob": "documents/catalog.pdf", "sha256": "a"*64, "products": [current.product.model_dump()]}
        items.append({"item_key": f"row-{index+2}", "row": index+2, "manifest": current.model_dump(mode="json"),
                      "sources": [binding], "original": {"PIMITEM Number": current.product.item_id, "MPN": current.product.mpn,
                                                       "Vendor Name": current.product.vendor}, "errors": [], "warnings": []})
        write_json(store, f"items/batch/row-{index+2}.json", {"state": "failed", "error": "prior failed result"})
    document = ParsedDocument(source="batchblob:///documents/catalog.pdf", cache_key="sha256:" + "a"*64,
                              parsed_at=NOW, paragraphs=[ParsedParagraph(text="Body: BRASS.", page_number=1)])
    key = "parses/" + digest((document.source + "a"*64 + PARSER_VERSION).encode()) + ".json"
    write_json(store, key, {"parser_version": PARSER_VERSION, "document": document.model_dump(mode="json"),
                           "document_sha256": digest(document.model_dump_json().encode())})
    write_json(store, "batches/batch.json", {"id": "batch", "owner": "owner", "items": items,
                                          "state": "completed", "original_definitions": [{"name": "Material"}],
                                          "input_hashes": {}, "attribute_reference": "attributes.xlsx"})
    return store, items


def test_four_product_worker_appends_attempts_preserves_history_and_persists_usage():
    store, items = seed_store()
    old = dict(store.data)
    client = Completion()
    def answer(system, user, schema, **kwargs):
        packet = model_packet(user, kwargs)
        if schema == QualityExtraction:
            client.responses.append([proposal(evidence_ids=[packet["evidence"][0]["citation_id"]])])
        return Completion.complete_structured(client, system, user, schema, **kwargs)
    client.complete_structured = answer
    summary = run_quality_batch(store, "batch", "owner", "first", completion=client, execution_id="first-execution")
    assert len(summary["products"]) == 4 and summary["model_calls"] == 5
    assert summary["new_di_calls"] == 0
    assert len(store.keys("quality-runs/batch/first/usage/")) == 5
    for item in items:
        key = "items/batch/" + item["item_key"] + ".json"
        state = read_json(store, key)[0]
        assert state["previous_attempt"] == json.loads(old[key][0])
        assert "/attempts/" in state["result_key"]
    first_results = {key: value for key, value in store.data.items() if key.startswith("results/")}
    run_quality_batch(store, "batch", "owner", "second", completion=client)
    assert all(store.data[key] == value for key, value in first_results.items())
    assert store.data["batches/batch.json"] == old["batches/batch.json"]
    assert len(BatchService(store).detail("batch", "row-2", "owner")["attempt_history"]) == 3
    with pytest.raises(Conflict):
        run_quality_batch(store, "batch", "owner", "first", completion=client, execution_id="first-execution")
    exported = read_workbook(BatchService(store).export("batch", "owner"))
    assert exported["Diagnostics"][0]["Model"] == "quality-model"
    assert exported["Results"][0]["Judge status"] == "accepted"
    calls = [row for row in exported["Diagnostics"] if row["Operation"] == "model"]
    assert len(calls) == len({row["Call ID"] for row in calls}) == 9
    assert {row["Run ID"] for row in calls} == {"first", "second"}
    assert sum(float(row["Cost USD"]) for row in calls) == pytest.approx(.009)
    assert len(store.keys("quality-judge-cache/")) == 1
    prefixes = [kwargs["cache_prefix"] for schema, packet, kwargs in client.calls if schema == QualityExtraction]
    assert len(set(prefixes)) == 1
    assert all("product-0" not in prefix and "product-1" not in prefix for prefix in prefixes)


def test_first_disputed_vote_uses_majority_and_persists_for_next_product():
    from backend.quality_judge import JudgeCache

    cache = JudgeCache(Store(), namespace="owner", policy="test")

    class Majority(Completion):
        def complete_structured(self, system, user, schema, **kwargs):
            answer = super().complete_structured(system, user, schema, **kwargs)
            if schema == QualityJudgment and sum(s == QualityJudgment for s, _, _ in self.calls) == 1:
                answer.decisions[0].decision = "judge_disputed"
                answer.decisions[0].reason = "Uncertain first vote."
            return answer

    first = Majority([[proposal()]])
    result = run_product(manifest(), [evidence()], first, run_id="first", judge_cache=cache)
    assert result.attributes[0].candidates[0].judge_status == "accepted"
    assert "Majority 2/3" in result.attributes[0].candidates[0].judge_reason
    assert sum(schema == QualityJudgment for schema, _, _ in first.calls) == 3
    second_manifest = manifest()
    second_manifest.product.item_id = "second-product"
    second = Completion([[proposal()]], disputed=True)
    repeated = run_product(second_manifest, [evidence()], second, run_id="second", judge_cache=cache)
    assert repeated.attributes[0].candidates[0].judge_status == "accepted"
    assert not any(schema == QualityJudgment for schema, _, _ in second.calls)


def test_judge_identity_invalidates_changed_definition_source_quote_and_owner():
    from backend.quality_judge import JudgeCache

    store = Store()
    definition, original = manifest().attributes[0], evidence()
    candidate = ground_candidate(QualityProposal(**proposal()), definition, [original])
    cache = JudgeCache(store, namespace="owner", policy="policy")
    key = cache.identity(definition, candidate, [original])
    cache.put(key, {"decision": "accepted", "reason": "Supported.", "votes": []})
    assert JudgeCache(store, namespace="owner", policy="policy").get(key)["decision"] == "accepted"
    assert JudgeCache(store, namespace="another-owner", policy="policy").get(key) is None
    assert cache.identity(definition.model_copy(update={"description": "Different meaning"}), candidate, [original]) != key
    assert cache.identity(definition, candidate.model_copy(update={"supporting_quote": "other quote"}), [original]) != key
    # Location-free: the same quote/value in another row, page or version reuses the verdict.
    moved = original.model_copy(update={"source_version": "changed",
                                        "source_locator": "https://example.com/other#page=9&paragraph=4"})
    assert cache.identity(definition, candidate, [moved]) == key
    # ...but a different document role or applicability status does not.
    titled = original.model_copy(update={"source_locator": original.source_locator + "&role=manufacturerTitleBlock"})
    assert cache.identity(definition, candidate, [titled]) != key
    unconfirmed = candidate.model_copy(update={"grounding": {**candidate.grounding,
                                                             "applicability": {"status": "family-unconfirmed"}}})
    assert cache.identity(definition, unconfirmed, [original]) != key


def test_vendor_judge_identity_uses_the_quoted_cell_column_not_the_row():
    from backend.quality_judge import JudgeCache

    def row(number, column):
        return Evidence(
            evidence_id=f"v{number}", source_id="vendor", source_tier="vendor_table", content_kind="source_excerpt",
            source_locator=f"https://example.com/vendor.xlsx#sheet=S&row={number}", source_version="sha256:" + "c" * 64,
            text=json.dumps({"sheet": "S", "row": number, "cells": [
                {"cell": f"A{number}", "column": "Part #", "value": f"P{number}"},
                {"cell": f"B{number}", "column": column, "value": "LOCKWING"}]}),
            observed_at=NOW, provider_retrieved_at=NOW,
        )
    definition = AttributeDefinition(attribute_id="Locking Feature", description="", value_type="string")
    cache = JudgeCache(policy="policy")
    def key(entry):
        candidate = Candidate(attribute_id="Locking Feature", value="Other: LOCKWING", evidence_ids=[entry.evidence_id],
                              origin="derived", supporting_quote="LOCKWING")
        return cache.identity(definition, candidate, [entry])
    assert key(row(1096, "DescrGen4")) == key(row(2201, "DescrGen4"))
    assert key(row(1096, "DescrGen4")) != key(row(1096, "Notes"))


def test_incomplete_judge_votes_are_visible_and_never_cached():
    from backend.quality_judge import JudgeCache

    cache = JudgeCache(Store(), policy="test")

    class MissingJudge(Completion):
        def complete_structured(self, system, user, schema, **kwargs):
            answer = super().complete_structured(system, user, schema, **kwargs)
            return QualityJudgment(decisions=[]) if schema == QualityJudgment else answer

    client = MissingJudge([[proposal()]])
    result = run_product(manifest(), [evidence()], client, run_id="r", judge_cache=cache)
    assert result.attributes[0].candidates[0].judge_status == "judge_disputed"
    assert "Incomplete judge votes" in result.attributes[0].candidates[0].judge_reason
    assert cache.memory == {}


def test_worker_ocr_verification_uses_one_bounded_analysis_and_the_same_usage_callback(monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from backend import quality_pdf, quality_worker
    from backend.quality_pdf import CachedPDFOCR

    store, items = seed_store(1)
    content = b"%PDF-synthetic-textless"
    source_hash = "51bd50b060bda424281f80c61b038cdc7221c07c05c80da432cd6c3dc2c34290"
    items[0]["sources"][0]["sha256"] = source_hash
    record, version = read_json(store, "batches/batch.json")
    record["items"] = items
    write_json(store, "batches/batch.json", record, version)
    store.write_bytes("documents/catalog.pdf", content)
    original_digest = quality_worker.digest
    monkeypatch.setattr(quality_worker, "digest", lambda value: source_hash if value == content else original_digest(value))
    monkeypatch.setattr(CachedEvidenceLoader, "load", lambda self, item: ([evidence()], [], []))
    monkeypatch.setattr(quality_pdf, "pdf_text", lambda content: ("", 1))

    class Parser:
        last_page_count = 1
        calls = 0

        def extract_pdf_bytes(self, data, *, source, page_limit):
            self.calls += 1
            assert data == content and page_limit == 1
            return ParsedDocument(
                source=source, cache_key="sha256:" + hashlib.sha256(data).hexdigest(), parsed_at=NOW,
                raw_text="Mueller Co. manufacturer drawing.",
                paragraphs=[ParsedParagraph(text="Mueller Co. manufacturer drawing.", page_number=1)],
            )

    parser = Parser()
    web = SimpleNamespace(pdf_ocr=CachedPDFOCR(store, parser))
    usage = []
    summary = run_quality_batch(
        store, "batch", "owner", "ocr-test", completion=Completion([[proposal()]]),
        web=web, ocr_smoke=True, usage_callback=usage.append,
    )
    assert parser.calls == summary["new_di_calls"] == 1
    calls = [entry for entry in usage if entry["operation"] == "document_intelligence"]
    assert len(calls) == 1 and calls[0]["analyzed_pages"] == 1
    assert calls[0]["phase"] == "ocr_verification" and calls[0]["status"] == "succeeded"
    assert read_json(store, "quality-runs/batch/ocr-test/ocr-verification.json")[0]["page_count"] == 1


def test_cache_miss_is_gap_never_fresh_di_and_image_is_not_pdf_hash(monkeypatch):
    store, items = seed_store(1)
    store.data = {key: value for key, value in store.data.items() if not key.startswith("parses/")}
    monkeypatch.setattr("backend.core.docintel.DocumentIntelligenceService.__init__", lambda *args, **kwargs: pytest.fail("DI must not initialize"))
    entries, retrieval, provenance = CachedEvidenceLoader(store).load(items[0])
    assert not entries and retrieval[0].status == "failed"
    assert provenance[0]["new_di"] is False
    store.write_bytes("rendered.png", png_bytes())
    assert _image(store, "rendered.png")[0].startswith("data:image/png;base64,")
    store.write_bytes("catalog.pdf", b"%PDF-1.7")
    with pytest.raises(ValueError, match="rendered PNG"):
        _image(store, "catalog.pdf")


@pytest.mark.parametrize("mode,status", [
    ("unset", "missing"), ("missing", "missing"), ("invalid", "invalid"),
    ("corrupt", "invalid"), ("denied", "unavailable"), ("present", "available"),
])
def test_ford_image_fault_is_per_item_and_text_results_survive(monkeypatch, mode, status):
    store, items = seed_store(2)
    items[0]["sources"][0]["sha256"] = FORD_PDF_SHA256
    items[1]["manifest"]["product"].update(vendor="Mueller", mpn="014255 215N")
    record, revision = read_json(store, "batches/batch.json")
    record["items"] = items
    write_json(store, "batches/batch.json", record, revision)
    monkeypatch.setattr(CachedEvidenceLoader, "load", lambda self, item: ([evidence()], [], []))
    blob = None if mode == "unset" else "documents/ford-image.png"
    if mode in {"present", "invalid", "corrupt"}:
        store.write_bytes(blob, png_bytes() if mode == "present" else b"\x89PNG\r\n\x1a\nbad" if mode == "corrupt" else b"%PDF-1.7")
    if mode == "denied":
        original_read = store.read_bytes
        def denied(key, max_bytes=64 * 1024 * 1024):
            if key == blob:
                raise PermissionError("Storage network denied")
            return original_read(key, max_bytes)
        monkeypatch.setattr(store, "read_bytes", denied)
    client = Completion([[proposal()], [proposal()]])
    summary = run_quality_batch(store, "batch", "owner", "image-" + mode, completion=client, ford_image_blob=blob)
    assert summary["state"] == "completed" and len(summary["products"]) == 2
    assert summary["model_calls"] == 4  # applicability differs between the products, so no verdict reuse
    ford = EnrichmentResult.model_validate(BatchService(store).detail("batch", "row-2", "owner")["machine_result"])
    other = EnrichmentResult.model_validate(BatchService(store).detail("batch", "row-3", "owner")["machine_result"])
    assert ford.attributes[0].candidates[0].value == other.attributes[0].candidates[0].value == "brass"
    assert sum(d["operation"] == "model" for d in ford.quality_diagnostics) == 2
    assert not any(d.get("operation") == "image_input" for d in other.input_diagnostics)
    diagnostic = next(d for d in ford.input_diagnostics if d.get("operation") == "image_input")
    assert diagnostic["status"] == status and diagnostic["text_only"] is (mode != "present")
    assert diagnostic["target_mpn"] == ford.manifest.product.mpn
    # The catalog image accompanies extraction only; judges see cited text entries.
    assert bool(client.calls[0][2]["images"]) is (mode == "present")
    assert all(kwargs["images"] is None for _, _, kwargs in client.calls[1:])
    assert all(packet["target_mpn"] == ford.manifest.product.mpn for _, packet, _ in client.calls[:2])
    public = read_workbook(build_reviewer_package([ford, other]).workbook)
    assert set(public) == {"Review", "Evidence", "Instructions", "Summary", "Questions"}
    technical = read_workbook(BatchService(store).export("batch", "owner"))
    assert any(row["Diagnostic"] == "Image input" and row["Status"] == status for row in technical["Diagnostics"])


@pytest.mark.parametrize("override", [None, "documents/missing-image.png"])
def test_cached_ford_pdf_renders_through_pipes_without_image_files(monkeypatch, override):
    import base64
    import subprocess
    from types import SimpleNamespace
    from backend.quality_worker import FORD_PDF_BLOB, image_input

    pdf = b"%PDF-1.7\nsynthetic approved page"
    monkeypatch.setattr("backend.quality_worker.FORD_PDF_SHA256", digest(pdf))
    png = b"\x89PNG\r\n\x1a\nsynthetic"
    calls = []
    def render(command, **kwargs):
        calls.append(command)
        assert command == ["pdftoppm", "-f", "1", "-l", "1", "-singlefile", "-scale-to", "2400", "-png", "-"]
        assert kwargs == {"input": pdf, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "check": True, "timeout": 120}
        return SimpleNamespace(stdout=png)
    monkeypatch.setattr("backend.quality_worker.subprocess.run", render)
    store = Store()
    store.write_bytes(FORD_PDF_BLOB, pdf)
    before = dict(store.data)
    images, diagnostic = image_input(store, override)
    assert base64.b64decode(images[0].split(",", 1)[1]) == png
    assert diagnostic["status"] == "available" and diagnostic["image_source"] == "cached_pdf_render"
    assert diagnostic["page"] == 1 and diagnostic["text_only"] is False
    assert len(calls) == 1
    assert store.data == before
    if override:
        assert diagnostic["override_error_type"] == "Missing"


def test_ford_renderer_refuses_wrong_pdf_hash_before_running_subprocess(monkeypatch):
    from backend.quality_worker import render_ford_page
    monkeypatch.setattr("backend.quality_worker.subprocess.run", lambda *args, **kwargs: pytest.fail("Wrong PDF cannot be rendered"))
    with pytest.raises(ValueError, match="hash-verified approved PDF"):
        render_ford_page(b"%PDF-1.7 not-the-approved-Ford-PDF")


def test_missing_poppler_is_nonfatal_text_only_diagnostic(monkeypatch):
    from backend.quality_worker import FORD_PDF_BLOB, image_input
    pdf = b"%PDF-1.7 synthetic approved"
    monkeypatch.setattr("backend.quality_worker.FORD_PDF_SHA256", digest(pdf))
    def missing(*args, **kwargs):
        raise FileNotFoundError("pdftoppm")
    monkeypatch.setattr("backend.quality_worker.subprocess.run", missing)
    store = Store()
    store.write_bytes(FORD_PDF_BLOB, pdf)
    images, diagnostic = image_input(store, None)
    assert images is None and diagnostic["text_only"] is True
    assert diagnostic["status"] == "unavailable"
    assert diagnostic["render_error_type"] == "FileNotFoundError"
    assert "pdftoppm" in diagnostic["reason"]


def test_real_poppler_renders_synthetic_page_to_data_url_without_files(monkeypatch):
    import base64
    import shutil
    if not shutil.which("pdftoppm"):
        pytest.skip("Poppler is installed in the worker image")
    from PIL import Image
    from backend.quality_worker import render_ford_page

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 600] /Resources << >> /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(content)
    content.extend(b"xref\n0 5\n0000000000 65535 f \n")
    content.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:]))
    content.extend(f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    pdf = bytes(content)
    monkeypatch.setattr("backend.quality_worker.FORD_PDF_SHA256", digest(pdf))
    data = render_ford_page(pdf)
    assert data.startswith("data:image/png;base64,")
    with Image.open(BytesIO(base64.b64decode(data.split(",", 1)[1]))) as image:
        assert image.size == (1200, 2400)


def test_usage_persists_on_failed_model_and_budget_callback_prevents_next_call():
    client = Completion([[proposal()]])
    usages = []
    def before(context):
        if usages:
            raise RuntimeError("Dollar limit reached")
    with pytest.raises(QualityUsageStop):
        run_product(manifest(), [evidence()], client, run_id="r", before_call=before, usage_callback=usages.append)
    assert len(client.calls) == 1 and len(usages) == 1


def test_meter_failure_is_persisted_and_never_swallowed_as_model_failure():
    store, _ = seed_store(1)
    client = Completion([[]])
    def broken_meter(record):
        raise RuntimeError("Usage persistence failed")
    with pytest.raises(QualityUsageStop):
        run_quality_batch(store, "batch", "owner", "meter-failed", completion=client, usage_callback=broken_meter)
    assert len(client.calls) == 1
    summary = read_json(store, "quality-runs/batch/meter-failed/summary.json")[0]
    assert summary["state"] == "failed" and summary["usage"][0]["meter_error"] == "RuntimeError"
    assert store.keys("quality-runs/batch/meter-failed/usage/") == ["quality-runs/batch/meter-failed/usage/0001.json"]


def test_quality_reviewer_is_five_safe_sheets_with_blank_decisions_and_questions():
    client = Completion([[proposal()]], disputed=True)
    result = run_product(manifest(), [evidence()], client, run_id="internal-run")
    package = build_reviewer_package([result])
    sheets = read_workbook(package.workbook)
    assert set(sheets) == {"Review", "Evidence", "Instructions", "Summary", "Questions"}
    row = sheets["Review"][0]
    assert row["Decision"] == row["Correction"] == row["Reason"] == ""
    assert row["Judge status"] == "judge_disputed" and row["Origin"] == "literal"
    assert len(sheets["Questions"]) == 1
    question = sheets["Questions"][0]
    assert question["Product ID"] == row["Product ID"] and question["Attribute"] == row["Attribute"]
    assert question["Question"] and question["Response"] == ""
    with ZipFile(BytesIO(package.workbook)) as archive:
        assert all("hyperlink" not in archive.read(name).decode().lower() for name in archive.namelist())
        document = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        assert not document.findall(".//m:f", ns)
        assert document.find(".//m:dataValidation", ns).attrib["allowBlank"] == "1"
        text = "".join(archive.read(name).decode() for name in archive.namelist())
        assert "internal-run" not in text and "a" * 64 not in text and "batchblob" not in text


def test_legacy_candidates_and_results_keep_defaults():
    candidate = Candidate(attribute_id="Primary Material", value="brass", evidence_ids=["e1"])
    assert candidate.origin == "model_generated" and candidate.judge_status == "not_judged"


def seed_smoke_store(cell_value="Lockwing", head_description=None):
    store, items = seed_store(1)
    item = items[0]
    current = manifest([
        AttributeDefinition(attribute_id="Operating Head Style", description="Operating head", value_type="string"),
        *[AttributeDefinition(attribute_id=f"Other attribute {index}", description="", value_type="string") for index in range(23)],
    ])
    current.product.item_id = "PIMITEM-213030"
    current.product.vendor = "Mueller Water Products, Inc. Mueller Co Llc"
    current.product.mpn = "014255    215N"
    item["manifest"] = current.model_dump(mode="json")
    item["original"].update({"PIMITEM Number": current.product.item_id, "Vendor Name": current.product.vendor, "MPN": current.product.mpn})
    item["sources"][0]["products"] = [current.product.model_dump()]
    headers = ["MPN", *[f"Field {index}" for index in range(2, 20)], "Operating Head Style"]
    rows = [headers, *[[f"OTHER-{index}"] for index in range(1094)], [current.product.mpn, *([""] * 18), cell_value]]
    if head_description is not None:
        rows[-1][17] = head_description
    workbook = write_workbook({"Vendor": rows})
    store.write_bytes("documents/mueller.xlsx", workbook)
    item["sources"].append({
        "source_id": "mueller-vendor", "kind": "blob", "format": "xlsx", "source_tier": "vendor_table",
        "blob": "documents/mueller.xlsx", "sha256": digest(workbook), "products": [current.product.model_dump()],
        "table": {"sheet": "Vendor", "mpn_column": "MPN", "expected_vendor": current.product.vendor},
    })
    record, version = read_json(store, "batches/batch.json")
    record["items"] = items
    write_json(store, "batches/batch.json", record, version)
    return store


class SmokeCompletion(Completion):
    def __init__(self, value="Lockwing"):
        super().__init__()
        self.value = value

    def complete_structured(self, system, user, schema, **kwargs):
        packet = json.loads(user)
        row = next(entry for entry in packet["evidence"] if entry["kind"] == "vendor_row")
        self.responses.append([proposal(
            attribute_id="Operating Head Style", value=self.value, supporting_quote=self.value,
            evidence_ids=[row["citation_id"]],
        )])
        return super().complete_structured(system, user, schema, **kwargs)


def test_smoke_only_uses_full_packet_one_call_and_changes_no_item_results():
    store = seed_smoke_store()
    previous = dict(store.data)
    client = SmokeCompletion()
    meter = []
    summary = run_quality_batch(
        store, "batch", "owner", "smoke-pass", completion=client, smoke_only=True,
        ford_image_blob="not-read-in-smoke", usage_callback=meter.append,
    )
    assert summary["smoke_only"] is True and summary["state"] == "completed"
    assert summary["products"] == [] and summary["model_calls"] == 1
    assert summary["smoke"]["value"] == "Lockwing"
    assert summary["smoke"]["source_cells"] == ["T1096"] and summary["smoke"]["source_row"] == 1096
    assert summary["smoke"]["grounding"]["cells"] == ["T1096"]
    assert read_json(store, "quality-runs/batch/smoke-pass/smoke.json")[0] == summary["smoke"]
    assert all(store.data[key] == value for key, value in previous.items())
    assert len(client.calls) == len(meter) == 1 and meter[0]["phase"] == "smoke"
    packet = client.calls[0][1]
    assert len(packet["definitions"]) == 24
    assert packet["unresolved_attributes"] == ["Operating Head Style"]
    assert packet["target_mpn"] == "014255    215N"
    assert packet["smoke_target"] == {"attribute": "Operating Head Style", "source_row": 1096, "source_cell": "T1096"}


@pytest.mark.parametrize("smoke_only,smoke_first", [(True, False), (False, True)])
def test_unsupported_smoke_answer_is_recorded_without_gating_full_processing(smoke_only, smoke_first):
    store = seed_smoke_store()
    client = SmokeCompletion("Tee")
    run_quality_batch(store, "batch", "owner", "smoke-fail", completion=client,
                      smoke_only=smoke_only, smoke_first=smoke_first)
    summary = read_json(store, "quality-runs/batch/smoke-fail/summary.json")[0]
    assert summary["state"] == "completed" and summary["smoke"]["status"] == "no_grounded_candidate"
    assert summary["smoke"]["observed_candidates"][0]["value"] == "Tee"
    assert summary["smoke"]["candidates"] == []
    assert summary["smoke"]["rejected_candidates"][0]["reason"]
    assert bool(store.keys("results/")) is smoke_first
    assert sum(entry["tier"] == "vendor_table" for entry in summary["usage"]) <= 3


@pytest.mark.parametrize("multiple", [False, True])
def test_grounded_live_canary_disagreement_continues_to_full_extraction_and_judge(multiple):
    quote = "FLAT HEAD  INDICATING ARROW  DRILLED FOR WIRE SEAL"
    store = seed_smoke_store(head_description=quote)

    class Disagreeing(Completion):
        def complete_structured(self, system, user, schema, **kwargs):
            packet = json.loads(user)
            if schema == QualityExtraction:
                if packet.get("smoke_target"):
                    row = next(entry for entry in packet["evidence"] if entry["kind"] == "vendor_row")
                    self.responses.append([proposal(
                        attribute_id="Operating Head Style", value="Flat Head", supporting_quote=quote,
                        evidence_ids=[row["citation_id"]],
                        reviewer_explanation="Review head shape separately from the Lockwing feature.",
                    )])
                    if multiple:
                        self.responses[-1].append(proposal(
                            attribute_id="Operating Head Style", value="Lockwing",
                            supporting_quote="Lockwing", evidence_ids=[row["citation_id"]],
                        ))
                else:
                    self.responses.append([])
            return super().complete_structured(system, user, schema, **kwargs)

    client = Disagreeing()
    summary = run_quality_batch(store, "batch", "owner", "disagreement", completion=client, smoke_first=True)
    assert summary["state"] == "completed" and len(summary["products"]) == 1
    smoke = summary["smoke"]
    assert smoke["status"] == ("multiple_grounded_interpretations" if multiple else "disagreed_with_expectation")
    assert smoke["value"] == "Flat Head"
    assert smoke["source_cells"] == ["R1096"]
    assert smoke["expected_source"]["value"] == "Lockwing"
    assert smoke["expected_source"]["source_cells"] == ["T1096"]
    result = BatchService(store).detail("batch", "row-2", "owner")["machine_result"]
    head = next(attribute for attribute in result["attributes"] if attribute["attribute_id"] == "Operating Head Style")
    assert head["candidates"][0]["value"] == "Flat Head"
    assert head["candidates"][0]["judge_status"] == "accepted"
    assert len(head["candidates"]) == (2 if multiple else 1)
    assert "Review head style versus locking feature" in head["candidates"][0]["qualification"]
    assert sum(entry["tier"] == "vendor_table" for entry in summary["usage"]) == 3


def test_smoke_first_processes_all_four_in_one_execution_and_reuses_vendor_call():
    from collections import Counter
    store = seed_smoke_store()
    others, items = seed_store(4)
    record, revision = read_json(store, "batches/batch.json")
    record["items"].extend(items[1:])
    write_json(store, "batches/batch.json", record, revision)
    for item in items[1:]:
        key = f"items/batch/{item['item_key']}.json"
        write_json(store, key, read_json(others, key)[0])
    class Combined(Completion):
        def complete_structured(self, system, user, schema, **kwargs):
            packet = model_packet(user, kwargs)
            target = packet["product"]["item_id"] == "PIMITEM-213030"
            if packet.get("smoke_target"):
                row = next(entry for entry in packet["evidence"] if entry["kind"] == "vendor_row")
                generated = [proposal(attribute_id="Operating Head Style", value="Lockwing",
                                      supporting_quote="Lockwing", evidence_ids=[row["citation_id"]])]
            elif schema == QualityExtraction:
                assert read_json(store, "quality-runs/batch/one-execution/smoke.json")[0]["status"] == "passed"
                row = next((entry for entry in packet["evidence"] if entry["kind"] != "vendor_row"), None)
                generated = [] if target or row is None else [proposal(evidence_ids=[row["citation_id"]])]
            if schema == QualityExtraction:
                self.responses.append(generated)
            return super().complete_structured(system, user, schema, **kwargs)
    client = Combined()
    summary = run_quality_batch(store, "batch", "owner", "one-execution", completion=client, smoke_first=True)
    assert summary["state"] == "completed" and summary["smoke_first"] is True
    assert summary["smoke_only"] is False and summary["smoke"]["status"] == "passed"
    assert len(summary["products"]) == 4
    assert summary["model_calls"] == len(client.calls) == 9
    counts = Counter((entry["item_id"], entry["tier"]) for entry in summary["usage"])
    assert max(counts.values()) <= 3
    assert counts[("PIMITEM-213030", "vendor_table")] == 3
    first = BatchService(store).detail("batch", "row-2", "owner")["machine_result"]
    head = next(attribute for attribute in first["attributes"] if attribute["attribute_id"] == "Operating Head Style")
    assert len(head["candidates"]) == 1 and head["candidates"][0]["value"] == "Lockwing"
    assert head["candidates"][0]["judge_status"] == "accepted"
    assert first["quality_diagnostics"][0]["phase"] == "smoke"
    rows = read_workbook(BatchService(store).export("batch", "owner"))["Diagnostics"]
    calls = [row for row in rows if row["Operation"] == "model"]
    assert len(calls) == len({row["Call ID"] for row in calls}) == 9


def test_missing_smoke_cell_fails_without_spending():
    store = seed_smoke_store("Unknown")
    client = SmokeCompletion()
    with pytest.raises(QualitySmokeError, match="T1096 must contain Lockwing"):
        run_quality_batch(store, "batch", "owner", "smoke-source-fail", completion=client, smoke_only=True)
    assert not client.calls
    assert read_json(store, "quality-runs/batch/smoke-source-fail/summary.json")[0]["model_calls"] == 0


def test_failed_request_without_usage_is_durable_and_exported_without_fabricated_cost():
    store = seed_smoke_store()
    contexts = []
    class NoUsage(Completion):
        def complete_structured(self, *args, **kwargs):
            self.last_usage = {
                "model": None, "deployment": "gpt-6-sol", "status": "request_failed",
                "error_type": "APIConnectionError", "usage_reported": False,
                "input_tokens": None, "cached_input_tokens": None, "reasoning_tokens": None,
                "output_tokens": None, "estimated_cost_usd": None,
            }
            raise RuntimeError("No response")
    with pytest.raises(RuntimeError, match="No response"):
        run_quality_batch(store, "batch", "owner", "no-usage", completion=NoUsage(),
                          smoke_only=True, before_call=contexts.append)
    record = read_json(store, "quality-runs/batch/no-usage/usage/0001.json")[0]
    assert record["run_id"] == "no-usage" and record["call_id"] == "no-usage:0001"
    assert contexts[0]["call_id"] == record["call_id"]
    assert record["usage_reported"] is False and record["cost_usd"] is None and record["input_tokens"] is None
    assert record["recorded_at"] and not store.keys("results/")
    rows = read_workbook(BatchService(store).export("batch", "owner"))["Diagnostics"]
    assert len(rows) == 1
    assert rows[0]["Run ID"] == "no-usage" and rows[0]["Call ID"] == "no-usage:0001"
    assert rows[0]["Operation"] == "model" and rows[0]["Status"] == "request_failed"
    assert rows[0]["Input tokens"] == rows[0]["Cost USD"] == ""
    assert rows[0]["Usage reported"] == "False" and rows[0]["Error type"] == "APIConnectionError"
    assert rows[0]["Deployment"] == "gpt-6-sol"


@pytest.mark.parametrize("reported", [True, False])
def test_cost_snapshot_survives_response_failure_and_discloses_unknown_usage(reported):
    from backend.quality_cost import QualityCostMeter
    store = seed_smoke_store()
    ticks = [0]
    meter = QualityCostMeter(store, "batch", "priced-failure", run_base_cost_usd=".5",
                             overnight_prior_cost_usd="4", worker_usd_per_second=".01",
                             clock=lambda: ticks[0])
    class FailedResponse(Completion):
        def complete_structured(self, *args, **kwargs):
            ticks[0] = 20
            self.last_usage = {
                "model": "quality-model", "status": "response_invalid" if reported else "request_failed",
                "usage_reported": reported, "input_tokens": 100 if reported else None,
                "output_tokens": 20 if reported else None, "cached_input_tokens": 0 if reported else None,
                "reasoning_tokens": 10 if reported else None, "estimated_cost_usd": .25 if reported else None,
            }
            raise ValueError("Unusable response")
    with pytest.raises(ValueError, match="Unusable response"):
        run_quality_batch(store, "batch", "owner", "priced-failure", completion=FailedResponse(),
                          smoke_only=True, before_call=meter.before_call, usage_callback=meter.record,
                          cost_summary=meter.summary)
    summary = read_json(store, "quality-runs/batch/priced-failure/summary.json")[0]
    cost = summary["cost"]
    assert cost == read_json(store, "quality-runs/batch/priced-failure/cost.json")[0]
    assert cost["worker_seconds"] == 20 and cost["worker_cost_usd"] == pytest.approx(.2)
    assert cost["known_run_cost_usd"] == pytest.approx(.95 if reported else .7)
    assert cost["known_overnight_cost_usd"] == pytest.approx(4.95 if reported else 4.7)
    assert cost["unpriced_model_calls"] == (0 if reported else 1)
    assert summary["usage"][0]["cost_usd"] == (.25 if reported else None)
    assert summary["state"] == "failed" and cost["model_calls"] == 1


def test_per_product_web_tiers_are_not_fetched_and_web_yield_is_reported(monkeypatch):
    store, _ = seed_store(1)
    monkeypatch.setattr(CachedEvidenceLoader, "load", lambda *args: ([], [], []))

    class Web:
        def load(self, manifest, tier, pending):
            pytest.fail("Per-product web tiers must not be fetched")

    summary = run_quality_batch(store, "batch", "owner", "web-run", completion=Completion(), web=Web())
    assert summary["model_calls"] == 0 and summary["usage"] == []
    assert summary["web_yield"]["total"] == {"web_cost_usd": 0, "web_operations": 0,
                                             "accepted_web_values": 0, "accepted_per_usd": None}
    result = EnrichmentResult.model_validate(read_json(store, summary["products"][0]["result_key"])[0])
    assert {o.source_tier: o.status for o in result.retrieval}["manufacturer_web"] == "not_attempted"


def test_smoke_cli_env_does_not_initialize_web(monkeypatch):
    from backend.quality_worker import main
    calls = []
    monkeypatch.setenv("QUALITY_SMOKE_ONLY", "true")
    monkeypatch.setenv("QUALITY_WEB_ENABLED", "true")
    monkeypatch.setattr("backend.quality_worker.configured_store", Store)
    monkeypatch.setattr("backend.quality_worker.run_quality_batch", lambda *args, **kwargs: calls.append(kwargs) or {"state": "completed"})
    monkeypatch.setattr("backend.quality_web.QualityWeb", lambda *args, **kwargs: pytest.fail("Smoke cannot initialize WebIQ"))
    assert main(["--batch-id", "batch", "--owner", "owner", "--run-id", "smoke"]) == 0
    assert calls[0]["smoke_only"] is True and calls[0]["web"] is None
    assert calls[0]["smoke_first"] is False


def test_default_cli_runs_smoke_then_products_and_can_explicitly_skip_it(monkeypatch):
    from backend.quality_worker import main
    calls = []
    monkeypatch.delenv("QUALITY_SMOKE_ONLY", raising=False)
    monkeypatch.delenv("QUALITY_SMOKE_FIRST", raising=False)
    monkeypatch.setenv("QUALITY_WEB_ENABLED", "false")
    monkeypatch.setattr("backend.quality_worker.configured_store", Store)
    monkeypatch.setattr("backend.quality_worker.run_quality_batch", lambda *args, **kwargs: calls.append(kwargs) or {"state": "completed"})
    args = ["--batch-id", "batch", "--owner", "owner", "--run-id", "first"]
    assert main(args) == 0
    assert calls[-1]["smoke_first"] is True and calls[-1]["smoke_only"] is False
    assert main([*args, "--no-smoke-first"]) == 0
    assert calls[-1]["smoke_first"] is False


def test_cli_shared_cost_scope_accumulates_executions_without_rebilling_build(monkeypatch):
    from backend.quality_cost import QualityCostMeter
    from backend.quality_worker import main
    store = Store()
    ticks = [0]
    outputs = []
    monkeypatch.setenv("QUALITY_RUN_BASE_COST_USD", ".5")
    monkeypatch.setenv("QUALITY_OVERNIGHT_PRIOR_COST_USD", "4")
    monkeypatch.setenv("QUALITY_WORKER_USD_PER_SECOND", ".01")
    monkeypatch.setenv("QUALITY_COST_RUN_ID", "logical-run")
    monkeypatch.setenv("QUALITY_WEB_ENABLED", "false")
    monkeypatch.setattr("backend.quality_worker.configured_store", lambda: store)
    monkeypatch.setattr("backend.quality_cost.QualityCostMeter",
                        lambda *args, **kwargs: QualityCostMeter(*args, **kwargs, clock=lambda: ticks[0]))
    def fake_worker(store, batch_id, owner, run_id, **kwargs):
        event = {"operation": "model", "run_id": run_id, "cost_usd": .25}
        kwargs["before_call"](event)
        ticks[0] += 10
        priced = kwargs["usage_callback"](event)
        result = {"state": "completed", "cost": kwargs["cost_summary"](), "usage": [priced]}
        outputs.append(result)
        return result
    monkeypatch.setattr("backend.quality_worker.run_quality_batch", fake_worker)
    for run_id in ("execution-1", "execution-2"):
        assert main(["--batch-id", "batch", "--owner", "owner", "--run-id", run_id]) == 0
    cost = read_json(store, "quality-runs/batch/logical-run/cost.json")[0]
    assert cost["run_base_cost_usd"] == .5 and cost["model_calls"] == 2
    assert cost["worker_seconds"] == 20 and cost["worker_cost_usd"] == pytest.approx(.2)
    assert cost["known_run_cost_usd"] == pytest.approx(1.2)
    assert cost["known_overnight_cost_usd"] == pytest.approx(5.2)
    assert outputs[-1]["cost"]["cost_run_id"] == "logical-run"
    assert outputs[-1]["cost"]["worker_rate_configured"] is True
    assert outputs[-1]["cost"]["worker_usd_per_second"] == .01
    assert outputs[-1]["cost"]["web_browse_usd_per_call"] == .0125
    assert all(output["usage"][0]["cost_run_id"] == "logical-run" for output in outputs)


def test_cli_flushes_elapsed_cost_when_worker_initialization_fails(monkeypatch, capsys):
    from backend.quality_cost import QualityCostMeter
    from backend.quality_worker import main
    store = Store()
    ticks = [0]
    monkeypatch.delenv("QUALITY_COST_RUN_ID", raising=False)
    monkeypatch.setenv("QUALITY_RUN_BASE_COST_USD", ".5")
    monkeypatch.setenv("QUALITY_WORKER_USD_PER_SECOND", ".01")
    monkeypatch.setenv("QUALITY_WEB_ENABLED", "false")
    monkeypatch.setattr("backend.quality_worker.configured_store", lambda: store)
    monkeypatch.setattr("backend.quality_cost.QualityCostMeter",
                        lambda *args, **kwargs: QualityCostMeter(*args, **kwargs, clock=lambda: ticks[0]))
    def fail_worker(*args, **kwargs):
        ticks[0] += 10
        raise RuntimeError("Cannot load stored batch")
    monkeypatch.setattr("backend.quality_worker.run_quality_batch", fail_worker)
    with pytest.raises(RuntimeError, match="Cannot load stored batch"):
        main(["--batch-id", "batch", "--owner", "owner", "--run-id", "initialization-failure"])
    cost = read_json(store, "quality-runs/batch/initialization-failure/cost.json")[0]
    assert cost["model_calls"] == 0 and cost["worker_seconds"] == 10
    assert cost["worker_cost_usd"] == pytest.approx(.1)
    assert cost["known_run_cost_usd"] == pytest.approx(.6)
    assert json.loads(capsys.readouterr().out)["quality_cost"] == cost


@pytest.mark.parametrize("priced", [True, False])
def test_cache_write_usage_and_price_basis_survive_meter_and_exports(priced):
    from backend.quality_cost import QualityCostMeter
    store, _ = seed_store(1)
    basis = "Synthetic public rate estimate; not finalized billing"
    class CachedUsage(Completion):
        def complete_structured(self, *args, **kwargs):
            response = super().complete_structured(*args, **kwargs)
            self.last_usage.update(
                cache_write_tokens=15, pricing_basis=basis, usage_reported=True,
                cost_usd=.001 if priced else None, estimated_cost_usd=.001 if priced else None,
            )
            return response
    meter = QualityCostMeter(store, "batch", "cache-pricing", clock=lambda: 0)
    summary = run_quality_batch(
        store, "batch", "owner", "cache-pricing",
        completion=CachedUsage([[proposal(evidence_ids=["E1"])]]),
        before_call=meter.before_call, usage_callback=meter.record, cost_summary=meter.summary,
    )
    assert summary["state"] == "completed" and summary["model_calls"] == 2
    assert all(entry["cache_write_tokens"] == 15 and entry["pricing_basis"] == basis for entry in summary["usage"])
    assert all(entry["cost_usd"] == (.001 if priced else None) for entry in summary["usage"])
    assert summary["cost"]["model_cost_usd"] == pytest.approx(.002 if priced else 0)
    assert summary["cost"]["unpriced_model_calls"] == (0 if priced else 2)
    technical = read_workbook(BatchService(store).export("batch", "owner"))["Diagnostics"]
    technical = [row for row in technical if row["Operation"] == "model"]
    assert all(row["Cache write tokens"] == "15" and row["Pricing basis"] == basis for row in technical)
    result = EnrichmentResult.model_validate(BatchService(store).detail("batch", "row-2", "owner")["machine_result"])
    public = read_workbook(build_reviewer_package([result]).workbook)
    assert "Diagnostics" not in public
    result.attributes[0].reviewer_explanation = "Synthetic\x01explanation"
    sanitized = read_workbook(build_reviewer_package([result]).workbook)
    assert len(sanitized) == 5
    assert sanitized["Review"][0]["Reviewer explanation"] == "Synthetic explanation"


def test_same_run_manual_retry_appends_usage_and_immutable_execution_results():
    from backend.quality_cost import QualityCostMeter
    store, _ = seed_store(1)
    summaries = []
    first_records = None
    for execution_id in ("execution-one", "execution-two"):
        meter = QualityCostMeter(store, "batch", "retry-run", run_base_cost_usd=".5", clock=lambda: 0)
        result = run_quality_batch(
            store, "batch", "owner", "retry-run", execution_id=execution_id,
            completion=Completion([[proposal(evidence_ids=["E1"])]]),
            usage_callback=meter.record, before_call=meter.before_call, cost_summary=meter.summary,
        )
        summaries.append(result)
        if first_records is None:
            first_records = {key: value for key, value in store.data.items()
                             if key.startswith("results/") or "/executions/execution-one/" in key or "/usage/" in key}
    assert summaries[0]["execution_id"] == "execution-one"
    assert summaries[1]["execution_id"] == "execution-two"
    assert summaries[1]["execution_model_calls"] == 2 and summaries[1]["model_calls"] == 4
    assert [entry["call_id"] for entry in summaries[1]["usage"]] == [
        "retry-run:0001", "retry-run:0002", "retry-run:0003", "retry-run:0004",
    ]
    assert {entry["execution_id"] for entry in summaries[1]["usage"]} == {"execution-one", "execution-two"}
    assert summaries[1]["cost"]["known_run_cost_usd"] == pytest.approx(.504)
    assert all(store.data[key] == value for key, value in first_records.items())
    assert summaries[0]["products"][0]["result_key"] != summaries[1]["products"][0]["result_key"]
    assert read_json(store, "quality-runs/batch/retry-run/summary.json")[0] == summaries[1]
    assert read_json(store, "quality-runs/batch/retry-run/executions/execution-one/summary.json")[0] == summaries[0]
    detail = BatchService(store).detail("batch", "row-2", "owner")
    assert len(detail["attempt_history"]) == 3
    rows = read_workbook(BatchService(store).export("batch", "owner"))["Diagnostics"]
    rows = [row for row in rows if row["Operation"] == "model"]
    assert len(rows) == 4 and len({row["Call ID"] for row in rows}) == 4
    assert {row["Execution ID"] for row in rows} == {"execution-one", "execution-two"}


def test_failed_manual_retry_preserves_prior_run_product_references():
    store, _ = seed_store(1)
    first = run_quality_batch(
        store, "batch", "owner", "retry-products", execution_id="successful",
        completion=Completion([[proposal(evidence_ids=["E1"])]]),
    )
    with pytest.raises(QualitySmokeError, match="Mueller 213030"):
        run_quality_batch(store, "batch", "owner", "retry-products", execution_id="failed",
                          completion=Completion(), smoke_only=True)
    latest = read_json(store, "quality-runs/batch/retry-products/summary.json")[0]
    assert latest["state"] == "failed" and latest["execution_products"] == []
    assert latest["products"] == first["products"]
    assert latest["model_calls"] == 2 and latest["execution_model_calls"] == 0
    assert read_json(store, "quality-runs/batch/retry-products/executions/successful/summary.json")[0] == first
