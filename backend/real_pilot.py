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
"""

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


APPROVAL_KEY = "configuration/real-pilot-approval.json"
BUDGET_KEY = "budgets/real-pilot.json"
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
        self._check_existing_binding(approval)

    @property
    def execution_scope(self):
        return approval_scope(self._approval)

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
        start, end = _timestamp(approval["not_before"]), _timestamp(approval["expires_at"])
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

    def _fresh_ledger(self):
        try:
            ledger, version = read_json(self.store, BUDGET_KEY)
        except Missing:
            ledger, version = None, None
        try:
            approval = self._validate(verify_runtime=True)
            if _sha256(approval) != self.approval_sha256:
                raise ValueError("Real-pilot approval changed")
            self._check_existing_binding(approval)
        except (ValueError, Missing):
            if ledger is not None and not ledger.get("invalidated"):
                ledger["invalidated"] = "authorization_or_binding_changed"
                write_json(self.store, BUDGET_KEY, ledger, version)
            raise
        if ledger is None:
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
            if len(ledger["executions"]) >= approval["limits"]["executions"]:
                raise ValueError("Real-pilot execution budget exhausted")
            ledger["executions"][key] = {"started_at": _now().isoformat()}
            write_json(self.store, BUDGET_KEY, ledger, version)
            self._execution_id = key
        return key

    def operation_key(self, item, *, tier, source_version, prompt_version):
        """Bind an action to the exact approved item; never include a retry nonce."""
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
            _integer(value, name, maximum=HARD_LIMITS[name])
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
            if self._execution_id not in ledger["executions"]:
                raise Conflict("Real-pilot worker execution is not reserved")
            if key in ledger["reservations"]:
                raise Conflict("Real-pilot operation already attempted; no automatic retry")
            limits = approval["limits"]
            if ledger["attempted"][operation] >= limits[operation]:
                raise ValueError("Real-pilot operation budget exhausted")
            usage = {"input_tokens": max_input_tokens, "output_tokens": max_output_tokens, "analysis_pages": analysis_pages}
            for name, value in usage.items():
                if ledger["reserved"][name] + value > limits[name]:
                    raise ValueError(f"Real-pilot {name} budget exhausted")
            cost = self._cost(approval, operation, **usage)
            if ledger["reserved"]["microdollars"] + cost > limits["spend_microdollars"]:
                raise ValueError("Real-pilot approved spend ceiling exhausted")
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
        return copy.deepcopy(output)
