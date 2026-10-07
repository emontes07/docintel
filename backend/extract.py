"""Single-product enrichment from supplied evidence; live inference requires opt-in."""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote

from pydantic import ValidationError

from backend.core.instructions import product_extraction_system_message
from backend.core.llm import LLMClient, LLMSchemaValidationError
from backend.evidence_verification import Fragment, match_quote, match_text, normalized, value_windows, windows
from backend.models.enrichment import (
    AttributeResult, Candidate, EnrichmentResult, Evidence, EvidenceMatch, ExtractionResponse, Manifest,
    EvidenceVerification, InferenceFailure, LiveBundle, OfflineBundle, OfflineSource, RetrievalOutcome, ReviewDecision,
    LEAD_FREE_DESCRIPTION_RULE, is_lead_free_attribute,
)
from backend.real_pilot import RealPilotBudgetExceeded
from backend.pdf_presentation import pdf_items
from backend.response_validation import (
    ResponseValidationError, invalid, parsed_content, response_diagnostic, schema_issues,
)


class StructuredCompletion(Protocol):
    def complete_structured(
        self, system: str, user: str, schema: type[ExtractionResponse]
    ) -> ExtractionResponse: ...


class ExecutionConfigurationError(ValueError):
    """A fixed, non-sensitive execution configuration error."""


def sanitized_failure(error: Exception, stage: str) -> InferenceFailure:
    if isinstance(error, ResponseValidationError):
        return InferenceFailure(
            stage=error.diagnostic.stage if error.diagnostic else "evidence_validation",
            exception_class="ResponseValidationError", parameter=error.issues[0].field_path,
            explanation=error.issues[0].message,
        )
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

    def append(text: str, position: str, page: int | None, role: str | None = None) -> None:
        if not text.strip():
            return
        locator = f"{document.source}#" + (f"page={page}&" if page is not None else "") + position
        if role:
            locator += "&role=" + quote(role, safe="")
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
        append(paragraph.text, f"paragraph={paragraph_index}", paragraph.page_number, paragraph.role)
    for table_index, table in enumerate(document.tables):
        for row_index, row in enumerate(table.cells):
            for column_index, text in enumerate(row):
                append(text, f"table={table_index}&row={row_index}&column={column_index}", table.page_number)
    return evidence


_LEAD_DESCRIPTION = r"(?:low lead|lead free|no lead)"
_PRODUCT_DESCRIPTION = re.compile(
    rf"\b(?:{_LEAD_DESCRIPTION} "
    r"(?:(?:brass|bronze|copper|angle|ball|gate|check|stop|quarter turn|full port) ){0,6}"
    r"(?:valve|product)"
    rf"|(?:valve|product) (?:is |made of )?{_LEAD_DESCRIPTION})\b"
)
_UNSAFE_DESCRIPTION = re.compile(
    r"\b(?:not|no|non|without|never|neither|nor|cannot|isn|isnt|isn't|aren|arent|doesn|doesnt|"
    r"or|either|optional|option|options|may|might|could|if|unless|except|excluding|"
    r"available|availability|alternative|alternatives|variant|variants|family|series|"
    r"separate|separately|sold|recommended|recommend|use|install|select|choose|"
    r"other|another|includes|include|contains|incorporates|compatible|compatibility|"
    r"like|ish|almost|assumed|alleged|purported|claim|claims|"
    r"only|component|components|body|bodies|stem|stems|seat|seats|seal|seals|"
    r"ring|rings|washer|washers|handle|handles|coating|coatings|solder|"
    r"kit|kits|accessory|accessories|replacement|replacements|"
    r"example|examples|requirement|requirements|required|requires|should|must|"
    r"nsf\d*|ansi\d*|certified|certification|certifications|certify|compliant|compliance|"
    r"meets|approved|standard|standards|ab1953|s372|astm)\b"
)
_AMBIGUOUS_APPLICABILITY = re.compile(
    r"\b(?:family (?:scope|only|level|applicability)|alternative variant|"
    r"component (?:scope|only|material|description))\b"
)


def _boolean_answer(tokens: list[str], label: list[str], value: bool) -> bool:
    answers = ("true", "yes") if value else ("false", "no")
    return any(
        tokens[offset:offset + len(label) + 1] == label + [answer]
        for offset in range(len(tokens)) for answer in answers
    )


