"""Attribute-level source cascade using the existing extraction and review contracts."""

from datetime import datetime, timezone
from time import monotonic
from typing import Callable

from backend.extract import ExecutionConfigurationError, StructuredCompletion, run_enrichment, source_evidence
from backend.models.enrichment import (
    AttributeResult, EnrichmentResult, LiveBundle, Manifest, OfflineSource,
    RetrievalOutcome, SourceTier,
)
from backend.telemetry import ItemTelemetry, TierTelemetry, milliseconds

TIERS: tuple[SourceTier, ...] = (
    "internal_pdf", "vendor_table", "manufacturer_web", "approved_web",
)
WEB_TIERS = frozenset({"manufacturer_web", "approved_web"})


class OptionalTierSkipped(ExecutionConfigurationError):
    """An optional admission decision, never a denial from the execution guard."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def attribute_resolved(result: AttributeResult) -> bool:
    return result.status == "existing" or (
        result.status == "proposed" and any(candidate.evidence_basis == "literal" for candidate in result.candidates)
    )


def run_cascade(
    manifest: Manifest,
    load_tier: Callable[[SourceTier, list[str]], list[OfflineSource]],
    completion: StructuredCompletion,
) -> EnrichmentResult:
    started = monotonic()
    telemetry = ItemTelemetry(elapsed_ms=0)
    attributes = {
        definition.attribute_id: AttributeResult(
            attribute_id=definition.attribute_id,
            status=("existing" if definition.attribute_id in manifest.existing_values
                    else "missing_evidence" if definition.unit_resolved else "definition_clarification_needed"),
            definition_clarification=(
                f"Confirm the expected unit for {definition.attribute_id}; no unit or dimensionless value is inferred."
                if not definition.unit_resolved else None
            ),
        )
        for definition in manifest.attributes
    }
    evidence = []
    retrieval = []
    calls = []
    failure = None
    diagnostics = []
    extraction_error = None
    for tier in TIERS:
        tier_started = monotonic()
        eligible = {definition.attribute_id for definition in manifest.attributes if definition.unit_resolved}
        pending = [name for name, result in attributes.items() if not attribute_resolved(result) and name in eligible]
        if not pending:
            retrieval.append(RetrievalOutcome(source_tier=tier, status="not_attempted"))
            telemetry.tiers.append(TierTelemetry(source_tier=tier, outcome="not_attempted", elapsed_ms=milliseconds(tier_started, monotonic())))
            continue
        retrieval_started = monotonic()
        sources = load_tier(tier, pending)
        retrieval_ms = milliseconds(retrieval_started, monotonic())
        if any(source.source_tier != tier for source in sources):
            raise ValueError("Source loader returned an incompatible tier")
        if not sources:
            retrieval.append(RetrievalOutcome(source_tier=tier, status="no_evidence"))
            telemetry.tiers.append(TierTelemetry(
                source_tier=tier, outcome="no_evidence", elapsed_ms=milliseconds(tier_started, monotonic()),
                retrieval_ms=retrieval_ms,
            ))
            continue
        scoped = Manifest(
            product=manifest.product,
            attributes=[definition for definition in manifest.attributes if definition.attribute_id in pending],
            source_ids=[source.source_id for source in sources],
        )
        extraction_started = monotonic()
        try:
            result = run_enrichment(
                LiveBundle(execution_mode="live_inference", manifest=scoped, sources=sources),
                execution_mode="live_inference", completion=completion, qualified=True,
            )
        except OptionalTierSkipped as error:
            if tier not in WEB_TIERS:
                raise
            evidence.extend(
                entry for source in sources
                if source.product == manifest.product and source.error_code is None
                for entry in source_evidence(source, datetime.now(timezone.utc))
            )
            retrieval.append(RetrievalOutcome(
                source_tier=tier, status="not_attempted", error_code=error.code,
            ))
            telemetry.tiers.append(TierTelemetry(
                source_tier=tier, outcome="skipped", elapsed_ms=milliseconds(tier_started, monotonic()),
                retrieval_ms=retrieval_ms, extraction_ms=milliseconds(extraction_started, monotonic()),
            ))
            continue
        telemetry.tiers.append(TierTelemetry(
            source_tier=tier, outcome="failed" if result.extraction_error or any(
                entry.status == "failed" and entry.source_tier == tier for entry in result.retrieval
            ) else "completed" if result.evidence else "no_evidence",
            elapsed_ms=milliseconds(tier_started, monotonic()), retrieval_ms=retrieval_ms,
            extraction_ms=milliseconds(extraction_started, monotonic()),
        ))
        evidence.extend(result.evidence)
        retrieval.extend(outcome for outcome in result.retrieval if outcome.source_tier == tier)
        calls.append(result.model_call_status)
        diagnostics.extend(result.validation_diagnostics)
        if result.extraction_error:
            extraction_error, failure = result.extraction_error, result.failure
        for current in result.attributes:
            target = attributes[current.attribute_id]
            target.candidates.extend(current.candidates)
            target.verification.extend(current.verification)
            values = {(type(candidate.value).__name__, candidate.value, candidate.unit) for candidate in target.candidates}
            if values:
                target.status = "conflict" if len(values) > 1 else "proposed"
            elif current.status in {"retrieval_failed", "extraction_failed"}:
                target.status = current.status
    status = ("failed" if "failed" in calls else "succeeded" if "succeeded" in calls
              else "not_attempted" if "not_attempted" in calls else "skipped")
    telemetry.elapsed_ms = milliseconds(started, monotonic())
    return EnrichmentResult(
        execution_mode="live_inference", candidate_source="llm",
        model_call_status=status,
        skip_reason=("no_missing_attributes" if not any(
            name not in manifest.existing_values for name in attributes
        ) else "no_eligible_evidence") if status == "skipped" else None,
        manifest=manifest, observed_at=datetime.now(timezone.utc),
        retrieval=retrieval, evidence=evidence, attributes=list(attributes.values()),
        extraction_error=extraction_error, failure=failure, validation_diagnostics=diagnostics,
        telemetry=telemetry,
    )
