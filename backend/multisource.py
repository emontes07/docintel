"""Attribute-level source cascade using the existing extraction and review contracts."""

from datetime import datetime, timezone
from typing import Callable

from backend.extract import StructuredCompletion, run_enrichment
from backend.models.enrichment import (
    AttributeResult, EnrichmentResult, LiveBundle, Manifest, OfflineSource,
    RetrievalOutcome, SourceTier,
)

TIERS: tuple[SourceTier, ...] = (
    "internal_pdf", "vendor_table", "manufacturer_web", "approved_web",
)


def run_cascade(
    manifest: Manifest,
    load_tier: Callable[[SourceTier, list[str]], list[OfflineSource]],
    completion: StructuredCompletion,
) -> EnrichmentResult:
    attributes = {
        definition.attribute_id: AttributeResult(
            attribute_id=definition.attribute_id,
            status="existing" if definition.attribute_id in manifest.existing_values else "missing_evidence",
        )
        for definition in manifest.attributes
    }
    evidence = []
    retrieval = []
    calls = []
    failure = None
    extraction_error = None
    for tier in TIERS:
        eligible = {definition.attribute_id for definition in manifest.attributes if definition.unit_resolved}
        pending = [name for name, result in attributes.items() if result.status not in {"existing", "proposed"} and name in eligible]
        if not pending:
            retrieval.append(RetrievalOutcome(source_tier=tier, status="not_attempted"))
            continue
        sources = load_tier(tier, pending)
        if any(source.source_tier != tier for source in sources):
            raise ValueError("Source loader returned an incompatible tier")
        if not sources:
            retrieval.append(RetrievalOutcome(source_tier=tier, status="no_evidence"))
            continue
        scoped = Manifest(
            product=manifest.product,
            attributes=[definition for definition in manifest.attributes if definition.attribute_id in pending],
            source_ids=[source.source_id for source in sources],
        )
        result = run_enrichment(
            LiveBundle(execution_mode="live_inference", manifest=scoped, sources=sources),
            execution_mode="live_inference", completion=completion, qualified=True,
        )
        evidence.extend(result.evidence)
        retrieval.extend(outcome for outcome in result.retrieval if outcome.source_tier == tier)
        calls.append(result.model_call_status)
        if result.extraction_error:
            extraction_error, failure = result.extraction_error, result.failure
        for current in result.attributes:
            target = attributes[current.attribute_id]
            target.candidates.extend(current.candidates)
            values = {(type(candidate.value).__name__, candidate.value, candidate.unit) for candidate in target.candidates}
            if values:
                target.status = "conflict" if len(values) > 1 else "proposed"
            elif current.status in {"retrieval_failed", "extraction_failed"}:
                target.status = current.status
    status = "failed" if "failed" in calls else "succeeded" if "succeeded" in calls else "skipped"
    return EnrichmentResult(
        execution_mode="live_inference", candidate_source="llm",
        model_call_status=status,
        skip_reason=("no_missing_attributes" if not any(
            name not in manifest.existing_values for name in attributes
        ) else "no_eligible_evidence") if status == "skipped" else None,
        manifest=manifest, observed_at=datetime.now(timezone.utc),
        retrieval=retrieval, evidence=evidence, attributes=list(attributes.values()),
        extraction_error=extraction_error, failure=failure,
    )
