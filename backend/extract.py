"""Single-product enrichment from supplied evidence; live inference requires opt-in."""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from backend.core.instructions import product_extraction_system_message
from backend.core.llm import LLMClient, LLMSchemaValidationError
from backend.models.enrichment import (
    AttributeResult, EnrichmentResult, Evidence, ExtractionResponse, Manifest,
    InferenceFailure, LiveBundle, OfflineBundle, OfflineSource, RetrievalOutcome, ReviewDecision,
)
from backend.real_pilot import RealPilotBudgetExceeded


class StructuredCompletion(Protocol):
    def complete_structured(
        self, system: str, user: str, schema: type[ExtractionResponse]
    ) -> ExtractionResponse: ...


class ExecutionConfigurationError(ValueError):
    """A fixed, non-sensitive execution configuration error."""


def sanitized_failure(error: Exception, stage: str) -> InferenceFailure:
    if isinstance(error, RealPilotBudgetExceeded):
        return InferenceFailure(
            stage="client_initialization", exception_class="RealPilotBudgetExceeded",
            parameter=error.dimension, explanation=str(error),
        )
    import httpx
    import openai
    from azure.core.exceptions import ClientAuthenticationError

    known_classes = (
        ClientAuthenticationError, openai.AuthenticationError, openai.PermissionDeniedError,
        openai.RateLimitError, openai.BadRequestError, openai.NotFoundError,
        openai.APITimeoutError, openai.APIConnectionError, openai.APIStatusError,
        httpx.TimeoutException, httpx.RequestError, LLMSchemaValidationError,
        ValidationError, ValueError, TypeError, RuntimeError,
    )
    exception_class = next((kind.__name__ for kind in known_classes if isinstance(error, kind)), "Exception")
    explanation = {
        "client_initialization": "Client initialization failed; sensitive details withheld.",
        "service_request": "Completion request failed; sensitive details withheld.",
        "structured_response_parsing": "Returned content did not satisfy the structured response schema.",
        "evidence_validation": "Candidates failed source citation, attribute, value, or unit validation.",
    }[stage]
    cause = error
    authentication_failure = False
    for _ in range(8):
        if isinstance(cause, (ClientAuthenticationError, openai.AuthenticationError)):
            authentication_failure = True
            break
        cause = cause.__cause__ or cause.__context__
        if cause is None:
            break
    if authentication_failure:
        stage, explanation = "authentication", "Azure authentication failed; no account or permission changes attempted."
    elif isinstance(error, (openai.APIConnectionError, httpx.RequestError)):
        stage, explanation = "network_connection", "Connection or timeout failure prevented a usable service response."
    elif isinstance(error, openai.APIStatusError):
        stage, explanation = "service_request", "The service rejected the request."
    elif isinstance(error, LLMSchemaValidationError):
        stage, explanation = "structured_response_parsing", "Returned content did not satisfy the structured response schema."

    status = error.status_code if isinstance(error, openai.APIStatusError) else None
    code = parameter = None
    if isinstance(error, openai.APIStatusError):
        body = error.body if isinstance(error.body, dict) else {}
        details = body.get("error", body)
        details = details if isinstance(details, dict) else {}
        raw_code = details.get("code")
        raw_parameter = details.get("param")
        codes = {
            "invalid_request_error", "invalid_parameter", "unsupported_parameter", "unsupported_value",
            "invalid_json_schema", "DeploymentNotFound", "OperationNotSupported", "InvalidRequest",
            "BadRequest", "Unauthorized", "Forbidden", "PermissionDenied", "RateLimitExceeded",
            "TooManyRequests", "insufficient_quota", "content_filter", "model_not_found",
        }
        parameters = {
            "response_format", "response_format.json_schema", "response_format.json_schema.schema",
            "messages", "messages[0].role", "model", "temperature", "max_tokens",
            "max_completion_tokens", "api-version", "reasoning_effort", "stream",
        }
        code = raw_code if isinstance(raw_code, str) and raw_code in codes else "unrecognized_not_displayed" if raw_code is not None else None
        parameter = raw_parameter if isinstance(raw_parameter, str) and raw_parameter in parameters else "unrecognized_not_displayed" if raw_parameter is not None else None
        if code in ("invalid_json_schema", "unsupported_parameter", "unsupported_value", "DeploymentNotFound", "insufficient_quota", "content_filter"):
            explanation = {
                "invalid_json_schema": "The service rejected the supplied structured-output schema.",
                "unsupported_parameter": "The service does not support the identified request parameter.",
                "unsupported_value": "The service does not support the value of the identified request parameter.",
                "DeploymentNotFound": "The configured deployment was not found by the service.",
                "insufficient_quota": "The service reported insufficient quota.",
                "content_filter": "The service rejected the request under its content policy.",
            }[code]
    return InferenceFailure(
        stage=stage, exception_class=exception_class, http_status=status,
        service_error_code=code, parameter=parameter, explanation=explanation,
    )


