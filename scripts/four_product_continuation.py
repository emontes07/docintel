"""Append-only four-product operator; invoke with ``python -m scripts.four_product_continuation``.

Local planning never reads credentials, starts readiness, stages source, or calls
Azure. Paid actions are separate explicit single-use commands after reviewed
committed source, merged CI, actual PREP caches and the private worker gate exist.
"""

import argparse
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import inspect
from importlib.metadata import version as installed_version
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import ssl
from time import monotonic, sleep
import tomllib
from urllib.parse import urlsplit
import uuid

from scripts import release, pilot_continuation as prior, row_continuation as row
from scripts import gapfill_continuation as gap


PREFIX, CHILD = "four-product", "four-product-v1"
OWNER_AUTHORIZATION_FILE = "four-product-owner-authorization-20261006T201622Z.json"
PREPARATION_IDENTITY_AUTHORIZATION_FILE = "four-product-preparation-identity-authorization-20261006T221236Z.json"
EXISTING_ROLE_EVIDENCE_FILE = "four-product-existing-role-evidence-20261006.json"
CONTINUATION_OWNER_AUTHORIZATION_FILE = "ford-continuation-owner-authorization-20261006T233051Z.json"
CAPACITY_OWNER_AUTHORIZATION_FILE = "four-product-capacity-owner-approval-v1.json"
PREPARATION_STOPPED_FILE = "ford-preparation-stopped-outcome.json"
KEY = "configuration/real-pilot-four-product.json"
AUDIT = "operations/real-pilot-four-product.json"
TIME_FIELDS = {"readiness_at", "operating_expires_at", "not_before", "expires_at"}
PRIOR_BUILD_USD = Decimal("0.0232268896")
POLICY = {
    "backend_builds": 1, "frontend_builds": 0, "build_cpu": 2, "build_timeout_seconds": 900,
    "worker_executions": 1, "worker_timeout_seconds": 600, "item_limit": 4,
    "inference_requests": 12, "internal_requests": 8, "web_requests": 4,
    "search": 4, "web_retrieval": 6, "browse": 0, "analysis": 0, "retrieval": 0,
    "sharepoint": 0, "incremental_microdollars": 5000000,
    "existing_key_bindings": 1, "new_credentials": 0, "new_permissions": 0,
}
IMAGE_SMOKE_FILES = {
    "uv.lock", "pyproject.toml", "backend/sdk_image_smoke.py", "backend/sdk_preflight.py",
    "backend/batch_worker.py", "backend/core/llm.py", "backend/core/websearch.py",
    "backend/core/websearch_webiq.py", "backend/models/enrichment.py",
}
IMAGE_SMOKE_PACKAGES = {
    "openai", "httpx", "azure-core", "azure-identity",
    "azure-ai-documentintelligence", "azure-storage-blob", "pydantic",
}
GATE_CHECKS = {
    "production_run_batch", "real_guard_reservations", "full_twelve_payloads",
    "all_four_previous_attempts", "append_only_history", "immutable_results",
    "attempt_history_export", "owner_isolation", "postprep_baseline",
    "zero_new_analysis", "fatal_guards_stop_queue", "missing_key_zero_web",
    "four_searches_six_balanced_pages", "public_queries_only", "preserved_consumption",
    "pacing_fits_600_seconds", "unchanged_30000_tpm", "ledger_byte_bounds_preserved",
    "native_sdk_no_send_receipts", "native_sdk_failure_fatal_before_reservation",
}
RETAINED_FILES = (
    OWNER_AUTHORIZATION_FILE,
    PREPARATION_IDENTITY_AUTHORIZATION_FILE, EXISTING_ROLE_EVIDENCE_FILE,
    CONTINUATION_OWNER_AUTHORIZATION_FILE, PREPARATION_STOPPED_FILE,
    "gapfill-actual-outcome.json", "row-rerun-actual-outcome.json",
    "gapfill-deployment-failure.json", "gapfill-closed.json", "gapfill-published.json",
    "gapfill-readiness.json", "gapfill-readiness-pin.json", "gapfill-backend-attempt.json",
    "gapfill-deploy-attempt.json", "row-rerun-closed.json",
)
require, sha, instant = release.require, prior.sha, row.instant


def now():
    return datetime.now(timezone.utc)


def path(work, name):
    return work / f"{PREFIX}-{name}.json"


def digest_file(candidate):
    return sha(release.private_json(candidate, max_bytes=64 * 1024 * 1024))


def baseline_path(work):
    original = path(work, "baseline")
    selected = path(work, "source-selection")
    if selected.exists():
        previous, current = (release.private_json(candidate, max_bytes=64 * 1024 * 1024)
                             for candidate in (original, selected))
        require(set(current) == set(previous) | {"supersedes_baseline_sha256"}
                and current["supersedes_baseline_sha256"] == digest_file(original)
                and all(current.get(key) == value for key, value in previous.items()
                        if key not in {"source_revision", "source_review"}),
                "Superseding source selection must preserve the original baseline and allowances")
        return selected
    return original


def source_work(work):
    original = work / CHILD
    return original / "canonical-write-enums" if baseline_path(work) != path(work, "baseline") else original


def decode_records(snapshot):
    records = {}
    for key, entry in snapshot["records"].items():
        require(isinstance(key, str) and not key.startswith(("/", "locks/"))
                and ".." not in key.split("/"), "Unsafe captured record key")
        raw = base64.b64decode(entry["base64"], validate=True)
        require(hashlib.sha256(raw).hexdigest() == entry["sha256"], "Captured record bytes changed")
        records[key] = json.loads(raw)
    return records


def original(work, config):
    """Accept the actual mixed-image failure, never the predeployment image pair."""
    require(prior.root(work) == work, "Continuation must use the retained release root")
    snapshot = release.private_json(work / "gapfill-actual-outcome.json", max_bytes=64 * 1024 * 1024)
    records = decode_records(snapshot)
    before = decode_records(release.private_json(
        work / "row-rerun-actual-outcome.json", max_bytes=64 * 1024 * 1024))
    failure = release.private_json(work / "gapfill-deployment-failure.json")
    closed = release.private_json(work / "gapfill-closed.json")
    published = release.private_json(work / "gapfill-published.json")
    deployed = release.private_json(work / "gapfill-deploy-attempt.json")
    runtime = snapshot["runtime"]
    require(records == before and len(records) == 29
            and runtime["temporary_processing_closed"] is True and runtime["active_executions"] == []
            and failure["processing_closed"] is True and failure["worker_unchanged"] is True
            and failure["worker_executions_started"] == failure["model_requests"] == 0
            and failure["webiq_searches"] == failure["direct_page_attempts"] == 0
            and failure["retried"] is False
            and failure["secret_reference_installed"] is False
            and closed["target"] == release.fingerprint(config)
            and closed["decision_sha256"] == published["decision_sha256"] == deployed["decision_sha256"]
            and runtime["backend_image"] == published["backend_image"]
            and runtime["worker_image"] == deployed["worker"]["template"]["containers"][0]["image"]
            and deployed["frontend"]["template"]["containers"][0]["image"] == published["frontend_image"]
            and gap.KEY not in records and gap.AUDIT not in records,
            "Exact closed gapfill failure and unchanged 29-record history required")
    approval = records["configuration/real-pilot-approval.json"]
    ledger = records["budgets/real-pilot.json"]
    require(gap.consumption(ledger) == gap.CONSUMED and not ledger.get("invalidated"),
            "Previous execution/model allowances remain consumed")
    return approval, records, {
        **config, "backend_image": runtime["backend_image"], "worker_image": runtime["worker_image"],
        "frontend_image": published["frontend_image"],
    }


def consumption(ledger):
    return {"executions": len(ledger["executions"]), "attempted": copy.deepcopy(ledger["attempted"]),
            "reserved": copy.deepcopy(ledger["reserved"])}


def owner_authorization(work, batch):
    """Verify the captured broad consent; pending capacity is NEVER activation."""
    candidate = work / OWNER_AUTHORIZATION_FILE
    require(not candidate.is_symlink(), "Owner authorization capture cannot be redirected")
    capture = release.private_json(candidate)
    require(capture.get("schema_version") == 1
            and capture.get("kind") == "owner_authorization_capture_not_readiness"
            and capture.get("approved") is True and capture.get("readiness_clock_started") is False
            and capture.get("initial_confirmation_at") == "2026-10-06T20:16:22.694Z"
            and capture.get("batch_id") == batch["id"]
            and capture.get("selected_item_keys") == [item["item_key"] for item in batch["items"]]
            and capture.get("selected_products") == [item["manifest"]["product"]["item_id"] for item in batch["items"]],
            "The exact captured four-product owner consent must remain bound to the retained batch")
    prep, model, web = (capture[name] for name in ("preparation_analysis_exception", "inference", "web"))
    worker, financial = capture["worker"], capture["financial"]
    require(prep["approved_in_followup"] is True and prep["before_readiness_clock"] is True
            and prep["analyses"] == 1 and prep["pages"] == "1-5" and prep["maximum_billable_pages"] == 5
            and prep["sdk_retries"] == prep["blind_retries"] == prep["worker_executions"] == 0
            and prep["source_sha256"] == "b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa"
            and model["total_requests"] == 12 and model["internal_requests"] == 8 and model["web_requests"] == 4
            and model["capacity_additions"] is None
            and model["capacity_exception_decision"] == "pending exact measured request to owner"
            and web["searches"] == 4 and web["searches_per_product"] == 1
            and web["paid_browse"] == 0 and web["direct_page_attempts"] == 6
            and worker["executions"] == 1 and worker["timeout_seconds"] == 600 and worker["retries"] == 0
            and worker["pacing_adjustments_approved"] is True and worker["provider_quota_increase_approved"] is False
            and Decimal(financial["operating_envelope_usd"]) == 5
            and Decimal(financial["previous_build_cost_included_usd"]) == PRIOR_BUILD_USD
            and financial["historical_forecasts_stacked"] is False,
            "Owner capture changed; it cannot be rewritten into capacity or a readiness receipt")
    return capture


def payload_entry(item_key, tier, prepared):
    """Describe a production-prepared complete request, not a hand-counted prompt."""
    accounting = prepared.accounting
    require(accounting["system_utf8_bytes"] == len(prepared.system.encode())
            and accounting["user_utf8_bytes"] == len(prepared.user.encode())
            and accounting["response_schema_utf8_bytes"] > 0 and accounting["framing_allowance"] > 0
            and accounting["max_input_tokens"] == sum(accounting[key] for key in (
                "system_utf8_bytes", "user_utf8_bytes", "response_schema_utf8_bytes", "framing_allowance")),
            "Payload measurement must include instructions, user content, schema and framing")
    return {"item_key": item_key, "tier": tier, "input_tokens": prepared.input_bound,
            "output_tokens": accounting["max_output_tokens"], "payload_sha256": prepared.version}


