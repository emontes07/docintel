"""PR B: no reference-chosen targets in prompts, second look, step cap and web yield."""

import json
from datetime import datetime, timezone

import pytest

from backend import quality_pipeline as pipeline
from backend import quality_tool_loop as tool_loop
from backend.models.enrichment import AttributeDefinition, Candidate, EnrichmentResult, Evidence, Manifest, ProductKey
from backend.quality_definitions import DEFINITION_RULES
from backend.quality_metrics import web_yield
from backend.quality_pipeline import QualityExtraction, QualityJudgment, QualityProposal, run_product

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
# Attribute names that the Cowork reference comparison surfaced as gaps and that a
# former tool-prompt priority list targeted. Prompts must not steer toward them.
REFERENCE_GAP_NAMES = ("Port Type", "Material Standard", "Compatible Meter Size", "Flanged Outlet")


def prompts() -> dict[str, str]:
    return {
        "shared_system": pipeline.SHARED_SYSTEM, "extract": pipeline.EXTRACT_TASK, "refine": pipeline.REFINE_TASK,
        "judge": pipeline.JUDGE_TASK, "second_look": pipeline.SECOND_LOOK_TASK, "definition_rules": DEFINITION_RULES,
        "tool_system": tool_loop.SYSTEM, "closeout": tool_loop.CLOSEOUT_INSTRUCTION,
        **{f"tool:{name}": text for name, text in tool_loop.DESCRIPTIONS.items()},
    }


@pytest.mark.parametrize("name", REFERENCE_GAP_NAMES)
def test_no_prompt_contains_reference_comparison_attribute_names(name):
    for label, text in prompts().items():
        # The single permitted mention is the documented, grounding-enforced derivation rule.
        stripped = text.replace(pipeline.NONFLANGED_OUTLET_RULE, "")
        assert name not in stripped, f"{name!r} appears in the {label} prompt"
    assert pipeline.EVIDENCE_RULES.count(pipeline.NONFLANGED_OUTLET_RULE) == 1


def evidence(key, text, locator, tier="internal_pdf"):
    return Evidence(
        evidence_id=key, source_id="catalog" if tier == "internal_pdf" else "vendor", source_tier=tier,
        source_locator=locator, source_version="sha256:" + "a" * 64, content_kind="source_excerpt",
        text=text, observed_at=NOW, provider_retrieved_at=NOW,
    )


def manifest():
    return Manifest(
        product=ProductKey(item_id="P1", vendor="Mueller", mpn="014255 215N", hierarchy_node="Angle Valves"),
        attributes=[AttributeDefinition(attribute_id=name, description=name, value_type="string")
                    for name in ("Seal / Softgoods Material", "Valve Type")],
    )


def local():
    return [
        evidence("t00", "DESCRIPTION", "https://x/doc.pdf#page=1&table=0&row=0&column=0"),
        evidence("t01", "MATERIAL", "https://x/doc.pdf#page=1&table=0&row=0&column=1"),
        evidence("t10", "O-RING", "https://x/doc.pdf#page=1&table=0&row=1&column=0"),
        evidence("t11", "EPDM ASTM D2000", "https://x/doc.pdf#page=1&table=0&row=1&column=1"),
        evidence("v1", json.dumps({"sheet": "S", "row": 2, "cells": [
            {"cell": "A2", "column": "Part #", "value": "014255 215N"},
            {"cell": "B2", "column": "Descr", "value": "ANGLE MTR STOP"}]}),
            "https://x/vendor.xlsx#sheet=S&row=2", "vendor_table"),
    ]