def _safe_description(text: str) -> bool:
    semantic = " ".join(normalized(text)[0])
    # "No-lead" is the one approved positive phrase containing a negation word.
    return not _UNSAFE_DESCRIPTION.search(re.sub(r"\bno lead\b(?! free\b)", "lead free", semantic))


def _vendor_description_context(candidate: Candidate, evidence: list[Evidence], quote: EvidenceMatch | None) -> bool:
    if (
        quote is None or len(quote.evidence_ids) != 1 or len(quote.cells) != 1
        or set(candidate.evidence_ids) != set(quote.evidence_ids)
    ):
        return False
    entry = next(entry for entry in evidence if entry.evidence_id == quote.evidence_ids[0])
    if entry.source_tier != "vendor_table" or not entry.text.lstrip().startswith("{"):
        return False
    cells = json.loads(entry.text)["cells"]
    cell = next((cell for cell in cells if cell["cell"] == quote.cells[0]), None)
    if cell is None or not isinstance(cell.get("column"), str):
        return False
    label = " ".join(normalized(cell["column"])[0])
    if label not in {"description", "product description", "general description"} and not re.fullmatch(r"descrgen[1-9]\d*", label):
        return False
    # A general-description cell describes the exact matched product row. Other
    # cells' size alternatives or hardware features do not scope its lead claim,
    # but explicit component-row scope or another unsafe lead claim still does.
    for other in cells:
        heading = " ".join(normalized(other.get("column", ""))[0]) if isinstance(other.get("column"), str) else ""
        if heading in {"component", "component name", "component type", "component description"}:
            return False
        text = " ".join(normalized(other["value"])[0])
        if re.search(rf"\b{_LEAD_DESCRIPTION}\b", text) and not _safe_description(other["value"]):
            return False
    return _safe_description(cell["value"]) and bool(
        re.search(rf"\b{_LEAD_DESCRIPTION}\b", " ".join(normalized(cell["value"])[0]))
    )


def _infer_lead_free(
    candidate: Candidate, evidence: list[Evidence], spans: list[list[Fragment]],
    quote: EvidenceMatch | None,
) -> bool:
    if (
        candidate.value is not True or not is_lead_free_attribute(candidate.attribute_id)
        or candidate.unit is not None or not candidate.supporting_quote
        or not _safe_description(candidate.supporting_quote)
        or not re.search(rf"\b{_LEAD_DESCRIPTION}\b", " ".join(normalized(candidate.supporting_quote)[0]))
    ):
        return False
    eligible = {
        entry.evidence_id for entry in evidence
        if entry.content_kind == "source_excerpt"
        and (entry.attribute_ids is None or candidate.attribute_id in entry.attribute_ids)
    }
    # Prefer a literal answer anywhere in eligible exact-product evidence. In
    # particular, an uncited explicit False must not be overwritten by inference.
    try:
        eligible_spans = windows(evidence, eligible)
    except ValueError:
        return False
    for span in eligible_spans:
        tokens = normalized(" ".join(part.text for part in span))[0]
        if any(_boolean_answer(tokens, ["lead", "free"], value) for value in (True, False)):
            return False
    for entry in evidence:
        if entry.evidence_id in eligible and entry.source_tier == "vendor_table" and entry.text.lstrip().startswith("{"):
            row = json.loads(entry.text)
            if any(
                is_lead_free_attribute(cell["column"])
                and normalized(cell["value"])[0] in (["true"], ["yes"], ["false"], ["no"])
                for cell in row["cells"] if isinstance(cell.get("column"), str)
            ):
                return False
    cited = [entry for entry in evidence if entry.evidence_id in candidate.evidence_ids]
    if any(_AMBIGUOUS_APPLICABILITY.search(" ".join(normalized(entry.qualification or "")[0])) for entry in cited):
        return False
    if _vendor_description_context(candidate, evidence, quote):
        return True
    texts = [" ".join(part.text for part in span) for span in spans]
    # Inspect complete cited fragments, not just the model's cropped/reordered
    # quote, so omitted negations, alternatives and component scope fail closed.
    return all(_safe_description(text) for text in texts) and any(
        _PRODUCT_DESCRIPTION.search(" ".join(normalized(text)[0])) for text in texts
    )


