"""One append-only Mueller continuation, in the existing pinned release root.

prepare/check/ready are local. Publication is one backend source upload and one
2-vCPU/900s build. Deployment changes API/manual-worker images and, when selected,
binds the owner's existing key to the existing job secret store. The
frontend is compared before and after, never patched. Activation requires an
externally supplied authenticated receipt, installs one amendment, starts one
600s/two-item slice and always closes processing. No action retries paid work.
"""

import argparse
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from time import monotonic, sleep
from urllib.parse import urlsplit
import uuid

if __package__:
    from . import release, pilot_continuation as prior, row_continuation as row
else:
    import release
    import pilot_continuation as prior
    import row_continuation as row


BASELINE_REVISION = "f56d7eafd56ba3dec90ca87500e1ddf71a1453c8"
PREFIX, CHILD = "gapfill", "gapfill-v1"
KEY, AUDIT = "configuration/real-pilot-gapfill.json", "operations/real-pilot-gapfill.json"
TIME_FIELDS = {"readiness_at", "operating_expires_at", "not_before", "expires_at"}
SUCCESSOR_FILES = {
    "backend/real_pilot.py", "backend/batch_worker.py", "scripts/gapfill_continuation.py",
    "tests/test_gapfill_continuation.py", "tests/test_gapfill_rerun.py",
    "DEPLOYMENT.md", "BATCH.md",
}
WORKER_INTERFACE_OLD = (
    '            if (guard.execution_scope != "full" or guard.recovery is not None\n'
    '                    or self.optional_web_policy.batch_sha256 != binding_digest(record)):\n'
)
WORKER_INTERFACE_NEW = (
    '            if (guard.execution_scope != "full"\n'
    '                    or (guard.recovery is not None and guard.gapfill is None)\n'
    '                    or self.optional_web_policy.batch_sha256 != binding_digest(record)):\n'
)
POLICY = {
    "backend_builds": 1, "frontend_builds": 0, "build_cpu": 2, "build_timeout_seconds": 900,
    "worker_executions": 1, "worker_timeout_seconds": 600, "item_limit": 2,
    "inference_requests": 6, "internal_requests": 4, "web_requests": 2,
    "additional_inference": 1, "additional_input_tokens": 151646,
    "additional_output_tokens": 2048, "input_tokens": 151646, "output_tokens": 12288,
    "search": 2, "web_retrieval": 4, "browse": 0, "analysis": 0, "retrieval": 0,
    "ford": 0, "sharepoint": 0, "incremental_microdollars": 5000000,
    "existing_key_bindings": 1, "new_credentials": 0, "new_permissions": 0,
}
CONSUMED = {"executions": 4, "inference": 11, "input_tokens": 257868, "output_tokens": 22528}
GATE_CHECKS = {
    "production_run_batch", "real_guard_reservations", "full_six_payloads",
    "append_only_history", "immutable_results", "attempt_history_export", "owner_isolation",
    "zero_new_analysis", "fatal_guards_stop_queue", "mock_discovery_distinct_from_retrieval",
    "preserved_consumption", "unchanged_business_pipeline", "missing_key_skips_without_provider_requests",
}
DECISION_FIELDS = {
    "schema_version", "approved", "approved_by", "source_revision", "target",
    "baseline_sha256", "gate_file", "gate_sha256", "snapshot_sha256",
    "scope_sha256", "policy", "webiq_secret_ref", "credential_origin", "credential_source",
}
require, sha, instant = release.require, prior.sha, row.instant


def now():
    return datetime.now(timezone.utc)


def path(work, name):
    return work / f"{PREFIX}-{name}.json"


def digest_file(candidate):
    """JSON record comparisons are semantic, not transport serialization order."""
    return sha(release.private_json(candidate, max_bytes=64 * 1024 * 1024))


def snapshot(work):
    return release.private_json(work / "row-rerun-actual-outcome.json", max_bytes=64 * 1024 * 1024)


def snapshot_records(work):
    value = snapshot(work)
    require(value["runtime"]["temporary_processing_closed"] is True
            and value["runtime"]["active_executions"] == [], "Retained snapshot must be closed")
    records = {}
    for key, entry in value["records"].items():
        raw = base64.b64decode(entry["base64"], validate=True)
        require(hashlib.sha256(raw).hexdigest() == entry["sha256"], "Captured raw record changed")
        records[key] = json.loads(raw)
    return records


def consumption(ledger):
    return {"executions": len(ledger["executions"]), "inference": ledger["attempted"]["inference"],
            **{name: ledger["reserved"][name] for name in ("input_tokens", "output_tokens")}}


def web_configuration(records, public_attribute_terms):
    """Construct nonsecret configuration at the owner's approved price basis."""
    from backend.core.websearch_policy import OptionalWebPolicy, PublicWebScope
    from backend.core.websearch_webiq import ENDPOINT
    from backend.real_pilot import RealPilotGuard

    approval = records["configuration/real-pilot-approval.json"]
    selected = records["configuration/real-pilot-row-rerun.json"]["selected_item_keys"]
    batch = records[f"batches/{approval['batch_id']}.json"]
    require(set(public_attribute_terms) == set(selected), "Explicit public attribute terms for both selected items required")
    items = {}
    for key in selected:
        item = next(item for item in batch["items"] if item["item_key"] == key)
        sources = [source for source in item["sources"]
                   if source["kind"] == "web" and source["source_tier"] == "manufacturer_web"]
        items[key] = PublicWebScope(
            manufacturer="Mueller", mpn=item["manifest"]["product"]["mpn"],
            attribute_terms=copy.deepcopy(public_attribute_terms[key]),
            source_ids=[source["source_id"] for source in sources],
            allowed_hosts=sorted({urlsplit(source["url"]).hostname for source in sources}),
        )
    optional_cost = RealPilotGuard._cost(approval, "inference", 52000, 4096, 0) + 25000
    policy = OptionalWebPolicy(
        batch_sha256=approval["batch_sha256"], items=items, max_cost_microdollars=optional_cost,
    )
    return {
        # Preserve the original two-item approval format as newer scopes add defaults.
        "public_web_policy": policy.model_dump(
            mode="json", exclude={"items": {"__all__": {"max_direct_page_attempts"}}},
        ),
        "web_environment": {"WEBSEARCH_PROVIDER": "webiq", "WEBIQ_ENDPOINT": ENDPOINT},
        "web_prices": {"search": "0.0125", "web_retrieval": "0"},
        "fixed_cost_microdollars": 198000,
    }


