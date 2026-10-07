"""Native constructors and real serialization; all sockets/auth/processes denied."""

import asyncio
import hashlib
from importlib.metadata import version
from io import BytesIO
import json
import socket
import subprocess
from urllib.parse import parse_qs, urlsplit

from azure.identity import AzureCliCredential, DefaultAzureCredential, ManagedIdentityCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
import httpx
import pytest

from backend.batch_worker import prepare_inference_request
from backend.core import llm, websearch_webiq
from backend.models.enrichment import ExtractionResponse
from backend import sdk_preflight
from backend.sdk_preflight import SDKPreflightError


ENDPOINT = "https://native-sdk-test.example"
PRIVATE = "PRIVATE-PROMPT-NOT-FOR-RECEIPTS"


@pytest.fixture(autouse=True)
def no_network_or_credentials(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Network, credentials, and subprocesses are forbidden")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    for credential in (AzureCliCredential, DefaultAzureCredential, ManagedIdentityCredential):
        monkeypatch.setattr(credential, "get_token", denied)


def prepared_request():
    return prepare_inference_request(
        "Preserve grounding, values, units, applicability and Boolean rules.",
        json.dumps({
            "product": {"vendor": "PUBLIC", "mpn": "VALVE-1"},
            "attributes": [{"attribute_id": "size", "definition": PRIVATE, "unit": "in"}],
            "evidence": [{
                "evidence_id": "source:sha:row1", "source_locator": "private-source#row1",
                "source_tier": "vendor_table", "text": PRIVATE + " 1/2 inch",
                "attribute_ids": ["size"], "qualification": "Exact approved MPN only.",
            }],
        }),
        ExtractionResponse, deployment="gpt-5",
    )


def assert_receipt(receipt, provider, package):
    assert receipt["provider"] == provider
    assert receipt["status"] == "validated_no_send"
    assert receipt["sdk_version"] == version(package)
    assert receipt["method"] == "POST"
    assert receipt["body_bytes"] > 0
    assert len(receipt["body_sha256"]) == len(receipt["request_sha256"]) == 64
    assert receipt["authentication_performed"] is False
    assert receipt["provider_send_performed"] is False
    assert receipt["provider_response_fabricated"] is False
    assert PRIVATE not in json.dumps(receipt)
    assert "native-sdk-no-send-placeholder" not in json.dumps(receipt)
    assert ENDPOINT not in json.dumps(receipt)


def test_native_model_uses_full_actual_prepared_prompt_schema_and_gpt5_parameters(monkeypatch):
    prepared = prepared_request()
    captures = []
    native_capture = sdk_preflight.HTTPRequestCapture.__call__

    def observe(self, request):
        captures.append((str(request.url), request.read()))
        return native_capture(self, request)

    monkeypatch.setattr(sdk_preflight.HTTPRequestCapture, "__call__", observe)
    receipt = llm.preflight_structured_request(
        prepared.system, prepared.user, ExtractionResponse,
        endpoint=ENDPOINT, deployment="gpt-5", **prepared.request_parameters,
    )
    assert_receipt(receipt, "model", "openai")
    assert len(captures) == 1
    url, raw_body = captures[0]
    body = json.loads(raw_body)
    assert "/openai/deployments/gpt-5/chat/completions?" in url
    assert receipt["api_version"] == llm.LLM_API_VERSION
    assert body == {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": prepared.system},
            {"role": "user", "content": prepared.user},
        ],
        "response_format": llm._response_format(ExtractionResponse, True),
        "max_completion_tokens": 2048, "reasoning_effort": "minimal",
    }
    assert PRIVATE in body["messages"][1]["content"]
    assert receipt["body_sha256"] == hashlib.sha256(raw_body).hexdigest()
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


