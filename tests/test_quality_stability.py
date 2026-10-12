"""Stability contracts: display formatting, extraction-output reuse and run-to-run scoring."""

import json
import random
from datetime import datetime, timezone

import pytest

from backend.batch_store import Conflict, Missing
from backend.models.enrichment import AttributeDefinition, Candidate, Evidence, Manifest, ProductKey
from backend.quality_amortization import _transplant_family_candidates
from backend.quality_format import canonical_value
from backend.quality_judge import JudgeCache
from backend.quality_pipeline import ExtractionCache, QualityExtraction, QualityJudgment, run_product
from backend.quality_scoring import compare_runs
from backend.reviewer_workbook import build_reviewer_package
from backend.workbooks import read_workbook

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


@pytest.mark.parametrize(("attribute", "value", "expected"), [
    ("Inlet Size", "Other: 3/4IN", 'Other: 3/4"'),
    ("Inlet Size", "Other: 3/4 in", 'Other: 3/4"'),
    ("Inlet Size", 'Other: 3/4"', 'Other: 3/4"'),
    ("Outlet Size", "Other: 1-1/2 IN", 'Other: 1-1/2"'),
    ("Inlet Connection Type", "Other: FEMALE IRON PIPE THREAD INLET", "Other: Female iron pipe thread"),
    ("Outlet Connection Type", "Other: FEMALE IRON PIPE THREAD OUTLET", "Other: Female iron pipe thread"),
    ("Valve Type", "Other: ANGLE MTR STOP", "Other: Angle meter stop"),
    ("Material Standard (Brass Alloy)", "Other: CAST BRASS ASTM B62 ALLOY C83600", "Other: Cast brass ASTM B62 alloy C83600"),
    ("Valve Type", "Other: Angle Key Service Valves", "Other: Angle Key Service Valves"),
])
def test_canonical_display_formatting(attribute, value, expected):
    assert canonical_value(attribute, value) == expected


def test_allowed_values_and_non_strings_are_unchanged():
    assert canonical_value("Inlet Size", "3/4IN", ["3/4IN"]) == "3/4IN"
    assert canonical_value("Flanged Outlet", False) is False


def test_run_scorer_counts_identical_conflict_sets_and_format_changes_as_stable():
    slots = [("P", "Seal"), ("P", "Inlet Size"), ("P", "Valve Type"), ("P", "Flanged Outlet")]
    seal = [{"value": "Other: EPDM", "judge_status": "judge_disputed"},
            {"value": "Other: NITRILE", "judge_status": "judge_disputed"}]
    left = {slots[0]: seal, slots[1]: [{"value": "Other: 3/4IN", "judge_status": "accepted"}],
            slots[2]: [{"value": "Other: ANGLE METER STOP", "judge_status": "accepted"}],
            slots[3]: [{"value": False, "judge_status": "accepted"}]}
    right = {slots[0]: list(reversed(seal)), slots[1]: [{"value": 'Other: 3/4"', "judge_status": "accepted"}],
             slots[2]: [{"value": "Other: ANGLE METER STOP", "judge_status": "judge_disputed"}]}
    result = compare_runs(slots, left, right)
    labels = {row["attribute_id"]: row["comparison"] for row in result["rows"]}
    assert labels == {"Seal": "identical", "Inlet Size": "format-only", "Valve Type": "changed",
                      "Flanged Outlet": "only-left"}
    assert result["stable"] == 2


class Store:
    def __init__(self):
        self.data = {}

    def read_bytes(self, key, max_bytes=64 * 1024 * 1024):
        if key not in self.data:
            raise Missing(key)
        return self.data[key], "etag"

    def write_bytes(self, key, value, version=None):
        if key in self.data and version is None:
            raise Conflict(key)
        self.data[key] = bytes(value)
        return "etag"

    def keys(self, prefix):
        return sorted(key for key in self.data if key.startswith(prefix))


class NondeterministicCompletion:
    """Each uncached extraction returns a different wording, as live models can."""

    deployment, effort = "test-deployment", "medium"

    def __init__(self, seed):
        self.random = random.Random(seed)
        self.calls = []
        self.last_usage = {}

    def complete_structured(self, system, user, schema, **kwargs):
        self.calls.append(schema.__name__)
        self.last_usage = {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001}
        if schema is QualityJudgment:
            packet = json.loads(user)
            return QualityJudgment(decisions=[
                {"candidate_id": c["candidate_id"], "decision": self.random.choice(["accepted", "judge_disputed"]),
                 "reason": "Checked."} for c in packet["candidates"]
            ])
        quote = self.random.choice(["3/4IN COPPER SERVICE INLET", '3/4" COPPER SERVICE INLET'])
        value = "Other: " + quote.split()[0]
        return QualityExtraction(candidates=[{
            "attribute_id": "Inlet Size", "value": value, "supporting_quote": quote.split()[0],
            "evidence_ids": ["row"], "origin": "derived", "normalization_rule": "exact_enum_or_explicit_other_v1",
        }])


def _inputs():
    manifest = Manifest(
        product=ProductKey(item_id="P-1", vendor="Vendor", mpn="M-1", hierarchy_node="Angle Valves"),
        attributes=[AttributeDefinition(attribute_id="Inlet Size", description="Inlet Size", value_type="string",
                                        type_guidance="Enumerated")],
    )
    cells = [{"cell": "A2", "column": "MPN", "value": "M-1"},
             {"cell": "B2", "column": "Descr", "value": '3/4IN COPPER SERVICE INLET; 3/4" COPPER SERVICE INLET'}]
    row = Evidence(
        evidence_id="row", source_id="vendor", source_version="v1", source_tier="vendor_table",
        source_locator="batchblob:///vendor.xlsx#sheet=S&row=2&cells=A2,B2",
        text=json.dumps({"sheet": "S", "row": 2, "cells": cells}), content_kind="source_excerpt", observed_at=NOW,
    )
    return manifest, [row]


