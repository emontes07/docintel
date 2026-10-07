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
  web_retrieval (decimal strings per individual unit, never per 1M). All must
  be positive except web_retrieval, which may be zero for direct HTTPS pages.

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

The later fixed ``configuration/real-pilot-gapfill.json`` preserves that entire
lineage, including the row-rerun amendment. It adds exactly one execution and
+1 inference/+151646 input/+2048 output capacity to the retained consumed ledger.
Only the same two products, four internal and two optional web inferences, two
WebIQ discoveries and four direct pages fit its fresh $5 incremental envelope.
The original internal-only approval is never replaced; the effective full scope
exists only for this hash-bound, runtime-policy-bound append-only amendment.
"""

import base64
import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
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
GAPFILL_KEY = "configuration/real-pilot-gapfill.json"
GAPFILL_AUDIT_KEY = "operations/real-pilot-gapfill.json"
FOUR_PRODUCT_KEY = "configuration/real-pilot-four-product.json"
FOUR_PRODUCT_AUDIT_KEY = "operations/real-pilot-four-product.json"
FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS = 31
FOUR_PRODUCT_SLICES = [["row-2", "row-4"], ["row-3", "row-5"]]
FOUR_PRODUCT_SLICE_FIELDS = {"worker_slices", "slice_authorization", "slice_authorization_sha256"}
FOUR_PRODUCT_FIELDS = {
    "schema_version", "approved", "approved_by", "approval_sha256", "batch_sha256",
    "ledger_sha256", "prior_row_sha256", "prior_row_audit_sha256", "prior_execution_ids",
    "selected_item_keys", "item_sha256", "result_sha256", "cached_documents",
    "historical_records", "prep_receipt_key", "prep_receipt_sha256", "baseline",
    "capacity_approval", "public_web_policy", "web_environment", "web_prices",
    "page_limits", "execution_order", "inference_interval_seconds",
    "fixed_cost_microdollars", "readiness_at", "operating_expires_at",
    "not_before", "expires_at",
}
FOUR_PRODUCT_CAPACITY_FIELDS = {
    "approved", "approved_by", "approved_at", "receipt_id", "plan_sha256",
    "max_requests", "max_input_tokens", "max_output_tokens",
    "additional_executions", "additional_inference", "additional_input_tokens",
    "additional_output_tokens",
    "duration_assumptions",
}
FOUR_PRODUCT_PREP_PREFIX = (
    "operations/ford-analysis-preparation/"
    "b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa/"
)
FOUR_PRODUCT_PREP_CACHE_KEY = "parses/2d9239607c598350d1cdd3ca96ee84acc4e0eb5c2cf11ee5c312492b774c63f3.json"
FOUR_PRODUCT_PREP_RECORDS = tuple(FOUR_PRODUCT_PREP_PREFIX + name for name in (
    "attempt.json", "authorization.json", "ledger-before.json", "submitted.json",
    "parsed.json", "analysis-receipt.json", "completed.json", "reserved.json",
    "packet.json", "storage-program.json",
))
FOUR_PRODUCT_PREP_CONTINUATION_PREFIX = FOUR_PRODUCT_PREP_PREFIX + "continuation/"
FOUR_PRODUCT_PREP_CONTINUATION_RECORDS = tuple(FOUR_PRODUCT_PREP_CONTINUATION_PREFIX + name for name in (
    "attempt.json", "authorization.json", "plan.json", "failure-before.json", "program.json", "completed.json",
))
GAPFILL_GRANTS = {
    "additional_inference": 1, "additional_input_tokens": 151646,
    "additional_output_tokens": 2048, "max_requests": 6,
    "max_output_tokens": 12288, "incremental_microdollars": 5000000,
}
GAPFILL_FIELDS = {
    "schema_version", "approved", "approved_by", "approval_sha256", "batch_sha256",
    "ledger_sha256", "prior_row_sha256", "prior_row_audit_sha256", "prior_execution_ids",
    "selected_item_keys", "item_sha256", "result_sha256", "cached_documents",
    "readiness_at", "operating_expires_at", "not_before", "expires_at",
    "public_web_policy", "web_environment", "web_prices", "fixed_cost_microdollars",
    *GAPFILL_GRANTS,
}
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


def _price(value, *, allow_zero=False):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,18})?", value):
        raise ValueError("Real-pilot prices must be decimal strings per unit")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid real-pilot unit price") from None
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError("Real-pilot unit prices must be positive; only direct web_retrieval may be zero")
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


def validate_gapfill(amendment, approval, batch, ledger, store):
    """Read-only verification; never replaces an approval or resets consumption."""
    class ReadOnlyCandidate:
        def read_bytes(self, key):
            if key == GAPFILL_KEY:
                return json.dumps(amendment).encode(), None
            return store.read_bytes(key)

    if (read_json(store, APPROVAL_KEY)[0] != approval
            or read_json(store, BUDGET_KEY)[0] != ledger
            or ledger.get("approval_sha256") != _sha256(approval) or ledger.get("invalidated")):
        raise ValueError("Gapfill original approval or consumption binding changed")
    checker = RealPilotGuard.__new__(RealPilotGuard)
    checker.store, checker.batch = ReadOnlyCandidate(), copy.deepcopy(batch)
    try:
        checker._validate_approval(approval, verify_runtime=False)
        checker.verify_recovery(ledger)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Incomplete or malformed gapfill prerequisite") from None
    return copy.deepcopy(checker.gapfill)


def validate_four_product(amendment, approval, batch, ledger, store):
    """Verify a supplied exact capacity approval against the post-PREP baseline."""
    class ReadOnlyCandidate:
        def read_bytes(self, key):
            if key == FOUR_PRODUCT_KEY:
                return json.dumps(amendment).encode(), None
            return store.read_bytes(key)

    if (read_json(store, APPROVAL_KEY)[0] != approval
            or read_json(store, BUDGET_KEY)[0] != ledger
            or ledger.get("approval_sha256") != _sha256(approval) or ledger.get("invalidated")):
        raise ValueError("Four-product original approval or consumption binding changed")
    checker = RealPilotGuard.__new__(RealPilotGuard)
    checker.store, checker.batch = ReadOnlyCandidate(), copy.deepcopy(batch)
    try:
        checker._validate_approval(approval, verify_runtime=False)
        checker.verify_recovery(ledger)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Incomplete or malformed four-product prerequisite") from None
    return copy.deepcopy(checker.four_product)


def validate_four_product_slices(value, capacity, approved_by):
    """The extra execution is only the explicitly approved second distinct slice."""
    if "worker_slices" not in value:
        if FOUR_PRODUCT_SLICE_FIELDS & set(value):
            raise ValueError("Incomplete two-slice authority")
        return 1
    authority = value.get("slice_authorization")
    if (value["worker_slices"] != FOUR_PRODUCT_SLICES
            or not isinstance(authority, dict) or set(authority) != {
                "schema_version", "approved", "approved_by", "capacity_request_file", "plan_sha256",
                "additional_inference", "additional_input_tokens", "additional_output_tokens",
                "original_worker_seconds", "additional_worker_execution_authorized_only_for_two_slices",
                "additional_worker_seconds", "full_sequence_minimum_slack_seconds", "timing_fallback_order",
                "all_other_scope_and_limits_unchanged", "no_retry_authority", "readiness_clock_started",
                "user_confirmation",
            } or authority.get("schema_version") != 1 or type(authority.get("schema_version")) is not int
            or authority.get("approved") is not True or authority.get("approved_by") != approved_by
            or authority.get("plan_sha256") != capacity["plan_sha256"]
            or value.get("slice_authorization_sha256") != _sha256(authority)
            or authority.get("capacity_request_file") != "four-product-capacity-request-v1.json"
            or any(type(authority.get(key)) is not int or authority[key] != expected for key, expected in {
                "original_worker_seconds": 600, "additional_worker_seconds": 600,
                "additional_worker_execution_authorized_only_for_two_slices": 1,
                "full_sequence_minimum_slack_seconds": 90,
                **{key: capacity[key] for key in (
                    "additional_inference", "additional_input_tokens", "additional_output_tokens")},
            }.items())
            or authority.get("all_other_scope_and_limits_unchanged") is not True
            or authority.get("no_retry_authority") is not True or authority.get("readiness_clock_started") is not False
            or authority.get("timing_fallback_order") != [
                "reduce_start_spacing_only_where_service_rate_limits_allow",
                "two_worker_slices_if_guard_supports_the_second_bounded_execution",
                "all_four_internal_tiers_first_then_web_by_most_unresolved",
            ] or not isinstance(authority.get("user_confirmation"), str) or not authority["user_confirmation"]):
        raise ValueError("Two worker slices require the bound explicit additional-execution authority")
    return 2


def validate_four_product_duration(value):
    """An empirical, provider-latency-conditional forecast; never a token-rate estimate."""
    fields = {
        "basis", "deployment_tpm", "inference_interval_seconds", "worker_timeout_seconds",
        "historical_max_model_seconds", "model_response_allowance_seconds",
        "startup_bookkeeping_seconds", "web_network_allowance_seconds", "forecast_seconds",
        "provider_rate_estimate_verified", "byte_bounds_used_as_rate_estimate",
        "billing_usage_used_as_rate_estimate",
    }
    if (not isinstance(value, dict) or set(value) != fields
            or value["basis"] != "empirical_provider_latency_conditional"
            or value["deployment_tpm"] != 30000 or value["worker_timeout_seconds"] != 600
            or value["inference_interval_seconds"] != FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS
            or value["provider_rate_estimate_verified"] is not False
            or value["byte_bounds_used_as_rate_estimate"] is not False
            or value["billing_usage_used_as_rate_estimate"] is not False):
        raise ValueError("Four-product duration must remain empirical and conditional; no invented provider token estimate")
    for name in ("historical_max_model_seconds", "model_response_allowance_seconds",
                 "startup_bookkeeping_seconds", "web_network_allowance_seconds", "forecast_seconds"):
        if type(value[name]) not in (int, float) or not math.isfinite(value[name]):
            raise ValueError("Four-product duration assumptions must be explicit finite values")
    response = value["model_response_allowance_seconds"]
    forecast = (11 * max(FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS, response) + response
                + value["startup_bookkeeping_seconds"] + value["web_network_allowance_seconds"])
    if (value["historical_max_model_seconds"] != 21.42772 or response < 22
            or value["startup_bookkeeping_seconds"] < 35 or value["web_network_allowance_seconds"] < 150
            or value["forecast_seconds"] != forecast or forecast >= 600):
        raise ValueError("Four-product conditional duration forecast does not fit the full 600-second worker")
    return value


class RealPilotGuard:
    def __init__(self, store, batch_record):
        self.store = store
        self.batch = copy.deepcopy(batch_record)
        self._mutex = threading.Lock()
        self._execution_id = None
        self._operation_keys = {}
        self._last_gapfill_inference = None
        self.active_slice_index = None
        self.active_slice_item_keys = None
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
        return "full" if self.four_product or self.gapfill else approval_scope(self._approval)

    @property
    def active_recovery(self):
        return self.four_product or self.gapfill or self.row_rerun or self.final_rerun or self.recovery

    def result_key(self, item_key):
        root = f"results/{self.batch['id']}/{item_key}"
        return f"{root}/attempts/{self.recovery_sha256}.json" if self.final_rerun else root + ".json"

    def _four_product(self, approval):
        try:
            raw, _ = self.store.read_bytes(FOUR_PRODUCT_KEY)
        except Missing:
            return None
        if len(raw) > 262144:
            raise ValueError("Four-product amendment exceeds its metadata bound")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) not in (FOUR_PRODUCT_FIELDS, FOUR_PRODUCT_FIELDS | FOUR_PRODUCT_SLICE_FIELDS):
            raise ValueError("Four-product amendment schema mismatch")
        selected = [item["item_key"] for item in self.batch["items"]]
        if (self.row_rerun is None or self.gapfill is not None
                or value["schema_version"] != 1 or type(value["schema_version"]) is not int
                or value["approved"] is not True or value["approved_by"] != approval["approved_by"]
                or value["approval_sha256"] != _sha256(approval)
                or value["batch_sha256"] != binding_digest(self.batch)
                or value["prior_row_sha256"] != _sha256(self.row_rerun)
                or value["selected_item_keys"] != selected or len(selected) != 4):
            raise ValueError("Four-product continuation requires the unchanged four-item row lineage")
        if value["execution_order"] != [selected[0], selected[2], selected[1], selected[3]]:
            raise ValueError("Four-product execution must interleave manufacturers without splitting each product")
        interval = _integer(value["inference_interval_seconds"], "four-product inference interval")
        if interval != FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS:
            raise ValueError("Four-product pacing requires exactly 31-second starts; the old 61-second interval cannot fit")
        for name in ("item_sha256", "result_sha256", "cached_documents", "historical_records", "page_limits"):
            if not isinstance(value[name], dict):
                raise ValueError("Four-product canonical record bindings are required")
        if (value["prep_receipt_key"] != FOUR_PRODUCT_PREP_PREFIX + "completed.json"
                or any(not re.fullmatch(r"parses/[a-f0-9]{64}\.json", key) for key in value["cached_documents"])
                or any(not isinstance(key, str) or key.startswith(("/", "items/", "batches/", "locks/"))
                       or ".." in key.split("/") or key in {BUDGET_KEY, FOUR_PRODUCT_KEY, FOUR_PRODUCT_AUDIT_KEY}
                       for key in value["historical_records"])):
            raise ValueError("Four-product completed PREP and immutable record paths are required")
        required_history = {
            APPROVAL_KEY, RECOVERY_KEY, RECOVERY_AUDIT_KEY, FINAL_RERUN_KEY, FINAL_RERUN_AUDIT_KEY,
            ROW_RERUN_KEY, ROW_RERUN_AUDIT_KEY, value["prep_receipt_key"],
            *FOUR_PRODUCT_PREP_RECORDS,
            *FOUR_PRODUCT_PREP_CONTINUATION_RECORDS,
            *value["cached_documents"], *value["result_sha256"],
        }
        if not required_history <= set(value["historical_records"]):
            raise ValueError("Four-product entire prior authority/cache/result lineage must remain pinned")
        if (set(value["item_sha256"]) != set(selected)
                or not value["cached_documents"]
                or not set(self.row_rerun["cached_documents"]) <= set(value["cached_documents"])
                or set(value["page_limits"]) != set(selected)
                or value["page_limits"] != {
                    key: 2 if index < 2 else 1 for index, key in enumerate(value["execution_order"])}
                or any(type(limit) is not int for limit in value["page_limits"].values())):
            raise ValueError("Four-product caches and balanced six-page allocation are required")
        prior = value["prior_execution_ids"]
        if not isinstance(prior, list) or len(prior) != 4 or len(set(prior)) != 4:
            raise ValueError("Four-product continuation must retain four consumed executions")
        hashes = [value["ledger_sha256"], value["prior_row_audit_sha256"],
                  value["prep_receipt_sha256"], *prior, *value["item_sha256"].values(),
                  *value["result_sha256"].values(), *value["cached_documents"].values(),
                  *value["historical_records"].values()]
        if not all(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) for digest in hashes):
            raise ValueError("Four-product canonical SHA-256 bindings are required")
        audit, _ = read_json(self.store, ROW_RERUN_AUDIT_KEY)
        if (_sha256(audit) != value["prior_row_audit_sha256"]
                or audit.get("amendment_sha256") != value["prior_row_sha256"]
                or audit.get("execution_id") not in prior):
            raise ValueError("Four-product prior row audit changed")
        baseline = value["baseline"]
        if (not isinstance(baseline, dict) or set(baseline) != {"attempted", "reserved", "executions"}
                or baseline["executions"] != 4
                or baseline["attempted"]["inference"] != 11
                or any(baseline["attempted"][name] for name in EXTERNAL_OPERATIONS)
                or baseline["reserved"]["input_tokens"] != 257868
                or baseline["reserved"]["output_tokens"] != 22528):
            raise ValueError("Four-product post-PREP consumption changed")
        _integer(baseline["attempted"]["analysis"], "post-PREP analysis", maximum=approval["limits"]["analysis"])
        _integer(baseline["reserved"]["analysis_pages"], "post-PREP pages", maximum=approval["limits"]["analysis_pages"])
        capacity = value["capacity_approval"]
        if (not isinstance(capacity, dict) or set(capacity) != FOUR_PRODUCT_CAPACITY_FIELDS
                or capacity["approved"] is not True or capacity["approved_by"] != approval["approved_by"]
                or not isinstance(capacity["receipt_id"], str) or not 1 <= len(capacity["receipt_id"]) <= 256
                or not re.fullmatch(r"[a-f0-9]{64}", capacity["plan_sha256"])):
            raise ValueError("An explicit exact measured capacity exception is required")
        _timestamp(capacity["approved_at"])
        slice_count = validate_four_product_slices(value, capacity, approval["approved_by"])
        validate_four_product_duration(capacity["duration_assumptions"])
        for name in FOUR_PRODUCT_CAPACITY_FIELDS - {
                "approved", "approved_by", "approved_at", "receipt_id", "plan_sha256", "duration_assumptions"}:
            _integer(capacity[name], name)
        if (capacity["max_requests"] != 12 or capacity["max_output_tokens"] != 24576
                or not 1 <= capacity["max_input_tokens"] <= 8 * 27952 + 4 * 26000
                or capacity["additional_executions"] != slice_count
                or capacity["additional_inference"] != max(0, 12 - (
                    approval["limits"]["inference"] - baseline["attempted"]["inference"]))
                or capacity["additional_input_tokens"] != max(0, capacity["max_input_tokens"] - (
                    self.row_rerun["effective_input_ceiling"] - baseline["reserved"]["input_tokens"]))
                or capacity["additional_output_tokens"] != max(0, 24576 - (
                    approval["limits"]["output_tokens"] - baseline["reserved"]["output_tokens"]))):
            raise ValueError("Four-product capacity must be the exact measured additive exception")
        ready, deadline = (_timestamp(value[name]) for name in ("readiness_at", "operating_expires_at"))
        start, end = (_timestamp(value[name]) for name in ("not_before", "expires_at"))
        if ((deadline - ready).total_seconds() != 5400
                or ready < _timestamp(self.row_rerun["operating_expires_at"])
                or not ready <= start < end <= deadline
                or (slice_count == 2 and (end - start).total_seconds() > 1200)
                or _timestamp(capacity["approved_at"]) > ready):
            raise ValueError("Four-product readiness requires prior capacity approval and fixed 90 minutes")
        from backend.core.websearch_policy import OptionalWebPolicy
        from backend.core.websearch_webiq import ENDPOINT

        policy = OptionalWebPolicy.model_validate(value["public_web_policy"])
        if (policy.batch_sha256 != value["batch_sha256"] or set(policy.items) != set(selected)
                or (policy.max_search_calls, policy.max_direct_page_attempts, policy.max_inference_calls,
                    policy.max_input_tokens, policy.max_output_tokens) != (4, 6, 4, 26000, 2048)):
            raise ValueError("Four-product public policy must permit only four bounded optional tiers")
        for item in self.batch["items"]:
            public = policy.items[item["item_key"]]
            sources = {entry["source_id"]: entry for entry in item["sources"]
                       if entry["kind"] == "web" and entry["source_tier"] == "manufacturer_web"}
            if (public.max_direct_page_attempts != value["page_limits"][item["item_key"]]
                    or public.mpn != item["manifest"]["product"]["mpn"]
                    or public.manufacturer.casefold() not in item["manifest"]["product"]["vendor"].casefold()
                    or not set(public.source_ids) <= set(sources)
                    or not set(public.attribute_terms) <= {
                        entry["attribute_id"] for entry in item["manifest"]["attributes"]}
                    or set(public.allowed_hosts) != {
                        urlsplit(sources[key]["url"]).hostname for key in public.source_ids}):
                raise ValueError("Four-product public manufacturer/MPN/attribute scope changed")
        if value["web_environment"] != {"WEBSEARCH_PROVIDER": "webiq", "WEBIQ_ENDPOINT": ENDPOINT}:
            raise ValueError("Four-product continuation permits only the exact WebIQ /web endpoint")
        if not isinstance(value["web_prices"], dict) or set(value["web_prices"]) != {"search", "web_retrieval"}:
            raise ValueError("Explicit conservative four-product web prices are required")
        prices = {name: _price(price, allow_zero=name == "web_retrieval")
                  for name, price in value["web_prices"].items()}
        # Include the already-consumed 116.134448s build, rounded UP only once,
        # plus one new 2-vCPU/900s build and one 600s worker. No old forecasts.
        fixed = _integer(value["fixed_cost_microdollars"], "fixed cost", minimum=221227 + 18000 * (slice_count - 1))
        model = self._cost(approval, "inference", capacity["max_input_tokens"], 24576, 0)
        web = int(((4 * prices["search"] + 6 * prices["web_retrieval"]) * 1000000).to_integral_value(rounding=ROUND_CEILING))
        if (fixed + model + web > 5000000
                or policy.max_cost_microdollars < web + self._cost(approval, "inference", 104000, 8192, 0)
                or policy.max_cost_microdollars > 5000000 - fixed):
            raise ValueError("Four-product incremental envelope including prior build exceeds $5")
        return value

    def _verify_four_product_prep(self, ledger):
        value = self.four_product
        try:
            self.store.read_bytes(FOUR_PRODUCT_PREP_PREFIX + "failure.json")
        except Missing:
            pass
        else:
            raise ValueError("Four-product PREP failed or has an unknown outcome; no retry or activation")
        completed, _ = read_json(self.store, value["prep_receipt_key"])
        receipt, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "analysis-receipt.json")
        before, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "ledger-before.json")
        attempt, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "attempt.json")
        authorization, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "authorization.json")
        submitted, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "submitted.json")
        parsed, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "parsed.json")
        approval, _ = read_json(self.store, APPROVAL_KEY)
        if (completed.get("status") != "prepared_cache_verified_for_both_products"
                or completed.get("worker_executions_charged") != 0
                or completed.get("readiness_clock_started") is not False
                or completed.get("ford_extraction_authorized") is not False
                or completed.get("reserved_analysis") != 1 or completed.get("reserved_pages") != 5
                or completed.get("raw_hashes_are_capture_integrity_only") is not True
                or completed.get("ledger_after_canonical_sha256") != _sha256(ledger)
                or completed.get("analysis_receipt") != receipt
                or completed.get("cache_key") != FOUR_PRODUCT_PREP_CACHE_KEY
                or completed["cache_key"] not in value["cached_documents"]
                or receipt.get("outcome") != "succeeded" or receipt.get("sdk_retries") != 0
                or receipt.get("source_sha256") != FOUR_PRODUCT_PREP_PREFIX.split("/")[-2]
                or receipt.get("source") != "batchblob:///documents/av-source-4.pdf"
                or receipt.get("request_options") != {"pages": "1-5"}
                or receipt.get("fresh_analysis") is not True or receipt.get("legacy_local_parse_reused") is not False
                or receipt.get("readiness_clock_started") is not False
                or receipt.get("model_requested") != receipt.get("model_returned")
                or receipt.get("model_returned") != "prebuilt-layout"
                or receipt.get("api_version_requested") != receipt.get("api_version_returned")
                or receipt.get("api_version_returned") != "2024-11-30"
                or receipt.get("sdk_package") != "azure-ai-documentintelligence"
                or not isinstance(receipt.get("sdk_version"), str) or not receipt["sdk_version"]
                or receipt.get("sdk_result_serialization") != "AnalyzeResult.as_dict; sorted ASCII JSON; compact separators"
                or type(receipt.get("sdk_result_bytes")) is not int or receipt["sdk_result_bytes"] <= 0
                or type(receipt.get("source_bytes")) is not int or receipt["source_bytes"] <= 0
                or receipt.get("raw_http_response_retained") is not False
                or before["executions"] != ledger["executions"]):
            raise ValueError("Four-product requires actual bounded PREP provenance without worker execution")
        _uuid(receipt.get("operation_id"), "PREP operation")
        contract = authorization.get("contract")
        if (not isinstance(contract, dict)
                or any(contract.get(key) != expected for key, expected in {
                    "purpose": "one_ford_analysis_preparation_before_readiness",
                    "source_sha256": receipt["source_sha256"], "source_location": receipt["source"],
                    "model": "prebuilt-layout", "api_version": "2024-11-30",
                    "request_options": {"pages": "1-5"}, "max_submissions": 1,
                    "max_analysis_pages": 5, "sdk_retries": 0, "worker_executions": 0,
                    "inference": 0, "search": 0, "web_retrieval": 0,
                    "identity_route": "local_operator_di_api_mi_storage",
                }.items())):
            raise ValueError("Four-product PREP contract must authorize only the bounded shared Ford analysis")
        if (not isinstance(receipt.get("sdk_result_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", receipt["sdk_result_sha256"])
                or submitted.get("operation_id") != receipt["operation_id"]
                or submitted.get("source_sha256") != receipt["source_sha256"]
                or submitted.get("request_options") != receipt["request_options"]
                or authorization.get("approved") is not True or authorization.get("approved_by") != approval["approved_by"]
                or authorization.get("parent_live_executor_only") is not True
                or authorization.get("existing_operator_di_access_verified") is not True
                or authorization.get("operator_analysis_api_storage_approved") is not True
                or _sha256(authorization) != receipt.get("preparation_authorization_sha256")
                or attempt.get("authorization_sha256") != _sha256(authorization)
                or attempt.get("contract") != authorization.get("contract")
                or attempt.get("ledger_before_canonical_sha256") != _sha256(before)
                or attempt.get("source_bytes") != receipt["source_bytes"]
                or submitted.get("source_bytes") != receipt["source_bytes"]
                or attempt.get("reservation_id") != receipt.get("reservation_id")):
            raise ValueError("Four-product PREP authorization/submission/operation lineage changed")
        reservation = ledger["reservations"].get(receipt.get("reservation_id"), {})
        if (reservation.get("execution_id") is not None or reservation.get("operation") != "analysis"
                or reservation.get("reservation_id") != receipt["reservation_id"]
                or reservation.get("purpose") != "one_ford_analysis_preparation_before_readiness"
                or reservation.get("item_key") != "row-4" or reservation.get("item_keys") != ["row-4", "row-5"]
                or reservation.get("preparation_authorization_sha256") != _sha256(authorization)
                or reservation.get("reserved_usage") != {"input_tokens": 0, "output_tokens": 0, "analysis_pages": 5}
                or reservation.get("actual_usage") != {
                    "input_tokens": 0, "output_tokens": 0, "analysis_pages": receipt.get("actual_page_count")}
                or type(receipt.get("actual_page_count")) is not int
                or not 1 <= receipt["actual_page_count"] <= 5
                or receipt.get("returned_pages") != list(range(1, receipt["actual_page_count"] + 1))
                or any(ledger["reservations"].get(key) != prior for key, prior in before["reservations"].items())
                or set(ledger["reservations"]) != set(before["reservations"]) | {receipt["reservation_id"]}
                or ledger["attempted"] != {**before["attempted"], "analysis": before["attempted"]["analysis"] + 1}
                or ledger["reserved"] != {
                    **before["reserved"], "analysis_pages": before["reserved"]["analysis_pages"] + 5,
                    "microdollars": before["reserved"]["microdollars"] + reservation.get("reserved_microdollars", -1)}
                or completed.get("reserved_microdollars") != reservation.get("reserved_microdollars")
                or attempt.get("reserved_microdollars") != reservation.get("reserved_microdollars")
                or reservation.get("reserved_microdollars") != self._cost(approval, "analysis", 0, 0, 5)):
            raise ValueError("Four-product post-PREP ledger must retain every charge without refunds")
        measured_cost = self._cost(approval, "analysis", 0, 0, receipt["actual_page_count"])
        mutable = {"attempted", "reserved", "reservations", "actual_usage", "estimated_usage_cost_microdollars"}
        if (set(ledger) != set(before)
                or any(ledger.get(key) != prior for key, prior in before.items() if key not in mutable)
                or ledger["actual_usage"] != {
                    **before["actual_usage"],
                    "analysis_pages": before["actual_usage"]["analysis_pages"] + receipt["actual_page_count"]}
                or ledger["estimated_usage_cost_microdollars"] != before["estimated_usage_cost_microdollars"] + measured_cost):
            raise ValueError("Four-product PREP usage must append to every retained accounting field")
        self._verify_four_product_prep_identities(approval, authorization, attempt, receipt, submitted, before, ledger)
        cache, _ = read_json(self.store, completed["cache_key"])
        if (cache.get("analysis_receipt_key") != FOUR_PRODUCT_PREP_PREFIX + "analysis-receipt.json"
                or cache.get("origin") != "ford_preparation_analysis_first_five_pages"
                or cache.get("parser_version") != receipt.get("parser_version")
                or receipt.get("parser_version") != "prebuilt-layout:2024-11-30:mapping-v1"
                or completed.get("cache_canonical_sha256") != _sha256(cache)
                or cache.get("document_sha256") != receipt.get("mapped_result_sha256")
                or cache.get("document") != parsed or parsed.get("source") != receipt["source"]
                or cache.get("document", {}).get("cache_key") != "sha256:" + receipt["source_sha256"]):
            raise ValueError("Four-product PREP cache/provenance association changed")
        self._verify_four_product_prep_continuation(approval, completed, receipt, ledger)
        if value["fixed_cost_microdollars"] < 221227 + measured_cost + (18000 if value.get("worker_slices") else 0):
            raise ValueError("Four-product fixed forecast must include verified actual PREP page cost")

    def _verify_four_product_prep_continuation(self, approval, completed, receipt, ledger):
        prefix = FOUR_PRODUCT_PREP_CONTINUATION_PREFIX
        try:
            self.store.read_bytes(prefix + "failure.json")
        except Missing:
            pass
        else:
            raise ValueError("Four-product PREP continuation failed or is unknown; no retry")
        attempt, authorization, plan, failure, program, audit = (
            read_json(self.store, key)[0] for key in FOUR_PRODUCT_PREP_CONTINUATION_RECORDS
        )
        packet, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "packet.json")
        original_auth, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "authorization.json")
        claim, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "reserved.json")
        if (set(authorization) != {
                "schema_version", "approved", "approved_by", "approved_at", "continuation_plan_sha256",
                "original_packet_sha256", "reservation_id", "one_continuation_only",
                "no_new_reservation", "no_retry_or_refund", "parent_live_executor_only",
            } or type(authorization["schema_version"]) is not int or authorization["schema_version"] != 1
                or authorization["approved_by"] != approval["approved_by"]
                or any(authorization[key] is not True for key in (
                    "approved", "one_continuation_only", "no_new_reservation", "no_retry_or_refund", "parent_live_executor_only"))
                or authorization["continuation_plan_sha256"] != _sha256(plan)
                or authorization["original_packet_sha256"] != _sha256(packet)
                or authorization["reservation_id"] != receipt["reservation_id"]
                or plan.get("original_packet_sha256") != _sha256(packet)
                or plan.get("original_authorization_sha256") != _sha256(original_auth)
                or plan.get("claim_sha256") != _sha256(claim)
                or plan.get("reservation_id") != receipt["reservation_id"]
                or plan.get("purpose") != "one_continuation_of_existing_ford_reservation"
                or plan.get("max_submissions") != 1 or plan.get("sdk_retries") != 0
                or plan.get("new_reservations") != 0 or plan.get("new_reserved_microdollars") != 0):
            raise ValueError("Four-product requires the explicit one-use continuation of the existing PREP reservation")
        metadata = failure.get("analysis_metadata", {})
        if (failure != plan.get("failure") or failure.get("status") != "failed_or_unknown_no_retry"
                or failure.get("allowance_refunded") is not False or failure.get("submitted_transport_errors") != []
                or metadata.get("operation_id") is not None or "accepted_at" in metadata
                or metadata.get("error_type") != "ClientAuthenticationError"
                or metadata.get("source_sha256") != receipt["source_sha256"]
                or metadata.get("source_bytes") != receipt["source_bytes"]
                or metadata.get("request_options") != {"pages": "1-5"}
                or plan.get("history_hash_semantics") != "sha256_sorted_compact_ascii_json"
                or plan.get("history_sha256", {}).get(BUDGET_KEY) != claim["reserved_ledger_canonical_sha256"]):
            raise ValueError("The stopped pre-submission failure and charged ledger must remain preserved")
        for record in (attempt, audit):
            if (record.get("plan_sha256") != _sha256(plan)
                    or record.get("authorization_sha256") != _sha256(authorization)
                    or record.get("reservation_id") != receipt["reservation_id"]
                    or record.get("new_reservations") != 0 or record.get("new_reserved_microdollars") != 0
                    or record.get("readiness_clock_started") is not False):
                raise ValueError("Continuation must not reset, rearm, refund, or add a reservation")
        if (attempt.get("state") != "one_continuation_claimed_no_retry"
                or attempt.get("claim_sha256") != _sha256(claim)
                or attempt.get("ledger_canonical_sha256") != claim["reserved_ledger_canonical_sha256"]
                or program.get("sha256") != plan.get("continuation_program_sha256")
                or not isinstance(program.get("source"), str)
                or hashlib.sha256(program["source"].encode()).hexdigest() != program["sha256"]
                or audit.get("status") != "existing_reservation_continuation_completed"
                or audit.get("original_packet_sha256") != _sha256(packet)
                or audit.get("original_failure_canonical_sha256") != _sha256(failure)
                or audit.get("original_completed_canonical_sha256") != _sha256(completed)
                or audit.get("cache_canonical_sha256") != completed["cache_canonical_sha256"]
                or audit.get("ledger_after_canonical_sha256") != _sha256(ledger)
                or audit.get("operation_id") != receipt["operation_id"]
                or audit.get("worker_executions_charged") != 0):
            raise ValueError("Continuation program/completion must bind the actual unchanged PREP result")
        proof, ci = plan["native_no_send_proof"], plan["ci_proof"]
        if (not isinstance(proof, dict) or set(proof) != {
                "status", "provider", "sdk_package", "sdk_version", "transport_package", "transport_version",
                "method", "api_version", "endpoint_sha256", "body_sha256", "body_bytes", "content_type",
                "request_sha256", "authentication_performed", "provider_send_performed",
                "provider_response_fabricated", "source_sha256", "source_bytes", "transport",
                "transport_real_calls", "network_calls", "credential_real_calls", "captured_requests",
                "query", "query_is_complete", "query_sha256", "pages", "request_options", "path_sha256", "path", "model_id",
            } or proof.get("status") != "validated_no_send" or proof.get("provider") != "document_intelligence"
                or proof.get("sdk_package") != "azure-ai-documentintelligence"
                or proof.get("transport_package") != "azure-core" or proof.get("content_type") != "application/octet-stream"
                or any(not isinstance(proof[key], str) or not re.fullmatch(r"[0-9.]+[a-z0-9.]{0,20}", proof[key])
                       for key in ("sdk_version", "transport_version"))
                or proof.get("sdk_version") != receipt["sdk_version"] or proof.get("method") != "POST"
                or proof.get("api_version") != receipt["api_version_requested"]
                or proof.get("source_sha256") != receipt["source_sha256"]
                or proof.get("body_sha256") != receipt["source_sha256"]
                or proof.get("source_bytes") != receipt["source_bytes"] or proof.get("body_bytes") != receipt["source_bytes"]
                or proof.get("transport") != "in_memory_no_send"
                or type(proof.get("captured_requests")) is not int or proof["captured_requests"] != 1
                or proof.get("request_options") != {"pages": "1-5"} or proof.get("pages") != "1-5"
                or proof.get("query") != {"api-version": [receipt["api_version_requested"]], "pages": ["1-5"]}
                or proof.get("query_is_complete") is not True or proof.get("model_id") != "prebuilt-layout"
                or proof.get("path") != "/documentintelligence/documentModels/prebuilt-layout:analyze"
                or any(type(proof[key]) is not int or proof[key] != 0
                       for key in ("transport_real_calls", "network_calls", "credential_real_calls"))
                or any(proof.get(key) is not False for key in (
                    "authentication_performed", "provider_send_performed", "provider_response_fabricated"))
                or any(not isinstance(proof.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", proof[key])
                       for key in ("endpoint_sha256", "request_sha256", "path_sha256", "query_sha256"))
                or not isinstance(ci, dict) or set(ci) != {
                    "schema_version", "status", "source_revision", "code_sha256", "checks",
                } or type(ci["schema_version"]) is not int or ci["schema_version"] != 1
                or ci.get("status") != "passed" or ci.get("code_sha256") != plan.get("local_code_sha256")
                or not plan.get("local_code_sha256")
                or not re.fullmatch(r"[a-f0-9]{40}", ci.get("source_revision", ""))
                or not isinstance(ci["checks"], list) or not ci["checks"]
                or any(not isinstance(check, dict) or set(check) != {"name", "run_id", "conclusion"}
                       or check["conclusion"] != "success"
                       or not isinstance(check["name"], str) or not re.fullmatch(r"[\w ./()-]{1,120}", check["name"])
                       or type(check["run_id"]) is not int or check["run_id"] <= 0
                       for check in ci["checks"])):
            raise ValueError("Exact installed native DI no-send proof and focused CI are mandatory")
        times = [_timestamp(value) for value in (
            failure["recorded_at"], authorization["approved_at"], attempt["started_at"],
            receipt["started_at"], receipt["mapped_at"], audit["completed_at"],
            self.four_product["capacity_approval"]["approved_at"],
        )]
        if times != sorted(times):
            raise ValueError("Continuation authority must precede the one approved DI submission")

    def _verify_four_product_prep_identities(self, approval, authorization, attempt, receipt, submitted, before, ledger):
        packet, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "packet.json")
        claim, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "reserved.json")
        program, _ = read_json(self.store, FOUR_PRODUCT_PREP_PREFIX + "storage-program.json")
        if (set(authorization) != {
                "schema_version", "approved", "approved_by", "approved_at", "packet_sha256", "contract",
                "parent_live_executor_only", "existing_operator_di_access_verified",
                "operator_analysis_api_storage_approved",
            } or authorization["schema_version"] != 1 or type(authorization["schema_version"]) is not int
                or authorization["packet_sha256"] != _sha256(packet)
                or attempt.get("packet_sha256") != _sha256(packet)
                or claim.get("packet_sha256") != _sha256(packet)
                or claim.get("authorization_sha256") != _sha256(authorization)
                or packet.get("contract") != authorization["contract"]
                or packet.get("batch_id") != approval["batch_id"] or packet.get("approved_by") != approval["approved_by"]
                or packet.get("api_principal_id") != approval["identities"]["api_principal_id"]
                or packet.get("analysis_endpoint") != approval["environment"]["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]):
            raise ValueError("Four-product PREP split-route authorization/packet binding changed")
        tenant = _uuid(packet.get("tenant_id"), "PREP tenant")
        subscription = _uuid(packet.get("subscription_id"), "PREP subscription")
        analysis = {
            "kind": "approved_operator", "credential": "AzureCliCredential",
            "principal_id": approval["approved_by"], "tenant_id": tenant, "subscription_id": subscription,
        }
        storage = {
            "kind": "api_system_assigned", "credential": "ManagedIdentityCredential",
            "principal_id": approval["identities"]["api_principal_id"], "tenant_id": tenant,
        }
        actual = receipt.get("analysis_identity")
        if (packet.get("analysis_identity") != analysis or claim.get("analysis_identity") != analysis
                or packet.get("storage_identity") != storage or claim.get("storage_identity") != storage
                or receipt.get("storage_identity") != storage
                or not isinstance(actual, dict) or set(actual) != set(analysis) | {
                    "token_audience", "identity_checked_at", "sdk_token_identity_matched",
                    "token_signature_validated_locally", "token_stored",
                }
                or any(actual.get(key) != expected for key, expected in analysis.items())
                or actual["token_audience"] not in {
                    "https://cognitiveservices.azure.com", "https://cognitiveservices.azure.com/"}
                or actual["sdk_token_identity_matched"] is not True
                or actual["token_signature_validated_locally"] is not False
                or actual["token_stored"] is not False
                or submitted.get("analysis_identity") != actual):
            raise ValueError("Four-product PREP must retain operator DI and API-MI storage identity without token transfer")
        provenance = receipt.get("input_provenance")
        local = packet.get("local_pdf")
        if (not isinstance(provenance, dict) or set(provenance) != {
                "kind", "local_path", "blob_source", "sha256", "bytes", "blob_etag", "blob_verified_at",
            } or not isinstance(local, dict)
                or provenance["kind"] != "approved_local_copy_equal_to_verified_blob"
                or provenance["blob_source"] != receipt["source"] or provenance["sha256"] != receipt["source_sha256"]
                or provenance["local_path"] != local.get("path")
                or not isinstance(provenance["local_path"], str) or not 1 <= len(provenance["local_path"]) <= 4096
                or local.get("sha256") != receipt["source_sha256"]
                or provenance["bytes"] != local.get("bytes") or provenance["bytes"] != receipt["source_bytes"]
                or provenance["blob_etag"] != claim.get("source_etag")
                or provenance["blob_etag"] != attempt.get("source_etag")
                or not isinstance(provenance["blob_etag"], str)
                or not re.fullmatch(r'["A-Za-z0-9_-]{1,256}', provenance["blob_etag"])
                or provenance["blob_verified_at"] != claim.get("blob_verified_at")
                or claim.get("source_sha256") != receipt["source_sha256"]
                or claim.get("source_bytes") != receipt["source_bytes"]
                or claim.get("reservation_id") != receipt["reservation_id"]
                or claim.get("reserved_microdollars") != attempt["reserved_microdollars"]
                or claim.get("readiness_clock_started") is not False):
            raise ValueError("Four-product PREP local input must equal the API-verified approved blob")
        if (set(program) != {"source", "sha256"} or not isinstance(program["source"], str)
                or not 1 <= len(program["source"].encode()) <= 200000
                or hashlib.sha256(program["source"].encode()).hexdigest() != program["sha256"]
                or program["sha256"] != packet.get("storage_program_sha256")
                or packet.get("history_hash_semantics") != "sha256_sorted_compact_ascii_json"
                or attempt.get("historical_record_hash_semantics") != "sha256_sorted_compact_ascii_json"
                or attempt.get("historical_record_sha256") != packet.get("history_sha256")
                or packet.get("history_sha256", {}).get(BUDGET_KEY) != _sha256(before)
                or packet["history_sha256"].get(APPROVAL_KEY) != _sha256(approval)):
            raise ValueError("Four-product PREP storage-only program or canonical history pin changed")
        charged = copy.deepcopy(before)
        charged["attempted"] = ledger["attempted"]
        charged["reserved"] = ledger["reserved"]
        reservation = copy.deepcopy(ledger["reservations"][receipt["reservation_id"]])
        reservation.pop("recorded_at", None)
        reservation.update(actual_usage=None, estimated_usage_cost_microdollars=None,
                           status="attempt_reserved_completion_unknown")
        charged["reservations"][receipt["reservation_id"]] = reservation
        if claim.get("reserved_ledger_canonical_sha256") != _sha256(charged):
            raise ValueError("Four-product PREP must prove the append-only reservation preceded operator analysis")
        timestamps = [_timestamp(stamp) for stamp in (
            authorization["approved_at"], attempt["reserved_at"], provenance["blob_verified_at"],
            receipt["started_at"], receipt["accepted_at"],
            receipt["completed_at"], receipt["mapped_at"], self.four_product["capacity_approval"]["approved_at"],
            self.four_product["readiness_at"],
        )]
        if timestamps != sorted(timestamps):
            raise ValueError("PREP completion must precede measured capacity approval and readiness")
        if not _timestamp(receipt["started_at"]) <= _timestamp(actual["identity_checked_at"]) <= _timestamp(receipt["completed_at"]):
            raise ValueError("PREP SDK identity must be observed during the analysis operation")

    def _four_product_states(self):
        value, states, results = self.four_product, {}, {}
        for item_key in value["selected_item_keys"]:
            raw, version = self.store.read_bytes(f"items/{self.batch['id']}/{item_key}.json")
            state = json.loads(raw)
            if (_sha256(state) != value["item_sha256"][item_key]
                    or state.get("state") not in {"partial_draft", "unresolved", "completed", "deferred"}):
                raise ValueError("Four-product closed item history changed")
            root, previous = f"results/{self.batch['id']}/{item_key}", state
            for _ in range(32):
                if not isinstance(previous, dict):
                    raise ValueError("Four-product previous attempt is malformed")
                target = previous.get("result_key") or root + ".json"
                if not isinstance(target, str) or not (
                        target == root + ".json" or re.fullmatch(
                            re.escape(root) + r"/attempts/[a-f0-9]{64}\.json", target)):
                    raise ValueError("Four-product history references an unrelated result")
                try:
                    results[target] = _sha256(read_json(self.store, target)[0])
                except Missing:
                    pass
                previous = previous.get("previous_attempt")
                if previous is None:
                    break
            else:
                raise ValueError("Four-product previous attempts exceed the bound")
            try:
                self.store.read_bytes(f"{root}/attempts/{_sha256(value)}.json")
            except Missing:
                states[item_key] = raw, version
                continue
            raise Conflict("Four-product immutable result already exists; no retry")
        if results != value["result_sha256"]:
            raise ValueError("Four-product immutable result history changed")
        return states

    def _gapfill(self, approval):
        try:
            raw, _ = self.store.read_bytes(GAPFILL_KEY)
        except Missing:
            return None
        if len(raw) > 65536:
            raise ValueError("Gapfill metadata exceeds its bound")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != GAPFILL_FIELDS:
            raise ValueError("Gapfill amendment schema mismatch")
        if any(not isinstance(value[name], dict) for name in
               ("cached_documents", "item_sha256", "result_sha256", "web_prices")):
            raise ValueError("Gapfill requires complete canonical record and price bindings")
        if (self.row_rerun is None or value["approved"] is not True
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["approved_by"] != approval["approved_by"]
                or value["approval_sha256"] != _sha256(approval)
                or value["batch_sha256"] != binding_digest(self.batch)
                or value["prior_row_sha256"] != _sha256(self.row_rerun)
                or value["selected_item_keys"] != self.row_rerun["selected_item_keys"]
                or set(value["cached_documents"]) != set(self.row_rerun["cached_documents"])
                or len(value["selected_item_keys"]) != 2
                or any(type(value[name]) is not int or value[name] != expected
                       for name, expected in GAPFILL_GRANTS.items())):
            raise ValueError("Gapfill must preserve the exact prior scope and additive grants")
        prior = value["prior_execution_ids"]
        if (not isinstance(prior, list) or len(prior) != 4 or len(set(prior)) != 4
                or set(value["item_sha256"]) != set(value["selected_item_keys"])
                or not isinstance(value["result_sha256"], dict)):
            raise ValueError("Gapfill requires four consumed executions and exact history")
        hashes = [value["ledger_sha256"], value["prior_row_audit_sha256"], *prior,
                  *value["item_sha256"].values(), *value["result_sha256"].values(),
                  *value["cached_documents"].values()]
        if not all(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) for digest in hashes):
            raise ValueError("Gapfill requires canonical SHA-256 bindings")
        audit, _ = read_json(self.store, ROW_RERUN_AUDIT_KEY)
        if (_sha256(audit) != value["prior_row_audit_sha256"]
                or audit.get("amendment_sha256") != value["prior_row_sha256"]
                or audit.get("execution_id") not in prior):
            raise ValueError("Gapfill prior row audit changed")
        ready, deadline = (_timestamp(value[name]) for name in ("readiness_at", "operating_expires_at"))
        start, end = (_timestamp(value[name]) for name in ("not_before", "expires_at"))
        if ((deadline - ready).total_seconds() != 5400
                or ready < _timestamp(self.row_rerun["operating_expires_at"])
                or not ready <= start < end <= deadline):
            raise ValueError("Gapfill requires a new readiness-anchored 90-minute window")
        from backend.core.websearch_policy import OptionalWebPolicy

        policy = OptionalWebPolicy.model_validate(value["public_web_policy"])
        if (policy.batch_sha256 != value["batch_sha256"]
                or set(policy.items) != set(value["selected_item_keys"])
                or (policy.max_search_calls, policy.max_direct_page_attempts, policy.max_inference_calls,
                    policy.max_input_tokens, policy.max_output_tokens) != (2, 4, 2, 26000, 2048)):
            raise ValueError("Gapfill requires the exact two-item optional web policy")
        for item in self.batch["items"]:
            if item["item_key"] not in policy.items:
                continue
            public = policy.items[item["item_key"]]
            sources = {entry["source_id"]: entry for entry in item["sources"] if entry["kind"] == "web"}
            if (public.mpn != item["manifest"]["product"]["mpn"]
                    or public.manufacturer.casefold() != "mueller"
                    or not set(public.source_ids) <= set(sources)
                    or not set(public.attribute_terms) <= {
                        entry["attribute_id"] for entry in item["manifest"]["attributes"]}
                    or set(public.allowed_hosts) != {
                        urlsplit(sources[key]["url"]).hostname for key in public.source_ids}):
                raise ValueError("Gapfill public identities and exact source hosts changed")
        environment = value["web_environment"]
        if (not isinstance(environment, dict) or set(environment) != {"WEBSEARCH_PROVIDER", "WEBIQ_ENDPOINT"}
                or environment["WEBSEARCH_PROVIDER"] != "webiq"):
            raise ValueError("Gapfill permits only WebIQ /web discovery")
        from backend.core.websearch_webiq import ENDPOINT

        if environment["WEBIQ_ENDPOINT"] != ENDPOINT:
            raise ValueError("Gapfill requires the immutable adapter's exact WebIQ /web endpoint")
        if set(value["web_prices"]) != {"search", "web_retrieval"}:
            raise ValueError("Explicit conservative web unit prices are required")
        prices = {key: _price(price, allow_zero=key == "web_retrieval")
                  for key, price in value["web_prices"].items()}
        fixed = _integer(value["fixed_cost_microdollars"], "fixed cost", minimum=198000)
        # One 2-vCPU/900s build plus one 1-vCPU/2GiB/600s worker, no historical stacking.
        model = self._cost(approval, "inference", 151646, 12288, 0)
        web = int(((2 * prices["search"] + 4 * prices["web_retrieval"]) * 1000000).to_integral_value(rounding=ROUND_CEILING))
        if (fixed + model + web > 5000000 or policy.max_cost_microdollars < web + self._cost(
                approval, "inference", 52000, 4096, 0) or policy.max_cost_microdollars > 5000000 - fixed):
            raise ValueError("Gapfill conservative fresh incremental envelope exceeds $5")
        return value

    def _gapfill_states(self):
        value, states, results = self.gapfill, {}, {}
        for item in self.batch["items"]:
            key = item["item_key"]
            raw, version = self.store.read_bytes(f"items/{self.batch['id']}/{key}.json")
            state = json.loads(raw)
            if key not in value["selected_item_keys"]:
                if state.get("state") != "deferred":
                    raise ValueError("Gapfill must leave every other product deferred")
                continue
            if (_sha256(state) != value["item_sha256"][key]
                    or state.get("state") not in {"partial_draft", "unresolved", "completed"}
                    or state.get("recovery_sha256") != value["prior_row_sha256"]):
                raise ValueError("Gapfill requires exact closed row item history")
            root = f"results/{self.batch['id']}/{key}"
            previous, depth = state, 0
            while previous is not None:
                if not isinstance(previous, dict) or depth >= 32:
                    raise ValueError("Gapfill item history is malformed")
                target = previous.get("result_key") or root + ".json"
                if not (target == root + ".json" or re.fullmatch(
                        re.escape(root) + r"/attempts/[a-f0-9]{64}\.json", target)):
                    raise ValueError("Gapfill history references an unrelated result")
                try:
                    results[target] = _sha256(read_json(self.store, target)[0])
                except Missing:
                    pass
                previous, depth = previous.get("previous_attempt"), depth + 1
            try:
                self.store.read_bytes(f"{root}/attempts/{_sha256(value)}.json")
            except Missing:
                states[key] = raw, version
                continue
            raise Conflict("Gapfill result already exists; no retry")
        if not results or results != value["result_sha256"]:
            raise ValueError("Gapfill immutable result history changed")
        return states

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
            if (self.four_product and self.four_product.get("worker_slices")
                    and self.active_slice_item_keys is not None and item["item_key"] not in self.active_slice_item_keys):
                raise ValueError("Product belongs to a different immutable worker slice")

    def check_recovery_cache(self, key):
        if self.active_recovery is not None:
            raw, _ = self.store.read_bytes(key)
            digest = _sha256(json.loads(raw)) if self.four_product or self.gapfill else hashlib.sha256(raw).hexdigest()
            if self.active_recovery["cached_documents"].get(key) != digest:
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

    def slice_record_key(self, index, name):
        if index not in (0, 1) or name not in {"attempt", "prepared", "completed"}:
            raise ValueError("Unknown four-product slice audit")
        return f"operations/real-pilot-four-product-slices/{_sha256(self.four_product)}/slice-{index + 1}/{name}.json"

    def _verify_slice_terminal(self, index, *, ledger=None):
        value = self.four_product
        attempt, _ = read_json(self.store, self.slice_record_key(index, "attempt"))
        prepared, _ = read_json(self.store, self.slice_record_key(index, "prepared"))
        completed, _ = read_json(self.store, self.slice_record_key(index, "completed"))
        if (completed.get("status") != "succeeded"
                or completed.get("amendment_sha256") != _sha256(value)
                or completed.get("slice_index") != index
                or completed.get("item_keys") != value["worker_slices"][index]
                or completed.get("execution_id") != attempt.get("execution_id")
                or completed.get("attempt_sha256") != _sha256(attempt)
                or completed.get("prepared_sha256") != _sha256(prepared)
                or prepared.get("attempt_sha256") != _sha256(attempt)
                or completed.get("recovery_audit_sha256") != _sha256(read_json(self.store, FOUR_PRODUCT_AUDIT_KEY)[0])
                or (ledger is not None and completed.get("ledger") != ledger)
                or not _timestamp(attempt["started_at"]) <= _timestamp(completed["completed_at"])
                   <= _timestamp(attempt["started_at"]) + timedelta(seconds=600)):
            raise ValueError("Previous worker slice is not an immutable successful terminal execution")
        for key in value["worker_slices"][index]:
            state, _ = read_json(self.store, f"items/{self.batch['id']}/{key}.json")
            result, _ = read_json(self.store, self.result_key(key))
            if (state.get("state") not in {"completed", "unresolved"}
                    or state.get("recovery_sha256") != _sha256(value)
                    or completed["item_sha256"].get(key) != _sha256(state)
                    or completed["result_sha256"].get(self.result_key(key)) != _sha256(result)):
                raise ValueError("Completed worker slice item/result changed or failed")
        return completed

    def _verify_second_slice(self, ledger):
        value = self.four_product
        progress = ledger.get("four_product", {})
        slices = progress.get("slices")
        if (progress.get("sha256") != _sha256(value) or not isinstance(slices, list) or len(slices) != 1
                or slices[0].get("item_keys") != value["worker_slices"][0]):
            raise Conflict("Only the distinct second slice may follow the first; no retry or third execution")
        terminal = self._verify_slice_terminal(0, ledger=ledger)
        attempt, _ = read_json(self.store, self.slice_record_key(0, "attempt"))
        baseline = attempt["ledger_before"]
        if (_sha256(baseline) != value["ledger_sha256"]
                or set(ledger["executions"]) != set(value["prior_execution_ids"]) | {attempt["execution_id"]}
                or any(ledger["executions"].get(key) != record for key, record in baseline["executions"].items())
                or any(ledger["reservations"].get(key) != record for key, record in baseline["reservations"].items())):
            raise ValueError("Second slice must preserve all prior charged reservations and executions")
        for key, expected in value["historical_records"].items():
            if _sha256(read_json(self.store, key)[0]) != expected:
                raise ValueError("Second slice historical record changed")
        for key in value["cached_documents"]:
            self.check_recovery_cache(key)
        self._verify_four_product_prep(baseline)
        for key in value["worker_slices"][1]:
            state, _ = read_json(self.store, f"items/{self.batch['id']}/{key}.json")
            if (state.get("state") != "recovery_ready" or state.get("recovery_sha256") != _sha256(value)
                    or _sha256(state) != terminal["remaining_item_sha256"].get(key)
                    or _sha256(state.get("previous_attempt")) != value["item_sha256"][key]):
                raise ValueError("Second slice requires untouched ready items and original history")
            try:
                self.store.read_bytes(self.result_key(key))
            except Missing:
                pass
            else:
                raise Conflict("Second slice cannot repeat an existing result")

    def finish_slice(self):
        """Append worker-terminal success; the operator separately observes Azure success."""
        if not self.four_product or not self.four_product.get("worker_slices"):
            return None
        with self._mutex, self.store.lease(BUDGET_KEY):
            _, ledger, _ = self._fresh_ledger()
            index = self.active_slice_index
            if index not in (0, 1) or ledger["four_product"]["slices"][index]["execution_id"] != self._execution_id:
                raise Conflict("Worker slice was not admitted")
            attempt, _ = read_json(self.store, self.slice_record_key(index, "attempt"))
            prepared, _ = read_json(self.store, self.slice_record_key(index, "prepared"))
            if _now() > _timestamp(attempt["started_at"]) + timedelta(seconds=600):
                raise ValueError("Timed-out worker slice cannot authorize another execution")
            if index == 1:
                self._verify_slice_terminal(0)
                previous, _ = read_json(self.store, self.slice_record_key(0, "completed"))
                if any(ledger["reservations"].get(key) != record for key, record in previous["ledger"]["reservations"].items()):
                    raise ValueError("Second slice changed first-slice reservations")
            states, results, remaining = {}, {}, {}
            for key in self.four_product["execution_order"]:
                state, _ = read_json(self.store, f"items/{self.batch['id']}/{key}.json")
                if key in self.active_slice_item_keys:
                    if (state.get("state") not in {"completed", "unresolved"} or not state.get("finished_at")
                            or state.get("recovery_sha256") != self.recovery_sha256
                            or state.get("result_key") != self.result_key(key)):
                        raise ValueError("Failed or incomplete worker slice cannot authorize another execution")
                    states[key] = _sha256(state)
                    results[self.result_key(key)] = _sha256(read_json(self.store, self.result_key(key))[0])
                elif index == 0:
                    if state.get("state") != "recovery_ready":
                        raise ValueError("First slice touched a later product")
                    remaining[key] = _sha256(state)
            record = {
                "status": "succeeded", "amendment_sha256": self.recovery_sha256,
                "slice_index": index, "item_keys": list(self.active_slice_item_keys), "execution_id": self._execution_id,
                "attempt_sha256": _sha256(attempt), "prepared_sha256": _sha256(prepared),
                "recovery_audit_sha256": _sha256(read_json(self.store, FOUR_PRODUCT_AUDIT_KEY)[0]),
                "completed_at": _now().isoformat(), "item_sha256": states, "result_sha256": results,
                "remaining_item_sha256": remaining, "ledger": ledger,
            }
            write_json(self.store, self.slice_record_key(index, "completed"), record)
            return copy.deepcopy(record)

    def verify_recovery(self, ledger):
        """Read-only preflight; no new allowance, recovery write or service call."""
        if self.four_product is not None:
            value = self.four_product
            if value.get("worker_slices") and ledger.get("four_product"):
                self._verify_second_slice(ledger)
                return
            if (_sha256(ledger) != value["ledger_sha256"]
                    or set(ledger["executions"]) != set(value["prior_execution_ids"])
                    or {"executions": len(ledger["executions"]), "attempted": ledger["attempted"],
                        "reserved": ledger["reserved"]} != value["baseline"]
                    or ledger.get("row_rerun", {}).get("sha256") != value["prior_row_sha256"]
                    or ledger.get("four_product") or ledger.get("gapfill")):
                raise ValueError("Four-product continuation requires exact post-PREP consumption")
            for key in (GAPFILL_KEY, GAPFILL_AUDIT_KEY, FOUR_PRODUCT_AUDIT_KEY):
                try:
                    self.store.read_bytes(key)
                except Missing:
                    continue
                raise Conflict("Four-product prior or current authority already attempted; no retry")
            for key, expected in value["historical_records"].items():
                if _sha256(read_json(self.store, key)[0]) != expected:
                    raise ValueError("Four-product pinned historical record changed")
            if (value["historical_records"].get(value["prep_receipt_key"]) != value["prep_receipt_sha256"]
                    or _sha256(read_json(self.store, value["prep_receipt_key"])[0]) != value["prep_receipt_sha256"]):
                raise ValueError("Four-product completed PREP receipt changed")
            for key in value["cached_documents"]:
                self.check_recovery_cache(key)
            self._verify_four_product_prep(ledger)
            self._four_product_states()
            return
        if self.gapfill is not None:
            value = self.gapfill
            if (_sha256(ledger) != value["ledger_sha256"]
                    or set(ledger["executions"]) != set(value["prior_execution_ids"])
                    or ledger["attempted"]["inference"] != 11
                    or any(ledger["attempted"][name] for name in EXTERNAL_OPERATIONS)
                    or ledger["reserved"]["input_tokens"] != 257868
                    or ledger["reserved"]["output_tokens"] != 22528
                    or ledger.get("row_rerun", {}).get("sha256") != value["prior_row_sha256"]
                    or ledger.get("gapfill")):
                raise ValueError("Gapfill requires unchanged consumption and four prior executions")
            for key in value["cached_documents"]:
                self.check_recovery_cache(key)
            self._gapfill_states()
            try:
                self.store.read_bytes(GAPFILL_AUDIT_KEY)
            except Missing:
                return
            raise Conflict("Gapfill already attempted; no retry")
        if self.row_rerun is not None:
            amendment = self.row_rerun
            if (_sha256(ledger) != amendment["ledger_sha256"]
                    or set(ledger["executions"]) != set(amendment["prior_execution_ids"])
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
        if self.active_recovery is None:
            return
        with self._mutex, self.store.lease(BUDGET_KEY):
            _, ledger, _ = self._fresh_ledger()
            if self.four_product is not None:
                if self.four_product.get("worker_slices") and self.active_slice_index == 1:
                    self._verify_slice_terminal(0)
                    attempt, _ = read_json(self.store, self.slice_record_key(1, "attempt"))
                    if ledger["four_product"]["slices"][1]["execution_id"] != self._execution_id:
                        raise Conflict("Second slice is not reserved")
                    fence()
                    write_json(self.store, self.slice_record_key(1, "prepared"), {
                        "attempt_sha256": _sha256(attempt), "execution_id": self._execution_id,
                        "amendment_sha256": self.recovery_sha256, "slice_index": 1,
                    })
                    return
                if ledger.get("four_product", {}).get("execution_id") != self._execution_id:
                    raise Conflict("Four-product execution exception is not reserved")
                states = self._four_product_states()
                fence()
                write_json(self.store, FOUR_PRODUCT_AUDIT_KEY, {
                    "amendment": self.four_product, "amendment_sha256": self.recovery_sha256,
                    "execution_id": self._execution_id, "recorded_at": _now().isoformat(),
                    "prior_states": {
                        key: {"base64": base64.b64encode(raw).decode(), "version": version}
                        for key, (raw, version) in states.items()
                    },
                    "prior_result_sha256": self.four_product["result_sha256"],
                    "prior_row_audit_sha256": self.four_product["prior_row_audit_sha256"],
                    "prep_receipt_sha256": self.four_product["prep_receipt_sha256"],
                })
                for key, (raw, version) in states.items():
                    fence()
                    write_json(self.store, f"items/{self.batch['id']}/{key}.json", {
                        "id": key, "batch_id": self.batch["id"], "state": "recovery_ready",
                        "requested_mode": "real_pilot", "recovery_sha256": self.recovery_sha256,
                        "result_key": self.result_key(key), "previous_attempt": json.loads(raw),
                    }, version)
                if self.four_product.get("worker_slices"):
                    attempt, _ = read_json(self.store, self.slice_record_key(0, "attempt"))
                    write_json(self.store, self.slice_record_key(0, "prepared"), {
                        "attempt_sha256": _sha256(attempt), "execution_id": self._execution_id,
                        "amendment_sha256": self.recovery_sha256, "slice_index": 0,
                    })
                return
            if self.gapfill is not None:
                if ledger.get("gapfill", {}).get("execution_id") != self._execution_id:
                    raise Conflict("Gapfill execution exception is not reserved")
                states = self._gapfill_states()
                fence()
                write_json(self.store, GAPFILL_AUDIT_KEY, {
                    "amendment": self.gapfill, "amendment_sha256": self.recovery_sha256,
                    "execution_id": self._execution_id, "recorded_at": _now().isoformat(),
                    "prior_states": {
                        key: {"base64": base64.b64encode(raw).decode(), "version": version}
                        for key, (raw, version) in states.items()
                    },
                    "prior_result_sha256": self.gapfill["result_sha256"],
                    "prior_row_audit_sha256": self.gapfill["prior_row_audit_sha256"],
                })
                for key, (raw, version) in states.items():
                    fence()
                    write_json(self.store, f"items/{self.batch['id']}/{key}.json", {
                        "id": key, "batch_id": self.batch["id"], "state": "recovery_ready",
                        "requested_mode": "real_pilot", "recovery_sha256": self.recovery_sha256,
                        "result_key": self.result_key(key), "previous_attempt": json.loads(raw),
                    }, version)
                return
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
        self.gapfill = self._gapfill(approval)
        self.four_product = self._four_product(approval)
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
        for name, price in prices.items():
            _price(price, allow_zero=name == "web_retrieval")
        self._validate_batch(approval)
        self._validate_environment(approval, verify_runtime=verify_runtime)
        public_continuation = self.four_product or self.gapfill
        if public_continuation:
            from backend.core.websearch_policy import OptionalWebPolicy, configured_optional_web_policy

            if verify_runtime:
                policy = configured_optional_web_policy()
                expected_policy = OptionalWebPolicy.model_validate(public_continuation["public_web_policy"])
                if (policy is None or policy.model_dump(mode="json") != expected_policy.model_dump(mode="json")
                        or any(os.environ.get(key) != value for key, value in public_continuation["web_environment"].items())):
                    raise ValueError("Continuation runtime public policy or WebIQ endpoint changed")

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
        runtime_scope = "full" if self.four_product or self.gapfill else scope
        if verify_runtime and os.environ.get("DOCINTEL_REAL_PILOT_EXECUTION_SCOPE", runtime_scope) != runtime_scope:
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
        if ledger.get("gapfill") and ledger["gapfill"]["sha256"] != _sha256(self.gapfill):
            raise ValueError("Gapfill changed after budget use")
        if ledger.get("four_product") and ledger["four_product"]["sha256"] != _sha256(self.four_product):
            raise ValueError("Four-product continuation changed after budget use")

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
            if self.active_recovery is not None:
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
        if self.four_product:
            approval = copy.deepcopy(approval)
            value, capacity = self.four_product, self.four_product["capacity_approval"]
            baseline = value["baseline"]
            approval["execution_scope"] = "full"
            approval["unit_prices_usd"].update(value["web_prices"])
            approval["limits"].update(
                inference=baseline["attempted"]["inference"] + capacity["max_requests"],
                input_tokens=baseline["reserved"]["input_tokens"] + capacity["max_input_tokens"],
                output_tokens=baseline["reserved"]["output_tokens"] + capacity["max_output_tokens"],
                search=4, web_retrieval=6, retrieval=0,
                analysis=baseline["attempted"]["analysis"],
                analysis_pages=baseline["reserved"]["analysis_pages"],
                spend_microdollars=baseline["reserved"]["microdollars"] + 5000000 - value["fixed_cost_microdollars"],
            )
        elif self.gapfill:
            approval = copy.deepcopy(approval)
            approval["execution_scope"] = "full"
            approval["unit_prices_usd"].update(self.gapfill["web_prices"])
            approval["limits"].update(inference=17, input_tokens=409514, output_tokens=34816,
                                      search=2, web_retrieval=4, analysis=0, retrieval=0)
            baseline = ledger.get("gapfill", {}).get("cost_before", ledger["reserved"]["microdollars"])
            approval["limits"]["spend_microdollars"] = baseline + 5000000 - self.gapfill["fixed_cost_microdollars"]
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
            limit = (approval["limits"]["executions"] + bool(self.final_rerun)
                     + bool(self.row_rerun) + bool(self.gapfill)
                     + (self.four_product["capacity_approval"]["additional_executions"] if self.four_product else 0))
            if len(ledger["executions"]) >= limit:
                raise ValueError("Real-pilot execution budget exhausted")
            self.verify_recovery(ledger)
            if self.final_rerun and (_timestamp(self.active_recovery["expires_at"]) - _now()).total_seconds() < 600:
                raise ValueError("Final rerun requires the full 600-second execution window")
            started = _now().isoformat()
            sliced = self.four_product and self.four_product.get("worker_slices")
            if sliced:
                index = len(ledger.get("four_product", {}).get("slices", []))
                if index not in (0, 1):
                    raise Conflict("No third worker slice is authorized")
                attempt = {
                    "amendment_sha256": self.recovery_sha256, "slice_index": index,
                    "item_keys": sliced[index], "execution_id": key, "started_at": started,
                    "ledger_before": copy.deepcopy(ledger),
                }
                write_json(self.store, self.slice_record_key(index, "attempt"), attempt)
                self.active_slice_index, self.active_slice_item_keys = index, list(sliced[index])
            ledger["executions"][key] = {"started_at": started}
            if self.four_product is not None:
                if not ledger.get("four_product"):
                    ledger["four_product"] = {
                        "sha256": self.recovery_sha256, "execution_id": key,
                        "baseline_sha256": self.four_product["ledger_sha256"],
                        "inference_before": ledger["attempted"]["inference"],
                        "input_before": ledger["reserved"]["input_tokens"],
                        "output_before": ledger["reserved"]["output_tokens"],
                        "analysis_before": ledger["attempted"]["analysis"],
                        "analysis_pages_before": ledger["reserved"]["analysis_pages"],
                        "cost_before": ledger["reserved"]["microdollars"],
                        "not_before": self.four_product["not_before"], "expires_at": self.four_product["expires_at"],
                        "selected_item_keys": self.four_product["selected_item_keys"],
                        "capacity_approval": self.four_product["capacity_approval"],
                        "prep_receipt_sha256": self.four_product["prep_receipt_sha256"],
                    }
                if sliced:
                    ledger["four_product"].setdefault("slices", []).append({
                        "slice_index": index, "item_keys": list(sliced[index]), "execution_id": key,
                        "attempt_sha256": _sha256(attempt),
                    })
            elif self.gapfill is not None:
                ledger["gapfill"] = {
                    "sha256": self.recovery_sha256, "execution_id": key,
                    "baseline_sha256": self.gapfill["ledger_sha256"],
                    "inference_before": 11, "input_before": 257868, "output_before": 22528,
                    "cost_before": ledger["reserved"]["microdollars"],
                    "not_before": self.gapfill["not_before"], "expires_at": self.gapfill["expires_at"],
                    "selected_item_keys": self.gapfill["selected_item_keys"],
                    "additional_execution_exception": 1, **GAPFILL_GRANTS,
                }
            elif self.row_rerun is not None:
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
        prices = {key: _price(value, allow_zero=key == "web_retrieval")
                  for key, value in approval["unit_prices_usd"].items()}
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
            gapfill_tier = None
            if approval_scope(approval) == "internal_only" and operation in EXTERNAL_OPERATIONS:
                raise ValueError("External operations are forbidden by internal-only approval")
            continuation = self.four_product or self.gapfill
            if continuation is not None:
                if operation not in {"inference", "search", "web_retrieval"} or item_key not in continuation["selected_item_keys"]:
                    raise ValueError("Gapfill forbids analysis, internal retrieval and other products")
                if self.four_product and self.four_product.get("worker_slices"):
                    if item_key not in (self.active_slice_item_keys or []):
                        raise ValueError("Reservation belongs to another worker slice")
                    if (ledger["four_product"]["slices"][-1]["execution_id"] != self._execution_id
                            or _now() > _timestamp(ledger["executions"][self._execution_id]["started_at"]) + timedelta(seconds=600)):
                        raise ValueError("Worker slice is no longer active")
                    try:
                        self.store.read_bytes(self.slice_record_key(self.active_slice_index, "completed"))
                    except Missing:
                        pass
                    else:
                        raise Conflict("Completed worker slice cannot reserve more work")
                previous = [entry for entry in ledger["reservations"].values()
                            if entry["execution_id"] == self._execution_id and entry["item_key"] == item_key]
                counts = {name: sum(entry["operation"] == name for entry in previous)
                          for name in ("inference", "search", "web_retrieval")}
                page_limit = self.four_product["page_limits"][item_key] if self.four_product else 2
                maximum = {"inference": 3, "search": 1, "web_retrieval": page_limit}[operation]
                if counts[operation] >= maximum:
                    raise RealPilotBudgetExceeded("gapfill_item_" + operation, 1, 0)
                if operation == "inference":
                    gapfill_tier = "web" if counts["search"] else "internal"
                    used = sum(entry.get("gapfill_tier") == gapfill_tier for entry in previous)
                    if used >= (1 if gapfill_tier == "web" else 2):
                        raise RealPilotBudgetExceeded("gapfill_" + gapfill_tier + "_inference", 1, 0)
                    if (max_output_tokens != 2048 or max_input_tokens + max_output_tokens > 30000
                            or max_input_tokens > (26000 if counts["search"] else 27952)
                            or (not counts["search"] and counts["inference"] >= 2)):
                        raise ValueError("Gapfill inference exceeds complete request or per-tier bounds")
                elif max_input_tokens or max_output_tokens:
                    raise ValueError("Web operations cannot reserve model tokens")
                if operation == "web_retrieval" and not counts["search"]:
                    raise ValueError("Direct pages require the selected item's bounded WebIQ discovery")
            elif self.recovery is not None:
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
            if self.row_rerun is not None and continuation is None:
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
            if gapfill_tier is not None:
                reservation["gapfill_tier"] = gapfill_tier
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
        if ledger.get("gapfill"):
            output["gapfill"] = ledger["gapfill"]
        if ledger.get("four_product"):
            output["four_product"] = ledger["four_product"]
        return copy.deepcopy(output)
