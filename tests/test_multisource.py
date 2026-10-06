import json
from datetime import datetime, timezone

import pytest

from backend.models.enrichment import (
    AttributeDefinition, Candidate, Evidence, ExtractionResponse, Manifest,
    OfflineSource, ProductKey,
)
from backend.multisource import OptionalTierSkipped, run_cascade


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*args, **kwargs):
        raise AssertionError("Synthetic cascade tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def manifest():
    return Manifest(
        product=ProductKey(item_id="001", vendor="Synthetic", mpn="PART-001", hierarchy_node="Valves"),
        attributes=[
            AttributeDefinition(attribute_id=name, description=name, value_type="string")
            for name in ("Body Material", "Inlet", "Outlet", "Unsupported")
        ],
    )


def source(manifest, tier, text, name=None):
    name = name or tier
    return OfflineSource(
        source_id=name, product=manifest.product, source_tier=tier,
        excerpts=[Evidence(
            evidence_id=name + ":v1", source_id=name,
            source_locator=f"https://synthetic.invalid/{name}#row=2",
            source_version="sha256:synthetic", source_tier=tier,
            content_kind="source_excerpt", text=text,
            observed_at=datetime.now(timezone.utc),
            attribute_ids=[definition.attribute_id for definition in manifest.attributes],
            qualification="Exact synthetic product, row-specific support only.",
        )],
    )


class Completion:
    def __init__(self):
        self.calls = []

    def complete_structured(self, system, user, schema):
        payload = json.loads(user)
        self.calls.append(payload)
        candidates = []
        for definition in payload["attributes"]:
            name = definition["attribute_id"]
            for evidence in payload["evidence"]:
                for part in evidence["text"].split("; "):
                    if part.startswith(name + ": "):
                        candidates.append(Candidate(
                            attribute_id=name, value=part.removeprefix(name + ": "),
                            evidence_ids=[evidence["evidence_id"]], supporting_quote=part,
                            qualification="Exact synthetic product and named component.",
                        ))
        return ExtractionResponse(candidates=candidates)


def test_missing_definition_unit_is_customer_clarification_not_extraction(manifest):
    manifest.attributes.append(AttributeDefinition(
        attribute_id="Pressure Rating", description="Pressure", value_type="number", unit_resolved=False,
    ))
    completion = Completion()
    result = run_cascade(
        manifest, lambda tier, pending: [source(manifest, tier, "Body Material: brass")], completion,
    )
    pressure = next(attribute for attribute in result.attributes if attribute.attribute_id == "Pressure Rating")
    assert pressure.status == "definition_clarification_needed"
    assert "Confirm the expected unit for Pressure Rating" in pressure.definition_clarification
    assert not pressure.candidates
    assert all(attribute["attribute_id"] != "Pressure Rating"
               for call in completion.calls for attribute in call["attributes"])


def test_cascade_only_requests_remaining_attributes_and_keeps_all_sources(manifest):
    completion = Completion()
    requests = []
    texts = {
        "internal_pdf": "Body Material: brass",
        "vendor_table": "Inlet: threaded",
        "manufacturer_web": "Outlet: swivel",
    }

    def load(tier, pending):
        requests.append((tier, pending))
        return [source(manifest, tier, texts[tier])] if tier in texts else []

    result = run_cascade(manifest, load, completion)
    assert [attribute.status for attribute in result.attributes] == ["proposed"] * 3 + ["missing_evidence"]
    assert requests[1][1] == ["Inlet", "Outlet", "Unsupported"]
    assert requests[2][1] == ["Outlet", "Unsupported"]
    assert requests[3][1] == ["Unsupported"]
    assert [item.source_tier for item in result.evidence] == ["internal_pdf", "vendor_table", "manufacturer_web"]
    assert len(completion.calls) == 3
    assert result.model_call_status == "succeeded"


def test_source_failure_does_not_discard_independent_evidence(manifest):
    def load(tier, pending):
        if tier == "internal_pdf":
            return [OfflineSource(source_id="missing", product=manifest.product, error_code="not_found"),
                    source(manifest, tier, "Body Material: brass")]
        return []

    result = run_cascade(manifest, load, Completion())
    assert result.attributes[0].status == "proposed"
    assert result.attributes[1].status == "retrieval_failed"
    assert result.retrieval[0].status == "failed"


def test_supported_conflicts_remain_conflicted_across_fallback(manifest):
    def load(tier, pending):
        if tier == "internal_pdf":
            return [source(manifest, tier, "Body Material: brass; Body Material: bronze")]
        if tier == "vendor_table":
            return [source(manifest, tier, "Body Material: brass; Inlet: threaded")]
        return []

    result = run_cascade(manifest, load, Completion())
    assert result.attributes[0].status == "conflict"
    assert {candidate.value for candidate in result.attributes[0].candidates} == {"brass", "bronze"}
    assert result.attributes[1].status == "proposed"


def test_wrong_product_and_out_of_scope_values_are_not_accepted(manifest):
    wrong = source(manifest, "internal_pdf", "Body Material: brass")
    wrong.product = manifest.product.model_copy(update={"mpn": "PART-002"})
    completion = Completion()
    result = run_cascade(manifest, lambda tier, _: [wrong] if tier == "internal_pdf" else [], completion)
    assert not completion.calls
    assert not result.evidence
    assert result.retrieval[0].error_code == "product_mismatch"
    scoped = source(manifest, "internal_pdf", "Body Material: brass")
    scoped.excerpts[0].attribute_ids = ["Inlet"]
    result = run_cascade(manifest, lambda tier, _: [scoped] if tier == "internal_pdf" else [], completion)
    assert result.attributes[0].status == "extraction_failed"
    assert result.extraction_error == "invalid_response"


def test_existing_values_and_missing_all_sources_do_not_trigger_model(manifest):
    manifest.existing_values = {"Body Material": "existing"}
    completion = Completion()
    result = run_cascade(manifest, lambda *_: [], completion)
    assert not completion.calls
    assert result.attributes[0].status == "existing"
    assert result.manifest.existing_values == {"Body Material": "existing"}
    assert result.skip_reason == "no_eligible_evidence"


def test_unqualified_response_is_not_a_real_proposal(manifest):
    class Unqualified:
        def complete_structured(self, system, user, schema):
            return ExtractionResponse(candidates=[Candidate(
                attribute_id="Body Material", value="brass", evidence_ids=["internal_pdf:v1"],
            )])

    result = run_cascade(
        manifest,
        lambda tier, _: [source(manifest, tier, "Body Material: brass")] if tier == "internal_pdf" else [],
        Unqualified(),
    )
    assert not result.attributes[0].candidates
    assert result.extraction_error == "invalid_response"


def test_descriptive_boolean_remains_pending_until_literal_support(manifest):
    manifest.attributes = [AttributeDefinition(attribute_id="Lead-Free", description="Lead-Free", value_type="boolean")]
    requests = []

    class BooleanCompletion:
        def complete_structured(self, system, user, schema):
            evidence = json.loads(user)["evidence"][0]
            return ExtractionResponse(candidates=[Candidate(
                attribute_id="Lead-Free", value=True, evidence_ids=[evidence["evidence_id"]],
                supporting_quote=evidence["text"], qualification="Exact synthetic product.",
            )])

    def load(tier, pending):
        requests.append((tier, pending))
        if tier == "internal_pdf":
            supplied = source(manifest, tier, "Lead-free brass valve")
            supplied.excerpts[0].qualification = "Exact synthetic product."
            return [supplied]
        if tier == "manufacturer_web":
            return [source(manifest, tier, "Lead-Free: Yes")]
        return []

    result = run_cascade(manifest, load, BooleanCompletion())
    assert requests == [
        ("internal_pdf", ["Lead-Free"]), ("vendor_table", ["Lead-Free"]),
        ("manufacturer_web", ["Lead-Free"]),
    ]
    assert [candidate.evidence_basis for candidate in result.attributes[0].candidates] == [
        "inferred_from_description", "literal",
    ]
    assert result.attributes[0].status == "proposed"


def test_optional_skip_preserves_only_product_matched_original_evidence(manifest):
    good = source(manifest, "manufacturer_web", "Body Material: brass", "original")
    wrong = source(manifest, "manufacturer_web", "Body Material: gold", "wrong")
    wrong.product = manifest.product.model_copy(update={"mpn": "OTHER-PART"})

    class Skip:
        def complete_structured(self, system, user, schema):
            raise OptionalTierSkipped("optional_complete_input_capacity")

    result = run_cascade(
        manifest, lambda tier, _: [good, wrong] if tier == "manufacturer_web" else [], Skip(),
    )
    assert [entry.source_id for entry in result.evidence] == ["original"]
    assert not any(attribute.candidates for attribute in result.attributes)
    assert any(entry.error_code == "optional_complete_input_capacity" for entry in result.retrieval)


@pytest.mark.parametrize("name,value,kind,quote,unit", [
    ("Maximum Pressure", 100, "number", "100 PSI working pressure", "PSI"),
    ("Primary Material", "EPDM", "string", "EPDM O-ring", None),
    ("Lead-Free", False, "boolean", "No lead in potable components", None),
])
def test_scope_and_semantic_shortcuts_are_rejected(manifest, name, value, kind, quote, unit):
    from backend.extract import validate_response

    manifest.attributes = [AttributeDefinition(attribute_id=name, description=name, value_type=kind, unit=unit)]
    supplied = source(manifest, "internal_pdf", quote)
    response = ExtractionResponse(candidates=[Candidate(
        attribute_id=name, value=value, unit=unit, evidence_ids=["internal_pdf:v1"],
        supporting_quote=quote, qualification="Synthetic claim requiring semantic validation.",
    )])
    with pytest.raises(ValueError):
        validate_response(response, manifest, supplied.excerpts)
