"""Provider-neutral contracts for one-product enrichment from supplied evidence."""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from backend.core.docintel import ParsedDocument

AttributeValue = str | int | float | bool
SourceTier = Literal["internal_pdf", "vendor_table", "manufacturer_web", "approved_web"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ProductKey(Contract):
    item_id: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    mpn: str = Field(min_length=1)
    hierarchy_node: str = Field(min_length=1)


class AttributeDefinition(Contract):
    attribute_id: str = Field(min_length=1)
    description: str
    value_type: Literal["string", "integer", "number", "boolean"]
    unit: str | None = None
    allowed_values: list[AttributeValue] = Field(default_factory=list)
    examples: list[AttributeValue] = Field(default_factory=list)
    definition_context: str | None = None
    type_guidance: str | None = None
    definition_node: str | None = None
    unit_resolved: bool = True

    def validate_value(self, value: AttributeValue, unit: str | None) -> None:
        types = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}
        if type(value) not in types[self.value_type]:
            raise ValueError("Attribute value has the wrong type")
        if isinstance(value, str) and not value.strip():
            raise ValueError("Attribute value must not be blank")
        if not self.unit_resolved:
            raise ValueError("Attribute unit guidance requires an explicit mapping")
        if unit != self.unit:
            raise ValueError("Attribute unit does not match its definition")
        if self.allowed_values and value not in self.allowed_values:
            raise ValueError("Attribute value is not allowed")


class Manifest(Contract):
    product: ProductKey
    attributes: list[AttributeDefinition] = Field(min_length=1)
    existing_values: dict[str, AttributeValue] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        definitions = {attribute.attribute_id: attribute for attribute in self.attributes}
        if len(definitions) != len(self.attributes) or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Attribute and source IDs must be unique")
        for attribute_id, value in self.existing_values.items():
            if attribute_id not in definitions:
                raise ValueError("Existing value references an unknown attribute")
            definitions[attribute_id].validate_value(value, definitions[attribute_id].unit)
        return self


class Evidence(Contract):
    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_locator: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_tier: SourceTier
    content_kind: Literal["source_excerpt", "generated_answer"]
    text: str = Field(min_length=1)
    observed_at: AwareDatetime
    provider_retrieved_at: AwareDatetime | None = None
    source_published_at: AwareDatetime | None = None
    attribute_ids: list[str] | None = None
    qualification: str | None = None
    discovery_method: Literal["supplied_reference", "webiq"] | None = None


class OfflineSource(Contract):
    source_id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    product: ProductKey
    document: ParsedDocument | None = None
    error_code: Literal["not_found", "access_denied", "timeout", "parse_failed"] | None = None
    provider_retrieved_at: AwareDatetime | None = None
    source_published_at: AwareDatetime | None = None
    source_tier: SourceTier = "internal_pdf"
    excerpts: list[Evidence] | None = None
    attribute_ids: list[str] | None = None
    qualification: str | None = None

    @model_validator(mode="after")
    def require_document_or_error(self) -> Self:
        if sum(value is not None for value in (self.document, self.excerpts, self.error_code)) != 1:
            raise ValueError("A source must contain a parsed document, excerpts, or a retrieval error")
        if self.document is not None and not self.document.cache_key:
            raise ValueError("Parsed documents require a source version/cache key")
        if self.excerpts is not None and any(
            item.source_id != self.source_id or item.source_tier != self.source_tier
            for item in self.excerpts
        ):
            raise ValueError("Excerpt source and tier must match the supplied source")
        return self


class Candidate(Contract):
    attribute_id: str
    value: AttributeValue
    unit: str | None = None
    evidence_ids: list[str] = Field(min_length=1)
    origin: Literal["model_generated"] = "model_generated"
    supporting_quote: str | None = None
    qualification: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class ExtractionResponse(Contract):
    candidates: list[Candidate]


