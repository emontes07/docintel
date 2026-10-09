"""Standing quality-run metrics computed from persisted results and priced usage."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from backend.models.enrichment import EnrichmentResult

WEB_TIERS = frozenset({"manufacturer_web", "approved_web"})
WEB_OPERATIONS = frozenset({"web_search", "web_browse", "direct_page"})
UNATTRIBUTED = "(unattributed)"


class WebYieldAccumulator:
    """Bounded-memory accepted-web-value counts for a streamed product batch."""

    def __init__(self):
        self.accepted: dict[str, int] = defaultdict(int)

    def add(self, result: EnrichmentResult) -> None:
        tiers = {item.evidence_id: item.source_tier for item in result.evidence}
        for attribute in result.attributes:
            for candidate in attribute.candidates:
                if candidate.judge_status == "accepted" and any(
                    tiers.get(key) in WEB_TIERS for key in candidate.evidence_ids
                ):
                    self.accepted[attribute.attribute_id] += 1

    def report(self, usage: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Accepted web-cited values per web dollar, per attribute.

        Search spend is attributed by usage.attribute_id where present; other spend
        remains explicitly unattributed. accepted_per_usd is null at zero spend.
        """
        rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"web_cost_usd": 0.0, "web_operations": 0,
                                                               "accepted_web_values": 0})
        for entry in usage:
            if entry.get("operation") not in WEB_OPERATIONS:
                continue
            row = rows[entry.get("attribute_id") or (entry.get("context") or {}).get("attribute_id") or UNATTRIBUTED]
            row["web_operations"] += 1
            row["web_cost_usd"] += float(entry.get("cost_usd") or 0)
        for name, count in self.accepted.items():
            rows[name]["accepted_web_values"] = count
        attributes = {}
        for name, row in sorted(rows.items()):
            cost = round(row["web_cost_usd"], 6)
            attributes[name] = {**row, "web_cost_usd": cost,
                                "accepted_per_usd": round(row["accepted_web_values"] / cost, 4) if cost else None}
        total_cost = round(sum(row["web_cost_usd"] for row in attributes.values()), 6)
        total_accepted = sum(row["accepted_web_values"] for row in attributes.values())
        return {"attributes": attributes, "total": {
            "web_cost_usd": total_cost, "web_operations": sum(row["web_operations"] for row in attributes.values()),
            "accepted_web_values": total_accepted,
            "accepted_per_usd": round(total_accepted / total_cost, 4) if total_cost else None,
        }}


def web_yield(results: Iterable[EnrichmentResult], usage: Iterable[dict[str, Any]]) -> dict[str, Any]:
    accumulator = WebYieldAccumulator()
    for result in results:
        accumulator.add(result)
    return accumulator.report(usage)
