import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from backend.core.llm import LLMClient, LLMSchemaValidationError
from backend.models.enrichment import Candidate, ExtractionResponse, ValidationIssue
from backend.response_validation import (
    ResponseValidationError, map_citations, parsed_content, response_diagnostic, schema_issues,
)


REFERENCES = {"E1": "source:version:paragraph=0", "E2": "source:version:table=0&row=1&column=2"}


@pytest.mark.parametrize("ids,expected", [
    (["E1"], [REFERENCES["E1"]]),
    ([" \tE1\n"], [REFERENCES["E1"]]),
    (["e1"], [REFERENCES["E1"]]),
    ([REFERENCES["E1"]], [REFERENCES["E1"]]),
    ([" " + REFERENCES["E1"] + " "], [REFERENCES["E1"]]),
    (["E2", "e1", "E2", REFERENCES["E1"]], [REFERENCES["E2"], REFERENCES["E1"]]),
])
def test_documented_citation_aliases_are_lossless_without_mutating_input(ids, expected):
    response = ExtractionResponse(candidates=[Candidate(attribute_id="Material", value="brass", evidence_ids=ids)])
    mapped = map_citations(response, REFERENCES)
    assert mapped.candidates[0].evidence_ids == expected
    assert response.candidates[0].evidence_ids == ids


@pytest.mark.parametrize("reference", [
    "E0", "E01", "E3", "E 1", "E1,E2", "0", "group:0", "paragraph=0",
    "source", "source:version:paragraph=99", "", "https://unapproved.invalid/?secret=private",
])
def test_groups_suffixes_joined_and_unknown_references_are_not_citations(reference):
    response = ExtractionResponse(candidates=[
        Candidate(attribute_id="Material", value="brass", evidence_ids=["E1", reference]),
    ])
    with pytest.raises(ResponseValidationError) as caught:
        map_citations(response, REFERENCES)
    assert caught.value.issues[0].field_path == "candidates[0].evidence_ids[1]"
    assert reference not in str(caught.value) or not reference


def test_empty_citations_are_rejected_before_mapping():
    with pytest.raises(ValidationError):
        Candidate(attribute_id="Material", value="brass", evidence_ids=[])


def test_original_aliases_never_override_explicit_row_references():
    response = ExtractionResponse(candidates=[Candidate(
        attribute_id="Material", value="brass", evidence_ids=["E2", "E1"],
    )])
    assert map_citations(response, {"E1": "E2", "E2": "original-second"}).candidates[0].evidence_ids == [
        "original-second", "E2",
    ]


def test_diagnostics_redact_values_quotes_unknown_keys_and_secret_like_references():
    secret = "private-document-quote-or-credential"
    payload = {"candidates": [{
        "attribute_id": secret, "value": secret, "unit": secret, "supporting_quote": secret,
        "qualification": secret, "evidence_ids": [" E999 ", secret, REFERENCES["E1"]],
        secret: secret,
    }], secret: secret}
    raw = json.dumps(payload)
    diagnostic = response_diagnostic(
        payload=payload, references=REFERENCES,
        issues=[ValidationIssue(field_path="candidates[0].evidence_ids[1]", message="Unknown citation reference.")],
        raw_response_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
    assert secret not in diagnostic.model_dump_json()
    assert [entry.value for entry in diagnostic.used_references] == [" E999 ", None, REFERENCES["E1"]]
    assert diagnostic.used_references[1].sha256 == hashlib.sha256(secret.encode()).hexdigest()
    assert diagnostic.valid_references == ["E1", "E2"]
    assert diagnostic.raw_response_sha256 == hashlib.sha256(raw.encode()).hexdigest()
    assert diagnostic.raw_response_hash_basis == "provider_content"
    assert not diagnostic.truncated


def test_schema_errors_keep_safe_field_paths_not_input_or_context():
    secret = "PRIVATE-SCHEMA-CONTENT"
    payload = {"candidates": [{"value": secret, "evidence_ids": secret, secret: secret}]}
    with pytest.raises(ValidationError) as caught:
        ExtractionResponse.model_validate(payload)
    issues = schema_issues(caught.value)
    serialized = json.dumps([issue.model_dump() for issue in issues])
    assert secret not in serialized
    assert any(issue.field_path == "candidates[0].evidence_ids" for issue in issues)
    assert any(issue.field_path == "candidates[0].<redacted-field>" for issue in issues)


def test_diagnostic_limits_are_explicit_and_missing_raw_content_is_not_a_fake_hash():
    payload = {"candidates": [{"value": "private", "evidence_ids": ["E1"]}] * 100}
    diagnostic = response_diagnostic(
        payload=payload, references=REFERENCES,
        issues=[ValidationIssue(field_path="$", message="Invalid response.")],
    )
    assert diagnostic.truncated and len(diagnostic.parsed_response["candidates"]) == 64
    assert diagnostic.raw_response_sha256 is None
    assert diagnostic.raw_response_hash_basis == "unavailable"
    assert parsed_content("not JSON PRIVATE") is None


@pytest.mark.parametrize("valid", [True, False])
def test_client_hashes_exact_provider_content_before_schema_validation(valid):
    content = '{"candidates": []}\n' if valid else '{"candidates": "PRIVATE"}\n'
    client = LLMClient.__new__(LLMClient)
    client.deployment = "synthetic"
    client.sync_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=Mock(return_value=SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )),
    )))
    if valid:
        assert client.complete_structured("test", "test", ExtractionResponse, max_retries=1).candidates == []
    else:
        with pytest.raises(LLMSchemaValidationError):
            client.complete_structured("test", "test", ExtractionResponse, max_retries=1)
    assert client.last_response_sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert client.last_usage == {"input_tokens": 12, "output_tokens": 8}
