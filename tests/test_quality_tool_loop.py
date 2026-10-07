"""Synthetic bounded tool-loop tests; no provider requests."""

from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
from email.message import Message
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from openai import AzureOpenAI
import pytest


from backend import quality_tool_model as model_module
from backend import quality_tool_loop as loop

from backend.batch_store import Missing
from backend.core.docintel import ParsedDocument
from backend.core.quality_model import QualityModelResponseError, ResponsesCompletion
from backend.core.websearch import OriginalPageEvidence
from backend.models.enrichment import AttributeDefinition, Evidence, Manifest, ProductKey
from backend.quality_cost import CostLimitExceeded
from backend.quality_pdf import CachedPDFOCR
from backend.quality_pipeline import ground_candidate
from backend.quality_web import QualityWeb

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
URL = "https://fordmeterbox.com/synthetic-product"
PDF_URL = "https://fordmeterbox.com/synthetic-product.pdf"
OTHER_URL = "https://supplier.example/synthetic-product"
PRODUCT = ProductKey(item_id="synthetic", vendor="Synthetic Vendor", mpn="TEST-123", hierarchy_node="Test")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("No live network or credentials allowed")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    monkeypatch.setattr(DefaultAzureCredential, "get_token", denied)
    monkeypatch.setattr(ManagedIdentityCredential, "get_token", denied)


def manifest():
    return Manifest(product=PRODUCT, source_ids=["pdf", "vendor"], attributes=[
        AttributeDefinition(attribute_id="Material", description="Body material", value_type="string"),
        AttributeDefinition(attribute_id="Resolved", description="Already accepted", value_type="string"),
    ])


def evidence(key="e1", text="Body: brass.", tier="internal_pdf", attribute="Material"):
    return Evidence(
        evidence_id=key, source_id="vendor" if tier == "vendor_table" else "pdf",
        source_locator="batchblob:///approved#sheet=Vendor&row=2" if tier == "vendor_table" else "batchblob:///approved#page=1&paragraph=0",
        source_version="sha256:" + "a" * 64, source_tier=tier, content_kind="source_excerpt",
        text=text, observed_at=NOW, attribute_ids=[attribute],
    )


def proposal(evidence_id="e1", **changes):
    return {
        "attribute_id": "Material", "value": "brass", "unit": None, "evidence_ids": [evidence_id],
        "supporting_quote": "Body: brass.", "origin": "literal", "normalization_rule": None,
        "justification": None, "reviewer_explanation": "Verify body material.", "confidence": None,
        **changes,
    }


def tool(name="read_pdf_page", call_id="call_1", **args):
    if not args and name == "read_pdf_page":
        args = {"attribute_id": "Material", "source_id": "pdf", "page": 1}
    return {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id, "name": name,
            "arguments": json.dumps(args), "status": "completed"}


def message(candidates=None, explanation="No further supported candidates"):
    return {"type": "message", "id": "msg", "role": "assistant", "status": "completed", "content": [
        {"type": "output_text", "annotations": [], "text": json.dumps({"candidates": candidates or [], "explanation": explanation})},
    ]}


def response(*items, **updates):
    value = {
        "id": "resp_synthetic", "object": "response", "created_at": 1, "model": "test-model",
        "status": "completed", "error": None, "incomplete_details": None, "instructions": None,
        "metadata": {}, "parallel_tool_calls": False, "tools": [], "tool_choice": "auto",
        "temperature": None, "top_p": None, "output": list(items),
        "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200},
                  "output_tokens": 500, "output_tokens_details": {"reasoning_tokens": 300},
                  "total_tokens": 1500},
    }
    value.update(updates)
    return value


def completion(responses=(), *, client=None):
    adapter = ResponsesCompletion.__new__(ResponsesCompletion)
    adapter.client = client or SimpleNamespace(max_retries=0, responses=SimpleNamespace(create=Mock(side_effect=list(responses))))
    adapter.deployment, adapter.effort, adapter.max_output_tokens = "test-deployment", "low", 1000
    adapter.pricing_usd_per_million = {"input": 2, "cached_input": .5, "cache_write": 2, "output": 8}
    adapter.pricing_basis = "synthetic_test"
    adapter.last_usage, adapter.call_records = {}, []
    adapter.usage_callback = Mock(side_effect=AssertionError("Double-metering completion callback"))
    return adapter


def maximum(context):
    if context["operation"] == "model":
        assert context["request"]["max_output_tokens"] <= 1000
        # Synthetic provider fixtures guarantee this bound; production supplies a
        # certified full input/max-output bound from the existing price schedule.
        return ".1"
    if context["operation"] == "document_intelligence":
        return Decimal(context["requested_pages"]) * Decimal(".01")
    return 0 if context["operation"] == "direct_page" else ".0125"


def price(entry):
    result = dict(entry)
    if entry["operation"] in {"web_search", "web_browse"}:
        result["cost_usd"] = .0125
    elif entry["operation"] == "document_intelligence":
        result["cost_usd"] = (
            0 if entry.get("cache_hit") or entry.get("analysis_attempted") is False
            else None if entry.get("analyzed_pages") is None
            else float(Decimal(entry["analyzed_pages"]) * Decimal(".01"))
        )
    return result


def judge(candidates, evidence, actions):
    return [c.model_copy(update={"judge_status": "accepted", "judge_reason": "Synthetic parent policy"}) for c in candidates]


def run(adapter, local=None, **kwargs):
    local = [evidence()] if local is None else local
    options = {
        "pass1_status": {"Material": "unresolved", "Resolved": "resolved"},
        "scope": loop.ProductSourceScope(PRODUCT, frozenset(e.evidence_id for e in local),
                                         ("fordmeterbox.com",), ("supplier.example",), frozenset({URL, PDF_URL, OTHER_URL})),
        "ground_candidate": ground_candidate, "judge_candidates": judge,
        "maximum_cost": maximum, "usage_callback": price,
    }
    options.update(kwargs)
    return loop.run_tool_loop(manifest(), local, adapter, **options)


def page(url, text="TEST-123 Body: brass."):
    return OriginalPageEvidence(text=text, final_url=url, content_hash=hashlib.sha256(text.encode()).hexdigest(),
                                retrieved_at=NOW, media_type="text/html", byte_size=len(text))


def web(**kwargs):
    return QualityWeb(search=Mock(return_value=[URL]), browse=Mock(side_effect=lambda url: {"url": url, "content": "Invented gold"}),
                      page_fetch=Mock(side_effect=page), **kwargs)