@pytest.mark.parametrize("changes", [
    {"unknown_native_parameter": PRIVATE},
    {"temperature": float("nan")},
    {"temperature": 0},
    {"temperature": True},
    {"max_tokens": 2048},
    {"max_tokens": 2048, "max_completion_tokens": 2048},
    {"max_completion_tokens": True},
    {"max_completion_tokens": 0},
    {"max_completion_tokens": "2048"},
    {"extra_body": {"temperature": 0}},
    {"extra_body": {"messages": PRIVATE}},
    {"stream": True},
    {"api_version": PRIVATE},
    {"endpoint": f"https://{PRIVATE}:password@example.test"},
])
def test_invalid_model_request_blocks_without_disclosing_input(changes):
    with pytest.raises(SDKPreflightError) as caught:
        llm.preflight_structured_request(
            PRIVATE, PRIVATE, ExtractionResponse,
            **{"endpoint": ENDPOINT, "deployment": "gpt-5", **changes},
        )
    assert "live execution blocked" in str(caught.value)
    assert PRIVATE not in str(caught.value)


def test_receipt_changes_when_actual_evidence_or_schema_parameters_change():
    request = prepared_request()
    kwargs = {"endpoint": ENDPOINT, "deployment": "gpt-5", **request.request_parameters}
    first = llm.preflight_structured_request(request.system, request.user, ExtractionResponse, **kwargs)
    second = llm.preflight_structured_request(request.system, request.user + " ", ExtractionResponse, **kwargs)
    assert first["request_sha256"] != second["request_sha256"]
    assert first["body_bytes"] + 1 == second["body_bytes"]


def test_gpt5_default_temperature_and_other_models_legacy_options_remain_valid():
    default = llm.preflight_structured_request(
        "system", "user", ExtractionResponse, endpoint=ENDPOINT, deployment="gpt-5",
        temperature=1, max_completion_tokens=2048,
    )
    other = llm.preflight_structured_request(
        "system", "user", ExtractionResponse, endpoint=ENDPOINT, deployment="gpt-4o",
        temperature=0, max_tokens=2048,
    )
    assert_receipt(default, "model", "openai")
    assert_receipt(other, "model", "openai")


