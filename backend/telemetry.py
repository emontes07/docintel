"""Advisory local timings and accounting summaries; never authorization or billing."""

from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TierTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_tier: str
    outcome: Literal["not_attempted", "no_evidence", "completed", "failed", "skipped"]
    elapsed_ms: float = Field(ge=0)
    retrieval_ms: float = Field(default=0, ge=0)
    extraction_ms: float = Field(default=0, ge=0)
    model_ms: float | None = Field(default=None, ge=0)
    model_requests: int | None = Field(default=None, ge=0)
    response_cache_hits: int = Field(default=0, ge=0)
    unknown_usage_calls: int = Field(default=0, ge=0)
    measured_input_tokens: int | None = Field(default=None, ge=0)
    measured_output_tokens: int | None = Field(default=None, ge=0)
    known_input_tokens: int = Field(default=0, ge=0)
    known_output_tokens: int = Field(default=0, ge=0)
    estimated_model_cost_usd: str | None = None
    known_model_cost_usd: str | None = None
    reserved_input_tokens: int = Field(default=0, ge=0)
    reserved_output_tokens: int = Field(default=0, ge=0)
    reserved_cost_microdollars: int = Field(default=0, ge=0)
    search_attempts: int = Field(default=0, ge=0)
    direct_page_attempts: int = Field(default=0, ge=0)


class ItemTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["execution-telemetry-v1"] = "execution-telemetry-v1"
    elapsed_ms: float = Field(ge=0)
    timing_basis: Literal["local_monotonic_wall_clock"] = "local_monotonic_wall_clock"
    accounting_status: Literal["not_instrumented", "complete", "partial"] = "not_instrumented"
    cost_basis: Literal["approved_unit_prices_times_measured_model_tokens_not_billing"] = (
        "approved_unit_prices_times_measured_model_tokens_not_billing"
    )
    model_unit_prices_usd: dict[str, str] | None = None
    model_requests: int | None = Field(default=None, ge=0)
    measured_input_tokens: int | None = Field(default=None, ge=0)
    measured_output_tokens: int | None = Field(default=None, ge=0)
    unknown_usage_calls: int = Field(default=0, ge=0)
    known_input_tokens: int = Field(default=0, ge=0)
    known_output_tokens: int = Field(default=0, ge=0)
    estimated_model_cost_usd: str | None = None
    reserved_input_tokens: int = Field(default=0, ge=0)
    reserved_output_tokens: int = Field(default=0, ge=0)
    reserved_cost_microdollars: int = Field(default=0, ge=0)
    tiers: list[TierTelemetry] = Field(default_factory=list)


def milliseconds(start: float, end: float) -> float:
    return max(0.0, (end - start) * 1000)


def _tokens(usage, name):
    value = usage.get(name) if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None


def _prices(prices):
    try:
        values = [Decimal(str(prices[name])) for name in ("input_token", "output_token")]
        return values if all(value.is_finite() and value >= 0 for value in values) else None
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def _cost_sum(values) -> str:
    with localcontext() as context:
        context.prec = 60
        return format(sum((Decimal(value) for value in values), Decimal(0)), "f")


