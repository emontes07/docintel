"""Offline Responses adapter tests, including the locked SDK's HTTP serializer."""

from copy import deepcopy
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
import httpx
from openai import AzureOpenAI, BadRequestError, Omit
from pydantic import BaseModel, ValidationError
import pytest

from backend.core import quality_model
from backend.core.quality_model import (
    QualityModelConfigurationError,
    QualityModelError,
    QualityModelResponseError,
    ResponsesCompletion,
)


ENDPOINT = "https://quality-test.openai.azure.com"
IMAGE = "data:image/png;base64,aW1hZ2U="


class Attribute(BaseModel):
    value: str
    evidence: str | None = None


class Extraction(BaseModel):
    attributes: list[Attribute]


def response_payload(text='{"attributes":[{"value":"brass","evidence":null}]}', **overrides):
    payload = {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "gpt-5-test-version",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": False,
        "tools": [],
        "tool_choice": "auto",
        "temperature": None,
        "top_p": None,
        "output": [{
            "id": "msg_test", "type": "message", "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 200},
            "output_tokens": 500,
            "output_tokens_details": {"reasoning_tokens": 300},
            "total_tokens": 1500,
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Network and real credentials are forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(ManagedIdentityCredential, "get_token", deny)
    monkeypatch.setattr(DefaultAzureCredential, "get_token", deny)
    for name in (
        "QUALITY_MODEL_DEPLOYMENT", "QUALITY_MODEL_EFFORT", "QUALITY_MODEL_MAX_OUTPUT_TOKENS",
        "QUALITY_MODEL_INPUT_USD_PER_MILLION", "QUALITY_MODEL_CACHED_INPUT_USD_PER_MILLION",
        "QUALITY_MODEL_CACHE_WRITE_USD_PER_MILLION", "QUALITY_MODEL_OUTPUT_USD_PER_MILLION",
        "QUALITY_MODEL_PRICE_BASIS",
        "AZURE_CLIENT_ID", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN",
        "LLM_ENDPOINT", "AI_FOUNDRY_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(quality_model.settings, "LLM_ENDPOINT", None)
    monkeypatch.setattr(quality_model.settings, "AI_FOUNDRY_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(quality_model.settings, "LLM_DEPLOYMENT", "gpt-5")


@pytest.fixture
def adapter(monkeypatch):
    client = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response_payload())))
    constructor = Mock(return_value=client)
    monkeypatch.setattr(quality_model, "AzureOpenAI", constructor)
    model = ResponsesCompletion(token_provider=lambda: "offline-token")
    return model


def set_prices(monkeypatch):
    monkeypatch.setenv("QUALITY_MODEL_INPUT_USD_PER_MILLION", "2")
    monkeypatch.setenv("QUALITY_MODEL_CACHED_INPUT_USD_PER_MILLION", "0.5")
    monkeypatch.setenv("QUALITY_MODEL_OUTPUT_USD_PER_MILLION", "8")


def test_defaults_and_managed_identity(monkeypatch):
    credential = Mock()
    identity = Mock(return_value=credential)
    provider = Mock(return_value="offline-token")
    get_provider = Mock(return_value=provider)
    constructor = Mock()
    monkeypatch.setattr(quality_model, "ManagedIdentityCredential", identity)
    monkeypatch.setattr(quality_model, "get_bearer_token_provider", get_provider)
    monkeypatch.setattr(quality_model, "AzureOpenAI", constructor)
    monkeypatch.setenv("AZURE_CLIENT_ID", "worker-identity")

    model = ResponsesCompletion()

    identity.assert_called_once_with(client_id="worker-identity")
    get_provider.assert_called_once_with(credential, quality_model.COGNITIVE_SERVICES_SCOPE)
    provider.assert_not_called()
    assert model.deployment == "gpt-5"
    assert model.effort == "medium"
    assert model.max_output_tokens == 16000
    assert model.last_usage == {}
    assert model.call_records == []
    constructor.assert_called_once_with(
        base_url=ENDPOINT + "/openai/v1", azure_ad_token_provider=provider,
        api_version="v1", max_retries=0,
    )


def test_injected_token_provider_does_not_create_credentials(monkeypatch):
    identity = Mock(side_effect=AssertionError("Unexpected managed identity"))
    monkeypatch.setattr(quality_model, "ManagedIdentityCredential", identity)
    monkeypatch.setattr(quality_model, "AzureOpenAI", Mock())
    ResponsesCompletion(token_provider=lambda: "offline-token")
    identity.assert_not_called()


def test_static_environment_token_cannot_override_worker_identity(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "unwanted-static-token")
    constructor = Mock()
    monkeypatch.setattr(quality_model, "AzureOpenAI", constructor)
    with pytest.raises(QualityModelConfigurationError, match="Unset AZURE_OPENAI_AD_TOKEN"):
        ResponsesCompletion(token_provider=lambda: "offline-token")
    constructor.assert_not_called()


def test_environment_and_constructor_precedence(monkeypatch):
    monkeypatch.setattr(quality_model, "AzureOpenAI", Mock())
    monkeypatch.setenv("LLM_ENDPOINT", "https://llm.example")
    monkeypatch.setenv("AI_FOUNDRY_ENDPOINT", "https://foundry.example")
    monkeypatch.setenv("QUALITY_MODEL_DEPLOYMENT", "offered-deployment")
    monkeypatch.setenv("QUALITY_MODEL_EFFORT", "high")
    monkeypatch.setenv("QUALITY_MODEL_MAX_OUTPUT_TOKENS", "24000")
    model = ResponsesCompletion(token_provider=lambda: "offline-token")
    assert (model.endpoint, model.deployment, model.effort, model.max_output_tokens) == (
        "https://llm.example", "offered-deployment", "high", 24000,
    )
    explicit = ResponsesCompletion(
        "https://explicit.example", "gpt-5", lambda: "offline-token",
        effort="medium", max_output_tokens=16000,
    )
    assert (explicit.endpoint, explicit.deployment, explicit.effort, explicit.max_output_tokens) == (
        "https://explicit.example", "gpt-5", "medium", 16000,
    )
    monkeypatch.delenv("LLM_ENDPOINT")
    assert ResponsesCompletion(token_provider=lambda: "offline-token").endpoint == "https://foundry.example"


@pytest.mark.parametrize("limit", ["0", "-1", "1.5", "", "NaN", "invalid"])
def test_invalid_environment_limit_fails_before_client_creation(monkeypatch, limit):
    constructor = Mock()
    monkeypatch.setattr(quality_model, "AzureOpenAI", constructor)
    monkeypatch.setenv("QUALITY_MODEL_MAX_OUTPUT_TOKENS", limit)
    with pytest.raises(QualityModelConfigurationError, match="positive integer"):
        ResponsesCompletion(token_provider=lambda: "offline-token")
    constructor.assert_not_called()


@pytest.mark.parametrize("limit", [True, 0, -1, 1.2, float("inf")])
def test_invalid_constructor_limit(monkeypatch, limit):
    monkeypatch.setattr(quality_model, "AzureOpenAI", Mock())
    with pytest.raises(QualityModelConfigurationError, match="positive integer"):
        ResponsesCompletion(token_provider=lambda: "offline-token", max_output_tokens=limit)


@pytest.mark.parametrize("value", ["-0.1", "nan", "inf", "", "wrong"])
def test_invalid_prices(monkeypatch, value):
    monkeypatch.setenv("QUALITY_MODEL_INPUT_USD_PER_MILLION", value)
    with pytest.raises(QualityModelConfigurationError, match="finite nonnegative"):
        ResponsesCompletion(token_provider=lambda: "offline-token")


def test_endpoint_and_deployment_are_required(monkeypatch):
    monkeypatch.setattr(quality_model.settings, "LLM_DEPLOYMENT", None)
    with pytest.raises(QualityModelConfigurationError, match="QUALITY_MODEL_DEPLOYMENT"):
        ResponsesCompletion(token_provider=lambda: "offline-token")
    monkeypatch.setattr(quality_model.settings, "AI_FOUNDRY_ENDPOINT", None)
    with pytest.raises(QualityModelConfigurationError, match="LLM_ENDPOINT"):
        ResponsesCompletion(deployment="gpt-5", token_provider=lambda: "offline-token")


def test_strict_responses_request_and_validated_result(adapter):
    result = adapter.complete_structured("Extract grounded attributes.", "source text", Extraction)
    assert result == Extraction(attributes=[Attribute(value="brass")])
    request = adapter.client.responses.create.call_args.kwargs
    assert request["model"] == "gpt-5"
    assert request["instructions"] == "Extract grounded attributes."
    assert request["input"] == [{
        "role": "user", "content": [{"type": "input_text", "text": "source text"}],
    }]
    assert request["reasoning"] == {"effort": "medium"}
    assert request["max_output_tokens"] == 16000
    assert request["store"] is False
    assert isinstance(request["extra_query"]["api-version"], Omit)
    assert not {"messages", "response_format", "max_completion_tokens", "temperature"} & request.keys()
    output = request["text"]["format"]
    assert output["name"] == "Extraction"
    assert output["type"] == "json_schema"
    assert output["strict"] is True
    assert output["schema"]["additionalProperties"] is False
    nested = output["schema"]["$defs"]["Attribute"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["value", "evidence"]
    assert "default" not in nested["properties"]["evidence"]


def test_images_and_judge_effort_override(adapter):
    adapter.complete_structured("Judge.", "Check image.", Extraction, images=[IMAGE], reasoning_effort="low")
    request = adapter.client.responses.create.call_args.kwargs
    assert request["reasoning"] == {"effort": "low"}
    assert request["input"][0]["content"] == [
        {"type": "input_text", "text": "Check image."},
        {"type": "input_image", "image_url": IMAGE, "detail": "auto"},
    ]
    adapter.complete_structured("Extract.", "Check text.", Extraction)
    assert adapter.client.responses.create.call_args.kwargs["reasoning"] == {"effort": "medium"}


@pytest.mark.parametrize("image", ["https://example/image.png", "data:text/plain;base64,abc", "data:image/png,abc"])
def test_invalid_images_do_not_send(adapter, image):
    with pytest.raises(QualityModelConfigurationError, match="image data URLs"):
        adapter.complete_structured("s", "u", Extraction, images=[image])
    adapter.client.responses.create.assert_not_called()
    assert adapter.call_records == []


def test_usage_and_cost_do_not_double_charge_reasoning(adapter, monkeypatch):
    set_prices(monkeypatch)
    callback = Mock()
    model = ResponsesCompletion(token_provider=lambda: "offline-token", usage_callback=callback)
    model.complete_structured("PRIVATE SYSTEM", "PRIVATE SOURCE", Extraction)
    record = model.last_usage
    assert record["model"] == "gpt-5-test-version"
    assert record["deployment"] == "gpt-5"
    assert record["input_tokens"] == 1000
    assert record["cached_input_tokens"] == 200
    assert record["output_tokens"] == 500
    assert record["reasoning_tokens"] == 300
    assert record["estimated_cost_usd"] == pytest.approx((800 * 2 + 200 * 0.5 + 500 * 8) / 1e6)
    assert record["call_purpose"] == "Extraction"
    assert record["status"] == "completed"
    assert record["usage_reported"] is True
    assert model.call_records == [record]
    callback.assert_called_once_with(record)
    serialized = json.dumps(record, allow_nan=False)
    assert "PRIVATE" not in serialized
    assert "offline-token" not in serialized


def test_absent_prices_are_unknown_not_zero(adapter):
    adapter.complete_structured("s", "u", Extraction)
    assert adapter.last_usage["estimated_cost_usd"] is None
    assert adapter.last_usage["pricing_usd_per_million"] == {
        "input": None, "cached_input": None, "cache_write": None, "output": None,
    }
    assert adapter.last_usage["pricing_basis"] == "OpenAI public pricing estimate; not final Azure billing"
    assert adapter.last_usage["cache_write_tokens"] is None


def test_absent_usage_is_unknown_and_does_not_reuse_prior_usage(adapter):
    adapter.complete_structured("s", "u", Extraction)
    adapter.client.responses.create.return_value = response_payload(usage=None)
    adapter.complete_structured("s", "u", Extraction)
    assert adapter.last_usage["usage_reported"] is False
    assert adapter.last_usage["input_tokens"] is None
    assert adapter.last_usage["cached_input_tokens"] is None
    assert adapter.last_usage["reasoning_tokens"] is None
    assert adapter.last_usage["output_tokens"] is None
    assert adapter.last_usage["estimated_cost_usd"] is None
    assert adapter.call_records[0]["input_tokens"] == 1000
    assert len(adapter.call_records) == 2


def test_partial_usage_keeps_unknown_details_and_cost(adapter, monkeypatch):
    set_prices(monkeypatch)
    model = ResponsesCompletion(token_provider=lambda: "offline-token")
    model.client.responses.create.return_value = response_payload(
        usage={"input_tokens": 100, "output_tokens": 30},
    )
    model.complete_structured("s", "u", Extraction)
    assert model.last_usage["input_tokens"] == 100
    assert model.last_usage["output_tokens"] == 30
    assert model.last_usage["cached_input_tokens"] is None
    assert model.last_usage["cache_write_tokens"] is None
    assert model.last_usage["reasoning_tokens"] is None
    assert model.last_usage["total_tokens"] is None
    assert model.last_usage["estimated_cost_usd"] is None


def test_reported_zero_usage_and_zero_prices_are_not_unknown(adapter, monkeypatch):
    for name in ("INPUT", "CACHED_INPUT", "OUTPUT"):
        monkeypatch.setenv(f"QUALITY_MODEL_{name}_USD_PER_MILLION", "0")
    model = ResponsesCompletion(token_provider=lambda: "offline-token")
    model.client.responses.create.return_value = response_payload(usage={
        "input_tokens": 0, "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0, "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    })
    model.complete_structured("s", "u", Extraction)
    assert model.last_usage["usage_reported"] is True
    assert model.last_usage["input_tokens"] == 0
    assert model.last_usage["output_tokens"] == 0
    assert model.last_usage["estimated_cost_usd"] == 0


def test_cost_overflow_does_not_break_serializable_usage(adapter, monkeypatch):
    set_prices(monkeypatch)
    monkeypatch.setenv("QUALITY_MODEL_INPUT_USD_PER_MILLION", "1e308")
    model = ResponsesCompletion(token_provider=lambda: "offline-token")
    model.complete_structured("s", "u", Extraction)
    assert model.last_usage["estimated_cost_usd"] is None
    json.dumps(model.last_usage, allow_nan=False)


def test_reasoning_items_are_not_mistaken_for_structured_output(adapter):
    payload = response_payload()
    payload["output"].insert(0, {"type": "reasoning", "summary": []})
    adapter.client.responses.create.return_value = payload
    assert adapter.complete_structured("s", "u", Extraction).attributes[0].value == "brass"


@pytest.mark.parametrize("write_tokens,write_price,expected", [
    (100, "2.5", 0.00669),
    (0, None, 0.00664),
    (100, None, None),
    (900, "2.5", None),
])
def test_reported_cache_writes_have_a_separate_price_without_double_counting(
    adapter, monkeypatch, write_tokens, write_price, expected,
):
    monkeypatch.setenv("QUALITY_MODEL_INPUT_USD_PER_MILLION", "2")
    monkeypatch.setenv("QUALITY_MODEL_CACHED_INPUT_USD_PER_MILLION", "0.2")
    monkeypatch.setenv("QUALITY_MODEL_OUTPUT_USD_PER_MILLION", "10")
    monkeypatch.setenv("QUALITY_MODEL_PRICE_BASIS", "OpenAI public short-context; not Azure invoice")
    if write_price is not None:
        monkeypatch.setenv("QUALITY_MODEL_CACHE_WRITE_USD_PER_MILLION", write_price)
    model = ResponsesCompletion(token_provider=lambda: "offline-token")
    payload = response_payload()
    payload["usage"]["input_tokens_details"]["cache_write_tokens"] = write_tokens
    model.client.responses.create.return_value = payload
    model.complete_structured("s", "u", Extraction)
    assert model.last_usage["cache_write_tokens"] == write_tokens
    assert model.last_usage["pricing_usd_per_million"]["cache_write"] == (
        float(write_price) if write_price is not None else None
    )
    assert model.last_usage["pricing_basis"] == "OpenAI public short-context; not Azure invoice"
    if expected is None:
        assert model.last_usage["estimated_cost_usd"] is None
    else:
        assert model.last_usage["estimated_cost_usd"] == pytest.approx(expected)


@pytest.mark.parametrize("text", ["not JSON", "```json\n{}\n```", '{"attributes":[{"value":42}]}'])
def test_invalid_output_retains_real_usage_and_never_retries(adapter, text):
    callback = Mock()
    adapter.usage_callback = callback
    adapter.client.responses.create.return_value = response_payload(text)
    with pytest.raises(QualityModelResponseError, match="did not validate") as error:
        adapter.complete_structured("s", "u", Extraction)
    assert isinstance(error.value.__cause__, ValidationError)
    assert adapter.last_usage["input_tokens"] == 1000
    assert adapter.last_usage["output_tokens"] == 500
    assert adapter.last_usage["status"] == "response_invalid"
    assert adapter.last_usage["error_type"] == "QualityModelResponseError"
    assert len(adapter.call_records) == 1
    callback.assert_called_once_with(adapter.last_usage)
    adapter.client.responses.create.assert_called_once()


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "max_output_tokens"),
        ({"status": "failed", "error": {"code": "server_error"}}, "server_error"),
        ({"status": "in_progress"}, "not completed"),
        ({"output": []}, "no structured output"),
        ({"output": [{"type": "message", "status": "incomplete", "content": []}]}, "incomplete output"),
        ({"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]}, "refused"),
    ],
)
def test_non_success_outputs_retain_usage(adapter, overrides, match):
    adapter.client.responses.create.return_value = response_payload(**overrides)
    with pytest.raises(QualityModelResponseError, match=match):
        adapter.complete_structured("s", "u", Extraction)
    assert adapter.last_usage["input_tokens"] == 1000
    assert adapter.last_usage["output_tokens"] == 500
    assert len(adapter.call_records) == 1


def test_sdk_error_surfaces_and_does_not_invent_usage(adapter):
    callback = Mock()
    adapter.usage_callback = callback
    failure = BadRequestError(
        "Deployment does not support requested reasoning",
        response=httpx.Response(400, request=httpx.Request("POST", ENDPOINT)),
        body={"error": {"code": "unsupported_parameter"}},
    )
    adapter.client.responses.create.side_effect = failure
    with pytest.raises(QualityModelError, match="Deployment does not support") as error:
        adapter.complete_structured("s", "u", Extraction)
    assert error.value.__cause__ is failure
    assert adapter.last_usage["status"] == "request_failed"
    assert adapter.last_usage["error_type"] == "BadRequestError"
    assert adapter.last_usage["input_tokens"] is None
    assert adapter.last_usage["estimated_cost_usd"] is None
    callback.assert_called_once_with(adapter.last_usage)


def test_callback_cannot_mutate_retained_records(adapter):
    def callback(record):
        record["input_tokens"] = -1
        record["pricing_usd_per_million"]["input"] = -1

    adapter.usage_callback = callback
    adapter.complete_structured("s", "u", Extraction)
    assert adapter.last_usage["input_tokens"] == 1000
    assert adapter.call_records[0]["input_tokens"] == 1000
    assert adapter.last_usage["pricing_usd_per_million"]["input"] is None
    adapter.last_usage["input_tokens"] = 0
    assert adapter.call_records[0]["input_tokens"] == 1000


def test_callback_failure_surfaces_after_usage_is_retained(adapter):
    adapter.usage_callback = Mock(side_effect=ValueError("callback offline"))
    with pytest.raises(QualityModelError, match="Usage callback failed"):
        adapter.complete_structured("s", "u", Extraction)
    assert adapter.last_usage["output_tokens"] == 500
    assert len(adapter.call_records) == 1


def test_callback_failure_does_not_replace_schema_error(adapter):
    adapter.client.responses.create.return_value = response_payload("invalid")
    adapter.usage_callback = Mock(side_effect=ValueError("callback offline"))
    with pytest.raises(QualityModelResponseError, match="did not validate") as error:
        adapter.complete_structured("s", "u", Extraction)
    assert "callback also failed" in error.value.__notes__[0]
    assert adapter.last_usage["output_tokens"] == 500


@pytest.mark.parametrize("invalid_json", [False, True])
@pytest.mark.parametrize("deployment", ["gpt-5", "gpt-6-sol"])
@pytest.mark.parametrize("endpoint", [
    ENDPOINT,
    "https://quality-test.cognitiveservices.azure.com/",
    "https://quality-test.cognitiveservices.azure.com/openai/v1/",
])
def test_native_locked_sdk_serializes_responses_and_retains_usage(
    monkeypatch, invalid_json, deployment, endpoint,
):
    captured = []
    payload = response_payload("invalid" if invalid_json else '{"attributes":[]}')
    token_provider = Mock(return_value="offline-token")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "unused-offline-test-key")

    def transport(request):
        captured.append(request)
        return httpx.Response(200, json=deepcopy(payload))

    with httpx.Client(transport=httpx.MockTransport(transport)) as http_client:
        def client_factory(**kwargs):
            return AzureOpenAI(**kwargs, http_client=http_client)

        monkeypatch.setattr(quality_model, "AzureOpenAI", client_factory)
        set_prices(monkeypatch)
        model = ResponsesCompletion(
            endpoint=endpoint, token_provider=token_provider, deployment=deployment,
        )
        if invalid_json:
            with pytest.raises(QualityModelResponseError, match="did not validate"):
                model.complete_structured("system", "user", Extraction, images=[IMAGE], reasoning_effort="low")
        else:
            assert model.complete_structured(
                "system", "user", Extraction, images=[IMAGE], reasoning_effort="low",
            ) == Extraction(attributes=[])

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/openai/v1/responses"
    assert not request.url.query
    assert request.headers["authorization"] == "Bearer offline-token"
    assert "api-key" not in request.headers
    token_provider.assert_called_once()
    body = json.loads(request.content)
    assert body["model"] == deployment
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_output_tokens"] == 16000
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["input"][0]["content"][1] == {
        "type": "input_image", "image_url": IMAGE, "detail": "auto",
    }
    assert model.last_usage["model"] == "gpt-5-test-version"
    assert model.last_usage["input_tokens"] == 1000
    assert model.last_usage["reasoning_tokens"] == 300
    assert model.last_usage["estimated_cost_usd"] == pytest.approx(0.0057)


def test_locked_sdk_sends_family_key_and_explicit_prefix_breakpoint(monkeypatch):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response_payload('{"attributes":[]}'))

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(quality_model, "AzureOpenAI", lambda **kwargs: AzureOpenAI(**kwargs, http_client=client))
        model = ResponsesCompletion(token_provider=lambda: "offline-token")
        for item in ("product-a", "product-b"):
            model.complete_structured(
                "same instructions", item, Extraction,
                prompt_cache_key="family-v1", cache_prefix="shared definitions and source text",
            )
    assert len(captured) == 2
    assert captured[0]["prompt_cache_key"] == captured[1]["prompt_cache_key"] == "family-v1"
    assert captured[0]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    first, second = [body["input"][0]["content"] for body in captured]
    assert first[0] == second[0] == {
        "type": "input_text", "text": "shared definitions and source text",
        "prompt_cache_breakpoint": {"mode": "explicit"},
    }
    assert first[1]["text"] == "product-a" and second[1]["text"] == "product-b"
    assert model.last_usage["cached_input_tokens"] == 200
    assert model.last_usage["explicit_cache_prefix"] is True


