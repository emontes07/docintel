from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.batch import digest
from backend.core.docintel import ParsedDocument, ParsedParagraph, ParsedTable
from backend.models.enrichment import AttributeDefinition, Evidence, Manifest, ProductKey
from backend.quality_pipeline import QualityProposal, ground_candidate
from backend.quality_worker import CachedEvidenceLoader, scope_document

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
PRODUCT = ProductKey(item_id="P1", vendor="Synthetic", mpn="MODEL-A", hierarchy_node="Valves")


def document(rows):
    return ParsedDocument(
        source="batchblob:///synthetic.pdf", cache_key="sha256:" + "a" * 64, parsed_at=NOW,
        tables=[ParsedTable(page_number=1, row_count=len(rows), column_count=2, cells=rows)],
        paragraphs=[
            ParsedParagraph(text="MODEL-C uses steel", page_number=1),
            ParsedParagraph(text="All models use the same operating principle.", page_number=1),
        ],
    )


@pytest.mark.parametrize("bound_sibling", [False, True])
def test_unlisted_catalog_variants_are_removed_with_their_paragraphs(bound_sibling):
    doc = document([["PART NUMBER", "MATERIAL"], ["MODEL-A", "Brass"], ["MODEL-B", "Bronze"], ["MODEL-C", "Steel"]])
    products = [PRODUCT.model_dump()]
    if bound_sibling:
        products.append(PRODUCT.model_copy(update={"item_id": "P2", "mpn": "MODEL-B"}).model_dump())
    scoped = scope_document(doc, PRODUCT, {"products": products})
    assert scoped.tables[0].cells == [["PART NUMBER", "MATERIAL"], ["MODEL-A", "Brass"], ["", ""], ["", ""]]
    assert scoped.paragraphs[0].text == ""
    assert scoped.paragraphs[1].text.startswith("All models")
    assert doc.tables[0].cells[-1] == ["MODEL-C", "Steel"]


def test_explicit_model_table_without_target_cannot_supply_foreign_rows():
    scoped = scope_document(document([["Model Number", "Material"], ["MODEL-C", "Steel"]]), PRODUCT, {"products": [PRODUCT.model_dump()]})
    assert scoped.tables[0].cells == [["Model Number", "Material"], ["", ""]]


def test_shared_component_table_is_not_mistaken_for_catalog_variants():
    doc = document([["Component", "Material"], ["Body", "Brass"], ["Seat", "EPDM"]])
    assert scope_document(doc, PRODUCT, {"products": [PRODUCT.model_dump()]}).tables == doc.tables


@pytest.mark.parametrize("source_format", ["pdf", "xlsx"])
def test_empty_attribute_scope_stays_empty_through_loading(monkeypatch, source_format):
    manifest = Manifest(product=PRODUCT, attributes=[AttributeDefinition(attribute_id="Material", description="", value_type="string")])
    raw = b"synthetic workbook"
    store = SimpleNamespace(read_bytes=lambda *args, **kwargs: (raw, "v"))
    loader = CachedEvidenceLoader(store)
    monkeypatch.setattr(loader, "vendor_index", lambda binding, content: {"workbook": {}, "mpn_rows": {}})
    monkeypatch.setattr(loader, "cached_document", lambda binding: document([["Component", "Material"], ["Body", "Brass"]]))
    monkeypatch.setattr("backend.quality_worker.read_vendor_table", lambda *args, **kwargs: [
        SimpleNamespace(sheet="Sheet", row=2, cells={"B2": "Brass"}, text='{"row":2,"cells":[{"cell":"B2","value":"Brass"}]}'),
    ])
    binding = {
        "source_id": "synthetic", "blob": "synthetic." + source_format, "kind": "blob", "format": source_format,
        "sha256": digest(raw), "products": [PRODUCT.model_dump()],
        "source_tier": "internal_pdf" if source_format == "pdf" else "vendor_table",
        "applicability": [{"product": PRODUCT.model_dump(), "attribute_ids": []}],
        "table": {"sheet": "Sheet", "mpn_column": "MPN", "expected_vendor": PRODUCT.vendor},
    }
    evidence, retrieval, _ = loader.load({"manifest": manifest.model_dump(mode="json"), "sources": [binding]})
    assert retrieval[0].status == "success"
    assert evidence and all(entry.attribute_ids == [] for entry in evidence)