def record_accounting(
    telemetry: ItemTelemetry, inference: list[dict], reservations: list[dict], *, unit_prices: dict | None = None,
) -> None:
    """Join existing receipts, excluding cache hits and duplicate failure notices.

    Missing usage is unknown, not zero. Known subtotals remain explicit when
    any request lacks a complete usage report. Reservations are never measured usage.
    """
    prices = _prices(unit_prices)
    telemetry.model_unit_prices_usd = None if prices is None else {
        name: format(value, "f") for name, value in zip(("input_token", "output_token"), prices)
    }
    for tier in telemetry.tiers:
        entries = [entry for entry in inference if entry.get("source_tier") == tier.source_tier]
        model = [entry for entry in entries if entry.get("method") == "model" and entry.get("new_model_call") is True]
        tier.model_requests = len(model)
        tier.response_cache_hits = sum(entry.get("method") == "compatible_response_cache" for entry in entries)
        tier.model_ms = (
            None if any(entry.get("elapsed_ms") is None for entry in model)
            else sum(entry["elapsed_ms"] for entry in model)
        )
        tier.unknown_usage_calls = sum(
            any(_tokens(entry.get("usage"), name) is None for name in ("input_tokens", "output_tokens"))
            for entry in model
        )
        tier.known_input_tokens = sum(_tokens(entry.get("usage"), "input_tokens") or 0 for entry in model)
        tier.known_output_tokens = sum(_tokens(entry.get("usage"), "output_tokens") or 0 for entry in model)
        tier.measured_input_tokens = None if any(_tokens(entry.get("usage"), "input_tokens") is None for entry in model) else tier.known_input_tokens
        tier.measured_output_tokens = None if any(_tokens(entry.get("usage"), "output_tokens") is None for entry in model) else tier.known_output_tokens
        tier.estimated_model_cost_usd = tier.known_model_cost_usd = None
        if prices is not None:
            with localcontext() as context:
                context.prec = 60
                cost = Decimal(tier.known_input_tokens) * prices[0] + Decimal(tier.known_output_tokens) * prices[1]
            tier.known_model_cost_usd = format(cost, "f")
            tier.estimated_model_cost_usd = None if tier.unknown_usage_calls else tier.known_model_cost_usd
        tier.reserved_input_tokens = tier.reserved_output_tokens = tier.reserved_cost_microdollars = 0
        tier.search_attempts = tier.direct_page_attempts = 0
        for receipt in reservations:
            if receipt.get("source_tier") != tier.source_tier:
                continue
            usage = receipt["reserved_usage"]
            tier.reserved_input_tokens += usage.get("input_tokens", 0)
            tier.reserved_output_tokens += usage.get("output_tokens", 0)
            tier.reserved_cost_microdollars += receipt["reserved_microdollars"]
            tier.search_attempts += receipt["operation"] == "search"
            tier.direct_page_attempts += receipt["operation"] == "web_retrieval"
    telemetry.accounting_status = "partial" if any(tier.unknown_usage_calls for tier in telemetry.tiers) else "complete"
    telemetry.model_requests = sum(tier.model_requests or 0 for tier in telemetry.tiers)
    telemetry.unknown_usage_calls = sum(tier.unknown_usage_calls for tier in telemetry.tiers)
    telemetry.known_input_tokens = sum(tier.known_input_tokens for tier in telemetry.tiers)
    telemetry.known_output_tokens = sum(tier.known_output_tokens for tier in telemetry.tiers)
    telemetry.measured_input_tokens = (
        telemetry.known_input_tokens if all(tier.measured_input_tokens is not None for tier in telemetry.tiers) else None
    )
    telemetry.measured_output_tokens = (
        telemetry.known_output_tokens if all(tier.measured_output_tokens is not None for tier in telemetry.tiers) else None
    )
    telemetry.estimated_model_cost_usd = (
        _cost_sum(tier.estimated_model_cost_usd for tier in telemetry.tiers)
        if all(tier.estimated_model_cost_usd is not None for tier in telemetry.tiers) else None
    )
    for name in ("reserved_input_tokens", "reserved_output_tokens", "reserved_cost_microdollars"):
        setattr(telemetry, name, sum(getattr(tier, name) for tier in telemetry.tiers))


