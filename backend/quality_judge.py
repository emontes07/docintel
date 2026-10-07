"""Persistent, policy-versioned verdicts for unique grounded proposal identities."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
import unicodedata
from typing import Literal

from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.models.enrichment import AttributeDefinition, Candidate, Contract, Evidence

logger = logging.getLogger(__name__)


class JudgeDecision(Contract):
    candidate_id: str
    decision: Literal["accepted", "judge_disputed"]
    reason: str


class QualityJudgment(Contract):
    decisions: list[JudgeDecision]


def _canonical(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


class JudgeCache:
    def __init__(self, store=None, *, namespace: str = "offline", policy: str):
        self.store = store
        self.namespace = hashlib.sha256(namespace.encode()).hexdigest()
        self.policy = hashlib.sha256(policy.encode()).hexdigest()
        self.memory: dict[str, dict] = {}

    def identity(self, definition: AttributeDefinition, candidate: Candidate, evidence: list[Evidence]) -> str:
        indexed = {entry.evidence_id: entry for entry in evidence}
        identity = {
            "definition": definition.model_dump(mode="json"),
            "value": _canonical(candidate.value) if isinstance(candidate.value, str) else candidate.value,
            "value_type": type(candidate.value).__name__, "unit": candidate.unit,
            "quote": _canonical(candidate.supporting_quote or ""),
            "locations": sorted(
                (indexed[key].source_locator, indexed[key].source_version)
                for key in candidate.evidence_ids
            ),
            "interpretation": {
                "origin": candidate.origin, "rule": candidate.normalization_rule,
                "inference_rule": candidate.inference_rule,
            },
            "policy": self.policy,
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        if key in self.memory:
            return self.memory[key]
        if self.store is None:
            return None
        try:
            value, _ = read_json(self.store, self.path(key))
        except Missing:
            return None
        JudgeDecision.model_validate({"candidate_id": key, "decision": value["decision"], "reason": value["reason"]})
        if value["policy"] != self.policy:
            raise ValueError("Judge cache policy mismatch")
        self.memory[key] = value
        return value

    def path(self, key: str) -> str:
        return f"quality-judge-cache/{self.namespace}/{self.policy}/{key}.json"

    def put(self, key: str, value: dict) -> dict:
        record = {**value, "policy": self.policy, "created_at": datetime.now(timezone.utc).isoformat()}
        if self.store is not None:
            try:
                write_json(self.store, self.path(key), record)
            except Conflict:
                record, _ = read_json(self.store, self.path(key))
                JudgeDecision.model_validate({"candidate_id": key, "decision": record["decision"], "reason": record["reason"]})
        self.memory[key] = record
        return record


def judge_candidates(
    candidates: list[Candidate], definitions: dict[str, AttributeDefinition], evidence: list[Evidence],
    packet: dict, call, cache: JudgeCache, *, diagnostics: list[dict], context: dict,
) -> None:
    """Batch fresh identities; only first-vote disputes receive two further votes."""
    grouped: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        key = cache.identity(definitions[candidate.attribute_id], candidate, evidence)
        grouped.setdefault(key, []).append(candidate)
    records, fresh = {}, {}
    for key, group in grouped.items():
        record = cache.get(key)
        diagnostics.append({
            **context, "operation": "judge_cache", "phase": "judge_cache",
            "status": "hit" if record is not None else "miss", "judge_key": key,
            "attribute_id": group[0].attribute_id, "candidates": len(group), "cost_usd": 0,
        })
        if record is None:
            fresh[key] = group[0]
        else:
            records[key] = record
    votes: dict[str, list[JudgeDecision]] = {key: [] for key in fresh}

    def vote(selected: dict[str, Candidate]) -> None:
        ids = {f"C{index + 1}": key for index, key in enumerate(selected)}
        request = {**packet, "candidates": [
            {"candidate_id": cid, **selected[key].model_dump(mode="json", include={
                "attribute_id", "value", "unit", "supporting_quote", "evidence_ids", "origin",
                "normalization_rule", "justification", "qualification",
            })} for cid, key in ids.items()
        ]}
        try:
            judged = call(request, QualityJudgment)
        except Exception as error:
            if getattr(error, "quality_budget_stop", False):
                raise
            logger.warning("Judge request failed; incomplete verdicts remain disputed and uncached (%s)", type(error).__name__)
            return
        counts = Counter(decision.candidate_id for decision in judged.decisions)
        for decision in judged.decisions:
            if decision.candidate_id in ids and counts[decision.candidate_id] == 1:
                votes[ids[decision.candidate_id]].append(decision)
        missing = set(ids) - {decision.candidate_id for decision in judged.decisions if counts[decision.candidate_id] == 1}
        if missing:
            logger.warning("Judge omitted or duplicated %d requested decisions; incomplete votes are not cached", len(missing))

    if fresh:
        vote(fresh)
        disputed = {key: candidate for key, candidate in fresh.items()
                    if not votes[key] or votes[key][0].decision == "judge_disputed"}
        for _ in range(2):
            if disputed:
                vote(disputed)
        for key in fresh:
            expected = 3 if key in disputed else 1
            current = votes[key]
            complete = len(current) == expected
            accepted = sum(v.decision == "accepted" for v in current)
            decision = "accepted" if complete and accepted > expected // 2 else "judge_disputed"
            supporting = next((v.reason for v in current if v.decision == decision), None)
            reason = (
                f"Majority {max(accepted, expected - accepted)}/3: {supporting}"
                if complete and expected == 3 else supporting
                if complete else "Incomplete judge votes; grounded proposal retained for human review, verdict not cached."
            )
            record = {"decision": decision, "reason": reason, "votes": [v.model_dump(mode="json") for v in current]}
            records[key] = cache.put(key, record) if complete else record
    for key, group in grouped.items():
        record = records[key]
        for candidate in group:
            candidate.judge_status = record["decision"]
            candidate.judge_reason = record["reason"]
            candidate.grounding = {**(candidate.grounding or {}), "judge_cache": {
                "key": key, "policy": cache.policy, "votes": record["votes"],
            }}