@pytest.mark.parametrize("invalid_json", [False, True])
def test_locked_sdk_preserves_newer_cache_write_usage_fields(monkeypatch, invalid_json):
    payload = response_payload("invalid" if invalid_json else '{"attributes":[]}')
    payload["usage"]["input_tokens_details"]["cache_write_tokens"] = 100
    captured_records = []
    monkeypatch.setenv("QUALITY_MODEL_INPUT_USD_PER_MILLION", "2")
    monkeypatch.setenv("QUALITY_MODEL_CACHED_INPUT_USD_PER_MILLION", "0.2")
    monkeypatch.setenv("QUALITY_MODEL_CACHE_WRITE_USD_PER_MILLION", "2.5")
    monkeypatch.setenv("QUALITY_MODEL_OUTPUT_USD_PER_MILLION", "10")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        monkeypatch.setattr(
            quality_model, "AzureOpenAI", lambda **kwargs: AzureOpenAI(**kwargs, http_client=client),
        )
        model = ResponsesCompletion(
            token_provider=lambda: "offline-token", deployment="gpt-6-sol",
            usage_callback=captured_records.append,
        )
        if invalid_json:
            with pytest.raises(QualityModelResponseError):
                model.complete_structured("s", "u", Extraction)
        else:
            model.complete_structured("s", "u", Extraction)
    assert captured_records[0]["cache_write_tokens"] == 100
    assert captured_records[0]["estimated_cost_usd"] == pytest.approx(0.00669)
    assert captured_records[0] == model.last_usage