def test_real_sdk_roundtrip_strict_functions_reasoning_and_parent_grounding():
    requests = []
    reasoning = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque-reasoning"}
    outputs = iter([response(reasoning, tool()), response(message([proposal()]))])
    def handle(request):
        requests.append(json.loads(request.content))
        assert "api-version" not in request.url.params
        return httpx.Response(200, json=next(outputs))
    client = AzureOpenAI(base_url="https://synthetic.openai.azure.com/openai/v1", api_key="offline",
                         api_version="v1", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    adapter = completion(client=client)
    meter = Mock(side_effect=price)
    grounding = Mock(side_effect=ground_candidate)
    try:
        result = run(adapter, ground_candidate=grounding, usage_callback=meter, prompt_cache_key="synthetic-family")
    finally:
        client.close()
    assert result.status == "completed" and result.steps == 3
    assert result.submissions[0].status == "judged"
    assert result.submissions[0].candidate.judge_status == "accepted"
    assert result.cost_usd == Decimal(".0114")
    assert len(requests) == 2 and meter.call_count == 2 and grounding.call_count == 1
    assert all(t["strict"] and t["parameters"]["additionalProperties"] is False for t in requests[0]["tools"])
    assert all(set(t["parameters"]["required"]) == set(t["parameters"]["properties"]) for t in requests[0]["tools"])
    assert requests[0]["prompt_cache_key"] == "synthetic-family"
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert reasoning in requests[1]["input"]
    output = next(i for i in requests[1]["input"] if i.get("type") == "function_call_output")
    assert output["call_id"] == "call_1" and json.loads(output["output"])["evidence"][0]["evidence_id"] == "e1"
    assert "Resolved" not in requests[0]["input"][0]["content"]
    assert all({"operation", "context", "status", "cost_usd"} <= set(d) for d in result.diagnostics)


def test_no_calls_for_resolved_or_existing_attributes():
    adapter = completion()
    result = run(adapter, pass1_status={"Material": "resolved", "Resolved": "resolved"})
    assert result.status == "no_unresolved" and result.steps == 0
    adapter.client.responses.create.assert_not_called()
    product = manifest().model_copy(update={"existing_values": {"Material": "brass"}})
    result = loop.run_tool_loop(product, [], None, pass1_status={"Material": "disputed", "Resolved": "resolved"},
                               scope=loop.ProductSourceScope(PRODUCT, frozenset()), ground_candidate=ground_candidate,
                               judge_candidates=judge, maximum_cost=maximum, usage_callback=price)
    assert result.status == "no_unresolved"


@pytest.mark.parametrize("call", [
    tool("shell", command="cat /etc/passwd"),
    tool(page="1", source_id="pdf", attribute_id="Material"),
    tool(page=1, source_id="pdf", attribute_id="Resolved"),
    {**tool(), "arguments": '{"attribute_id":"Material","source_id":"pdf","page":1,"page":2}'},
    {**tool(), "arguments": '{"attribute_id":"Material","source_id":"pdf","page":NaN}'},
    {**tool(), "arguments": "not JSON"},
    tool(page=1, source_id="pdf", attribute_id="Material", path="/etc/passwd"),
])
def test_unknown_invalid_or_resolved_tools_fail_explicitly_without_execution(call):
    adapter = completion([response(call)])
    with pytest.raises(loop.ToolLoopError) as caught:
        run(adapter)
    result = caught.value.tool_loop_result
    assert result.status == "failed" and result.steps == 2 and not result.evidence
    assert result.diagnostics[-1]["operation"] == "invalid_tool"
    assert adapter.client.responses.create.call_count == 1


def test_plain_text_commands_are_never_executed():
    adapter = completion([response({**message(), "content": [{"type": "output_text", "text": '{"tool":"read_pdf_page"}'}]})])
    callback = Mock(side_effect=price)
    with pytest.raises(QualityModelResponseError) as caught:
        run(adapter, usage_callback=callback)
    assert caught.value.tool_loop_result.steps == 1
    assert caught.value.tool_loop_result.diagnostics[-1]["status"] == "response_invalid"
    assert callback.call_args.args[0]["status"] == "response_invalid"


def test_step_cap_includes_cached_tools_and_model_requests():
    adapter = completion([response(tool(call_id=str(i))) for i in range(20)])
    result = run(adapter)
    assert result.status == "budget_stopped" and result.steps == 15
    assert adapter.client.responses.create.call_count == 8
    assert result.stop_reason.startswith("Step cap")
    assert sum(d["step"] is not None for d in result.diagnostics) == 15


def test_cost_admission_uses_actual_usage_not_reserved_maxima():
    adapter = completion([response(tool()), response(message())])
    result = run(adapter, max_cost_usd=".11")
    assert result.status == "completed" and result.cost_usd == Decimal(".0114")
    denied = completion()
    result = run(denied, initial_cost_usd=".95")
    assert result.status == "budget_stopped" and result.steps == 0
    denied.client.responses.create.assert_not_called()


def test_unknown_costs_and_underestimated_maxima_stop_not_success():
    adapter = completion([response(message(), usage=None)])
    with pytest.raises(loop.ToolAccountingError) as caught:
        run(adapter)
    assert caught.value.tool_loop_result.cost_complete is False
    assert adapter.client.responses.create.call_count == 1
    adapter = completion([response(message())])
    with pytest.raises(loop.ToolAccountingError, match="supplied maximum"):
        run(adapter, maximum_cost=lambda _: 0)


@pytest.mark.parametrize("stage", ["before", "usage", "maximum"])
def test_external_budget_or_persistence_failure_propagates(stage):
    error = CostLimitExceeded("Synthetic parent meter stop")
    def fail(*_):
        raise error
    adapter = completion([response(message())])
    options = {"before_call": fail} if stage == "before" else {"usage_callback": fail} if stage == "usage" else {"maximum_cost": fail}
    with pytest.raises(CostLimitExceeded) as caught:
        run(adapter, **options)
    assert caught.value is error and error.tool_loop_result.status == "failed"
    assert adapter.client.responses.create.call_count == (1 if stage == "usage" else 0)


def test_scope_filters_neighbors_resolved_fragments_and_never_reads_full_pdf():
    local = [evidence(), evidence("neighbor", "Neighbor: gold."), evidence("resolved", "Resolved secret", attribute="Resolved")]
    adapter = completion([response(tool()), response(message())])
    scope = loop.ProductSourceScope(PRODUCT, frozenset({"e1", "resolved"}))
    result = run(adapter, local, scope=scope)
    assert [e.evidence_id for e in result.evidence] == ["e1"]
    history = str(adapter.client.responses.create.call_args_list)
    assert "Neighbor: gold" not in history and "Resolved secret" not in history
    with pytest.raises(loop.ToolLoopError, match="different product"):
        run(completion(), scope=loop.ProductSourceScope(PRODUCT.model_copy(update={"item_id": "other"}), frozenset({"e1"})))


def test_vendor_search_uses_only_approved_row_ids():
    local = [evidence("row1", "Current product brass", "vendor_table"), evidence("row2", "Neighbor gold", "vendor_table")]
    adapter = completion([response(tool("search_vendor_rows", attribute_id="Material")), response(message())])
    result = run(adapter, local, scope=loop.ProductSourceScope(PRODUCT, frozenset({"row1"})))
    assert [e.evidence_id for e in result.evidence] == ["row1"]


def test_discovery_cannot_support_candidate_or_bypass_parent():
    adapter = completion([
        response(tool("web_search", scope="manufacturer", attribute_id="Material")),
        response(message([proposal("search-result")])),
    ])
    infrastructure = web()
    grounding = Mock()
    with pytest.raises(loop.ToolLoopError, match="retrieved evidence") as caught:
        run(adapter, web=infrastructure, ground_candidate=grounding)
    assert not caught.value.tool_loop_result.evidence
    infrastructure.page_fetch.assert_not_called()
    infrastructure.browse.assert_not_called()
    grounding.assert_not_called()


def test_browse_requires_independent_original_and_caches_by_source():
    infrastructure = web()
    adapter = completion([
        response(tool("browse", url=URL, attribute_id="Material")),
        response(tool("browse", call_id="repeat", url=URL, attribute_id="Material")),
        response(message()),
    ])
    result = run(adapter, web=infrastructure)
    assert result.steps == 6
    assert [e.text for e in result.evidence] == ["TEST-123 Body: brass."]
    assert result.evidence[0].provider_retrieved_at == NOW
    assert result.cost_usd == Decimal(".0296")
    infrastructure.browse.assert_called_once()
    infrastructure.page_fetch.assert_called_once()
    assert [d["operation"] for d in result.diagnostics] == ["model", "web_browse", "direct_page", "model", "browse", "model"]


def test_failed_browse_or_original_does_not_return_discovery_as_evidence():
    for failure_at in ("browse", "page_fetch"):
        infrastructure = web()
        getattr(infrastructure, failure_at).side_effect = ValueError("Synthetic unavailable source")
        adapter = completion([response(tool("browse", url=URL, attribute_id="Material"))])
        with pytest.raises(ValueError) as caught:
            run(adapter, web=infrastructure)
        result = caught.value.tool_loop_result
        assert not result.evidence and result.status == "failed"
        assert sum(d["operation"] == "web_browse" for d in result.diagnostics) == 1


@pytest.mark.parametrize("url", [
    OTHER_URL, "file:///etc/passwd", "https://127.0.0.1/a", "https://fordmeterbox.com/unapproved",
    "https://fordmeterbox.com/a?token=secret", "https://fordmeterbox.com@127.0.0.1/a",
])
def test_unapproved_or_unsafe_sources_never_reach_web(url):
    infrastructure = web()
    adapter = completion([response(tool("browse", url=url, attribute_id="Material"))])
    with pytest.raises(Exception):
        run(adapter, web=infrastructure)
    infrastructure.browse.assert_not_called()
    infrastructure.page_fetch.assert_not_called()


def test_manufacturer_first_and_domain_constrained_search_then_approved():
    infrastructure = web()
    infrastructure.search.side_effect = [[], [OTHER_URL]]
    adapter = completion([
        response(tool("web_search", scope="manufacturer", attribute_id="Material")),
        response(tool("web_search", call_id="second", scope="approved", attribute_id="Material")),
        response(message()),
    ])
    result = run(adapter, web=infrastructure)
    assert result.status == "completed"
    queries = [call.args[0] for call in infrastructure.search.call_args_list]
    assert "site:fordmeterbox.com" in queries[0] and "site:supplier.example" in queries[1]
    assert all('"TEST-123"' in query for query in queries)
    assert not result.evidence


def test_grounding_and_judge_controls_are_parent_callbacks_not_copied():
    grounding = Mock(side_effect=ground_candidate)
    judging = Mock(side_effect=lambda candidates, evidence, actions: [
        c.model_copy(update={"judge_status": "judge_disputed", "judge_reason": "Parent applicability map disputes"})
        for c in candidates
    ])
    adapter = completion([response(tool()), response(message([proposal()]))])
    result = run(adapter, ground_candidate=grounding, judge_candidates=judging)
    assert grounding.call_count == judging.call_count == 1
    assert result.submissions[0].candidate.judge_status == "judge_disputed"
    adapter = completion([response(tool()), response(message([proposal(supporting_quote="Body: gold.")]))])
    result = run(adapter, ground_candidate=grounding, judge_candidates=judging)
    assert result.submissions[0].status == "ground_rejected"
    assert result.submissions[0].candidate is None and judging.call_count == 1


def test_parent_judge_shares_action_and_cost_gate():
    def paid_judge(candidates, evidence, actions):
        return actions.call("model", lambda: judge(candidates, evidence, actions),
                            context={"phase": "judge", "request": {"max_output_tokens": 1000}},
                            usage=lambda: {"cost_usd": .02})
    adapter = completion([response(tool()), response(message([proposal()]))])
    result = run(adapter, judge_candidates=paid_judge)
    assert result.steps == 4 and result.cost_usd == Decimal(".0314")
    assert result.diagnostics[-1]["phase"] == "judge"
    adapter = completion([response(tool()), response(message([proposal()]))])
    result = run(adapter, judge_candidates=paid_judge, max_steps=3)
    assert result.status == "budget_stopped" and result.submissions[0].status == "pending_review"
    assert result.submissions[0].candidate.judge_status == "not_judged"


class Store:
    def __init__(self):
        self.data = {}
    def read_bytes(self, key):
        if key not in self.data:
            raise Missing(key)
        return self.data[key]
    def write_bytes(self, key, value, version=None):
        version = str(len(self.data) + 1)
        self.data[key] = (value, version)
        return version


def pdf_web(monkeypatch, *, text="", pages=2, callback_error=None):
    import backend.quality_web as quality_web
    raw = b"%PDF-synthetic-offline-test"
    parser = Mock()
    parser.last_page_count = pages
    parser.extract_pdf_bytes.return_value = ParsedDocument(
        source=PDF_URL, cache_key="sha256:" + hashlib.sha256(raw).hexdigest(), parsed_at=NOW,
        tables=[], paragraphs=[], raw_text="TEST-123 Body: brass.",
    )
    if callback_error:
        parser.extract_pdf_bytes.side_effect = callback_error
    headers = Message()
    headers["Content-Type"] = "application/pdf"
    response = SimpleNamespace(status=200, headers=headers, getheader=lambda key, default=None: headers.get(key, default),
                               read=lambda _: raw)
    connection = SimpleNamespace(request=Mock(), getresponse=lambda: response, close=Mock())
    monkeypatch.setattr(quality_web, "_PinnedHTTPSConnection", lambda *_: connection)
    monkeypatch.setattr(quality_web, "_public_addresses", lambda *_: [("synthetic-address",)])
    monkeypatch.setattr(quality_web, "pdf_text", Mock(return_value=(text, pages)))
    ocr = CachedPDFOCR(Store(), parser)
    return QualityWeb(search=lambda _: [], pdf_ocr=ocr), parser, quality_web.pdf_text


def test_pdf_text_then_existing_ocr_fallback_actual_pages_and_source_cache(monkeypatch):
    infrastructure, parser, text_reader = pdf_web(monkeypatch)
    adapter = completion([
        response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material")),
        response(tool("fetch_pdf", call_id="repeat", url=PDF_URL, attribute_id="Material")),
        response(message()),
    ])
    result = run(adapter, web=infrastructure)
    assert result.status == "completed" and result.steps == 6
    assert result.cost_usd == Decimal(".0371")
    assert result.evidence and "document_intelligence_ocr" in result.evidence[0].qualification
    parser.extract_pdf_bytes.assert_called_once()
    text_reader.assert_called_once()
    di = next(d for d in result.diagnostics if d["operation"] == "document_intelligence")
    assert di["analyzed_pages"] == 2 and di["cost_usd"] == .02


def test_text_pdf_never_calls_di(monkeypatch):
    infrastructure, parser, _ = pdf_web(monkeypatch, text="TEST-123 Body: brass.")
    adapter = completion([response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material")), response(message())])
    result = run(adapter, web=infrastructure)
    assert result.status == "completed" and result.steps == 3
    parser.extract_pdf_bytes.assert_not_called()
    assert "pdftotext" in result.evidence[0].qualification


def test_di_admission_checks_remaining_money_and_steps_before_ocr(monkeypatch):
    for options in ({"max_steps": 2}, {"initial_cost_usd": ".98", "maximum_cost": lambda context: ".006" if context["operation"] == "model" else maximum(context)}):
        infrastructure, parser, _ = pdf_web(monkeypatch)
        adapter = completion([response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material"))])
        result = run(adapter, web=infrastructure, **options)
        assert result.status == "budget_stopped" and not result.evidence
        parser.extract_pdf_bytes.assert_not_called()
        assert result.steps == 2


def test_di_callback_errors_are_not_sanitized_into_success_or_retry(monkeypatch):
    infrastructure, parser, _ = pdf_web(monkeypatch)
    error = RuntimeError("Synthetic persistence failed")
    def before(context):
        if context["operation"] == "document_intelligence":
            raise error
    adapter = completion([response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material"))])
    with pytest.raises(RuntimeError) as caught:
        run(adapter, web=infrastructure, before_call=before)
    assert caught.value is error and error.tool_loop_result.status == "failed"
    parser.extract_pdf_bytes.assert_not_called()


def test_real_sdk_api_error_is_explicit_and_never_retried():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"message": "Synthetic invalid schema", "type": "invalid_request_error"}})
    client = AzureOpenAI(base_url="https://synthetic.openai.azure.com/openai/v1", api_key="offline",
                         api_version="v1", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(loop.ToolAccountingError) as caught:
            run(completion(client=client))
    finally:
        client.close()
    assert len(requests) == 1 and caught.value.tool_loop_result.status == "failed"
    assert "Synthetic invalid schema" in str(caught.value.__cause__)
    assert caught.value.tool_loop_result.cost_complete is False


@pytest.mark.parametrize("limit", [0, 16, True])
def test_step_cap_cannot_be_relaxed(limit):
    with pytest.raises(ValueError):
        run(completion(), max_steps=limit)


def test_dollar_cap_cannot_be_relaxed_or_unknown():
    for limit in ("1.01", "NaN", "Infinity", "-1"):
        with pytest.raises((ValueError, loop.ToolAccountingError)):
            run(completion(), max_cost_usd=limit)


def test_multi_call_response_counts_each_call_without_parallel_execution():
    adapter = completion([response(tool(call_id="one"), tool(call_id="two")), response(message())])
    result = run(adapter, max_steps=3)
    assert result.steps == 3 and result.status == "budget_stopped"
    assert adapter.client.responses.create.call_count == 1


def test_repeated_call_id_never_replays_a_paid_tool():
    infrastructure = web()
    duplicate = tool("browse", url=URL, attribute_id="Material")
    adapter = completion([response(duplicate), response(duplicate)])
    with pytest.raises(loop.ToolLoopError, match="Duplicate function") as caught:
        run(adapter, web=infrastructure)
    assert caught.value.tool_loop_result.diagnostics[-1]["operation"] == "invalid_tool"
    infrastructure.browse.assert_called_once()
    infrastructure.page_fetch.assert_called_once()


@pytest.mark.parametrize("broken", [
    response(message(), status="incomplete"),
    response({"type": "web_search_call", "id": "unconfigured_native_tool"}),
    response({**message(), "content": [{"type": "refusal", "refusal": "no"}]}),
    response(tool(), message()),
])
def test_incomplete_refused_and_unsupported_api_outputs_are_explicit(broken):
    adapter = completion([broken])
    with pytest.raises(QualityModelResponseError) as caught:
        run(adapter)
    assert caught.value.tool_loop_result.steps == 1
    assert adapter.client.responses.create.call_count == 1


def test_local_location_and_source_allowlists_fail_closed():
    for changes in ({"source_id": "scoring-reference"}, {"source_locator": "batchblob:///approved"},
                    {"content_kind": "generated_answer"}):
        with pytest.raises(loop.ToolLoopError):
            run(completion(), [evidence().model_copy(update=changes)])


def test_parent_judge_cannot_invent_values_or_omit_decisions():
    for bad_judge in (
        lambda candidates, evidence, actions: [],
        lambda candidates, evidence, actions: [
            c.model_copy(update={"value": "gold", "judge_status": "accepted"}) for c in candidates
        ],
    ):
        adapter = completion([response(tool()), response(message([proposal()]))])
        with pytest.raises(loop.ToolLoopError):
            run(adapter, judge_candidates=bad_judge)


def test_paid_action_at_exact_remaining_amount_is_admitted():
    infrastructure = web()
    adapter = completion([
        response(tool("web_search", scope="manufacturer", attribute_id="Material")),
    ])
    result = run(adapter, web=infrastructure, initial_cost_usd=".9818",
                 maximum_cost=lambda context: ".0057" if context["operation"] == "model" else maximum(context))
    assert result.cost_usd == 1 and result.status == "budget_stopped"
    assert result.steps == 2
    infrastructure.search.assert_called_once()


def test_search_cache_and_missing_hosts_do_not_charge_phantom_web_calls():
    infrastructure = web()
    adapter = completion([
        response(tool("web_search", scope="manufacturer", attribute_id="Material")),
        response(tool("web_search", call_id="repeat", scope="manufacturer", attribute_id="Material")),
        response(message()),
    ])
    result = run(adapter, web=infrastructure)
    infrastructure.search.assert_called_once()
    assert result.cost_usd == Decimal(".0296")
    infrastructure = web()
    adapter = completion([response(tool("web_search", scope="approved", attribute_id="Material"))])
    scope = loop.ProductSourceScope(PRODUCT, frozenset({"e1"}))
    with pytest.raises(loop.ToolLoopError) as caught:
        run(adapter, web=infrastructure, scope=scope)
    assert caught.value.tool_loop_result.cost_usd == Decimal(".0057")
    infrastructure.search.assert_not_called()


def test_browse_extra_get_consumes_its_own_step_and_never_falls_back_to_discovery():
    infrastructure = web()
    adapter = completion([response(tool("browse", url=URL, attribute_id="Material"))])
    result = run(adapter, web=infrastructure, max_steps=2)
    assert result.status == "budget_stopped" and not result.evidence
    assert result.cost_usd == Decimal(".0182") and result.steps == 2
    infrastructure.browse.assert_called_once()
    infrastructure.page_fetch.assert_not_called()


def test_pdf_cache_hit_records_zero_new_analysis_and_no_extra_step(monkeypatch):
    infrastructure, parser, _ = pdf_web(monkeypatch)
    first = run(completion([response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material")), response(message())]), web=infrastructure)
    second = run(completion([response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material")), response(message())]), web=infrastructure)
    assert first.steps == 4 and second.steps == 3
    assert first.cost_usd == Decimal(".0314") and second.cost_usd == Decimal(".0114")
    parser.extract_pdf_bytes.assert_called_once()
    di = next(d for d in second.diagnostics if d["operation"] == "document_intelligence")
    assert di["cache_hit"] and di["cost_usd"] == 0 and di["step"] is None


def test_unknown_di_usage_stops_and_failed_parser_does_not_hide_meter_failure(monkeypatch):
    infrastructure, parser, _ = pdf_web(monkeypatch)
    parser.last_page_count = None
    adapter = completion([response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material"))])
    with pytest.raises(loop.ToolAccountingError, match="Unpriced") as caught:
        run(adapter, web=infrastructure)
    assert caught.value.tool_loop_result.cost_complete is False
    infrastructure, parser, _ = pdf_web(monkeypatch, callback_error=ValueError("Synthetic parse failure"))
    error = CostLimitExceeded("Synthetic monetary callback stop")
    def failed_meter(entry):
        if entry["operation"] == "document_intelligence":
            raise error
        return price(entry)
    adapter = completion([response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material"))])
    with pytest.raises(CostLimitExceeded) as caught:
        run(adapter, web=infrastructure, usage_callback=failed_meter)
    assert caught.value is error
    parser.extract_pdf_bytes.assert_called_once()


def test_pdf_over_ocr_page_limit_never_starts_ocr(monkeypatch):
    infrastructure, parser, _ = pdf_web(monkeypatch, pages=6)
    adapter = completion([response(tool("fetch_pdf", url=PDF_URL, attribute_id="Material"))])
    with pytest.raises(ValueError) as caught:
        run(adapter, web=infrastructure)
    assert caught.value.tool_loop_result.evidence == []
    parser.extract_pdf_bytes.assert_not_called()


def test_real_sdk_explicit_cache_breakpoint_precedes_dynamic_and_continued_input():
    requests, admissions = [], []
    prefix = json.dumps({"definitions": [manifest().attributes[0].model_dump(mode="json")], "shared_source_documents": []})
    reasoning = {"id": "rs_cache", "type": "reasoning", "summary": [], "encrypted_content": "continued-secret"}
    outputs = iter([response(reasoning, tool()), response(message())])
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(outputs))
    def bound(context):
        if context["operation"] == "model":
            admissions.append(context["request"]["extra_body"]["input"])
        return maximum(context)
    client = AzureOpenAI(base_url="https://synthetic.openai.azure.com/openai/v1", api_key="offline",
                         api_version="v1", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    adapter = completion(client=client)
    try:
        result = run(adapter, prompt_cache_key="shared-family", cache_prefix=prefix, maximum_cost=bound)
    finally:
        client.close()
    assert len(requests) == len(admissions) == 2
    for request in requests:
        assert request["prompt_cache_key"] == "shared-family"
        assert request["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
        content = request["input"][0]["content"]
        assert content[0] == {
            "type": "input_text", "text": prefix, "prompt_cache_breakpoint": {"mode": "explicit"},
        }
        assert "TEST-123" in content[1]["text"] and "prompt_cache_breakpoint" not in content[1]
        assert sum("prompt_cache_breakpoint" in part for part in content) == 1
    assert reasoning in requests[1]["input"]
    assert any(item.get("type") == "function_call_output" for item in requests[1]["input"])
    assert requests[0]["input"] == admissions[0] and requests[1]["input"] == admissions[1]
    usage = [entry for entry in result.diagnostics if entry["operation"] == "model"]
    assert all(entry["prompt_cache_key"] == "shared-family" and entry["explicit_cache_prefix"] for entry in usage)
    assert adapter.last_usage["explicit_cache_prefix"] is True
    assert prefix not in str(result.diagnostics)


@pytest.mark.parametrize("key,prefix", [(None, "approved"), ("", "approved"), ("family", ""), ("family", 7)])
def test_invalid_cache_configuration_fails_before_request(key, prefix):
    adapter = completion()
    with pytest.raises((ValueError, loop.ToolLoopError)):
        run(adapter, prompt_cache_key=key, cache_prefix=prefix)
    adapter.client.responses.create.assert_not_called()


def test_key_only_keeps_implicit_cache_shape_and_reports_no_explicit_prefix():
    adapter = completion([response(message())])
    result = run(adapter, prompt_cache_key="family")
    request = adapter.client.responses.create.call_args.kwargs
    assert request["extra_body"] == {"prompt_cache_key": "family"}
    assert isinstance(request["input"][0]["content"], str)
    assert result.diagnostics[0]["prompt_cache_key"] == "family"
    assert result.diagnostics[0]["explicit_cache_prefix"] is False


def parent_cached_judge(cache, outcomes):
    """Exercise the actual parent cache/voting function, not a copied test policy."""
    from backend.quality_judge import judge_candidates as parent_judge_candidates
    observed = []
    outcomes = iter(outcomes)
    def callback(candidates, evidence, actions):
        def call(packet, schema):
            def invoke():
                observed.append(packet)
                outcome = next(outcomes)
                if isinstance(outcome, Exception):
                    raise outcome
                return schema.model_validate({"decisions": [
                    {"candidate_id": item["candidate_id"], "decision": outcome, "reason": "Synthetic parent vote"}
                    for item in packet["candidates"]
                ]})
            return actions.call(
                "model", invoke, context={"phase": "judge", "request": {"max_output_tokens": 1000}},
                usage=lambda: {"cost_usd": .02},
            )
        parent_judge_candidates(
            candidates, {d.attribute_id: d for d in manifest().attributes}, evidence,
            {"product": PRODUCT.model_dump(mode="json")}, call, cache,
            diagnostics=actions.result.diagnostics, context=actions.context,
        )
        return candidates
    return callback, observed


@pytest.mark.parametrize("votes", [["accepted"], ["judge_disputed", "accepted", "accepted"]])
def test_real_parent_cached_judge_is_reused_and_preserves_cache_metadata(votes):
    from backend.quality_judge import JudgeCache
    cache = JudgeCache(namespace="synthetic-owner", policy="same-parent-policy")
    callback, observed = parent_cached_judge(cache, votes)
    adapter = completion([response(tool()), response(message([proposal(), proposal()]))])
    result = run(adapter, judge_candidates=callback)
    assert len(observed) == len(votes) and result.steps == 3 + len(votes)
    assert result.cost_usd == Decimal(".0114") + Decimal(".02") * len(votes)
    assert all(len(packet["candidates"]) == 1 for packet in observed)
    assert all("grounding" not in candidate and "judge_status" not in candidate
               for packet in observed for candidate in packet["candidates"])
    for submission in result.submissions:
        assert submission.status == "judged" and submission.candidate.judge_status == "accepted"
        assert len(submission.candidate.grounding["judge_cache"]["votes"]) == len(votes)
    adapter = completion([response(tool()), response(message([proposal()]))])
    reused = run(adapter, judge_candidates=callback)
    assert reused.status == "completed" and reused.steps == 3 and reused.cost_usd == Decimal(".0114")
    assert len(observed) == len(votes)
    assert any(entry["operation"] == "judge_cache" and entry["status"] == "hit" for entry in reused.diagnostics)


def test_parent_cache_extra_dispute_votes_obey_same_step_gate():
    from backend.quality_judge import JudgeCache
    cache = JudgeCache(namespace="synthetic-owner", policy="same-parent-policy")
    callback, observed = parent_cached_judge(cache, ["judge_disputed", "accepted", "accepted"])
    adapter = completion([response(tool()), response(message([proposal()]))])
    result = run(adapter, judge_candidates=callback, max_steps=4)
    assert result.status == "budget_stopped" and result.steps == 4
    assert len(observed) == 1 and not cache.memory
    assert result.submissions[0].status == "pending_review"


def test_parent_cache_cannot_hide_schema_errors_or_retry_failed_model_requests():
    from backend.quality_judge import JudgeCache
    cache = JudgeCache(namespace="synthetic-owner", policy="same-parent-policy")
    error = QualityModelResponseError("Synthetic invalid judge schema")
    callback, observed = parent_cached_judge(cache, [error, "accepted", "accepted"])
    adapter = completion([response(tool()), response(message([proposal()]))])
    with pytest.raises(QualityModelResponseError) as caught:
        run(adapter, judge_candidates=callback)
    assert caught.value is error and len(observed) == 1 and not cache.memory
    assert error.tool_loop_result.status == "failed"
    assert error.tool_loop_result.steps == 4
    assert error.tool_loop_result.cost_usd == Decimal(".0314")


def test_parent_cannot_modify_grounding_except_for_its_cache_metadata():
    def tampered(candidates, evidence, actions):
        return [candidate.model_copy(update={
            "judge_status": "accepted",
            "grounding": {**candidate.grounding, "quote": {"text": "invented"}, "judge_cache": {"key": "anything"}},
        }) for candidate in candidates]
    adapter = completion([response(tool()), response(message([proposal()]))])
    with pytest.raises(loop.ToolLoopError, match="changed a candidate"):
        run(adapter, judge_candidates=tampered)


@pytest.mark.parametrize("name,args", [
    ("web_search", {"scope": "manufacturer"}),
    ("search_vendor_rows", {}),
])
def test_expected_answer_query_argument_is_never_a_search_instruction(name, args):
    infrastructure = web()
    adapter = completion([response(tool(name, attribute_id="Material", query="REFERENCE_ONLY_VALUE", **args))])
    with pytest.raises(loop.ToolLoopError, match="Invalid function arguments"):
        run(adapter, web=infrastructure)
    infrastructure.search.assert_not_called()
    infrastructure.browse.assert_not_called()
    initial = adapter.client.responses.create.call_args.kwargs["input"]
    assert "REFERENCE_ONLY_VALUE" not in str(initial)


def test_search_terms_are_only_product_identity_and_unresolved_definition_name():
    infrastructure = web()
    local = [evidence(text="Body: APPROVED_SOURCE_DESCRIPTOR.")]
    adapter = completion([
        response(tool()), response(tool("web_search", call_id="search", scope="manufacturer", attribute_id="Material")),
        response(message()),
    ])
    run(adapter, local, web=infrastructure)
    infrastructure.search.assert_called_once_with('"TEST-123" Material (site:fordmeterbox.com)')


@pytest.mark.parametrize("kind", ["extra_context", "changed_definition", "changed_source", "neighbor_source", "citation_hint"])
def test_reference_hints_cannot_enter_via_cache_prefix(kind):
    from backend.quality_pipeline import product_packet
    local = [evidence(), evidence("neighbor", "OTHER_PRODUCT_VALUE")]
    packet = product_packet(manifest(), [local[0]], "internal_pdf", ["Material"], shared_ids={"e1"})
    prefix = {key: packet[key] for key in ("definitions", "shared_source_documents")}
    if kind == "extra_context":
        prefix["expected_answer"] = "REFERENCE_ONLY_VALUE"
    elif kind == "changed_definition":
        prefix["definitions"][0]["description"] += " REFERENCE_ONLY_VALUE"
    elif kind == "changed_source":
        prefix["shared_source_documents"][0]["text"] = "REFERENCE_ONLY_VALUE"
    elif kind == "citation_hint":
        prefix["shared_source_documents"][0]["citation_id"] = "REFERENCE_ONLY_VALUE"
    else:
        neighbor = product_packet(manifest(), [local[1]], "internal_pdf", ["Material"], shared_ids={"neighbor"})
        prefix["shared_source_documents"] = neighbor["shared_source_documents"]
    adapter = completion()
    with pytest.raises(loop.ToolLoopError, match="Unapproved cache prefix"):
        run(adapter, local, scope=loop.ProductSourceScope(PRODUCT, frozenset({"e1"})),
            prompt_cache_key="family", cache_prefix=json.dumps(prefix))
    adapter.client.responses.create.assert_not_called()


def test_matching_wording_in_authoritative_definition_and_source_is_permitted():
    from backend.quality_pipeline import product_packet
    product = manifest()
    product.attributes[0].description = "Definition includes LEGITIMATE_DESCRIPTOR."
    local = [evidence(text="Body: LEGITIMATE_DESCRIPTOR.")]
    packet = product_packet(product, local, "internal_pdf", ["Material"], shared_ids={"e1"})
    prefix = json.dumps({key: packet[key] for key in ("definitions", "shared_source_documents")})
    adapter = completion([response(message())])
    result = loop.run_tool_loop(
        product, local, adapter, pass1_status={"Material": "unresolved", "Resolved": "resolved"},
        scope=loop.ProductSourceScope(PRODUCT, frozenset({"e1"})),
        ground_candidate=ground_candidate, judge_candidates=judge, maximum_cost=maximum, usage_callback=price,
        prompt_cache_key="family", cache_prefix=prefix,
    )
    assert result.status == "completed"
    request = adapter.client.responses.create.call_args.kwargs
    assert "LEGITIMATE_DESCRIPTOR" in request["extra_body"]["input"][0]["content"][0]["text"]
    assert not result.evidence  # A cache prefix never authorizes unrequested evidence.


def test_composed_judge_override_logs_actual_request_reasoning_effort():
    adapter = completion([response(message())])
    adapter.effort = "medium"
    model = model_module.ResponsesToolModel(adapter)
    request = model.build_request("Synthetic judge instruction", [{"role": "user", "content": "{}"}], [], loop.ToolConclusion)
    request["reasoning"] = {"effort": "low"}
    request["tool_choice"] = "none"
    model.request(request)
    assert adapter.client.responses.create.call_args.kwargs["reasoning"] == {"effort": "low"}
    assert model.last_usage["reasoning_effort"] == adapter.last_usage["reasoning_effort"] == "low"
    adapter.usage_callback.assert_not_called()


def structured_prefix(structured=None):
    from backend.quality_pipeline import product_packet
    packet = product_packet(
        manifest(), [evidence()], "internal_pdf", ["Material"], shared_ids={"e1"}, structured=structured,
    )
    return {key: packet[key] for key in ("definitions", "structured_definitions", "shared_source_documents")}


def parent_structured_definitions():
    from backend.quality_definitions import derive_definition
    specs = {a.attribute_id: derive_definition(a.model_dump(mode="json")) for a in manifest().attributes}
    specs["Material"] = derive_definition(
        manifest().attributes[0].model_dump(mode="json"),
        original_row={"potential_attribute_name": "Material", "potential_attribute_data_type": "Enumerated",
                      "allowed_values": ["SYNTHETIC_APPROVED_OPTION"]},
    )
    return specs


def test_pr2_structured_prefix_derived_from_manifest_needs_no_explicit_input():
    from backend.quality_definitions import derive_definition, model_instruction
    adapter = completion([response(message())])
    prefix = structured_prefix()
    result = run(adapter, prompt_cache_key="family", cache_prefix=json.dumps(prefix))
    assert result.status == "completed"
    request = adapter.client.responses.create.call_args.kwargs
    dynamic = json.loads(request["input"][0]["content"])
    expected = model_instruction(derive_definition(manifest().attributes[0].model_dump(mode="json")))
    assert dynamic["structured_definitions"] == [expected]
    assert json.loads(request["extra_body"]["input"][0]["content"][0]["text"]) == prefix


def test_pr2_explicit_parent_definition_objects_enrich_prefix_and_dynamic_packet():
    from backend.quality_definitions import model_instruction
    specs = parent_structured_definitions()
    adapter = completion([response(message())])
    prefix = structured_prefix(specs)
    result = run(adapter, structured_definitions=specs, prompt_cache_key="family", cache_prefix=json.dumps(prefix))
    assert result.status == "completed"
    dynamic = json.loads(adapter.client.responses.create.call_args.kwargs["input"][0]["content"])
    assert dynamic["structured_definitions"] == [model_instruction(specs["Material"])]
    assert dynamic["structured_definitions"][0]["allowed_values"] == ["SYNTHETIC_APPROVED_OPTION"]
    assert not result.evidence


def test_explicit_structured_objects_support_partial_mapping_and_legacy_prefix():
    specs = parent_structured_definitions()
    prefix = structured_prefix()
    del prefix["structured_definitions"]
    adapter = completion([response(message())])
    result = run(adapter, structured_definitions={"Material": specs["Material"]},
                 prompt_cache_key="family", cache_prefix=json.dumps(prefix))
    assert result.status == "completed"


def test_enriched_prefix_is_rejected_without_explicit_approved_parent_definitions():
    adapter = completion()
    with pytest.raises(loop.ToolLoopError, match="Unapproved cache prefix"):
        run(adapter, prompt_cache_key="family", cache_prefix=json.dumps(structured_prefix(parent_structured_definitions())))
    adapter.client.responses.create.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("allowed_values", ["REFERENCE_ONLY_VALUE"]),
    ("normalization_rule", "REFERENCE_ONLY_INSTRUCTION"),
    ("definition_questions", ["REFERENCE_ONLY_VALUE"]),
    ("field_provenance", {"kind": "REFERENCE_ONLY_CONTEXT"}),
    ("attribute_id", "Other product attribute"),
    ("unit_resolved", 1),
])
def test_structured_prefix_cannot_introduce_arbitrary_hints_or_type_coercions(field, value):
    prefix = structured_prefix()
    prefix["structured_definitions"][0][field] = value
    adapter = completion()
    with pytest.raises(loop.ToolLoopError, match="Unapproved cache prefix"):
        run(adapter, prompt_cache_key="family", cache_prefix=json.dumps(prefix))
    adapter.client.responses.create.assert_not_called()


@pytest.mark.parametrize("kind", ["raw_instruction", "unknown_attribute", "changed_fields", "changed_semantics"])
def test_explicit_structured_input_must_match_its_approved_derivation(kind):
    from backend.quality_definitions import model_instruction
    specs = parent_structured_definitions()
    if kind == "raw_instruction":
        specs["Material"] = model_instruction(specs["Material"])
    elif kind == "unknown_attribute":
        specs["Unknown"] = specs["Material"]
    elif kind == "changed_fields":
        specs["Material"] = replace(specs["Material"], original_fields={
            **specs["Material"].original_fields, "description": "REFERENCE_ONLY_VALUE",
        })
    else:
        specs["Material"] = replace(specs["Material"], definition_questions=("REFERENCE_ONLY_VALUE",))
    adapter = completion()
    with pytest.raises(loop.ToolLoopError):
        run(adapter, structured_definitions=specs)
    adapter.client.responses.create.assert_not_called()


NEW_PUBLIC_URL = "https://new-supplier.example/current-product"


def public_scope(**changes):
    return replace(
        loop.ProductSourceScope(PRODUCT, frozenset({"e1"}), ("fordmeterbox.com",), allow_public_web_discovery=True),
        **changes,
    )


def public_discovery_turns(final=None):
    return [
        response(tool("web_search", call_id="manufacturer", scope="manufacturer", attribute_id="Material")),
        response(tool("web_search", call_id="public", scope="approved", attribute_id="Material")),
        response(final or tool("browse", call_id="original", url=NEW_PUBLIC_URL, attribute_id="Material")),
        response(message()),
    ]


def test_opt_in_public_discovery_authorizes_exact_url_without_external_approval(monkeypatch):
    infrastructure = web()
    infrastructure.search.side_effect = [[], [NEW_PUBLIC_URL]]
    dns = Mock(return_value=[("synthetic-global-address",)])
    monkeypatch.setattr(loop, "_public_addresses", dns)
    adapter = completion(public_discovery_turns())
    result = run(adapter, scope=public_scope(), web=infrastructure)
    assert result.status == "completed" and result.steps == 9 and result.cost_usd == Decimal(".0603")
    queries = [call.args[0] for call in infrastructure.search.call_args_list]
    assert queries == ['"TEST-123" Material (site:fordmeterbox.com)', '"TEST-123" Material']
    dns.assert_called_once_with("new-supplier.example")
    infrastructure.browse.assert_called_once_with(NEW_PUBLIC_URL)
    infrastructure.page_fetch.assert_called_once_with(NEW_PUBLIC_URL)
    assert result.evidence[0].source_locator == NEW_PUBLIC_URL
    assert result.evidence[0].source_tier == "approved_web"
    assert "Invented gold" not in result.evidence[0].text
    preflight = next(e for e in result.diagnostics if e["operation"] == "public_url_validation")
    assert preflight["cost_usd"] == 0 and preflight["step"] == 6


def test_public_discovery_never_authorizes_all_paths_on_a_discovered_host(monkeypatch):
    from backend.core.websearch import ExternalEvidenceError
    infrastructure = web()
    infrastructure.search.side_effect = [[], [NEW_PUBLIC_URL]]
    dns = Mock()
    monkeypatch.setattr(loop, "_public_addresses", dns)
    unknown = tool("browse", call_id="unknown", url=NEW_PUBLIC_URL + "/unlisted", attribute_id="Material")
    with pytest.raises((loop.ToolLoopError, ExternalEvidenceError)):
        run(completion(public_discovery_turns(unknown)), scope=public_scope(), web=infrastructure)
    dns.assert_not_called()
    infrastructure.browse.assert_not_called()
    infrastructure.page_fetch.assert_not_called()


def test_public_discovery_requires_manufacturer_configuration_and_first_attempt():
    adapter = completion()
    with pytest.raises(loop.ToolLoopError, match="manufacturer domains"):
        run(adapter, scope=public_scope(manufacturer_hosts=()))
    adapter.client.responses.create.assert_not_called()
    infrastructure = web()
    adapter = completion([response(tool("web_search", scope="approved", attribute_id="Material"))])
    with pytest.raises(loop.ToolLoopError, match="manufacturer domains first"):
        run(adapter, scope=public_scope(), web=infrastructure)
    infrastructure.search.assert_not_called()


def test_manufacturer_search_can_authorize_new_manufacturer_url_without_callback():
    infrastructure = web()
    infrastructure.search.return_value = [URL]
    adapter = completion([
        response(tool("web_search", scope="manufacturer", attribute_id="Material")),
        response(tool("browse", call_id="original", url=URL, attribute_id="Material")),
        response(message()),
    ])
    result = run(adapter, scope=public_scope(), web=infrastructure)
    assert result.status == "completed" and result.evidence[0].source_tier == "manufacturer_web"
    infrastructure.page_fetch.assert_called_once_with(URL)


@pytest.mark.parametrize("url", [
    "http://new-supplier.example/current-product", "https://127.0.0.1/private",
    "https://host.internal/private", "https://new-supplier.example/current-product?token=private",
    "https://user:password@new-supplier.example/current-product",
])
def test_public_discovery_still_rejects_unsafe_urls(url):
    from backend.core.websearch import ExternalEvidenceError
    infrastructure = web()
    infrastructure.search.side_effect = [[], [url]]
    with pytest.raises((loop.ToolLoopError, ExternalEvidenceError)):
        run(completion(public_discovery_turns()), scope=public_scope(), web=infrastructure)
    infrastructure.browse.assert_not_called()
    infrastructure.page_fetch.assert_not_called()


def test_public_dns_rejection_does_not_make_or_charge_a_browse_request(monkeypatch):
    from backend.core.websearch import ExternalEvidenceError
    infrastructure = web()
    infrastructure.search.side_effect = [[], [NEW_PUBLIC_URL]]
    monkeypatch.setattr(loop, "_public_addresses", Mock(side_effect=ExternalEvidenceError("Private address", code="unsafe_address")))
    with pytest.raises(ExternalEvidenceError) as caught:
        run(completion(public_discovery_turns()), scope=public_scope(), web=infrastructure)
    assert caught.value.tool_loop_result.cost_usd == Decimal(".0421")
    assert not caught.value.tool_loop_result.evidence
    infrastructure.browse.assert_not_called()
    infrastructure.page_fetch.assert_not_called()


def test_public_dns_preflight_obeys_step_cap_and_source_cache(monkeypatch):
    infrastructure = web()
    infrastructure.search.side_effect = [[], [NEW_PUBLIC_URL]]
    dns = Mock(return_value=[("synthetic-global-address",)])
    monkeypatch.setattr(loop, "_public_addresses", dns)
    result = run(completion(public_discovery_turns()), scope=public_scope(), web=infrastructure, max_steps=5)
    assert result.status == "budget_stopped" and result.steps == 5
    dns.assert_not_called()
    infrastructure.browse.assert_not_called()
    infrastructure.search.side_effect = [[], [NEW_PUBLIC_URL]]
    turns = public_discovery_turns()
    turns.insert(-1, response(tool("browse", call_id="repeat", url=NEW_PUBLIC_URL, attribute_id="Material")))
    result = run(completion(turns), scope=public_scope(), web=infrastructure)
    assert result.status == "completed" and result.steps == 11
    dns.assert_called_once()
    infrastructure.browse.assert_called_once()
    infrastructure.page_fetch.assert_called_once()


def test_prior_pass_urls_can_be_seeded_for_this_product_only(monkeypatch):
    from urllib.parse import urlsplit
    infrastructure = web()
    infrastructure.seen = {PRODUCT.item_id: {NEW_PUBLIC_URL}, "other-product": {"https://other.example/neighbor"}}
    urls = frozenset(infrastructure.seen[PRODUCT.item_id])
    hosts = tuple(sorted({urlsplit(url).hostname for url in urls}))
    scope = public_scope(approved_urls=urls, approved_hosts=hosts)
    infrastructure.search.return_value = []
    monkeypatch.setattr(loop, "_public_addresses", Mock(return_value=[("synthetic-global-address",)]))
    adapter = completion([
        response(tool("web_search", scope="manufacturer", attribute_id="Material")),
        response(tool("browse", call_id="prior", url=NEW_PUBLIC_URL, attribute_id="Material")),
        response(message()),
    ])
    result = run(adapter, scope=scope, web=infrastructure)
    assert result.status == "completed"
    assert {e.source_locator for e in result.evidence} == {NEW_PUBLIC_URL}
    assert "other.example" not in str(adapter.client.responses.create.call_args_list)
