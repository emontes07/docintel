"""One backend-only continuation of the interrupted internal pilot, not a new pilot."""

import argparse
import base64
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.parse import urlsplit
import uuid

if __package__:
    from . import release
else:
    import release


ROOT_PIN = Path.home() / "Library/Application Support/DocIntel/continuation-root.json"
CHILD = "cached-subset-continuation-v1"
RECOVERY_KEY = "configuration/real-pilot-recovery.json"
POLICY = {
    "backend_builds": 1, "build_cpu": 2, "build_timeout_seconds": 900,
    "worker_executions": 1, "worker_timeout_seconds": 600,
    "item_limit": 2, "inference_requests": 4, "analysis": 0,
    "search": 0, "web_retrieval": 0, "retrieval": 0,
    "total_microdollars": 10000000,
}
COMPONENT_FIELDS = {"consumed_component_upper_microdollars", "build_upper_microdollars", "worker_upper_microdollars"}
INCIDENTAL_UNITS = {
    "stored_images": "byte_days", "stored_blobs": "byte_days",
    "billable_logs": "bytes", "transfer": "bytes", "storage_operations": "operations",
    "orchestration_app_requests": "requests", "rollout_cpu": "vcpu_seconds",
    "rollout_memory": "gib_seconds",
}
REQUIRED_HISTORY = {
    "source.json", "images.json", "clean-checks.json", "context.json",
    "context-modes.json", "backend-smoke.json", "frontend-smoke.json",
    "pilot-acceptance.json", "pilot-binding.json", "pilot-enablement.json",
    "pilot-enabled.json", "pilot-execution-attempt-1.json",
    "pilot-execution-result-1.json", "pilot-disable-attempt.json",
    "pilot-disabled.json", "real-pilot-approval.json", "target.json",
}
HISTORY_PATTERNS = (
    "*-publication-*.json", "metadata-preflight-*.json", "publication-*.json",
    "pilot-*.json", "*baseline.json", "*-intended.json", "worker-*.json",
    "failed-pilot-accounting.json", "execution-1-terminal.json",
)
SELECTION_FIELDS = {
    "ledger_sha256", "selected_item_keys", "interrupted_sha256",
    "cached_documents", "prior_execution_id",
}
DECISION_FIELDS = {
    "schema_version", "approved", "approved_by", "target", "approval_sha256",
    "history_sha256", "source_revision", "validation_sha256", "policy", "selection", "cost",
    "publication_not_before", "publication_expires_at",
}
VALIDATION_FILES = ("source.json", "context.json", "context-modes.json", "clean-checks.json", "backend-smoke.json")
require = release.require


