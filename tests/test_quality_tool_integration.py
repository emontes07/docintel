"""Production pass orchestration with synthetic Responses and persistent judging."""

import json
from unittest.mock import Mock

import pytest

from backend.batch_store import SQLiteStore
from backend.quality_cost import QualityCostMeter
from backend.quality_judge import JudgeCache
from backend.quality_pipeline import QualityExtraction, QualityJudgment, run_product
from tests.test_quality_tool_loop import completion, evidence, manifest, maximum, message, price, proposal, response, tool


def test_pipeline_tool_pass_grounds_judges_reuses_cache_and_charges_once(tmp_path):
    product = manifest()
    product.existing_values = {"Resolved": "retained"}
    excerpt = evidence(text=f"Part number: {product.product.mpn}. Body: brass.")
    store = SQLiteStore(tmp_path / "store")
    cache = JudgeCache(store, namespace="owner", policy="unchanged")
    prices = {"search_usd": ".0125", "browse_usd": ".0125", "di_usd_per_page": ".01"}
    outputs = []
    counts = []
    for run in ("first", "repeat"):
        adapter = completion()
        tool_turns = []
        judge_calls = []
        meter = QualityCostMeter(store, "batch", run, clock=lambda: 0)
        usage = []

        def first_pass(*args, **kwargs):
            adapter.last_usage = {"cost_usd": .001, "usage_reported": True}
            return QualityExtraction(candidates=[])

        adapter.complete_structured = first_pass

        def create(**request):
            if request["text"]["format"]["name"] == "QualityJudgment":
                judge_calls.append(request)
                assert request["reasoning"]["effort"] == "low"
                packet = json.loads(request["input"][0]["content"])
                verdict = message()
                verdict["content"][0]["text"] = json.dumps({"decisions": [{
                    "candidate_id": packet["candidates"][0]["candidate_id"],
                    "decision": "accepted", "reason": "Exact quoted product body.",
                }]})
                return response(verdict)
            tool_turns.append(request)
            return response(tool()) if len(tool_turns) == 1 else response(message([proposal()]))

        adapter.client.responses.create.side_effect = create

        def record(entry):
            priced = meter.record(entry)
            priced["call_id"] = f"{run}-{len(usage)}"
            usage.append(priced)
            return priced

        result = run_product(
            product, [excerpt], adapter, run_id=run, judge_cache=cache,
            usage_callback=record, before_call=meter.before_call,
            tool_loop_enabled=True, tool_prices=prices,
        )
        summary, = [entry for entry in result.quality_diagnostics if entry["operation"] == "tool_loop_summary"]
        assert summary["status"] == "completed"
        assert summary["steps"] <= 15 and summary["tool_loop_cost_usd"] <= 1
        assert summary["requested_attributes"] == ["Material"]
        material = next(attribute for attribute in result.attributes if attribute.attribute_id == "Material")
        assert not material.rejected_candidates
        assert material.candidates[0].judge_status == "accepted"
        assert material.candidates[0].grounding["applicability"]["status"] == "exact"
        assert meter.summary()["model_calls"] == 2 + len(tool_turns) + len(judge_calls)
        assert sum(entry["operation"] == "model" for entry in usage) == meter.summary()["model_calls"]
        assert len({entry["call_id"] for entry in usage}) == len(usage)
        outputs.append(material.candidates[0].value)
        counts.append(len(judge_calls))
    assert outputs == ["brass", "brass"]
    assert counts == [1, 0]


def test_tool_rechecking_does_not_duplicate_an_existing_cached_dispute(tmp_path):
    product = manifest()
    product.existing_values = {"Resolved": "retained"}
    adapter = completion([response(tool()), response(message([proposal()]))])
    judge_votes = []

    def normal_pass(system, user, schema, **options):
        adapter.last_usage = {"cost_usd": .001, "usage_reported": True}
        if schema is QualityExtraction:
            return schema.model_validate({"candidates": [proposal()]})
        judge_votes.append(user)
        return QualityJudgment.model_validate({"decisions": [{
            "candidate_id": json.loads(user)["candidates"][0]["candidate_id"],
            "decision": "judge_disputed", "reason": "Synthetic support disagreement.",
        }]})

    adapter.complete_structured = normal_pass
    meter = QualityCostMeter(SQLiteStore(tmp_path / "meter"), "batch", "run", clock=lambda: 0)
    result = run_product(
        product, [evidence()], adapter, run_id="run", usage_callback=meter.record,
        before_call=meter.before_call, tool_loop_enabled=True,
        tool_prices={"search_usd": ".0125", "browse_usd": ".0125", "di_usd_per_page": ".01"},
    )
    material = next(attribute for attribute in result.attributes if attribute.attribute_id == "Material")
    assert len(material.candidates) == 1 and material.candidates[0].judge_status == "judge_disputed"
    assert material.candidates[0].grounding["tool_rechecked"] is True
    assert len(judge_votes) == 2  # agreeing disputes need no third vote
    assert adapter.client.responses.create.call_count == 2


@pytest.mark.parametrize(("name", "counter", "prior", "called"), [
    ("web_search", "search", 12, 0), ("web_search", "search", 11, 1), ("browse", "browse", 6, 0),
])
def test_tool_pass_shares_first_pass_web_limits(name, counter, prior, called):
    from backend.quality_tool_loop import ProductSourceScope, run_tool_loop
    from backend.quality_web import QualityWeb

    product, excerpt = manifest(), evidence()
    url = "https://muellercompany.com/catalog.pdf"
    web = QualityWeb(search=Mock(return_value=[]), browse=Mock(), page_fetch=Mock())
    web.counts[product.product.item_id] = {"search": 0, "browse": 0, "direct_page": 0}
    web.counts[product.product.item_id][counter] = prior
    arguments = {"scope": "manufacturer"} if name == "web_search" else {"url": url}
    adapter = completion([response(tool(name, attribute_id="Material", **arguments)), response(message())])
    result = run_tool_loop(
        product, [excerpt], adapter, pass1_status={"Material": "unresolved", "Resolved": "resolved"},
        scope=ProductSourceScope(product.product, frozenset({"e1"}), manufacturer_hosts=("muellercompany.com",),
                                 approved_urls=frozenset({url})),
        ground_candidate=Mock(side_effect=AssertionError("No candidate was supplied")),
        judge_candidates=Mock(side_effect=AssertionError("No candidate was supplied")),
        maximum_cost=maximum, usage_callback=price, web=web,
    )
    assert result.status == "completed"
    assert (web.search if name == "web_search" else web.browse).call_count == called
    assert web.counts[product.product.item_id][counter] == prior + called
    web.page_fetch.assert_not_called()