def source_evidence(source: OfflineSource, observed_at: datetime) -> list[Evidence]:
    if source.excerpts is not None:
        return [item.model_copy(deep=True) for item in source.excerpts]
    document = source.document
    if document is None:
        return []
    evidence = []

    def append(text: str, position: str, page: int | None) -> None:
        if not text.strip():
            return
        locator = f"{document.source}#" + (f"page={page}&" if page is not None else "") + position
        evidence.append(Evidence(
            evidence_id=f"{source.source_id}:{document.cache_key}:{position}",
            source_id=source.source_id,
            source_locator=locator,
            source_version=document.cache_key,
            source_tier=source.source_tier,
            content_kind="source_excerpt",
            text=text,
            observed_at=observed_at,
            provider_retrieved_at=source.provider_retrieved_at,
            source_published_at=source.source_published_at,
            attribute_ids=source.attribute_ids,
            qualification=source.qualification,
        ))

    for paragraph_index, paragraph in enumerate(document.paragraphs):
        append(paragraph.text, f"paragraph={paragraph_index}", paragraph.page_number)
    for table_index, table in enumerate(document.tables):
        for row_index, row in enumerate(table.cells):
            for column_index, text in enumerate(row):
                append(text, f"table={table_index}&row={row_index}&column={column_index}", table.page_number)
    return evidence


def validate_response(
    response: ExtractionResponse, manifest: Manifest, evidence: list[Evidence]
) -> None:
    definitions = {attribute.attribute_id: attribute for attribute in manifest.attributes}
    citations = {item.evidence_id: item for item in evidence}
    for candidate in response.candidates:
        if candidate.attribute_id not in definitions or candidate.attribute_id in manifest.existing_values:
            raise ValueError("Candidate must target a known missing attribute")
        definitions[candidate.attribute_id].validate_value(candidate.value, candidate.unit)
        for evidence_id in candidate.evidence_ids:
            if evidence_id not in citations or citations[evidence_id].content_kind != "source_excerpt":
                raise ValueError("Candidate must cite an available source excerpt")
            scope = citations[evidence_id].attribute_ids
            if scope is not None and candidate.attribute_id not in scope:
                raise ValueError("Candidate exceeds the approved product/attribute applicability")
        if candidate.supporting_quote is not None and (
            not candidate.supporting_quote.strip() or not any(
                candidate.supporting_quote in citations[key].text for key in candidate.evidence_ids
            )
        ):
            raise ValueError("Supporting quotation must occur verbatim in cited evidence")
        literal = str(candidate.value)
        if isinstance(candidate.value, float) and candidate.value.is_integer():
            literal = str(int(candidate.value))
        literals = ("true", "yes") if candidate.value is True else ("false", "no") if candidate.value is False else (literal,)
        pattern = rf"(?<!\w)(?:{'|'.join(re.escape(value) for value in literals)})(?!\w)"
        supporting_text = [candidate.supporting_quote] if candidate.supporting_quote else [
            citations[evidence_id].text for evidence_id in candidate.evidence_ids
        ]
        if not any(re.search(pattern, text, re.IGNORECASE) for text in supporting_text):
            raise ValueError("Candidates require literal value support in source excerpts")
        if candidate.unit and not any(
            re.search(rf"(?<!\w){re.escape(candidate.unit)}(?!\w)", text, re.IGNORECASE)
            for text in supporting_text
        ):
            raise ValueError("Candidates require explicit unit support in source excerpts")
        if candidate.supporting_quote and re.search(r"\b(maximum|max)\b", candidate.attribute_id, re.IGNORECASE):
            if re.search(r"\bworking\b", candidate.supporting_quote, re.IGNORECASE) and not re.search(
                r"\b(maximum|max)\b", candidate.supporting_quote, re.IGNORECASE
            ):
                raise ValueError("Working rating does not establish a maximum rating")
        if candidate.supporting_quote and candidate.attribute_id.casefold() in {"material", "primary material", "body material"}:
            if re.search(r"\b(o-ring|seat|stem|seal|ball|pin|washer)s?\b", candidate.supporting_quote, re.IGNORECASE) and not re.search(
                r"\b(body|primary material)\b", candidate.supporting_quote, re.IGNORECASE
            ):
                raise ValueError("A component material does not establish the product/body material")
        if candidate.supporting_quote and type(candidate.value) is bool:
            answer = "(?:true|yes)" if candidate.value else "(?:false|no)"
            if not re.search(
                rf"{re.escape(candidate.attribute_id)}\s*[:=]\s*{answer}(?!\w)",
                candidate.supporting_quote, re.IGNORECASE,
            ):
                raise ValueError("Boolean proposals require an explicit labeled answer, not negation or missing evidence")


