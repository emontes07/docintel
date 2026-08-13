"""Generic Azure AI Foundry chat client with schema-constrained structured output.

Wraps the Foundry deployment named by ``settings.LLM_DEPLOYMENT`` (gpt-4o) and
returns validated Pydantic instances rather than raw JSON.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI, AzureOpenAI
from pydantic import BaseModel, ValidationError

from backend.core.config import settings

logger = logging.getLogger(__name__)

# Structured outputs (response_format=json_schema) require this api-version or newer.
LLM_API_VERSION = "2025-01-01-preview"
COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 2

ModelT = TypeVar("ModelT", bound=BaseModel)

UserContent = Union[str, List[Dict[str, Any]]]


class LLMError(Exception):
    """Base class for all LLM client failures."""


class LLMSchemaValidationError(LLMError):
    """The model's output could not be validated against the requested schema."""

    def __init__(self, schema: Type[BaseModel], raw_content: Optional[str], errors: Any):
        self.schema = schema
        self.raw_content = raw_content
        self.errors = errors
        super().__init__(
            f"LLM response did not match schema {schema.__name__}: {errors}"
        )


def _strictify(node: Any) -> Any:
    """Recursively make a JSON schema satisfy Azure OpenAI strict structured outputs.

    Strict mode requires every object to disallow extra properties and to list
    all of its properties as required.
    """
    if isinstance(node, dict):
        node = {key: _strictify(value) for key, value in node.items()}
        if node.get("type") == "object":
            node["additionalProperties"] = False
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
        return node
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    return node


def _response_format(schema: Type[BaseModel], strict: bool) -> Dict[str, Any]:
    json_schema = schema.model_json_schema()
    if strict:
        json_schema = _strictify(json_schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "schema": json_schema,
            "strict": strict,
        },
    }


def _build_messages(system: str, user: UserContent) -> List[Dict[str, Any]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse(
    schema: Type[ModelT], content: Optional[str]
) -> Union[ModelT, ValidationError, ValueError]:
    """Return a validated instance, or the exception describing why it failed."""
    if content is None:
        return ValueError("model returned empty content")
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, ValueError) as exc:
        return exc
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        return exc


class LLMClient:
    """Reusable chat client for the Foundry LLM deployment.

    Callers pass a Pydantic model; the deployment is asked for JSON-schema
    constrained output and the validated instance is returned.
    """

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        deployment: Optional[str] = None,
        token_provider: Optional[Any] = None,
        api_version: str = LLM_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.endpoint = endpoint or settings.AI_FOUNDRY_ENDPOINT
        self.deployment = deployment or settings.LLM_DEPLOYMENT
        self.timeout = timeout

        if token_provider is None:
            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(), COGNITIVE_SERVICES_SCOPE
            )

        self.sync_client = AzureOpenAI(
            azure_endpoint=self.endpoint,
            azure_ad_token_provider=token_provider,
            api_version=api_version,
            timeout=timeout,
        )
        self.async_client = AsyncAzureOpenAI(
            azure_endpoint=self.endpoint,
            azure_ad_token_provider=token_provider,
            api_version=api_version,
            timeout=timeout,
        )
        logger.info(
            "Initialized LLM client with managed identity (deployment: %s)",
            self.deployment,
        )

    def complete_structured(
        self,
        system: str,
        user: UserContent,
        schema: Type[ModelT],
        *,
        strict: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: int = DEFAULT_RETRY_DELAY,
        **kwargs: Any,
    ) -> ModelT:
        """Call the deployment and return a validated ``schema`` instance."""
        messages = _build_messages(system, user)
        response_format = _response_format(schema, strict)
        last_content: Optional[str] = None
        last_error: Any = None

        for attempt in range(max_retries):
            if attempt:
                logger.info(
                    "Retrying LLMClient.complete_structured() - attempt %s", attempt
                )
                time.sleep(retry_delay)

            response = self.sync_client.chat.completions.create(
                model=self.deployment,
                messages=messages,
                response_format=response_format,
                **kwargs,
            )
            last_content = response.choices[0].message.content
            result = _parse(schema, last_content)
            if isinstance(result, BaseModel):
                return result

            last_error = result
            logger.warning(
                "LLM response failed %s validation - retrying ...", schema.__name__
            )

        raise LLMSchemaValidationError(schema, last_content, last_error)

    async def acomplete_structured(
        self,
        system: str,
        user: UserContent,
        schema: Type[ModelT],
        *,
        strict: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_delay: int = DEFAULT_RETRY_DELAY,
        **kwargs: Any,
    ) -> ModelT:
        """Async counterpart of :meth:`complete_structured`."""
        messages = _build_messages(system, user)
        response_format = _response_format(schema, strict)
        last_content: Optional[str] = None
        last_error: Any = None

        for attempt in range(max_retries):
            if attempt:
                logger.info(
                    "Retrying LLMClient.acomplete_structured() - attempt %s", attempt
                )
                await asyncio.sleep(retry_delay)

            response = await self.async_client.chat.completions.create(
                model=self.deployment,
                messages=messages,
                response_format=response_format,
                **kwargs,
            )
            last_content = response.choices[0].message.content
            result = _parse(schema, last_content)
            if isinstance(result, BaseModel):
                return result

            last_error = result
            logger.warning(
                "LLM response failed %s validation - retrying ...", schema.__name__
            )

        raise LLMSchemaValidationError(schema, last_content, last_error)
