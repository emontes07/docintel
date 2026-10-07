"""Native SDK serialization gates: in-memory transport, no authentication or send.

These receipts prove local constructor/request compatibility, not provider
acceptance, identity, entitlement, source grounding, or a successful response.
"""

import hashlib
from importlib import import_module
from importlib.metadata import version
import inspect
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import openai


class SDKPreflightError(RuntimeError):
    """Fatal local incompatibility; must escape before any budget reservation."""

    def __init__(self, provider: str, error_type: str):
        self.provider = provider
        self.error_type = error_type
        super().__init__(f"{provider} native SDK no-send preflight failed ({error_type}); live execution blocked")


class _RequestCaptured(BaseException):
    """Stop at the transport without SDK retries or a fabricated provider reply."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _receipt(*, provider, sdk_package, transport_package, method, url, body, content_type):
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HTTPS endpoint without embedded credentials required")
    if content_type not in ("application/json", "application/octet-stream", "application/pdf"):
        raise ValueError("Unexpected native content type")
    query = parse_qs(parsed.query)
    api_version = query.get("api-version", [None])[0]
    if api_version is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-preview)?", api_version):
        raise ValueError("Invalid API version")
    return {
        "status": "validated_no_send",
        "provider": provider,
        "sdk_package": sdk_package,
        "sdk_version": version(sdk_package),
        "transport_package": transport_package,
        "transport_version": version(transport_package),
        "method": method,
        "api_version": api_version,
        "endpoint_sha256": _digest(str(url).encode()),
        "body_sha256": _digest(body),
        "body_bytes": len(body),
        "content_type": content_type,
        "request_sha256": _digest(method.encode() + b"\n" + str(url).encode() + b"\n" + body),
        "authentication_performed": False,
        "provider_send_performed": False,
        "provider_response_fabricated": False,
    }


class HTTPRequestCapture:
    """An in-memory handler for native httpx/httpx2 MockTransport instances."""

    def __init__(self, *, provider, sdk_package, transport_package, expected_body):
        self.provider = provider
        self.sdk_package = sdk_package
        self.transport_package = transport_package
        self.expected_body = expected_body
        self.receipt = None

    def __call__(self, request):
        body = request.read()
        if request.method != "POST" or json.loads(body) != self.expected_body:
            raise ValueError("Native serialized request differs from production request")
        self.receipt = _receipt(
            provider=self.provider, sdk_package=self.sdk_package,
            transport_package=self.transport_package, method=request.method,
            url=request.url, body=body, content_type=request.headers.get("content-type"),
        )
        safe_fields = {
            "model", "messages", "response_format", "max_completion_tokens",
            "max_tokens", "reasoning_effort", "temperature", "stream",
            "query", "maxResults", "contentFormat", "maxLength",
        }
        self.receipt["body_fields"] = sorted(set(self.expected_body).intersection(safe_fields))
        self.receipt["body_field_count"] = len(self.expected_body)
        raise _RequestCaptured()

    async def async_capture(self, request):
        await request.aread()
        return self(request)


def _openai_http_module():
    # OpenAI 1.x uses httpx; newer releases use httpx2. Use the installed SDK's
    # exported native default client, rather than guessing its transport type.
    for base in openai.DefaultHttpxClient.__mro__:
        module = base.__module__.split(".", 1)[0]
        if module in ("httpx", "httpx2"):
            return import_module(module)
    raise TypeError("Unsupported OpenAI native HTTP client")


def _openai_capture(request_arguments):
    body = dict(request_arguments)
    for option in ("extra_headers", "extra_query", "timeout"):
        body.pop(option, None)
    extra_body = body.pop("extra_body", None)
    if extra_body:
        if not isinstance(extra_body, dict) or set(extra_body).intersection(body):
            raise ValueError("Extra body must not override the validated request")
        body.update(extra_body)
    if body.get("stream"):
        raise ValueError("Structured completion requires a non-streaming response")
    if "max_tokens" in body and "max_completion_tokens" in body:
        raise ValueError("Only one completion-token limit may be supplied")
    for parameter in ("max_tokens", "max_completion_tokens"):
        value = body.get(parameter)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError("Completion-token limits must be positive integers")
    if body.get("model") == "gpt-5":
        # The installed SDK serializes these without model-specific checking.
        # Preserve the producer's reasoning-model parameter contract locally.
        if "max_tokens" in body:
            raise ValueError("GPT-5 requires max_completion_tokens")
        temperature = body.get("temperature")
        if temperature is not None and (type(temperature) not in (int, float) or temperature != 1):
            raise ValueError("GPT-5 does not support a non-default temperature")
    return body


def _offline_openai_options(client_options, http_client):
    options = dict(client_options)
    if set(options).intersection(("api_key", "azure_ad_token", "base_url")):
        raise ValueError("Unexpected production authentication or endpoint mode")
    options["azure_ad_token_provider"] = lambda: "native-sdk-no-send-placeholder"
    # Explicit sentinels also prevent the native constructor from loading
    # ambient AZURE_OPENAI_API_KEY / AZURE_OPENAI_AD_TOKEN credentials.
    options["api_key"] = "native-sdk-no-send-placeholder"
    options["azure_ad_token"] = "native-sdk-no-send-placeholder"
    options["http_client"] = http_client
    return options


def preflight_openai_request(*, client_options: dict, request_arguments: dict) -> dict:
    """Invoke genuine AzureOpenAI.create with production args and no-send HTTP."""
    try:
        native = _openai_http_module()
        capture = HTTPRequestCapture(
            provider="model", sdk_package="openai", transport_package=native.__name__,
            expected_body=_openai_capture(request_arguments),
        )
        with native.Client(transport=native.MockTransport(capture), trust_env=False) as http_client:
            with openai.AzureOpenAI(**_offline_openai_options(client_options, http_client)) as client:
                try:
                    client.chat.completions.create(**request_arguments)
                except _RequestCaptured:
                    pass
        if capture.receipt is None:
            raise ValueError("Native request was not captured")
        return capture.receipt
    except Exception as error:
        raise SDKPreflightError("model", type(error).__name__) from None


async def apreflight_openai_request(*, client_options: dict, request_arguments: dict) -> dict:
    """The same gate through genuine AsyncAzureOpenAI and its native transport."""
    try:
        native = _openai_http_module()
        capture = HTTPRequestCapture(
            provider="model", sdk_package="openai", transport_package=native.__name__,
            expected_body=_openai_capture(request_arguments),
        )
        async with native.AsyncClient(
            transport=native.MockTransport(capture.async_capture), trust_env=False,
        ) as http_client:
            async with openai.AsyncAzureOpenAI(**_offline_openai_options(client_options, http_client)) as client:
                try:
                    await client.chat.completions.create(**request_arguments)
                except _RequestCaptured:
                    pass
        if capture.receipt is None:
            raise ValueError("Native request was not captured")
        return capture.receipt
    except Exception as error:
        raise SDKPreflightError("model", type(error).__name__) from None


def preflight_httpx_request(*, client_options: dict, request_arguments: dict) -> dict:
    """Serialize the production WebIQ stream request using genuine httpx."""
    try:
        capture = HTTPRequestCapture(
            provider="webiq", sdk_package="httpx", transport_package="httpx",
            expected_body=request_arguments["json"],
        )
        with httpx.Client(**client_options, transport=httpx.MockTransport(capture)) as client:
            try:
                with client.stream(**request_arguments):
                    raise ValueError("No-send transport returned a response")
            except _RequestCaptured:
                pass
        if capture.receipt is None:
            raise ValueError("Native request was not captured")
        return capture.receipt
    except Exception as error:
        raise SDKPreflightError("webiq", type(error).__name__) from None


def preflight_document_intelligence(
    *, source_bytes: bytes | None = None, client_factory=None, analyze_kwargs: dict | None = None,
    client_options: dict | None = None, analyze_arguments: dict | None = None,
) -> dict:
    """Validate the exact DI constructor and analyze arguments, without sending.

    Prefer the production client factory plus its request builder's kwargs.
    The factory receives only inert ``credential`` and ``transport`` overrides;
    bind its endpoint beforehand. The shared-dictionary form is also supported.
    A seekable binary body is restored after validating exact source bytes.
    """
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AccessToken
    from azure.core.pipeline.transport import HttpTransport

    class NoSendCredential:
        def get_token(self, *scopes, **kwargs):
            return AccessToken("native-sdk-no-send-placeholder", 4102444800)

    class NoSendTransport(HttpTransport):
        def __init__(self, expected_body):
            self.expected_body = expected_body
            self.receipt = None
            self.captured_requests = 0

        def open(self):
            pass

        def close(self):
            pass

        def __exit__(self, *args):
            self.close()

        def send(self, request, **kwargs):
            self.captured_requests += 1
            body = request.body
            if hasattr(body, "read"):
                body = body.read()
            if request.method != "POST" or body != self.expected_body:
                raise ValueError("Native serialized DI request differs from source bytes")
            self.receipt = _receipt(
                provider="document_intelligence", sdk_package="azure-ai-documentintelligence",
                transport_package="azure-core", method=request.method, url=request.url,
                body=body, content_type=request.headers.get("Content-Type"),
            )
            self.receipt["source_sha256"] = _digest(body)
            self.receipt["source_bytes"] = len(body)
            parsed = urlsplit(request.url)
            query = parse_qs(parsed.query)
            safe_query = {key: value for key, value in query.items() if key in ("api-version", "pages")}
            self.receipt.update(
                transport="in_memory_no_send",
                transport_real_calls=0, network_calls=0, credential_real_calls=0,
                captured_requests=self.captured_requests,
                query=safe_query, query_is_complete=len(safe_query) == len(query),
                query_sha256=_digest(parsed.query.encode()),
                pages=query.get("pages", [None])[0],
                request_options={"pages": query["pages"][0]} if "pages" in query else {},
                path_sha256=_digest(parsed.path.encode()),
            )
            public_path = "/documentintelligence/documentModels/prebuilt-layout:analyze"
            if parsed.path == public_path:
                self.receipt.update(path=public_path, model_id="prebuilt-layout")
            else:
                self.receipt["model_id_sha256"] = _digest(arguments["model_id"].encode())
            raise _RequestCaptured()

    body = None
    original_position = None
    try:
        if client_factory is not None:
            if client_options is not None or analyze_arguments is not None:
                raise ValueError("Do not mix DI factory and dictionary interfaces")
            if not callable(client_factory) or not isinstance(source_bytes, bytes) or not source_bytes:
                raise ValueError("DI factory preflight requires exact source bytes")
            if not isinstance(analyze_kwargs, dict):
                raise ValueError("DI factory preflight requires production analyze kwargs")
            arguments = dict(analyze_kwargs)
        else:
            if source_bytes is not None or analyze_kwargs is not None:
                raise ValueError("DI source bytes and analyze kwargs require the production factory")
            if not isinstance(client_options, dict) or not isinstance(analyze_arguments, dict):
                raise ValueError("DI dictionary preflight requires production client/request options")
            supported_client_options = {
                "endpoint", "credential", "api_version", "retry_total", "retry_connect",
                "retry_read", "retry_status", "connection_timeout", "read_timeout",
            }
            if set(client_options) - supported_client_options:
                raise ValueError("Unsupported DI constructor options")
            arguments = dict(analyze_arguments)
        body = arguments.get("body")
        signature = inspect.signature(DocumentIntelligenceClient.begin_analyze_document)
        supported_request_options = {
            name for name, parameter in signature.parameters.items()
            if parameter.kind != inspect.Parameter.VAR_KEYWORD and name != "self"
        } | {"content_type"}
        if set(arguments) - supported_request_options:
            raise ValueError("Unsupported DI analyze options")
        if not isinstance(arguments.get("model_id"), str) or not arguments["model_id"]:
            raise ValueError("DI model ID must be a nonempty string")
        pages = arguments.get("pages")
        if pages is not None:
            # The Azure serializer coerces arbitrary objects to strings here;
            # reject invalid page bounds before that permissive conversion.
            if not isinstance(pages, str) or not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", pages):
                raise ValueError("DI page bounds must be a page-range string")
            for interval in pages.split(","):
                bounds = [int(part) for part in interval.split("-")]
                if not 1 <= bounds[0] <= bounds[-1]:
                    raise ValueError("DI page bounds must be positive and ascending")
        if isinstance(body, bytes):
            source = body
        elif hasattr(body, "seek") and hasattr(body, "tell") and hasattr(body, "read"):
            original_position = body.tell()
            source = body.read()
            body.seek(original_position)
        else:
            raise ValueError("DI preflight requires actual binary source bytes")
        if not isinstance(source, bytes) or not source:
            raise ValueError("DI preflight requires nonempty binary source bytes")
        if source_bytes is not None and source != source_bytes:
            raise ValueError("DI request builder changed the approved source bytes")
        transport = NoSendTransport(source)
        credential = NoSendCredential()
        if client_factory is not None:
            client = client_factory(credential=credential, transport=transport)
        else:
            options = {**client_options, "credential": credential, "transport": transport}
            client = DocumentIntelligenceClient(**options)
        if not isinstance(client, DocumentIntelligenceClient):
            raise ValueError("The production factory must construct a native DI client")
        # Reject ignored overrides before entering the native transport or
        # executing authentication policies.
        if (
            client._client._pipeline._transport is not transport
            or client._config.authentication_policy._credential is not credential
        ):
            client.close()
            raise ValueError("The production factory ignored no-send overrides")
        with client:
            try:
                client.begin_analyze_document(**arguments)
            except _RequestCaptured:
                pass
        if transport.receipt is None:
            raise ValueError("Native DI request was not captured")
        return transport.receipt
    except Exception as error:
        raise SDKPreflightError("document_intelligence", type(error).__name__) from None
    finally:
        if original_position is not None:
            body.seek(original_position)