class ReplayCompletion:
    def __init__(self, response: ExtractionResponse):
        self.response = response

    def complete_structured(
        self, system: str, user: str, schema: type[ExtractionResponse]
    ) -> ExtractionResponse:
        return schema.model_validate(self.response.model_dump())


def run_offline(
    bundle: OfflineBundle,
    *,
    completion: StructuredCompletion | None = None,
    observed_at: datetime | None = None,
    web_provider: str | None = None,
) -> EnrichmentResult:
    return run_enrichment(
        bundle, completion=completion, observed_at=observed_at, web_provider=web_provider,
    )


def run_enrichment(
    bundle: OfflineBundle | LiveBundle,
    *,
    execution_mode: Literal["offline_replay", "live_inference"] = "offline_replay",
    completion: StructuredCompletion | None = None,
    observed_at: datetime | None = None,
    web_provider: str | None = None,
    no_retries: bool = False,
    qualified: bool = False,
) -> EnrichmentResult:
    if execution_mode not in ("offline_replay", "live_inference"):
        raise ExecutionConfigurationError("Unsupported execution_mode")
    live = execution_mode == "live_inference"
    if no_retries and (not live or completion is not None):
        raise ExecutionConfigurationError("Disabling retries requires live mode with the configured LLMClient")
    expected_bundle = LiveBundle if live else OfflineBundle
    if not isinstance(bundle, expected_bundle) or bundle.execution_mode != execution_mode:
        raise ExecutionConfigurationError("Input execution_mode must match the explicitly selected execution mode")
    bundle = expected_bundle.model_validate(bundle.model_dump())
    if not live and completion is not None:
        raise ExecutionConfigurationError("Replay does not accept a completion client; select live_inference explicitly")
    if live and isinstance(completion, ReplayCompletion):
        raise ExecutionConfigurationError("Live inference does not accept ReplayCompletion")
    if web_provider is not None:
        from backend.core.websearch import ProviderUnavailableError

        raise ProviderUnavailableError(
            "Web retrieval is disabled for supplied-document enrichment in both execution modes."
        )
    observed_at = observed_at or datetime.now(timezone.utc)
    manifest = bundle.manifest
    sources = {source.source_id: source for source in bundle.sources}
    evidence = []
    retrieval = []
    for source_id in manifest.source_ids:
        source = sources.get(source_id)
        error = "fixture_missing" if source is None else source.error_code
        if source is not None and source.product != manifest.product:
            error = "product_mismatch"
        chunks = source_evidence(source, observed_at) if source is not None and not error else []
        evidence.extend(chunks)
        retrieval.append(RetrievalOutcome(
            source_tier=source.source_tier if source is not None else "internal_pdf", source_id=source_id,
            status="failed" if error else "success" if chunks else "no_evidence",
            error_code=error,
        ))
    if not manifest.source_ids:
        retrieval.append(RetrievalOutcome(source_tier="internal_pdf", status="not_attempted"))
    for tier in ("vendor_table", "manufacturer_web", "approved_web"):
        if not any(outcome.source_tier == tier for outcome in retrieval):
            retrieval.append(RetrievalOutcome(source_tier=tier, status="not_attempted"))

    response = ExtractionResponse(candidates=[])
    extraction_error = None
    failure = None
    invalid_attributes = set()
    model_call_status = "not_attempted"
    missing_attributes = any(attribute.attribute_id not in manifest.existing_values for attribute in manifest.attributes)
    skip_reason = "no_missing_attributes" if not missing_attributes else "no_eligible_evidence" if not evidence else None
    if live and skip_reason:
        model_call_status = "skipped"
    if skip_reason is None:
        prompt = json.dumps({
            "product": manifest.product.model_dump(),
            "attributes": [attribute.model_dump(exclude={"examples"}) for attribute in manifest.attributes
                           if attribute.attribute_id not in manifest.existing_values],
            "evidence": [item.model_dump(mode="json") for item in evidence],
        })
        if live and completion is None:
            from backend.core.config import settings

            endpoint = settings.LLM_ENDPOINT or settings.AI_FOUNDRY_ENDPOINT
            if not endpoint or not endpoint.strip() or not settings.LLM_DEPLOYMENT or not settings.LLM_DEPLOYMENT.strip():
                raise ExecutionConfigurationError("Live inference requires LLM_ENDPOINT (or AI_FOUNDRY_ENDPOINT) and LLM_DEPLOYMENT")
        stage = "client_initialization"
        try:
            generator = (completion if completion is not None else LLMClient()) if live else ReplayCompletion(bundle.generated_response)
            options = {}
            if no_retries:
                generator.sync_client = generator.sync_client.with_options(max_retries=0)
                options = {"max_retries": 1, "retry_delay": 0}
            stage = "service_request" if live else "structured_response_parsing"
            if live:
                model_call_status = "failed"
            response = generator.complete_structured(
                product_extraction_system_message, prompt, ExtractionResponse,
                **options,
            )
            if live:
                model_call_status = "succeeded"
            stage = "structured_response_parsing"
            response = ExtractionResponse.model_validate(response.model_dump())
            stage = "evidence_validation"
            if qualified:
                accepted = []
                for candidate in response.candidates:
                    try:
                        if not candidate.supporting_quote or not candidate.qualification or not candidate.qualification.strip():
                            raise ValueError("Real proposals require quotations and applicability qualifications")
                        validate_response(ExtractionResponse(candidates=[candidate]), manifest, evidence)
                        accepted.append(candidate)
                    except ValueError as error:
                        invalid_attributes.add(candidate.attribute_id)
                        extraction_error = "invalid_response"
                        failure = sanitized_failure(error, stage)
                response = ExtractionResponse(candidates=accepted)
            else:
                validate_response(response, manifest, evidence)
        except RealPilotBudgetExceeded as error:
            model_call_status = "not_attempted"
            extraction_error = "model_failed"
            failure = sanitized_failure(error, "client_initialization")
        except ExecutionConfigurationError:
            raise
        except Exception as error:
            extraction_error = "invalid_response" if stage != "client_initialization" and isinstance(error, (ValidationError, ValueError, LLMSchemaValidationError)) else "model_failed"
            failure = sanitized_failure(error, stage)
            response = ExtractionResponse(candidates=[])

    results = []
    retrieval_failed = any(outcome.status == "failed" for outcome in retrieval)
    for attribute in manifest.attributes:
        candidates = [candidate for candidate in response.candidates if candidate.attribute_id == attribute.attribute_id]
        values = {(type(candidate.value).__name__, candidate.value, candidate.unit) for candidate in candidates}
        if attribute.attribute_id in manifest.existing_values:
            status = "existing"
        elif extraction_error and not invalid_attributes:
            status = "extraction_failed"
        elif candidates:
            status = "conflict" if len(values) > 1 else "proposed"
        elif attribute.attribute_id in invalid_attributes:
            status = "extraction_failed"
        else:
            status = "retrieval_failed" if retrieval_failed else "missing_evidence"
        results.append(AttributeResult(attribute_id=attribute.attribute_id, status=status, candidates=candidates))
    return EnrichmentResult(
        execution_mode=execution_mode,
        candidate_source="llm" if live else "supplied_response",
        model_call_status=model_call_status, skip_reason=skip_reason,
        manifest=manifest, observed_at=observed_at, retrieval=retrieval,
        evidence=evidence, attributes=results, extraction_error=extraction_error,
        failure=failure,
    )


