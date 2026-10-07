"""Responses function-calling composition for the bounded quality tool pass.

Only ``ResponsesToolModel.request`` makes a request. The loop owns admission and
the single usage callback; this composition does NOT call the wrapped adapter's
usage_callback. It does preserve last_usage/call_records. No retries, credentials,
clients, or resources are created here.

The optional parent-approved cache_prefix is inserted before dynamic input with
an explicit 30-minute breakpoint. SDK 1.91 receives these fields in extra_body,
including the complete overridden input so the breakpoint survives serialization.
Continuation items remain after that prefix, in their original order.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from openai import Omit
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel

from backend.core.quality_model import (
    QualityModelConfigurationError, QualityModelResponseError, ResponsesCompletion,
)


def field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def strict_json(text: str) -> Any:
    """Reject duplicate keys and non-JSON numeric constants, not just syntax errors."""
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field: " + key)
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("Invalid JSON constant: " + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


class ResponsesToolModel:
    """Wrap an existing ResponsesCompletion without changing its source.

    ``build_request`` is side-effect free and exposes the complete request to the
    caller's maximum-cost estimator. That estimator must bound input AND maximum
    output (including reasoning), including continued/encrypted reasoning items.
    It must not assume a prompt-cache hit. A pricing/token bound that cannot be
    established must fail closed before ``request``.
    """

    def __init__(self, completion: ResponsesCompletion):
        self.completion = completion
        self.last_usage: dict[str, Any] = {}

    def build_request(
        self, instructions: str, inputs: list[dict[str, Any]], tools: list[dict[str, Any]],
        conclusion: type[BaseModel], *, prompt_cache_key: str | None = None,
        cache_prefix: str | None = None,
    ) -> dict[str, Any]:
        extra_body: dict[str, Any] = {}
        if prompt_cache_key is not None:
            if not isinstance(prompt_cache_key, str) or not prompt_cache_key.strip():
                raise QualityModelConfigurationError("prompt_cache_key must be a nonempty string")
            extra_body["prompt_cache_key"] = prompt_cache_key.strip()
        if cache_prefix is not None:
            if not isinstance(cache_prefix, str) or not cache_prefix.strip() or not prompt_cache_key:
                raise QualityModelConfigurationError("A nonempty cache_prefix requires prompt_cache_key")
            cached_input = deepcopy(inputs)
            if not cached_input or cached_input[0].get("role") != "user":
                raise QualityModelConfigurationError("Explicit cache prefix requires initial user input")
            content = cached_input[0].get("content")
            if isinstance(content, str):
                content = [{"type": "input_text", "text": content}]
            if not isinstance(content, list):
                raise QualityModelConfigurationError("Initial user content must be text or a content list")
            cached_input[0]["content"] = [
                {"type": "input_text", "text": cache_prefix, "prompt_cache_breakpoint": {"mode": "explicit"}},
                *content,
            ]
            extra_body.update(
                prompt_cache_options={"mode": "explicit", "ttl": "30m"},
                input=cached_input,
            )
        return {
            "model": self.completion.deployment,
            "instructions": instructions,
            "input": deepcopy(inputs),
            "tools": deepcopy(tools),
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": self.completion.effort},
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": self.completion.max_output_tokens,
            "store": False,
            "text": {"format": {
                "type": "json_schema", "name": conclusion.__name__,
                "schema": to_strict_json_schema(conclusion), "strict": True,
            }},
            # extra_body also works with the repository's older locked SDK.
            **({"extra_body": extra_body} if extra_body else {}),
        }

    def request(self, request: dict[str, Any]) -> Any:
        completion = self.completion
        self.last_usage = {}
        completion.last_usage = {}
        client = completion.client
        # Do not silently inherit an SDK client's default retry policy.
        if getattr(client, "max_retries", None) != 0:
            raise QualityModelConfigurationError("Tool-loop Responses client must have max_retries=0")
        response, failure, status = None, None, "request_failed"
        try:
            response = client.responses.create(**request, extra_query={"api-version": Omit()})
            status = "response_invalid"
            if field(response, "status") != "completed":
                raise QualityModelResponseError(
                    f"Tool-loop Responses request did not complete: {field(response, 'status')!r}"
                )
            if not isinstance(field(response, "output"), list) or not field(response, "output"):
                raise QualityModelResponseError("Tool-loop Responses returned no output items")
            status = "completed"
            return response
        except Exception as error:
            failure = error
            raise
        finally:
            self.last_usage = completion._usage_record(
                response, "QualityToolLoop", field(request.get("reasoning"), "effort", completion.effort), status, failure,
            )
            extra_body = request.get("extra_body", {})
            self.last_usage.update(
                prompt_cache_key=extra_body.get("prompt_cache_key"),
                explicit_cache_prefix="prompt_cache_options" in extra_body,
            )
            completion.last_usage = deepcopy(self.last_usage)
            completion.call_records.append(deepcopy(self.last_usage))

    @staticmethod
    def output_items(response: Any) -> list[dict[str, Any]]:
        items = []
        for item in field(response, "output", []):
            value = item.model_dump(mode="json", exclude_none=True) if isinstance(item, BaseModel) else deepcopy(item)
            if not isinstance(value, dict) or value.get("type") not in {"reasoning", "function_call", "message"}:
                raise QualityModelResponseError("Unsupported Responses output item; no tool was executed")
            items.append(value)
        return items


def parse_turn(items: list[dict[str, Any]], schema: type[BaseModel]) -> tuple[list[dict[str, Any]], Any]:
    """Text is only terminal structured output; never interpreted as a command."""
    calls, texts = [], []
    for item in items:
        kind = item["type"]
        if kind == "function_call":
            if (not isinstance(item.get("call_id"), str) or not item["call_id"]
                    or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("arguments"), str)
                    or item.get("status", "completed") != "completed"):
                raise QualityModelResponseError("Malformed Responses function_call")
            calls.append(item)
        elif kind == "message":
            if item.get("status") != "completed" or item.get("role") != "assistant":
                raise QualityModelResponseError("Incomplete or invalid Responses message")
            for part in item.get("content", []):
                if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise QualityModelResponseError("Responses refusal or unsupported output content")
                texts.append(part["text"])
    if calls:
        if texts:
            raise QualityModelResponseError("Mixed tool calls and terminal output are not accepted")
        return calls, None
    if not texts:
        raise QualityModelResponseError("Responses returned neither tool calls nor terminal structured output")
    try:
        return [], schema.model_validate(strict_json("".join(texts)), strict=True)
    except ValueError as error:
        raise QualityModelResponseError("Invalid tool-loop terminal schema or JSON") from error