class EvidenceBundle(Contract):
    manifest: Manifest
    sources: list[OfflineSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("Source IDs must be unique")
        return self


class OfflineBundle(EvidenceBundle):
    execution_mode: Literal["offline_replay"] = "offline_replay"
    generated_response: ExtractionResponse


class LiveBundle(EvidenceBundle):
    execution_mode: Literal["live_inference"]


class RetrievalOutcome(Contract):
    source_tier: SourceTier
    source_id: str | None = None
    status: Literal["success", "no_evidence", "failed", "not_attempted"]
    error_code: str | None = None


class ReviewDecision(Contract):
    attribute_id: str
    decision: Literal["approve", "correct", "reject"]
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    reason: str = Field(min_length=1)
    candidate_index: int | None = Field(default=None, ge=0)
    corrected_value: AttributeValue | None = None
    corrected_unit: str | None = None


class ReviewAnnotation(Contract):
    origin: Literal["review_annotation"] = "review_annotation"
    candidate_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    author: str = Field(min_length=1)
    annotated_at: AwareDatetime


class EvidenceMatch(Contract):
    method: Literal["verbatim", "normalized", "adjacent_fragments", "vendor_cells"]
    normalization: list[str]
    evidence_ids: list[str]
    source_locations: list[str]
    cells: list[str] = Field(default_factory=list)
    normalized_sha256: str


class EvidenceVerification(Contract):
    version: Literal["grounded-normalization-v1"] = "grounded-normalization-v1"
    quote: EvidenceMatch | None = None
    value: EvidenceMatch
    unit: EvidenceMatch | None = None


class AttributeResult(Contract):
    attribute_id: str
    status: Literal["existing", "proposed", "conflict", "missing_evidence", "retrieval_failed", "extraction_failed", "definition_clarification_needed"]
    definition_clarification: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    verification: list[EvidenceVerification] = Field(default_factory=list)
    review: ReviewDecision | None = None
    review_annotations: list[ReviewAnnotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_annotation_targets(self) -> Self:
        if self.verification and len(self.verification) != len(self.candidates):
            raise ValueError("Verification records must correspond to the candidate order")
        if any(annotation.candidate_index >= len(self.candidates) for annotation in self.review_annotations):
            raise ValueError("Review annotation must reference an existing candidate")
        return self


class InferenceFailure(Contract):
    stage: Literal["client_initialization", "authentication", "network_connection", "service_request", "structured_response_parsing", "evidence_validation"]
    exception_class: str
    http_status: int | None = None
    service_error_code: str | None = None
    parameter: str | None = None
    explanation: str


class ValidationIssue(Contract):
    field_path: str
    message: str


class DiagnosticReference(Contract):
    field_path: str
    value: str | None = None
    sha256: str
    redacted: bool


class ResponseValidationDiagnostic(Contract):
    schema_version: Literal[1] = 1
    stage: Literal["structured_response_parsing", "evidence_validation"]
    issues: list[ValidationIssue]
    used_references: list[DiagnosticReference]
    valid_references: list[str]
    parsed_response: dict[str, JsonValue]
    raw_response_sha256: str | None = None
    raw_response_hash_basis: Literal["provider_content", "unavailable"]
    source_tier: SourceTier | None = None
    reservation_id: str | None = None
    prompt_format: str | None = None
    truncated: bool = False


class EnrichmentResult(Contract):
    execution_mode: Literal["offline_replay", "live_inference"]
    candidate_source: Literal["supplied_response", "llm"]
    model_call_status: Literal["not_attempted", "skipped", "succeeded", "failed"]
    skip_reason: Literal["no_eligible_evidence", "no_missing_attributes", "definition_clarification_needed"] | None = None
    manifest: Manifest
    observed_at: AwareDatetime
    retrieval: list[RetrievalOutcome]
    evidence: list[Evidence]
    attributes: list[AttributeResult]
    extraction_error: Literal["invalid_response", "model_failed"] | None = None
    failure: InferenceFailure | None = None
    validation_diagnostics: list[ResponseValidationDiagnostic] = Field(default_factory=list)