def amendment_scope(records, public_web_policy, web_environment, web_prices, *, fixed_cost_microdollars=198000):
    """Construct only the hash-bound scope; readiness supplies the later window."""
    from backend.real_pilot import GAPFILL_GRANTS

    approval = records["configuration/real-pilot-approval.json"]
    previous = records["configuration/real-pilot-row-rerun.json"]
    ledger = records["budgets/real-pilot.json"]
    require(consumption(ledger) == CONSUMED, "Cannot mint capacity from another baseline")
    selected = previous["selected_item_keys"]
    results = {}
    for item_key in selected:
        state = records[f"items/{approval['batch_id']}/{item_key}.json"]
        for _ in range(32):
            root = f"results/{approval['batch_id']}/{item_key}.json"
            result_key = state.get("result_key") or root
            if result_key in records:
                results[result_key] = sha(records[result_key])
            state = state.get("previous_attempt")
            if state is None:
                break
        else:
            raise ValueError("Prior history exceeds its bound")
    return {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
        "approval_sha256": sha(approval), "batch_sha256": approval["batch_sha256"],
        "ledger_sha256": sha(ledger), "prior_row_sha256": sha(previous),
        "prior_row_audit_sha256": sha(records["operations/real-pilot-row-rerun.json"]),
        "prior_execution_ids": sorted(ledger["executions"]), "selected_item_keys": selected,
        "item_sha256": {key: sha(records[f"items/{approval['batch_id']}/{key}.json"]) for key in selected},
        "result_sha256": results,
        "cached_documents": {key: sha(records[key]) for key in previous["cached_documents"]},
        "public_web_policy": public_web_policy, "web_environment": web_environment,
        "web_prices": web_prices, "fixed_cost_microdollars": fixed_cost_microdollars, **GAPFILL_GRANTS,
    }


def history(work):
    """Capture a finite preexisting baseline; later sibling artifacts are unrelated."""
    result = {}
    for candidate in sorted(work.rglob("*.json")):
        relative = candidate.relative_to(work)
        if relative.parts[0] == CHILD or {"source", "context"} & set(relative.parts):
            continue
        if len(relative.parts) == 1 and candidate.name.startswith(PREFIX + "-"):
            continue
        require(not candidate.is_symlink() and candidate.stat().st_mode & 0o077 == 0,
                "Retained records must remain owner-only regular files")
        result[str(relative)] = digest_file(candidate)
    return result


def verify_history(work, retained):
    require(isinstance(retained, dict) and retained, "Retained baseline must contain explicit record pins")
    for name, expected in retained.items():
        relative = Path(name)
        candidate = work / relative
        require(not relative.is_absolute() and ".." not in relative.parts
                and candidate.is_file() and not candidate.is_symlink()
                and candidate.resolve().is_relative_to(work.resolve())
                and candidate.stat().st_mode & 0o077 == 0
                and digest_file(candidate) == expected,
                "A pinned historical record changed or disappeared")


def original(work, config):
    require(prior.root(work) == work, "Continuation cannot move or mint another release root")
    approval, _ = row.original(work, config)
    closure = release.private_json(work / "row-rerun-closed.json")
    attempt = release.private_json(work / "row-rerun-worker-attempt.json")
    outcome = release.private_json(work / "row-rerun-worker-result.json")
    require(attempt["attempt"] == outcome["attempt"] == 4
            and closure["decision_sha256"] == attempt["decision_sha256"] == outcome["decision_sha256"]
            and closure["target"] == release.fingerprint(config), "Fourth attempt and closure must remain consumed")
    records = snapshot_records(work)
    ledger = records["budgets/real-pilot.json"]
    require(records["configuration/real-pilot-approval.json"] == approval
            and consumption(ledger) == CONSUMED and not ledger.get("invalidated"),
            "Exact original approval and consumption required")
    images = release.private_json(row.source_work(work) / "images.json")
    previous = {**config, **{kind + "_image": images[kind] for kind in ("backend", "frontend")}}
    require(snapshot(work)["runtime"]["backend_image"] == previous["backend_image"]
            == snapshot(work)["runtime"]["worker_image"], "Prior image and retained snapshot disagree")
    return approval, previous


def expected_worker_source():
    baseline = release.command(["git", "show", BASELINE_REVISION + ":backend/batch_worker.py"], cwd=release.ROOT)
    require(baseline.count(WORKER_INTERFACE_OLD.encode()) == 1, "Exact f56 worker interface prerequisite changed")
    return baseline.replace(WORKER_INTERFACE_OLD.encode(), WORKER_INTERFACE_NEW.encode(), 1)


def source_boundary(revision):
    require(isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision)
            and revision != BASELINE_REVISION, "Exact committed guard/helper successor required")
    release.command(["git", "merge-base", "--is-ancestor", BASELINE_REVISION, revision], cwd=release.ROOT)
    changed = set(release.command(
        ["git", "diff", "--name-only", BASELINE_REVISION, revision], cwd=release.ROOT,
    ).decode().splitlines())
    require({"backend/real_pilot.py", "backend/batch_worker.py", "scripts/gapfill_continuation.py"}
            <= changed <= SUCCESSOR_FILES,
            "Track A must preserve business-pipeline/frontend code apart from the exact guard interface exception")
    require(release.command(["git", "show", revision + ":backend/batch_worker.py"], cwd=release.ROOT)
            == expected_worker_source(),
            "Worker may contain only the exact gapfill scope exception; all Track B changes remain excluded")


def prepare(work, config, revision):
    with release.pilot_lock(work):
        approval, previous = original(work, config)
        source_boundary(revision)
        require(not path(work, "baseline").exists() and not (work / CHILD).exists(),
                "Gapfill preparation is single-use")
        release.save_once(path(work, "baseline"), {
            "work_root": str(work), "root_pin_sha256": digest_file(prior.ROOT_PIN),
            "source_revision": revision, "target": release.fingerprint(config),
            "approval_sha256": sha(approval), "history": history(work),
            "frontend_image": previous["frontend_image"],
        })
        release.stage(revision, work / CHILD)
        release.build_context(work / CHILD)


