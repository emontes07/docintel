"""Integration tests for the structured-output LLM client.

These tests call the real Azure OpenAI LLM API.
Run with:  uv run pytest tests/integration/test_analysis.py -v -s
"""

import pytest
from pydantic import BaseModel, Field

from backend.core.llm import LLMSchemaValidationError

pytestmark = pytest.mark.integration


class Summary(BaseModel):
    """Small schema used to exercise structured output."""

    title: str = Field(description="Short title for the text")
    tags: list[str] = Field(description="Between 2 and 5 retrieval tags")
    word_count: int = Field(description="Approximate word count of the input")


SAMPLE_TEXT = (
    "Invoice 4471 was issued on 3 March 2025 by Contoso Ltd to Fabrikam Inc "
    "for consulting services totalling 12,500 EUR, payable within 30 days."
)

SYSTEM = "Extract structured information from the user's text."


class TestStructuredOutput:
    """Validated Pydantic instances come back from the deployment."""

    def test_sync_structured_completion(self, structured_llm):
        result = structured_llm.complete_structured(SYSTEM, SAMPLE_TEXT, Summary)

        assert isinstance(result, Summary)
        assert result.title
        assert isinstance(result.tags, list)
        assert len(result.tags) > 0
        assert isinstance(result.word_count, int)

    @pytest.mark.asyncio
    async def test_async_structured_completion(self, structured_llm):
        result = await structured_llm.acomplete_structured(
            SYSTEM, SAMPLE_TEXT, Summary
        )

        assert isinstance(result, Summary)
        assert result.title
        assert isinstance(result.tags, list)


class TestSchemaValidationFailure:
    """Output that does not satisfy the schema raises the typed exception."""

    @staticmethod
    def _stub_response(content: str):
        class _Message:
            def __init__(self, c):
                self.content = c

        class _Choice:
            def __init__(self, c):
                self.message = _Message(c)

        class _Response:
            def __init__(self, c):
                self.choices = [_Choice(c)]

        return _Response(content)

    def test_invalid_payload_raises_typed_error(self, structured_llm, monkeypatch):
        """A well-formed JSON body with the wrong types fails Pydantic validation."""
        bad_payload = '{"title": "Invoice", "tags": "not-a-list", "word_count": "many"}'

        def fake_create(*args, **kwargs):
            return self._stub_response(bad_payload)

        monkeypatch.setattr(
            structured_llm.sync_client.chat.completions, "create", fake_create
        )

        with pytest.raises(LLMSchemaValidationError) as exc_info:
            structured_llm.complete_structured(
                SYSTEM, SAMPLE_TEXT, Summary, max_retries=1
            )

        error = exc_info.value
        assert error.schema is Summary
        assert error.raw_content == bad_payload
        assert "Summary" in str(error)

    def test_malformed_json_raises_typed_error(self, structured_llm, monkeypatch):
        """Content that is not JSON at all surfaces as the same typed error."""

        def fake_create(*args, **kwargs):
            return self._stub_response("this is not json")

        monkeypatch.setattr(
            structured_llm.sync_client.chat.completions, "create", fake_create
        )

        with pytest.raises(LLMSchemaValidationError):
            structured_llm.complete_structured(
                SYSTEM, SAMPLE_TEXT, Summary, max_retries=1
            )