def summary_report(results, *, expected_items: int | None = None) -> dict:
    """Return a small JSON-ready batch summary; retain null totals when incomplete."""
    results = list(results)
    expected = len(results) if expected_items is None else expected_items
    if expected < len(results):
        raise ValueError("Expected item count cannot be smaller than supplied results")
    measured = [result.telemetry for result in results if result.telemetry is not None]
    tiers = [tier for item in measured for tier in item.tiers]
    complete = len(measured) == expected and all(item.accounting_status == "complete" for item in measured)
    cost_complete = complete and all(tier.estimated_model_cost_usd is not None for tier in tiers)
    elapsed = [item.elapsed_ms for item in measured]
    literal = inferred = unresolved = 0
    for result in results:
        for attribute in result.attributes:
            literal += sum(candidate.evidence_basis == "literal" for candidate in attribute.candidates)
            inferred += sum(candidate.evidence_basis == "inferred_from_description" for candidate in attribute.candidates)
            unresolved += attribute.status != "existing" and not (
                attribute.status == "proposed" and any(candidate.evidence_basis == "literal" for candidate in attribute.candidates)
            )
    return {
        "version": "execution-summary-v1", "items": expected, "items_with_result": len(results),
        "instrumented_items": len(measured), "items_without_telemetry": expected - len(measured),
        "accounting_complete": complete, "sum_item_elapsed_ms": sum(elapsed) if len(measured) == expected else None,
        "mean_item_elapsed_ms": sum(elapsed) / len(elapsed) if elapsed else None,
        "model_requests": sum(tier.model_requests or 0 for tier in tiers) if complete else None,
        "known_model_requests": sum(tier.model_requests or 0 for tier in tiers),
        "unknown_usage_calls": sum(tier.unknown_usage_calls for tier in tiers),
        "measured_input_tokens": sum(tier.known_input_tokens for tier in tiers) if complete else None,
        "measured_output_tokens": sum(tier.known_output_tokens for tier in tiers) if complete else None,
        "known_input_tokens": sum(tier.known_input_tokens for tier in tiers),
        "known_output_tokens": sum(tier.known_output_tokens for tier in tiers),
        "estimated_model_cost_usd": _cost_sum(tier.estimated_model_cost_usd for tier in tiers) if cost_complete else None,
        "reserved_input_tokens": sum(tier.reserved_input_tokens for tier in tiers),
        "reserved_output_tokens": sum(tier.reserved_output_tokens for tier in tiers),
        "reserved_cost_microdollars": sum(tier.reserved_cost_microdollars for tier in tiers),
        "literal_candidates": literal, "inferred_review_candidates": inferred, "unresolved_attributes": unresolved,
        "qualification": "Local timings; summed item durations are not batch wall time. Estimates are not billed cost or measured accuracy.",
    }


def technical_rows(telemetry: ItemTelemetry | None, *, row) -> list[list]:
    if telemetry is None:
        return [[row, "", "not_instrumented"]]
    if not telemetry.tiers:
        return [[row, "", telemetry.accounting_status, *([None] * 18), telemetry.elapsed_ms]]
    return [[row, tier.source_tier, telemetry.accounting_status, tier.outcome, tier.elapsed_ms,
             tier.retrieval_ms, tier.extraction_ms, tier.model_ms, tier.model_requests,
             tier.response_cache_hits, tier.measured_input_tokens, tier.measured_output_tokens,
             tier.unknown_usage_calls, tier.known_input_tokens, tier.known_output_tokens,
             tier.estimated_model_cost_usd, tier.reserved_input_tokens, tier.reserved_output_tokens,
             tier.reserved_cost_microdollars, tier.search_attempts, tier.direct_page_attempts, telemetry.elapsed_ms]
            for tier in telemetry.tiers]


TECHNICAL_COLUMNS = [
    "Row", "Source tier", "Accounting status", "Tier outcome", "Tier elapsed ms", "Retrieval ms",
    "Extraction and validation ms", "Model request ms", "New model requests", "Response cache hits",
    "Measured input tokens", "Measured output tokens", "Calls with missing usage", "Known input subtotal",
    "Known output subtotal", "Estimated model cost USD (not billing)", "Reserved input tokens",
    "Reserved output tokens", "Reserved cost microdollars", "Search attempts", "Direct page attempts",
    "Item elapsed ms",
]