def evidence(work, decision):
    candidate = work / decision["gate_file"]
    require(candidate.parent == work and not candidate.is_symlink()
            and digest_file(candidate) == decision["gate_sha256"], "Pinned root-confined private gate changed")
    return release.private_json(candidate, max_bytes=64 * 1024 * 1024)


def decision_from_gate(work, config, gate_name="gapfill-private-gate.json", *,
                       credential_source="owner_env_file", secret_ref="webiq-gbbdemo"):
    """Construct, but do not write or activate, the exact approved continuation decision."""
    require(Path(gate_name).name == gate_name, "Private gate must remain inside the existing root")
    approval, _ = original(work, config)
    baseline = release.private_json(path(work, "baseline"))
    gate = release.private_json(work / gate_name, max_bytes=64 * 1024 * 1024)
    return {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
        "source_revision": baseline["source_revision"], "target": release.fingerprint(config),
        "baseline_sha256": digest_file(path(work, "baseline")),
        "gate_file": gate_name, "gate_sha256": digest_file(work / gate_name),
        "snapshot_sha256": gate["snapshot_sha256"], "scope_sha256": gate["scope_sha256"],
        "policy": copy.deepcopy(POLICY), "credential_origin": "GBBdemo",
        "credential_source": credential_source, "webiq_secret_ref": secret_ref,
    }


def validate(work, config, decision):
    approval, previous = original(work, config)
    require(set(decision) == DECISION_FIELDS and decision["schema_version"] == 1
            and type(decision["schema_version"]) is int and decision["approved"] is True
            and decision["approved_by"] == approval["approved_by"]
            and decision["target"] == release.fingerprint(config)
            and decision["credential_origin"] == "GBBdemo"
            and decision["credential_source"] in {"existing_job_secret", "owner_env_file", "unavailable"}
            and (decision["credential_source"] == "unavailable") == (decision["webiq_secret_ref"] is None)
            and (decision["webiq_secret_ref"] is None
                 or isinstance(decision["webiq_secret_ref"], str)
                 and re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,251}[a-z0-9])?", decision["webiq_secret_ref"]))
            and decision["policy"] == POLICY
            and all(type(value) is int for value in decision["policy"].values()), "Exact approved gapfill decision required")
    if decision["credential_source"] == "owner_env_file" and not path(work, "deployed").exists():
        existing_webiq_key()
    baseline = release.private_json(path(work, "baseline"))
    verify_history(work, baseline["history"])
    require(digest_file(path(work, "baseline")) == decision["baseline_sha256"]
            and baseline["root_pin_sha256"] == digest_file(prior.ROOT_PIN)
            and baseline["work_root"] == str(work)
            and baseline["source_revision"] == decision["source_revision"]
            and baseline["target"] == decision["target"] and baseline["approval_sha256"] == sha(approval)
            and baseline["frontend_image"] == previous["frontend_image"], "Root/source/history binding changed")
    source_boundary(decision["source_revision"])
    child = work / CHILD
    require(not child.is_symlink() and child.resolve() == child, "Source context escaped original root")
    require(release.verify_source(child)["revision"] == decision["source_revision"], "Exact source receipt changed")
    release.require_component_source_receipts(child, "backend")
    ci = release.private_json(child / "ci.json")
    require(ci.get("revision") == decision["source_revision"] and ci.get("merged") is True
            and ci.get("base") == "main" and ci.get("checks") == row.CI_CHECKS, "Exact merged CI required")
    for name in ("scripts/gapfill_continuation.py", "scripts/release.py", "scripts/pilot_continuation.py",
                 "scripts/row_continuation.py", "backend/real_pilot.py"):
        require((release.ROOT / name).read_bytes() == (child / "source" / name).read_bytes(),
                "Running guard/helper differs from exact staged source")
    gate = evidence(work, decision)
    require(gate.get("passed") is True and gate.get("no_live_operations") is True
            and gate.get("source_revision") == decision["source_revision"]
            and gate.get("snapshot_sha256") == decision["snapshot_sha256"]
            == digest_file(work / "row-rerun-actual-outcome.json")
            and gate.get("scope_sha256") == decision["scope_sha256"] == sha(gate["amendment_scope"])
            and GATE_CHECKS <= set(gate.get("checks", {}))
            and all(value is True for value in gate["checks"].values())
            and gate.get("full_internal_input_tokens") == 99646
            and gate.get("six_request_input_cap") == 151646
            and gate.get("six_request_output_cap") == 12288
            and gate.get("historical_forecasts_stacked") is False,
            "Real worker/real guard offline private gate must pass with all six complete payloads")
    records = snapshot_records(work)
    require(gate["remote_object_sha256"] == {key: sha(value) for key, value in records.items()},
            "Gate must protect every retained record, diagnostic and prior attempt")
    scope = gate["amendment_scope"]
    from backend.real_pilot import GAPFILL_FIELDS, GAPFILL_GRANTS
    require(set(scope) == GAPFILL_FIELDS - TIME_FIELDS and scope["approval_sha256"] == sha(approval)
            and scope["ledger_sha256"] == sha(records["budgets/real-pilot.json"])
            and scope["selected_item_keys"] == ["row-2", "row-3"]
            and all(scope[name] == value and type(scope[name]) is int for name, value in GAPFILL_GRANTS.items()),
            "Scope must preserve the exact baseline and minimum additive capacity")
    for attempt in work.glob(PREFIX + "-*-attempt.json"):
        require(release.private_json(attempt).get("decision_sha256") == sha(decision),
                "Consumed gapfill authority cannot change or retry")
    return approval, previous


def binding(decision):
    return {"decision_sha256": sha(decision), "target": decision["target"]}


def receipt(work, name, decision):
    value = release.private_json(path(work, name))
    require(all(value.get(key) == expected for key, expected in binding(decision).items()),
            "Gapfill receipt binding changed")
    return value


