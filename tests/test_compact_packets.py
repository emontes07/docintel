"""PR A: compact model-facing packets, shared cache prefix and per-phase output limits."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.models.enrichment import AttributeDefinition, EnrichmentResult, Evidence, Manifest, ProductKey
from backend.quality_cost import CostLimitExceeded, QualityCostMeter
from backend.quality_definitions import DEFINITION_RULES, derive_definition
from backend.quality_pipeline import (
    EXTRACT_TASK, JUDGE_TASK, SHARED_SYSTEM, QualityExtraction, QualityJudgment, cached_packet_parts,
    complete_quality_call, expand_citations, QualityProposal, product_packet, run_product,
    shared_source_ids, source_family,
)

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
RETAINED = Path(__file__).resolve().parents[1] / "output/private/phase3/final-a/results.json"
HASH = re.compile(r"[0-9a-f]{40,}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
NAMES = ["Manufacturer", "Primary Material", "Seal / Softgoods Material"]


def manifest():
    return Manifest(
        product=ProductKey(item_id="P1", vendor="Ford", mpn="AV11-333W-NL", hierarchy_node="Angle Valves"),
        attributes=[AttributeDefinition(attribute_id=name, description=name, value_type="string",
                                        type_guidance="Enumerated", definition_context="REFERENCE_SOURCE_BASIS")
                    for name in NAMES],
    )


def pdf(key, locator, text, attribute_ids=None, qualification="Exact association. Family wording requires review."):
    return Evidence(
        evidence_id=f"pdf:sha256:{'a' * 64}:{key}", source_id="catalog",
        source_locator="batchblob:///documents/catalog.pdf#" + locator, source_version="sha256:" + "a" * 64,
        source_tier="internal_pdf", content_kind="source_excerpt", text=text, observed_at=NOW,
        provider_retrieved_at=NOW, attribute_ids=attribute_ids or NAMES, qualification=qualification,
    )


def vendor():
    return Evidence(
        evidence_id=f"vendor:{'b' * 64}:Sheet:7", source_id="vendor", source_tier="vendor_table",
        source_locator="batchblob:///documents/vendor.xlsx#sheet=Sheet&row=7&cells=A7,B7",
        source_version="sha256:" + "b" * 64, content_kind="source_excerpt", observed_at=NOW, provider_retrieved_at=NOW,
        text=json.dumps({"sheet": "Sheet", "row": 7, "cells": [
            {"cell": "A7", "column": "Part #", "value": "AV11-333W-NL"},
            {"cell": "B7", "column": "Description", "value": "LOW LEAD BRASS"},
        ]}), attribute_ids=NAMES, qualification="Exact vendor row.",
    )


def local():
    return [
        pdf("p0", "page=1&paragraph=0&role=manufacturerTitleBlock", "DRFTR CHKR THIRD ANGLE PROJECTION Mueller Co.",
            ["Manufacturer"], "Exact association. Family wording requires review. Title block: identity only."),
        pdf("t00", "page=1&table=0&row=0&column=0", "NO"),
        pdf("t01", "page=1&table=0&row=0&column=1", "DESCRIPTION"),
        pdf("t02", "page=1&table=0&row=0&column=2", "MATERIAL"),
        pdf("t10", "page=1&table=0&row=1&column=0", "4"),
        pdf("t11", "page=1&table=0&row=1&column=1", "O-RING"),
        pdf("t12", "page=1&table=0&row=1&column=2", "EPDM ASTM D2000"),
        vendor(),
    ]


def test_model_view_has_no_hashes_timestamps_or_repeated_scope_and_keeps_cues():
    evidence = local()
    packet = product_packet(manifest(), evidence, "internal_pdf", NAMES, tier_only=True)
    prefix, view = cached_packet_parts(packet)
    text = prefix + json.dumps(view, ensure_ascii=False)
    assert not HASH.search(text) and not TIMESTAMP.search(text)
    assert "REFERENCE_SOURCE_BASIS" not in text  # raw definition context is not sent
    assert "evidence_id" not in text and "attribute_ids" not in text and "source_version" not in text
    assert set(json.loads(prefix)) == {"definitions"}
    assert all(set(d) >= {"attribute_id", "expected_type", "derivation_rule"} for d in json.loads(prefix)["definitions"])
    assert "normalization_rule" not in prefix and DEFINITION_RULES in SHARED_SYSTEM
    assert [entry["kind"] for entry in view["evidence"]] == ["title_block", "table_row"]  # vendor row excluded
    title, row = view["evidence"]
    assert title["document_role"] == "manufacturerTitleBlock" and title["identity_only"] is True
    assert title["limited_to"] == ["Manufacturer"] and "identity only" in title["qualification"]
    assert row["header_labels"] == ["NO", "DESCRIPTION", "MATERIAL"] and "MATERIAL=EPDM ASTM D2000" in row["text"]
    assert "qualification" not in row and "limited_to" not in row
    assert view["sources"]["D1"]["qualification"] == "Exact association. Family wording requires review."
    assert text.count("Exact association. Family wording requires review.") == 2  # source table + title delta
    expanded = expand_citations(QualityProposal(
        attribute_id="Seal / Softgoods Material", value="Other: EPDM ASTM D2000", evidence_ids=[row["citation_id"]],
        supporting_quote="EPDM ASTM D2000"), packet)
    assert {key.rsplit(":", 1)[1] for key in expanded.evidence_ids} == {"t00", "t01", "t02", "t10", "t11", "t12"}


def test_vendor_tier_packet_is_only_its_row_plus_identity_header():
    packet = product_packet(manifest(), local(), "vendor_table", NAMES, tier_only=True)
    _, view = cached_packet_parts(packet)
    assert [entry["kind"] for entry in view["evidence"]] == ["vendor_row"]
    assert '"column": "Description"' in view["evidence"][0]["text"]
    assert view["product"] == {"item_id": "P1", "mpn": "AV11-333W-NL", "vendor": "Ford", "hierarchy_node": "Angle Valves"}
    assert "image_instruction" not in view


class Recorder:
    effort, max_output_tokens = "medium", 16000

    def __init__(self):
        self.calls = []

    def complete_structured(self, system, user, schema, **kwargs):
        self.calls.append((system, json.loads(user), schema, kwargs))
        if schema is QualityJudgment:
            return QualityJudgment(decisions=[{"candidate_id": c["candidate_id"], "decision": "accepted",
                                               "reason": "Supported."} for c in json.loads(user)["candidates"]])
        if len(self.calls) == 1:
            row = next(e for e in json.loads(user)["evidence"] if e["kind"] == "table_row")
            return QualityExtraction(candidates=[QualityProposal(
                attribute_id="Seal / Softgoods Material", value="Other: EPDM ASTM D2000",
                evidence_ids=[row["citation_id"]], supporting_quote="EPDM ASTM D2000", origin="derived",
                normalization_rule="exact_enum_or_explicit_other_v1")])
        return QualityExtraction(candidates=[])


def test_shared_instructions_and_one_prefix_for_every_phase_with_phase_task_after_breakpoint(monkeypatch):
    client = Recorder()
    run_product(manifest(), local(), client, run_id="compact")
    assert {system for system, *_ in client.calls} == {SHARED_SYSTEM}
    assert len({kwargs["cache_prefix"] for *_, kwargs in client.calls}) == 1
    assert len({kwargs["prompt_cache_key"] for *_, kwargs in client.calls}) == 1
    tasks = [payload["task"] for _, payload, _, _ in client.calls]
    assert tasks[0] == EXTRACT_TASK and JUDGE_TASK in tasks
    limits = {schema.__name__: kwargs["max_output_tokens"] for _, _, schema, kwargs in client.calls}
    assert limits == {"QualityExtraction": 8000, "QualityJudgment": 2000}
    judge = next(payload for _, payload, schema, _ in client.calls if schema is QualityJudgment)
    assert len(judge["evidence"]) == 1 and judge["evidence"][0]["kind"] == "table_row"  # cited entries only
    assert judge["candidates"][0]["citation_ids"] == [judge["evidence"][0]["citation_id"]]
    refine = client.calls[1][1]
    assert refine["first_pass_candidates"] == [{
        "attribute_id": "Seal / Softgoods Material", "value": "Other: EPDM ASTM D2000", "unit": None,
        "citation_ids": [refine["evidence"][1]["citation_id"]], "quote": "EPDM ASTM D2000",
    }]
    monkeypatch.setenv("QUALITY_MAX_OUTPUT_TOKENS_JUDGE", "1500")
    client = Recorder()
    client.max_output_tokens = 1000
    run_product(manifest(), local(), client, run_id="ceiling")
    assert {kwargs["max_output_tokens"] for *_, kwargs in client.calls} == {1000}


def test_without_cache_key_definitions_travel_in_the_payload():
    client = Recorder()
    packet = product_packet(manifest(), local(), "internal_pdf", NAMES, tier_only=True)
    complete_quality_call(client, EXTRACT_TASK, packet, QualityExtraction,
                          context={"operation": "model", "phase": "extract"})
    _, payload, _, kwargs = client.calls[0]
    assert "cache_prefix" not in kwargs and payload["definitions"] and payload["task"] == EXTRACT_TASK


def test_spend_caps_are_configurable(tmp_path):
    from tests.test_quality_pipeline import Store
    meter = QualityCostMeter(Store(), "batch", "run", run_cap_usd="25", overnight_cap_usd="25")
    meter.before_call({"maximum_cost_usd": 24})
    with pytest.raises(CostLimitExceeded):
        meter.before_call({"maximum_cost_usd": 25})
    assert meter.summary()["run_cap_usd"] == 25
    with pytest.raises(ValueError):
        QualityCostMeter(Store(), "batch", "run2", run_cap_usd=0)
    default = QualityCostMeter(Store(), "batch", "run3")
    assert (default.summary()["run_cap_usd"], default.summary()["overnight_cap_usd"]) == (10, 40)


def test_retained_mueller_and_ford_pass1_packets_fit_40k_characters():
    if not RETAINED.is_file():
        pytest.skip("Optional private retained Final A result is not present")
    results = {key: EnrichmentResult.model_validate(value) for key, value in json.loads(RETAINED.read_text()).items()}
    local_sets = {key: [e for e in r.evidence if e.source_tier in {"internal_pdf", "vendor_table"}]
                  for key, r in results.items()}
    shared = shared_source_ids(list(local_sets.values()))
    for key, result in results.items():
        structured = {a.attribute_id: derive_definition(a.model_dump(mode="json")) for a in result.manifest.attributes}
        pending = [a.attribute_id for a in result.manifest.attributes]
        for tier in ("internal_pdf", "vendor_table"):
            packet = product_packet(result.manifest, local_sets[key], tier, pending, structured=structured,
                                    shared_ids=shared[source_family(local_sets[key])], tier_only=True)
            prefix, view = cached_packet_parts(packet)
            size = len(prefix) + len(json.dumps({"task": EXTRACT_TASK, **view}, ensure_ascii=False))
            assert size <= 40_000, (key, tier, size)
            assert not HASH.search(json.dumps(view)) and not TIMESTAMP.search(json.dumps(view))


def test_shared_instructions_keep_origin_and_row_presentation_cues():
    assert '"derived" when it is\nquoted source text placed under "Other:"' in SHARED_SYSTEM
    assert "never inferred" in SHARED_SYSTEM
    assert 'never "Row N:", header labels, "=" or "|"' in SHARED_SYSTEM
