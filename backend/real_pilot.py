"""Server-only, finite real-pilot authorization; this module performs no paid work.

The private ``configuration/real-pilot-approval.json`` record has exactly these
fields (no credentials, prices guessed by code, or client-supplied approvals):

* schema_version: 1; approved: true; id: canonical UUID
* execution_scope: optional "full" (default) or "internal_only"; hash-bound with
  the complete approval. Internal-only permits approved Blob PDF/XLSX copies,
  requires search/web_retrieval/retrieval limits of zero, and omits web settings
  and search/web_retrieval prices. Website references remain metadata only.
* approved_by: operator UUID present in server DOCINTEL_REAL_PILOT_OPERATOR_IDS
* not_before, expires_at: timezone-aware ISO timestamps, at most 1200s apart
* batch_id, owner, batch_sha256: exact intake identity and binding_digest(record)
* customer_processing_approved: must be true in every scope; authorizes only the
  approved customer-data handling/services, not WebIQ entitlement
* identities: api_principal_id and worker_principal_id (canonical UUIDs; they may
  differ). At execution, DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID must match the
  worker principal independently provisioned by the deployment administrator.
* environment: approved nonsecret settings from ENVIRONMENT_KEYS; AZURE_CLIENT_ID
  is mandatory (empty string means system-assigned managed identity). Required
  service settings depend on enabled operation limits.
* limits: products, executions, analysis, inference, search, web_retrieval, retrieval,
  analysis_pages, input_tokens, output_tokens, spend_microdollars (integers)
* unit_prices_usd: analysis_page, input_token, output_token, search,
  web_retrieval (positive decimal strings, per individual unit, never per 1M)

Enablement additionally requires DOCINTEL_REAL_PILOT_ENABLED=true. The operator
allowlist is server-controlled: an uploaded name or a self-asserted verified flag
is not verification. Only an authenticated operator's installed approval belongs
in the private configuration record; HTTP request data must never write it.

Call before_execution once per worker slice, operation_key for each exact action,
reserve BEFORE calling a service, then record_usage with measured usage if known.
Construction checks approval/batch validity without assuming the API identity is
the worker identity. before_execution and every reserve check worker runtime
settings and its independently configured principal binding; no identity lookup
or token request is performed by the guard.
Pass conservative server-computed token/page upper bounds, and configure clients
to enforce them and disable SDK/application retries. Failed/unknown calls retain
their full reservation. There is deliberately no release/reset/retry API.

The singleton ledger pins the entire approval at first execution, including its
ID. Editing or replacing the approval cannot mint a fresh allowance. A new pilot
requires a separately reviewed operational procedure, not automatic rollover.
``retrieval`` counts Graph/internal source attempts separately (maximum four);
it has no assumed billable price or token/page allowance. ``web_retrieval`` is the
canonical operation name for the twelve-fetch web ceiling.

The fixed ``configuration/real-pilot-recovery.json`` is a separate, explicitly
approved amendment, never a replacement approval or allowance. It binds the
original approval, batch, one consumed execution, exact pre-inference ledger,
two interrupted item records and compatible cached parses. Its later window is
at most 1200 seconds. Only four selected-item inference requests are permitted,
each reserving at most 30000 input-plus-output tokens. New analysis is forbidden.
The remaining execution is charged before an immutable history audit and fenced
conditional status recovery; every other batch item is explicitly deferred.
"""

import base64
import copy
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from urllib.parse import urlsplit

from backend.batch_store import Conflict, Missing, read_json, write_json


class RealPilotBudgetExceeded(ValueError):
    """A capacity denial before reservation, not an authorization failure."""

    def __init__(self, dimension: str, requested: int, remaining: int):
        self.dimension = dimension
        self.requested = requested
        self.remaining = remaining
        super().__init__(
            f"Real-pilot {dimension} budget exhausted: requested {requested}, remaining {remaining}. "
            "No service call or reservation was made."
        )


APPROVAL_KEY = "configuration/real-pilot-approval.json"
BUDGET_KEY = "budgets/real-pilot.json"
RECOVERY_KEY = "configuration/real-pilot-recovery.json"
RECOVERY_AUDIT_KEY = "operations/real-pilot-recovery.json"
FINAL_RERUN_KEY = "configuration/real-pilot-final-rerun.json"
FINAL_RERUN_AUDIT_KEY = "operations/real-pilot-final-rerun.json"
ROW_RERUN_KEY = "configuration/real-pilot-row-rerun.json"
ROW_RERUN_AUDIT_KEY = "operations/real-pilot-row-rerun.json"
FINAL_RERUN_FIELDS = {
    "schema_version", "approved", "approved_by", "approval_sha256", "batch_sha256",
    "ledger_sha256", "prior_recovery_sha256", "prior_execution_ids",
    "selected_item_keys", "item_sha256", "result_sha256", "cached_documents",
    "readiness_at", "operating_expires_at", "not_before", "expires_at",
}
ROW_RERUN_FIELDS = {
    "schema_version", "approved", "approved_by", "approval_sha256", "batch_sha256",
    "ledger_sha256", "prior_final_rerun_sha256", "prior_final_audit_sha256",
    "prior_execution_ids", "selected_item_keys", "item_sha256", "result_sha256",
    "cached_documents", "additional_input_tokens", "effective_input_ceiling",
    "max_requests", "max_output_tokens", "readiness_at", "operating_expires_at",
    "not_before", "expires_at",
}
RECOVERY_FIELDS = {
    "schema_version", "approved", "approved_by", "approval_sha256", "batch_sha256",
    "ledger_sha256", "not_before", "expires_at", "selected_item_keys",
    "interrupted_sha256", "cached_documents", "prior_execution_id",
}
HARD_LIMITS = {
    "products": 4,
    "executions": 2,
    "analysis": 2,
    "inference": 16,
    "search": 8,
    "web_retrieval": 12,
    "retrieval": 4,
    "analysis_pages": 10,
    "input_tokens": 200000,
    "output_tokens": 32768,
}
OPERATIONS = ("analysis", "inference", "search", "web_retrieval", "retrieval")
PRICE_KEYS = {"analysis_page", "input_token", "output_token", "search", "web_retrieval"}
EXTERNAL_OPERATIONS = {"search", "web_retrieval", "retrieval"}
INTERNAL_PRICE_KEYS = PRICE_KEYS - {"search", "web_retrieval"}
EXTERNAL_ENVIRONMENT_KEYS = {
    "WEBSEARCH_PROVIDER", "WEBIQ_ENDPOINT", "AI_FOUNDRY_PROJECT_ENDPOINT",
    "BING_CONNECTION_ID", "AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME",
}
ENVIRONMENT_KEYS = {
    "AZURE_CLIENT_ID",
    "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
    "LLM_ENDPOINT",
    "LLM_DEPLOYMENT",
    "AOAI_API_VERSION",
    "WEBSEARCH_PROVIDER",
    "WEBIQ_ENDPOINT",
    "AI_FOUNDRY_PROJECT_ENDPOINT",
    "BING_CONNECTION_ID",
    "AZURE_SEARCH_ENDPOINT",
    "AZURE_SEARCH_INDEX_NAME",
}
APPROVAL_FIELDS = {
    "schema_version", "approved", "id", "approved_by", "not_before", "expires_at",
    "batch_id", "owner", "batch_sha256", "customer_processing_approved",
    "identities", "environment", "limits", "unit_prices_usd",
}