def ready(work, config, decision):
    with release.pilot_lock(work):
        validate(work, config, decision)
        require(decision["webiq_secret_ref"] is not None,
                "Existing GBBdemo key location/reference is unresolved; obtain owner input before starting readiness")
        require(release.command(["git", "rev-parse", "HEAD"], cwd=release.ROOT).decode().strip()
                == decision["source_revision"], "Readiness must use the exact merged successor")
        require(not path(work, "readiness").exists() and not list(work.glob(PREFIX + "-*-attempt.json")),
                "Readiness is create-once; never renew")
        anchor = now()
        previous = snapshot_records(work)["configuration/real-pilot-row-rerun.json"]
        require(anchor >= instant(previous["operating_expires_at"]), "Prior window must be closed")
        value = {**binding(decision), "readiness_at": anchor.isoformat(),
                 "publication_expires_at": (anchor + timedelta(seconds=2700)).isoformat(),
                 "overall_expires_at": (anchor + timedelta(seconds=5400)).isoformat()}
        release.save_once(path(work, "readiness"), value)
        release.save_once(path(work, "readiness-pin"), {**binding(decision), "sha256": sha(value)})
        return value


def window(work, decision, phase, minimum=0):
    if phase == "cleanup":
        return {}
    value = receipt(work, "readiness", decision)
    require(receipt(work, "readiness-pin", decision)["sha256"] == sha(value), "Readiness changed")
    anchor = instant(value["readiness_at"])
    require(instant(value["publication_expires_at"]) == anchor + timedelta(seconds=2700)
            and instant(value["overall_expires_at"]) == anchor + timedelta(seconds=5400),
            "Readiness deadlines cannot move")
    end = instant(value["publication_expires_at" if phase == "publication" else "overall_expires_at"])
    if phase == "processing":
        end = min(end, instant(receipt(work, "deployed", decision)["processing_expires_at"]))
    require(anchor <= now() < end and (end - now()).total_seconds() >= minimum,
            f"Gapfill {phase} window lacks its full {minimum}-second allowance")
    return value


@contextmanager
def live(work, decision, phase, minimum=0):
    """Check again at the actual upload/build/worker submission boundary."""
    window(work, decision, phase, minimum)
    originals = {name: getattr(release, name) for name in ("azure", "console_code", "upload_publication_context")}

    def guarded(name, function):
        def call(*args, **kwargs):
            needed = minimum
            if phase == "publication" and (name == "upload_publication_context" or args[:3] == ("rest", "--method", "POST")):
                needed = max(needed, 900)
            window(work, decision, phase, needed)
            if name in {"azure", "upload_publication_context"}:
                previous = kwargs.get("before_send")
                def before_send():
                    if previous is not None:
                        previous()
                    window(work, decision, phase, needed)
                kwargs["before_send"] = before_send
            return function(*args, **kwargs)
        return call

    try:
        for name, function in originals.items():
            setattr(release, name, guarded(name, function))
        yield
    finally:
        for name, function in originals.items():
            setattr(release, name, function)


def console(config, approval, packet, body, timeout=30):
    code, marker = row.remote_packet(packet, body)
    backend = release.app(config, config["backend"], timeout=timeout)
    release.require_pilot_identity(backend, approval["identities"]["api_principal_id"])
    return release.console_code([
        "az", "containerapp", "exec", "--subscription", config["subscription"],
        "-g", config["group"], "-n", config["backend"],
        "--revision", backend["properties"]["latestReadyRevisionName"],
        "--command", "/usr/bin/env PYTHON_BASIC_REPL=1 /app/.venv/bin/python -q", "--only-show-errors",
    ], code, marker, timeout=timeout)


def remote_snapshot(work, config, decision, approval):
    gate = evidence(work, decision)
    console(config, approval, {"objects": gate["remote_object_sha256"], "absent": [KEY, AUDIT]}, '''
for key, expected in packet["objects"].items():
    assert _sha256(read_json(store, key)[0]) == expected
for key in packet["absent"]:
    try:
        store.read_bytes(key)
    except Missing:
        pass
    else:
        raise ValueError("Gapfill already configured or consumed")
''')


def existing_webiq_key():
    """Read only the owner's existing ignored key file; never log or persist values."""
    from dotenv import dotenv_values

    candidate = release.ROOT / ".env.local"
    require(candidate.is_file() and not candidate.is_symlink()
            and candidate.stat().st_mode & 0o777 == 0o600,
            "Owner-provided .env.local must be a mode-0600 regular file")
    release.private_path(candidate)
    try:
        key = dotenv_values(candidate, interpolate=False).get("WEBIQ_API_KEY")
    except Exception:
        raise ValueError("Owner-provided WebIQ key could not be read; details withheld") from None
    require(isinstance(key, str) and bool(key) and key.isascii()
            and not any(ord(character) <= 32 or ord(character) == 127 for character in key),
            "Owner-provided WEBIQ_API_KEY is absent or malformed; never create another credential")
    return key


def credential_binding(job, decision, *, allow_install=False):
    """Resolve an existing reference, or plan owner-key binding on the existing job."""
    reference = decision["webiq_secret_ref"]
    if reference is None:
        return None
    secrets = job["properties"]["configuration"].get("secrets") or []
    require(isinstance(secrets, list)
            and all(isinstance(entry, dict) and not entry.get("value") for entry in secrets),
            "Existing GBBdemo secret metadata must not expose literal credential values")
    matches = [entry for entry in secrets if entry.get("name") == reference]
    if not matches and allow_install and decision.get("credential_source") == "owner_env_file":
        require(not secrets, "Owner-key binding cannot overwrite or reconstruct another job secret")
        existing_webiq_key()
    else:
        require(len(matches) == 1, "Existing GBBdemo job secret reference is unavailable; no credential creation")
    entry = release.environment_entries(release.safe_containers(job)[0]).get("WEBIQ_API_KEY")
    expected = {"name": "WEBIQ_API_KEY", "secretRef": reference}
    require(entry is None or entry == expected, "Worker WebIQ credential reference drift")
    return expected