def validate_response(
    response: ExtractionResponse, manifest: Manifest, evidence: list[Evidence]
) -> list[EvidenceVerification]:
    definitions = {attribute.attribute_id: attribute for attribute in manifest.attributes}
    citations = {item.evidence_id: item for item in evidence}
    verified = []
    for index, candidate in enumerate(response.candidates):
        path = f"candidates[{index}]"
        if candidate.attribute_id not in definitions or candidate.attribute_id in manifest.existing_values:
            raise invalid(path + ".attribute_id", "Candidate must target a known missing attribute")
        definition = definitions[candidate.attribute_id]
        try:
            definition.validate_value(candidate.value, candidate.unit)
        except ValueError as error:
            field = ".unit" if not definition.unit_resolved or candidate.unit != definition.unit else ".value"
            raise invalid(path + field, str(error)) from error
        for offset, evidence_id in enumerate(candidate.evidence_ids):
            if evidence_id not in citations or citations[evidence_id].content_kind != "source_excerpt":
                raise invalid(f"{path}.evidence_ids[{offset}]", "Candidate must cite an available source excerpt")
            scope = citations[evidence_id].attribute_ids
            if scope is not None and candidate.attribute_id not in scope:
                raise invalid(f"{path}.evidence_ids[{offset}]", "Candidate exceeds the approved product/attribute applicability")
        try:
            spans = windows(evidence, set(candidate.evidence_ids))
            source_values = value_windows(evidence, set(candidate.evidence_ids))
            quote_match = match_quote(candidate.supporting_quote, evidence, set(candidate.evidence_ids)) if candidate.supporting_quote is not None else None
        except ValueError as error:
            raise invalid(path + ".evidence_ids", str(error)) from error
        if candidate.supporting_quote is not None and quote_match is None:
            raise invalid(path + ".supporting_quote", "Supporting quotation has no normalized match in cited evidence")
        if type(candidate.value) is bool:
            label = normalized(candidate.attribute_id)[0]
            literal_boolean = bool(candidate.supporting_quote) and _boolean_answer(
                normalized(candidate.supporting_quote or "")[0], label, candidate.value,
            )
            if not literal_boolean:
                if not _infer_lead_free(candidate, evidence, spans, quote_match):
                    raise invalid(path + ".supporting_quote", "Boolean proposals require an explicit labeled answer or the approved Lead-Free descriptive rule")
                inferred = Candidate.model_validate({
                    **candidate.model_dump(), "evidence_basis": "inferred_from_description",
                    "inference_rule": LEAD_FREE_DESCRIPTION_RULE, "confidence": None,
                    "qualification": "Exact-product description; product applicability must be reviewed.",
                })
                response.candidates[index] = inferred
                verified.append(EvidenceVerification(
                    quote=quote_match, value=None, evidence_basis=inferred.evidence_basis,
                    inference_rule=inferred.inference_rule,
                ))
                continue
        if candidate.evidence_basis != "literal" or candidate.inference_rule is not None:
            raise invalid(path + ".evidence_basis", "Literal candidates must not carry descriptive inference metadata")
        literal = str(candidate.value)
        if isinstance(candidate.value, float) and candidate.value.is_integer():
            literal = str(int(candidate.value))
        literals = ("true", "yes") if candidate.value is True else ("false", "no") if candidate.value is False else (literal,)
        value_spans = [[part] for span in source_values for part in span if part.vendor]
        if not value_spans:
            value_spans = source_values
            if candidate.supporting_quote is not None:
                quote_span = [[Fragment(citations[candidate.evidence_ids[0]], candidate.supporting_quote)]]
                if not any(match_text(value, quote_span) for value in literals):
                    raise invalid(path + ".value", "Candidate value has no normalized support in its grounded quotation")
        value_match = next((match for value in literals if (match := match_text(value, value_spans))), None)
        if value_match is None:
            raise invalid(path + ".value", "Candidate value has no normalized support in cited source text or vendor cells")
        if candidate.attribute_id.casefold() in {"material", "primary material", "body material"}:
            non_body = {
                evidence_id for item in pdf_items(evidence) for evidence_id, component in item.component_materials
                if not re.fullmatch(r"(?:(?:main|valve) )*body(?: casting| assembly)?", " ".join(normalized(component)[0]))
            }
            if non_body.intersection(value_match.evidence_ids):
                body_spans = windows(evidence, {
                    part.evidence.evidence_id for span in value_spans for part in span
                } - non_body)
                value_match = next((match for value in literals if (match := match_text(value, body_spans))), None)
                if value_match is None:
                    raise invalid(path + ".value", "A component material does not establish the product/body material")
        unit_match = match_text(candidate.unit, value_spans) if candidate.unit else None
        if candidate.unit and (unit_match is None or (
            candidate.supporting_quote and not any(part.vendor for span in spans for part in span)
            and match_text(candidate.unit, [[Fragment(citations[candidate.evidence_ids[0]], candidate.supporting_quote)]]) is None
        )):
            raise invalid(path + ".unit", "Candidates require explicit unit support in source excerpts")
        semantic_quote = " ".join(normalized(candidate.supporting_quote or "")[0])
        if candidate.supporting_quote and re.search(r"\b(maximum|max)\b", candidate.attribute_id, re.IGNORECASE):
            if re.search(r"\bworking\b", semantic_quote) and not re.search(
                r"\b(maximum|max)\b", semantic_quote
            ):
                raise invalid(path + ".supporting_quote", "Working rating does not establish a maximum rating")
        if candidate.supporting_quote and candidate.attribute_id.casefold() in {"material", "primary material", "body material"}:
            if re.search(r"\b(o ring|seat|stem|seal|ball|pin|washer)s?\b", semantic_quote) and not re.search(
                r"\b(body|primary material)\b", semantic_quote
            ):
                raise invalid(path + ".supporting_quote", "A component material does not establish the product/body material")
        if candidate.supporting_quote and type(candidate.value) is bool:
            quote_tokens = normalized(candidate.supporting_quote)[0]
            label = normalized(candidate.attribute_id)[0]
            # Punctuation may vary, but a boolean still needs its explicit labeled answer.
            if not _boolean_answer(quote_tokens, label, candidate.value):
                raise invalid(path + ".supporting_quote", "Boolean proposals require an explicit labeled answer, not negation or missing evidence")
        verified.append(EvidenceVerification(quote=quote_match, value=value_match, unit=unit_match))
    return verified


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
    verification = []
    extraction_error = None
    failure = None
    diagnostics = []
    invalid_attributes = set()
    model_call_status = "not_attempted"
    missing_attributes = any(attribute.attribute_id not in manifest.existing_values and attribute.unit_resolved for attribute in manifest.attributes)
    clarification_needed = any(not attribute.unit_resolved for attribute in manifest.attributes)
    skip_reason = ("definition_clarification_needed" if clarification_needed else "no_missing_attributes") if not missing_attributes else "no_eligible_evidence" if not evidence else None
    if live and skip_reason:
        model_call_status = "skipped"
    if skip_reason is None:
        prompt = json.dumps({
            "product": manifest.product.model_dump(),
            "attributes": [attribute.model_dump(exclude={"examples"}) for attribute in manifest.attributes
                           if attribute.attribute_id not in manifest.existing_values and attribute.unit_resolved],
            "evidence": [item.model_dump(mode="json") for item in evidence],
        })
        if live and completion is None:
            from backend.core.config import settings

            endpoint = settings.LLM_ENDPOINT or settings.AI_FOUNDRY_ENDPOINT
            if not endpoint or not endpoint.strip() or not settings.LLM_DEPLOYMENT or not settings.LLM_DEPLOYMENT.strip():
                raise ExecutionConfigurationError("Live inference requires LLM_ENDPOINT (or AI_FOUNDRY_ENDPOINT) and LLM_DEPLOYMENT")
        stage = "client_initialization"
        generator = None
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
                issues = []
                for index, candidate in enumerate(response.candidates):
                    try:
                        if not candidate.supporting_quote or not candidate.qualification or not candidate.qualification.strip():
                            field = "supporting_quote" if not candidate.supporting_quote else "qualification"
                            raise invalid(f"candidates[0].{field}", "Real proposals require quotations and applicability qualifications")
                        proposed = ExtractionResponse(candidates=[candidate])
                        checks = validate_response(proposed, manifest, evidence)
                        accepted.extend(proposed.candidates)
                        verification.extend(checks)
                    except ResponseValidationError as error:
                        invalid_attributes.add(candidate.attribute_id)
                        extraction_error = "invalid_response"
                        failure = sanitized_failure(error, stage)
                        issues.extend(issue.model_copy(update={
                            "field_path": issue.field_path.replace("candidates[0]", f"candidates[{index}]", 1),
                        }) for issue in error.issues)
                if issues:
                    handler = getattr(type(generator), "validation_failure", None)
                    diagnostics.append(handler(generator, issues) if handler else response_diagnostic(
                        payload=response.model_dump(mode="json"),
                        references={entry.evidence_id: entry.evidence_id for entry in evidence}, issues=issues,
                        raw_response_sha256=getattr(generator, "last_response_sha256", None),
                    ))
                    error = ResponseValidationError(issues)
                    error.diagnostic = diagnostics[-1]
                    failure = sanitized_failure(error, stage)
                response = ExtractionResponse(candidates=accepted)
            else:
                verification = validate_response(response, manifest, evidence)
            if extraction_error is None:
                cache_validated = getattr(type(generator), "validated_response", None)
                if cache_validated is not None:
                    cache_validated(generator, response)
        except RealPilotBudgetExceeded as error:
            model_call_status = "not_attempted"
            extraction_error = "model_failed"
            failure = sanitized_failure(error, "client_initialization")
        except ExecutionConfigurationError:
            raise
        except Exception as error:
            extraction_error = "invalid_response" if stage != "client_initialization" and isinstance(error, (ValidationError, ValueError, LLMSchemaValidationError)) else "model_failed"
            failure = sanitized_failure(error, stage)
            if isinstance(error, ResponseValidationError):
                diagnostics.append(error.diagnostic or response_diagnostic(
                    payload=response.model_dump(mode="json"),
                    references={entry.evidence_id: entry.evidence_id for entry in evidence},
                    issues=error.issues, raw_response_sha256=getattr(generator, "last_response_sha256", None),
                ))
            elif isinstance(error, (LLMSchemaValidationError, ValidationError)):
                content = error.raw_content if isinstance(error, LLMSchemaValidationError) else None
                diagnostics.append(response_diagnostic(
                    payload=parsed_content(content), references={entry.evidence_id: entry.evidence_id for entry in evidence},
                    issues=schema_issues(error.errors if isinstance(error, LLMSchemaValidationError) else error),
                    stage="structured_response_parsing",
                    raw_response_sha256=getattr(generator, "last_response_sha256", None),
                ))
            response = ExtractionResponse(candidates=[])
            verification = []

    results = []
    retrieval_failed = any(outcome.status == "failed" for outcome in retrieval)
    for attribute in manifest.attributes:
        candidates = [candidate for candidate in response.candidates if candidate.attribute_id == attribute.attribute_id]
        values = {(type(candidate.value).__name__, candidate.value, candidate.unit) for candidate in candidates}
        if attribute.attribute_id in manifest.existing_values:
            status = "existing"
        elif not attribute.unit_resolved:
            status = "definition_clarification_needed"
        elif extraction_error and not invalid_attributes:
            status = "extraction_failed"
        elif candidates:
            status = "conflict" if len(values) > 1 else "proposed"
        elif attribute.attribute_id in invalid_attributes:
            status = "extraction_failed"
        else:
            status = "retrieval_failed" if retrieval_failed else "missing_evidence"
        results.append(AttributeResult(
            attribute_id=attribute.attribute_id, status=status, candidates=candidates,
            verification=[check for candidate, check in zip(response.candidates, verification)
                          if candidate.attribute_id == attribute.attribute_id],
            definition_clarification=(
                f"Confirm the expected unit for {attribute.attribute_id}; no unit or dimensionless value is inferred."
                if status == "definition_clarification_needed" else None
            ),
        ))
    return EnrichmentResult(
        execution_mode=execution_mode,
        candidate_source="llm" if live else "supplied_response",
        model_call_status=model_call_status, skip_reason=skip_reason,
        manifest=manifest, observed_at=observed_at, retrieval=retrieval,
        evidence=evidence, attributes=results, extraction_error=extraction_error,
        failure=failure, validation_diagnostics=diagnostics,
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