@pytest.mark.parametrize(("text", "unit", "value"), [
    ("100 pounds per square inch", "psi", 100),
    ("90°", "degree", 90),
    ("100 millimetres", "mm", 100),
])
def test_supported_source_unit_aliases_remain_grounded_and_record_normalization(text, unit, value):
    source = Evidence(
        evidence_id="E1", source_id="source", source_tier="internal_pdf", content_kind="source_excerpt",
        source_locator="https://example.com/catalog#page=1&paragraph=0", source_version="v1",
        text=text, observed_at=NOW,
    )
    candidate = ground_candidate(
        QualityProposal(attribute_id="Test", value=value, unit=unit, evidence_ids=["E1"], supporting_quote=text),
        AttributeDefinition(attribute_id="Test", description="", value_type="number", unit=unit), [source],
    )
    assert candidate.unit == unit
    assert candidate.origin == "derived"
    assert candidate.normalization_rule == "unit_alias_v1"


def test_temperature_degree_sign_is_not_an_angle_unit():
    source = Evidence(
        evidence_id="E1", source_id="source", source_tier="internal_pdf", content_kind="source_excerpt",
        source_locator="https://example.com/catalog#page=1&paragraph=0", source_version="v1",
        text="90°C", observed_at=NOW,
    )
    with pytest.raises(ValueError, match="unit"):
        ground_candidate(
            QualityProposal(attribute_id="Test", value=90, unit="degree", evidence_ids=["E1"], supporting_quote="90°C"),
            AttributeDefinition(attribute_id="Test", description="", value_type="number", unit="degree"), [source],
        )


@pytest.mark.parametrize(("quoted", "expanded"), [
    ("LLB", "low lead brass"),
    ("LLB", "brass"),
    ("FIP", "female iron pipe"),
    ("EPDM", "ethylene propylene diene monomer"),
])
def test_documented_abbreviations_can_expand_without_inventing_values(quoted, expanded):
    source = Evidence(
        evidence_id="E1", source_id="source", source_tier="internal_pdf", content_kind="source_excerpt",
        source_locator="https://example.com/catalog#page=1&paragraph=0", source_version="v1",
        text=quoted, observed_at=NOW,
    )
    definition = AttributeDefinition(attribute_id="Test", description="", value_type="string")
    proposal = QualityProposal(
        attribute_id="Test", value=expanded, evidence_ids=["E1"], supporting_quote=quoted,
        origin="derived", normalization_rule=f"Expand {quoted} to {expanded}.",
    )
    assert ground_candidate(proposal, definition, [source]).value == expanded
    with pytest.raises(ValueError, match="value is not grounded"):
        ground_candidate(proposal.model_copy(update={"value": "invented stainless steel"}), definition, [source])


@pytest.mark.parametrize(("value", "answer"), [(True, "Yes"), (True, "True"), (False, "No"), (False, "False")])
def test_explicit_labeled_boolean_answers_remain_literal(value, answer):
    text = "Lead-Free: " + answer
    source = Evidence(
        evidence_id="E1", source_id="source", source_tier="internal_pdf", content_kind="source_excerpt",
        source_locator="https://example.com/catalog#page=1&paragraph=0", source_version="v1",
        text=text, observed_at=NOW,
    )
    definition = AttributeDefinition(attribute_id="Lead-Free", description="", value_type="boolean")
    candidate = ground_candidate(QualityProposal(
        attribute_id="Lead-Free", value=value, evidence_ids=["E1"], supporting_quote=text,
    ), definition, [source])
    assert candidate.origin == "literal"
    assert candidate.evidence_basis == "literal"


