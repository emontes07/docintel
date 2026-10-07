"""Production pass orchestration with synthetic Responses and persistent judging."""

import json

from backend.batch_store import SQLiteStore
from backend.quality_cost import QualityCostMeter
from backend.quality_judge import JudgeCache
from backend.quality_pipeline import QualityExtraction, run_product
from tests.test_quality_tool_loop import completion, evidence, manifest, message, proposal, response, tool


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
