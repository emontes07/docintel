"""Single-use Ford layout PREPARATION, before readiness, never a worker execution.

The local plan command is offline. Only the parent/operator may run execute:
the API identity handles storage; the existing operator analyzes the local PDF.
No deployment, permission change, worker start, model or WebIQ call is available.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

from backend.batch_store import Missing, read_json, write_json
from backend.batch_worker import BatchProcessor
from backend.core.docintel import ParsedDocument
from backend.pilot import PARSER_VERSION
from backend.real_pilot import APPROVAL_KEY, BUDGET_KEY, RealPilotGuard


SOURCE_SHA256 = "b50c311840c19d96fd994a2a8f281f243c37e63257aad81c41821e0df6910cfa"
API_VERSION = "2024-11-30"
REQUEST_OPTIONS = {"pages": "1-5"}
SOURCE_ID = "av-source-4"
SOURCE_KEY = "documents/av-source-4.pdf"
LOCATION = "batchblob:///" + SOURCE_KEY
PRODUCTS = {"PIMITEM-225830": "AV11-333W-NL", "PIMITEM-221315": "AV11-444W-NL"}
PREFIX = "operations/ford-analysis-preparation/" + SOURCE_SHA256 + "/"
ABSENT_ACTIVATIONS = ("configuration/real-pilot-gapfill.json", "operations/real-pilot-gapfill.json")
CONTRACT = {
    "purpose": "one_ford_analysis_preparation_before_readiness",
    "source_id": SOURCE_ID, "source_key": SOURCE_KEY, "source_location": LOCATION,
    "source_sha256": SOURCE_SHA256, "products": PRODUCTS, "model": "prebuilt-layout",
    "api_version": API_VERSION, "parser_version": "prebuilt-layout:2024-11-30:mapping-v1",
    "request_options": REQUEST_OPTIONS, "max_submissions": 1, "max_analysis_pages": 5, "sdk_retries": 0,
    "worker_executions": 0, "inference": 0, "search": 0, "web_retrieval": 0,
    "timing_exception": "recorded_preparation_before_any_readiness_clock",
    "identity_route": "local_operator_di_api_mi_storage",
}
EVENT_FIELDS = {
    "model_requested", "api_version_requested", "request_options", "sdk_retries",
    "sdk_package", "sdk_version", "source_sha256", "source_bytes",
    "started_at", "operation_id", "outcome", "accepted_at", "analysis_identity",
}
RESULT_FIELDS = {
    "completed_at", "model_returned", "api_version_returned", "actual_page_count", "returned_pages",
    "sdk_result_sha256", "sdk_result_serialization", "sdk_result_bytes", "raw_http_response_retained",
    "source", "parser_version", "mapped_at", "mapped_result_sha256", "cache_key",
    "preparation_authorization_sha256", "reservation_id", "fresh_analysis",
    "legacy_local_parse_reused", "readiness_clock_started", "storage_identity", "input_provenance",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def record_sha(raw):
    return sha(canonical(json.loads(raw)))


def now():
    return datetime.now(timezone.utc).isoformat()


def cache_keys():
    return tuple("parses/" + sha((LOCATION + SOURCE_SHA256 + PARSER_VERSION + suffix).encode()) + ".json"
                 for suffix in ("", ":pages=1-5"))


def absent(store, key):
    try:
        store.read_bytes(key)
    except Missing:
        return
    raise ValueError("Preparation exists; never retry")


def selected_sources(batch):
    selected = []
    for item in batch["items"]:
        product = item["manifest"]["product"]
        if product["item_id"] not in PRODUCTS:
            continue
        require(product["mpn"] == PRODUCTS[product["item_id"]], "Ford product association changed")
        sources = [entry for entry in item["sources"] if entry["source_id"] == SOURCE_ID]
        require(len(sources) == 1, "Exactly one approved Ford PDF binding required")
        binding = sources[0]
        require(all(binding.get(key) == value for key, value in {
            "kind": "blob", "format": "pdf", "source_tier": "internal_pdf",
            "blob": SOURCE_KEY, "sha256": SOURCE_SHA256,
        }.items()), "Ford source bytes/binding changed")
        selected.append((item, binding))
    require(len(selected) == 2 and {item["manifest"]["product"]["item_id"] for item, _ in selected} == set(PRODUCTS),
            "Exact Ford products required")
    return selected


def verify_authorization(packet, authorization, original):
    require(set(authorization) == {
        "schema_version", "approved", "approved_by", "approved_at", "packet_sha256",
        "contract", "parent_live_executor_only", "existing_operator_di_access_verified",
        "operator_analysis_api_storage_approved",
    }, "Invalid preparation approval")
    require(authorization["schema_version"] == 1 and authorization["approved"] is True
            and authorization["parent_live_executor_only"] is True
            and authorization["existing_operator_di_access_verified"] is True
            and authorization["operator_analysis_api_storage_approved"] is True,
            "Explicit split-route approval required")
    require(authorization["approved_by"] == original["approved_by"]
            and authorization["packet_sha256"] == sha(canonical(packet))
            and canonical(authorization["contract"]) == canonical(CONTRACT), "Preparation approval binding changed")
    require(packet["analysis_identity"] == {
        "kind": "approved_operator", "credential": "AzureCliCredential",
        "principal_id": original["approved_by"], "tenant_id": packet["tenant_id"],
        "subscription_id": packet["subscription_id"],
    } and packet["storage_identity"] == {
        "kind": "api_system_assigned", "credential": "ManagedIdentityCredential",
        "principal_id": original["identities"]["api_principal_id"], "tenant_id": packet["tenant_id"],
    }, "Operator/API identity mismatch")
    approved_at = datetime.fromisoformat(authorization["approved_at"])
    require(approved_at.tzinfo is not None and approved_at <= datetime.now(timezone.utc),
            "An actual timezone-qualified preparation authorization timestamp is required")


def capacity(approval, ledger):
    require(ledger.get("invalidated") is None, "Ledger invalidated")
    require(ledger["approval_id"] == approval["id"] and ledger["approval_sha256"] == sha(canonical(approval)),
            "Approval/ledger drift")
    for field, amount, maximum in (("analysis", 1, 2), ("analysis_pages", 5, 10)):
        used = ledger["attempted"][field] if field == "analysis" else ledger["reserved"][field]
        limit = approval["limits"][field]
        require(type(used) is int and used >= 0 and type(limit) is int and used + amount <= limit <= maximum,
                "Existing analysis/page capacity exhausted")
    cost = RealPilotGuard._cost(approval, "analysis", 0, 0, 5)
    require(0 < cost <= 50000 and ledger["reserved"]["microdollars"] + cost <= approval["limits"]["spend_microdollars"],
            "Existing price/spend ceiling exceeded")
    return cost


def claim_preparation(store, packet, authorization, *, runtime_check):
    """API storage phase: verify Blob bytes, archive and charge before local DI."""
    runtime_check()
    require(canonical(packet["contract"]) == canonical(CONTRACT)
            and PARSER_VERSION == CONTRACT["parser_version"], "Parser contract drift")
    require(packet["history_hash_semantics"] == "sha256_sorted_compact_ascii_json", "Canonical history pins required")
    for key, expected in packet["history_sha256"].items():
        require(record_sha(store.read_bytes(key)[0]) == expected, "History drift")
    original, _ = read_json(store, APPROVAL_KEY)
    verify_authorization(packet, authorization, original)
    batch, _ = read_json(store, "batches/" + original["batch_id"] + ".json")
    selected = selected_sources(batch)
    require(batch["owner"] == original["owner"] and batch["id"] == packet["batch_id"], "Owner/batch mismatch")
    content, source_version = store.read_bytes(SOURCE_KEY, max_bytes=10 * 1024 * 1024)
    require(sha(content) == SOURCE_SHA256 and content.startswith(b"%PDF-") and b"%%EOF" in content[-1024:],
            "Blob content mismatch")
    operation_key = sha(canonical({"purpose": CONTRACT["purpose"], "source": CONTRACT, "batch_id": batch["id"]}))
    reservation_id = sha(canonical({"operation": "analysis", "operation_key": operation_key}))
    with store.lease(batch["id"]), store.lease(BUDGET_KEY):
        runtime_check()
        for key, expected in packet["history_sha256"].items():
            require(record_sha(store.read_bytes(key)[0]) == expected, "History drift")
        for key in (*ABSENT_ACTIVATIONS, *cache_keys(), PREFIX + "attempt.json"):
            absent(store, key)
        ledger_raw, version = store.read_bytes(BUDGET_KEY)
        ledger = json.loads(ledger_raw)
        cost = capacity(original, ledger)
        require(reservation_id not in ledger["reservations"], "Reservation exists")
        audit = {
            "contract": CONTRACT, "packet_sha256": sha(canonical(packet)),
            "authorization_sha256": sha(canonical(authorization)), "reserved_at": now(),
            "source_etag": source_version, "source_bytes": len(content),
            "ledger_before_sha256": sha(ledger_raw), "historical_record_sha256": packet["history_sha256"],
            "ledger_before_canonical_sha256": record_sha(ledger_raw),
            "historical_record_hash_semantics": "sha256_sorted_compact_ascii_json",
            "reservation_id": reservation_id, "reserved_microdollars": cost,
            "state": "attempt_claimed_no_retry", "readiness_clock_started": False,
        }
        write_json(store, PREFIX + "attempt.json", audit)
        write_json(store, PREFIX + "authorization.json", authorization)
        store.write_bytes(PREFIX + "ledger-before.json", ledger_raw)
        usage = {"input_tokens": 0, "output_tokens": 0, "analysis_pages": 5}
        reservation = {
            "reservation_id": reservation_id, "approval_id": original["id"], "execution_id": None,
            "operation": "analysis", "operation_key_sha256": operation_key,
            "item_key": selected[0][0]["item_key"], "item_keys": [item["item_key"] for item, _ in selected],
            "purpose": CONTRACT["purpose"], "preparation_authorization_sha256": sha(canonical(authorization)),
            "reserved_at": audit["reserved_at"], "reserved_usage": usage,
            "reserved_microdollars": cost, "actual_usage": None, "estimated_usage_cost_microdollars": None,
            "status": "attempt_reserved_completion_unknown",
        }
        ledger["attempted"]["analysis"] += 1
        ledger["reserved"]["analysis_pages"] += 5
        ledger["reserved"]["microdollars"] += cost
        ledger["reservations"][reservation_id] = reservation
        write_json(store, BUDGET_KEY, ledger, version)
        claim = {
            "packet_sha256": sha(canonical(packet)), "authorization_sha256": sha(canonical(authorization)),
            "reservation_id": reservation_id, "reserved_microdollars": cost,
            "reserved_ledger_canonical_sha256": sha(canonical(ledger)),
            "source_sha256": SOURCE_SHA256, "source_bytes": len(content), "source_etag": source_version,
            "blob_verified_at": now(), "analysis_identity": packet["analysis_identity"],
            "storage_identity": packet["storage_identity"], "readiness_clock_started": False,
        }
        write_json(store, PREFIX + "reserved.json", claim)
    return claim


def check_claim(store, packet, authorization, claim_sha256):
    original, _ = read_json(store, APPROVAL_KEY)
    verify_authorization(packet, authorization, original)
    claim, _ = read_json(store, PREFIX + "reserved.json")
    require(sha(canonical(claim)) == claim_sha256
            and claim["packet_sha256"] == sha(canonical(packet))
            and claim["authorization_sha256"] == sha(canonical(authorization)), "Preparation claim mismatch")
    require(record_sha(store.read_bytes(BUDGET_KEY)[0]) == claim["reserved_ledger_canonical_sha256"],
            "Reservation drift")
    for key, expected in packet["history_sha256"].items():
        if key != BUDGET_KEY:
            require(record_sha(store.read_bytes(key)[0]) == expected, "History drift")
    absent(store, PREFIX + "completed.json")
    absent(store, PREFIX + "failure.json")
    return original, claim


def validate_event(event, packet, claim):
    require(set(event) == EVENT_FIELDS, "Submitted metadata rejected")
    require(event["model_requested"] == "prebuilt-layout" and event["api_version_requested"] == API_VERSION
            and event["request_options"] == REQUEST_OPTIONS and event["sdk_retries"] == 0
            and event["source_sha256"] == SOURCE_SHA256 and event["source_bytes"] == claim["source_bytes"]
            and event["outcome"] == "submission_outcome_unknown"
            and re.fullmatch(r"[a-f0-9-]{36}", event["operation_id"]), "Submitted operation contract mismatch")
    identity = event["analysis_identity"]
    require(set(identity) == set(packet["analysis_identity"]) | {
        "token_audience", "identity_checked_at", "sdk_token_identity_matched",
        "token_signature_validated_locally", "token_stored",
    }, "Unexpected credential metadata")
    require(all(identity.get(key) == value for key, value in packet["analysis_identity"].items())
            and identity["sdk_token_identity_matched"] is True
            and identity["token_signature_validated_locally"] is False and identity["token_stored"] is False
            and identity["token_audience"] in ("https://cognitiveservices.azure.com", "https://cognitiveservices.azure.com/"),
            "Operator identity mismatch")
    require(event["sdk_package"] == "azure-ai-documentintelligence"
            and re.fullmatch(r"[0-9.]+[a-z0-9.]{0,20}", event["sdk_version"]), "SDK version metadata rejected")
    for timestamp in (event["started_at"], event["accepted_at"], identity["identity_checked_at"]):
        require(len(timestamp) <= 40 and datetime.fromisoformat(timestamp).tzinfo is not None, "Timestamp rejected")


def validate_wire_receipt(receipt, packet):
    require(set(receipt) == EVENT_FIELDS | RESULT_FIELDS, "Unexpected analysis metadata")
    event = {key: receipt[key] for key in EVENT_FIELDS}
    event["outcome"] = "submission_outcome_unknown"
    validate_event(event, packet, {"source_bytes": packet["local_pdf"]["bytes"]})
    require(receipt["outcome"] == "succeeded" and receipt["model_returned"] == "prebuilt-layout"
            and receipt["api_version_returned"] == API_VERSION
            and type(receipt["actual_page_count"]) is int and 1 <= receipt["actual_page_count"] <= 5
            and receipt["returned_pages"] == list(range(1, receipt["actual_page_count"] + 1))
            and receipt["source"] == LOCATION and receipt["parser_version"] == PARSER_VERSION
            and receipt["cache_key"] == cache_keys()[1]
            and receipt["storage_identity"] == packet["storage_identity"]
            and receipt["sdk_result_serialization"] == "AnalyzeResult.as_dict; sorted ASCII JSON; compact separators"
            and type(receipt["sdk_result_bytes"]) is int and receipt["sdk_result_bytes"] > 0
            and receipt["fresh_analysis"] is True and receipt["legacy_local_parse_reused"] is False
            and receipt["readiness_clock_started"] is False and receipt["raw_http_response_retained"] is False,
            "Unexpected result metadata")
    for field in ("sdk_result_sha256", "mapped_result_sha256", "preparation_authorization_sha256", "reservation_id"):
        require(re.fullmatch(r"[a-f0-9]{64}", receipt[field]), "Result digest rejected")
    source = receipt["input_provenance"]
    require(set(source) == {"kind", "local_path", "blob_source", "sha256", "bytes", "blob_etag", "blob_verified_at"}
            and source["kind"] == "approved_local_copy_equal_to_verified_blob"
            and source["local_path"] == packet["local_pdf"]["path"]
            and source["blob_source"] == LOCATION and source["sha256"] == SOURCE_SHA256
            and source["bytes"] == packet["local_pdf"]["bytes"]
            and re.fullmatch(r'["A-Za-z0-9_-]{1,256}', source["blob_etag"]), "Input metadata rejected")
    for timestamp in (receipt["completed_at"], receipt["mapped_at"], source["blob_verified_at"]):
        require(len(timestamp) <= 40 and datetime.fromisoformat(timestamp).tzinfo is not None, "Timestamp rejected")


def persist_submitted(store, packet, authorization, payload, *, runtime_check):
    runtime_check()
    _, claim = check_claim(store, packet, authorization, payload["claim_sha256"])
    validate_event(payload["event"], packet, claim)
    write_json(store, PREFIX + "submitted.json", payload["event"])
    return {"submitted_canonical_sha256": sha(canonical(payload["event"]))}


def complete_preparation(store, packet, authorization, payload, *, runtime_check):
    """API storage phase: persist the new local parse, measured usage and one cache."""
    runtime_check()
    original, claim = check_claim(store, packet, authorization, payload["claim_sha256"])
    submitted, _ = read_json(store, PREFIX + "submitted.json")
    validate_event(submitted, packet, claim)
    document = ParsedDocument.model_validate(payload["document"])
    receipt = payload["receipt"]
    validate_wire_receipt(receipt, packet)
    require(all(receipt.get(key) == value for key, value in submitted.items() if key != "outcome"),
            "Operation mismatch")
    require(document.source == LOCATION and document.cache_key == "sha256:" + SOURCE_SHA256
            and receipt["mapped_result_sha256"] == sha(document.model_dump_json().encode())
            and receipt["mapped_at"] == document.parsed_at.isoformat()
            and receipt["reservation_id"] == claim["reservation_id"]
            and receipt["preparation_authorization_sha256"] == claim["authorization_sha256"],
            "Mapped result mismatch")
    require(receipt["input_provenance"]["bytes"] == claim["source_bytes"]
            and receipt["input_provenance"]["blob_etag"] == claim["source_etag"]
            and receipt["input_provenance"]["blob_verified_at"] == claim["blob_verified_at"],
            "Input provenance mismatch")
    store.write_bytes(PREFIX + "parsed.json", document.model_dump_json().encode())
    write_json(store, PREFIX + "analysis-receipt.json", receipt)
    with store.lease(BUDGET_KEY):
        ledger, version = read_json(store, BUDGET_KEY)
        require(sha(canonical(ledger)) == claim["reserved_ledger_canonical_sha256"], "Ledger drift")
        measured = {"input_tokens": 0, "output_tokens": 0, "analysis_pages": receipt["actual_page_count"]}
        measured_cost = RealPilotGuard._cost(original, "analysis", **measured)
        ledger["reservations"][claim["reservation_id"]].update(
            actual_usage=measured, estimated_usage_cost_microdollars=measured_cost,
            recorded_at=now(), status="usage_reported_not_billing",
        )
        ledger["actual_usage"]["analysis_pages"] += measured["analysis_pages"]
        ledger["estimated_usage_cost_microdollars"] += measured_cost
        write_json(store, BUDGET_KEY, ledger, version)
    write_json(store, cache_keys()[1], {
        "parser_version": PARSER_VERSION, "origin": "ford_preparation_analysis_first_five_pages",
        "document": document.model_dump(mode="json"), "document_sha256": receipt["mapped_result_sha256"],
        "analysis_receipt_key": PREFIX + "analysis-receipt.json",
    })
    batch = read_json(store, "batches/" + packet["batch_id"] + ".json")[0]
    for _, binding in selected_sources(batch):
        reused, _ = BatchProcessor(store).cached(cache_keys()[1], binding, LOCATION)
        require(reused == document, "Production cache rejected preparation")
    for key, expected in packet["history_sha256"].items():
        if key != BUDGET_KEY:
            require(record_sha(store.read_bytes(key)[0]) == expected, "History drift")
    result = {
        "status": "prepared_cache_verified_for_both_products", "cache_key": cache_keys()[1],
        "cache_sha256": sha(store.read_bytes(cache_keys()[1])[0]), "analysis_receipt": receipt,
        "ledger_after_sha256": sha(store.read_bytes(BUDGET_KEY)[0]), "reserved_analysis": 1,
        "cache_canonical_sha256": record_sha(store.read_bytes(cache_keys()[1])[0]),
        "ledger_after_canonical_sha256": record_sha(store.read_bytes(BUDGET_KEY)[0]),
        "raw_hashes_are_capture_integrity_only": True,
        "reserved_pages": 5, "reserved_microdollars": claim["reserved_microdollars"], "worker_executions_charged": 0,
        "readiness_clock_started": False, "ford_extraction_authorized": False,
    }
    write_json(store, PREFIX + "completed.json", result)
    return result


# LOCAL CLI: only the reviewed prefix above is loaded into the existing API process.

import argparse
import ast
import base64
import copy
import subprocess
import zlib
from pathlib import Path

from scripts import release


RUNTIME_FILES = (
    "backend/core/docintel.py", "backend/batch_store.py", "backend/batch_worker.py",
    "backend/pilot.py", "backend/real_pilot.py",
)


def analyze_local(content, packet, authorization, claim, *, parser_factory, submitted, save_local):
    """No storage credential or token crosses this local SDK boundary."""
    require(sha(content) == SOURCE_SHA256 == claim["source_sha256"]
            and len(content) == claim["source_bytes"] == packet["local_pdf"]["bytes"],
            "Local approved PDF differs from the verified Blob bytes")
    submitted_error = []

    def accepted(event):
        save_local("submitted.json", event)
        try:
            submitted({"claim_sha256": sha(canonical(claim)), "event": event})
        except Exception as error:
            submitted_error.append(type(error).__name__)

    parser = parser_factory(accepted)
    try:
        document = parser.extract_pdf_bytes(content, source=LOCATION, page_limit=5)
        receipt = copy.deepcopy(parser.analysis_receipt)
        require(receipt["outcome"] == "succeeded" and parser.last_page_count == receipt["actual_page_count"],
                "Complete independent SDK provenance required")
        receipt.update(
            source=LOCATION, parser_version=PARSER_VERSION, mapped_at=document.parsed_at.isoformat(),
            mapped_result_sha256=sha(document.model_dump_json().encode()), cache_key=cache_keys()[1],
            preparation_authorization_sha256=sha(canonical(authorization)), reservation_id=claim["reservation_id"],
            fresh_analysis=True, legacy_local_parse_reused=False, readiness_clock_started=False,
            storage_identity=packet["storage_identity"], input_provenance={
                "kind": "approved_local_copy_equal_to_verified_blob", "local_path": packet["local_pdf"]["path"],
                "blob_source": LOCATION, "sha256": SOURCE_SHA256, "bytes": claim["source_bytes"],
                "blob_etag": claim["source_etag"], "blob_verified_at": claim["blob_verified_at"],
            },
        )
        save_local("parsed.json", document.model_dump(mode="json"))
        save_local("analysis-receipt.json", receipt)
        require(not submitted_error, "Submitted-record transport outcome unknown; retained local parse, never retry")
        return {"claim_sha256": sha(canonical(claim)), "document": document.model_dump(mode="json"), "receipt": receipt}
    except Exception as error:
        save_local("failure.json", {
            "status": "failed_or_unknown_no_retry", "error_type": type(error).__name__, "recorded_at": now(),
            "analysis_metadata": parser.analysis_receipt, "submitted_transport_errors": submitted_error,
            "allowance_refunded": False,
        })
        raise ValueError("Local preparation failed or unknown; inspect preserved evidence, never retry") from None


def run_preparation(store, packet, authorization, *, parser_factory, runtime_check, save_local=None):
    """Offline adapter exercising the same claim, local SDK and storage-finalize phases."""
    retained = {}
    save_local = save_local or (lambda name, value: retained.setdefault(name, copy.deepcopy(value)))
    claim = claim_preparation(store, packet, authorization, runtime_check=runtime_check)
    runtime_check()
    require(record_sha(store.read_bytes(BUDGET_KEY)[0]) == claim["reserved_ledger_canonical_sha256"],
            "Reserved ledger changed before local SDK; never retry")
    payload = analyze_local(
        store.read_bytes(SOURCE_KEY)[0], packet, authorization, claim, parser_factory=parser_factory,
        submitted=lambda value: persist_submitted(store, packet, authorization, value, runtime_check=runtime_check),
        save_local=save_local,
    )
    return complete_preparation(store, packet, authorization, payload, runtime_check=runtime_check)


def load_snapshot(work):
    path = work / "gapfill-actual-outcome.json"
    value = release.private_json(path, max_bytes=64 * 1024 * 1024)
    require(value["runtime"]["temporary_processing_closed"] is True
            and value["runtime"]["active_executions"] == [], "Latest snapshot must be closed")
    records = {}
    for key, entry in value["records"].items():
        raw = base64.b64decode(entry["base64"], validate=True)
        require(sha(raw) == entry["sha256"], "Snapshot record digest mismatch")
        records[key] = raw
    return path, value, records


def approved_local_pdf(work):
    value = release.private_json(work / "ford-local-parse-readiness.json")["approved_local_pdf"]
    path = Path(value["path"])
    require(value["sha256"] == SOURCE_SHA256 and path.name == "001162_AV11-xxxW-NL_techspec.pdf"
            and path.is_absolute() and not path.is_symlink(), "Exact approved local PDF required")
    require(path.stat().st_size == value["bytes"] <= 10 * 1024 * 1024, "Approved local PDF size changed")
    content = path.read_bytes()
    require(sha(content) == SOURCE_SHA256, "Approved local PDF hash changed")
    return {"path": str(path), "sha256": SOURCE_SHA256, "bytes": len(content)}


def plan(work, config, source_revision):
    _, snapshot, records = load_snapshot(work)
    approval = json.loads(records[APPROVAL_KEY])
    ledger = json.loads(records[BUDGET_KEY])
    selected_sources(json.loads(records[f"batches/{approval['batch_id']}.json"]))
    require(re.fullmatch(r"[a-f0-9]{40}", source_revision), "Exact existing API source revision required")
    root = Path(__file__).resolve().parents[1]
    semantics = {**snapshot, "records": {key: json.loads(raw) for key, raw in records.items()}}
    return {
        "schema_version": 1, "contract": CONTRACT, "batch_id": approval["batch_id"],
        "snapshot_sha256": sha(canonical(semantics)), "runtime": snapshot["runtime"],
        "snapshot_hash_semantics": "records_as_parsed_canonical_json",
        "history_hash_semantics": "sha256_sorted_compact_ascii_json",
        "history_sha256": {key: record_sha(raw) for key, raw in records.items()},
        "api_source_revision": source_revision,
        "runtime_code_sha256": {name: sha(subprocess.check_output(
            ["git", "show", f"{source_revision}:{name}"], cwd=root,
        )) for name in RUNTIME_FILES},
        "helper_sha256": sha(Path(__file__).read_bytes()),
        "provenance_source_sha256": sha((root / "backend/analysis_provenance.py").read_bytes()),
        "target_sha256": sha(canonical(config)), "analysis_endpoint": approval["environment"]["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"],
        "api_principal_id": approval["identities"]["api_principal_id"],
        "reserved_microdollars": capacity(approval, ledger), "snapshot_records": len(records),
        "approved_by": approval["approved_by"],
        "tenant_id": config["tenant"], "subscription_id": config["subscription"],
        "analysis_identity": {
            "kind": "approved_operator", "credential": "AzureCliCredential",
            "principal_id": approval["approved_by"], "tenant_id": config["tenant"],
            "subscription_id": config["subscription"],
        },
        "storage_identity": {
            "kind": "api_system_assigned", "credential": "ManagedIdentityCredential",
            "principal_id": approval["identities"]["api_principal_id"], "tenant_id": config["tenant"],
        },
        "local_pdf": approved_local_pdf(work),
        "storage_program_sha256": sha(storage_source().encode()),
    }


def storage_source():
    tree = ast.parse(Path(__file__).read_text().split("\n# LOCAL CLI:", 1)[0])
    tree.body = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef))]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body.pop(0)
    return ast.unparse(tree)


def remote_program(packet, authorization, config, *, phase="claim", value=None):
    root = Path(__file__).resolve().parents[1]
    helper = Path(__file__).read_bytes()
    provenance = (root / "backend/analysis_provenance.py").read_bytes()
    require(sha(helper) == packet["helper_sha256"] and sha(provenance) == packet["provenance_source_sha256"],
            "Reviewed helper/provenance source changed")
    require(phase in {"claim", "submitted", "complete"}, "Only claim/submitted/complete storage phases are permitted")
    if phase == "submitted":
        require(set(value) == {"claim_sha256", "event"}
                and re.fullmatch(r"[a-f0-9]{64}", value["claim_sha256"]), "Submitted packet rejected")
        validate_event(value["event"], packet, {"source_bytes": packet["local_pdf"]["bytes"]})
    if phase == "complete":
        require(set(value) == {"claim_sha256", "document", "receipt"}
                and re.fullmatch(r"[a-f0-9]{64}", value["claim_sha256"]), "Completion packet rejected")
        mapped = ParsedDocument.model_validate(value["document"])
        require(canonical(mapped.model_dump(mode="json")) == canonical(value["document"]),
                "Only normalized mapped parse evidence may reach storage")
        validate_wire_receipt(value["receipt"], packet)
    source = storage_source()
    require(sha(source.encode()) == packet["storage_program_sha256"], "Reviewed storage program changed")
    payload = {
        "phase": phase, "value": value, "prefix": PREFIX,
        "packet_sha256": sha(canonical(packet)), "authorization_sha256": sha(canonical(authorization)),
        "storage_program_sha256": packet["storage_program_sha256"],
        "runtime_code_sha256": packet["runtime_code_sha256"],
        "storage_url": f"https://{config['storage']}.blob.core.windows.net",
        "container": config["container"],
    }
    if phase == "claim":
        payload.update(packet=packet, authorization=authorization, helper=source)
    marker = "DOCINTEL_FORD_PREPARATION_OK:" + phase + ":" + sha(canonical(packet))
    code = f'''import base64, hashlib, json, os
from pathlib import Path
p = json.loads({canonical(payload).decode()!r})
def check(ok):
    if not ok:
        raise ValueError("Existing API code, identity, storage or closed-state drift")
for name, expected in p["runtime_code_sha256"].items():
    check(hashlib.sha256(Path("/app", name).read_bytes()).hexdigest() == expected)
def closed():
    check(os.environ.get("DOCINTEL_BATCH_MODE") == "hosted")
    check(os.environ.get("DOCINTEL_BATCH_LIVE_ENABLED") == "false")
    check(os.environ.get("DOCINTEL_REAL_PILOT_ENABLED") in (None, "false"))
    check(os.environ.get("DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED") in (None, "false"))
    check(os.environ.get("DOCINTEL_PILOT_UPLOAD_ENABLED") in (None, "false"))
    check(os.environ.get("DOCINTEL_BATCH_STORAGE_URL") == p["storage_url"])
    check(os.environ.get("DOCINTEL_BATCH_CONTAINER") == p["container"])
    check(not os.environ.get("AZURE_CLIENT_ID"))
closed()
from backend.batch_store import configured_store, read_json, write_json
store = configured_store()
if p["phase"] != "claim":
    p["packet"] = read_json(store, p["prefix"] + "packet.json")[0]
    p["authorization"] = read_json(store, p["prefix"] + "authorization.json")[0]
    audit = read_json(store, p["prefix"] + "storage-program.json")[0]
    check(audit["sha256"] == p["storage_program_sha256"])
    p["helper"] = audit["source"]
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
check(digest(p["packet"]) == p["packet_sha256"])
check(digest(p["authorization"]) == p["authorization_sha256"])
check(p["storage_program_sha256"] == p["packet"]["storage_program_sha256"])
check(hashlib.sha256(p["helper"].encode()).hexdigest() == p["storage_program_sha256"])
scope = {{"__name__": "ford_preparation_remote"}}
exec(compile(p["helper"], "<reviewed-ford-preparation>", "exec"), scope)
if p["phase"] == "claim":
    result = scope["claim_preparation"](store, p["packet"], p["authorization"], runtime_check=closed)
    write_json(store, p["prefix"] + "storage-program.json", {{"source": p["helper"], "sha256": p["storage_program_sha256"]}})
    write_json(store, p["prefix"] + "packet.json", p["packet"])
elif p["phase"] == "submitted":
    result = scope["persist_submitted"](store, p["packet"], p["authorization"], p["value"], runtime_check=closed)
else:
    result = scope["complete_preparation"](store, p["packet"], p["authorization"], p["value"], runtime_check=closed)
print("DOCINTEL_FORD_PREPARATION_RESULT:" + base64.b64encode(json.dumps(result).encode()).decode())
print({marker!r})
'''
    packed = base64.b85encode(zlib.compress(code.encode(), 9)).decode()
    code = f"exec(__import__('zlib').decompress(__import__('base64').b85decode({packed!r})))"
    require(len(base64.b64encode(code.encode())) + 80 <= 16384, "Reviewed console frame exceeds bounded transport")
    return code, marker


def verify_existing_di_access(config, packet):
    """Read-only existing OPERATOR grant check; API and worker grants are unchanged."""
    from urllib.parse import urlsplit

    endpoint = urlsplit(packet["analysis_endpoint"])
    require(endpoint.scheme == "https" and endpoint.hostname
            and endpoint.hostname.endswith(".cognitiveservices.azure.com")
            and not endpoint.username and not endpoint.query and not endpoint.fragment,
            "Exact existing Document Intelligence endpoint required")
    account = endpoint.hostname.split(".")[0]
    resource = release.azure(
        "cognitiveservices", "account", "show", "--subscription", config["subscription"],
        "-g", config["group"], "-n", account,
    )
    expected = (f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}"
                f"/providers/Microsoft.CognitiveServices/accounts/{account}")
    require(resource["id"].lower() == expected.lower()
            and resource["kind"] == "FormRecognizer"
            and resource["properties"].get("disableLocalAuth") is True
            and resource["properties"]["endpoint"].rstrip("/") == packet["analysis_endpoint"].rstrip("/"),
            "Existing DI resource binding differs")
    assignments = release.azure(
        "role", "assignment", "list", "--subscription", config["subscription"],
        "--scope", expected, "--include-inherited",
        "--fill-principal-name", "false", "--fill-role-definition-name", "false",
    )
    require(any(
        entry.get("principalId") == packet["analysis_identity"]["principal_id"]
        and entry.get("roleDefinitionId", "").rsplit("/", 1)[-1] == "a97b65f3-24c7-4388-baec-2e87135dc908"
        and (expected.lower() == str(entry.get("scope", "")).lower()
             or expected.lower().startswith(str(entry.get("scope", "")).lower().rstrip("/") + "/"))
        and str(entry.get("scope", "")).lower().startswith("/subscriptions/")
        and not entry.get("condition")
        for entry in assignments
    ), "BLOCKED: existing operator DI access not verified; no new grant or identity fallback authorized")
    return {
        "verified_at": now(), "principal_id": packet["analysis_identity"]["principal_id"], "resource_id": resource["id"],
        "role": "Cognitive Services User", "existing_grant_only": True, "permissions_changed": False,
    }


def operator_credential(config, packet):
    from azure.identity import AzureCliCredential
    from backend.analysis_provenance import VerifiedOperatorCredential

    return VerifiedOperatorCredential(
        AzureCliCredential(tenant_id=config["tenant"]),
        packet["analysis_identity"],
    )


CONTINUATION_FILES = (
    "scripts/ford_analysis_preparation.py", "backend/analysis_provenance.py",
    "backend/sdk_preflight.py", "backend/core/docintel.py",
    "tests/test_ford_analysis_preparation.py", "tests/test_private_ford_preparation.py",
    "tests/test_ford_preparation_continuation.py",
)
CONTINUATION_FUNCTIONS = (
    "validate_continuation_authorization", "continuation_state", "begin_continuation",
    "submit_continuation", "complete_continuation",
)


def validate_continuation_authorization(proposal, authorization, packet):
    require(set(authorization) == {
        "schema_version", "approved", "approved_by", "approved_at", "continuation_plan_sha256",
        "original_packet_sha256", "reservation_id", "one_continuation_only",
        "no_new_reservation", "no_retry_or_refund", "parent_live_executor_only",
    }, "Exact continuation authorization required")
    require(authorization["schema_version"] == 1 and authorization["approved"] is True
            and all(authorization[key] is True for key in (
                "one_continuation_only", "no_new_reservation", "no_retry_or_refund", "parent_live_executor_only",
            )), "One existing-reservation continuation only")
    require(authorization["approved_by"] == packet["approved_by"]
            and authorization["continuation_plan_sha256"] == sha(canonical(proposal))
            and authorization["original_packet_sha256"] == proposal["original_packet_sha256"]
            and authorization["reservation_id"] == proposal["reservation_id"],
            "Continuation authority binding mismatch")
    when = datetime.fromisoformat(authorization["approved_at"])
    require(when.tzinfo is not None and datetime.fromisoformat(proposal["failure"]["recorded_at"]) <= when
            <= datetime.now(timezone.utc), "Continuation approval must follow the preserved failure")
    require(proposal["purpose"] == "one_continuation_of_existing_ford_reservation"
            and proposal["new_reservations"] == 0 and proposal["new_reserved_microdollars"] == 0
            and proposal["max_submissions"] == 1 and proposal["sdk_retries"] == 0,
            "Continuation cannot expand the original allowance")
    proof = proposal["native_no_send_proof"]
    require(set(proof) == {
        "status", "provider", "sdk_package", "sdk_version", "transport_package", "transport_version",
        "method", "api_version", "endpoint_sha256", "body_sha256", "body_bytes", "content_type",
        "request_sha256", "authentication_performed", "provider_send_performed",
        "provider_response_fabricated", "source_sha256", "source_bytes",
        "transport", "transport_real_calls", "network_calls", "credential_real_calls", "captured_requests",
        "query", "query_is_complete", "query_sha256", "pages", "request_options", "path_sha256", "path", "model_id",
    }, "Only metadata-only native DI proof is permitted")
    require(proof["status"] == "validated_no_send" and proof["provider"] == "document_intelligence"
            and proof["sdk_package"] == "azure-ai-documentintelligence"
            and proof["transport_package"] == "azure-core" and proof["method"] == "POST"
            and proof["api_version"] == API_VERSION and proof["content_type"] == "application/octet-stream"
            and proof["source_sha256"] == proof["body_sha256"] == SOURCE_SHA256
            and proof["source_bytes"] == proof["body_bytes"] == packet["local_pdf"]["bytes"]
            and proof["transport"] == "in_memory_no_send" and proof["captured_requests"] == 1
            and all(proof[key] == 0 for key in ("transport_real_calls", "network_calls", "credential_real_calls"))
            and proof["request_options"] == REQUEST_OPTIONS and proof["pages"] == "1-5"
            and proof["model_id"] == "prebuilt-layout"
            and proof["path"] == "/documentintelligence/documentModels/prebuilt-layout:analyze"
            and proof["query_is_complete"] is True
            and proof["query"] == {"api-version": [API_VERSION], "pages": ["1-5"]}
            and all(proof[key] is False for key in (
                "authentication_performed", "provider_send_performed", "provider_response_fabricated",
            )), "An exact native no-send proof is mandatory")
    for key in ("endpoint_sha256", "request_sha256", "path_sha256", "query_sha256"):
        require(re.fullmatch(r"[a-f0-9]{64}", proof[key]), "Native request digest required")
    for key in ("sdk_version", "transport_version"):
        require(re.fullmatch(r"[0-9.]+[a-z0-9.]{0,20}", proof[key]), "Installed SDK version required")
    ci = proposal["ci_proof"]
    require(ci["schema_version"] == 1 and ci["status"] == "passed"
            and ci["code_sha256"] == proposal["local_code_sha256"]
            and re.fullmatch(r"[a-f0-9]{40}", ci["source_revision"])
            and ci["checks"] and all(check["conclusion"] == "success" for check in ci["checks"]),
            "Corrected implementation must pass focused CI")


def continuation_state(store, proposal, authorization, *, started):
    packet, _ = read_json(store, PREFIX + "packet.json")
    original_auth, _ = read_json(store, PREFIX + "authorization.json")
    require(sha(canonical(packet)) == proposal["original_packet_sha256"]
            and sha(canonical(original_auth)) == proposal["original_authorization_sha256"],
            "Original preparation authority changed")
    validate_continuation_authorization(proposal, authorization, packet)
    _, claim = check_claim(store, packet, original_auth, proposal["claim_sha256"])
    require(claim["reservation_id"] == proposal["reservation_id"]
            and claim["reserved_microdollars"] == 50000, "Only the original charged reservation may continue")
    for key, expected in proposal["history_sha256"].items():
        require(record_sha(store.read_bytes(key)[0]) == expected, "Stopped history changed")
    failure = proposal["failure"]
    metadata = failure["analysis_metadata"]
    require(failure["status"] == "failed_or_unknown_no_retry" and failure["allowance_refunded"] is False
            and failure["submitted_transport_errors"] == []
            and metadata["operation_id"] is None and "accepted_at" not in metadata
            and metadata["error_type"] == "ClientAuthenticationError"
            and metadata["source_sha256"] == SOURCE_SHA256
            and metadata["source_bytes"] == claim["source_bytes"]
            and metadata["request_options"] == REQUEST_OPTIONS,
            "Only the evidenced pre-submission credential failure may continue")
    ledger, _ = read_json(store, BUDGET_KEY)
    before, _ = read_json(store, PREFIX + "ledger-before.json")
    reservation = ledger["reservations"][claim["reservation_id"]]
    expected = json.loads(canonical(before))
    expected["attempted"]["analysis"] += 1
    expected["reserved"]["analysis_pages"] += 5
    expected["reserved"]["microdollars"] += 50000
    expected["reservations"][claim["reservation_id"]] = reservation
    require(canonical(expected) == canonical(ledger)
            and reservation["status"] == "attempt_reserved_completion_unknown"
            and reservation["actual_usage"] is None and reservation["execution_id"] is None
            and reservation["reserved_usage"] == {"input_tokens": 0, "output_tokens": 0, "analysis_pages": 5}
            and reservation["reserved_microdollars"] == 50000,
            "Original charged ledger, prior reservations or usage changed")
    for key in (*cache_keys(), PREFIX + "parsed.json", PREFIX + "analysis-receipt.json"):
        absent(store, key)
    prefix = PREFIX + "continuation/"
    absent(store, prefix + "failure.json")
    absent(store, prefix + "completed.json")
    if started:
        attempt, _ = read_json(store, prefix + "attempt.json")
        require(attempt["plan_sha256"] == sha(canonical(proposal))
                and attempt["authorization_sha256"] == sha(canonical(authorization))
                and attempt["reservation_id"] == claim["reservation_id"], "Continuation attempt changed")
    else:
        absent(store, PREFIX + "submitted.json")
        for name in ("attempt.json", "authorization.json", "plan.json", "failure-before.json", "program.json"):
            absent(store, prefix + name)
    return packet, original_auth, claim


def begin_continuation(store, proposal, authorization, program, *, runtime_check):
    runtime_check()
    packet, _, claim = continuation_state(store, proposal, authorization, started=False)
    require(sha(program.encode()) == proposal["continuation_program_sha256"], "Continuation code changed")
    content, version = store.read_bytes(SOURCE_KEY, max_bytes=10 * 1024 * 1024)
    require(sha(content) == SOURCE_SHA256 and len(content) == claim["source_bytes"]
            and version == claim["source_etag"], "Previously verified Blob changed")
    with store.lease(packet["batch_id"]), store.lease(BUDGET_KEY):
        runtime_check()
        continuation_state(store, proposal, authorization, started=False)
        attempt = {
            "state": "one_continuation_claimed_no_retry", "started_at": now(),
            "plan_sha256": sha(canonical(proposal)), "authorization_sha256": sha(canonical(authorization)),
            "reservation_id": claim["reservation_id"], "claim_sha256": proposal["claim_sha256"],
            "ledger_canonical_sha256": claim["reserved_ledger_canonical_sha256"],
            "blob_reverified_at": now(), "source_sha256": SOURCE_SHA256,
            "source_etag": version, "source_bytes": len(content),
            "new_reservations": 0, "new_reserved_microdollars": 0, "readiness_clock_started": False,
        }
        prefix = PREFIX + "continuation/"
        write_json(store, prefix + "attempt.json", attempt)
        write_json(store, prefix + "authorization.json", authorization)
        write_json(store, prefix + "plan.json", proposal)
        write_json(store, prefix + "failure-before.json", proposal["failure"])
        write_json(store, prefix + "program.json", {"source": program, "sha256": proposal["continuation_program_sha256"]})
    return attempt


def submit_continuation(store, proposal, authorization, payload, *, runtime_check):
    runtime_check()
    packet, original_auth, _ = continuation_state(store, proposal, authorization, started=True)
    require(payload["claim_sha256"] == proposal["claim_sha256"], "Original reservation binding required")
    attempt, _ = read_json(store, PREFIX + "continuation/attempt.json")
    require(datetime.fromisoformat(payload["event"]["started_at"])
            >= datetime.fromisoformat(attempt["started_at"]), "Analysis predates continuation")
    require(payload["event"]["sdk_version"] == proposal["native_no_send_proof"]["sdk_version"],
            "Actual SDK differs from the no-send validation")
    return persist_submitted(store, packet, original_auth, payload, runtime_check=runtime_check)


def complete_continuation(store, proposal, authorization, payload, *, runtime_check):
    runtime_check()
    packet, original_auth, claim = continuation_state(store, proposal, authorization, started=True)
    require(payload["claim_sha256"] == proposal["claim_sha256"], "Original reservation binding required")
    result = complete_preparation(store, packet, original_auth, payload, runtime_check=runtime_check)
    audit = {
        "status": "existing_reservation_continuation_completed", "completed_at": now(),
        "plan_sha256": sha(canonical(proposal)), "authorization_sha256": sha(canonical(authorization)),
        "reservation_id": claim["reservation_id"], "original_packet_sha256": proposal["original_packet_sha256"],
        "original_failure_canonical_sha256": sha(canonical(proposal["failure"])),
        "original_completed_canonical_sha256": record_sha(store.read_bytes(PREFIX + "completed.json")[0]),
        "cache_canonical_sha256": result["cache_canonical_sha256"],
        "ledger_after_canonical_sha256": result["ledger_after_canonical_sha256"],
        "operation_id": result["analysis_receipt"]["operation_id"],
        "new_reservations": 0, "new_reserved_microdollars": 0, "worker_executions_charged": 0,
        "readiness_clock_started": False,
    }
    write_json(store, PREFIX + "continuation/completed.json", audit)
    return {"preparation": result, "continuation": audit}


def continuation_source():
    tree = ast.parse(Path(__file__).read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in CONTINUATION_FUNCTIONS]
    require({node.name for node in nodes} == set(CONTINUATION_FUNCTIONS), "Continuation code incomplete")
    return ast.unparse(ast.Module(body=nodes, type_ignores=[]))


def continuation_inputs(work):
    snapshot = release.private_json(work / "ford-preparation-stopped-outcome.json", max_bytes=64 * 1024 * 1024)
    require(snapshot["status"] == "preparation_stopped_before_service_submission"
            and snapshot["retry_performed"] is False and snapshot["readiness_clock_started"] is False
            and snapshot["runtime"]["temporary_processing_closed"] is True
            and snapshot["runtime"]["active_executions"] == [], "An unchanged, closed stopped snapshot is required")
    records = {}
    for key, entry in snapshot["records"].items():
        raw = base64.b64decode(entry["base64"], validate=True)
        require(sha(raw) == entry["sha256"], "Stopped snapshot capture changed")
        records[key] = raw
    packet = json.loads(records[PREFIX + "packet.json"])
    original_auth = json.loads(records[PREFIX + "authorization.json"])
    claim = json.loads(records[PREFIX + "reserved.json"])
    failure = release.private_json(work / "ford-preparation-local-failure.json")
    require(canonical(snapshot["local_failure"]) == canonical(failure), "Preserved local failure changed")
    for filename, expected in (
        ("ford-preparation-plan-v4.json", packet), ("ford-preparation-approved-v4.json", original_auth),
        ("ford-preparation-claim.json", claim),
    ):
        require(canonical(release.private_json(work / filename)) == canonical(expected),
                "Original local/API preparation binding changed")
    program = json.loads(records[PREFIX + "storage-program.json"])
    require(sha(program["source"].encode()) == program["sha256"] == packet["storage_program_sha256"],
            "Original immutable API program changed")
    verify_authorization(packet, original_auth, json.loads(records[APPROVAL_KEY]))
    require(sha(canonical(packet)) == claim["packet_sha256"]
            and sha(canonical(original_auth)) == claim["authorization_sha256"]
            and record_sha(records[BUDGET_KEY]) == claim["reserved_ledger_canonical_sha256"],
            "Original reservation or stopped ledger changed")
    for key, expected in packet["history_sha256"].items():
        if key != BUDGET_KEY:
            require(record_sha(records[key]) == expected, "Prior history changed")
    require(all(key not in records for key in (
        *cache_keys(), PREFIX + "submitted.json", PREFIX + "parsed.json",
        PREFIX + "analysis-receipt.json", PREFIX + "completed.json", PREFIX + "failure.json",
    )) and not any(key.startswith(PREFIX + "continuation/") for key in records),
            "Snapshot is not an uncontinued pre-submission stop")
    return snapshot, records, packet, original_auth, claim, failure


def continuation_code_hashes():
    root = Path(__file__).resolve().parents[1]
    return {name: sha((root / name).read_bytes()) for name in CONTINUATION_FILES}


def verify_continuation_ci(proof, hashes):
    require(set(proof) == {"schema_version", "status", "source_revision", "code_sha256", "checks"}
            and proof["schema_version"] == 1 and proof["status"] == "passed"
            and proof["code_sha256"] == hashes and re.fullmatch(r"[a-f0-9]{40}", proof["source_revision"]),
            "Passing focused CI must bind every corrected source/test file")
    require(isinstance(proof["checks"], list) and proof["checks"] and all(
        set(check) == {"name", "run_id", "conclusion"} and check["conclusion"] == "success"
        and isinstance(check["name"], str) and re.fullmatch(r"[\w ./()-]{1,120}", check["name"])
        and type(check["run_id"]) is int and check["run_id"] > 0
        for check in proof["checks"]
    ), "Successful identified CI checks required")
    root = Path(__file__).resolve().parents[1]
    for name, expected in hashes.items():
        require(sha(subprocess.check_output(["git", "show", f"{proof['source_revision']}:{name}"], cwd=root)) == expected,
                "CI revision does not contain the reviewed corrected files")


def continuation_plan(work, config, *, ci_path, owner_approval_path):
    from backend.analysis_provenance import preflight_preparation

    snapshot, records, packet, original_auth, claim, failure = continuation_inputs(work)
    require(sha(canonical(config)) == packet["target_sha256"], "Original target changed")
    hashes = continuation_code_hashes()
    ci = release.private_json(ci_path)
    verify_continuation_ci(ci, hashes)
    local = approved_local_pdf(work)
    require(local == packet["local_pdf"], "Original approved local bytes/path changed")
    proof = preflight_preparation(Path(local["path"]).read_bytes(), endpoint=packet["analysis_endpoint"])
    evidence = {}
    for path in (
        owner_approval_path, ci_path, work / "ford-preparation-stopped-verification.json",
        work / "four-product-preparation-identity-authorization-20261006T221236Z.json",
        work / "four-product-existing-role-evidence-20261006.json",
    ):
        require(path.parent.resolve() == work.resolve() and path.name not in evidence,
                "Distinct create-once evidence files must be in the private release root")
        evidence[path.name] = sha(canonical(release.private_json(path)))
    return {
        "schema_version": 1, "purpose": "one_continuation_of_existing_ford_reservation",
        "original_packet_sha256": sha(canonical(packet)),
        "original_authorization_sha256": sha(canonical(original_auth)), "claim_sha256": sha(canonical(claim)),
        "reservation_id": claim["reservation_id"], "failure": failure,
        "stopped_snapshot_sha256": sha(canonical({**snapshot, "records": {
            key: json.loads(raw) for key, raw in records.items()
        }})),
        "history_sha256": {key: record_sha(raw) for key, raw in records.items()},
        "history_hash_semantics": "sha256_sorted_compact_ascii_json",
        "local_code_sha256": hashes, "ci_proof": ci, "native_no_send_proof": proof,
        "local_evidence_sha256": evidence, "ci_filename": ci_path.name,
        "owner_approval_filename": owner_approval_path.name,
        "continuation_program_sha256": sha(continuation_source().encode()),
        "new_reservations": 0, "new_reserved_microdollars": 0, "max_submissions": 1, "sdk_retries": 0,
    }


def continuation_remote_program(proposal, authorization, packet, config, *, phase, value=None):
    require(phase in {"start", "submitted", "complete"}, "Only bounded continuation phases are permitted")
    validate_continuation_authorization(proposal, authorization, packet)
    source = continuation_source()
    require(proposal["local_code_sha256"] == continuation_code_hashes()
            and sha(source.encode()) == proposal["continuation_program_sha256"],
            "Reviewed continuation implementation changed")
    if phase == "submitted":
        require(set(value) == {"claim_sha256", "event"}
                and value["claim_sha256"] == proposal["claim_sha256"], "Continuation submission packet rejected")
        validate_event(value["event"], packet, {"source_bytes": packet["local_pdf"]["bytes"]})
    if phase == "complete":
        require(set(value) == {"claim_sha256", "document", "receipt"}
                and value["claim_sha256"] == proposal["claim_sha256"], "Continuation completion packet rejected")
        mapped = ParsedDocument.model_validate(value["document"])
        require(canonical(mapped.model_dump(mode="json")) == canonical(value["document"]), "Unnormalized parse")
        validate_wire_receipt(value["receipt"], packet)
    payload = {
        "phase": phase, "value": value, "prefix": PREFIX,
        "plan_sha256": sha(canonical(proposal)), "authorization_sha256": sha(canonical(authorization)),
        "original_packet_sha256": proposal["original_packet_sha256"],
        "original_program_sha256": packet["storage_program_sha256"],
        "continuation_program_sha256": proposal["continuation_program_sha256"],
        "runtime_code_sha256": packet["runtime_code_sha256"],
        "storage_url": f"https://{config['storage']}.blob.core.windows.net", "container": config["container"],
    }
    if phase == "start":
        payload.update(proposal=proposal, authorization=authorization, program=source)
    marker = "DOCINTEL_FORD_CONTINUATION_OK:" + phase + ":" + payload["plan_sha256"]
    code = f'''import base64, hashlib, json, os
from pathlib import Path
p = json.loads({canonical(payload).decode()!r})
def check(ok):
    if not ok:
        raise ValueError("Continuation source/state binding rejected")
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
for name, expected in p["runtime_code_sha256"].items():
    check(hashlib.sha256(Path("/app", name).read_bytes()).hexdigest() == expected)
def closed():
    check(os.environ.get("DOCINTEL_BATCH_MODE") == "hosted")
    check(os.environ.get("DOCINTEL_BATCH_LIVE_ENABLED") == "false")
    for name in ("DOCINTEL_REAL_PILOT_ENABLED", "DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED", "DOCINTEL_PILOT_UPLOAD_ENABLED"):
        check(os.environ.get(name) in (None, "false"))
    check(os.environ.get("DOCINTEL_BATCH_STORAGE_URL") == p["storage_url"])
    check(os.environ.get("DOCINTEL_BATCH_CONTAINER") == p["container"])
    check(not os.environ.get("AZURE_CLIENT_ID"))
closed()
from backend.batch_store import configured_store, read_json
store = configured_store()
original = read_json(store, p["prefix"] + "packet.json")[0]
check(digest(original) == p["original_packet_sha256"])
audit = read_json(store, p["prefix"] + "storage-program.json")[0]
check(audit["sha256"] == original["storage_program_sha256"] == p["original_program_sha256"])
check(hashlib.sha256(audit["source"].encode()).hexdigest() == p["original_program_sha256"])
if p["phase"] != "start":
    p["proposal"] = read_json(store, p["prefix"] + "continuation/plan.json")[0]
    p["authorization"] = read_json(store, p["prefix"] + "continuation/authorization.json")[0]
    program = read_json(store, p["prefix"] + "continuation/program.json")[0]
    check(program["sha256"] == p["continuation_program_sha256"])
    p["program"] = program["source"]
check(digest(p["proposal"]) == p["plan_sha256"])
check(digest(p["authorization"]) == p["authorization_sha256"])
check(p["proposal"]["continuation_program_sha256"] == p["continuation_program_sha256"])
check(hashlib.sha256(p["program"].encode()).hexdigest() == p["continuation_program_sha256"])
scope = {{"__name__": "ford_continuation_remote"}}
exec(compile(audit["source"], "<original-storage-only-program>", "exec"), scope)
exec(compile(p["program"], "<reviewed-continuation-storage-program>", "exec"), scope)
if p["phase"] == "start":
    result = scope["begin_continuation"](store, p["proposal"], p["authorization"], p["program"], runtime_check=closed)
else:
    function = scope["submit_continuation" if p["phase"] == "submitted" else "complete_continuation"]
    result = function(store, p["proposal"], p["authorization"], p["value"], runtime_check=closed)
print("DOCINTEL_FORD_CONTINUATION_RESULT:" + base64.b64encode(json.dumps(result).encode()).decode())
print({marker!r})
'''
    packed = base64.b85encode(zlib.compress(code.encode(), 9)).decode()
    code = f"exec(__import__('zlib').decompress(__import__('base64').b85decode({packed!r})))"
    require(len(base64.b64encode(code.encode())) + 80 <= 16384, "Continuation frame exceeds bounded transport")
    return code, marker


def execute_continuation(work, config, proposal, authorization):
    from backend.analysis_provenance import COGNITIVE_SCOPE, RecordedPreparationParser, preflight_preparation

    attempt_path = work / "ford-preparation-continuation-local-attempt.json"
    require(not attempt_path.exists(), "Continuation already attempted; never retry")
    _, _, packet, original_auth, claim, _ = continuation_inputs(work)
    validate_continuation_authorization(proposal, authorization, packet)
    expected = continuation_plan(
        work, config, ci_path=work / proposal["ci_filename"],
        owner_approval_path=work / proposal["owner_approval_filename"],
    )
    require(canonical(expected) == canonical(proposal), "Reviewed continuation plan/proofs changed")
    identity_evidence = verify_runtime(config, packet, original_auth)
    content = Path(packet["local_pdf"]["path"]).read_bytes()
    proof = preflight_preparation(content, endpoint=packet["analysis_endpoint"])
    require(canonical(proof) == canonical(proposal["native_no_send_proof"]), "Native no-send proof changed")
    release.save_once(attempt_path, {
        "state": "continuation_attempted_no_retry", "started_at": now(),
        "plan_sha256": sha(canonical(proposal)), "authorization_sha256": sha(canonical(authorization)),
        "reservation_id": claim["reservation_id"], "native_no_send_proof": proof,
        "existing_operator_identity_evidence": identity_evidence,
    })
    credential = operator_credential(config, packet)
    try:
        credential.get_token(COGNITIVE_SCOPE)

        def storage_phase(phase, value=None):
            code, marker = continuation_remote_program(proposal, authorization, packet, config, phase=phase, value=value)
            release.save_once(work / f"ford-preparation-continuation-{phase}-console-attempt.json", {
                "status": "outcome_unknown_no_retry", "phase": phase, "code_sha256": sha(code.encode()),
                "plan_sha256": sha(canonical(proposal)), "started_at": now(),
            })
            output = release.console_code([
                "az", "containerapp", "exec", "--subscription", config["subscription"], "-g", config["group"],
                "-n", config["backend"], "--revision", packet["runtime"]["backend_revision"],
                "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors",
            ], code, marker, timeout=120)
            lines = [line.partition(":")[2] for line in output.decode().splitlines()
                     if line.startswith("DOCINTEL_FORD_CONTINUATION_RESULT:")]
            require(len(lines) == 1, "Unknown continuation storage outcome; never retry")
            return json.loads(base64.b64decode(lines[0], validate=True))

        acknowledged = storage_phase("start")
        require(acknowledged["plan_sha256"] == sha(canonical(proposal))
                and acknowledged["authorization_sha256"] == sha(canonical(authorization))
                and acknowledged["reservation_id"] == claim["reservation_id"]
                and acknowledged["new_reservations"] == acknowledged["new_reserved_microdollars"] == 0,
                "Continuation acknowledgement changed")
        release.save_once(work / "ford-preparation-continuation-acknowledgement.json", acknowledged)
        payload = analyze_local(
            content, packet, original_auth, claim,
            parser_factory=lambda submitted: RecordedPreparationParser(
                endpoint=packet["analysis_endpoint"], credential=credential, submitted=submitted,
            ),
            submitted=lambda value: storage_phase("submitted", value),
            save_local=lambda name, value: release.save_once(work / ("ford-preparation-continuation-local-" + name), value),
        )
        result = storage_phase("complete", payload)
        release.save_once(work / "ford-preparation-continuation-result.json", result)
        return result
    except Exception as error:
        release.save_once(work / "ford-preparation-continuation-stopped.json", {
            "status": "continuation_failed_or_unknown_no_retry", "error_type": type(error).__name__,
            "recorded_at": now(), "reservation_id": claim["reservation_id"], "allowance_refunded": False,
        })
        raise ValueError("Continuation failed or unknown; preserve evidence, no further retry") from None


def verify_runtime(config, packet, authorization):
    release.approved_operator(config, authorization)
    account = release.azure("account", "show")
    require(account["id"] == config["subscription"] and account["tenantId"] == config["tenant"],
            "Existing Azure account differs from the approved target")
    operator = release.azure("ad", "signed-in-user", "show")
    require(operator["id"] == packet["analysis_identity"]["principal_id"],
            "Existing signed-in operator differs; no identity switching authorized")
    backend = release.app(config, config["backend"])
    job, active = release.active_executions(config)
    require(not active, "Worker must remain stopped")
    release.require_pilot_identity(backend, packet["api_principal_id"])
    require(backend["properties"]["provisioningState"] == "Succeeded"
            and backend["properties"]["latestReadyRevisionName"] == packet["runtime"]["backend_revision"]
            and backend["properties"]["latestRevisionName"] == packet["runtime"]["backend_revision"],
            "Existing API revision changed; no deployment authorized")
    require([c["image"] for c in release.safe_containers(backend)] == [packet["runtime"]["backend_image"]]
            and [c["image"] for c in release.safe_containers(job)] == [packet["runtime"]["worker_image"]],
            "Existing image binding changed")
    release.require_real_pilot_off(backend)
    release.require_real_pilot_off(job)
    return verify_existing_di_access(config, packet)


def execute(work, config, packet, authorization):
    require(not (work / "ford-preparation-console-attempt.json").exists(),
            "Existing preparation console attempt; inspect state, never retry")
    _, _, records = load_snapshot(work)
    original = json.loads(records[APPROVAL_KEY])
    verify_authorization(packet, authorization, original)
    require(canonical(plan(work, config, packet["api_source_revision"])) == canonical(packet),
            "Reviewed preparation packet changed")
    identity_evidence = verify_runtime(config, packet, authorization)
    content = Path(packet["local_pdf"]["path"]).read_bytes()
    require(sha(content) == SOURCE_SHA256 and len(content) == packet["local_pdf"]["bytes"], "Local PDF changed")
    from backend.analysis_provenance import COGNITIVE_SCOPE, RecordedPreparationParser, preflight_preparation

    preflight_preparation(content, endpoint=packet["analysis_endpoint"])
    credential = operator_credential(config, packet)
    credential.get_token(COGNITIVE_SCOPE)

    def storage_phase(phase, value=None):
        code, marker = remote_program(packet, authorization, config, phase=phase, value=value)
        filename = "ford-preparation-console-attempt.json" if phase == "claim" else f"ford-preparation-{phase}-console-attempt.json"
        release.save_once(work / filename, {
            "phase": phase, "packet_sha256": sha(canonical(packet)),
            "authorization_sha256": sha(canonical(authorization)), "code_sha256": sha(code.encode()),
            "started_at": now(), "status": "outcome_unknown_no_retry",
            "existing_operator_identity_evidence": identity_evidence,
        })
        output = release.console_code([
            "az", "containerapp", "exec", "--subscription", config["subscription"], "-g", config["group"],
            "-n", config["backend"], "--revision", packet["runtime"]["backend_revision"],
            "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors",
        ], code, marker, timeout=120)
        lines = [line.partition(":")[2] for line in output.decode().splitlines()
                 if line.startswith("DOCINTEL_FORD_PREPARATION_RESULT:")]
        require(len(lines) == 1, "Unknown storage outcome; inspect records, never retry")
        return json.loads(base64.b64decode(lines[0], validate=True))

    claim = storage_phase("claim")
    require(claim["packet_sha256"] == sha(canonical(packet))
            and claim["authorization_sha256"] == sha(canonical(authorization))
            and claim["source_sha256"] == SOURCE_SHA256
            and claim["analysis_identity"] == packet["analysis_identity"], "Claim acknowledgement mismatch")
    release.save_once(work / "ford-preparation-claim.json", claim)
    payload = analyze_local(
        content, packet, authorization, claim,
        parser_factory=lambda submitted: RecordedPreparationParser(
            endpoint=packet["analysis_endpoint"], credential=credential, submitted=submitted,
        ),
        submitted=lambda value: storage_phase("submitted", value),
        save_local=lambda name, value: release.save_once(work / ("ford-preparation-local-" + name), value),
    )
    try:
        result = storage_phase("complete", payload)
    except Exception as error:
        release.save_once(work / "ford-preparation-local-storage-failure.json", {
            "status": "completion_storage_outcome_unknown_no_retry", "error_type": type(error).__name__,
            "local_parse_retained": True, "recorded_at": now(), "allowance_refunded": False,
        })
        raise ValueError("Completion storage failed or unknown; local parse retained, never repeat analysis") from None
    release.save_once(work / "ford-preparation-result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "execute", "continuation-plan", "continue"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--api-source")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--ci", type=Path)
    parser.add_argument("--owner-approval", type=Path)
    parser.add_argument("--plan-name")
    args = parser.parse_args(argv)
    continuation = args.action in ("continuation-plan", "continue")
    args.plan_name = args.plan_name or (
        "ford-preparation-continuation-plan-v1.json" if continuation else "ford-preparation-plan-v4.json"
    )
    require(Path(args.plan_name).name == args.plan_name, "Plan name must be a private root basename")
    plan_path = args.work / args.plan_name
    config = release.load_config(args.work / "target.json")
    if continuation:
        if args.action == "continuation-plan":
            require(args.ci is not None and args.owner_approval is not None,
                    "Focused CI and the new explicit owner approval capture are required")
            proposal = continuation_plan(args.work, config, ci_path=args.ci, owner_approval_path=args.owner_approval)
            release.save_once(plan_path, proposal)
            print(json.dumps({"status": "OFFLINE_CONTINUATION_PLAN_ONLY",
                              "plan_sha256": sha(canonical(proposal)), "new_reservations": 0}))
            return 0
        require(args.authorization is not None, "New narrow continuation authorization required")
        result = execute_continuation(
            args.work, config, release.private_json(plan_path), release.private_json(args.authorization),
        )
        print(json.dumps({"status": result["continuation"]["status"],
                          "cache_key": result["preparation"]["cache_key"]}))
        return 0
    require(args.api_source is not None, "Exact existing API source revision required")
    packet = plan(args.work, config, args.api_source)
    if args.action == "plan":
        release.save_once(plan_path, packet)
        print(json.dumps({"status": "OFFLINE_PLAN_ONLY", "packet_sha256": sha(canonical(packet)),
                          "reserved_microdollars": packet["reserved_microdollars"], "snapshot_records": packet["snapshot_records"]}))
        return 0
    require(args.authorization is not None, "Existing explicit owner authorization file required")
    authorization = release.private_json(args.authorization)
    require(canonical(release.private_json(plan_path)) == canonical(packet), "Reviewed plan required")
    result = execute(args.work, config, packet, authorization)
    print(json.dumps({"status": result["status"], "cache_key": result["cache_key"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