def apply_reviews(result: EnrichmentResult, reviews: list[ReviewDecision]) -> EnrichmentResult:
    reviewed = result.model_copy(deep=True)
    attributes = {attribute.attribute_id: attribute for attribute in reviewed.attributes}
    definitions = {attribute.attribute_id: attribute for attribute in result.manifest.attributes}
    for review in reviews:
        attribute = attributes.get(review.attribute_id)
        if attribute is None or attribute.status == "existing" or attribute.review is not None:
            raise ValueError("Review must target an unreviewed missing attribute")
        if not review.reviewer.strip() or not review.reason.strip():
            raise ValueError("Reviewer and reason must not be blank")
        if review.decision == "approve":
            if review.candidate_index is None or review.candidate_index >= len(attribute.candidates):
                raise ValueError("Approval must select an existing candidate")
        elif review.candidate_index is not None:
            raise ValueError("Only approval selects a candidate")
        if review.decision == "correct":
            if review.corrected_value is None:
                raise ValueError("Correction requires a value")
            definitions[review.attribute_id].validate_value(review.corrected_value, review.corrected_unit)
        elif review.corrected_value is not None or review.corrected_unit is not None:
            raise ValueError("Only correction supplies a replacement value")
        attribute.review = review
    return reviewed


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich one product from supplied parsed-document evidence; replay is the default.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--web-provider")
    parser.add_argument("--live", action="store_true", help="Explicitly authorize LLM inference; requires live_inference input")
    parser.add_argument("--no-retries", action="store_true", help="Live diagnostic only: one completion attempt and zero SDK retries")
    args = parser.parse_args()
    try:
        mode = "live_inference" if args.live else "offline_replay"
        schema = LiveBundle if args.live else OfflineBundle
        bundle = schema.model_validate_json(args.input.read_text())
        if args.output.exists():
            raise ExecutionConfigurationError("Output path must not already exist")
        result = run_enrichment(bundle, execution_mode=mode, web_provider=args.web_provider, no_retries=args.no_retries)
        if args.reviews:
            reviews = [ReviewDecision.model_validate(value) for value in json.loads(args.reviews.read_text())]
            result = apply_reviews(result, reviews)
        with args.output.open("x") as output:
            output.write(result.model_dump_json(indent=2, exclude_none=True) + "\n")
    except Exception as error:
        from backend.core.websearch import ProviderUnavailableError

        if isinstance(error, ValidationError):
            message = "Input/review schema mismatch: check execution_mode, generated_response, and required fields for the selected mode"
        else:
            message = str(error) if isinstance(error, (ProviderUnavailableError, ExecutionConfigurationError)) else type(error).__name__
        parser.exit(2, f"Extraction failed: {message}\n")
    return 1 if result.extraction_error or any(outcome.status == "failed" for outcome in result.retrieval) else 0


if __name__ == "__main__":
    raise SystemExit(main())