def publish(work, config, decision):
    with release.pilot_lock(work):
        approval, previous = validate(work, config, decision)
        require(not path(work, "backend-attempt").exists(), "Backend publication consumed; no retry")
        with live(work, decision, "publication"):
            window(work, decision, "publication", 900)
            release.verify_target(previous)
            release.identity_contract(previous)
            release.require_real_pilot_off(prior.ready(previous, "backend"))
            prior.ready(previous, "frontend")
            credential = credential_binding(prior.require_worker(previous, approval), decision, allow_install=True)
            remote_snapshot(work, previous, decision, approval)
            capacity = prior.require_model_capacity(previous, approval)
            release.save_once(path(work, "capacity"), {
                **binding(decision), **capacity, "optional_web_credential_available": credential is not None,
                "credential_source": decision["credential_source"],
                "missing_credential_outcome": "not_configured_skip_without_provider_request",
            })
            attempt = path(work, "backend-attempt")
            release.save_once(attempt, {**binding(decision), "kind": "backend", "cpu": 2,
                                       "timeout_seconds": 900, "attempt_id": str(uuid.uuid4()),
                                       "capacity_sha256": digest_file(path(work, "capacity")),
                                       "status": "attempted_outcome_unknown"})
            value = window(work, decision, "publication", 900)
            result = release.execute_publication(
                config, work / CHILD, "backend", decision["source_revision"], attempt,
                expires=instant(value["publication_expires_at"]), validate_upload_window=True,
            )
            require(result["status"] == "Succeeded" and re.fullmatch(r"sha256:[a-f0-9]{64}", result["digest"]),
                    "Backend build must produce an immutable digest")
            release.save_once(path(work, "published"), {
                **binding(decision), **result,
                "backend_image": config["registry"] + ".azurecr.io/docintel/backend@" + result["digest"],
                "frontend_image": previous["frontend_image"],
            })


def current_config(work, config, decision):
    published = receipt(work, "published", decision)
    return {**config, **{kind + "_image": published[kind + "_image"] for kind in ("backend", "frontend")}}


def writable_job_configuration(configuration):
    """Remove two captured GET-only defaults, then use the pinned write schema."""
    require(isinstance(configuration, dict), "Job configuration must be an object")
    require(configuration.get("identitySettings") in (None, []) and configuration.get("dapr") is None,
            "Nonempty GET-only job settings cannot be preserved by API 2024-03-01")
    projected = copy.deepcopy(configuration)
    projected.pop("identitySettings", None)
    projected.pop("dapr", None)
    return release.write_schema().project_component("jobs", "PATCH", ("properties", "configuration"), projected)


class WorkerPatchFailure(ValueError):
    """Only bounded transport metadata may cross the private credential boundary."""

    def __init__(self, phase, *, http_status=None, curl_returncode=None):
        self.phase = phase if phase in {"configuration", "credential", "management_token", "worker_patch"} else "unknown"
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.curl_returncode = curl_returncode if type(curl_returncode) is int else None
        super().__init__(
            f"Existing-key worker PATCH did not confirm deployment: phase={self.phase}, "
            f"http_status={self.http_status}, curl_returncode={self.curl_returncode}; details withheld, no retry"
        )

    def metadata(self):
        return {"phase": self.phase, "http_status": self.http_status,
                "curl_returncode": self.curl_returncode, "retry_permitted": False}


def http_status(output):
    if isinstance(output, bytes) and re.fullmatch(rb"[1-5][0-9]{2}", output.strip()):
        return int(output.strip())
    return None


def patch(work, config, decision, label, resource, containers, *, job=False, bind_existing_secret=None):
    with live(work, decision, "cleanup" if label == "close" else "overall"):
        fresh, active = release.active_executions(config) if job else (release.app(config, config["backend"]), [])
        require(not active and fresh == resource, "Resource drift; never overwrite")
        candidate = path(work, label + "-patch")
        configuration = release.UNCHANGED_INGRESS
        if bind_existing_secret is not None:
            require(job and label == "deploy-worker" and decision["credential_source"] == "owner_env_file"
                    and bind_existing_secret == decision["webiq_secret_ref"]
                    and not resource["properties"]["configuration"].get("secrets"),
                    "Only the approved owner key may bind to the existing empty job secret store")
            configuration = writable_job_configuration(resource["properties"]["configuration"])
            configuration["secrets"] = [{"name": bind_existing_secret}]
        payload = release.build_update_payload(resource, containers, job=job, configuration=configuration)
        if bind_existing_secret is not None:
            release.save_once(path(work, "credential-binding"), {
                **binding(decision), "secret_ref": bind_existing_secret, "resource_id": resource["id"],
                "credential_origin": "GBBdemo", "source": "owner_env_file",
                "credential_created": False, "permissions_changed": False, "value_persisted_locally": False,
                "state": "binding_attempted_outcome_unknown",
            })
        release.save_once(candidate, payload)
        args = ["rest", "--method", "PATCH", "--url",
                "https://management.azure.com" + resource["id"] + "?api-version=2024-03-01",
                "--body", "@" + str(candidate)]
        if resource.get("etag"):
            args.extend(("--headers", "If-Match=" + resource["etag"]))
        if bind_existing_secret is None:
            release.azure(*args)
        else:
            try:
                secure_worker_patch(work, config, decision, resource, payload)
            except WorkerPatchFailure as error:
                try:
                    release.save_once(path(work, label + "-failure"), {
                        **binding(decision), **error.metadata(), "observed_at": now().isoformat(),
                    })
                except (OSError, ValueError):
                    raise error from None
                raise
        expected = prior.shape(resource)
        expected["template"]["containers"] = release.writable_containers(containers)
        if bind_existing_secret is not None:
            expected["configuration"]["secrets"] = [{"name": bind_existing_secret}]
        end = monotonic() + 180
        while True:
            actual, active = release.active_executions(config) if job else (release.app(config, config["backend"]), [])
            require(not active, "Worker became active during mutation")
            if prior.shape(actual) == expected:
                return
            require(prior.shape(actual) == prior.shape(resource) and monotonic() < end,
                    "Mutation not confirmed; no repeat request")
            sleep(5)


def secure_worker_patch(work, config, decision, resource, redacted_payload, *, check_window=None, on_preflight=None):
    """Compatibility wrapper retaining the original gapfill clock by default."""
    if check_window is None:
        check_window = lambda: window(work, decision, "overall", 600)
    return send_existing_secret_patch(
        config, resource, redacted_payload,
        secret_ref=decision.get("webiq_secret_ref"), check_window=check_window,
        **({"on_preflight": on_preflight} if on_preflight is not None else {}),
    )