class SecondLook:
    effort, max_output_tokens = "medium", 16000

    def __init__(self):
        self.calls = []

    def complete_structured(self, system, user, schema, **kwargs):
        packet = json.loads(user)
        self.calls.append((packet, schema, kwargs))
        if schema is QualityJudgment:
            return QualityJudgment(decisions=[{"candidate_id": c["candidate_id"], "decision": "accepted",
                                               "reason": "Supported."} for c in packet["candidates"]])
        if packet["task"] == pipeline.SECOND_LOOK_TASK:
            row = next(e for e in packet["evidence"] if e["kind"] == "table_row")
            return QualityExtraction(candidates=[QualityProposal(
                attribute_id="Seal / Softgoods Material", value="EPDM ASTM D2000", evidence_ids=[row["citation_id"]],
                supporting_quote="EPDM ASTM D2000")])
        if packet["active_tier"] == "vendor_table" and "first_pass_candidates" not in packet:
            row = packet["evidence"][0]
            return QualityExtraction(candidates=[QualityProposal(
                attribute_id="Valve Type", value="ANGLE MTR STOP", evidence_ids=[row["citation_id"]],
                supporting_quote="ANGLE MTR STOP")])
        return QualityExtraction(candidates=[])


def test_second_look_reads_all_local_tiers_for_pending_attributes_and_judges_new_values():
    client = SecondLook()
    result = run_product(manifest(), local(), client, run_id="second-look", second_look_enabled=True)
    look = [packet for packet, schema, _ in client.calls if packet["task"] == pipeline.SECOND_LOOK_TASK]
    assert len(look) == 1
    packet = look[0]
    assert packet["unresolved_attributes"] == ["Seal / Softgoods Material"]  # resolved Valve Type is not re-asked
    assert {entry["kind"] for entry in packet["evidence"]} == {"table_row", "vendor_row"}
    assert "image_instruction" not in packet
    seal = next(a for a in result.attributes if a.attribute_id == "Seal / Softgoods Material")
    assert seal.candidates[0].value == "EPDM ASTM D2000" and seal.candidates[0].judge_status == "accepted"
    assert seal.candidates[0].grounding["quality_pass"] == "second_look"
    summary = next(d for d in result.quality_diagnostics if d["operation"] == "second_look_summary")
    assert summary["added"] == summary["accepted"] == 1
    assert not any(d.get("phase") == "tool_loop" for d in result.quality_diagnostics)


def test_second_look_is_off_by_default_and_never_runs_with_the_tool_loop():
    client = SecondLook()
    run_product(manifest(), local(), client, run_id="off")
    assert not any(packet["task"] == pipeline.SECOND_LOOK_TASK for packet, _, _ in client.calls)


def test_tool_loop_is_capped_at_six_steps_when_enabled(monkeypatch):
    seen = {}

    def fake_loop(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after capturing limits")

    monkeypatch.setattr(tool_loop, "run_tool_loop", fake_loop)
    with pytest.raises(RuntimeError, match="capturing"):
        run_product(manifest(), local(), SecondLook(), run_id="cap", tool_loop_enabled=True,
                    usage_callback=lambda entry: entry, tool_prices={})
    assert seen["max_steps"] == 6


def test_web_yield_is_accepted_web_values_per_dollar_by_attribute():
    web = evidence("w1", "Port: full", "https://maker.example/p", "manufacturer_web")
    result = EnrichmentResult(
        execution_mode="live_inference", candidate_source="llm", model_call_status="succeeded",
        manifest=manifest(), observed_at=NOW, evidence=[web], retrieval=[], attributes=[{
            "attribute_id": "Valve Type", "status": "proposed", "candidates": [Candidate(
                attribute_id="Valve Type", value="full", evidence_ids=["w1"], supporting_quote="full",
                judge_status="accepted")]}],
    )
    usage = [
        {"operation": "web_search", "attribute_id": "Valve Type", "cost_usd": .0125},
        {"operation": "web_browse", "context": {"attribute_id": "Valve Type"}, "cost_usd": .0125},
        {"operation": "web_search", "cost_usd": .0125},
        {"operation": "model", "attribute_id": "Valve Type", "cost_usd": 1},
    ]
    report = web_yield([result], usage)
    assert report["attributes"]["Valve Type"] == {"web_cost_usd": .025, "web_operations": 2,
                                                  "accepted_web_values": 1, "accepted_per_usd": 40.0}
    assert report["attributes"]["(unattributed)"]["accepted_per_usd"] == 0.0
    assert report["total"]["accepted_per_usd"] == round(1 / .0375, 4)
    assert web_yield([], [])["total"]["accepted_per_usd"] is None