def test_preflight_never_uses_ambient_azure_openai_secrets(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", PRIVATE)
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", PRIVATE)
    captured_headers = []
    capture = sdk_preflight.HTTPRequestCapture.__call__

    def observe(self, request):
        captured_headers.append(dict(request.headers))
        return capture(self, request)

    monkeypatch.setattr(sdk_preflight.HTTPRequestCapture, "__call__", observe)
    receipt = llm.preflight_structured_request(
        "system", "user", ExtractionResponse, endpoint=ENDPOINT, deployment="gpt-5",
    )
    assert_receipt(receipt, "model", "openai")
    assert PRIVATE not in json.dumps(captured_headers)
    assert captured_headers[0]["authorization"] == "Bearer native-sdk-no-send-placeholder"


def test_model_self_gate_precedes_real_token_provider():
    class ReachedRealCredential(BaseException):
        pass

    def credential():
        raise ReachedRealCredential()

    client = llm.LLMClient(endpoint=ENDPOINT, deployment="gpt-5", token_provider=credential)
    try:
        with pytest.raises(ReachedRealCredential):
            client.complete_structured(
                PRIVATE, PRIVATE, ExtractionResponse, max_retries=1,
                max_completion_tokens=2048, reasoning_effort="minimal",
            )
        assert_receipt(client.last_sdk_preflight, "model", "openai")
        client.last_sdk_preflight = None
        with pytest.raises(SDKPreflightError):
            client.complete_structured(
                PRIVATE, PRIVATE, ExtractionResponse, max_retries=1,
                nonexistent_parameter=PRIVATE,
            )
        assert client.last_sdk_preflight is None
    finally:
        client.sync_client.close()
        asyncio.run(client.async_client.close())


def test_model_production_serialization_matches_preflight_without_a_fake_response():
    class ProductionRequestCaptured(BaseException):
        pass

    native = sdk_preflight._openai_http_module()
    requests = []
    client = llm.LLMClient(
        endpoint=ENDPOINT, deployment="gpt-5",
        token_provider=lambda: "unit-test-not-a-real-token",
    )

    def capture_production(request):
        requests.append(request)
        assert client.last_sdk_preflight["body_sha256"] == hashlib.sha256(request.read()).hexdigest()
        raise ProductionRequestCaptured()

    original = client.sync_client
    client.sync_client = original.with_options(
        http_client=native.Client(transport=native.MockTransport(capture_production), trust_env=False),
        max_retries=0,
    )
    original.close()
    prepared = prepared_request()
    try:
        with pytest.raises(ProductionRequestCaptured):
            client.complete_structured(
                prepared.system, prepared.user, ExtractionResponse,
                max_retries=1, **prepared.request_parameters,
            )
        assert len(requests) == 1
        assert_receipt(client.last_sdk_preflight, "model", "openai")
    finally:
        client.sync_client.close()
        asyncio.run(client.async_client.close())


@pytest.mark.asyncio
async def test_async_native_self_gate_precedes_real_token_provider():
    class ReachedRealCredential(BaseException):
        pass

    def credential():
        raise ReachedRealCredential()

    client = llm.LLMClient(endpoint=ENDPOINT, deployment="gpt-5", token_provider=credential)
    try:
        with pytest.raises(ReachedRealCredential):
            await client.acomplete_structured(
                PRIVATE, PRIVATE, ExtractionResponse, max_retries=1,
                max_completion_tokens=2048, reasoning_effort="minimal",
            )
        assert_receipt(client.last_sdk_preflight, "model", "openai")
    finally:
        client.sync_client.close()
        await client.async_client.close()


def test_native_webiq_preflight_exact_query_and_host_filter(monkeypatch):
    requests = []
    capture = sdk_preflight.HTTPRequestCapture.__call__

    def observe(self, request):
        requests.append(request)
        return capture(self, request)

    monkeypatch.setattr(sdk_preflight.HTTPRequestCapture, "__call__", observe)
    client = websearch_webiq.WebIQSearchClient(websearch_webiq.ENDPOINT, PRIVATE, max_results=2)
    receipt = client.preflight_search("Public manufacturer VALVE-1 size", ["vendor.example"], authorized=True)
    assert_receipt(receipt, "webiq", "httpx")
    assert receipt["discovery_only_not_evidence"] is True
    assert receipt["allowed_domains_count"] == 1
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "query": "Public manufacturer VALVE-1 size", "maxResults": 2,
        "contentFormat": "passage", "maxLength": 2000,
    }
    assert requests[0].headers["x-apikey"] == PRIVATE
    other = client.preflight_search("Public manufacturer VALVE-1 size", ["other.example"], authorized=True)
    assert receipt["body_sha256"] == other["body_sha256"]
    assert receipt["allowed_domains_sha256"] != other["allowed_domains_sha256"]


@pytest.mark.parametrize("query,hosts,authorized", [
    (PRIVATE, ["vendor.example"], True),
    ("Public MPN", ["*.example"], True),
    ("Public MPN", ["vendor.example"], False),
])
def test_webiq_preflight_validation_is_fatal_before_reservation(query, hosts, authorized):
    client = websearch_webiq.WebIQSearchClient(websearch_webiq.ENDPOINT, PRIVATE)
    with pytest.raises(SDKPreflightError) as caught:
        client.preflight_search(query, hosts, authorized=authorized)
    assert PRIVATE not in str(caught.value)
    assert client.last_sdk_preflight is None


def test_webiq_live_path_reuses_exact_preflight_request_and_native_constructor(monkeypatch):
    """The second transport is an explicit synthetic response, never a finding."""
    sent = []
    client = websearch_webiq.WebIQSearchClient(websearch_webiq.ENDPOINT, PRIVATE)

    def synthetic_response(request):
        assert client.last_sdk_preflight is not None
        sent.append(request)
        return httpx.Response(200, json={"webResults": []})

    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(synthetic_response))
    assert client.search("Public MPN size", ["vendor.example"], authorized=True) == []
    assert len(sent) == 1
    assert client.last_sdk_preflight["body_sha256"] == hashlib.sha256(sent[0].content).hexdigest()