def send_existing_secret_patch(config, resource, redacted_payload, *, secret_ref, check_window, on_preflight=None):
    """Namespace-independent transport; the only key source is the existing owner file."""
    expected_id = (f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}"
                   f"/providers/Microsoft.App/jobs/{config['job']}")
    require(resource["id"].lower() == expected_id.lower(), "Credential binding must target the existing manual job")
    url = "https://management.azure.com" + resource["id"] + "?api-version=2024-03-01"
    release.write_schema().validate_request("PATCH", url, redacted_payload)
    packet = {"contract": "job_secret", "method": "PATCH", "url": url, "timeout": 60,
              "payload": redacted_payload, "headers": {"Content-Type": "application/json"}}
    if resource.get("etag"):
        packet["headers"]["If-Match"] = resource["etag"]
    release._emit_preflight(on_preflight, release.native_http(packet), "worker_secret_redacted")
    require(callable(check_window), "A bounded operation clock check is required")
    check_window()
    phase = "configuration"
    status, returncode = None, None
    try:
        payload = copy.deepcopy(redacted_payload)
        secrets = payload["properties"]["configuration"]["secrets"]
        require(secrets == [{"name": secret_ref}], "Exact owner-key reference required")
        phase = "credential"
        secrets[0]["value"] = existing_webiq_key()
        release.write_schema().validate_request("PATCH", url, payload)
        phase = "management_token"
        token = release.azure("account", "get-access-token", "--subscription", config["subscription"],
                              "--resource", "https://management.azure.com/", timeout=30)
        bearer = token["accessToken"]
        require(isinstance(bearer, str) and bearer and bearer.isascii()
                and not any(ord(character) <= 32 or ord(character) == 127 for character in bearer)
                and token.get("subscription", config["subscription"]) == config["subscription"]
                and token.get("tenant", config["tenant"]) == config["tenant"],
                "Existing management identity binding changed")
        packet["headers"]["Authorization"] = "Bearer " + bearer
        packet["payload"] = payload
        release._emit_preflight(on_preflight, release.native_http(packet), "worker_secret_credential_bound")
        phase = "worker_patch"
        check_window()
        result = release.native_http(packet, send=True)
        status = result["http_status"]
        if status not in (200, 202):
            raise WorkerPatchFailure(phase, http_status=status)
    except WorkerPatchFailure:
        raise
    except subprocess.TimeoutExpired as error:
        raise WorkerPatchFailure(phase, http_status=http_status(error.stdout)) from None
    except Exception:
        raise WorkerPatchFailure(phase, http_status=status, curl_returncode=returncode) from None


def deploy(work, config, decision):
    with release.pilot_lock(work), live(work, decision, "overall", 600):
        approval, previous = validate(work, config, decision)
        current = current_config(work, config, decision)
        require(not path(work, "deploy-attempt").exists(), "Deployment already attempted")
        backend, frontend = (prior.ready(previous, kind) for kind in ("backend", "frontend"))
        release.require_real_pilot_off(backend)
        job = prior.require_worker(previous, approval)
        credential_binding(job, decision, allow_install=True)
        remote_snapshot(work, previous, decision, approval)
        release.save_once(path(work, "deploy-attempt"), {
            **binding(decision), "backend": prior.shape(backend), "worker": prior.shape(job),
            "frontend": prior.shape(frontend),
        })
        for label, resource, is_job in (("backend", backend, False), ("worker", job, True)):
            containers = release.safe_containers(resource)
            containers[0]["image"] = current["backend_image"]
            bind = (decision["webiq_secret_ref"] if is_job and decision["credential_source"] == "owner_env_file"
                    and not resource["properties"]["configuration"].get("secrets") else None)
            patch(work, previous, decision, "deploy-" + label, resource, containers, job=is_job,
                  bind_existing_secret=bind)
        prior.ready(current, "backend")
        credential_binding(prior.require_worker(current, approval), decision)
        if path(work, "credential-binding").exists():
            release.save_once(path(work, "credential-bound"), {
                **binding(decision), "secret_ref": decision["webiq_secret_ref"],
                "verified_name_only": True, "credential_created": False, "permissions_changed": False,
            })
        require(prior.shape(prior.ready(current, "frontend")) == prior.shape(frontend),
                "Frontend must remain unchanged")
        anchor = now()
        end = min(anchor + timedelta(seconds=1200), instant(window(work, decision, "overall")["overall_expires_at"]))
        require((end - anchor).total_seconds() >= 600, "Full worker no longer fits")
        release.save_once(path(work, "deployed"), {
            **binding(decision), "both_ready_at": anchor.isoformat(), "processing_expires_at": end.isoformat(),
        })


def amendment(work, decision):
    value, deployed = receipt(work, "readiness", decision), receipt(work, "deployed", decision)
    return {**evidence(work, decision)["amendment_scope"], "readiness_at": value["readiness_at"],
            "operating_expires_at": value["overall_expires_at"], "not_before": deployed["both_ready_at"],
            "expires_at": deployed["processing_expires_at"]}


def verify_auth(work, decision, candidate):
    value = receipt(work, "auth-preworker", decision)
    require(value.get("verified") is True and value.get("authenticated") is True
            and value.get("amendment_sha256") == sha(candidate)
            and value.get("backend_image") == receipt(work, "published", decision)["backend_image"]
            and value.get("owner") == snapshot(work)["owner"]
            and value.get("batch_id") == snapshot(work)["batch_id"]
            and instant(candidate["not_before"]) <= instant(value["observed_at"]) <= now(),
            "Externally supplied authenticated owner/batch/image/amendment receipt required")