def _reviewer_rows(result):
    sheets = read_workbook(build_reviewer_package([result]).workbook)
    return sheets["Review"], sheets["Evidence"]


def test_two_runs_on_identical_inputs_produce_identical_reviewer_rows():
    store = Store()
    manifest, evidence = _inputs()
    rows = []
    calls = []
    for seed, run_id in ((1, "run-a"), (99, "run-b")):
        completion = NondeterministicCompletion(seed)
        result = run_product(
            manifest, evidence, completion, run_id=run_id,
            extraction_cache=ExtractionCache(store, namespace="owner", deployment=completion.deployment),
            judge_cache=JudgeCache(store, namespace="owner", policy="judge-policy"),
        )
        rows.append(_reviewer_rows(result))
        calls.append(list(completion.calls))
    assert rows[0] == rows[1]
    assert calls[0] and calls[1] == []
    assert rows[0][0][0]["Proposed value"] in {'3/4"', 'Other: 3/4"'}


def test_changed_evidence_or_deployment_never_reuses_an_extraction_output():
    store = Store()
    manifest, evidence = _inputs()
    first = NondeterministicCompletion(1)
    run_product(manifest, evidence, first, run_id="a",
                extraction_cache=ExtractionCache(store, namespace="owner", deployment="dep-1"))
    changed = evidence[0].model_copy(update={"source_version": "v2"})
    second = NondeterministicCompletion(1)
    run_product(manifest, [changed], second, run_id="b",
                extraction_cache=ExtractionCache(store, namespace="owner", deployment="dep-1"))
    third = NondeterministicCompletion(1)
    run_product(manifest, evidence, third, run_id="c",
                extraction_cache=ExtractionCache(store, namespace="owner", deployment="dep-2"))
    assert "QualityExtraction" in second.calls and "QualityExtraction" in third.calls


def test_format_duplicates_collapse_to_the_best_judged_candidate():
    from backend.models.enrichment import AttributeResult
    from backend.quality_pipeline import finalize_display

    definition = AttributeDefinition(attribute_id="Inlet Size", description="", value_type="string")
    attribute = AttributeResult(attribute_id="Inlet Size", status="conflict", candidates=[
        Candidate(attribute_id="Inlet Size", value="Other: 3/4IN", evidence_ids=["a"], supporting_quote="3/4IN",
                  judge_status="judge_disputed"),
        Candidate(attribute_id="Inlet Size", value='Other: 3/4"', evidence_ids=["b"], supporting_quote='3/4"',
                  judge_status="accepted"),
    ])
    finalize_display(attribute, definition, "Enumerated")
    assert [(c.value, c.judge_status) for c in attribute.candidates] == [('Other: 3/4"', "accepted")]
    assert attribute.status == "proposed"
    assert attribute.candidates[0].grounding["format_duplicates"][0]["evidence_ids"] == ["a"]


def test_unconfirmed_document_values_are_never_transferred_family_wide():
    manifest = Manifest(
        product=ProductKey(item_id="F-1", vendor="Ford", mpn="AV94-444W-Q-NL", hierarchy_node="Angle Valves"),
        attributes=[AttributeDefinition(attribute_id="Primary Material", description="Primary Material",
                                        value_type="string", type_guidance="Enumerated")],
        source_ids=["pdf"],
    )
    heading = Evidence(evidence_id="h", source_id="pdf", source_version="v1", source_tier="internal_pdf",
                       source_locator="batchblob:///pdf.pdf#page=1&paragraph=1",
                       text="Angle Key Service Valves - (AV11-xxxW-NL style)", content_kind="source_excerpt",
                       observed_at=NOW)
    body = heading.model_copy(update={"evidence_id": "b", "source_locator": "batchblob:///pdf.pdf#page=1&paragraph=2",
                                      "text": "Body: BRASS"})
    family = Candidate(attribute_id="Primary Material", value="Other: BRASS", evidence_ids=["b"],
                       supporting_quote="BRASS", origin="derived", normalization_rule="exact_enum_or_explicit_other_v1")
    assert _transplant_family_candidates([family], manifest, [heading, body]) == []
    av11 = manifest.model_copy(update={"product": manifest.product.model_copy(update={"mpn": "AV11-444W-NL"})})
    transferred = _transplant_family_candidates([family], av11, [heading, body])
    assert transferred and transferred[0].grounding["applicability"]["status"] == "family-confirmed"


def test_format_duplicates_keep_the_confirmed_citation_over_an_unconfirmed_one():
    from backend.models.enrichment import AttributeResult
    from backend.quality_pipeline import finalize_display

    definition = AttributeDefinition(attribute_id="Primary Material", description="", value_type="string")
    unconfirmed = Candidate(attribute_id="Primary Material", value="Other: LOW LEAD BRASS", evidence_ids=["pdf"],
                            supporting_quote="LOW LEAD BRASS", judge_status="accepted",
                            grounding={"applicability": {"status": "family-unconfirmed"}})
    exact = Candidate(attribute_id="Primary Material", value="Other: Low lead brass", evidence_ids=["row"],
                      supporting_quote="LOW LEAD BRASS", judge_status="accepted",
                      grounding={"applicability": {"status": "exact"}})
    attribute = AttributeResult(attribute_id="Primary Material", status="conflict", candidates=[unconfirmed, exact])
    finalize_display(attribute, definition, "Enumerated")
    assert [c.evidence_ids for c in attribute.candidates] == [["row"]]
    assert attribute.candidates[0].grounding["format_duplicates"][0]["applicability"] == "family-unconfirmed"