def approval_scope(approval):
    scope = approval.get("execution_scope", "full")
    if scope not in ("full", "internal_only"):
        raise ValueError("Real-pilot execution_scope must be full or internal_only")
    return scope


def _sha256(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False,
        separators=(",", ":"),
    ).encode()).hexdigest()


def binding_digest(batch_record):
    """Bind complete selected items, sources, definitions, and workbook hashes."""
    return _sha256({key: batch_record[key] for key in (
        "id", "owner", "items", "input_hashes", "attribute_reference",
        "original_definitions", "product_count",
    )})


def _integer(value, name, *, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Invalid real-pilot {name}")
    return value


def _uuid(value, name):
    if not isinstance(value, str):
        raise ValueError(f"Invalid real-pilot {name}")
    try:
        valid = str(uuid.UUID(value))
    except ValueError:
        raise ValueError(f"Invalid real-pilot {name}") from None
    if valid != value:
        raise ValueError(f"Invalid real-pilot {name}")
    return value


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ValueError("Real-pilot timestamps must be timezone-aware") from None


def _now():
    return datetime.now(timezone.utc)


def _price(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,18})?", value):
        raise ValueError("Real-pilot prices must be positive decimal strings per unit")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid real-pilot unit price") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("Real-pilot unit prices must be positive")
    return parsed


def current_environment():
    """Resolve only nonsecret actual client settings, never request overrides."""
    from backend.core.config import settings

    result = {key: getattr(settings, key, None) for key in ENVIRONMENT_KEYS}
    result["AZURE_CLIENT_ID"] = os.environ.get("AZURE_CLIENT_ID")
    result["LLM_ENDPOINT"] = settings.LLM_ENDPOINT or settings.AI_FOUNDRY_ENDPOINT
    for key in ("AZURE_SEARCH_ENDPOINT", "AZURE_SEARCH_INDEX_NAME"):
        result[key] = os.environ.get(key) or result[key]
    return result


def validate_recovery(recovery, approval, batch, ledger, store):
    """Validate a proposed recovery against live bytes without any write methods."""
    class ReadOnlyCandidate:
        def read_bytes(self, key):
            if key == RECOVERY_KEY:
                return json.dumps(recovery).encode(), None
            return store.read_bytes(key)

    if (_sha256(read_json(store, APPROVAL_KEY)[0]) != _sha256(approval)
            or _sha256(read_json(store, BUDGET_KEY)[0]) != _sha256(ledger)
            or ledger.get("approval_sha256") != _sha256(approval)
            or ledger.get("invalidated")):
        raise ValueError("Recovery original approval or consumption binding changed")
    checker = RealPilotGuard.__new__(RealPilotGuard)
    checker.store, checker.batch = ReadOnlyCandidate(), copy.deepcopy(batch)
    try:
        checker._validate_approval(approval, verify_runtime=False)
        checker.verify_recovery(ledger)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Incomplete or malformed recovery prerequisite") from None
    return copy.deepcopy(checker.recovery)


def validate_final_rerun(amendment, approval, batch, ledger, store):
    """Read-only verification of the single result-preserving execution exception."""
    class ReadOnlyCandidate:
        def read_bytes(self, key):
            if key == FINAL_RERUN_KEY:
                return json.dumps(amendment).encode(), None
            return store.read_bytes(key)

    if (_sha256(read_json(store, APPROVAL_KEY)[0]) != _sha256(approval)
            or _sha256(read_json(store, BUDGET_KEY)[0]) != _sha256(ledger)
            or ledger.get("approval_sha256") != _sha256(approval)
            or ledger.get("invalidated")):
        raise ValueError("Final rerun original approval or consumption binding changed")
    checker = RealPilotGuard.__new__(RealPilotGuard)
    checker.store, checker.batch = ReadOnlyCandidate(), copy.deepcopy(batch)
    try:
        checker._validate_approval(approval, verify_runtime=False)
        checker.verify_recovery(ledger)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Incomplete or malformed final rerun prerequisite") from None
    return copy.deepcopy(checker.final_rerun)


def validate_row_rerun(amendment, approval, batch, ledger, store):
    """Verify the append-only row-rerun exception without changing any record."""
    class ReadOnlyCandidate:
        def read_bytes(self, key):
            if key == ROW_RERUN_KEY:
                return json.dumps(amendment).encode(), None
            return store.read_bytes(key)

    if (_sha256(read_json(store, APPROVAL_KEY)[0]) != _sha256(approval)
            or _sha256(read_json(store, BUDGET_KEY)[0]) != _sha256(ledger)
            or ledger.get("approval_sha256") != _sha256(approval)
            or ledger.get("invalidated")):
        raise ValueError("Row rerun original approval or consumption binding changed")
    checker = RealPilotGuard.__new__(RealPilotGuard)
    checker.store, checker.batch = ReadOnlyCandidate(), copy.deepcopy(batch)
    try:
        checker._validate_approval(approval, verify_runtime=False)
        checker.verify_recovery(ledger)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Incomplete or malformed row rerun prerequisite") from None
    return copy.deepcopy(checker.row_rerun)