def sha(value):
    """The backend's canonical JSON digest, distinct from release.fingerprint."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False,
        separators=(",", ":"),
    ).encode()).hexdigest()


def raw_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_reference(value):
    require(isinstance(value, dict) and set(value) == {"name", "sha256"}
            and isinstance(value["name"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json", value["name"])
            and isinstance(value["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", value["sha256"]),
            "Forecast evidence must reference the retained private audit basename and raw hash")
    return value


def root_pin():
    require(not ROOT_PIN.is_symlink(), "The owner-only original root pin cannot be redirected")
    pin = release.private_json(ROOT_PIN)
    require(isinstance(pin, dict) and set(pin) == {
        "schema_version", "work_root", "history_sha256", "original_receipts",
        "component_floors_microdollars", "cost_audit",
    } and type(pin["schema_version"]) is int and pin["schema_version"] == 1,
            "A separately reviewed private original-root pin is required")
    require(isinstance(pin["work_root"], str) and Path(pin["work_root"]).is_absolute(),
            "Original release root must be an absolute private path")
    receipts = pin["original_receipts"]
    require(isinstance(receipts, dict) and set(receipts) == {
        "target.json", "images.json", "real-pilot-approval.json",
    } and all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
              for value in [pin["history_sha256"], *receipts.values()]),
            "The root pin must bind original target, images, approval and history hashes")
    floors = pin["component_floors_microdollars"]
    require(isinstance(floors, dict) and set(floors) == COMPONENT_FIELDS
            and all(type(value) is int and value > 0 for value in floors.values()),
            "Private reviewed prior-consumption/build/worker floors are required")
    audit_reference(pin["cost_audit"])
    return pin


def root(path=None):
    pinned = Path(root_pin()["work_root"])
    chosen = pinned if path is None else Path(path).expanduser()
    resolved = release.private_path(chosen)
    require(not chosen.is_symlink() and not pinned.is_symlink() and resolved == pinned.resolve(),
            "Continuation must use the original fixed release root")
    require(resolved.is_dir() and resolved.stat().st_mode & 0o077 == 0,
            "Original release root must exist and be owner-only")
    return resolved


def history(work):
    paths = {work / name for name in REQUIRED_HISTORY}
    for pattern in HISTORY_PATTERNS:
        paths.update(path for path in work.glob(pattern) if not path.name.startswith("continuation-"))
    require(not (work / "pilot-execution-attempt-2.json").exists(),
            "The original last worker allowance has already been attempted")
    for path in paths:
        require(path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0,
                "All original release/consumption receipts must remain owner-only regular files")
    return {path.name: raw_sha(path) for path in sorted(paths)}


def original(work, config):
    require(root(work) == work, "Use the canonical original release root")
    pin = root_pin()
    require(all(raw_sha(work / name) == expected for name, expected in pin["original_receipts"].items()),
            "Original privately pinned target/image/approval receipts changed")
    approval = release.private_json(work / "real-pilot-approval.json", max_bytes=65536)
    release.approved_operator(config, approval)
    require(approval.get("execution_scope") == "internal_only"
            and approval.get("approved") is True and approval["customer_processing_approved"] is True,
            "Only the original approved internal-only pilot may continue")
    require(all(approval["limits"][name] == 0 for name in ("search", "web_retrieval", "retrieval")),
            "External allowances must remain zero")
    require(approval["limits"]["executions"] == 2, "Continuation requires the original two-worker allowance")
    require(approval["identities"]["api_principal_id"] == config["api_principal_id"],
            "Original API identity differs from target")
    require(approval["owner"].split("/")[0] == config["tenant"], "Original owner tenant differs")
    release.require_release_images(config, work)
    binding = release.private_json(work / "pilot-binding.json")
    expected = {"target": release.fingerprint(config), "approval_sha256": release.fingerprint(approval),
                "approval_id": approval["id"], "batch_id": approval["batch_id"]}
    require(binding == expected, "Original pilot binding differs; no new approval ID or target")
    first = release.private_json(work / "pilot-execution-attempt-1.json")
    result = release.private_json(work / "pilot-execution-result-1.json")
    require(first["binding"] == result["binding"] == binding
            and first["attempt"] == result["attempt"] == 1 and result.get("execution_name"),
            "Exactly one confirmed original worker attempt is required")
    disabled = release.private_json(work / "pilot-disabled.json")
    closed_binding = {"target": binding["target"], "approval_sha256": binding["approval_sha256"]}
    require(all(disabled.get(key) == value for key, value in closed_binding.items())
            and set(disabled) in (set(closed_binding), set(closed_binding) | {"reconciled_existing_patch", "verified_revision"}),
            "Original activation must be closed, not reset")
    if "reconciled_existing_patch" in disabled:
        require(disabled["reconciled_existing_patch"] is True
                and isinstance(disabled["verified_revision"], str)
                and disabled["verified_revision"].startswith(config["backend"] + "--"),
                "Original reconciled closure must identify its verified backend revision")
    return approval


def prepare(work, config, revision):
    """Local export only; never overwrite the original context or any receipt."""
    with release.pilot_lock(work):
        approval = original(work, config)
        prior = history(work)
        require(sha(prior) == root_pin()["history_sha256"], "Original pinned receipt history changed")
        child = work / CHILD
        require(not child.exists(), "The single continuation context already exists")
        require(re.fullmatch(r"[0-9a-f]{40}", revision or ""), "Full reviewed commit required")
        release.save_once(work / "continuation-baseline.json", {
            "target": release.fingerprint(config), "approval_sha256": sha(approval),
            "history": prior, "history_sha256": sha(prior), "source_revision": revision,
            "context_child": CHILD,
            "work_root": str(work), "root_pin_sha256": raw_sha(ROOT_PIN),
        })
        release.stage(revision, child)
        release.build_context(child)


def validate_source(work, decision):
    child = work / CHILD
    require(not child.is_symlink() and child.resolve().parent == work, "Fixed context child required")
    for name in VALIDATION_FILES:
        release.private_json(child / name, max_bytes=4 * 1024 * 1024)
    require(sha({name: raw_sha(child / name) for name in VALIDATION_FILES}) == decision["validation_sha256"],
            "Approved source/context/validation receipts changed")
    source = release.verify_source(child)
    previous = release.verify_source(work)
    require(source["revision"] == decision["source_revision"], "Continuation source revision differs")
    protected = lambda name: name.startswith("frontend/") or name in {"pyproject.toml", "uv.lock"}
    require({key: value for key, value in source["files"].items() if protected(key)}
            == {key: value for key, value in previous["files"].items() if protected(key)},
            "Frontend and dependency locks must remain unchanged")
    checks = release.private_json(child / "clean-checks.json")
    smoke = release.private_json(child / "backend-smoke.json")
    inventory = release.private_json(child / "context.json", max_bytes=4 * 1024 * 1024)
    modes = release.private_json(child / "context-modes.json", max_bytes=4 * 1024 * 1024)
    context = child / "context"
    entries = list(context.rglob("*"))
    require(all(not path.is_symlink() and (path.is_file() or path.is_dir()) for path in entries),
            "Context links/special files are forbidden")
    require(inventory == {str(path.relative_to(context)): raw_sha(path) for path in entries if path.is_file()}
            and modes == {str(path.relative_to(context)): path.stat().st_mode & 0o777 for path in entries},
            "Continuation tested context changed")
    require(all(source["files"].get(name) == digest for name, digest in inventory.items()),
            "Context differs from reviewed source")
    require(checks["revision"] == smoke["revision"] == source["revision"]
            and checks["backend"] == "clean_locked_install_offline_tests"
            and checks["lock_sha256"] == source["files"]["uv.lock"],
            "Exact clean locked backend validation required")
    require(smoke.get("passed") is True and smoke.get("network") == "none"
            and smoke.get("startup") == "health_200_anonymous_batch_401" and smoke.get("uid") == 10001
            and smoke["context_sha256"] == release.fingerprint(inventory)
            and smoke["modes_sha256"] == release.fingerprint(modes), "Exact offline backend smoke required")
    return child


def incidental_ceiling(proof):
    require(proof.get("execution_window_seconds") == 3600
            and type(proof["execution_window_seconds"]) is int
            and proof.get("estimate_basis") == "conservative_quantities_not_final_billing",
            "Incidental estimate must cover the bounded one-hour execution, not indefinite future storage")
    require(proof.get("storage_month_days") == 30 and type(proof["storage_month_days"]) is int,
            "Storage forecasts must disclose the 30-day price conversion without implying deletion")
    uncertainty = proof.get("uncertainty")
    require(isinstance(uncertainty, list) and uncertainty
            and all(isinstance(note, str) and note.strip() for note in uncertainty),
            "Finite cost estimates must disclose their remaining uncertainty")
    retention = proof.get("monthly_retention_disclosure")
    require(isinstance(retention, dict)
            and retention.get("period_days") == 30 and type(retention["period_days"]) is int
            and retention.get("automatic_deletion") is False
            and type(retention.get("estimated_microdollars")) is int
            and retention["estimated_microdollars"] >= 0
            and isinstance(retention.get("basis"), str) and retention["basis"].strip(),
            "Disclose a 30-day retained-data estimate separately; no deletion or infinite-lifetime proof")
    envelopes = proof.get("envelopes")
    require(isinstance(envelopes, dict) and set(envelopes) == set(INCIDENTAL_UNITS),
            "Prior and bounded one-hour incidental quantity-price coverage is required")
    total = 0
    with localcontext() as context:
        context.prec = 60
        for name, unit in INCIDENTAL_UNITS.items():
            entry = envelopes[name]
            require(isinstance(entry, dict) and set(entry) == {
                "prior_quantity", "new_quantity", "unit", "unit_price_usd", "basis",
            }, "Each incidental envelope must state prior/new quantities, unit, price and evidence basis")
            require(entry["unit"] == unit and isinstance(entry["basis"], str) and entry["basis"].strip(),
                    "Incidental quantity units and supporting evidence must be explicit")
            require(all(type(entry[key]) is int and 0 <= entry[key] <= 10 ** 18
                        for key in ("prior_quantity", "new_quantity")),
                    "Unknown incidental quantities cannot be replaced by a dollar contingency")
            price = entry["unit_price_usd"]
            require(isinstance(price, str)
                    and re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,18})?", price)
                    and Decimal(price) > 0, "Verified positive per-unit incidental price required")
            total += int(((entry["prior_quantity"] + entry["new_quantity"])
                          * Decimal(price) * 1000000).to_integral_value(rounding=ROUND_CEILING))
    return total


def validate(work, config, decision):
    approval = original(work, config)
    baseline = release.private_json(work / "continuation-baseline.json")
    prior = history(work)
    pin = root_pin()
    require(baseline["work_root"] == str(work) and baseline["root_pin_sha256"] == raw_sha(ROOT_PIN),
            "Original root pin changed; cloning/repointing cannot create a new allowance")
    require(prior == baseline["history"] and sha(prior) == baseline["history_sha256"] == pin["history_sha256"],
            "Original receipt/counter history changed")
    require(set(decision) == DECISION_FIELDS and type(decision["schema_version"]) is int
            and decision["schema_version"] == 1 and decision["approved"] is True,
            "An explicit, schema-exact continuation decision is required")
    require(decision["approved_by"] == approval["approved_by"], "Original authorized operator required")
    require(decision["target"] == baseline["target"] == release.fingerprint(config)
            and decision["approval_sha256"] == baseline["approval_sha256"] == sha(approval)
            and decision["history_sha256"] == baseline["history_sha256"]
            and decision["source_revision"] == baseline["source_revision"],
            "Continuation decision must bind original target, approval, history and reviewed source")
    require(decision["policy"] == POLICY
            and all(type(value) is int for value in decision["policy"].values()),
            "Only one backend build and the last two-item worker are authorized")
    selection = decision["selection"]
    require(set(selection) == SELECTION_FIELDS, "Recovery selection schema mismatch")
    selected = selection["selected_item_keys"]
    require(isinstance(selected, list) and len(selected) == 2 and len(set(selected)) == 2
            and all(isinstance(key, str) and 1 <= len(key) <= 128 for key in selected),
            "Exactly two existing item keys are required")
    require(set(selection["interrupted_sha256"]) == set(selected)
            and 1 <= len(selection["cached_documents"]) <= 2
            and all(re.fullmatch(r"parses/[a-f0-9]{64}\.json", key) for key in selection["cached_documents"]),
            "Exact interrupted statuses and existing parse keys are required")
    require(all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in (
        selection["ledger_sha256"], selection["prior_execution_id"],
        *selection["interrupted_sha256"].values(), *selection["cached_documents"].values(),
    )), "Canonical SHA-256 bindings required")
    cost = decision["cost"]
    require(set(cost) == {"consumed_component_upper_microdollars", "build_upper_microdollars",
                         "worker_upper_microdollars", "inference_upper_microdollars",
                         "incidental_forecast_microdollars", "evidence_sha256"},
            "Complete independently reviewed remaining-cost evidence required")
    require(all(type(cost[name]) is int and cost[name] > 0 for name in cost if name != "evidence_sha256"),
            "Positive conservative cost bounds required; unknown is not zero")
    floors = pin["component_floors_microdollars"]
    require(all(cost[name] >= minimum for name, minimum in floors.items()),
            "Cost bounds omit retained off-ledger consumption or the bounded build/worker")
    forecast = sum(value for key, value in cost.items() if key != "evidence_sha256")
    require(forecast <= POLICY["total_microdollars"],
            "Continuation exceeds the original $10 less prior consumption")
    evidence = work / "continuation-cost-evidence.json"
    require(not evidence.is_symlink(), "Monetary evidence must remain in the fixed release root")
    money = release.private_json(evidence)
    require(raw_sha(evidence) == cost["evidence_sha256"], "Approved cost evidence changed")
    require(isinstance(money, dict) and money.get("money_ready") is True
            and money.get("schema_version") == 1 and type(money["schema_version"]) is int
            and money.get("assurance") == "conservative_forecast_not_billing_cap"
            and money.get("approval_granted") is False
            and money.get("approved_guard_price_basis_retained") is True
            and isinstance(money.get("decision", {}), dict)
            and money.get("decision", {}).get("money") == "GO",
            "Monetary NO-GO: original approved price basis and complete incidental bounds are required")
    audit = audit_reference(money.get("audit_receipt"))
    require(audit == pin["cost_audit"], "Only the authoritative pinned financial forecast audit may be used")
    audit_path = work / audit["name"]
    require(not audit_path.is_symlink(), "Retained cost audit cannot be redirected")
    release.private_json(audit_path)
    require(raw_sha(audit_path) == audit["sha256"], "Retained cost audit changed")
    incidentals = money.get("incidental_forecast_microdollars")
    require(type(incidentals) is int and incidentals >= 0
            and incidentals == cost["incidental_forecast_microdollars"],
            "Unallocated incremental costs require a verified integer ceiling; unknown is never zero")
    expected_totals = {
        "original_total_microdollars": POLICY["total_microdollars"],
        "total_forecast_microdollars": forecast,
        "remaining_contingency_microdollars": POLICY["total_microdollars"] - forecast,
    }
    require(all(type(money.get(key)) is int and money[key] == value for key, value in expected_totals.items()),
            "Evidence must account for all five forecast components and the unchanged original budget")
    proofs = money.get("verification_receipts")
    require(isinstance(proofs, dict) and set(proofs) == {"rates", "incidentals"},
            "Monetary clearance needs independently verified rate and complete incidental evidence")
    for kind, coverage in (("rates", "original_approved_guard_price_ceilings"),
                           ("incidentals", "prior_and_one_hour_execution_incidentals")):
        path = work / f"continuation-cost-{kind}-verified.json"
        require(not path.is_symlink(), "Monetary verification receipts cannot be redirected")
        proof = release.private_json(path)
        require(raw_sha(path) == proofs[kind] and isinstance(proof, dict)
                and proof.get("verified") is True and proof.get("coverage") == coverage
                and proof.get("target") == decision["target"]
                and proof.get("history_sha256") == decision["history_sha256"]
                and proof.get("source_revision") == decision["source_revision"]
                and isinstance(proof.get("basis"), str) and proof["basis"].strip(),
                "Unverified, changed or unbound monetary evidence; invented amounts are not clearance")
        if kind == "rates":
            require(proof.get("unit_prices_usd") == approval["unit_prices_usd"],
                    "Price evidence must retain the original approved service ceilings")
            bounds = proof.get("component_upper_microdollars")
            require(isinstance(bounds, dict) and set(bounds) == COMPONENT_FIELDS
                    and all(type(bounds[name]) is int and minimum <= bounds[name] <= cost[name]
                            for name, minimum in floors.items()),
                    "The decision must cover all independently verified component ceilings")
        else:
            require(type(proof.get("upper_microdollars")) is int
                    and proof["upper_microdollars"] == incidentals == incidental_ceiling(proof),
                    "Prior/one-hour incidental coverage must equal supported quantity estimates times prices")
    child = validate_source(work, decision)
    attempt = work / "continuation-attempt.json"
    if attempt.exists():
        require(release.private_json(attempt)["decision_sha256"] == sha(decision),
                "The one continuation decision is already pinned; no rollover")
    return approval, child


def binding(decision):
    return {"decision_sha256": sha(decision), "target": decision["target"]}


def receipt(work, name, decision):
    value = release.private_json(work / ("continuation-" + name + ".json"))
    require(all(value.get(key) == expected for key, expected in binding(decision).items()),
            "Continuation action receipt binding changed")
    return value


def window(value, minimum=0):
    release.pilot_window(value)
    remaining = (datetime.fromisoformat(value["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
    require(remaining >= minimum, "Insufficient active window for the bounded next operation")


def recovery_packet(approval, decision, recovery):
    expected = {"schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
                "approval_sha256": sha(approval), "batch_sha256": approval["batch_sha256"],
                **decision["selection"]}
    require(set(recovery) == set(expected) | {"not_before", "expires_at"}
            and type(recovery["schema_version"]) is int and recovery["approved"] is True
            and all(recovery.get(key) == value for key, value in expected.items()),
            "Recovery must link the original immutable approval and exact reviewed selection")
    window(recovery)
    require(datetime.fromisoformat(recovery["not_before"]) >= datetime.fromisoformat(approval["expires_at"]),
            "Recovery window must follow the closed original activation")
    require(datetime.fromisoformat(recovery["expires_at"])
            <= datetime.fromisoformat(decision["publication_not_before"]) + timedelta(hours=1),
            "Fresh recovery window must remain inside the estimated one-hour continuation")
    release.bounded_metadata(recovery)


def remote_code(approval, decision, recovery=None, *, write=False, configured=False):
    """The console emits only a digest marker, never source/status/cache content."""
    require(not write or recovery is not None, "Recovery candidate required")
    require(not configured or recovery is not None, "Configured recovery candidate required")
    payload = {"approval_sha256": sha(approval), "batch_id": approval["batch_id"],
               "selection": decision["selection"], "cost": decision["cost"], "recovery": recovery,
               "require_configured": configured}
    encoded = release.bounded_metadata(payload)
    marker = "DOCINTEL_CONTINUATION_OK:" + sha(payload)
    code = f'''import base64, hashlib, json, os, zlib
from decimal import Decimal, localcontext
from backend.batch_store import Missing, configured_store, read_json, write_json
from backend.real_pilot import RealPilotGuard, BUDGET_KEY, _sha256
raw = zlib.decompress(base64.b64decode({encoded!r}, validate=True))
assert len(raw) <= 65536
packet = json.loads(raw)
assert _sha256(packet) == {sha(payload)!r}
store = configured_store()
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
assert _sha256(approval) == packet["approval_sha256"]
batch, _ = read_json(store, "batches/" + packet["batch_id"] + ".json")
assert batch["state"] in ("queued", "running") and batch["mode"] == "real_pilot"
ledger, _ = read_json(store, BUDGET_KEY)
selection = packet["selection"]
assert _sha256(ledger) == selection["ledger_sha256"] and not ledger.get("invalidated")
assert ledger["approval_sha256"] == _sha256(approval)
assert ledger["batch_sha256"] == approval["batch_sha256"] and ledger["owner"] == approval["owner"]
assert list(ledger["executions"]) == [selection["prior_execution_id"]]
assert approval["limits"]["executions"] == 2
assert ledger["attempted"]["inference"] == 0
assert ledger["reserved"]["input_tokens"] == ledger["reserved"]["output_tokens"] == 0
assert all(ledger["attempted"][name] == 0 for name in ("search", "web_retrieval", "retrieval"))
assert packet["cost"]["consumed_component_upper_microdollars"] >= ledger["reserved"]["microdollars"]
input_left = approval["limits"]["input_tokens"] - ledger["reserved"]["input_tokens"]
output_left = min(4 * 2048, approval["limits"]["output_tokens"] - ledger["reserved"]["output_tokens"])
with localcontext() as context:
    context.prec = 60
    rates = [Decimal(approval["unit_prices_usd"][key]) * 1000000 for key in ("input_token", "output_token")]
    rounding = 0 if all(rate == rate.to_integral_value() for rate in rates) else 3
model_upper = RealPilotGuard._cost(approval, "inference", input_left, output_left, 0) + rounding
assert packet["cost"]["inference_upper_microdollars"] >= model_upper
assert ledger["reserved"]["microdollars"] + model_upper <= approval["limits"]["spend_microdollars"]
class ReadOnly:
    def read_bytes(self, name, **kwargs):
        return store.read_bytes(name, **kwargs)
checker = RealPilotGuard.__new__(RealPilotGuard)
checker.store, checker.batch = ReadOnly(), batch
checker._validate_batch(approval)
assert set(selection["selected_item_keys"]) <= {{item["item_key"] for item in batch["items"]}}
for item in batch["items"]:
    key = item["item_key"]
    for prefix in ("results", "reviews"):
        try:
            store.read_bytes(prefix + "/" + batch["id"] + "/" + key + ".json")
        except Missing:
            pass
        else:
            raise ValueError("Existing results/reviews prohibit recovery")
    try:
        raw_status, _ = store.read_bytes("items/" + batch["id"] + "/" + key + ".json")
    except Missing:
        assert key not in selection["selected_item_keys"]
    else:
        assert key in selection["selected_item_keys"]
        assert hashlib.sha256(raw_status).hexdigest() == selection["interrupted_sha256"][key]
        assert json.loads(raw_status)["state"] == "interrupted"
    if key in selection["selected_item_keys"]:
        for source in item["sources"]:
            if source["kind"] == "blob" and source.get("format") in ("pdf", "xlsx"):
                content, _ = store.read_bytes(source["blob"], max_bytes=10 * 1024 * 1024)
                assert hashlib.sha256(content).hexdigest() == source["sha256"]
for key, digest in selection["cached_documents"].items():
    content, _ = store.read_bytes(key)
    assert hashlib.sha256(content).hexdigest() == digest
if packet["recovery"] is not None:
    from backend.real_pilot import validate_recovery
    os.environ["DOCINTEL_REAL_PILOT_OPERATOR_IDS"] = approval["approved_by"]
    assert validate_recovery(packet["recovery"], approval, batch, ledger, store) == packet["recovery"]
try:
    existing, _ = read_json(store, {RECOVERY_KEY!r})
except Missing:
    existing = None
assert existing is None or existing == packet["recovery"]
assert not packet["require_configured"] or existing == packet["recovery"]
'''
    if write:
        code += f'''assert existing is None
write_json(store, {RECOVERY_KEY!r}, packet["recovery"])
confirmed, _ = read_json(store, {RECOVERY_KEY!r})
assert confirmed == packet["recovery"]
'''
    code += f"print({marker!r})\n"
    require(len(base64.b64encode(code.encode())) + 80 <= 16384,
            "Continuation metadata console frame exceeds transport bound")
    return code, marker


def verify_remote(config, approval, decision, recovery=None, *, write=False, configured=False):
    backend = release.app(config, config["backend"])
    release.require_pilot_identity(backend, approval["identities"]["api_principal_id"])
    code, marker = remote_code(approval, decision, recovery, write=write, configured=configured)
    release.run_pilot_console(config, backend, code, marker)


def require_worker(config, approval):
    job = release.require_pilot_job(config, approval)
    resources = release.safe_containers(job)[0].get("resources", {})
    require(resources.get("cpu") == 1 and resources.get("memory") == "2Gi",
            "Existing worker must retain the verified one-CPU/two-GiB cost envelope")
    return job


def require_model_capacity(config, approval):
    """Read management metadata only; never probe a completion or change quota."""
    endpoint = urlsplit(approval["environment"]["LLM_ENDPOINT"])
    match = re.fullmatch(
        r"([a-z0-9][a-z0-9-]{1,62})\.(?:openai\.azure\.com|cognitiveservices\.azure\.com)",
        endpoint.hostname or "",
    )
    require(match is not None and endpoint.scheme == "https"
            and endpoint.port in (None, 443) and endpoint.path in ("", "/")
            and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment,
            "The original model endpoint must identify its exact Azure account")
    account = match.group(1)
    deployment = approval["environment"]["LLM_DEPLOYMENT"]
    require(isinstance(deployment, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", deployment),
            "Exact original model deployment name required")
    metadata = release.azure(
        "cognitiveservices", "account", "deployment", "show",
        "--subscription", config["subscription"], "-g", config["group"],
        "--name", account, "--deployment-name", deployment, timeout=60,
    )
    expected_id = (f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}"
                   f"/providers/Microsoft.CognitiveServices/accounts/{account}/deployments/{deployment}")
    require(isinstance(metadata, dict) and isinstance(metadata.get("id"), str)
            and metadata["id"].lower() == expected_id.lower(), "Model deployment resource binding differs")
    properties = metadata.get("properties", {})
    model = properties.get("model", {})
    require(properties.get("provisioningState") == "Succeeded"
            and model.get("format") == "OpenAI" and model.get("name") == "gpt-5"
            and model.get("version") == "2025-08-07"
            and metadata.get("sku", {}).get("name") == "GlobalStandard",
            "Existing GPT-5 snapshot/SKU/readiness differs from the reviewed model and cost basis")
    limits = properties.get("rateLimits")
    require(isinstance(limits, list), "Explicit deployment token-rate metadata is required")
    tokens = [entry for entry in limits if isinstance(entry, dict) and entry.get("key") == "token"]
    require(len(tokens) == 1, "One explicit token-rate limit is required; SKU capacity is not TPM proof")
    count, period = tokens[0].get("count"), tokens[0].get("renewalPeriod")
    require(type(count) in (int, float) and math.isfinite(count) and count >= 30000
            and type(period) in (int, float) and period == 60,
            "Deployment must explicitly provide at least 30000 tokens per 60 seconds")
    return {"deployment_sha256": sha(metadata), "tokens_per_minute": count}


def preflight(work, config, decision):
    approval, _ = validate(work, config, decision)
    release.verify_target(config)
    release.require_pilot_acceptance(config, work)
    require_worker(config, approval)
    release.require_real_pilot_off(release.app(config, config["backend"]))
    capacity = require_model_capacity(config, approval)
    verify_remote(config, approval, decision)
    return capacity


def publish(work, config, decision):
    with release.pilot_lock(work):
        require(not (work / "continuation-attempt.json").exists(), "Continuation already attempted; never resubmit")
        capacity = preflight(work, config, decision)
        child = work / CHILD
        active = {"not_before": decision["publication_not_before"], "expires_at": decision["publication_expires_at"]}
        window(active, 900)
        attempt = work / "continuation-attempt.json"
        release.save_once(attempt, {
            **binding(decision), "decision": decision, "attempt_id": str(uuid.uuid4()),
            "cpu": 2, "timeout_seconds": 900, "status": "attempted_outcome_unknown",
            "helper_sha256": raw_sha(Path(__file__)),
            "model_capacity": capacity,
        })
        result = release.execute_publication(
            config, child, "backend", decision["source_revision"], attempt,
            expires=datetime.fromisoformat(active["expires_at"]), validate_upload_window=True,
        )
        prior = release.private_json(work / "images.json")
        published = {
            "revision": decision["source_revision"],
            "backend": config["registry"] + ".azurecr.io/docintel/backend@" + result["digest"],
            "backend_run_id": result["runId"], "frontend": prior["frontend"],
            "preserved_frontend": {"work": str(work), "revision": prior["revision"],
                                   "receipt_sha256": release.fingerprint(prior)},
            "tool_revision": result["tool_revision"], "tool_sha256": result["tool_sha256"],
        }
        release.save_once(child / "images.json", published)
        release.save_once(work / "continuation-published.json", {
            **binding(decision), "images_sha256": raw_sha(child / "images.json"),
        })


def published_config(work, config, decision):
    published = receipt(work, "published", decision)
    path = work / CHILD / "images.json"
    require(raw_sha(path) == published["images_sha256"], "Continuation publication receipt changed")
    images = release.private_json(path)
    current = {**config, "backend_image": images["backend"]}
    release.require_release_images(current, work / CHILD)
    release.require_pilot_build_receipts(current, work / CHILD)
    return current


def shape(resource):
    projected = copy.deepcopy({
        "identity": resource.get("identity"), "configuration": resource["properties"]["configuration"],
        "template": resource["properties"]["template"],
    })
    projected["template"]["containers"] = release.writable_containers(release.safe_containers(resource))
    return projected


def ready(config, kind):
    deadline = monotonic() + 180
    while True:
        resource = release.app(config, config[kind], timeout=30)
        properties = resource["properties"]
        require(release.safe_containers(resource)[0]["image"] == release.image(config, kind), "App image differs")
        require(properties["provisioningState"] not in ("Failed", "Canceled"),
                "App provisioning failed; inspect without repeating mutation")
        if (properties["provisioningState"] == "Succeeded"
                and properties["latestRevisionName"] == properties["latestReadyRevisionName"]):
            break
        require(monotonic() < deadline, "App revision is not ready; inspect without repeating mutation")
        sleep(5)
    revision = release.azure("containerapp", "revision", "show", "--subscription", config["subscription"],
                             "-g", config["group"], "-n", config[kind], "--revision", properties["latestReadyRevisionName"], timeout=30)
    require(revision["properties"]["healthState"] == "Healthy"
            and release.safe_containers(revision)[0]["image"] == release.image(config, kind), "Healthy image not confirmed")
    require(release.writable_containers(release.safe_containers(revision))
            == release.writable_containers(release.safe_containers(resource)),
            "Ready revision does not contain the confirmed runtime/authorization settings")
    if kind == "backend":
        release.require_backend_runtime(release.safe_containers(resource),
                                        release.writable_ingress(properties["configuration"].get("ingress")))
    return resource


def patch(work, config, label, resource, containers, *, job=False):
    if job:
        current, running = release.active_executions(config)
        require(not running, "Worker became active before mutation")
    else:
        current = release.app(config, config["backend"])
    require(current == resource, "Resource changed before mutation; inspect, never overwrite drift")
    path = work / ("continuation-" + label + "-patch.json")
    payload = {"properties": {"template": {"containers": release.writable_containers(containers)}}}
    release.save_once(path, payload)
    arguments = ["rest", "--method", "PATCH", "--url", "https://management.azure.com" + resource["id"]
                 + "?api-version=2024-03-01", "--body", "@" + str(path)]
    if resource.get("etag"):
        arguments.extend(("--headers", "If-Match=" + resource["etag"]))
    release.azure(*arguments)
    expected = shape(resource)
    expected["template"]["containers"] = release.writable_containers(containers)
    deadline = monotonic() + 180
    while True:
        if job:
            actual, running = release.active_executions(config, timeout=30)
            require(not running, "Worker became active during mutation; stop and inspect")
        else:
            actual = release.app(config, config["backend"], timeout=30)
        observed = shape(actual)
        if observed == expected:
            break
        require(observed == shape(resource), "Configuration/identity drift; do not repeat mutation")
        require(monotonic() < deadline, "Mutation not confirmed; inspect the same request, do not repeat")
        sleep(5)


def deploy(work, config, decision):
    with release.pilot_lock(work):
        require(not (work / "continuation-deploy-attempt.json").exists(), "Deployment already attempted")
        approval, _ = validate(work, config, decision)
        current = published_config(work, config, decision)
        preflight(work, config, decision)
        end = datetime.fromisoformat(decision["publication_not_before"]) + timedelta(hours=1)
        require(datetime.now(timezone.utc) < end, "Estimated one-hour deployment window has elapsed")
        backend = ready(config, "backend")
        frontend = ready(config, "frontend")
        job = require_worker(config, approval)
        require(job["properties"]["configuration"]["triggerType"] == "Manual", "Existing manual worker required")
        release.save_once(work / "continuation-deploy-attempt.json", {
            **binding(decision), "backend": shape(backend), "frontend": shape(frontend), "worker": shape(job),
        })
        containers = release.safe_containers(backend)
        containers[0]["image"] = current["backend_image"]
        patch(work, config, "deploy-backend", backend, containers)
        ready(current, "backend")
        containers = release.safe_containers(job)
        containers[0]["image"] = current["backend_image"]
        patch(work, config, "deploy-worker", job, containers, job=True)
        require(shape(ready(current, "frontend")) == shape(frontend), "Frontend changed; continuation stopped")
        release.save_once(work / "continuation-deployed.json", {
            **binding(decision), "backend_image": current["backend_image"],
            "synthetic_acceptance_reused_sha256": raw_sha(work / "pilot-acceptance.json"),
        })


def active_target(work, config, decision, approval, *, processing=True):
    receipt(work, "deployed", decision)
    baseline = receipt(work, "deploy-attempt", decision)
    current = published_config(work, config, decision)
    release.verify_target(current)
    release.identity_contract(current)
    backend = ready(current, "backend")
    require(shape(ready(current, "frontend")) == baseline["frontend"], "Unchanged frontend baseline drift")
    job = require_worker(current, approval) if processing else release.active_executions(current)[0]
    require(job["properties"]["configuration"]["triggerType"] == "Manual", "Manual worker required")
    expected_job = copy.deepcopy(baseline["worker"])
    expected_job["template"]["containers"][0]["image"] = current["backend_image"]
    require(shape(job) == expected_job, "Persistent worker identity/configuration drift")
    expected_backend = copy.deepcopy(baseline["backend"])
    expected_backend["template"]["containers"][0]["image"] = current["backend_image"]
    for value in (expected_backend, actual := shape(backend)):
        value["template"]["containers"] = release.writable_containers(value["template"]["containers"])
        value["template"]["containers"][0]["env"] = [
            entry for entry in value["template"]["containers"][0].get("env", [])
            if entry["name"] not in release.PILOT_API_SETTINGS
        ]
    require(actual == expected_backend, "Backend identity/auth/storage/runtime drift")
    capacity = require_model_capacity(current, approval) if processing else None
    return current, backend, job, capacity


def configure(work, config, decision, recovery):
    with release.pilot_lock(work):
        approval, _ = validate(work, config, decision)
        recovery_packet(approval, decision, recovery)
        window(recovery, 600)
        current, backend, _, capacity = active_target(work, config, decision, approval)
        release.require_real_pilot_off(backend)
        require(not (work / "continuation-configure-attempt.json").exists(), "Recovery configuration already attempted")
        verify_remote(current, approval, decision, recovery)
        release.save_once(work / "continuation-configure-attempt.json", {
            **binding(decision), "recovery_sha256": sha(recovery), "recovery": recovery,
            "model_capacity": capacity,
        })
        verify_remote(current, approval, decision, recovery, write=True)
        release.save_once(work / "continuation-configured.json", {**binding(decision), "recovery_sha256": sha(recovery)})


def configured_recovery(work, decision, approval):
    candidate = receipt(work, "configure-attempt", decision)
    recovery = candidate["recovery"]
    require(candidate["recovery_sha256"] == receipt(work, "configured", decision)["recovery_sha256"] == sha(recovery),
            "Recovery configuration receipt changed")
    recovery_packet(approval, decision, recovery)
    return recovery


def enable(work, config, decision):
    with release.pilot_lock(work):
        approval, _ = validate(work, config, decision)
        recovery = configured_recovery(work, decision, approval)
        current, backend, _, capacity = active_target(work, config, decision, approval)
        release.require_real_pilot_off(backend)
        containers = release.safe_containers(backend)
        entries = release.environment_entries(containers[0])
        require(entries.get("DOCINTEL_PILOT_UPLOAD_ENABLED") in (
            None, {"name": "DOCINTEL_PILOT_UPLOAD_ENABLED", "value": "false"}), "Source upload must remain disabled")
        verify_remote(current, approval, decision, recovery, configured=True)
        window(recovery, 600)
        intended = {name: {"name": name, "value": value} for name, value in release.pilot_values(approval).items()}
        release.save_once(work / "continuation-enablement.json", {
            **binding(decision), "recovery_sha256": sha(recovery),
            "baseline": {name: entries.get(name) for name in release.PILOT_API_SETTINGS}, "intended": intended,
            "model_capacity": capacity,
        })
        release.merge_environment(containers[0], release.pilot_values(approval))
        patch(work, current, "enable", backend, containers)
        ready(current, "backend")
        release.save_once(work / "continuation-enabled.json", binding(decision))


def start(work, config, decision):
    with release.pilot_lock(work):
        approval, _ = validate(work, config, decision)
        recovery = configured_recovery(work, decision, approval)
        receipt(work, "enabled", decision)
        require(not (work / "continuation-closed.json").exists(), "Continuation is closed")
        require(not (work / "continuation-worker-attempt.json").exists(), "Final worker already attempted; never retry")
        current, backend, job, capacity = active_target(work, config, decision, approval)
        entries = release.environment_entries(release.safe_containers(backend)[0])
        require(all(entries.get(name) == value for name, value in receipt(work, "enablement", decision)["intended"].items()),
                "Enabled API authorization settings drift")
        verify_remote(current, approval, decision, recovery, configured=True)
        template = copy.deepcopy(job["properties"]["template"])
        container = template["containers"][0]
        container["command"] = ["/app/.venv/bin/python"]
        container["args"] = ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", approval["batch_id"],
                             "--concurrency", "1", "--max-batches", "1", "--item-limit", "2"]
        container["env"] = [entry for entry in container.get("env", [])
                            if entry["name"] not in release.PILOT_EXTERNAL_ENVIRONMENT_KEYS | {"WEBIQ_API_KEY"}]
        release.merge_environment(container, {**approval["environment"], **release.pilot_values(approval),
                                              "DOCINTEL_REAL_PILOT_EXECUTION_SCOPE": "internal_only"})
        template["containers"] = release.writable_containers(template["containers"])
        window(recovery, 600)
        fresh, running = release.active_executions(current)
        require(not running and fresh == job, "Worker changed before the last start")
        path = work / "continuation-worker-template.json"
        release.save_once(path, template)
        release.save_once(work / "continuation-worker-attempt.json", {
            **binding(decision), "recovery_sha256": sha(recovery), "execution_sha256": sha(template),
            "original_attempt_sha256": raw_sha(work / "pilot-execution-attempt-1.json"),
            "attempt": 2, "state": "start_attempted_completion_unknown",
            "model_capacity": capacity,
        })
        window(recovery, 600)
        result = release.azure("containerapp", "job", "start", "--subscription", current["subscription"],
                               "-g", current["group"], "-n", current["job"], "--yaml", str(path))
        require(isinstance(result, dict) and isinstance(result.get("name"), str) and result["name"],
                "Worker submission is uncertain; inspect existing execution, never resubmit")
        release.save_once(work / "continuation-worker-result.json", {
            **binding(decision), "attempt": 2, "execution_name": result["name"],
        })


def close(work, config, decision):
    """Restore only managed API flags, even after expiry or an uncertain start."""
    with release.pilot_lock(work):
        approval, _ = validate(work, config, decision)
        current, backend, _, _ = active_target(work, config, decision, approval, processing=False)
        baseline = receipt(work, "enablement", decision)
        containers = release.safe_containers(backend)
        entries = release.environment_entries(containers[0])
        actual = {name: entries.get(name) for name in release.PILOT_API_SETTINGS}
        require(actual in (baseline["baseline"], baseline["intended"]), "Managed pilot setting drift; unsafe closure")
        if actual != baseline["baseline"]:
            require(not (work / "continuation-close-attempt.json").exists(), "Closure already attempted; inspect before proceeding")
            for name, entry in baseline["baseline"].items():
                if entry is None:
                    entries.pop(name, None)
                else:
                    entries[name] = entry
            containers[0]["env"] = list(entries.values())
            release.save_once(work / "continuation-close-attempt.json", binding(decision))
            patch(work, current, "close", backend, containers)
        ready(current, "backend")
        if not (work / "continuation-closed.json").exists():
            release.save_once(work / "continuation-closed.json", binding(decision))


def rollback(work, config, decision):
    with release.pilot_lock(work):
        validate(work, config, decision)
        current = published_config(work, config, decision)
        baseline = receipt(work, "deploy-attempt", decision)
        if (work / "continuation-enablement.json").exists():
            receipt(work, "closed", decision)
        release.verify_target(current)
        backend = release.app(current, current["backend"])
        job, running = release.active_executions(current)
        require(not running, "Stop the active worker explicitly before rollback")
        require(shape(release.app(current, current["frontend"])) == baseline["frontend"],
                "Frontend changed; refusing rollback drift")
        release.require_real_pilot_off(backend)
        for kind, resource in (("backend", backend), ("worker", job)):
            actual = shape(resource)
            require(actual["template"]["containers"][0]["image"] in (
                config["backend_image"], current["backend_image"]), "Rollback image drift")
            actual["template"]["containers"][0]["image"] = config["backend_image"]
            require(actual == baseline[kind], "Rollback configuration/identity drift")
        require(not (work / "continuation-rollback-attempt.json").exists(), "Rollback already attempted")
        release.save_once(work / "continuation-rollback-attempt.json", binding(decision))
        for label, resource, is_job in (("backend", backend, False), ("worker", job, True)):
            containers = release.safe_containers(resource)
            if containers[0]["image"] != config["backend_image"]:
                containers[0]["image"] = config["backend_image"]
                patch(work, current, "rollback-" + label, resource, containers, job=is_job)
        ready(config, "backend")
        release.save_once(work / "continuation-rolled-back.json", binding(decision))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "preflight", "publish", "deploy", "configure", "enable", "start", "close", "rollback"))
    parser.add_argument("--work", type=Path, help="Must match the independently pinned original private root")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--recovery", type=Path)
    parser.add_argument("--approve")
    args = parser.parse_args()
    work = root(args.work)
    config = release.load_config(args.config or work / "target.json")
    if args.action == "prepare":
        prepare(work, config, args.revision)
        return
    decision = release.private_json(args.decision, max_bytes=65536)
    if args.action == "check":
        validate(work, config, decision)
        print("Local continuation bindings/remaining-cost bounds verified; no Azure operation performed.")
        return
    if args.action != "preflight":
        require(args.approve == args.action, "Explicit approval for this continuation action required")
    if args.action == "configure":
        configure(work, config, decision, release.private_json(args.recovery, max_bytes=65536))
    else:
        globals()[args.action](work, config, decision)
    print("Continuation " + args.action + " confirmed; original receipts and consumed allowances retained.")


if __name__ == "__main__":
    main()
