"""Standing quality-run metrics computed from persisted results and priced usage."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from backend.models.enrichment import EnrichmentResult

WEB_TIERS = frozenset({"manufacturer_web", "approved_web"})
WEB_OPERATIONS = frozenset({"web_search", "web_browse", "direct_page"})
UNATTRIBUTED = "(unattributed)"


def web_yield(results: Iterable[EnrichmentResult], usage: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Accepted web-cited values per web dollar, per attribute.

    Web spend is attributed by the usage record's attribute_id (tool-loop searches
    carry one); spend without an attribute stays in an explicit unattributed row.
    accepted_per_usd is None when no web money was spent, never a division by zero.
    """
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"web_cost_usd": 0.0, "web_operations": 0,
                                                           "accepted_web_values": 0})
    for entry in usage:
        if entry.get("operation") not in WEB_OPERATIONS:
            continue
        row = rows[entry.get("attribute_id") or (entry.get("context") or {}).get("attribute_id") or UNATTRIBUTED]
        row["web_operations"] += 1
        row["web_cost_usd"] += float(entry.get("cost_usd") or 0)
    for result in results:
        tiers = {item.evidence_id: item.source_tier for item in result.evidence}
        for attribute in result.attributes:
            for candidate in attribute.candidates:
                if candidate.judge_status == "accepted" and any(
                    tiers.get(key) in WEB_TIERS for key in candidate.evidence_ids
                ):
                    rows[attribute.attribute_id]["accepted_web_values"] += 1
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
