"""Managed-identity Azure Responses adapter with per-call usage reporting."""

from collections.abc import Callable, Mapping
from copy import deepcopy
import math
import os
from typing import Any, TypeVar, cast

from azure.identity import ManagedIdentityCredential, get_bearer_token_provider
from openai import AzureOpenAI, Omit, OpenAIError
from openai.lib._pydantic import to_strict_json_schema
from openai.types.responses import ResponseInputParam
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, ValidationError

from backend.core.config import settings


T = TypeVar("T", bound=BaseModel)
COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_OUTPUT_TOKENS = 16_000


class QualityModelError(RuntimeError):
    """A Responses request, structured response, or usage callback failed."""


class QualityModelConfigurationError(QualityModelError, ValueError):
    """The adapter's local configuration is invalid."""


class QualityModelResponseError(QualityModelError):
    """The provider refused, truncated, failed, or returned invalid output."""


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _nonempty(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualityModelConfigurationError(f"{name} must be a nonempty string")
    return value.strip()


def _price(name: str) -> float | None:
    value = os.getenv(name)
    if value is None:
        return None
    try:
        price = float(value)
    except ValueError as exc:
        raise QualityModelConfigurationError(f"{name} must be a finite nonnegative number") from exc
    if not math.isfinite(price) or price < 0:
        raise QualityModelConfigurationError(f"{name} must be a finite nonnegative number")
    return price


def _tokens(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class ResponsesCompletion:
    """One Responses request per completion; replace ``client`` for offline tests.

    ``usage_callback`` receives a JSON-serializable record even when response
    validation fails. Unreported usage and unconfigured cost are ``None``, not
    fabricated zeroes. ``call_purpose`` is the requested Pydantic schema's name.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        deployment: str | None = None,
        token_provider: Callable[[], str] | None = None,
        usage_callback: Callable[[dict[str, Any]], None] | None = None,
        effort: str | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self.endpoint = _nonempty(
            endpoint if endpoint is not None else (
                os.getenv("LLM_ENDPOINT") or os.getenv("AI_FOUNDRY_ENDPOINT")
                or settings.LLM_ENDPOINT or settings.AI_FOUNDRY_ENDPOINT
            ),
            "LLM_ENDPOINT (or AI_FOUNDRY_ENDPOINT)",
        )
        self.deployment = _nonempty(
            deployment if deployment is not None else (
                os.getenv("QUALITY_MODEL_DEPLOYMENT") or settings.LLM_DEPLOYMENT
            ),
            "QUALITY_MODEL_DEPLOYMENT (or LLM_DEPLOYMENT)",
        )
        self.effort = _nonempty(
            effort if effort is not None else os.getenv("QUALITY_MODEL_EFFORT", DEFAULT_EFFORT),
            "QUALITY_MODEL_EFFORT",
        )
        limit: int | str = (
            max_output_tokens if max_output_tokens is not None else
            os.getenv("QUALITY_MODEL_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))
        )
        if isinstance(limit, bool) or not isinstance(limit, (int, str)):
            raise QualityModelConfigurationError(
                "QUALITY_MODEL_MAX_OUTPUT_TOKENS must be a positive integer"
            )
        try:
            self.max_output_tokens = int(limit)
        except (ValueError, TypeError) as exc:
            raise QualityModelConfigurationError(
                "QUALITY_MODEL_MAX_OUTPUT_TOKENS must be a positive integer"
            ) from exc
        if self.max_output_tokens <= 0:
            raise QualityModelConfigurationError(
                "QUALITY_MODEL_MAX_OUTPUT_TOKENS must be a positive integer"
            )
        self.pricing_usd_per_million = {
            "input": _price("QUALITY_MODEL_INPUT_USD_PER_MILLION"),
            "cached_input": _price("QUALITY_MODEL_CACHED_INPUT_USD_PER_MILLION"),
            "cache_write": _price("QUALITY_MODEL_CACHE_WRITE_USD_PER_MILLION"),
            "output": _price("QUALITY_MODEL_OUTPUT_USD_PER_MILLION"),
        }
        self.pricing_basis = os.getenv(
            "QUALITY_MODEL_PRICE_BASIS",
            "OpenAI public pricing estimate; not final Azure billing",
        )
        self.usage_callback = usage_callback
        self.last_usage: dict[str, Any] = {}
        self.call_records: list[dict[str, Any]] = []

        # AzureOpenAI otherwise prioritizes this ambient static token over a provider.
        if os.getenv("AZURE_OPENAI_AD_TOKEN"):
            raise QualityModelConfigurationError(
                "Unset AZURE_OPENAI_AD_TOKEN; ResponsesCompletion requires its managed-identity "
                "or explicitly supplied token provider"
            )
        if token_provider is None:
            token_provider = get_bearer_token_provider(
                ManagedIdentityCredential(client_id=os.getenv("AZURE_CLIENT_ID") or None),
                COGNITIVE_SERVICES_SCOPE,
            )
        base_url = self.endpoint.rstrip("/")
        if not base_url.endswith("/openai/v1"):
            base_url += "/openai/v1"
        self.client = AzureOpenAI(
            base_url=base_url,
            azure_ad_token_provider=token_provider,
            api_version="v1",
            max_retries=0,
        )

    def complete_structured(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        images: list[str] | None = None,
        reasoning_effort: str | None = None,
    ) -> T:
        self.last_usage = {}
        effort = _nonempty(
            reasoning_effort if reasoning_effort is not None else self.effort,
            "reasoning_effort",
        )
        content: list[dict[str, str]] = [{"type": "input_text", "text": user}]
        for image in images or []:
            if not isinstance(image, str) or not image.startswith("data:image/") or ";base64," not in image:
                raise QualityModelConfigurationError("images must contain base64 image data URLs")
            content.append({"type": "input_image", "image_url": image, "detail": "auto"})
        text_format = {
            "type": "json_schema",
            "name": schema.__name__,
            "schema": to_strict_json_schema(schema),
            "strict": True,
        }
        response: Any = None
        failure: Exception | None = None
        status = "request_failed"
        try:
            response = self.client.responses.create(
                model=self.deployment,
                instructions=system,
                input=cast(ResponseInputParam, [{"role": "user", "content": content}]),
                text={"format": text_format},
                reasoning=cast(Reasoning, {"effort": effort}),
                max_output_tokens=self.max_output_tokens,
                store=False,
                # SDK 1.91 requires api_version at construction; the v1 API does not.
                extra_query={"api-version": Omit()},
            )
            status = "response_invalid"
            result = self._parse_response(response, schema)
            status = "completed"
            return result
        except OpenAIError as exc:
            failure = exc
            raise QualityModelError(
                f"Azure Responses request failed for deployment {self.deployment!r}: {exc}"
            ) from exc
        except Exception as exc:
            failure = exc
            raise
        finally:
            record = self._usage_record(response, schema.__name__, effort, status, failure)
            self.last_usage = deepcopy(record)
            self.call_records.append(deepcopy(record))
            if self.usage_callback is not None:
                try:
                    self.usage_callback(deepcopy(record))
                except Exception as exc:
                    if failure is None:
                        raise QualityModelError(
                            "Usage callback failed; provider usage remains in last_usage and call_records"
                        ) from exc
                    failure.add_note(
                        f"Usage callback also failed ({type(exc).__name__}); provider usage was retained"
                    )

    @staticmethod
    def _parse_response(response: Any, schema: type[T]) -> T:
        status = _field(response, "status")
        if status != "completed":
            reason = _field(_field(response, "incomplete_details"), "reason")
            error_code = _field(_field(response, "error"), "code")
            raise QualityModelResponseError(
                f"Azure Responses response was not completed (status={status!r}, "
                f"reason={reason or error_code!r})"
            )
        text: list[str] = []
        for item in _field(response, "output", []) or []:
            if _field(item, "type") != "message":
                continue
            if _field(item, "status", "completed") != "completed":
                raise QualityModelResponseError("Azure Responses returned an incomplete output message")
            for part in _field(item, "content", []) or []:
                if _field(part, "type") == "refusal":
                    raise QualityModelResponseError("Azure Responses refused the structured-output request")
                if _field(part, "type") == "output_text":
                    text.append(_field(part, "text", ""))
        if not text or not "".join(text).strip():
            raise QualityModelResponseError("Azure Responses returned no structured output text")
        try:
            return schema.model_validate_json("".join(text))
        except ValidationError as exc:
            raise QualityModelResponseError(
                f"Azure Responses output did not validate as {schema.__name__} "
                f"({exc.error_count()} validation errors)"
            ) from exc

    def _usage_record(
        self,
        response: Any,
        purpose: str,
        effort: str,
        status: str,
        failure: Exception | None,
    ) -> dict[str, Any]:
        usage = _field(response, "usage")
        input_tokens = _tokens(_field(usage, "input_tokens"))
        input_details = _field(usage, "input_tokens_details")
        cached_input_tokens = _tokens(_field(input_details, "cached_tokens"))
        cache_write_tokens = _tokens(_field(input_details, "cache_write_tokens"))
        output_tokens = _tokens(_field(usage, "output_tokens"))
        reasoning_tokens = _tokens(_field(_field(usage, "output_tokens_details"), "reasoning_tokens"))
        input_price = self.pricing_usd_per_million["input"]
        cached_price = self.pricing_usd_per_million["cached_input"]
        write_price = self.pricing_usd_per_million["cache_write"]
        output_price = self.pricing_usd_per_million["output"]
        cost = None
        reported_writes = cache_write_tokens or 0
        if (
            input_tokens is not None and cached_input_tokens is not None
            and output_tokens is not None and cached_input_tokens + reported_writes <= input_tokens
            and input_price is not None and cached_price is not None and output_price is not None
            and (not reported_writes or write_price is not None)
        ):
            # Reasoning tokens are already included in output_tokens.
            cost = (
                (input_tokens - cached_input_tokens - reported_writes) * input_price
                + cached_input_tokens * cached_price
                + reported_writes * (write_price or 0)
                + output_tokens * output_price
            ) / 1_000_000
            if not math.isfinite(cost):
                cost = None
        return {
            "model": _field(response, "model"),
            "deployment": self.deployment,
            "response_id": _field(response, "id"),
            "call_purpose": purpose,
            "reasoning_effort": effort,
            "status": status,
            "response_status": _field(response, "status"),
            "error_type": type(failure).__name__ if failure is not None else None,
            "usage_reported": usage is not None,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
            "output_tokens": output_tokens,
            "total_tokens": _tokens(_field(usage, "total_tokens")),
            "estimated_cost_usd": cost,
            "pricing_usd_per_million": dict(self.pricing_usd_per_million),
            "pricing_basis": self.pricing_basis,
        }