def di_client_options():
    return {
        "endpoint": ENDPOINT, "api_version": "2024-11-30",
        "credential": AzureCliCredential(tenant_id="00000000-0000-0000-0000-000000000001"),
        "retry_total": 0, "retry_connect": 0, "retry_read": 0, "retry_status": 0,
        "connection_timeout": 10, "read_timeout": 30,
    }


def test_native_di_uses_exact_binary_body_pages_and_api_without_real_auth(monkeypatch):
    source = b"%PDF-1.4\nSYNTHETIC SERIALIZATION ONLY " + PRIVATE.encode()
    body = BytesIO(source)
    captures = []
    receipt_builder = sdk_preflight._receipt

    def observe(**kwargs):
        captures.append(kwargs)
        return receipt_builder(**kwargs)

    monkeypatch.setattr(sdk_preflight, "_receipt", observe)
    receipt = sdk_preflight.preflight_document_intelligence(
        client_options=di_client_options(),
        analyze_arguments={"model_id": "prebuilt-layout", "body": body, "pages": "1-5"},
    )
    assert_receipt(receipt, "document_intelligence", "azure-ai-documentintelligence")
    assert receipt["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert receipt["source_bytes"] == len(source)
    assert body.tell() == 0
    assert len(captures) == 1 and captures[0]["body"] == source
    parsed = urlsplit(captures[0]["url"])
    assert parsed.path == "/documentintelligence/documentModels/prebuilt-layout:analyze"
    assert parse_qs(parsed.query) == {"api-version": ["2024-11-30"], "pages": ["1-5"]}
    assert receipt["path"] == parsed.path
    assert receipt["model_id"] == "prebuilt-layout"
    assert receipt["query"] == parse_qs(parsed.query)
    assert receipt["query_is_complete"] is True
    assert receipt["request_options"] == {"pages": "1-5"}
    assert receipt["pages"] == "1-5"
    assert receipt["transport"] == "in_memory_no_send"
    assert receipt["captured_requests"] == 1
    assert receipt["network_calls"] == receipt["credential_real_calls"] == receipt["transport_real_calls"] == 0


@pytest.mark.parametrize("changes", [
    {"body": {"urlSource": "https://unapproved.example/private"}},
    {"pages": object()},
    {"unknown_native_parameter": PRIVATE},
    {"body": BytesIO(b"")},
])
def test_invalid_di_request_fails_fatally_without_authentication(changes):
    arguments = {"model_id": "prebuilt-layout", "body": BytesIO(b"%PDF-TEST"), "pages": "1-5", **changes}
    with pytest.raises(SDKPreflightError) as caught:
        sdk_preflight.preflight_document_intelligence(
            client_options=di_client_options(), analyze_arguments=arguments,
        )
    assert PRIVATE not in str(caught.value)
    if isinstance(arguments["body"], BytesIO):
        assert arguments["body"].tell() == 0


def test_di_changed_page_bound_changes_native_request_fingerprint():
    arguments = {"model_id": "prebuilt-layout", "body": BytesIO(b"%PDF-TEST"), "pages": "1-5"}
    first = sdk_preflight.preflight_document_intelligence(
        client_options=di_client_options(), analyze_arguments=arguments,
    )
    second = sdk_preflight.preflight_document_intelligence(
        client_options=di_client_options(), analyze_arguments={**arguments, "pages": "1-4"},
    )
    assert first["body_sha256"] == second["body_sha256"]
    assert first["request_sha256"] != second["request_sha256"]


def test_di_preflight_invokes_production_factory_and_exact_request_builder_output():
    constructed = []

    def factory(*, credential, transport):
        options = {**di_client_options(), "credential": credential, "transport": transport}
        client = DocumentIntelligenceClient(**options)
        constructed.append(client)
        return client

    source = b"%PDF-SYNTHETIC-NATIVE-FACTORY"
    arguments = {"model_id": "prebuilt-layout", "body": BytesIO(source), "pages": "1-5"}
    receipt = sdk_preflight.preflight_document_intelligence(
        source_bytes=source, client_factory=factory, analyze_kwargs=arguments,
    )
    assert_receipt(receipt, "document_intelligence", "azure-ai-documentintelligence")
    assert len(constructed) == 1
    assert isinstance(constructed[0], DocumentIntelligenceClient)
    assert arguments["body"].tell() == 0
    assert receipt["source_sha256"] == hashlib.sha256(source).hexdigest()


@pytest.mark.parametrize("ignored", ["credential", "transport"])
def test_di_factory_must_honor_both_no_send_overrides_before_native_policy_execution(ignored):
    def factory(*, credential, transport):
        options = di_client_options()
        if ignored != "credential":
            options["credential"] = credential
        if ignored != "transport":
            options["transport"] = transport
        return DocumentIntelligenceClient(**options)

    source = b"%PDF-TEST"
    with pytest.raises(SDKPreflightError) as caught:
        sdk_preflight.preflight_document_intelligence(
            source_bytes=source, client_factory=factory,
            analyze_kwargs={"model_id": "prebuilt-layout", "body": BytesIO(source), "pages": "1-5"},
        )
    assert caught.value.error_type == "ValueError"


def test_di_factory_request_cannot_change_source_bytes():
    def not_reached(**kwargs):
        raise AssertionError("Factory must not be reached")

    body = BytesIO(b"%PDF-CHANGED")
    with pytest.raises(SDKPreflightError) as caught:
        sdk_preflight.preflight_document_intelligence(
            source_bytes=b"%PDF-ORIGINAL", client_factory=not_reached,
            analyze_kwargs={"model_id": "prebuilt-layout", "body": body, "pages": "1-5"},
        )
    assert caught.value.error_type == "ValueError"
    assert body.tell() == 0


def test_di_factory_receipt_is_deterministic_for_pinned_continuation_comparison():
    def factory(*, credential, transport):
        return DocumentIntelligenceClient(
            **{**di_client_options(), "credential": credential, "transport": transport},
        )

    source = b"%PDF-EXACT-SAME-SOURCE"
    arguments = {"model_id": "prebuilt-layout", "body": BytesIO(source), "pages": "1-5"}
    first = sdk_preflight.preflight_document_intelligence(
        source_bytes=source, client_factory=factory, analyze_kwargs=arguments,
    )
    second = sdk_preflight.preflight_document_intelligence(
        source_bytes=source, client_factory=factory, analyze_kwargs=arguments,
    )
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["captured_requests"] == second["captured_requests"] == 1


def test_di_receipt_does_not_expose_private_endpoint_path_or_custom_model_name():
    receipt = sdk_preflight.preflight_document_intelligence(
        client_options={**di_client_options(), "endpoint": ENDPOINT + "/" + PRIVATE},
        analyze_arguments={"model_id": PRIVATE, "body": BytesIO(b"%PDF-TEST"), "pages": "1-5"},
    )
    assert PRIVATE not in json.dumps(receipt)
    assert "path" not in receipt and "model_id" not in receipt
    assert len(receipt["path_sha256"]) == len(receipt["model_id_sha256"]) == 64


def test_shared_preparation_producer_returns_exact_repeatable_native_di_proof():
    from backend.analysis_provenance import API_VERSION, REQUEST_OPTIONS, preflight_preparation

    source = b"%PDF-SYNTHETIC-SHARED-PRODUCTION-BUILDER"
    first = preflight_preparation(source, endpoint=ENDPOINT)
    second = preflight_preparation(source, endpoint=ENDPOINT)
    assert first == second
    assert_receipt(first, "document_intelligence", "azure-ai-documentintelligence")
    assert first["api_version"] == API_VERSION
    assert first["request_options"] == REQUEST_OPTIONS == {"pages": "1-5"}
    assert first["content_type"] == "application/octet-stream"
    assert first["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert first["source_bytes"] == len(source)
    assert first["transport_real_calls"] == first["credential_real_calls"] == first["network_calls"] == 0