def active_target(work, config, decision):
    approval, _ = original(work, config)
    current = current_config(work, config, decision)
    release.verify_target(current)
    release.identity_contract(current)
    baseline = receipt(work, "deploy-attempt", decision)
    for kind, actual in (
        ("backend", prior.ready(current, "backend")), ("frontend", prior.ready(current, "frontend")),
        ("worker", prior.require_worker(current, approval)),
    ):
        expected = copy.deepcopy(baseline[kind])
        if kind != "frontend":
            expected["template"]["containers"][0]["image"] = current["backend_image"]
        if kind == "worker" and path(work, "credential-binding").exists():
            credential = receipt(work, "credential-binding", decision)
            require(credential["secret_ref"] == decision["webiq_secret_ref"]
                    and credential["resource_id"] == actual["id"]
                    and receipt(work, "credential-bound", decision)["secret_ref"] == credential["secret_ref"],
                    "Bound credential reference changed")
            expected["configuration"]["secrets"] = [{"name": credential["secret_ref"]}]
        if kind == "backend" and path(work, "enablement").exists():
            entries = release.environment_entries(expected["template"]["containers"][0])
            entries.update(receipt(work, "enablement", decision)["intended"])
            expected["template"]["containers"][0]["env"] = list(entries.values())
        require(prior.shape(actual) == expected, "Deployed resource drift before activation")
    return current


def configure(work, config, decision):
    approval, _ = validate(work, config, decision)
    candidate = amendment(work, decision)
    verify_auth(work, decision, candidate)
    release.save_once(path(work, "configure-attempt"), {**binding(decision), "amendment": candidate})
    packet = {"approval_sha256": sha(approval), "batch_id": approval["batch_id"], "amendment": candidate}
    body = f'''from backend.real_pilot import validate_gapfill
approval, _ = read_json(store, "configuration/real-pilot-approval.json")
assert _sha256(approval) == packet["approval_sha256"]
os.environ["DOCINTEL_REAL_PILOT_OPERATOR_IDS"] = approval["approved_by"]
batch_key = "batches/" + packet["batch_id"] + ".json"
with store.lease(packet["batch_id"]) as batch_fence, store.lease(BUDGET_KEY) as budget_fence:
    batch, version = read_json(store, batch_key)
    ledger, _ = read_json(store, BUDGET_KEY)
    assert validate_gapfill(packet["amendment"], approval, batch, ledger, store) == packet["amendment"]
    assert batch["state"] in ("completed", "deferred")
    batch_fence()
    budget_fence()
    write_json(store, {KEY!r}, packet["amendment"])
    batch["state"] = "queued"
    batch_fence()
    budget_fence()
    write_json(store, batch_key, batch, version)
'''
    console(config, approval, packet, body)
    release.save_once(path(work, "configured"), {**binding(decision), "amendment_sha256": sha(candidate)})


def enable(work, config, decision):
    approval, _ = original(work, config)
    config = current_config(work, config, decision)
    backend = prior.ready(config, "backend")
    release.require_real_pilot_off(backend)
    containers = release.safe_containers(backend)
    entries = release.environment_entries(containers[0])
    require(entries.get("DOCINTEL_PILOT_UPLOAD_ENABLED") in (
        None, {"name": "DOCINTEL_PILOT_UPLOAD_ENABLED", "value": "false"}), "Source upload must remain disabled")
    intended = release.pilot_values(approval)
    release.save_once(path(work, "enablement"), {
        **binding(decision), "baseline": {name: entries.get(name) for name in intended},
        "intended": {name: {"name": name, "value": value} for name, value in intended.items()},
    })
    release.merge_environment(containers[0], intended)
    patch(work, config, decision, "enable", backend, containers)
    prior.ready(config, "backend")


def start(work, config, decision):
    approval, _ = original(work, config)
    config = active_target(work, config, decision)
    candidate = amendment(work, decision)
    verify_auth(work, decision, candidate)
    receipt(work, "configured", decision)
    require(not path(work, "worker-attempt").exists(), "Fifth worker attempt consumed; never retry")
    job = prior.require_worker(config, approval)
    template = copy.deepcopy(job["properties"]["template"])
    container = template["containers"][0]
    credential = credential_binding(job, decision)
    container["command"] = ["/app/.venv/bin/python"]
    container["args"] = ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", approval["batch_id"],
                         "--concurrency", "1", "--max-batches", "1", "--item-limit", "2"]
    release.merge_environment(container, {
        **approval["environment"], **release.pilot_values(approval), **candidate["web_environment"],
        "DOCINTEL_REAL_PILOT_EXECUTION_SCOPE": "full", "DOCINTEL_OPTIONAL_WEB_GAPFILL_ENABLED": "true",
        "DOCINTEL_OPTIONAL_WEB_GAPFILL_POLICY_JSON": json.dumps(candidate["public_web_policy"], sort_keys=True),
    })
    entries = release.environment_entries(container)
    entries.pop("WEBIQ_SUBSCRIPTION_KEY", None)
    if credential is None:
        entries.pop("WEBIQ_API_KEY", None)
    else:
        entries["WEBIQ_API_KEY"] = credential
    container["env"] = list(entries.values())
    template = release.writable_execution_template(template)
    capacity = prior.require_model_capacity(config, approval)
    fresh, active = release.active_executions(config)
    require(not active and fresh == job, "Worker changed or active before start")
    release.save_once(path(work, "worker-template"), template)
    release.save_once(path(work, "worker-attempt"), {
        **binding(decision), "attempt": 5, "attempted_at": now().isoformat(),
        "amendment_sha256": sha(candidate), "execution_sha256": sha(template), "model_capacity": capacity,
        "optional_web_credential_available": credential is not None,
        "missing_credential_outcome": "not_configured_skip_without_provider_request",
        "status": "start_attempted_completion_unknown",
    })
    window(work, decision, "processing", 600)
    result = release.azure("containerapp", "job", "start", "--subscription", config["subscription"],
                           "-g", config["group"], "-n", config["job"], "--yaml", str(path(work, "worker-template")),
                           resource_snapshot=fresh, before_send=lambda: window(work, decision, "processing", 600))
    require(isinstance(result, dict) and isinstance(result.get("name"), str) and result["name"],
            "Worker outcome unknown; inspect the same attempt, never retry")
    release.save_once(path(work, "worker-result"), {**binding(decision), "execution_name": result["name"], "attempt": 5})