class RealPilotGuard:
    def __init__(self, store, batch_record):
        self.store = store
        self.batch = copy.deepcopy(batch_record)
        self._mutex = threading.Lock()
        self._execution_id = None
        self._operation_keys = {}
        try:
            approval = self._validate()
        except (ValueError, Missing):
            self._invalidate_existing("authorization_or_binding_changed")
            raise
        self.approval_id = approval["id"]
        self.approval_sha256 = _sha256(approval)
        self.batch_sha256 = binding_digest(self.batch)
        self._approval = copy.deepcopy(approval)
        self.recovery_sha256 = _sha256(self.active_recovery)
        self._check_existing_binding(approval)

    @property
    def execution_scope(self):
        return approval_scope(self._approval)

    @property
    def active_recovery(self):
        return self.row_rerun or self.final_rerun or self.recovery

    def result_key(self, item_key):
        root = f"results/{self.batch['id']}/{item_key}"
        return f"{root}/attempts/{self.recovery_sha256}.json" if self.final_rerun else root + ".json"

    def _final_rerun(self, approval):
        try:
            raw, _ = self.store.read_bytes(FINAL_RERUN_KEY)
        except Missing:
            return None
        if len(raw) > 65536:
            raise ValueError("Final rerun exceeds its metadata bound")
        amendment = json.loads(raw)
        if not isinstance(amendment, dict) or set(amendment) != FINAL_RERUN_FIELDS:
            raise ValueError("Final rerun schema mismatch")
        if (self.recovery is None or type(amendment["schema_version"]) is not int
                or amendment["schema_version"] != 1 or amendment["approved"] is not True
                or amendment["approved_by"] != approval["approved_by"]
                or amendment["approval_sha256"] != _sha256(approval)
                or amendment["batch_sha256"] != approval["batch_sha256"]
                or amendment["prior_recovery_sha256"] != _sha256(self.recovery)
                or amendment["selected_item_keys"] != self.recovery["selected_item_keys"]
                or amendment["cached_documents"] != self.recovery["cached_documents"]
                or approval_scope(approval) != "internal_only"
                or approval["limits"]["executions"] != 2):
            raise ValueError("Final rerun must bind the original approval, recovery and selected cached workload")
        selected = set(amendment["selected_item_keys"])
        prior = amendment["prior_execution_ids"]
        if (not isinstance(prior, list) or len(prior) != 2 or len(set(prior)) != 2
                or not isinstance(amendment["item_sha256"], dict)
                or not isinstance(amendment["result_sha256"], dict)
                or set(amendment["item_sha256"]) != selected
                or set(amendment["result_sha256"]) != selected):
            raise ValueError("Final rerun requires two prior executions and exact item/result bindings")
        hashes = [amendment["ledger_sha256"], *prior,
                  *amendment["item_sha256"].values(), *amendment["result_sha256"].values()]
        if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes):
            raise ValueError("Final rerun requires exact SHA-256 bindings")
        ready = _timestamp(amendment["readiness_at"])
        deadline = _timestamp(amendment["operating_expires_at"])
        start, end = _timestamp(amendment["not_before"]), _timestamp(amendment["expires_at"])
        if ((deadline - ready).total_seconds() != 5400
                or not ready <= start < end <= deadline):
            raise ValueError("Final rerun activation must fit the readiness-anchored 90-minute operating window")
        return amendment

    def _row_rerun(self, approval):
        try:
            raw, _ = self.store.read_bytes(ROW_RERUN_KEY)
        except Missing:
            return None
        if len(raw) > 65536:
            raise ValueError("Row rerun exceeds its metadata bound")
        amendment = json.loads(raw)
        if not isinstance(amendment, dict) or set(amendment) != ROW_RERUN_FIELDS:
            raise ValueError("Row rerun schema mismatch")
        if (self.final_rerun is None or type(amendment["schema_version"]) is not int
                or amendment["schema_version"] != 1 or amendment["approved"] is not True
                or amendment["approved_by"] != approval["approved_by"]
                or amendment["approval_sha256"] != _sha256(approval)
                or amendment["batch_sha256"] != approval["batch_sha256"]
                or amendment["prior_final_rerun_sha256"] != _sha256(self.final_rerun)
                or amendment["selected_item_keys"] != self.final_rerun["selected_item_keys"]
                or amendment["cached_documents"] != self.final_rerun["cached_documents"]
                or approval_scope(approval) != "internal_only"
                or approval["limits"]["input_tokens"] != 200000
                or approval["limits"]["inference"] != 16
                or approval["limits"]["output_tokens"] != 32768):
            raise ValueError("Row rerun must preserve the original approval and selected cached workload")
        grants = {
            "additional_input_tokens": 57868, "effective_input_ceiling": 257868,
            "max_requests": 4, "max_output_tokens": 8192,
        }
        if any(type(amendment[name]) is not int or amendment[name] != value for name, value in grants.items()):
            raise ValueError("Row rerun requires exactly the authorized bounded exception")
        prior = amendment["prior_execution_ids"]
        if (not isinstance(prior, list) or len(prior) != 3 or len(set(prior)) != 3
                or not isinstance(amendment["item_sha256"], dict)
                or set(amendment["item_sha256"]) != set(amendment["selected_item_keys"])
                or not isinstance(amendment["result_sha256"], dict)):
            raise ValueError("Row rerun requires three consumed executions and exact history bindings")
        hashes = [amendment["ledger_sha256"], amendment["prior_final_audit_sha256"],
                  *prior, *amendment["item_sha256"].values(),
                  *(value for value in amendment["result_sha256"].values() if value is not None)]
        if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes):
            raise ValueError("Row rerun requires exact SHA-256 bindings")
        prior_audit, _ = read_json(self.store, FINAL_RERUN_AUDIT_KEY)
        if (_sha256(prior_audit) != amendment["prior_final_audit_sha256"]
                or prior_audit.get("amendment_sha256") != amendment["prior_final_rerun_sha256"]
                or prior_audit.get("execution_id") not in prior):
            raise ValueError("Row rerun prior final-attempt audit changed")
        ready, deadline = (_timestamp(amendment[name]) for name in ("readiness_at", "operating_expires_at"))
        start, end = (_timestamp(amendment[name]) for name in ("not_before", "expires_at"))
        if ((deadline - ready).total_seconds() != 5400
                or ready < _timestamp(self.final_rerun["operating_expires_at"])
                or not ready <= start < end <= deadline):
            raise ValueError("Row rerun must follow the prior window and fit its readiness-anchored 90 minutes")
        return amendment

    def _row_states(self):
        states, results = {}, {}
        amendment = self.row_rerun
        for item in self.batch["items"]:
            key = item["item_key"]
            raw, version = self.store.read_bytes(f"items/{self.batch['id']}/{key}.json")
            state = json.loads(raw)
            if key not in amendment["selected_item_keys"]:
                if state.get("state") != "deferred":
                    raise ValueError("Row rerun must leave other products deferred")
                continue
            if (hashlib.sha256(raw).hexdigest() != amendment["item_sha256"][key]
                    or state.get("state") not in {"unresolved", "interrupted"}
                    or state.get("recovery_sha256") != amendment["prior_final_rerun_sha256"]):
                raise ValueError("Row rerun requires the exact stopped, reconciled item history")
            if state["state"] == "interrupted":
                prior_audit, _ = read_json(self.store, FINAL_RERUN_AUDIT_KEY)
                execution = prior_audit["execution_id"]
                audit_key = f"operations/interrupted-reconciliation/{self.batch['id']}/{key}/{execution}.json"
                interruption = state.get("interruption", {})
                audit_raw, _ = self.store.read_bytes(audit_key)
                applied, _ = read_json(self.store, audit_key.removesuffix(".json") + ".applied.json")
                if (interruption.get("audit_key") != audit_key or interruption.get("execution_id") != execution
                        or applied.get("kind") != "interrupted_reconciliation_applied"
                        or applied.get("execution_id") != execution
                        or applied.get("audit_sha256") != _sha256(json.loads(audit_raw))
                        or applied.get("records", {}).get("item", {}).get("sha256") != hashlib.sha256(raw).hexdigest()):
                    raise ValueError("Row rerun requires the preserved applied stopped-state reconciliation")
            root = f"results/{self.batch['id']}/{key}"
            previous = state
            depth = 0
            while previous is not None:
                if not isinstance(previous, dict) or depth >= 32:
                    raise ValueError("Row rerun item history is malformed or exceeds its bound")
                result_key = previous.get("result_key") or root + ".json"
                if not isinstance(result_key, str) or not (
                    result_key == root + ".json"
                    or re.fullmatch(re.escape(root) + r"/attempts/[a-f0-9]{64}\.json", result_key)
                ):
                    raise ValueError("Row rerun history references an unrelated result")
                try:
                    result, _ = self.store.read_bytes(result_key)
                    results[result_key] = hashlib.sha256(result).hexdigest()
                except Missing:
                    results[result_key] = None
                previous = previous.get("previous_attempt")
                depth += 1
            future = f"{root}/attempts/{_sha256(amendment)}.json"
            try:
                self.store.read_bytes(future)
            except Missing:
                states[key] = (raw, version)
                continue
            raise Conflict("Row rerun result already exists; no automatic retry")
        if results != amendment["result_sha256"] or not any(value is not None for value in results.values()):
            raise ValueError("Row rerun immutable result history changed")
        return states

    def _final_states(self):
        states = {}
        amendment = self.final_rerun
        for item in self.batch["items"]:
            key = item["item_key"]
            raw, version = self.store.read_bytes(f"items/{self.batch['id']}/{key}.json")
            state = json.loads(raw)
            if key not in amendment["selected_item_keys"]:
                if state.get("state") != "deferred":
                    raise ValueError("Final rerun must leave other products deferred")
                continue
            if (hashlib.sha256(raw).hexdigest() != amendment["item_sha256"][key]
                    or state.get("state") != "unresolved" or state.get("result_key")):
                raise ValueError("Final rerun prior item history changed")
            result, _ = self.store.read_bytes(f"results/{self.batch['id']}/{key}.json")
            parsed = json.loads(result)
            if (hashlib.sha256(result).hexdigest() != amendment["result_sha256"][key]
                    or not parsed.get("extraction_error")
                    or any(attribute.get("candidates") for attribute in parsed["attributes"])):
                raise ValueError("Final rerun requires unchanged immutable failed results without proposals")
            try:
                reviews, _ = read_json(self.store, f"reviews/{self.batch['id']}/{key}.json")
            except Missing:
                reviews = []
            if reviews:
                raise ValueError("Final rerun cannot change the result associated with existing reviews")
            states[key] = (raw, version)
        return states

    def _recovery(self, approval):
        try:
            raw, _ = self.store.read_bytes(RECOVERY_KEY)
        except Missing:
            return None
        if len(raw) > 65536:
            raise ValueError("Real-pilot recovery exceeds its metadata bound")
        recovery = json.loads(raw)
        if not isinstance(recovery, dict) or set(recovery) != RECOVERY_FIELDS:
            raise ValueError("Real-pilot recovery schema mismatch")
        if (type(recovery["schema_version"]) is not int or recovery["schema_version"] != 1
                or recovery["approved"] is not True
                or recovery["approved_by"] != approval["approved_by"]
                or recovery["approval_sha256"] != _sha256(approval)
                or recovery["batch_sha256"] != approval["batch_sha256"]
                or approval_scope(approval) != "internal_only"):
            raise ValueError("Real-pilot recovery approval/batch/operator binding mismatch")
        selected = recovery["selected_item_keys"]
        if (not isinstance(selected, list) or len(selected) != 2
                or not all(isinstance(key, str) for key in selected)
                or len(set(selected)) != 2
                or not set(selected) <= {item["item_key"] for item in self.batch["items"]}):
            raise ValueError("Recovery requires exactly two existing selected items")
        interrupted = recovery["interrupted_sha256"]
        caches = recovery["cached_documents"]
        if (not isinstance(interrupted, dict) or set(interrupted) != set(selected)
                or not isinstance(caches, dict) or not 1 <= len(caches) <= 2
                or not all(re.fullmatch(r"parses/[a-f0-9]{64}\.json", key) for key in caches)):
            raise ValueError("Recovery must bind interrupted states and existing parse caches")
        hashes = [recovery["ledger_sha256"], recovery["prior_execution_id"],
                  *interrupted.values(), *caches.values()]
        if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes):
            raise ValueError("Recovery requires exact SHA-256 bindings")
        return recovery

    def check_recovery_item(self, item):
        if self.active_recovery is not None:
            selected = next((entry for entry in self.batch["items"]
                             if entry["item_key"] == item.get("item_key")), None)
            if (selected is None or _sha256(selected) != _sha256(item)
                    or item["item_key"] not in self.active_recovery["selected_item_keys"]):
                raise ValueError("Item is deferred or differs from the recovery binding")

    def check_recovery_cache(self, key):
        if self.active_recovery is not None:
            raw, _ = self.store.read_bytes(key)
            if self.active_recovery["cached_documents"].get(key) != hashlib.sha256(raw).hexdigest():
                raise ValueError("Recovery parse cache is missing, changed or not approved")

    def _recovery_states(self):
        states = {}
        for item in self.batch["items"]:
            key = item["item_key"]
            for prefix in ("results", "reviews"):
                try:
                    self.store.read_bytes(f"{prefix}/{self.batch['id']}/{key}.json")
                except Missing:
                    pass
                else:
                    raise ValueError("Recovery cannot overwrite existing machine results or reviews")
            path = f"items/{self.batch['id']}/{key}.json"
            try:
                raw, version = self.store.read_bytes(path)
            except Missing:
                if key in self.recovery["selected_item_keys"]:
                    raise ValueError("Recovery requires the original interrupted item state") from None
                states[key] = (None, None)
                continue
            if (key not in self.recovery["selected_item_keys"]
                    or hashlib.sha256(raw).hexdigest() != self.recovery["interrupted_sha256"][key]
                    or json.loads(raw).get("state") != "interrupted"):
                raise ValueError("Recovery item history changed or a deferred item was already attempted")
            states[key] = (raw, version)
        return states

    def verify_recovery(self, ledger):
        """Read-only preflight; no new allowance, recovery write or service call."""
        if self.row_rerun is not None:
            amendment = self.row_rerun
            if (_sha256(ledger) != amendment["ledger_sha256"]
                    or list(ledger["executions"]) != amendment["prior_execution_ids"]
                    or ledger["attempted"]["inference"] != 7
                    or ledger["reserved"]["input_tokens"] != 165722
                    or ledger["reserved"]["output_tokens"] != 14336
                    or ledger.get("final_rerun", {}).get("sha256") != amendment["prior_final_rerun_sha256"]
                    or ledger.get("row_rerun")):
                raise ValueError("Row rerun requires unchanged consumed reservations and prior executions")
            for key in amendment["cached_documents"]:
                self.check_recovery_cache(key)
            self._row_states()
            try:
                self.store.read_bytes(ROW_RERUN_AUDIT_KEY)
            except Missing:
                return
            raise Conflict("Row rerun already attempted; no automatic retry")
        if self.final_rerun is not None:
            amendment = self.final_rerun
            if (_sha256(ledger) != amendment["ledger_sha256"]
                    or list(ledger["executions"]) != amendment["prior_execution_ids"]
                    or ledger["attempted"]["inference"] != 4
                    or ledger["reserved"]["input_tokens"] != 92854
                    or ledger["reserved"]["output_tokens"] != 8192
                    or ledger.get("recovery", {}).get("sha256") != amendment["prior_recovery_sha256"]
                    or ledger.get("final_rerun")):
                raise ValueError("Final rerun requires the exact consumed ledger, without resetting reservations")
            prior_audit, _ = read_json(self.store, RECOVERY_AUDIT_KEY)
            if prior_audit.get("recovery_sha256") != amendment["prior_recovery_sha256"]:
                raise ValueError("Prior recovery audit binding changed")
            for key in amendment["cached_documents"]:
                self.check_recovery_cache(key)
            self._final_states()
            try:
                self.store.read_bytes(FINAL_RERUN_AUDIT_KEY)
            except Missing:
                return
            raise Conflict("Final rerun already attempted; no automatic retry")
        if self.recovery is None:
            return
        if (_sha256(ledger) != self.recovery["ledger_sha256"]
                or list(ledger["executions"]) != [self.recovery["prior_execution_id"]]
                or ledger["attempted"]["inference"] != 0
                or ledger["reserved"]["input_tokens"] != 0
                or ledger["reserved"]["output_tokens"] != 0):
            raise ValueError("Recovery requires the exact prior pre-inference consumption snapshot")
        for key in self.recovery["cached_documents"]:
            self.check_recovery_cache(key)
        self._recovery_states()
        try:
            self.store.read_bytes(RECOVERY_AUDIT_KEY)
        except Missing:
            return
        raise Conflict("Recovery was already attempted; no automatic retry")

    def prepare_recovery(self, fence):
        """Called under the batch lease, after charging the final worker slice."""
        if self.recovery is None:
            return
        with self._mutex, self.store.lease(BUDGET_KEY):
            _, ledger, _ = self._fresh_ledger()
            if self.row_rerun is not None:
                if ledger.get("row_rerun", {}).get("execution_id") != self._execution_id:
                    raise Conflict("Row rerun execution exception is not reserved")
                states = self._row_states()
                fence()
                write_json(self.store, ROW_RERUN_AUDIT_KEY, {
                    "amendment": self.row_rerun, "amendment_sha256": self.recovery_sha256,
                    "execution_id": self._execution_id, "recorded_at": _now().isoformat(),
                    "prior_states": {
                        key: {"base64": base64.b64encode(raw).decode(), "version": version}
                        for key, (raw, version) in states.items()
                    },
                    "prior_result_sha256": self.row_rerun["result_sha256"],
                    "prior_final_audit_sha256": self.row_rerun["prior_final_audit_sha256"],
                })
                for key, (raw, version) in states.items():
                    fence()
                    write_json(self.store, f"items/{self.batch['id']}/{key}.json", {
                        "id": key, "batch_id": self.batch["id"], "state": "recovery_ready",
                        "requested_mode": "real_pilot", "recovery_sha256": self.recovery_sha256,
                        "result_key": self.result_key(key), "previous_attempt": json.loads(raw),
                    }, version)
                return
            if self.final_rerun is not None:
                if ledger.get("final_rerun", {}).get("execution_id") != self._execution_id:
                    raise Conflict("Final rerun execution exception is not reserved")
                states = self._final_states()
                audit = {
                    "amendment": self.final_rerun, "amendment_sha256": self.recovery_sha256,
                    "execution_id": self._execution_id, "recorded_at": _now().isoformat(),
                    "prior_states": {
                        key: {"base64": base64.b64encode(raw).decode(), "version": version}
                        for key, (raw, version) in states.items()
                    },
                    "prior_result_sha256": self.final_rerun["result_sha256"],
                }
                fence()
                write_json(self.store, FINAL_RERUN_AUDIT_KEY, audit)
                for key, (raw, version) in states.items():
                    state = {
                        "id": key, "batch_id": self.batch["id"], "state": "recovery_ready",
                        "requested_mode": "real_pilot", "recovery_sha256": self.recovery_sha256,
                        "result_key": self.result_key(key), "previous_attempt": json.loads(raw),
                    }
                    fence()
                    write_json(self.store, f"items/{self.batch['id']}/{key}.json", state, version)
                return
            if ledger.get("recovery", {}).get("execution_id") != self._execution_id:
                raise Conflict("Recovery worker execution is not reserved")
            states = self._recovery_states()
            audit = {
                "recovery_sha256": self.recovery_sha256,
                "recovery": self.recovery, "execution_id": self._execution_id,
                "recorded_at": _now().isoformat(),
                "prior_states": {
                    key: {"base64": base64.b64encode(raw).decode(), "version": version}
                    for key, (raw, version) in states.items() if raw is not None
                },
            }
            fence()
            write_json(self.store, RECOVERY_AUDIT_KEY, audit)
            for item in self.batch["items"]:
                key = item["item_key"]
                raw, version = states[key]
                state = {
                    "id": key, "batch_id": self.batch["id"],
                    "recovery_sha256": self.recovery_sha256,
                    "requested_mode": "real_pilot",
                }
                if raw is not None:
                    state.update(state="recovery_ready", previous_attempt=json.loads(raw))
                else:
                    state.update(
                        state="deferred", deferred_at=_now().isoformat(),
                        error="Deferred by the two-product recovery scope; no enrichment attempted.",
                    )
                fence()
                write_json(self.store, f"items/{self.batch['id']}/{key}.json", state, version)

    def _invalidate_existing(self, reason):
        with self.store.lease(BUDGET_KEY):
            try:
                ledger, version = read_json(self.store, BUDGET_KEY)
            except Missing:
                return
            if not ledger.get("invalidated"):
                ledger["invalidated"] = reason
                write_json(self.store, BUDGET_KEY, ledger, version)

    def _validate(self, *, verify_runtime=False):
        if os.environ.get("DOCINTEL_REAL_PILOT_ENABLED", "false") != "true":
            raise ValueError("Real-pilot execution is disabled")
        try:
            approval, _ = read_json(self.store, APPROVAL_KEY)
        except Missing:
            raise ValueError("Explicit server-side real-pilot approval is required") from None
        try:
            self._validate_approval(approval, verify_runtime=verify_runtime)
        except (KeyError, TypeError, AttributeError):
            raise ValueError("Incomplete or malformed real-pilot approval or batch") from None
        return approval

    def _validate_approval(self, approval, *, verify_runtime):
        if (not isinstance(approval, dict) or not APPROVAL_FIELDS <= set(approval)
                or not set(approval) <= APPROVAL_FIELDS | {"execution_scope"}):
            raise ValueError("Real-pilot approval schema mismatch")
        scope = approval_scope(approval)
        if type(approval["schema_version"]) is not int or approval["schema_version"] != 1 or approval["approved"] is not True:
            raise ValueError("Explicit real-pilot approval is required")
        _uuid(approval["id"], "approval ID")
        operator = _uuid(approval["approved_by"], "operator ID")
        operators = os.environ.get("DOCINTEL_REAL_PILOT_OPERATOR_IDS", "").split(",")
        if operator not in {value.strip() for value in operators if value.strip()}:
            raise ValueError("Real-pilot approving operator is not server-verified")
        self.recovery = self._recovery(approval)
        self.final_rerun = self._final_rerun(approval)
        self.row_rerun = self._row_rerun(approval)
        window = self.active_recovery or approval
        original_start, original_end = _timestamp(approval["not_before"]), _timestamp(approval["expires_at"])
        if self.recovery is not None and not 0 < (original_end - original_start).total_seconds() <= 1200:
            raise ValueError("Original real-pilot approval window exceeds 1200 seconds")
        start, end = _timestamp(window["not_before"]), _timestamp(window["expires_at"])
        if self.recovery is not None and start < original_end:
            raise ValueError("Recovery window must follow the original approval window")
        if not 0 < (end - start).total_seconds() <= 1200 or not start <= _now() < end:
            raise ValueError("Real-pilot approval is not active or exceeds 1200 seconds")
        if approval["customer_processing_approved"] is not True:
            raise ValueError("Explicit customer processing approval is required in every scope")
        limits = approval["limits"]
        if not isinstance(limits, dict) or set(limits) != set(HARD_LIMITS) | {"spend_microdollars"}:
            raise ValueError("Real-pilot limits schema mismatch")
        for name, ceiling in HARD_LIMITS.items():
            _integer(limits[name], name, maximum=ceiling)
        for name in ("products", "executions", "spend_microdollars"):
            _integer(limits[name], name, minimum=1)
        if scope == "internal_only" and any(limits[name] != 0 for name in EXTERNAL_OPERATIONS):
            raise ValueError("Internal-only approval requires zero search, web_retrieval and retrieval budgets")
        prices = approval["unit_prices_usd"]
        required_prices = INTERNAL_PRICE_KEYS if scope == "internal_only" else PRICE_KEYS
        if not isinstance(prices, dict) or set(prices) != required_prices:
            raise ValueError("All conservative upper-bound unit prices are required")
        for price in prices.values():
            _price(price)
        self._validate_batch(approval)
        self._validate_environment(approval, verify_runtime=verify_runtime)

    def _validate_batch(self, approval):
        batch = self.batch
        if not re.fullmatch(r"[a-f0-9]{64}", batch["id"]) or not batch["owner"]:
            raise ValueError("Real-pilot requires an exact intake batch and owner")
        persisted, _ = read_json(self.store, f"batches/{batch['id']}.json")
        if (approval["batch_id"] != batch["id"] or approval["owner"] != batch["owner"]
                or approval["batch_sha256"] != binding_digest(batch)
                or binding_digest(persisted) != binding_digest(batch)):
            raise ValueError("Real-pilot batch/owner/items/input binding mismatch")
        if batch.get("valid") is not True or persisted.get("valid") is not True:
            raise ValueError("Real-pilot batch must pass intake validation")
        if persisted.get("mode") not in (None, "real_pilot"):
            raise ValueError("Real-pilot cannot authorize another execution mode")
        items = batch["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= approval["limits"]["products"]:
            raise ValueError("Real-pilot product limit exceeded")
        if type(batch["product_count"]) is not int or batch["product_count"] != len(items):
            raise ValueError("Real-pilot product count mismatch")
        if len({item["item_key"] for item in items}) != len(items):
            raise ValueError("Real-pilot item keys must be unique")
        identities = set()
        for item in items:
            if item.get("errors") or not item.get("manifest"):
                raise ValueError("Real-pilot cannot authorize invalid items")
            product = item["manifest"]["product"]
            identity = (product["item_id"], product["vendor"], product["mpn"])
            if identity in identities:
                raise ValueError("Real-pilot product identities must be unique")
            identities.add(identity)
        if not batch["input_hashes"] or not all(
            isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
            for value in batch["input_hashes"].values()
        ):
            raise ValueError("Real-pilot requires exact input hashes")
        if not {"manifest", "attributes"} <= set(batch["input_hashes"]):
            raise ValueError("Real-pilot requires both workbook input hashes")

    def _validate_environment(self, approval, *, verify_runtime):
        identities = approval["identities"]
        if not isinstance(identities, dict) or set(identities) != {"api_principal_id", "worker_principal_id"}:
            raise ValueError("Real-pilot must record separate API and worker principals")
        for name, value in identities.items():
            _uuid(value, name)
        expected = approval["environment"]
        if not isinstance(expected, dict) or not set(expected) <= ENVIRONMENT_KEYS:
            raise ValueError("Real-pilot environment contains unsupported settings")
        scope = approval_scope(approval)
        if scope == "internal_only" and set(expected) & EXTERNAL_ENVIRONMENT_KEYS:
            raise ValueError("Internal-only approval must omit external service environment settings")
        if verify_runtime and os.environ.get("DOCINTEL_REAL_PILOT_EXECUTION_SCOPE", scope) != scope:
            raise ValueError("Real-pilot runtime execution scope mismatch")
        required = {"AZURE_CLIENT_ID"}
        limits = approval["limits"]
        if limits["analysis"]:
            required.add("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        if limits["inference"]:
            required.update(("LLM_ENDPOINT", "LLM_DEPLOYMENT", "AOAI_API_VERSION"))
        if limits["search"] or limits["web_retrieval"]:
            required.add("WEBSEARCH_PROVIDER")
            provider = expected.get("WEBSEARCH_PROVIDER")
            if provider == "webiq":
                required.add("WEBIQ_ENDPOINT")
            elif provider == "bing":
                required.update(("AI_FOUNDRY_PROJECT_ENDPOINT", "LLM_DEPLOYMENT"))
            else:
                raise ValueError("Real-pilot web provider must be explicitly bound")
        if not required <= set(expected):
            raise ValueError("Real-pilot service/identity binding is incomplete")
        actual = current_environment() if verify_runtime else {}
        for name, value in expected.items():
            if name == "AZURE_CLIENT_ID" and value == "":
                if verify_runtime and actual.get(name) not in (None, ""):
                    raise ValueError("Real-pilot managed identity configuration mismatch")
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Real-pilot service/identity binding is incomplete")
            if verify_runtime and actual.get(name) != value:
                raise ValueError("Real-pilot service/identity configuration mismatch")
            if name.endswith("ENDPOINT"):
                parsed = urlsplit(value)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("Real-pilot endpoints must be HTTPS without credentials")
        if expected["AZURE_CLIENT_ID"]:
            _uuid(expected["AZURE_CLIENT_ID"], "managed identity")
        if verify_runtime and os.environ.get("DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID") != identities["worker_principal_id"]:
            raise ValueError("Real-pilot worker principal does not match deployment binding")

    def _check_existing_binding(self, approval):
        try:
            ledger, version = read_json(self.store, BUDGET_KEY)
        except Missing:
            return
        if ledger.get("approval_sha256") != _sha256(approval):
            if not ledger.get("invalidated"):
                ledger["invalidated"] = "approval_drift"
                write_json(self.store, BUDGET_KEY, ledger, version)
            raise ValueError("Real-pilot approval changed after first budget use")
        if ledger.get("invalidated"):
            raise ValueError("Real-pilot budget has been invalidated")
        if ledger.get("recovery") and ledger["recovery"]["sha256"] != _sha256(self.recovery):
            raise ValueError("Real-pilot recovery changed after budget use")
        if ledger.get("final_rerun") and ledger["final_rerun"]["sha256"] != _sha256(self.final_rerun):
            raise ValueError("Final rerun changed after budget use")
        if ledger.get("row_rerun") and ledger["row_rerun"]["sha256"] != _sha256(self.row_rerun):
            raise ValueError("Row rerun changed after budget use")

    def _fresh_ledger(self):
        try:
            ledger, version = read_json(self.store, BUDGET_KEY)
        except Missing:
            ledger, version = None, None
        try:
            approval = self._validate(verify_runtime=True)
            if _sha256(approval) != self.approval_sha256:
                raise ValueError("Real-pilot approval changed")
            if _sha256(self.active_recovery) != self.recovery_sha256:
                raise ValueError("Real-pilot recovery changed")
            self._check_existing_binding(approval)
        except (ValueError, Missing):
            if ledger is not None and not ledger.get("invalidated"):
                ledger["invalidated"] = "authorization_or_binding_changed"
                write_json(self.store, BUDGET_KEY, ledger, version)
            raise
        if ledger is None:
            if self.recovery is not None:
                raise ValueError("Recovery cannot create a fresh consumption ledger")
            ledger = {
                "schema_version": 1, "approval_id": self.approval_id,
                "execution_scope": self.execution_scope,
                "approval_sha256": self.approval_sha256,
                "approved_by": approval["approved_by"], "batch_id": self.batch["id"],
                "identities": copy.deepcopy(approval["identities"]),
                "owner": self.batch["owner"], "batch_sha256": self.batch_sha256,
                "not_before": approval["not_before"], "expires_at": approval["expires_at"],
                "created_at": _now().isoformat(), "invalidated": None,
                "attempted": {operation: 0 for operation in OPERATIONS},
                "reserved": {"input_tokens": 0, "output_tokens": 0, "analysis_pages": 0, "microdollars": 0},
                "actual_usage": {"input_tokens": 0, "output_tokens": 0, "analysis_pages": 0},
                "estimated_usage_cost_microdollars": 0, "actual_billed_microdollars": None,
                "cost_basis": "approved_upper_bound_prices_not_actual_billing",
                "executions": {}, "reservations": {},
            }
        return approval, ledger, version

    def before_execution(self, execution_key):
        """Charge a worker slice, even if it later crashes before doing paid work."""
        if not isinstance(execution_key, str) or not 1 <= len(execution_key) <= 256:
            raise ValueError("Explicit bounded worker execution key is required")
        key = _sha256({"approval_id": self.approval_id, "execution_key": execution_key})
        with self._mutex, self.store.lease(BUDGET_KEY):
            approval, ledger, version = self._fresh_ledger()
            persisted, _ = read_json(self.store, f"batches/{self.batch['id']}.json")
            if persisted.get("mode") != "real_pilot":
                raise ValueError("Worker must explicitly select real_pilot mode")
            if key in ledger["executions"]:
                raise Conflict("Real-pilot execution already attempted; no automatic retry")
            limit = approval["limits"]["executions"] + (1 if self.final_rerun else 0) + (1 if self.row_rerun else 0)
            if len(ledger["executions"]) >= limit:
                raise ValueError("Real-pilot execution budget exhausted")
            self.verify_recovery(ledger)
            if self.final_rerun and (_timestamp(self.active_recovery["expires_at"]) - _now()).total_seconds() < 600:
                raise ValueError("Final rerun requires the full 600-second execution window")
            ledger["executions"][key] = {"started_at": _now().isoformat()}
            if self.row_rerun is not None:
                ledger["row_rerun"] = {
                    "sha256": self.recovery_sha256, "execution_id": key,
                    "baseline_sha256": self.row_rerun["ledger_sha256"],
                    "inference_before": ledger["attempted"]["inference"],
                    "input_before": ledger["reserved"]["input_tokens"],
                    "output_before": ledger["reserved"]["output_tokens"],
                    "not_before": self.row_rerun["not_before"], "expires_at": self.row_rerun["expires_at"],
                    "selected_item_keys": self.row_rerun["selected_item_keys"],
                    "additional_execution_exception": 1, "additional_input_tokens": 57868,
                    "effective_input_ceiling": 257868,
                }
            elif self.final_rerun is not None:
                ledger["final_rerun"] = {
                    "sha256": self.recovery_sha256, "execution_id": key,
                    "baseline_sha256": self.final_rerun["ledger_sha256"],
                    "inference_before": ledger["attempted"]["inference"],
                    "not_before": self.final_rerun["not_before"], "expires_at": self.final_rerun["expires_at"],
                    "selected_item_keys": self.final_rerun["selected_item_keys"],
                    "additional_execution_exception": 1,
                }
            elif self.recovery is not None:
                ledger["recovery"] = {
                    "sha256": self.recovery_sha256, "execution_id": key,
                    "baseline_sha256": self.recovery["ledger_sha256"],
                    "not_before": self.recovery["not_before"], "expires_at": self.recovery["expires_at"],
                    "selected_item_keys": self.recovery["selected_item_keys"],
                }
            write_json(self.store, BUDGET_KEY, ledger, version)
            self._execution_id = key
        return key

    def operation_key(self, item, *, tier, source_version, prompt_version):
        """Bind an action to the exact approved item; never include a retry nonce."""
        self.check_recovery_item(item)
        selected = next((entry for entry in self.batch["items"] if entry["item_key"] == item.get("item_key")), None)
        if selected is None or _sha256(selected) != _sha256(item):
            raise ValueError("Real-pilot operation item is not approved")
        for value in (tier, source_version, prompt_version):
            if not isinstance(value, str) or not 1 <= len(value) <= 512:
                raise ValueError("Real-pilot operation needs tier/source/prompt version")
        key = _sha256({
            "approval_id": self.approval_id, "batch_sha256": self.batch_sha256,
            "item": selected, "tier": tier, "source_version": source_version,
            "prompt_version": prompt_version,
        })
        self._operation_keys[key] = item["item_key"]
        return key

    @staticmethod
    def _cost(approval, operation, input_tokens, output_tokens, analysis_pages):
        prices = {key: _price(value) for key, value in approval["unit_prices_usd"].items()}
        with localcontext() as context:
            context.prec = 60
            cost = prices["input_token"] * input_tokens + prices["output_token"] * output_tokens
            if operation == "analysis":
                cost += prices["analysis_page"] * analysis_pages
            elif operation in ("search", "web_retrieval"):
                cost += prices[operation]
            return int((cost * 1000000).to_integral_value(rounding=ROUND_CEILING))

    def reserve(self, operation, operation_key, *, item_key, max_input_tokens=0,
                max_output_tokens=0, analysis_pages=0):
        """Persist one conservative reservation before one non-retried paid call."""
        if operation not in OPERATIONS:
            raise ValueError("Unknown real-pilot operation")
        if not self._execution_id:
            raise ValueError("Real-pilot before_execution is required")
        if self._operation_keys.get(operation_key) != item_key:
            raise ValueError("Real-pilot operation key must bind an approved item")
        for name, value in (("input_tokens", max_input_tokens), ("output_tokens", max_output_tokens),
                            ("analysis_pages", analysis_pages)):
            _integer(value, name)
            if value > HARD_LIMITS[name]:
                raise RealPilotBudgetExceeded(name, value, HARD_LIMITS[name])
        if operation == "inference" and (max_input_tokens == 0 or max_output_tokens == 0):
            raise ValueError("Real-pilot inference requires server token upper bounds")
        if operation == "retrieval" and (max_input_tokens or max_output_tokens or analysis_pages):
            raise ValueError("Internal retrieval cannot authorize model or analysis units")
        if (operation == "analysis" and analysis_pages == 0) or (operation != "analysis" and analysis_pages != 0):
            raise ValueError("Real-pilot analysis requires an explicit page upper bound")
        key = _sha256({"operation": operation, "operation_key": operation_key})
        with self._mutex, self.store.lease(BUDGET_KEY):
            approval, ledger, version = self._fresh_ledger()
            if approval_scope(approval) == "internal_only" and operation in EXTERNAL_OPERATIONS:
                raise ValueError("External operations are forbidden by internal-only approval")
            if self.recovery is not None:
                if operation != "inference" or item_key not in self.recovery["selected_item_keys"]:
                    raise ValueError("Recovery permits only selected-item inference; new analysis is forbidden")
                if max_input_tokens + max_output_tokens > 30000:
                    raise RealPilotBudgetExceeded("recovery_request_tokens", max_input_tokens + max_output_tokens, 30000)
                active = "row_rerun" if self.row_rerun else "final_rerun"
                baseline = ledger.get(active, {}).get("inference_before", 0) if self.final_rerun else 0
                if ledger["attempted"]["inference"] >= baseline + 4:
                    raise RealPilotBudgetExceeded("recovery_inference", 1, 0)
            if self._execution_id not in ledger["executions"]:
                raise Conflict("Real-pilot worker execution is not reserved")
            if key in ledger["reservations"]:
                raise Conflict("Real-pilot operation already attempted; no automatic retry")
            limits = dict(approval["limits"])
            if self.row_rerun is not None:
                limits["input_tokens"] = self.row_rerun["effective_input_ceiling"]
                available_output = self.row_rerun["max_output_tokens"] - (
                    ledger["reserved"]["output_tokens"] - ledger["row_rerun"]["output_before"]
                )
                if max_output_tokens > available_output:
                    raise RealPilotBudgetExceeded("row_rerun_output_tokens", max_output_tokens, available_output)
            if ledger["attempted"][operation] >= limits[operation]:
                raise RealPilotBudgetExceeded(operation, 1, limits[operation] - ledger["attempted"][operation])
            usage = {"input_tokens": max_input_tokens, "output_tokens": max_output_tokens, "analysis_pages": analysis_pages}
            for name, value in usage.items():
                if ledger["reserved"][name] + value > limits[name]:
                    raise RealPilotBudgetExceeded(name, value, limits[name] - ledger["reserved"][name])
            cost = self._cost(approval, operation, **usage)
            if ledger["reserved"]["microdollars"] + cost > limits["spend_microdollars"]:
                raise RealPilotBudgetExceeded(
                    "spend_microdollars", cost, limits["spend_microdollars"] - ledger["reserved"]["microdollars"],
                )
            reservation = {
                "reservation_id": key, "approval_id": self.approval_id,
                "execution_id": self._execution_id, "operation": operation,
                "operation_key_sha256": operation_key, "item_key": item_key,
                "reserved_at": _now().isoformat(), "reserved_usage": usage,
                "reserved_microdollars": cost, "actual_usage": None,
                "estimated_usage_cost_microdollars": None,
                "status": "attempt_reserved_completion_unknown",
            }
            ledger["attempted"][operation] += 1
            for name, value in usage.items():
                ledger["reserved"][name] += value
            ledger["reserved"]["microdollars"] += cost
            ledger["reservations"][key] = reservation
            write_json(self.store, BUDGET_KEY, ledger, version)
        return copy.deepcopy(reservation)

    def record_usage(self, reservation_id, *, input_tokens=0, output_tokens=0, analysis_pages=0):
        """Record measured units, including late responses; never refund budget."""
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens, "analysis_pages": analysis_pages}
        for name, value in usage.items():
            _integer(value, name)
        with self._mutex, self.store.lease(BUDGET_KEY):
            ledger, version = read_json(self.store, BUDGET_KEY)
            if ledger.get("approval_sha256") != self.approval_sha256:
                raise Conflict("Real-pilot usage belongs to another approval")
            reservation = ledger["reservations"].get(reservation_id)
            if reservation is None:
                raise ValueError("Unknown real-pilot reservation")
            if reservation["actual_usage"] is not None:
                raise Conflict("Real-pilot actual usage was already recorded")
            overrun = any(usage[name] > reservation["reserved_usage"][name] for name in usage)
            cost = self._cost(self._approval, reservation["operation"], **usage)
            reservation.update(
                actual_usage=usage, estimated_usage_cost_microdollars=cost,
                recorded_at=_now().isoformat(), status="usage_reported_not_billing",
            )
            for name, value in usage.items():
                ledger["actual_usage"][name] += value
            ledger["estimated_usage_cost_microdollars"] += cost
            if overrun:
                ledger["invalidated"] = "actual_usage_exceeded_reservation"
            write_json(self.store, BUDGET_KEY, ledger, version)
            if overrun:
                raise ValueError("Real-pilot actual usage exceeded its reservation; budget invalidated")
        return copy.deepcopy(reservation)

    def metadata(self):
        """Return bounded accounting metadata, not customer content or billing."""
        ledger, _ = read_json(self.store, BUDGET_KEY)
        if ledger.get("approval_sha256") != self.approval_sha256:
            raise Conflict("Real-pilot budget belongs to another approval")
        output = {key: ledger[key] for key in (
            "approval_id", "approval_sha256", "approved_by", "batch_id", "identities",
            "batch_sha256", "not_before", "expires_at", "invalidated", "attempted",
            "reserved", "actual_usage", "estimated_usage_cost_microdollars",
            "actual_billed_microdollars", "cost_basis",
        )}
        output["execution_count"] = len(ledger["executions"])
        output["execution_scope"] = ledger.get("execution_scope", "full")
        output["unknown_usage_reservations"] = sum(
            reservation["actual_usage"] is None for reservation in ledger["reservations"].values()
        )
        output["usage_reporting_complete"] = output["unknown_usage_reservations"] == 0
        if ledger.get("recovery"):
            output["recovery"] = ledger["recovery"]
        if ledger.get("final_rerun"):
            output["final_rerun"] = ledger["final_rerun"]
        if ledger.get("row_rerun"):
            output["row_rerun"] = ledger["row_rerun"]
        return copy.deepcopy(output)