def preparation_identity_authorization(work, approval, config):
    capture = release.private_json(work / PREPARATION_IDENTITY_AUTHORIZATION_FILE)
    roles = release.private_json(work / EXISTING_ROLE_EVIDENCE_FILE)
    require(type(capture.get("schema_version")) is int and capture["schema_version"] == 1
            and capture.get("approved") is True
            and capture.get("record_actual_analysis_identity_in_provenance") is True
            and capture.get("source_sha256") == "b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa"
            and capture.get("pages") == "1-5"
            and type(capture.get("max_analysis_submissions")) is int and capture["max_analysis_submissions"] == 1
            and all(type(capture.get(key)) is int and capture[key] == 0 for key in (
                "worker_executions_for_preparation", "permission_grants", "identity_attachments",
                "deployments", "counter_resets"))
            and capture.get("readiness_clock_started") is False
            and capture.get("all_other_authority") == "Unchanged from " + OWNER_AUTHORIZATION_FILE
            and instant(capture["recorded_after_confirmation_at"]) <= now(),
            "The separately recorded one-call split-identity approval must remain unchanged")
    identities = roles["identities"]
    expected = {"worker": approval["identities"]["worker_principal_id"],
                "api": approval["identities"]["api_principal_id"], "approved_operator": approval["approved_by"]}
    hostname = urlsplit(approval["environment"]["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]).hostname
    require(hostname is not None, "The original Document Intelligence resource endpoint is required")
    account = hostname.split(".")[0]
    scope = (f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}"
             f"/providers/Microsoft.CognitiveServices/accounts/{account}")
    require(roles.get("role") == "Cognitive Services User"
            and roles.get("role_definition_id") == "a97b65f3-24c7-4388-baec-2e87135dc908"
            and roles.get("document_intelligence_scope", "").casefold() == scope.casefold()
            and all(identities[name]["principal_id"] == principal for name, principal in expected.items())
            and identities["worker"].get("matching_unconditional_role_exists") is True
            and identities["worker"].get("new_grant_needed_for_worker_analysis") is False
            and identities["approved_operator"].get("matching_unconditional_role_exists") is True
            and identities["approved_operator"].get("storage_blob_data_grant_observed") is False
            and identities["api"].get("matching_unconditional_role_exists") is False
            and identities["api"].get("assignments_returned_for_this_principal_at_or_above_di_scope") == 0
            and roles.get("permissions_changed") is False and roles.get("identity_attachments_changed") is False,
            "Preserve the existing worker/operator DI grants and API DI gap; no IAM changes are authorized")
    return capture, roles


def continuation_owner_authorization(work, claim, packet):
    value = release.private_json(work / CONTINUATION_OWNER_AUTHORIZATION_FILE)
    conditions = value.get("conditions", {})
    require(value.get("schema_version") == 1 and value.get("approved") is True
            and value.get("original_packet_sha256") == sha(packet)
            and value.get("existing_reservation_id") == claim["reservation_id"]
            and value.get("max_continuations") == 1 and value.get("max_di_submissions_in_continuation") == 1
            and all(type(value.get(key)) is int and value[key] == 0 for key in (
                "counter_resets", "extra_analysis_reservations", "permission_changes", "identity_changes"))
            and value.get("whole_helper_rerun_authorized") is False
            and value.get("preserve_original_claim_failure_receipts_and_history") is True
            and value.get("readiness_clock_started") is False
            and all(conditions.get(key) is True for key in (
                "focused_ci_passed_before_continuation",
                "permanent_regression_constructs_real_AzureCliCredential_with_production_arguments",
                "no_send_uses_exact_client_and_request_against_installed_sdk", "no_send_failure_is_release_blocker"))
            and conditions.get("native_no_send_construction_before_live_calls") == ["DI", "model", "WebIQ", "deployment"]
            and instant(value["recorded_after_confirmation_at"]) <= now(),
            "The one existing-reservation continuation and mandatory native SDK policy must remain exact")
    return value


def verify_continued_preparation(work, postprep):
    from backend.real_pilot import BUDGET_KEY, FOUR_PRODUCT_PREP_PREFIX, FOUR_PRODUCT_PREP_CONTINUATION_PREFIX

    packet = postprep[FOUR_PRODUCT_PREP_PREFIX + "packet.json"]
    claim = postprep[FOUR_PRODUCT_PREP_PREFIX + "reserved.json"]
    plan = postprep[FOUR_PRODUCT_PREP_CONTINUATION_PREFIX + "plan.json"]
    authorization = postprep[FOUR_PRODUCT_PREP_CONTINUATION_PREFIX + "authorization.json"]
    completed = postprep[FOUR_PRODUCT_PREP_CONTINUATION_PREFIX + "completed.json"]
    owner = continuation_owner_authorization(work, claim, packet)
    stopped_snapshot = release.private_json(work / PREPARATION_STOPPED_FILE, max_bytes=64 * 1024 * 1024)
    stopped = decode_records(stopped_snapshot)
    require(plan["stopped_snapshot_sha256"] == sha({**stopped_snapshot, "records": stopped})
            and plan["history_sha256"] == {key: sha(value) for key, value in stopped.items()}
            and sha(stopped[BUDGET_KEY]) == claim["reserved_ledger_canonical_sha256"]
            and completed["status"] == "existing_reservation_continuation_completed"
            and completed["original_completed_canonical_sha256"]
            == sha(postprep[FOUR_PRODUCT_PREP_PREFIX + "completed.json"])
            and instant(owner["recorded_after_confirmation_at"]) <= instant(authorization["approved_at"]),
            "The actual stopped reservation must have one explicitly authorized completed continuation")
    for key, value in stopped.items():
        if key != BUDGET_KEY:
            require(postprep.get(key) == value, "Continuation must preserve every stopped record")
    require(postprep[BUDGET_KEY]["executions"] == stopped[BUDGET_KEY]["executions"]
            and postprep[BUDGET_KEY]["attempted"] == stopped[BUDGET_KEY]["attempted"]
            and postprep[BUDGET_KEY]["reserved"] == stopped[BUDGET_KEY]["reserved"]
            and set(postprep[BUDGET_KEY]["reservations"]) == set(stopped[BUDGET_KEY]["reservations"]),
            "Continuation cannot add executions/reservations or reset charged counters")
    evidence = plan["local_evidence_sha256"]
    require(plan["owner_approval_filename"] == CONTINUATION_OWNER_AUTHORIZATION_FILE
            and evidence.get(CONTINUATION_OWNER_AUTHORIZATION_FILE) == sha(owner),
            "The recorded continuation owner approval must be bound by the new plan")
    for name, expected in evidence.items():
        require(Path(name).name == name and name.endswith(".json")
                and digest_file(work / name) == expected, "Pinned continuation evidence changed")
    return owner


def capacity_request(ledger, approval, row_amendment, plan, *, duration_assumptions=None,
                     worker_slices=None, slice_authorization=None):
    """A measured request, NOT approval. Does not grant/refund/reset any capacity."""
    require(isinstance(plan, list) and len(plan) == 12, "All twelve complete payload bounds must be measured")
    counts = {}
    for entry in plan:
        require(set(entry) == {"item_key", "tier", "input_tokens", "output_tokens", "payload_sha256"}
                and entry["tier"] in {"internal_pdf", "vendor_table", "manufacturer_web"}
                and type(entry["input_tokens"]) is int and entry["input_tokens"] > 0
                and type(entry["output_tokens"]) is int and entry["output_tokens"] == 2048
                and entry["input_tokens"] <= (26000 if entry["tier"] == "manufacturer_web" else 27952)
                and re.fullmatch(r"[a-f0-9]{64}", entry["payload_sha256"]),
                "Complete serialized payload and exact output bound required")
        pair = entry["item_key"], entry["tier"]
        counts[pair] = counts.get(pair, 0) + 1
    selected = {entry["item_key"] for entry in plan}
    require(len(selected) == 4 and len(counts) == 12 and set(counts.values()) == {1},
            "Exactly two internal and one optional public payload per product required")
    remaining = {
        "inference": approval["limits"]["inference"] - ledger["attempted"]["inference"],
        "input_tokens": row_amendment["effective_input_ceiling"] - ledger["reserved"]["input_tokens"],
        "output_tokens": approval["limits"]["output_tokens"] - ledger["reserved"]["output_tokens"],
    }
    require(min(remaining.values()) >= 0, "Retained capacity is inconsistent")
    total = sum(entry["input_tokens"] for entry in plan)
    if duration_assumptions is not None:
        from backend.real_pilot import validate_four_product_duration

        validate_four_product_duration(duration_assumptions)
    requested = {
        "plan_sha256": sha(plan), "max_requests": 12, "max_input_tokens": total, "max_output_tokens": 24576,
        "additional_executions": 1, "additional_inference": max(0, 12 - remaining["inference"]),
        "additional_input_tokens": max(0, total - remaining["input_tokens"]),
        "additional_output_tokens": max(0, 24576 - remaining["output_tokens"]),
        "duration_assumptions": copy.deepcopy(duration_assumptions),
    }
    if worker_slices is not None:
        from backend.real_pilot import validate_four_product_slices

        requested["additional_executions"] = validate_four_product_slices({
            "worker_slices": worker_slices, "slice_authorization": slice_authorization,
            "slice_authorization_sha256": sha(slice_authorization),
        }, requested, approval["approved_by"])
    return requested


def web_configuration(records, public_attribute_terms, manufacturers):
    from backend.core.websearch_policy import OptionalWebPolicy, PublicWebScope
    from backend.core.websearch_webiq import ENDPOINT
    from backend.real_pilot import RealPilotGuard

    approval = records["configuration/real-pilot-approval.json"]
    batch = records[f"batches/{approval['batch_id']}.json"]
    selected = [item["item_key"] for item in batch["items"]]
    require(len(selected) == 4 and set(public_attribute_terms) == set(manufacturers) == set(selected),
            "Reviewed public identities/attribute labels for all four products required")
    scopes = {}
    execution_order = [selected[0], selected[2], selected[1], selected[3]]
    page_limits = {key: 2 if index < 2 else 1 for index, key in enumerate(execution_order)}
    for item in batch["items"]:
        key = item["item_key"]
        sources = [source for source in item["sources"]
                   if source["kind"] == "web" and source["source_tier"] == "manufacturer_web"]
        scopes[key] = PublicWebScope(
            manufacturer=manufacturers[key], mpn=item["manifest"]["product"]["mpn"],
            attribute_terms=public_attribute_terms[key], source_ids=[source["source_id"] for source in sources],
            allowed_hosts=sorted({urlsplit(source["url"]).hostname for source in sources}),
            max_direct_page_attempts=page_limits[key],
        )
    policy = OptionalWebPolicy(
        batch_sha256=approval["batch_sha256"], items=scopes, max_search_calls=4,
        max_direct_page_attempts=6, max_inference_calls=4,
        max_cost_microdollars=RealPilotGuard._cost(approval, "inference", 104000, 8192, 0) + 50000,
    )
    return {
        "public_web_policy": policy.model_dump(mode="json"),
        "web_environment": {"WEBSEARCH_PROVIDER": "webiq", "WEBIQ_ENDPOINT": ENDPOINT},
        "web_prices": {"search": "0.0125", "web_retrieval": "0"},
        "page_limits": page_limits,
    }


def amendment_scope(records, *, capacity_approval, plan, prep_receipt_key, public_web_policy,
                    web_environment, web_prices, page_limits, fixed_cost_microdollars,
                    inference_interval_seconds=31, worker_slices=None, slice_authorization=None):
    """Bind actual post-PREP records only after the separate capacity approval."""
    from backend.real_pilot import FOUR_PRODUCT_CAPACITY_FIELDS, FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS

    approval = records["configuration/real-pilot-approval.json"]
    ledger, previous = records["budgets/real-pilot.json"], records["configuration/real-pilot-row-rerun.json"]
    require(isinstance(capacity_approval, dict) and capacity_approval.get("duration_assumptions") is not None,
            "Exact capacity approval must include the conditional duration assumptions")
    require(inference_interval_seconds == FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS
            and type(inference_interval_seconds) is int, "Only the approved four-scope 31-second start spacing is supported")
    expected = capacity_request(
        ledger, approval, previous, plan, duration_assumptions=capacity_approval.get("duration_assumptions"),
        worker_slices=worker_slices, slice_authorization=slice_authorization,
    )
    require(isinstance(capacity_approval, dict) and set(capacity_approval) == FOUR_PRODUCT_CAPACITY_FIELDS
            and capacity_approval["approved"] is True
            and capacity_approval["approved_by"] == approval["approved_by"]
            and all(capacity_approval[key] == value for key, value in expected.items()),
            "Exact user-approved measured capacity receipt required; planning is not authority")
    instant(capacity_approval["approved_at"])
    require(prep_receipt_key in records, "Actual completed PREP receipt required")
    batch = records[f"batches/{approval['batch_id']}.json"]
    selected = [item["item_key"] for item in batch["items"]]
    require({entry["item_key"] for entry in plan} == set(selected), "Payload plan belongs to another item scope")
    results = {}
    for item_key in selected:
        state = records[f"items/{approval['batch_id']}/{item_key}.json"]
        for _ in range(32):
            root = f"results/{approval['batch_id']}/{item_key}"
            result_key = state.get("result_key") or root + ".json"
            require(isinstance(result_key, str) and (result_key == root + ".json"
                    or re.fullmatch(re.escape(root) + r"/attempts/[a-f0-9]{64}\.json", result_key)),
                    "Prior attempt references an unrelated result")
            if result_key in records:
                results[result_key] = sha(records[result_key])
            state = state.get("previous_attempt")
            if state is None:
                break
        else:
            raise ValueError("Prior history exceeds its bound")
    caches = {key: sha(value) for key, value in records.items() if re.fullmatch(r"parses/[a-f0-9]{64}\.json", key)}
    history = {key: sha(value) for key, value in records.items()
               if not key.startswith(("items/", "batches/")) and key != "budgets/real-pilot.json"}
    require(KEY not in records and AUDIT not in records and gap.KEY not in records and gap.AUDIT not in records,
            "Prior or current continuation authority already exists")
    scope = {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
        "approval_sha256": sha(approval), "batch_sha256": approval["batch_sha256"],
        "ledger_sha256": sha(ledger), "prior_row_sha256": sha(previous),
        "prior_row_audit_sha256": sha(records["operations/real-pilot-row-rerun.json"]),
        "prior_execution_ids": sorted(ledger["executions"]), "selected_item_keys": selected,
        "item_sha256": {key: sha(records[f"items/{approval['batch_id']}/{key}.json"]) for key in selected},
        "result_sha256": results, "cached_documents": caches, "historical_records": history,
        "prep_receipt_key": prep_receipt_key, "prep_receipt_sha256": sha(records[prep_receipt_key]),
        "baseline": consumption(ledger), "capacity_approval": copy.deepcopy(capacity_approval),
        "public_web_policy": public_web_policy, "web_environment": web_environment, "web_prices": web_prices,
        "page_limits": page_limits, "fixed_cost_microdollars": fixed_cost_microdollars,
        "execution_order": [selected[0], selected[2], selected[1], selected[3]],
        "inference_interval_seconds": inference_interval_seconds,
    }
    if worker_slices is not None:
        scope.update(worker_slices=copy.deepcopy(worker_slices), slice_authorization=copy.deepcopy(slice_authorization),
                     slice_authorization_sha256=sha(slice_authorization))
    return scope


def fixed_cost(completed_prep, approval, *, worker_slices=None):
    """Forecast completed PREP usage without releasing its reserved allowance."""
    from backend.real_pilot import RealPilotGuard

    receipt = completed_prep["analysis_receipt"]
    pages = receipt.get("actual_page_count")
    require(completed_prep.get("status") == "prepared_cache_verified_for_both_products"
            and receipt.get("outcome") == "succeeded" and type(pages) is int and 1 <= pages <= 5
            and receipt.get("returned_pages") == list(range(1, pages + 1)),
            "Completed actual PREP page usage is required for the fixed forecast")
    measured_cost = RealPilotGuard._cost(approval, "analysis", 0, 0, pages)
    from backend.real_pilot import FOUR_PRODUCT_SLICES

    require(worker_slices is None or worker_slices == FOUR_PRODUCT_SLICES, "Only two distinct authorized worker slices")
    return (int((PRIOR_BUILD_USD * 1000000).to_integral_value(rounding=ROUND_CEILING)) + 198000
            + measured_cost + (18000 if worker_slices is not None else 0))


def validate_pacing(proof, actual_plan, *, execution_order, interval_seconds, duration_assumptions=None,
                    worker_slices=None):
    """Verify 31-second starts and an explicitly conditional empirical forecast."""
    from backend.real_pilot import FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS, validate_four_product_duration
    if worker_slices is not None:
        return validate_slice_pacing(proof, actual_plan, worker_slices=worker_slices,
                                     execution_order=execution_order, interval_seconds=interval_seconds,
                                     duration_assumptions=duration_assumptions)

    require(isinstance(proof, dict) and proof.get("deployment_tpm") == 30000
            and proof.get("basis") == "empirical_provider_latency_conditional"
            and proof.get("quota_changed") is False
            and proof.get("ledger_byte_bounds_preserved") is True
            and proof.get("worker_timeout_seconds") == 600
            and proof.get("provider_rate_estimate_verified") is False
            and proof.get("byte_bounds_used_as_rate_estimate") is False
            and proof.get("billing_usage_used_as_rate_estimate") is False
            and proof.get("web_allowance_is_hard_deadline") is False
            and proof.get("phase_timeouts_bound_dns") is False,
            "Pacing must not claim reserved bytes or billed tokens are the provider's rate estimate")
    assumptions = validate_four_product_duration(proof["duration_assumptions"])
    if duration_assumptions is not None:
        require(assumptions == duration_assumptions, "Duration assumptions differ from the exact owner capacity decision")
    elapsed = proof["worker_elapsed_seconds"]
    simulated = proof.get("simulated_worker_elapsed_seconds")
    local = proof.get("measured_local_worker_seconds")
    forecast = proof.get("forecast_including_local_seconds")
    require(all(type(value) in (int, float) and math.isfinite(value) and value > 0
                for value in (elapsed, simulated, local, forecast))
            and proof.get("local_measurement") == "perf_counter_whole_real_worker_with_memory_store"
            and proof.get("remote_storage_latency_measured") is False
            and math.isclose(elapsed, simulated + local, abs_tol=1e-8)
            and math.isclose(forecast, assumptions["forecast_seconds"] + local, abs_tol=1e-8)
            and forecast < 600,
            "Measured local worker overhead must be charged conservatively; remote latency remains conditional")
    item_seconds = proof.get("measured_item_local_seconds")
    require(isinstance(item_seconds, dict) and set(item_seconds) == set(execution_order)
            and all(type(value) in (int, float) and math.isfinite(value) and value > 0
                    for value in item_seconds.values())
            and proof.get("prior_two_comparison") == "same_run_mueller_item_subtotal_not_historical_replay",
            "Actual per-item local measurements and a clearly labeled prior-pair comparison are required")
    expected_measurements = {
        "measured_four_item_subtotal_seconds": sum(item_seconds.values()),
        "measured_prior_two_item_subtotal_seconds": sum(item_seconds[key] for key in ("row-2", "row-3")),
        "measured_shared_local_seconds": local - sum(item_seconds.values()),
        "remaining_margin_seconds": 600 - forecast,
    }
    for name, expected in expected_measurements.items():
        measured = proof.get(name)
        require(type(measured) in (int, float) and math.isfinite(measured)
                and measured >= 0 and math.isclose(measured, expected, abs_tol=1e-8),
                "Four-versus-prior-pair local accounting or remaining margin changed")
    require(type(elapsed) in (int, float) and math.isfinite(elapsed)
            and 0 < elapsed <= forecast < 600
            and type(interval_seconds) is int and interval_seconds == FOUR_PRODUCT_INFERENCE_INTERVAL_SECONDS,
            "Full worker trace and conditional forecast must fit 600 seconds using exactly 31-second starts")
    requests = proof["requests"]
    order = [(key, tier) for key in execution_order
             for tier in ("internal_pdf", "vendor_table", "manufacturer_web")]
    require(isinstance(requests, list) and len(requests) == len(actual_plan) == 12
            and [(entry["item_key"], entry["tier"]) for entry in requests] == order,
            "A complete interleaved twelve-request worker trace is required")
    planned = {(entry["item_key"], entry["tier"]): entry for entry in actual_plan}
    require(len(planned) == 12, "Complete payload accounting must identify each request exactly once")
    for index, request in enumerate(requests):
        plan = planned[(request["item_key"], request["tier"])]
        require(request["payload_sha256"] == plan["payload_sha256"]
                and request["input_byte_bound"] == plan["input_tokens"]
                and request["output_tokens"] == plan["output_tokens"],
                "Pacing trace must bind the complete payload without reducing ledger byte bounds")
        start, end = request["start_seconds"], request["end_seconds"]
        require(all(type(value) in (int, float) and math.isfinite(value) for value in (start, end))
                and start >= assumptions["startup_bookkeeping_seconds"]
                and 0 <= end - start <= assumptions["model_response_allowance_seconds"] + 1e-9
                and end <= simulated, "Request timing is absent or outside the conditional duration assumptions")
        if index:
            previous = requests[index - 1]
            require(start >= previous["end_seconds"]
                    and start - previous["start_seconds"] + 1e-9 >= interval_seconds,
                    "Trace must preserve serialized per-product requests and the reviewed pacing interval")
        active = [entry for entry in requests[:index + 1] if start - 60 < entry["start_seconds"] <= start]
        require(len(active) <= 2, "Trace exceeds the reviewed maximum of two request starts per minute")
    web_delays = proof.get("web_delay_events")
    require(isinstance(web_delays, list) and len(web_delays) == 4,
            "All four products need explicit simulated web-network allowances")
    for index, (event, item_key) in enumerate(zip(web_delays, execution_order)):
        start, end = event["start_seconds"], event["end_seconds"]
        require(event["item_key"] == item_key
                and all(type(value) in (int, float) and math.isfinite(value) for value in (start, end))
                and requests[index * 3 + 1]["end_seconds"] <= start <= end <= requests[index * 3 + 2]["start_seconds"]
                and math.isclose(end - start, assumptions["web_network_allowance_seconds"] / 4, abs_tol=1e-8),
                "The full web allowance must be interleaved once per product before its web model call")
    return proof


def validate_slice_pacing(proof, actual_plan, *, worker_slices, execution_order, interval_seconds, duration_assumptions):
    from backend.real_pilot import FOUR_PRODUCT_SLICES, validate_four_product_duration

    require(worker_slices == FOUR_PRODUCT_SLICES and execution_order == sum(worker_slices, [])
            and interval_seconds == 31 and isinstance(proof.get("slices"), list) and len(proof["slices"]) == 2
            and len(actual_plan) == 12, "Both distinct six-request slice traces are required")
    assumptions = validate_four_product_duration(duration_assumptions)
    require(proof.get("duration_assumptions") == assumptions and proof.get("deployment_tpm") == 30000
            and proof.get("quota_changed") is False and proof.get("ledger_byte_bounds_preserved") is True
            and proof.get("worker_timeout_seconds") == 600
            and all(proof.get(key) is False for key in (
                "provider_rate_estimate_verified", "byte_bounds_used_as_rate_estimate", "billing_usage_used_as_rate_estimate",
                "web_allowance_is_hard_deadline", "phase_timeouts_bound_dns")),
            "Slice timing cannot weaken the unchanged quota or conditional latency assumptions")
    for index, value in enumerate(proof["slices"]):
        planned = actual_plan[index * 6:(index + 1) * 6]
        items = worker_slices[index]
        require([(entry["item_key"], entry["tier"]) for entry in planned] == [
            (key, tier) for key in items for tier in ("internal_pdf", "vendor_table", "manufacturer_web")
        ], "Each slice completes both internal tiers and optional web before advancing")
        require(value.get("slice_index") == index and value.get("item_keys") == items
                and isinstance(value.get("requests"), list) and len(value["requests"]) == 6,
                "Each slice must bind its own six complete requests")
        local, simulated, elapsed, forecast = (value.get(key) for key in (
            "measured_local_worker_seconds", "simulated_worker_elapsed_seconds",
            "worker_elapsed_seconds", "forecast_including_local_seconds"))
        base = (5 * max(31, assumptions["model_response_allowance_seconds"])
                + assumptions["model_response_allowance_seconds"] + assumptions["startup_bookkeeping_seconds"]
                + assumptions["web_network_allowance_seconds"] / 2)
        require(all(type(x) in (int, float) and math.isfinite(x) and x > 0 for x in (local, simulated, elapsed, forecast))
                and math.isclose(elapsed, simulated + local, abs_tol=1e-8)
                and math.isclose(forecast, base + local, abs_tol=1e-8) and elapsed <= forecast <= 510
                and value.get("local_measurement") == "perf_counter_whole_real_worker_with_memory_store"
                and value.get("remote_storage_latency_measured") is False
                and math.isclose(value.get("remaining_margin_seconds", -1), 600 - forecast, abs_tol=1e-8),
                "Every slice needs at least 90 seconds of measured conditional forecast slack")
        previous = None
        for request, payload in zip(value["requests"], planned):
            require(all(request.get(key) == payload[key] for key in ("item_key", "tier", "payload_sha256"))
                    and request.get("input_byte_bound") == payload["input_tokens"]
                    and request.get("output_tokens") == payload["output_tokens"],
                    "Slice request fingerprints/bounds differ from the unchanged twelve-payload plan")
            start, end = request.get("start_seconds"), request.get("end_seconds")
            require(all(type(x) in (int, float) and math.isfinite(x) for x in (start, end))
                    and assumptions["startup_bookkeeping_seconds"] <= start <= end <= simulated
                    and end - start <= assumptions["model_response_allowance_seconds"] + 1e-8
                    and (previous is None or (start >= previous["end_seconds"]
                                             and start - previous["start_seconds"] + 1e-8 >= 31)),
                    "Each slice must preserve serialized 31-second request starts")
            previous = request
        events = value.get("web_delay_events")
        require(isinstance(events, list) and len(events) == 2, "Each slice needs both explicit web-network allowances")
        for offset, event in enumerate(events):
            require(event["item_key"] == items[offset]
                    and value["requests"][offset * 3 + 1]["end_seconds"] <= event["start_seconds"]
                    <= event["end_seconds"] <= value["requests"][offset * 3 + 2]["start_seconds"]
                    and math.isclose(event["end_seconds"] - event["start_seconds"],
                                     assumptions["web_network_allowance_seconds"] / 4, abs_tol=1e-8),
                    "Both products retain their full bounded web-delay scenario")
    require(proof.get("requests") == [request for value in proof["slices"] for request in value["requests"]]
            and all(type(proof.get(key)) in (int, float)
                    and math.isclose(proof[key], sum(value[key] for value in proof["slices"]), abs_tol=1e-8)
                    for key in ("measured_local_worker_seconds", "simulated_worker_elapsed_seconds",
                                "worker_elapsed_seconds", "forecast_including_local_seconds"))
            and math.isclose(proof.get("remaining_margin_seconds", -1),
                             min(value["remaining_margin_seconds"] for value in proof["slices"]), abs_tol=1e-8),
            "Aggregate twelve-request timing must equal the two distinct measured worker slices")
    return proof


def execution_policy(scope):
    return {**POLICY, **({"worker_executions": 2, "item_limit": 2} if scope.get("worker_slices") else {})}


def validate_provider_preflights(proof, actual_plan, public_policy):
    """Read metadata-only native receipts; never construct credentials or send."""
    require(isinstance(proof, dict) and set(proof) == {"model", "webiq", "web_retrieval"}
            and all(isinstance(entries, list) for entries in proof.values())
            and len(proof["model"]) == len(actual_plan) == 12
            and len(proof["webiq"]) == 4 and len(proof["web_retrieval"]) == 6,
            "All twelve model, four WebIQ and six direct-page exact-request no-send receipts are required")
    for provider, package, entries in (
        ("model", "openai", proof["model"]), ("webiq", "httpx", proof["webiq"]),
    ):
        for index, entry in enumerate(entries):
            receipt = entry["receipt"]
            require(receipt.get("status") == "validated_no_send" and receipt.get("provider") == provider
                    and receipt.get("sdk_package") == package and receipt.get("method") == "POST"
                    and receipt.get("content_type") == "application/json"
                    and all(receipt.get(key) is False for key in (
                        "authentication_performed", "provider_send_performed", "provider_response_fabricated"))
                    and type(receipt.get("body_bytes")) is int and receipt["body_bytes"] > 0
                    and receipt.get("transport_package") in ({"httpx", "httpx2"} if provider == "model" else {"httpx"})
                    and receipt.get("sdk_version") == installed_version(package)
                    and receipt.get("transport_version") == installed_version(receipt["transport_package"])
                    and all(isinstance(receipt.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", receipt[key])
                            for key in ("endpoint_sha256", "body_sha256", "request_sha256")),
                    "Native request/installed-SDK receipt failed; no live calls are authorized")
            if provider == "model":
                planned = actual_plan[index]
                require(all(entry.get(key) == planned[key] for key in ("item_key", "tier", "payload_sha256"))
                        and {"model", "messages", "response_format", "max_completion_tokens"}
                        <= set(receipt.get("body_fields", [])),
                        "Native model receipt must bind the full prepared prompt/schema")
            else:
                planned = actual_plan[index * 3]
                scope = public_policy["items"][planned["item_key"]]
                require(entry.get("item_key") == planned["item_key"]
                        and isinstance(entry.get("query_sha256"), str)
                        and re.fullmatch(r"[a-f0-9]{64}", entry["query_sha256"])
                        and receipt.get("allowed_domains_sha256") == sha(sorted(scope["allowed_hosts"]))
                        and receipt.get("allowed_domains_count") == len(scope["allowed_hosts"])
                        and receipt.get("discovery_only_not_evidence") is True
                        and set(receipt.get("body_fields", [])) == {"query", "maxResults", "contentFormat", "maxLength"},
                        "Native WebIQ receipt must bind its exact public query and host filter")
    validate_page_preflights(proof["web_retrieval"], actual_plan, public_policy)
    return proof


def validate_page_preflights(entries, actual_plan, public_policy):
    hash_fields = {
        "endpoint_sha256", "path_sha256", "request_sha256", "body_sha256",
        "wire_request_sha256", "headers_sha256", "allowed_hosts_sha256",
    }
    zero_fields = {
        "transport_real_calls", "network_calls", "dns_calls", "socket_connect_calls",
        "tls_handshakes", "credential_real_calls",
    }
    false_fields = {
        "authentication_performed", "provider_send_performed", "provider_response_fabricated",
        "dns_address_safety_verified", "source_content_verified",
    }
    fields = hash_fields | zero_fields | false_fields | {
        "status", "provider", "sdk_package", "sdk_version", "transport_package", "transport_version",
        "client", "method", "api_version", "body_bytes", "wire_request_bytes", "header_names",
        "allowed_hosts_count", "timeout_seconds", "response_max_bytes", "transport", "captured_requests",
    }
    expected_items = [
        planned["item_key"] for planned in actual_plan[::3]
        for _ in range(public_policy["items"][planned["item_key"]]["max_direct_page_attempts"])
    ]
    require(len(entries) == len(expected_items) == 6, "Six balanced direct-page native receipts are required")
    seen = set()
    for entry, item_key in zip(entries, expected_items):
        require(isinstance(entry, dict) and set(entry) == {"item_key", "url_sha256", "receipt"}
                and entry["item_key"] == item_key,
                "Direct-page receipts must preserve the approved per-product order and allocation")
        receipt, scope = entry["receipt"], public_policy["items"][item_key]
        require(isinstance(receipt, dict) and set(receipt) == fields
                and receipt["status"] == "validated_no_send" and receipt["provider"] == "web_retrieval"
                and receipt["sdk_package"] == "http.client" and receipt["sdk_version"] == platform.python_version()
                and receipt["transport_package"] == "ssl" and receipt["transport_version"] == ssl.OPENSSL_VERSION
                and receipt["client"] == "_PinnedHTTPSConnection"
                and receipt["method"] == "GET" and receipt["api_version"] is None
                and receipt["transport"] == "in_memory_no_send"
                and type(receipt["captured_requests"]) is int and receipt["captured_requests"] == 1
                and all(type(receipt[key]) is int and receipt[key] == 0 for key in zero_fields)
                and all(receipt[key] is False for key in false_fields)
                and all(isinstance(receipt[key], str) and re.fullmatch(r"[a-f0-9]{64}", receipt[key])
                        for key in hash_fields)
                and type(receipt["body_bytes"]) is int and receipt["body_bytes"] == 0
                and receipt["body_sha256"] == hashlib.sha256(b"").hexdigest()
                and type(receipt["wire_request_bytes"]) is int and 0 < receipt["wire_request_bytes"] <= 8192
                and receipt["header_names"] == ["accept", "accept-encoding", "host", "user-agent"]
                and receipt["allowed_hosts_sha256"] == sha(sorted(scope["allowed_hosts"]))
                and type(receipt["allowed_hosts_count"]) is int
                and receipt["allowed_hosts_count"] == len(scope["allowed_hosts"])
                and type(receipt["timeout_seconds"]) in (int, float) and receipt["timeout_seconds"] == 15
                and type(receipt["response_max_bytes"]) is int and receipt["response_max_bytes"] == 262144
                and entry["url_sha256"] == receipt["endpoint_sha256"],
                "Direct-page native request/installed-runtime receipt failed; no retrieval is authorized")
        binding = (item_key, entry["url_sha256"])
        require(binding not in seen, "Duplicate direct-page proof cannot replace another approved page")
        seen.add(binding)
    return entries


def _validate_image_requests(proof):
    installed = proof["installed_sdk_versions"]
    requests = proof["native_requests"]
    common_fields = {
        "status", "provider", "sdk_package", "sdk_version", "transport_package", "transport_version",
        "method", "api_version", "endpoint_sha256", "body_sha256", "body_bytes", "content_type",
        "request_sha256", "authentication_performed", "provider_send_performed", "provider_response_fabricated",
    }
    require(isinstance(requests, list) and requests, "Image smoke requires native provider captures")
    providers, openssl_versions, model_transports = set(), set(), set()
    for native in requests:
        require(isinstance(native, dict) and native.get("status") == "validated_no_send"
                and native.get("provider") in {"model", "webiq", "web_retrieval"}
                and all(native.get(key) is False for key in (
                    "authentication_performed", "provider_send_performed", "provider_response_fabricated"))
                and all(isinstance(native.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", native[key])
                        for key in ("endpoint_sha256", "body_sha256", "request_sha256"))
                and type(native.get("body_bytes")) is int,
                "Image native capture must be genuine no-send metadata, not a service result")
        provider = native["provider"]
        providers.add(provider)
        if provider in {"model", "webiq"}:
            fields_expected = common_fields | {"body_fields", "body_field_count"}
            if provider == "webiq":
                fields_expected |= {"allowed_domains_sha256", "allowed_domains_count", "discovery_only_not_evidence"}
            sdk = "openai" if provider == "model" else "httpx"
            transport = native.get("transport_package")
            require(set(native) == fields_expected
                    and native.get("sdk_package") == sdk and native.get("sdk_version") == installed[sdk]
                    and transport in ({"httpx", "httpx2"} if provider == "model" else {"httpx"})
                    and transport in installed and native.get("transport_version") == installed[transport]
                    and native.get("method") == "POST" and native.get("content_type") == "application/json"
                    and native["body_bytes"] > 0,
                    "Image provider capture must match its image-installed locked packages")
            fields = native.get("body_fields")
            require(isinstance(fields, list) and all(isinstance(name, str) for name in fields)
                    and type(native.get("body_field_count")) is int
                    and native["body_field_count"] == len(fields) == len(set(fields)),
                    "Image capture lacks complete native request metadata")
            if provider == "model":
                model_transports.add(transport)
                require(native.get("api_version") == "2025-01-01-preview"
                        and {"model", "messages", "response_format", "max_completion_tokens"} <= set(fields),
                        "Image model capture must contain the prepared prompt/schema")
            else:
                require(native.get("api_version") is None
                        and set(fields) == {"query", "maxResults", "contentFormat", "maxLength"}
                        and native.get("discovery_only_not_evidence") is True
                        and type(native.get("allowed_domains_count")) is int and native["allowed_domains_count"] > 0
                        and isinstance(native.get("allowed_domains_sha256"), str)
                        and re.fullmatch(r"[a-f0-9]{64}", native["allowed_domains_sha256"]),
                        "Image WebIQ capture must retain its public-domain binding")
        else:
            require(set(native) == common_fields - {"content_type"} | {
                        "client", "path_sha256", "wire_request_sha256", "wire_request_bytes", "headers_sha256",
                        "header_names", "allowed_hosts_sha256", "allowed_hosts_count", "timeout_seconds",
                        "response_max_bytes", "transport", "captured_requests", "transport_real_calls", "network_calls",
                        "dns_calls", "socket_connect_calls", "tls_handshakes", "credential_real_calls",
                        "dns_address_safety_verified", "source_content_verified",
                    } and native.get("sdk_package") == "http.client" and native.get("sdk_version") == proof["python_version"]
                    and native.get("transport_package") == "ssl"
                    and isinstance(native.get("transport_version"), str) and native["transport_version"]
                    and native.get("client") == "_PinnedHTTPSConnection" and native.get("method") == "GET"
                    and native.get("api_version") is None and native["body_bytes"] == 0
                    and native["body_sha256"] == hashlib.sha256(b"").hexdigest()
                    and native.get("transport") == "in_memory_no_send"
                    and type(native.get("captured_requests")) is int and native["captured_requests"] == 1
                    and all(type(native.get(key)) is int and native[key] == 0 for key in (
                        "transport_real_calls", "network_calls", "dns_calls", "socket_connect_calls",
                        "tls_handshakes", "credential_real_calls"))
                    and native.get("dns_address_safety_verified") is False and native.get("source_content_verified") is False
                    and all(isinstance(native.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", native[key])
                            for key in ("path_sha256", "wire_request_sha256", "headers_sha256", "allowed_hosts_sha256"))
                    and type(native.get("wire_request_bytes")) is int and 0 < native["wire_request_bytes"] <= 8192
                    and native.get("header_names") == ["accept", "accept-encoding", "host", "user-agent"]
                    and type(native.get("allowed_hosts_count")) is int and native["allowed_hosts_count"] > 0
                    and type(native.get("timeout_seconds")) in (int, float) and 0 < native["timeout_seconds"] <= 15
                    and type(native.get("response_max_bytes")) is int and 0 < native["response_max_bytes"] <= 262144,
                    "Image direct-page capture must bind its image Python/OpenSSL runtime and zero-send counters")
            openssl_versions.add(native["transport_version"])
    require(providers == {"model", "webiq", "web_retrieval"} and len(openssl_versions) == 1
            and set(installed) == IMAGE_SMOKE_PACKAGES | model_transports,
            "Image smoke must cover all three native builders in one locked image runtime")


def validate_image_smoke(work, decision):
    """Admit image-side evidence against reviewed Git bytes, never host SDKs."""
    source = path(work, "image-sdk-smoke")
    require(source.is_file() and not source.is_symlink(),
            "Actual image SDK smoke receipt is required before activation")
    envelope = receipt(work, "image-sdk-smoke", decision)
    require(set(envelope) == {
                "schema_version", "decision_sha256", "target", "observed_api_revision",
                "observed_api_image", "observed_worker_image", "proof",
            } and type(envelope["schema_version"]) is int and envelope["schema_version"] == 1
            and isinstance(envelope["observed_api_revision"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", envelope["observed_api_revision"]),
            "Image smoke needs the operator-verified API revision and image observations")
    published = receipt(work, "published", decision)
    image = published.get("backend_image")
    require(isinstance(image, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[a-f0-9]{64}", image)
            and envelope["observed_api_image"] == envelope["observed_worker_image"] == image
            and published.get("digest") == image.rsplit("@", 1)[1],
            "Image smoke API and worker images must match the published immutable backend")
    proof = envelope["proof"]
    require(isinstance(proof, dict) and set(proof) == {
                "schema_version", "status", "source_revision", "image_digest", "image_identity_basis",
                "code_sha256", "lock_sha256", "installed_sdk_versions", "python_version", "network",
                "blocked_operation_attempts", "native_requests", "request_basis", "authentication_performed",
                "provider_send_performed", "worker_execution_started",
            } and type(proof["schema_version"]) is int and proof["schema_version"] == 1
            and proof["status"] == "validated_no_send" and proof["source_revision"] == decision["source_revision"]
            and isinstance(proof["source_revision"], str) and re.fullmatch(r"[a-f0-9]{40}", proof["source_revision"])
            and proof["image_digest"] == published["digest"]
            and proof["image_identity_basis"] == "operator_verified_deployment_and_build_not_self_attested"
            and proof["network"] == "python_dns_socket_process_and_real_credential_operations_denied"
            and proof["request_basis"] == "complete_prepared_planning_inputs_actual_runtime_requests_self_gate"
            and all(proof[key] is False for key in (
                "authentication_performed", "provider_send_performed", "worker_execution_started"))
            and isinstance(proof["python_version"], str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", proof["python_version"]),
            "Image smoke must be the source-bound producer proof, not host/startup evidence or self-attestation")
    blocked = proof["blocked_operation_attempts"]
    require(isinstance(blocked, dict) and set(blocked) == {"network", "credential", "subprocess"}
            and all(type(value) is int and value == 0 for value in blocked.values()),
            "Image smoke attempted a forbidden operation")
    files = proof["code_sha256"]
    require(isinstance(files, dict) and IMAGE_SMOKE_FILES <= set(files),
            "Image smoke source manifest is incomplete")
    reviewed = {}
    for name, digest in files.items():
        require(isinstance(name, str) and not PurePosixPath(name).is_absolute()
                and ".." not in PurePosixPath(name).parts and str(PurePosixPath(name)) == name
                and (name in {"uv.lock", "pyproject.toml"} or name.startswith("backend/"))
                and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest),
                "Image smoke source manifest contains an invalid path or digest")
        raw = release.command(["git", "show", decision["source_revision"] + ":" + name], cwd=release.ROOT)
        require(hashlib.sha256(raw).hexdigest() == digest, "Image smoke source differs from the reviewed commit")
        reviewed[name] = raw
    require(proof["lock_sha256"] == files["uv.lock"], "Image smoke dependency-lock binding changed")
    installed = proof["installed_sdk_versions"]
    require(isinstance(installed, dict) and IMAGE_SMOKE_PACKAGES <= set(installed)
            and set(installed) <= IMAGE_SMOKE_PACKAGES | {"httpx2"},
            "Image smoke installed-package manifest is incomplete")
    packages = tomllib.loads(reviewed["uv.lock"].decode())["package"]
    require(isinstance(packages, list) and all(isinstance(entry, dict) for entry in packages),
            "Image smoke requires the reviewed target dependency lock")
    for name, version in installed.items():
        locked = [entry.get("version") for entry in packages if entry.get("name") == name]
        require(isinstance(version, str) and bool(version) and locked
                and all(isinstance(value, str) and value == version for value in locked),
                "Image-installed package differs from its reviewed target lock")
    _validate_image_requests(proof)
    return envelope


def source_boundary(revision, review):
    """Review the actual committed publication source, including authorized Track B."""
    require(isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
            and review.get("approved") is True and review.get("revision") == revision
            and review.get("workstreams") == ["track_b", "four_product", "write_schema", "pacing"],
            "Exact reviewed source and all authorized workstreams required")
    base = review["base_revision"]
    require(isinstance(base, str) and re.fullmatch(r"[a-f0-9]{40}", base),
            "Reviewed committed comparison base required")
    release.command(["git", "merge-base", "--is-ancestor", base, revision], cwd=release.ROOT)
    changed = set(release.command(["git", "diff", "--name-only", base, revision], cwd=release.ROOT).decode().splitlines())
    require(changed and changed == set(review["files"])
            and not any(name.startswith("frontend/") for name in changed),
            "Review must pin the entire changed publication source; frontend must remain unchanged")
    for name, expected in review["files"].items():
        raw = release.command(["git", "show", revision + ":" + name], cwd=release.ROOT)
        require(hashlib.sha256(raw).hexdigest() == expected, "Reviewed committed file hash changed")
    require(release.command(["git", "rev-parse", revision + "^{tree}"], cwd=release.ROOT).decode().strip()
            == review["tree"], "Reviewed publication tree changed")
    return review


def verify_running_source(revision):
    files = release.command(
        ["git", "ls-tree", "-r", "--name-only", revision, "--", "backend", "scripts", "tests"], cwd=release.ROOT,
    ).decode().splitlines()
    for name in files:
        candidate = release.ROOT / name
        require(candidate.is_file() and not candidate.is_symlink()
                and candidate.read_bytes() == release.command(["git", "show", revision + ":" + name], cwd=release.ROOT),
                "Running operator/worker differs from exact reviewed committed source")
    untracked = release.command(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "backend", "scripts", "tests"], cwd=release.ROOT,
    ).decode().strip()
    require(not untracked, "Uncommitted app/operator files cannot pass publication source verification")


def operator_revision(work, decision):
    selected = path(work, "operator-source")
    if not selected.exists():
        return decision["source_revision"]
    value = release.private_json(selected)
    require(set(value) == {"decision_sha256", "source_review", "ci", "publication_sha256", "deployment_attempt_sha256"}
            and value["decision_sha256"] == sha(decision)
            and value["publication_sha256"] == sha(receipt(work, "published", decision))
            and value["deployment_attempt_sha256"] == sha(receipt(work, "deploy-attempt", decision)),
            "Operator correction must retain the original decision, publication and deployment attempt")
    review, ci = value["source_review"], value["ci"]
    require(review["base_revision"] == decision["source_revision"]
            and set(review["files"]) <= {
                "scripts/four_product_continuation.py", "tests/test_four_product_continuation.py",
                "docs/FOUR_PRODUCT_CONTINUATION.md",
            }, "Only the reviewed deployment operator correction may differ from the published application")
    source_boundary(review["revision"], review)
    require(ci.get("revision") == review["revision"] and ci.get("merged") is True
            and ci.get("base") == "main" and ci.get("checks") == row.CI_CHECKS,
            "The deployment operator correction requires exact merged CI")
    return review["revision"]


def history(work):
    retained = {}
    for candidate in sorted(work.rglob("*.json")):
        relative = candidate.relative_to(work)
        if (relative.parts[0] == CHILD or {"source", "context"} & set(relative.parts)
                or len(relative.parts) == 1 and candidate.name.startswith(PREFIX + "-")
                and candidate.name not in RETAINED_FILES):
            continue
        require(not candidate.is_symlink() and candidate.stat().st_mode & 0o077 == 0,
                "Retained history must remain owner-only regular files")
        retained[str(relative)] = digest_file(candidate)
    require(set(RETAINED_FILES) <= set(retained), "Exact latest failure/publication receipts are required")
    return retained


def verify_history(work, retained):
    gap.verify_history(work, retained)


def binding(decision):
    return {"decision_sha256": sha(decision), "target": decision["target"]}


def preflight_recorder(work, decision, operation, request_binding):
    require(isinstance(operation, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,80}", operation)
            and isinstance(request_binding, dict) and request_binding
            and set(request_binding) <= {
                "resource_sha256", "payload_sha256", "execution_sha256", "attempt_sha256", "execution_name_sha256",
            } and all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
                      for value in request_binding.values()),
            "Native receipt operation and canonical request bindings are required")
    bound = {**binding(decision), "operation": operation, "request_binding": copy.deepcopy(request_binding),
             "binding_hash_semantics": "sha256_sorted_compact_ascii_json"}

    def record(proof):
        require(isinstance(proof, dict) and proof.get("status") == "validated" and proof.get("no_send") is True
                and proof.get("operation_stage") in {
                    "azure_write", "publication_upload_metadata", "binary_source_upload", "publication_schedule_run",
                    "worker_secret_redacted", "worker_secret_credential_bound",
                }, "Only successful staged native no-send metadata may be retained")
        stage = proof["operation_stage"]
        release.save_once(path(work, operation + "-" + stage + "-preflight"), {
            "schema_version": 1, **bound, "operation_stage": stage, "native_no_send": copy.deepcopy(proof),
        })

    return record


def receipt(work, name, decision):
    value = release.private_json(path(work, name), max_bytes=64 * 1024 * 1024)
    require(all(value.get(key) == expected for key, expected in binding(decision).items()),
            "Four-product receipt binding changed")
    return value


def evidence(work, decision):
    name = decision["gate_file"]
    require(Path(name).name == name and digest_file(work / name) == decision["gate_sha256"],
            "Private gate must remain root-confined and unchanged")
    return release.private_json(work / name, max_bytes=64 * 1024 * 1024)


def validate(work, config, decision):
    require("check_window" in inspect.signature(gap.send_existing_secret_patch).parameters,
            "Shared schema-validated secret helper must support the four-product readiness callback")
    approval, records, previous = original(work, config)
    captured_authorization = owner_authorization(work, records[f"batches/{approval['batch_id']}.json"])
    identity_authorization, role_evidence = preparation_identity_authorization(work, approval, previous)
    require(set(decision) == {
        "schema_version", "approved", "approved_by", "source_revision", "target", "baseline_sha256",
        "gate_file", "gate_sha256", "scope_sha256", "capacity_approval", "policy",
        "credential_origin", "credential_source", "webiq_secret_ref",
    } and decision.get("approved") is True and type(decision.get("schema_version")) is int
            and decision.get("schema_version") == 1
            and decision.get("approved_by") == approval["approved_by"]
            and decision.get("policy") in (POLICY, {**POLICY, "worker_executions": 2, "item_limit": 2})
            and decision.get("target") == release.fingerprint(config)
            and decision.get("credential_source") in {"owner_env_file", "existing_job_secret", "unavailable"}
            and decision.get("credential_origin") == "GBBdemo"
            and (decision.get("credential_source") == "unavailable") == (decision.get("webiq_secret_ref") is None)
            and (decision.get("webiq_secret_ref") is None or isinstance(decision["webiq_secret_ref"], str)
                 and re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?", decision["webiq_secret_ref"])),
            "Exact reviewed four-product decision required")
    baseline = release.private_json(baseline_path(work), max_bytes=64 * 1024 * 1024)
    require(digest_file(baseline_path(work)) == decision["baseline_sha256"], "Post-PREP baseline changed")
    verify_history(work, baseline["history"])
    require(baseline["source_revision"] == decision["source_revision"]
            and baseline["target"] == decision["target"]
            and baseline["work_root"] == str(work)
            and baseline["root_pin_sha256"] == digest_file(prior.ROOT_PIN)
            and baseline["owner_authorization_sha256"] == sha(captured_authorization)
            and baseline["preparation_identity_authorization_sha256"] == sha(identity_authorization)
            and baseline["existing_role_evidence_sha256"] == sha(role_evidence)
            and baseline["approval_sha256"] == sha(approval), "Source/target/approval baseline changed")
    source_boundary(decision["source_revision"], baseline["source_review"])
    verify_running_source(operator_revision(work, decision))
    child = source_work(work)
    require(not child.is_symlink() and child.resolve() == child
            and release.verify_source(child)["revision"] == decision["source_revision"],
            "Exact staged source receipt changed")
    release.require_component_source_receipts(child, "backend")
    gate = evidence(work, decision)
    require(gate.get("passed") is True and gate.get("no_live_operations") is True
            and gate.get("source_revision") == decision["source_revision"]
            and GATE_CHECKS <= set(gate.get("checks", {}))
            and all(value is True for value in gate["checks"].values())
            and gate.get("postprep_snapshot_sha256") == baseline["postprep_snapshot_sha256"]
            and gate.get("scope_sha256") == decision["scope_sha256"] == sha(gate["amendment_scope"])
            and gate.get("historical_forecasts_stacked") is False,
            "Actual post-PREP real-worker/real-guard twelve-payload private gate required")
    require(gate["amendment_scope"]["capacity_approval"] == decision["capacity_approval"],
            "Measured exact capacity approval changed")
    require(decision["policy"] == execution_policy(gate["amendment_scope"]), "Worker count must match the bound scope")
    if gate["amendment_scope"].get("worker_slices"):
        authority = release.private_json(work / CAPACITY_OWNER_AUTHORIZATION_FILE)
        require(authority == gate["amendment_scope"]["slice_authorization"]
                and sha(authority) == gate["amendment_scope"]["slice_authorization_sha256"],
                "Two-slice authority must equal the retained owner capture")
    validate_pacing(
        gate["pacing_evidence"], gate["actual_payload_plan"],
        execution_order=gate["amendment_scope"]["execution_order"],
        interval_seconds=gate["amendment_scope"]["inference_interval_seconds"],
        duration_assumptions=decision["capacity_approval"]["duration_assumptions"],
        worker_slices=gate["amendment_scope"].get("worker_slices"),
    )
    validate_provider_preflights(
        gate["provider_preflights"], gate["actual_payload_plan"], gate["amendment_scope"]["public_web_policy"],
    )
    require(instant(decision["capacity_approval"]["approved_at"]) <= now(),
            "An actual prior capacity approval is required before readiness")
    require(decision["capacity_approval"]["plan_sha256"] == sha(gate["payload_plan"]),
            "Private complete payload plan changed")
    postprep = decode_records(release.private_json(
        work / baseline["postprep_snapshot_file"], max_bytes=64 * 1024 * 1024))
    continuation_owner = verify_continued_preparation(work, postprep)
    require(baseline["continuation_owner_authorization_sha256"] == sha(continuation_owner),
            "Continuation owner authority changed after the baseline")
    from backend.real_pilot import FOUR_PRODUCT_PREP_PREFIX

    require(instant(identity_authorization["recorded_after_confirmation_at"])
            <= instant(postprep[FOUR_PRODUCT_PREP_PREFIX + "authorization.json"]["approved_at"]),
            "Narrow split-route approval must follow the explicit identity-route approval")
    require(postprep[FOUR_PRODUCT_PREP_PREFIX + "ledger-before.json"] == records["budgets/real-pilot.json"]
            and postprep[FOUR_PRODUCT_PREP_PREFIX + "packet.json"]["history_sha256"]
            == {key: sha(value) for key, value in records.items()},
            "PREP must archive the exact latest canonical pre-analysis ledger and record set")
    packet = postprep[FOUR_PRODUCT_PREP_PREFIX + "packet.json"]
    require(packet["tenant_id"] == config["tenant"] and packet["subscription_id"] == config["subscription"],
            "The PREP identity route must remain in the original target tenant and subscription")
    require(digest_file(work / baseline["postprep_snapshot_file"]) == baseline["postprep_snapshot_sha256"]
            and gate["remote_object_sha256"] == {key: sha(value) for key, value in postprep.items()},
            "Actual post-PREP snapshot or cache receipt changed")
    for key, value in records.items():
        if key != "budgets/real-pilot.json":
            require(postprep.get(key) == value, "PREP cannot alter any prior result/allowance/attempt")
    for key, value in records["budgets/real-pilot.json"]["reservations"].items():
        require(postprep["budgets/real-pilot.json"]["reservations"].get(key) == value, "PREP cannot refund reservations")
    scope = gate["amendment_scope"]
    require(amendment_scope(
        postprep, capacity_approval=decision["capacity_approval"], plan=gate["payload_plan"],
        **{key: scope[key] for key in (
            "prep_receipt_key", "public_web_policy", "web_environment", "web_prices", "page_limits",
            "fixed_cost_microdollars", "inference_interval_seconds",
        )},
        **({key: scope[key] for key in ("worker_slices", "slice_authorization")} if scope.get("worker_slices") else {}),
    ) == scope, "Private gate scope must reconstruct from exact actual post-PREP records")
    ci = release.private_json(source_work(work) / "ci.json")
    require(ci.get("revision") == decision["source_revision"] and ci.get("merged") is True
            and ci.get("base") == "main" and ci.get("checks") == row.CI_CHECKS,
            "Exact reviewed publication revision must have merged CI")
    for attempt in work.glob(PREFIX + "-*-attempt.json"):
        require(release.private_json(attempt).get("decision_sha256") == sha(decision),
                "Consumed authority cannot change or retry")
    return approval, previous


def prepare(work, config, revision, source_review, postprep_snapshot_file):
    """Local create-once baseline; intentionally does NOT stage or start a clock."""
    with release.pilot_lock(work):
        approval, records, previous = original(work, config)
        captured_authorization = owner_authorization(work, records[f"batches/{approval['batch_id']}.json"])
        identity_authorization, role_evidence = preparation_identity_authorization(work, approval, previous)
        source_boundary(revision, source_review)
        verify_running_source(revision)
        require(Path(postprep_snapshot_file).name == postprep_snapshot_file,
                "Actual post-PREP snapshot must remain in retained root")
        postprep = decode_records(release.private_json(work / postprep_snapshot_file, max_bytes=64 * 1024 * 1024))
        continuation_owner = verify_continued_preparation(work, postprep)
        release.save_once(path(work, "baseline"), {
            "source_revision": revision, "source_review": source_review, "target": release.fingerprint(config),
            "work_root": str(work), "root_pin_sha256": digest_file(prior.ROOT_PIN),
            "owner_authorization_sha256": sha(captured_authorization),
            "preparation_identity_authorization_sha256": sha(identity_authorization),
            "existing_role_evidence_sha256": sha(role_evidence),
            "continuation_owner_authorization_sha256": sha(continuation_owner),
            "approval_sha256": sha(approval), "history": history(work),
            "postprep_snapshot_file": postprep_snapshot_file,
            "postprep_snapshot_sha256": digest_file(work / postprep_snapshot_file),
        })


def ready(work, config, decision):
    with release.pilot_lock(work):
        approval, previous = validate(work, config, decision)
        require(not path(work, "closed").exists() and not path(work, "readiness").exists()
                and not list(work.glob(PREFIX + "-*-attempt.json")), "Readiness is create-once, never renewable")
        release.verify_target(previous)
        backend, _, job = mixed_resources(previous, approval)
        deployment_preflight = release.preflight_deployment(
            previous, backend, job, release.writable_execution_template(job["properties"]["template"]),
        )
        require(isinstance(deployment_preflight, dict) and deployment_preflight.get("status") == "validated"
                and deployment_preflight.get("no_send") is True,
                "Captured deployment shapes must pass native no-send before readiness")
        capacity = prior.require_model_capacity(previous, approval)
        require(capacity["tokens_per_minute"] == 30000,
                "Current deployment must retain 30000 TPM before readiness; no quota change is authorized")
        anchor = now()
        value = {**binding(decision), "readiness_at": anchor.isoformat(),
                 "publication_expires_at": (anchor + timedelta(seconds=2700)).isoformat(),
                 "overall_expires_at": (anchor + timedelta(seconds=5400)).isoformat(),
                 "model_capacity": capacity, "model_capacity_checked_at": anchor.isoformat(),
                 "quota_changed": False, "deployment_no_send": deployment_preflight,
                 "deployment_preflight_scope": "captured_shapes_only_exact_dynamic_requests_rechecked_before_send"}
        release.save_once(path(work, "readiness"), value)
        release.save_once(path(work, "readiness-pin"), {**binding(decision), "sha256": sha(value)})
        return value


def window(work, decision, phase, minimum=0):
    if phase == "cleanup":
        return {}
    require(not path(work, "closed").exists(), "Four-product authority is closed; no retry")
    value = receipt(work, "readiness", decision)
    require(receipt(work, "readiness-pin", decision)["sha256"] == sha(value), "Readiness changed")
    anchor = instant(value["readiness_at"])
    capacity = value.get("model_capacity")
    require(isinstance(capacity, dict) and capacity.get("tokens_per_minute") == 30000
            and value.get("quota_changed") is False
            and value.get("model_capacity_checked_at") is not None
            and instant(value["model_capacity_checked_at"]) == anchor,
            "Readiness lacks its current unchanged 30000-TPM metadata check")
    deployment = value.get("deployment_no_send", {})
    require(isinstance(deployment, dict) and deployment.get("status") == "validated" and deployment.get("no_send") is True,
            "Readiness lacks the native deployment no-send report")
    require(instant(value["publication_expires_at"]) == anchor + timedelta(seconds=2700)
            and instant(value["overall_expires_at"]) == anchor + timedelta(seconds=5400),
            "Readiness deadlines cannot move")
    end = instant(value["publication_expires_at" if phase == "publication" else "overall_expires_at"])
    if phase == "processing":
        end = min(end, instant(receipt(work, "deployed", decision)["processing_expires_at"]))
    current = now()
    require(anchor <= current < end and (end - current).total_seconds() >= minimum,
            f"Four-product {phase} window lacks full {minimum} seconds")
    return value


@contextmanager
def live(work, decision, phase, minimum=0):
    window(work, decision, phase, minimum)
    originals = {name: getattr(release, name) for name in ("azure", "console_code", "upload_publication_context")}

    def guarded(name, function):
        def call(*args, **kwargs):
            needed = minimum
            publication_write = phase == "publication" and (
                name == "upload_publication_context" or args[:3] == ("rest", "--method", "POST"))
            if publication_write:
                needed = max(900, needed)
            window(work, decision, phase, needed)
            if publication_write or (name in {"azure", "upload_publication_context"}
                                     and kwargs.get("before_send") is not None):
                existing_window = kwargs.get("before_send")

                def check_window():
                    if existing_window is not None:
                        existing_window()
                    window(work, decision, phase, needed)

                kwargs["before_send"] = check_window
            return function(*args, **kwargs)
        return call

    try:
        for name, function in originals.items():
            setattr(release, name, guarded(name, function))
        yield
    finally:
        for name, function in originals.items():
            setattr(release, name, function)


def decision_from_gate(work, config, gate_name, capacity_approval, *, credential_source="owner_env_file",
                       secret_ref="webiq-gbbdemo"):
    require(Path(gate_name).name == gate_name, "Private gate must remain in retained root")
    baseline = release.private_json(baseline_path(work))
    approval, _, _ = original(work, config)
    gate = release.private_json(work / gate_name, max_bytes=64 * 1024 * 1024)
    return {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
        "source_revision": baseline["source_revision"], "target": release.fingerprint(config),
        "baseline_sha256": digest_file(baseline_path(work)),
        "gate_file": gate_name, "gate_sha256": digest_file(work / gate_name),
        "scope_sha256": sha(gate["amendment_scope"]), "capacity_approval": capacity_approval,
        "policy": execution_policy(gate["amendment_scope"]), "credential_origin": "GBBdemo",
        "credential_source": credential_source, "webiq_secret_ref": secret_ref,
    }


def stage(work, config, revision):
    """Explicit local stage, separate from offline inspection/planning."""
    baseline = release.private_json(baseline_path(work))
    require(baseline["source_revision"] == revision and baseline["target"] == release.fingerprint(config),
            "Source stage must match the reviewed baseline")
    source_boundary(revision, baseline["source_review"])
    verify_running_source(revision)
    require(not source_work(work).exists(), "Source stage is create-once")
    release.stage(revision, source_work(work))
    release.build_context(source_work(work))


def remote_snapshot(work, config, decision, approval):
    gap.console(config, approval, {"objects": evidence(work, decision)["remote_object_sha256"],
                                  "absent": [KEY, AUDIT, gap.KEY, gap.AUDIT]}, '''
for key, expected in packet["objects"].items():
    assert _sha256(read_json(store, key)[0]) == expected
for key in packet["absent"]:
    try:
        store.read_bytes(key)
    except Missing:
        pass
    else:
        raise ValueError("Continuation authority already installed or consumed")
''')


def mixed_resources(config, approval):
    backend, frontend = (prior.ready(config, kind) for kind in ("backend", "frontend"))
    worker_config = {**config, "backend_image": config.get("worker_image", config["backend_image"])}
    return backend, frontend, prior.require_worker(worker_config, approval)


def publish(work, config, decision):
    with release.pilot_lock(work):
        approval, previous = validate(work, config, decision)
        require(not path(work, "backend-attempt").exists(), "Publication already attempted; no retry")
        with live(work, decision, "publication", 900):
            release.verify_target(previous)
            release.identity_contract(previous)
            backend, _, job = mixed_resources(previous, approval)
            release.require_real_pilot_off(backend)
            gap.credential_binding(job, decision, allow_install=True)
            remote_snapshot(work, previous, decision, approval)
            capacity = prior.require_model_capacity(previous, approval)
            require(capacity["tokens_per_minute"] == 30000,
                    "Four-product pacing binds the existing 30000-TPM deployment; do not change quota")
            attempt = path(work, "backend-attempt")
            release.save_once(attempt, {
                **binding(decision), "kind": "backend", "cpu": 2, "timeout_seconds": 900,
                "attempt_id": str(uuid.uuid4()), "model_capacity": capacity,
                "status": "attempted_outcome_unknown", "prior_consumed_build_usd": str(PRIOR_BUILD_USD),
            })
            limits = window(work, decision, "publication", 900)
            result = release.execute_publication(
                config, source_work(work), "backend", decision["source_revision"], attempt,
                expires=instant(limits["publication_expires_at"]), validate_upload_window=True,
                on_preflight=preflight_recorder(work, decision, "backend-publication", {
                    "attempt_sha256": sha(release.private_json(attempt)),
                }),
            )
            require(result["status"] == "Succeeded" and re.fullmatch(r"sha256:[a-f0-9]{64}", result["digest"]),
                    "The single backend build must produce an immutable digest")
            release.save_once(path(work, "published"), {
                **binding(decision), **result,
                "backend_image": config["registry"] + ".azurecr.io/docintel/backend@" + result["digest"],
                "frontend_image": previous["frontend_image"],
            })


def current_config(work, config, decision):
    published = receipt(work, "published", decision)
    return {**config, "backend_image": published["backend_image"], "worker_image": published["backend_image"],
            "frontend_image": published["frontend_image"]}


def patch(work, config, decision, label, resource, containers, *, job=False, bind_existing_secret=None):
    """Reuse authoritative write projections; never send a GET object as a PATCH."""
    from scripts.azure_write_schema import build_payload, project_component, validate_request

    phase = "cleanup" if label == "close" else "overall"
    with live(work, decision, phase):
        fresh, active = release.active_executions(config) if job else (release.app(config, config["backend"]), [])
        require(not active and fresh == resource, "Resource drift; do not overwrite")
        kind = "jobs" if job else "containerApps"
        projected = project_component(kind, "PATCH", ("properties", "template", "containers"), containers)
        properties = {"template": {"containers": projected}}
        if bind_existing_secret is not None:
            require(job and label == "deploy-worker" and decision["credential_source"] == "owner_env_file"
                    and bind_existing_secret == decision["webiq_secret_ref"]
                    and not resource["properties"]["configuration"].get("secrets"),
                    "Only the owner's existing key may bind to the existing empty manual job secret store")
            properties["configuration"] = gap.writable_job_configuration(resource["properties"]["configuration"])
            properties["configuration"]["secrets"] = [{"name": bind_existing_secret}]
        payload = build_payload(kind, "PATCH", properties=properties, **({} if job else {"location": resource["location"]}))
        url = "https://management.azure.com" + resource["id"] + "?api-version=2024-03-01"
        validate_request("PATCH", url, payload)
        candidate = path(work, label + "-patch")
        release.save_once(candidate, payload)
        record_preflight = preflight_recorder(work, decision, label, {
            "resource_sha256": sha(resource), "payload_sha256": sha(payload),
        })
        if bind_existing_secret is not None:
            release.save_once(path(work, "credential-binding"), {
                **binding(decision), "secret_ref": bind_existing_secret, "resource_id": resource["id"],
                "credential_created": False, "permissions_changed": False, "value_persisted_locally": False,
                "state": "binding_attempted_outcome_unknown", "credential_origin": "GBBdemo",
            })
            gap.send_existing_secret_patch(
                config, resource, payload, secret_ref=decision["webiq_secret_ref"],
                check_window=lambda: window(work, decision, "overall", 600),
                on_preflight=record_preflight,
            )
        else:
            arguments = ["rest", "--method", "PATCH", "--url", url, "--body", "@" + str(candidate)]
            if resource.get("etag"):
                arguments.extend(("--headers", "If-Match=" + resource["etag"]))
            release.azure(*arguments, before_send=lambda: window(work, decision, phase), on_preflight=record_preflight)
        expected = prior.shape(resource)
        expected["template"]["containers"] = projected
        if bind_existing_secret is not None:
            expected["configuration"]["secrets"] = [{"name": bind_existing_secret}]
        end = monotonic() + 180
        while True:
            actual, active = release.active_executions(config) if job else (release.app(config, config["backend"]), [])
            require(not active, "Worker became active during mutation")
            if prior.shape(actual) == expected:
                return
            require(prior.shape(actual) == prior.shape(resource) and monotonic() < end,
                    "Write not confirmed; inspect the same attempt, never resubmit")
            sleep(5)


def deploy(work, config, decision, *, continue_before_write=False):
    with release.pilot_lock(work), live(work, decision, "overall", 600):
        approval, previous = validate(work, config, decision)
        current = current_config(work, config, decision)
        backend, frontend, job = mixed_resources(previous, approval)
        release.require_real_pilot_off(backend)
        gap.credential_binding(job, decision, allow_install=True)
        remote_snapshot(work, previous, decision, approval)
        captured = {
            **binding(decision), "backend": prior.shape(backend), "frontend": prior.shape(frontend), "worker": prior.shape(job),
        }
        if continue_before_write:
            require(path(work, "operator-source").exists()
                    and receipt(work, "deploy-attempt", decision) == captured,
                    "Pre-write continuation requires the reviewed correction and unchanged original resources")
            require(not any(path(work, name).exists() for name in (
                "deploy-backend-patch", "deploy-worker-patch", "credential-binding", "deployed",
            )) and not list(work.glob(PREFIX + "-deploy-*-preflight.json")),
                    "A submitted or outcome-unknown deployment cannot be retried")
            release.save_once(path(work, "deployment-continuation-attempt"), {
                **binding(decision), "original_attempt_sha256": sha(captured),
                "operator_source_sha256": digest_file(path(work, "operator-source")),
                "readiness_sha256": sha(receipt(work, "readiness", decision)),
                "reason": "canonical_container_projection_failed_before_any_write",
            })
        else:
            require(not path(work, "deploy-attempt").exists(), "Deployment already attempted")
            release.save_once(path(work, "deploy-attempt"), captured)
        for label, resource, is_job in (("backend", backend, False), ("worker", job, True)):
            containers = release.writable_containers(release.safe_containers(resource))
            containers[0]["image"] = current["backend_image"]
            secret = (decision["webiq_secret_ref"] if is_job and decision["credential_source"] == "owner_env_file"
                      and not resource["properties"]["configuration"].get("secrets") else None)
            patch(work, previous, decision, "deploy-" + label, resource, containers, job=is_job, bind_existing_secret=secret)
        _, actual_frontend, actual_job = mixed_resources(current, approval)
        gap.credential_binding(actual_job, decision)
        require(prior.shape(actual_frontend) == prior.shape(frontend), "Frontend changed during backend-only continuation")
        if path(work, "credential-binding").exists():
            release.save_once(path(work, "credential-bound"), {
                **binding(decision), "secret_ref": decision["webiq_secret_ref"], "verified_name_only": True,
            })
        anchor = now()
        end = min(anchor + timedelta(seconds=1200), instant(window(work, decision, "overall")["overall_expires_at"]))
        require((end - anchor).total_seconds() >= 600, "Full worker window no longer fits")
        release.save_once(path(work, "deployed"), {
            **binding(decision), "both_ready_at": anchor.isoformat(), "processing_expires_at": end.isoformat(),
        })


def continue_deploy(work, config, decision):
    return deploy(work, config, decision, continue_before_write=True)


def amendment(work, decision):
    ready_receipt, deployed = receipt(work, "readiness", decision), receipt(work, "deployed", decision)
    return {**evidence(work, decision)["amendment_scope"], "readiness_at": ready_receipt["readiness_at"],
            "operating_expires_at": ready_receipt["overall_expires_at"],
            "not_before": deployed["both_ready_at"], "expires_at": deployed["processing_expires_at"]}


def verify_auth(work, decision, candidate):
    value = receipt(work, "auth-preworker", decision)
    gate = evidence(work, decision)
    require(value.get("verified") is True and value.get("authenticated") is True
            and value.get("amendment_sha256") == sha(candidate)
            and value.get("backend_image") == receipt(work, "published", decision)["backend_image"]
            and value.get("owner") == gate["owner"] and value.get("batch_id") == gate["batch_id"]
            and instant(candidate["not_before"]) <= instant(value["observed_at"]) <= now(),
            "Externally supplied authenticated owner/batch/image/amendment receipt required")


def active_target(work, config, decision):
    approval, _ = validate(work, config, decision)
    current = current_config(work, config, decision)
    backend, frontend, job = mixed_resources(current, approval)
    baseline = receipt(work, "deploy-attempt", decision)
    for kind, actual in (("backend", backend), ("frontend", frontend), ("worker", job)):
        expected = copy.deepcopy(baseline[kind])
        if kind != "frontend":
            expected["template"]["containers"][0]["image"] = current["backend_image"]
        if kind == "worker" and path(work, "credential-binding").exists():
            require(receipt(work, "credential-bound", decision)["secret_ref"] == decision["webiq_secret_ref"],
                    "Existing owner key binding not confirmed")
            expected["configuration"]["secrets"] = [{"name": decision["webiq_secret_ref"]}]
        if kind == "backend" and path(work, "enablement").exists():
            entries = release.environment_entries(expected["template"]["containers"][0])
            entries.update(receipt(work, "enablement", decision)["intended"])
            expected["template"]["containers"][0]["env"] = list(entries.values())
        require(prior.shape(actual) == expected, "Deployed resource drift before activation")
    return current


def configuration_packet(approval, candidate):
    """Build and bound the exact metadata console frame without any live call."""
    packet = {"approval_sha256": sha(approval), "batch_id": approval["batch_id"], "amendment": candidate}
    body = f'''from backend.real_pilot import validate_four_product
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
assert _sha256(approval) == packet["approval_sha256"]
os.environ["DOCINTEL_REAL_PILOT_OPERATOR_IDS"] = approval["approved_by"]
batch_key = "batches/" + packet["batch_id"] + ".json"
with store.lease(packet["batch_id"]) as batch_fence, store.lease(BUDGET_KEY) as budget_fence:
    batch, version = read_json(store, batch_key)
    ledger, _ = read_json(store, BUDGET_KEY)
    assert validate_four_product(packet["amendment"], approval, batch, ledger, store) == packet["amendment"]
    assert batch["state"] in ("completed", "deferred")
    batch_fence()
    budget_fence()
    write_json(store, {KEY!r}, packet["amendment"])
    batch["state"] = "queued"
    batch_fence()
    budget_fence()
    write_json(store, batch_key, batch, version)
'''
    row.remote_packet(packet, body)
    return packet, body


def configure(work, config, decision):
    approval, _ = validate(work, config, decision)
    candidate = amendment(work, decision)
    verify_auth(work, decision, candidate)
    packet, body = configuration_packet(approval, candidate)
    release.save_once(path(work, "configure-attempt"), {**binding(decision), "amendment": candidate})
    gap.console(current_config(work, config, decision), approval, packet, body)
    release.save_once(path(work, "configured"), {**binding(decision), "amendment_sha256": sha(candidate)})


def enable(work, config, decision):
    approval, _ = validate(work, config, decision)
    config = current_config(work, config, decision)
    backend = prior.ready(config, "backend")
    release.require_real_pilot_off(backend)
    containers = release.safe_containers(backend)
    entries = release.environment_entries(containers[0])
    intended = release.pilot_values(approval)
    release.save_once(path(work, "enablement"), {
        **binding(decision), "baseline": {name: entries.get(name) for name in intended},
        "intended": {name: {"name": name, "value": value} for name, value in intended.items()},
    })
    release.merge_environment(containers[0], intended)
    patch(work, config, decision, "enable", backend, containers)
    prior.ready(config, "backend")


def worker_receipt_name(name, slice_index=None):
    require(slice_index is None or type(slice_index) is int and slice_index in (0, 1), "Only the two distinct worker slices")
    return "worker-" + (f"slice-{slice_index + 1}-" if slice_index is not None else "") + name


def start(work, config, decision, *, slice_index=None):
    approval, _ = validate(work, config, decision)
    current = active_target(work, config, decision)
    candidate = amendment(work, decision)
    sliced = candidate.get("worker_slices") is not None
    require(sliced == (slice_index is not None), "Start must select the exact authorized slice mode")
    name = lambda suffix: worker_receipt_name(suffix, slice_index)
    verify_auth(work, decision, candidate)
    receipt(work, "configured", decision)
    require(not path(work, name("attempt")).exists(), "Worker slice authority consumed; no retry")
    if slice_index == 1:
        previous = receipt(work, worker_receipt_name("terminal", 0), decision)
        require(previous["properties"]["status"] == "Succeeded" and previous.get("slice_index") == 0,
                "Second slice requires the first Azure execution's terminal success")
        terminal_key = f"operations/real-pilot-four-product-slices/{sha(candidate)}/slice-1/completed.json"
        output = gap.console(current, approval, {
            "key": terminal_key, "amendment_sha256": sha(candidate), "batch_id": approval["batch_id"],
            "item_keys": candidate["worker_slices"][0],
        }, '''
terminal = read_json(store, packet["key"])[0]
assert terminal["status"] == "succeeded" and terminal["slice_index"] == 0
assert terminal["amendment_sha256"] == packet["amendment_sha256"] and terminal["item_keys"] == packet["item_keys"]
assert terminal["ledger"] == read_json(store, BUDGET_KEY)[0]
for item in packet["item_keys"]:
    state = read_json(store, f'items/{packet["batch_id"]}/{item}.json')[0]
    assert state["state"] in {"completed", "unresolved"}
    assert _sha256(state) == terminal["item_sha256"][item]
for key, expected in terminal["result_sha256"].items():
    assert _sha256(read_json(store, key)[0]) == expected
print("FOUR_PRODUCT_SLICE_TERMINAL=" + json.dumps({"terminal_sha256": _sha256(terminal)}))
''')
        matches = [line.removeprefix("FOUR_PRODUCT_SLICE_TERMINAL=") for line in output.decode().splitlines()
                   if line.startswith("FOUR_PRODUCT_SLICE_TERMINAL=")]
        require(len(matches) == 1, "First slice worker-terminal success must precede the second native start")
        release.save_once(path(work, worker_receipt_name("guard-terminal", 0)),
                          {**binding(decision), **json.loads(matches[0]), "terminal_key": terminal_key})
    job = prior.require_worker(current, approval)
    template = copy.deepcopy(job["properties"]["template"])
    container = template["containers"][0]
    container["command"] = ["/app/.venv/bin/python"]
    container["args"] = ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", approval["batch_id"],
                         "--concurrency", "1", "--max-batches", "1", "--item-limit", "2" if sliced else "4"]
    release.merge_environment(container, {
        **approval["environment"], **release.pilot_values(approval), **candidate["web_environment"],
        "DOCINTEL_REAL_PILOT_EXECUTION_SCOPE": "full", "DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED": "true",
        "DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON": json.dumps(candidate["public_web_policy"], sort_keys=True),
    })
    entries = release.environment_entries(container)
    entries.pop("WEBIQ_SUBSCRIPTION_KEY", None)
    credential = gap.credential_binding(job, decision)
    if credential is None:
        entries.pop("WEBIQ_API_KEY", None)
    else:
        entries["WEBIQ_API_KEY"] = credential
    container["env"] = list(entries.values())
    template = release.writable_execution_template(template)
    capacity = prior.require_model_capacity(current, approval)
    require(capacity["tokens_per_minute"] == 30000,
            "Four-product pacing binds the existing 30000-TPM deployment; do not change quota")
    fresh, active = release.active_executions(current)
    require(not active and fresh == job, "Manual job changed or is active")
    release.save_once(path(work, name("template")), template)
    release.save_once(path(work, name("attempt")), {
        **binding(decision), "attempt": 5 + (slice_index or 0), "attempted_at": now().isoformat(),
        "amendment_sha256": sha(candidate), "execution_sha256": sha(template), "model_capacity": capacity,
        "optional_web_credential_available": credential is not None, "status": "start_attempted_completion_unknown",
        **({"slice_index": slice_index, "item_keys": candidate["worker_slices"][slice_index]} if sliced else {}),
    })
    window(work, decision, "processing", 600)
    result = release.azure("containerapp", "job", "start", "--subscription", config["subscription"],
                           "-g", config["group"], "-n", config["job"], "--yaml", str(path(work, name("template"))),
                           resource_snapshot=job, before_send=lambda: window(work, decision, "processing", 600),
                           on_preflight=preflight_recorder(work, decision, name("start"), {
                               "resource_sha256": sha(job), "execution_sha256": sha(template),
                           }))
    require(isinstance(result, dict) and isinstance(result.get("name"), str) and result["name"],
            "Worker outcome unknown; never repeat the start request")
    release.save_once(path(work, name("result")), {
        **binding(decision), "execution_name": result["name"], "attempt": 5 + (slice_index or 0),
        **({"slice_index": slice_index} if sliced else {}),
    })


def usage(work, config, decision, *, timeout=30, slice_index=None):
    """An advisory read. Failure never stops or restarts the bounded worker."""
    candidate = amendment(work, decision)
    approval, _, _ = original(work, config)
    packet = {"amendment_sha256": sha(candidate), "approval_sha256": sha(approval)}
    output = gap.console(current_config(work, config, decision), approval, packet, '''
ledger, _ = read_json(store, BUDGET_KEY)
assert ledger["approval_sha256"] == packet["approval_sha256"]
entry = ledger["four_product"]
assert entry["sha256"] == packet["amendment_sha256"]
value = {"inference": ledger["attempted"]["inference"] - entry["inference_before"],
         "input_tokens": ledger["reserved"]["input_tokens"] - entry["input_before"],
         "output_tokens": ledger["reserved"]["output_tokens"] - entry["output_before"],
         "reserved_microdollars": ledger["reserved"]["microdollars"] - entry["cost_before"]}
print("DOCINTEL_FOUR_USAGE:" + base64.b64encode(json.dumps(value).encode()).decode())
''', timeout=timeout)
    lines = [line.removeprefix("DOCINTEL_FOUR_USAGE:") for line in output.decode().splitlines()
             if line.startswith("DOCINTEL_FOUR_USAGE:")]
    require(len(lines) == 1, "Usage probe unavailable")
    value = json.loads(base64.b64decode(lines[0], validate=True))
    release.save_once(path(work, ("slice-" + str(slice_index + 1) + "-" if slice_index is not None else "")
                           + "usage-" + uuid.uuid4().hex), {
        **binding(decision), **value, "observed_at": now().isoformat(), "advisory_only": True,
        "fixed_cost_microdollars": candidate["fixed_cost_microdollars"], "historical_forecasts_stacked": False,
    })
    return value


def observe(work, config, decision, *, slice_index=None):
    name = lambda suffix: worker_receipt_name(suffix, slice_index)
    execution = receipt(work, name("result"), decision)["execution_name"]
    end = instant(amendment(work, decision)["expires_at"])
    if slice_index is not None:
        end = min(end, instant(receipt(work, name("attempt"), decision)["attempted_at"]) + timedelta(seconds=600))
    while now() < end:
        properties = None
        try:
            values = release.azure(
                "containerapp", "job", "execution", "list", "--subscription", config["subscription"],
                "-g", config["group"], "-n", config["job"], timeout=min(30, (end - now()).total_seconds()),
            )
            matches = [value for value in values if value.get("name") == execution]
            require(len(matches) == 1, "Reserved execution status unavailable")
            properties = matches[0]["properties"]
            usage(work, config, decision, timeout=min(30, max(1, (end - now()).total_seconds())),
                  **({"slice_index": slice_index} if slice_index is not None else {}))
        except Exception:
            release.save_once(path(work, "warning-" + uuid.uuid4().hex), {
                **binding(decision), "reason": "advisory_probe_unavailable",
                "action": "continue_without_retry_or_stop", "observed_at": now().isoformat(),
            })
        if properties and properties["status"] in release.TERMINAL:
            release.save_once(path(work, name("terminal")), {
                **binding(decision), "execution_name": execution, "properties": properties,
                **({"slice_index": slice_index} if slice_index is not None else {}),
            })
            require(properties["status"] == "Succeeded", "Worker failed; no retry")
            return
        sleep(min(15, max(0, (end - now()).total_seconds())))
    stop_name = "expiry-stop" if slice_index is None else name("expiry-stop")
    release.save_once(path(work, stop_name + "-attempt"), {**binding(decision), "execution_name": execution})
    release.azure("containerapp", "job", "stop", "--subscription", config["subscription"],
                  "-g", config["group"], "-n", config["job"], "--job-execution-name", execution,
                  on_preflight=preflight_recorder(work, decision, stop_name, {
                      "attempt_sha256": sha(receipt(work, stop_name + "-attempt", decision)),
                      "execution_name_sha256": sha(execution),
                  }))
    raise ValueError("Authority expired; only the reserved execution was stopped")


def close(work, config, decision):
    """Closure is independent of CI, private gate and the expired readiness clock."""
    with release.pilot_lock(work), live(work, decision, "cleanup"):
        backend = release.app(config, config["backend"])
        if path(work, "enablement").exists():
            enabled = receipt(work, "enablement", decision)
            containers = release.safe_containers(backend)
            entries = release.environment_entries(containers[0])
            actual = {name: entries.get(name) for name in enabled["baseline"]}
            require(actual in (enabled["baseline"], enabled["intended"]), "Unexpected enablement drift during closure")
            if actual != enabled["baseline"]:
                release.save_once(path(work, "close-attempt"), binding(decision))
                for name, value in enabled["baseline"].items():
                    if value is None:
                        entries.pop(name, None)
                    else:
                        entries[name] = value
                containers[0]["env"] = list(entries.values())
                patch(work, config, decision, "close", backend, containers)
        release.require_real_pilot_off(release.app(config, config["backend"]))
        if path(work, "deploy-attempt").exists():
            require(prior.shape(release.app(config, config["frontend"]))
                    == receipt(work, "deploy-attempt", decision)["frontend"], "Frontend drift at closure")
        if not path(work, "closed").exists():
            release.save_once(path(work, "closed"), binding(decision))


def activate(work, config, decision):
    with release.pilot_lock(work):
        validate(work, config, decision)
        image_smoke = validate_image_smoke(work, decision)
        window(work, decision, "processing", 600)
        release.save_once(path(work, "activation-attempt"), {
            **binding(decision), "image_sdk_smoke_sha256": sha(image_smoke),
        })
    try:
        with live(work, decision, "processing", 600):
            active_target(work, config, decision)
            configure(work, config, decision)
            enable(work, config, decision)
            if decision.get("policy", {}).get("worker_executions") != 2:
                start(work, config, decision)
        if decision.get("policy", {}).get("worker_executions") == 2:
            for index in range(2):
                with live(work, decision, "processing", 600):
                    start(work, config, decision, slice_index=index)
                observe(work, config, decision, slice_index=index)
        else:
            observe(work, config, decision)
    finally:
        close(work, config, decision)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "stage", "check", "ready", "publish", "deploy", "continue-deploy", "activate", "close"))
    parser.add_argument("--work", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--source-review", type=Path)
    parser.add_argument("--postprep-snapshot")
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--approve")
    options = parser.parse_args()
    work = prior.root(options.work)
    config = release.load_config(options.config or work / "target.json")
    if options.action == "prepare":
        prepare(work, config, options.revision, release.private_json(options.source_review), options.postprep_snapshot)
    elif options.action == "stage":
        require(options.approve == "stage", "Explicit source-stage approval required")
        stage(work, config, options.revision)
    else:
        decision = release.private_json(options.decision)
        if options.action == "check":
            validate(work, config, decision)
        else:
            require(options.approve == options.action, "Explicit bounded action approval required")
            globals()[options.action.replace("-", "_")](work, config, decision)
    print("Four-product action confirmed; retained approvals, attempts and charges preserved.")


if __name__ == "__main__":
    main()