@pytest.mark.parametrize("origin", ["literal", "derived", "inferred"])
def test_description_cannot_bypass_lead_free_review_by_changing_origin(origin):
    source = Evidence(
        evidence_id="E1", source_id="source", source_tier="internal_pdf", content_kind="source_excerpt",
        source_locator="https://example.com/catalog#page=1&paragraph=0", source_version="v1",
        text="Lead-free brass valve", observed_at=NOW,
    )
    candidate = ground_candidate(QualityProposal(
        attribute_id="Lead-Free", value=True, evidence_ids=["E1"], supporting_quote=source.text,
        origin=origin, normalization_rule="Convert description to Boolean." if origin == "derived" else None,
    ), AttributeDefinition(attribute_id="Lead-Free", description="", value_type="boolean"), [source])
    assert candidate.origin == "inferred"
    assert candidate.evidence_basis == "inferred_from_description"
    assert candidate.justification and "requires review" in candidate.qualification.lower()


@pytest.mark.parametrize("attribute,text", [
    ("Locking Feature", "LOCKWING"),
    ("Locking Feature", "Padlock wing for locking the valve in a closed position"),
    ("Padlock Wing", "Padlock wing for locking the valve in a closed position"),
])
@pytest.mark.parametrize("origin", ["literal", "derived", "inferred"])
def test_reproduced_feature_descriptions_are_review_only_inferences(attribute, text, origin):
    source = Evidence(
        evidence_id="E1", source_id="source", source_tier="internal_pdf", content_kind="source_excerpt",
        source_locator="https://example.com/catalog#page=1&paragraph=0", source_version="v1",
        text=text, observed_at=NOW,
    )
    candidate = ground_candidate(QualityProposal(
        attribute_id=attribute, value=True, evidence_ids=["E1"], supporting_quote=text, origin=origin,
    ), AttributeDefinition(attribute_id=attribute, description="", value_type="boolean"), [source])
    assert candidate.origin == "inferred"
    assert candidate.evidence_basis == "inferred_from_description"
    assert candidate.inference_rule == "quoted_feature_presence_v1"
    assert candidate.justification and "requires review" in candidate.qualification.lower()
    assert candidate.judge_status == "not_judged"


@pytest.mark.parametrize("attribute,text,value", [
    ("Padlock Wing", "LOCKWING", True),
    ("Padlock Wing", "Padlock Wing: N", True),
    ("Locking Feature", "Locking Feature", True),
    ("Locking Feature", "No LOCKWING", True),
    ("Locking Feature", "Optional LOCKWING", True),
    ("Locking Feature", "LOCKWING or flat head", True),
    ("Padlock Wing", "Replacement padlock wing for locking the valve", True),
    ("Padlock Wing", "Padlock wing for locking the valve in a closed position", False),
    ("Flanged Outlet", "Female pipe thread", False),
])
def test_feature_inference_does_not_broaden_absence_roles_or_alternatives(attribute, text, value):
    source = Evidence(
        evidence_id="E1", source_id="source", source_tier="internal_pdf", content_kind="source_excerpt",
        source_locator="https://example.com/catalog#page=1&paragraph=0", source_version="v1",
        text=text, observed_at=NOW,
    )
    with pytest.raises(ValueError):
        ground_candidate(QualityProposal(
            attribute_id=attribute, value=value, evidence_ids=["E1"], supporting_quote=text,
            origin="inferred", justification="Proposed interpretation.",
        ), AttributeDefinition(attribute_id=attribute, description="", value_type="boolean"), [source])


@pytest.mark.parametrize("origin", ["literal", "derived", "inferred"])
def test_explicit_negative_lead_answer_cannot_be_reinterpreted_as_positive_description(origin):
    source = Evidence(
        evidence_id="E1", source_id="source", source_tier="internal_pdf", content_kind="source_excerpt",
        source_locator="https://example.com/catalog#page=1&paragraph=0", source_version="v1",
        text="Lead-Free: No", observed_at=NOW,
    )
    with pytest.raises(ValueError):
        ground_candidate(QualityProposal(
            attribute_id="Lead-Free", value=True, evidence_ids=["E1"], supporting_quote=source.text,
            origin=origin, normalization_rule="Convert description." if origin == "derived" else None,
            justification="Lead-Free wording.",
        ), AttributeDefinition(attribute_id="Lead-Free", description="", value_type="boolean"), [source])