def observe(work, config, decision):
    execution = receipt(work, "worker-result", decision)["execution_name"]
    end = instant(amendment(work, decision)["expires_at"])
    while now() < end:
        try:
            with live(work, decision, "processing"):
                values = release.azure("containerapp", "job", "execution", "list", "--subscription", config["subscription"],
                                       "-g", config["group"], "-n", config["job"], timeout=min(30, (end - now()).total_seconds()))
            matches = [value for value in values if value.get("name") == execution]
            require(len(matches) == 1, "Reserved execution not uniquely visible")
            properties = matches[0]["properties"]
        except Exception:
            release.save_once(path(work, "warning-" + uuid.uuid4().hex), {
                **binding(decision), "reason": "usage_or_status_unavailable", "observed_at": now().isoformat(),
                "action": "continue_without_retry_or_stop",
            })
        else:
            try:
                with live(work, decision, "processing"):
                    usage(work, config, decision, timeout=min(30, (end - now()).total_seconds()))
            except Exception:
                release.save_once(path(work, "warning-" + uuid.uuid4().hex), {
                    **binding(decision), "reason": "usage_unavailable", "observed_at": now().isoformat(),
                    "action": "continue_without_retry_or_stop",
                })
            if properties["status"] in release.TERMINAL:
                release.save_once(path(work, "worker-terminal"), {
                    **binding(decision), "execution_name": execution, "properties": properties,
                })
                require(properties["status"] == "Succeeded", "Worker failed; no retry")
                return
        sleep(min(15, max(0, (end - now()).total_seconds())))
    release.save_once(path(work, "expiry-stop-attempt"), {**binding(decision), "execution_name": execution})
    release.azure("containerapp", "job", "stop", "--subscription", config["subscription"],
                  "-g", config["group"], "-n", config["job"], "--job-execution-name", execution)
    raise ValueError("Authorization expired; only the reserved execution was stopped")


def usage(work, config, decision, *, timeout=30):
    candidate = amendment(work, decision)
    approval = snapshot_records(work)["configuration/real-pilot-approval.json"]
    packet = {"approval_sha256": sha(approval), "amendment_sha256": sha(candidate),
              "prior_execution_ids": candidate["prior_execution_ids"]}
    body = '''
ledger, _ = read_json(store, BUDGET_KEY)
assert ledger["approval_sha256"] == packet["approval_sha256"]
entry = ledger.get("gapfill")
assert entry and entry["sha256"] == packet["amendment_sha256"]
assert set(ledger["executions"]) == set(packet["prior_execution_ids"]) | {entry["execution_id"]}
reservations = [value for value in ledger["reservations"].values()
                if value["execution_id"] == entry["execution_id"]]
value = {"incremental_reserved_microdollars": ledger["reserved"]["microdollars"] - entry["cost_before"],
         "incremental_input_tokens": ledger["reserved"]["input_tokens"] - entry["input_before"],
         "incremental_output_tokens": ledger["reserved"]["output_tokens"] - entry["output_before"],
         "usage_reporting_complete": all(value["actual_usage"] is not None for value in reservations),
         "actual_billed_microdollars": None, "reservation_count": len(reservations)}
print("DOCINTEL_GAPFILL_USAGE:" + base64.b64encode(json.dumps(value).encode()).decode())
'''
    output = console(config, approval, packet, body, timeout=timeout)
    lines = [line.removeprefix("DOCINTEL_GAPFILL_USAGE:") for line in output.decode().splitlines()
             if line.startswith("DOCINTEL_GAPFILL_USAGE:")]
    require(len(lines) == 1, "Usage probe did not return one bound result")
    value = json.loads(base64.b64decode(lines[0], validate=True))
    release.save_once(path(work, "usage-" + uuid.uuid4().hex), {
        **binding(decision), **value, "observed_at": now().isoformat(),
        "fixed_cost_upper_microdollars": candidate["fixed_cost_microdollars"],
        "fresh_incremental_upper_microdollars":
            value["incremental_reserved_microdollars"] + candidate["fixed_cost_microdollars"],
        "historical_forecasts_stacked": False, "advisory_only": True,
    })
    return value


def close(work, config, decision):
    # Closure deliberately does not depend on gate/CI validation or an unexpired clock.
    config = current_config(work, config, decision)
    with release.pilot_lock(work), live(work, decision, "cleanup"):
        backend = release.app(config, config["backend"])
        if path(work, "enablement").exists():
            enabled = receipt(work, "enablement", decision)
            containers = release.safe_containers(backend)
            entries = release.environment_entries(containers[0])
            actual = {name: entries.get(name) for name in enabled["baseline"]}
            require(actual in (enabled["baseline"], enabled["intended"]), "Unsafe enablement drift during closure")
            if actual != enabled["baseline"]:
                release.save_once(path(work, "close-attempt"), binding(decision))
                for name, value in enabled["baseline"].items():
                    if value is None:
                        entries.pop(name, None)
                    else:
                        entries[name] = value
                containers[0]["env"] = list(entries.values())
                patch(work, config, decision, "close", backend, containers)
        release.require_real_pilot_off(prior.ready(config, "backend"))
        frontend = receipt(work, "deploy-attempt", decision)["frontend"]
        frontend["template"]["containers"] = release.writable_containers(frontend["template"]["containers"])
        require(prior.shape(prior.ready(config, "frontend")) == frontend,
                "Frontend drift detected at closure")
        if not path(work, "closed").exists():
            release.save_once(path(work, "closed"), binding(decision))


def activate(work, config, decision):
    with release.pilot_lock(work):
        validate(work, config, decision)
        window(work, decision, "processing", 600)
        require(not path(work, "closed").exists(), "Activation already closed")
        release.save_once(path(work, "activation-attempt"), binding(decision))
    current = current_config(work, config, decision)
    try:
        with live(work, decision, "processing", 600):
            active_target(work, config, decision)
            configure(work, config, decision)
            enable(work, config, decision)
            start(work, config, decision)
        observe(work, current, decision)
    finally:
        close(work, current, decision)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "ready", "publish", "deploy", "activate", "close"))
    parser.add_argument("--work", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--approve")
    options = parser.parse_args()
    work = prior.root(options.work)
    config = release.load_config(options.config or work / "target.json")
    if options.action == "prepare":
        prepare(work, config, options.revision)
    else:
        decision = release.private_json(options.decision)
        if options.action == "check":
            validate(work, config, decision)
        else:
            require(options.approve == options.action, "Explicit bounded action approval required")
            globals()[options.action](work, config, decision)
    print("Gapfill action confirmed; prior approvals, history and consumption preserved.")


if __name__ == "__main__":
    main()
