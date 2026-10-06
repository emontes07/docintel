"""Append-only row-rerun authority and opt-in, network-free private acceptance.

The private loader uses DOCINTEL_TEST_PDF_ROWS_WORK/DOCUMENTS. Optional
DOCINTEL_TEST_ROW_RERUN_OUTPUT creates new mode-0600 artifacts in that same WORK.
DOCINTEL_TEST_ROW_RERUN_HOSTED_RECONCILIATION loads a root-confined descriptor
and its hash-bound raw-record bundle, without reconciling or fetching anything.
No hosted state, original allowance, extraction code, or retained input is changed.

The descriptor extends the helper's reconciliation contract with records_file
and records_sha256; its bundle is {"records": {key: {base64, sha256, version}}}.
All captured bytes and versions are preserved. Exclude lease records. Only
missing approved document blobs may be filled from hash-identical local files.
An optional snapshot_file names the root snapshot; otherwise its hash must
identify exactly one root JSON file. All these files must be owner-only.

Hosted-capture mode requires clean committed source, row-rerun-baseline.json
and target.json, and emits OUTPUT-gate.json after both real worker simulations
pass. Without a descriptor, the old reconstructed gate remains preliminary and
cannot emit a helper-consumable gate. Output workbooks carry a notice worksheet;
owner-isolation checks are local, never presented as hosted verification.
"""

import base64
import copy
import json
import os
import re
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET

import pytest

from backend import batch_worker as worker, extract, real_pilot
from backend.batch import BatchService
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.extract import ExecutionConfigurationError
from backend.models.enrichment import ExtractionResponse
from backend.workbooks import MAIN, REL, read_workbook, write_workbook
from scripts import row_continuation as row_helper
from tests.test_final_rerun import (
    all_records, configured, final_case, network_blocked, recovery_case,  # noqa: F401
)
from tests.test_interrupted_reconciliation import request_for
from tests.test_private_pdf_rows import (
    MemoryStore, configure_offline, hydrate, item_summary, no_network,  # noqa: F401
    private_inputs, put, reconcile_private, request_record, response_for, sha,  # noqa: F401
    verify_exports, write_private,
)


ROW_KEY = "configuration/real-pilot-row-rerun.json"
ROW_AUDIT = "operations/real-pilot-row-rerun.json"
IMMUTABLE_BASELINE = "34fd22e13056ef8fea01cc073a573e9f395a6796"
IMMUTABLE_FILES = (
    "backend/batch_worker.py", "backend/extract.py", "backend/pdf_presentation.py",
    "backend/evidence_verification.py", "backend/response_validation.py",
    "backend/models/enrichment.py", "backend/core/instructions.py", "backend/multisource.py",
    "backend/batch.py", "frontend/components/enrichment-review.tsx",
)


class WorkerMemoryStore(MemoryStore):
    """Keep the production worker heartbeat and SQLite CAS in one isolated store."""

    def __init__(self, records):
        self.mutex = threading.RLock()
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.execute("CREATE TABLE records (key TEXT PRIMARY KEY, value BLOB NOT NULL, version TEXT NOT NULL)")
        self.connection.executemany(
            "INSERT INTO records VALUES (?,?,?)",
            [(key, raw, version) for key, (raw, version) in records.items()],
        )
        self.connection.commit()

    @contextmanager
    def connect(self):
        with self.mutex, self.connection:
            yield self.connection


def private_json(root, name):
    candidate = Path(name)
    candidate = candidate if candidate.is_absolute() else root / candidate
    assert candidate.parent.resolve() == root.resolve() and not candidate.is_symlink()
    assert candidate.is_file() and candidate.stat().st_mode & 0o077 == 0
    raw = candidate.read_bytes()
    return candidate, raw, json.loads(raw)


def inject_approved_documents(store, batch, documents):
    selected = read_json(store, real_pilot.FINAL_RERUN_KEY)[0]["selected_item_keys"]
    needed = {source["blob"]: source["sha256"] for item in batch["items"] if item["item_key"] in selected
              for source in item["sources"] if source["kind"] == "blob"}
    missing = {}
    for key, digest in needed.items():
        try:
            assert sha(store.read_bytes(key)[0]) == digest
        except Missing:
            missing[key] = digest
    injected = {}
    if missing:
        for candidate in documents.iterdir():
            if candidate.is_file() and not candidate.is_symlink() and candidate.suffix.lower() in {".pdf", ".xlsx"}:
                raw = candidate.read_bytes()
                for key, digest in list(missing.items()):
                    if sha(raw) == digest:
                        store.write_bytes(key, raw)
                        injected[key] = digest
                        del missing[key]
    assert not missing, "Approved local copies must match every missing document hash"
    return injected


def load_hosted_records(root, descriptor_name, documents):
    descriptor_path, descriptor_raw, descriptor = private_json(root, descriptor_name)
    required = {"verified", "status_only", "snapshot_sha256", "consumption", "ledger_sha256",
                "prior_final_audit_sha256", "object_sha256", "records_file", "records_sha256"}
    assert required <= set(descriptor)
    assert descriptor["verified"] is True and descriptor["status_only"] is True
    assert descriptor["consumption"] == row_helper.CONSUMED
    records_path, records_raw, bundle = private_json(root, descriptor["records_file"])
    assert records_path != descriptor_path and sha(records_raw) == descriptor["records_sha256"]
    assert set(bundle) == {"records"} and isinstance(bundle["records"], dict)
    records = {}
    for key, entry in bundle["records"].items():
        assert isinstance(key, str) and not key.startswith(("/", "locks/")) and ".." not in key.split("/")
        assert set(entry) == {"base64", "sha256", "version"}
        assert isinstance(entry["version"], str) and entry["version"]
        raw = base64.b64decode(entry["base64"], validate=True)
        assert sha(raw) == entry["sha256"]
        records[key] = (raw, entry["version"])
    assert records and descriptor["object_sha256"] == {key: sha(value[0]) for key, value in records.items()}
    assert ROW_KEY not in records and ROW_AUDIT not in records
    if "snapshot_file" in descriptor:
        snapshot_path, snapshot_raw, snapshot = private_json(root, descriptor["snapshot_file"])
    else:
        matches = [candidate for candidate in root.glob("*.json")
                   if not candidate.is_symlink() and sha(candidate.read_bytes()) == descriptor["snapshot_sha256"]]
        assert len(matches) == 1, "An unambiguous hash-bound root snapshot is required"
        snapshot_path, snapshot_raw, snapshot = private_json(root, matches[0])
    assert sha(snapshot_raw) == descriptor["snapshot_sha256"]
    assert snapshot["verified"] is True and snapshot["temporary_processing_closed"] is True
    assert type(snapshot["active_executions"]) is int and snapshot["active_executions"] == 0
    assert snapshot["consumption"] == row_helper.CONSUMED
    store = WorkerMemoryStore(records)
    try:
        approval = read_json(store, real_pilot.APPROVAL_KEY)[0]
        batch = read_json(store, f"batches/{approval['batch_id']}.json")[0]
        ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
        audit = read_json(store, real_pilot.FINAL_RERUN_AUDIT_KEY)[0]
        assert {
            "executions": len(ledger["executions"]), "inference": ledger["attempted"]["inference"],
            "input_tokens": ledger["reserved"]["input_tokens"], "output_tokens": ledger["reserved"]["output_tokens"],
        } == row_helper.CONSUMED
        assert "row_rerun" not in ledger
        assert real_pilot._sha256(ledger) == descriptor["ledger_sha256"] == snapshot["ledger_sha256"]
        assert real_pilot._sha256(audit) == descriptor["prior_final_audit_sha256"] == snapshot["prior_final_audit_sha256"]
        assert batch["state"] == "interrupted"
        previous = read_json(store, real_pilot.FINAL_RERUN_KEY)[0]
        assert previous == audit["amendment"]
        for item in batch["items"]:
            state = read_json(store, f"items/{batch['id']}/{item['item_key']}.json")[0]
            if item["item_key"] in previous["selected_item_keys"]:
                assert state["state"] in {"unresolved", "interrupted"}
                assert state["recovery_sha256"] == real_pilot._sha256(previous)
                if state["state"] == "interrupted":
                    intent_key = state["interruption"]["audit_key"]
                    intent = read_json(store, intent_key)[0]
                    applied = read_json(store, intent_key.removesuffix(".json") + ".applied.json")[0]
                    assert intent["kind"] == "interrupted_reconciliation_intent"
                    assert applied["kind"] == "interrupted_reconciliation_applied"
                    assert applied["execution_id"] == state["interruption"]["execution_id"] == audit["execution_id"]
                    assert applied["audit_key"] == intent_key and intent["current_attempt_result_absent"] is True
                    assert applied["audit_sha256"] == real_pilot._sha256(intent)
                    for name, key in (("item", f"items/{batch['id']}/{item['item_key']}.json"),
                                      ("batch", f"batches/{batch['id']}.json")):
                        raw, version = store.read_bytes(key)
                        assert applied["records"][name] == {"sha256": sha(raw), "version": version}
                        assert json.loads(raw) == intent["replacement"][name]
                        prior_raw = base64.b64decode(intent["prior"][name]["base64"], validate=True)
                        assert sha(prior_raw) == intent["prior"][name]["sha256"]
                    prior_state = json.loads(base64.b64decode(intent["prior"]["item"]["base64"], validate=True))
                    assert prior_state["state"] == "running" and state["interruption"]["remote_completion"] == "unknown"
                    assert state["interruption"]["automatic_retry"] is False
                    changing = {"state", "error", "interruption"}
                    assert {key: value for key, value in state.items() if key not in changing} == {
                        key: value for key, value in prior_state.items() if key not in changing
                    }
                    for name, key in (("ledger", real_pilot.BUDGET_KEY),
                                      ("execution_audit", real_pilot.FINAL_RERUN_AUDIT_KEY)):
                        raw, version = store.read_bytes(key)
                        assert intent["request"]["expected"][name] == {"sha256": sha(raw), "version": version}
            else:
                assert state["state"] == "deferred"
        bindings = result_bindings(store, batch, previous["selected_item_keys"])
        assert any(value is None for value in bindings.values())
        for key, digest in previous["result_sha256"].items():
            assert sha(store.read_bytes(f"results/{batch['id']}/{key}.json")[0]) == digest
        for key, digest in previous["cached_documents"].items():
            assert sha(store.read_bytes(key)[0]) == digest
        injected = inject_approved_documents(store, batch, documents)
        assert all(store.read_bytes(key) == value for key, value in records.items())
    except BaseException:
        store.connection.close()
        raise
    return store, batch, approval, {
        "scope": "local replay of operator-captured raw records; no hosted access performed",
        "descriptor_file": descriptor_path.name, "descriptor_sha256": sha(descriptor_raw),
        "snapshot_file": snapshot_path.name, "snapshot_sha256": sha(snapshot_raw),
        "records_file": records_path.name, "records_sha256": sha(records_raw),
        "ledger_sha256": descriptor["ledger_sha256"],
        "prior_final_audit_sha256": descriptor["prior_final_audit_sha256"],
        "object_sha256": descriptor["object_sha256"], "injected_local_document_sha256": injected,
        "locally_reconciled": False, "hosted_verification_performed": False,
        "input_files": {path.name: sha(raw) for path, raw in (
            (descriptor_path, descriptor_raw), (snapshot_path, snapshot_raw), (records_path, records_raw),
        )},
    }


def helper_bindings(root, approval, revision, hosted):
    _, baseline_raw, baseline = private_json(root, "row-rerun-baseline.json")
    _, target_raw, target = private_json(root, "target.json")
    assert baseline["work_root"] == str(root.resolve())
    assert baseline["baseline_revision"] == IMMUTABLE_BASELINE
    assert baseline["source_revision"] == revision
    assert baseline["approval_sha256"] == real_pilot._sha256(approval)
    assert baseline["target"] == row_helper.release.fingerprint(target)
    assert baseline["history"] == row_helper.history(root)
    assert baseline["history_sha256"] == real_pilot._sha256(baseline["history"])
    return {
        "baseline_sha256": sha(baseline_raw), "baseline_revision": IMMUTABLE_BASELINE,
        "target_sha256": sha(target_raw), "target": baseline["target"],
        "source_revision": revision, "approval_sha256": baseline["approval_sha256"],
        "history_sha256": baseline["history_sha256"],
        "snapshot_file": hosted["snapshot_file"], "snapshot_sha256": hosted["snapshot_sha256"],
        "reconciliation_file": hosted["descriptor_file"], "reconciliation_sha256": hosted["descriptor_sha256"],
        "records_file": hosted["records_file"], "records_sha256": hosted["records_sha256"],
    }


def helper_gate(bindings, hosted, approval, cases, source, checks):
    assert set(checks) == row_helper.GATE_CHECKS and all(value is True for value in checks.values())
    assert [case["mode"] for case in cases] == ["supported_pdf", "full_fallthrough"]
    assert [case["request_totals"] for case in cases] == [
        {"input_tokens": 89596, "output_tokens": 8192}, {"input_tokens": 92146, "output_tokens": 8192},
    ]
    assert row_helper.capacity(approval)["input_tokens"] == 92146
    scopes = [{key: value for key, value in case["amendment"].items() if key not in row_helper.TIME_FIELDS}
              for case in cases]
    assert scopes[0] == scopes[1] and set(scopes[0]) == row_helper.AMENDMENT_FIELDS - row_helper.TIME_FIELDS
    pins = hosted["object_sha256"]
    scope = scopes[0]
    assert scope["approval_sha256"] == bindings["approval_sha256"] == real_pilot._sha256(approval)
    assert scope["ledger_sha256"] == hosted["ledger_sha256"]
    assert scope["prior_final_audit_sha256"] == hosted["prior_final_audit_sha256"]
    required = {real_pilot.APPROVAL_KEY, real_pilot.BUDGET_KEY, real_pilot.FINAL_RERUN_KEY,
                real_pilot.FINAL_RERUN_AUDIT_KEY, f"batches/{approval['batch_id']}.json"}
    required |= {f"items/{approval['batch_id']}/{key}.json" for key in scope["selected_item_keys"]}
    required |= set(scope["cached_documents"])
    required |= {key for key, value in scope["result_sha256"].items() if value is not None}
    assert required <= set(pins)
    assert any(key.startswith("operations/interrupted-reconciliation/") and key.endswith(".applied.json") for key in pins)
    assert all(pins[f"items/{approval['batch_id']}/{key}.json"] == value for key, value in scope["item_sha256"].items())
    assert all(pins[key] == value for key, value in scope["cached_documents"].items())
    assert all(pins[key] == value for key, value in scope["result_sha256"].items() if value is not None)
    rates = row_helper.RATES
    direct = 2 * 2 * 900 * Decimal(rates["build"]) + 600 * (
        Decimal(rates["worker_cpu"]) + 2 * Decimal(rates["worker_memory"]))
    direct += 92146 * Decimal(rates["input"]) + 8192 * Decimal(rates["output"])
    assert direct == Decimal("0.726132")
    return {
        "schema_version": 1, "passed": True, "no_live_operations": True, **bindings,
        "label": "REPRODUCTION ONLY; local production-path assertions, not model findings",
        "checks": checks, "amendment_scope": scope, "scope_sha256": real_pilot._sha256(scope),
        "remote_object_sha256": pins, "planned_input_tokens": 89596,
        "full_fallthrough_input_tokens": 92146, "full_output_tokens": 8192,
        "maximum_direct_microdollars": int(direct * 1000000), "verified_rates": rates,
        "incidentals_disclosure_only": True, "historical_forecasts_stacked": False,
        "owner_isolation_scope": "local BatchService detail/export assertions only; not hosted verification",
        "cases_are_independent_offline_alternatives": True, "hosted_verification_performed": False,
        "source_attestation": source,
    }


def reproduction_workbook(content):
    notice = write_workbook({"REPRODUCTION": [
        ["Notice"], ["REPRODUCTION ONLY — NOT ACTUAL MODEL OUTPUT OR PRODUCTION PRODUCT FINDINGS"],
        ["Independent offline scenario; no hosted calls. Production-mode fields describe the exercised code path."],
    ]})
    with ZipFile(BytesIO(content)) as original, ZipFile(BytesIO(notice)) as labeled:
        entries = {name: original.read(name) for name in original.namelist()}
        workbook = ET.fromstring(entries["xl/workbook.xml"])
        sheets = workbook.find(f"{{{MAIN}}}sheets")
        assert sheets is not None and all(sheet.attrib["name"] != "REPRODUCTION" for sheet in sheets)
        relation = "rIdReproduction"
        sheets.insert(0, ET.Element(f"{{{MAIN}}}sheet", name="REPRODUCTION",
                                   sheetId=str(max(int(sheet.attrib["sheetId"]) for sheet in sheets) + 1),
                                   attrib={f"{{{REL}}}id": relation}))
        relationships = ET.fromstring(entries["xl/_rels/workbook.xml.rels"])
        namespace = "http://schemas.openxmlformats.org/package/2006/relationships"
        assert all(entry.attrib["Id"] != relation for entry in relationships)
        ET.SubElement(relationships, f"{{{namespace}}}Relationship", Id=relation, Type=f"{REL}/worksheet",
                      Target="worksheets/reproduction.xml")
        types = ET.fromstring(entries["[Content_Types].xml"])
        ET.SubElement(types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
                      PartName="/xl/worksheets/reproduction.xml",
                      ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
        entries.update({
            "xl/workbook.xml": ET.tostring(workbook), "xl/_rels/workbook.xml.rels": ET.tostring(relationships),
            "[Content_Types].xml": ET.tostring(types),
            "xl/worksheets/reproduction.xml": labeled.read("xl/worksheets/sheet1.xml"),
        })
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as output:
        for name, raw in entries.items():
            output.writestr(name, raw)
    result = stream.getvalue()
    assert {key: value for key, value in read_workbook(result).items() if key != "REPRODUCTION"} == read_workbook(content)
    return result


def result_bindings(store, batch, selected):
    bindings = {}
    for item_key in selected:
        state = read_json(store, f"items/{batch['id']}/{item_key}.json")[0]
        depth = 0
        while state is not None:
            assert isinstance(state, dict) and depth < 32
            root = f"results/{batch['id']}/{item_key}"
            key = state.get("result_key") or root + ".json"
            assert key == root + ".json" or re.fullmatch(re.escape(root) + r"/attempts/[a-f0-9]{64}\.json", key)
            try:
                bindings[key] = sha(store.read_bytes(key)[0])
            except Missing:
                bindings[key] = None
            state = state.get("previous_attempt")
            depth += 1
    return bindings


def row_amendment(store, batch, approval, ready=None):
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    previous = read_json(store, real_pilot.FINAL_RERUN_KEY)[0]
    audit = read_json(store, real_pilot.FINAL_RERUN_AUDIT_KEY)[0]
    ready = ready or max(
        datetime.now(timezone.utc),
        datetime.fromisoformat(previous["operating_expires_at"]) + timedelta(seconds=1),
    )
    selected = previous["selected_item_keys"]
    return {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
        "approval_sha256": real_pilot._sha256(approval),
        "batch_sha256": real_pilot.binding_digest(batch),
        "ledger_sha256": real_pilot._sha256(ledger),
        "prior_final_rerun_sha256": real_pilot._sha256(previous),
        "prior_final_audit_sha256": real_pilot._sha256(audit),
        "prior_execution_ids": list(ledger["executions"]),
        "selected_item_keys": selected,
        "item_sha256": {key: sha(store.read_bytes(f"items/{batch['id']}/{key}.json")[0]) for key in selected},
        "result_sha256": result_bindings(store, batch, selected),
        "cached_documents": previous["cached_documents"],
        "additional_input_tokens": 57868, "effective_input_ceiling": 257868,
        "max_requests": 4, "max_output_tokens": 8192,
        "readiness_at": ready.isoformat(),
        "operating_expires_at": (ready + timedelta(seconds=5400)).isoformat(),
        "not_before": (ready + timedelta(seconds=1)).isoformat(),
        "expires_at": (ready + timedelta(seconds=1201)).isoformat(),
    }


def reconcile_synthetic_final(store, batch, approval, item_key, monkeypatch):
    audit = read_json(store, real_pilot.FINAL_RERUN_AUDIT_KEY)[0]
    started = datetime.fromisoformat(audit["recorded_at"])
    observed = started + timedelta(seconds=2)
    image = "synthetic.invalid/worker@sha256:" + "f" * 64
    evidence = {
        "execution_id": audit["execution_id"],
        "execution": {
            "temporary_processing_closed": True, "observed_at": observed.isoformat(),
            "execution": {
                "id": "/subscriptions/synthetic/resourceGroups/synthetic/providers/Microsoft.App/jobs/synthetic/executions/stopped",
                "name": "stopped",
                "properties": {
                    "status": "Stopped", "startTime": started.isoformat(),
                    "template": {"containers": [{
                        "image": image,
                        "args": ["-m", "backend.batch_worker", "--real-pilot", "--batch-id", batch["id"],
                                 "--concurrency", "1", "--max-batches", "1", "--item-limit", "2"],
                    }]},
                },
            },
        },
        "worker": {"active_executions": 0, "manual_worker": "Manual", "worker_image": image,
                   "observed_at": observed.isoformat()},
        "timing": {
            "observed_at": observed.isoformat(),
            "events": [{"eventTimestamp": (started + timedelta(seconds=1)).isoformat(),
                        "operationName": {"value": "Microsoft.App/jobs/stop/action"},
                        "status": {"value": "Succeeded"}}],
        },
    }

    class ReconciliationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed + timedelta(seconds=1)

    put(store, f"batches/{batch['id']}.json", {**batch, "state": "running"})
    with monkeypatch.context() as context:
        context.setenv("DOCINTEL_REAL_PILOT_ENABLED", "false")
        context.setenv("DOCINTEL_BATCH_LIVE_ENABLED", "false")
        context.setattr("backend.interrupted_reconciliation.datetime", ReconciliationClock)
        BatchService(store).reconcile_interrupted(
            batch["id"], item_key, approval["approved_by"],
            **request_for(store, batch["id"], item_key, evidence),
        )


@pytest.fixture
def row_case(final_case, monkeypatch):
    store, batch, approval, previous = final_case
    guard = real_pilot.RealPilotGuard(store, batch)
    guard.before_execution("synthetic-consumed-final-execution")
    guard.prepare_recovery(lambda: None)
    for index, bound in enumerate((24000, 24000, 24868)):
        item = batch["items"][index // 2]
        key = guard.operation_key(item, tier="inference", source_version=f"synthetic-final-{index}", prompt_version="prior")
        reservation = guard.reserve("inference", key, item_key=item["item_key"],
                                    max_input_tokens=bound, max_output_tokens=2048)
        guard.record_usage(reservation["reservation_id"], input_tokens=10, output_tokens=5)
    for index, item in enumerate(batch["items"][:2]):
        key = f"items/{batch['id']}/{item['item_key']}.json"
        state = read_json(store, key)[0]
        state.update(
            state="unresolved" if index == 0 else "running",
            error="REPRODUCTION ONLY: synthetic stopped final attempt",
            started_at=read_json(store, real_pilot.FINAL_RERUN_AUDIT_KEY)[0]["recorded_at"],
            result_key=guard.result_key(item["item_key"]),
        )
        put(store, key, state)
        if index == 0:
            machine = read_json(store, f"results/{batch['id']}/{item['item_key']}.json")[0]
            write_json(store, state["result_key"], machine)
    reconcile_synthetic_final(store, batch, approval, batch["items"][1]["item_key"], monkeypatch)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert len(ledger["executions"]) == 3 and ledger["attempted"]["inference"] == 7
    assert ledger["reserved"]["input_tokens"] == 165722 and ledger["reserved"]["output_tokens"] == 14336
    amendment = row_amendment(store, batch, approval)
    write_json(store, ROW_KEY, amendment)
    monkeypatch.setattr(real_pilot, "_now", lambda: datetime.fromisoformat(amendment["not_before"]) + timedelta(seconds=1))
    return store, batch, approval, amendment


def start_row(case):
    store, batch, _, _ = case
    guard = real_pilot.RealPilotGuard(store, batch)
    guard.before_execution("synthetic-row-execution")
    guard.prepare_recovery(lambda: None)
    return guard


def reserve(guard, batch, index, input_tokens=1, output_tokens=1):
    item = batch["items"][index % 2]
    key = guard.operation_key(item, tier="inference", source_version=f"synthetic-row-request-{index}", prompt_version="row-test")
    return guard.reserve("inference", key, item_key=item["item_key"],
                         max_input_tokens=input_tokens, max_output_tokens=output_tokens)


def assert_history_preserved(store, batch, amendment, before, *, completed):
    selected = amendment["selected_item_keys"]
    for key, value in before.items():
        if key.startswith(("results/", "parses/", "operations/", "response-diagnostics/", "configuration/")):
            assert store.read_bytes(key) == value
    old_ledger = json.loads(before[real_pilot.BUDGET_KEY][0])
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert ledger["final_rerun"] == old_ledger["final_rerun"]
    assert ledger["recovery"] == old_ledger["recovery"]
    for group in ("executions", "reservations"):
        assert all(ledger[group][key] == value for key, value in old_ledger[group].items())
    assert len(ledger["executions"]) == 4
    assert ledger["attempted"]["analysis"] == old_ledger["attempted"]["analysis"]
    assert ledger["reserved"]["analysis_pages"] == old_ledger["reserved"]["analysis_pages"]
    audit = read_json(store, ROW_AUDIT)[0]
    assert audit["amendment"] == amendment and audit["amendment_sha256"] == real_pilot._sha256(amendment)
    for key, expected in amendment["result_sha256"].items():
        if expected is None:
            with pytest.raises(Missing):
                store.read_bytes(key)
        else:
            assert sha(store.read_bytes(key)[0]) == expected
    for item in batch["items"]:
        key = item["item_key"]
        path = f"items/{batch['id']}/{key}.json"
        if key not in selected:
            assert store.read_bytes(path) == before[path]
            continue
        state = read_json(store, path)[0]
        assert state["previous_attempt"] == json.loads(before[path][0])
        assert base64.b64decode(audit["prior_states"][key]["base64"], validate=True) == before[path][0]
        expected = f"results/{batch['id']}/{key}/attempts/{real_pilot._sha256(amendment)}.json"
        assert state["result_key"] == expected
        if completed:
            detail = BatchService(store).detail(batch["id"], key, batch["owner"])
            assert detail["machine_result"] == read_json(store, expected)[0]
            assert len(detail["attempt_history"]) >= 3
            raw = store.read_bytes(expected)
            with pytest.raises(Conflict):
                write_json(store, expected, {"replacement": "forbidden"})
            assert store.read_bytes(expected) == raw
            with pytest.raises(Missing):
                BatchService(store).detail(batch["id"], key, "unrelated-owner")
    return ledger


def test_row_validation_is_read_only_and_binds_complete_result_history(row_case):
    store, batch, approval, amendment = row_case
    before = all_records(store)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert real_pilot.validate_row_rerun(amendment, approval, batch, ledger, store) == amendment
    assert before == all_records(store)
    assert len(amendment["result_sha256"]) == 4
    assert list(amendment["result_sha256"].values()).count(None) == 1
    assert approval["limits"]["input_tokens"] == 200000


@pytest.mark.parametrize("field", [
    "schema_version", "approved", "approved_by", "approval_sha256", "batch_sha256",
    "ledger_sha256", "prior_final_rerun_sha256", "prior_final_audit_sha256",
    "prior_execution_ids", "selected_item_keys", "item_sha256", "result_sha256",
    "cached_documents", "additional_input_tokens", "effective_input_ceiling",
    "max_requests", "max_output_tokens", "readiness_at", "operating_expires_at",
    "not_before", "expires_at", "unexpected",
])
def test_row_rejects_changed_authority_without_writes(row_case, field):
    store, batch, approval, amendment = row_case
    changed = copy.deepcopy(amendment)
    if field == "approved":
        changed[field] = False
    elif field in {"schema_version", "additional_input_tokens", "effective_input_ceiling", "max_requests", "max_output_tokens"}:
        changed[field] += 1
    elif isinstance(changed.get(field), (dict, list)):
        changed[field] = {} if isinstance(changed[field], dict) else []
    else:
        changed[field] = "invalid"
    before = all_records(store)
    with pytest.raises((ValueError, Conflict)):
        real_pilot.validate_row_rerun(changed, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store)
    assert all_records(store) == before


@pytest.mark.parametrize("target", ["final_audit", "pending_result", "existing_result", "current_item", "cached_document"])
def test_row_rejects_changed_prior_records(row_case, target):
    store, batch, approval, amendment = row_case
    if target == "pending_result":
        key = next(key for key, value in amendment["result_sha256"].items() if value is None)
        write_json(store, key, {"synthetic_late_completion": True})
    else:
        key = {
            "final_audit": real_pilot.FINAL_RERUN_AUDIT_KEY,
            "existing_result": next(key for key, value in amendment["result_sha256"].items() if value is not None),
            "current_item": f"items/{batch['id']}/{amendment['selected_item_keys'][0]}.json",
            "cached_document": next(iter(amendment["cached_documents"])),
        }[target]
        value = read_json(store, key)[0]
        value["synthetic_changed"] = True
        put(store, key, value)
    before = all_records(store)
    with pytest.raises((ValueError, Conflict)):
        real_pilot.validate_row_rerun(amendment, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store)
    assert all_records(store) == before


def test_row_preparation_preserves_old_final_audit_and_deferred_items(row_case):
    store, batch, _, amendment = row_case
    before = all_records(store)
    guard = start_row(row_case)
    assert guard.row_rerun == amendment
    assert_history_preserved(store, batch, amendment, before, completed=False)
    for key in amendment["selected_item_keys"]:
        assert read_json(store, f"items/{batch['id']}/{key}.json")[0]["state"] == "recovery_ready"


def test_row_fifth_request_and_second_execution_are_denied(row_case):
    store, batch, _, _ = row_case
    guard = start_row(row_case)
    for index in range(4):
        reserve(guard, batch, index)
    before = all_records(store)
    with pytest.raises(real_pilot.RealPilotBudgetExceeded) as caught:
        reserve(guard, batch, 4)
    assert caught.value.requested == 1 and caught.value.remaining == 0
    assert all_records(store) == before
    with pytest.raises(ValueError, match="execution budget exhausted"):
        guard.before_execution("synthetic-forbidden-fifth-execution")
    assert all_records(store) == before
    assert read_json(store, real_pilot.BUDGET_KEY)[0]["attempted"]["inference"] == 11


@pytest.mark.parametrize("prior_requests,requested,remaining", [(0, 8193, 8192), (3, 2049, 2048)])
def test_row_output_scope_is_separate_from_original_output_allowance(row_case, prior_requests, requested, remaining):
    store, batch, _, _ = row_case
    guard = start_row(row_case)
    for index in range(prior_requests):
        reserve(guard, batch, index, output_tokens=2048)
    before = all_records(store)
    with pytest.raises(real_pilot.RealPilotBudgetExceeded) as caught:
        reserve(guard, batch, prior_requests, output_tokens=requested)
    assert caught.value.dimension == "row_rerun_output_tokens"
    assert (caught.value.requested, caught.value.remaining) == (requested, remaining)
    assert all_records(store) == before


def test_row_input_exact_ceiling_without_rewriting_original_approval(row_case):
    store, batch, approval, _ = row_case
    approval_before = store.read_bytes(real_pilot.APPROVAL_KEY)
    guard = start_row(row_case)
    for index in range(3):
        reserve(guard, batch, index, input_tokens=24000)
    before = all_records(store)
    with pytest.raises(real_pilot.RealPilotBudgetExceeded) as caught:
        reserve(guard, batch, 3, input_tokens=20147)
    assert caught.value.dimension == "input_tokens" and caught.value.remaining == 20146
    assert all_records(store) == before
    reserve(guard, batch, 3, input_tokens=20146)
    assert read_json(store, real_pilot.BUDGET_KEY)[0]["reserved"]["input_tokens"] == 257868
    assert store.read_bytes(real_pilot.APPROVAL_KEY) == approval_before
    assert read_json(store, real_pilot.APPROVAL_KEY)[0]["limits"] == approval["limits"]


@pytest.mark.parametrize("seconds,allowed", [(599, False), (600, True)])
def test_row_needs_full_fixed_600_second_window(row_case, monkeypatch, seconds, allowed):
    store, batch, _, amendment = row_case
    monkeypatch.setattr(real_pilot, "_now", lambda: datetime.fromisoformat(amendment["expires_at"]) - timedelta(seconds=seconds))
    guard = real_pilot.RealPilotGuard(store, batch)
    before = all_records(store)
    if allowed:
        guard.before_execution("synthetic-window-boundary")
        assert len(read_json(store, real_pilot.BUDGET_KEY)[0]["executions"]) == 4
    else:
        with pytest.raises(ValueError, match="600"):
            guard.before_execution("synthetic-window-too-short")
        assert all_records(store) == before


def test_row_fatal_guard_stops_before_second_queued_item(row_case, monkeypatch):
    store, batch, _, amendment = row_case
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    before = all_records(store)
    called = []
    original = worker.RealBatchProcessor.__call__

    def source_drift(processor, item, mode):
        called.append(item["item_key"])
        key = next(iter(amendment["cached_documents"]))
        raw, version = processor.store.read_bytes(key)
        processor.store.write_bytes(key, raw + b" ", version)
        return original(processor, item, mode)

    monkeypatch.setattr(worker.RealBatchProcessor, "__call__", source_drift)
    with pytest.raises(ExecutionConfigurationError):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
    assert called == amendment["selected_item_keys"][:1]
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    old = json.loads(before[real_pilot.BUDGET_KEY][0])
    assert ledger["reservations"] == old["reservations"] and len(ledger["executions"]) == 4
    first, second = amendment["selected_item_keys"]
    assert read_json(store, f"items/{batch['id']}/{first}.json")[0]["state"] == "failed"
    pending = read_json(store, f"items/{batch['id']}/{second}.json")[0]
    assert pending["state"] == "recovery_ready" and "started_at" not in pending


@pytest.fixture
def captured_case(row_case, tmp_path):
    store, batch, approval, amendment = row_case
    root = tmp_path / "sealed-capture"
    root.mkdir(mode=0o700)
    documents = root / "documents"
    documents.mkdir(mode=0o700)
    records = {key: value for key, value in all_records(store).items() if key != ROW_KEY}
    for index, item in enumerate(batch["items"]):
        for source in item["sources"]:
            if source["kind"] == "blob":
                raw = records[source["blob"]][0]
                if not (documents / f"synthetic-{index}.pdf").exists():
                    write_private(documents, f"synthetic-{index}.pdf", raw)
    missing_blob = next(source["blob"] for source in batch["items"][0]["sources"] if source["kind"] == "blob")
    del records[missing_blob]
    bundle = {"records": {
        key: {"base64": base64.b64encode(raw).decode(), "sha256": sha(raw), "version": version}
        for key, (raw, version) in records.items()
    }}
    snapshot = {
        "verified": True, "temporary_processing_closed": True, "active_executions": 0,
        "consumption": row_helper.CONSUMED,
        "ledger_sha256": amendment["ledger_sha256"],
        "prior_final_audit_sha256": amendment["prior_final_audit_sha256"],
    }
    snapshot_receipt = write_private(root, "row-rerun-snapshot.json", snapshot)
    records_receipt = write_private(root, "row-rerun-records.json", bundle)
    descriptor = {
        "verified": True, "status_only": True, "consumption": row_helper.CONSUMED,
        "snapshot_file": "row-rerun-snapshot.json", "snapshot_sha256": snapshot_receipt["sha256"],
        "ledger_sha256": amendment["ledger_sha256"], "prior_final_audit_sha256": amendment["prior_final_audit_sha256"],
        "object_sha256": {key: sha(value[0]) for key, value in records.items()},
        "records_file": "row-rerun-records.json", "records_sha256": records_receipt["sha256"],
    }
    write_private(root, "row-rerun-reconciliation.json", descriptor)
    target = {"target": "synthetic-only"}
    write_private(root, "target.json", target)
    history = row_helper.history(root)
    revision = "b" * 40
    baseline = {
        "work_root": str(root), "baseline_revision": IMMUTABLE_BASELINE, "source_revision": revision,
        "target": row_helper.release.fingerprint(target), "approval_sha256": real_pilot._sha256(approval),
        "history": history, "history_sha256": real_pilot._sha256(history),
    }
    write_private(root, "row-rerun-baseline.json", baseline)
    return root, documents, records, descriptor, bundle, approval, amendment, revision


def test_hosted_capture_preserves_raw_bytes_versions_and_never_reconciles(captured_case, monkeypatch):
    root, documents, records, _, _, approval, _, revision = captured_case
    monkeypatch.setattr(BatchService, "reconcile_interrupted",
                        lambda *args, **kwargs: pytest.fail("Captured interrupted records must not be rewritten"))
    store, batch, loaded_approval, hosted = load_hosted_records(root, root / "row-rerun-reconciliation.json", documents)
    try:
        assert loaded_approval == approval
        assert all(store.read_bytes(key) == value for key, value in records.items())
        assert len(hosted["injected_local_document_sha256"]) == 1
        amendment = row_amendment(store, batch, approval)
        write_json(store, ROW_KEY, amendment)
        configure_offline(monkeypatch, approval, amendment)
        assert real_pilot.validate_row_rerun(
            amendment, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store,
        ) == amendment
        bound = helper_bindings(root, approval, revision, hosted)
        assert bound["reconciliation_sha256"] == sha((root / "row-rerun-reconciliation.json").read_bytes())
        assert hosted["locally_reconciled"] is hosted["hosted_verification_performed"] is False
    finally:
        store.connection.close()


@pytest.mark.parametrize("fault", [
    "raw_bytes", "record_sha", "record_version", "object_pins", "bundle_sha", "ledger_sha",
    "final_audit_sha", "snapshot_sha", "consumption", "unverified", "not_status_only",
    "missing_intent", "missing_original_result", "already_installed", "outside_root", "permissions",
])
def test_hosted_capture_rejects_unbound_or_incomplete_inputs(captured_case, fault):
    root, documents, _, descriptor, bundle, _, _, _ = captured_case
    descriptor = copy.deepcopy(descriptor)
    bundle = copy.deepcopy(bundle)
    entry = bundle["records"][real_pilot.APPROVAL_KEY]
    if fault == "raw_bytes":
        entry["base64"] = base64.b64encode(b"changed").decode()
    elif fault == "record_sha":
        entry["sha256"] = "0" * 64
    elif fault == "record_version":
        entry["version"] = ""
    elif fault == "missing_intent":
        key = next(key for key in bundle["records"] if key.startswith("operations/interrupted-reconciliation/")
                   and not key.endswith(".applied.json"))
        del bundle["records"][key]
    elif fault == "missing_original_result":
        key = next(key for key in bundle["records"] if key.startswith("results/") and "/attempts/" not in key)
        del bundle["records"][key]
    elif fault == "already_installed":
        bundle["records"][ROW_KEY] = copy.deepcopy(entry)
    raw = json.dumps(bundle).encode()
    (root / "row-rerun-records.json").write_bytes(raw)
    descriptor["records_sha256"] = sha(raw)
    descriptor["object_sha256"] = {key: value["sha256"] for key, value in bundle["records"].items()}
    if fault in {"object_pins", "bundle_sha", "ledger_sha", "final_audit_sha", "snapshot_sha"}:
        field = {"object_pins": "object_sha256", "bundle_sha": "records_sha256",
                 "ledger_sha": "ledger_sha256", "final_audit_sha": "prior_final_audit_sha256",
                 "snapshot_sha": "snapshot_sha256"}[fault]
        descriptor[field] = {} if fault == "object_pins" else "0" * 64
    elif fault == "consumption":
        descriptor["consumption"]["input_tokens"] -= 1
    elif fault in {"unverified", "not_status_only"}:
        descriptor["verified" if fault == "unverified" else "status_only"] = False
    elif fault == "outside_root":
        descriptor["records_file"] = "../row-rerun-records.json"
    (root / "row-rerun-reconciliation.json").write_text(json.dumps(descriptor))
    if fault == "permissions":
        (root / "row-rerun-reconciliation.json").chmod(0o644)
    with pytest.raises((AssertionError, ValueError, Missing)):
        load_hosted_records(root, "row-rerun-reconciliation.json", documents)


def test_helper_binding_rejects_changed_target_history_or_source(captured_case):
    root, documents, _, _, _, approval, _, revision = captured_case
    store, _, _, hosted = load_hosted_records(root, "row-rerun-reconciliation.json", documents)
    try:
        helper_bindings(root, approval, revision, hosted)
        with pytest.raises(AssertionError):
            helper_bindings(root, approval, "c" * 40, hosted)
        (root / "target.json").write_text('{"target":"changed"}')
        with pytest.raises(AssertionError):
            helper_bindings(root, approval, revision, hosted)
    finally:
        store.connection.close()


def test_helper_gate_exact_scope_cost_and_local_only_labels(captured_case):
    root, documents, _, _, _, approval, amendment, revision = captured_case
    store, _, _, hosted = load_hosted_records(root, "row-rerun-reconciliation.json", documents)
    try:
        bindings = helper_bindings(root, approval, revision, hosted)
        priced = copy.deepcopy(approval)
        priced["unit_prices_usd"].update(input_token=row_helper.RATES["input"], output_token=row_helper.RATES["output"])
        amendment = {**amendment, "approval_sha256": real_pilot._sha256(priced)}
        bindings = {**bindings, "approval_sha256": real_pilot._sha256(priced)}
        cases = [{"mode": mode, "request_totals": {"input_tokens": tokens, "output_tokens": 8192},
                  "amendment": amendment}
                 for mode, tokens in (("supported_pdf", 89596), ("full_fallthrough", 92146))]
        checks = dict.fromkeys(row_helper.GATE_CHECKS, True)
        gate = helper_gate(bindings, hosted, priced, cases, {"synthetic_only": True}, checks)
        assert gate["scope_sha256"] == real_pilot._sha256(gate["amendment_scope"])
        assert not row_helper.TIME_FIELDS & set(gate["amendment_scope"])
        assert gate["remote_object_sha256"] == hosted["object_sha256"]
        assert gate["maximum_direct_microdollars"] == 726132
        assert gate["hosted_verification_performed"] is gate["historical_forecasts_stacked"] is False
        assert "not hosted verification" in gate["owner_isolation_scope"]
        checks["owner_isolation"] = False
        with pytest.raises(AssertionError):
            helper_gate(bindings, hosted, priced, cases, {}, checks)
    finally:
        store.connection.close()


def test_reproduction_export_notice_preserves_all_original_worksheets():
    original = write_workbook({"Attempts": [["State", "Mode"], ["completed", "real_pilot"]],
                               "Evidence": [["ID", "Locator"], ["synthetic", "fixture.pdf#page=1"]]})
    labeled = reproduction_workbook(original)
    with ZipFile(BytesIO(original)) as before, ZipFile(BytesIO(labeled)) as after:
        assert all(before.read(name) == after.read(name) for name in before.namelist() if name.startswith("xl/worksheets/"))
    assert list(read_workbook(labeled))[0] == "REPRODUCTION"
    assert "NOT ACTUAL MODEL OUTPUT" in read_workbook(labeled)["REPRODUCTION"][0]["Notice"]


def immutable_extraction_hashes():
    workspace = Path(__file__).resolve().parents[1]
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", IMMUTABLE_BASELINE, "--", "backend", "frontend"], cwd=workspace, text=True,
    ).splitlines()
    assert set(changed) <= {"backend/real_pilot.py"}, "Successor must not change extraction, prompts, evidence or frontend"
    assert not subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "backend", "frontend"], cwd=workspace, text=True,
    ).strip()
    hashes = {}
    for name in IMMUTABLE_FILES:
        baseline = subprocess.check_output(["git", "show", f"{IMMUTABLE_BASELINE}:{name}"], cwd=workspace)
        assert (workspace / name).read_bytes() == baseline
        hashes[name] = sha(baseline)
    return hashes


def source_attestation(*, committed):
    workspace = Path(__file__).resolve().parents[1]
    paths = ["backend/real_pilot.py", "tests/test_row_rerun.py", "scripts/row_continuation.py"]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    clean = not subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=workspace, text=True,
    ).strip()
    if committed:
        subprocess.check_output(["git", "ls-files", "--error-unmatch", "--", *paths], cwd=workspace)
        assert clean, "Committed acceptance requires every tracked source file clean"
    return {
        "revision": revision, "tracked_source_clean": clean, "committed_required": committed,
        "immutable_baseline": IMMUTABLE_BASELINE, "immutable_extraction_sha256": immutable_extraction_hashes(),
        "source_sha256": {name: sha((workspace / name).read_bytes()) for name in paths},
    }


def test_exact_private_row_rerun_go_gate(private_inputs, monkeypatch):
    inputs = private_inputs
    descriptor = os.environ.get("DOCINTEL_TEST_ROW_RERUN_HOSTED_RECONCILIATION")
    source = source_attestation(
        committed=bool(descriptor) or os.environ.get("DOCINTEL_TEST_ROW_RERUN_REQUIRE_COMMITTED") == "true",
    )
    hosted, bindings = None, None
    if descriptor:
        original, batch, approval, hosted = load_hosted_records(inputs["root"], descriptor, inputs["documents"])
        reconciliation = hosted
        bindings = helper_bindings(inputs["root"], approval, source["revision"], hosted)
        assert read_json(original, real_pilot.FINAL_RERUN_AUDIT_KEY)[0] == inputs["after"]["final_audit"]
        for key, entry in inputs["before"]["records"].items():
            if key.startswith(("configuration/", "operations/", "results/", "parses/", "response-diagnostics/")):
                assert original.read_bytes(key)[0] == base64.b64decode(entry["base64"], validate=True)
        for item_key, detail in inputs["after"]["details"].items():
            for attempt in [detail, *detail.get("attempt_history", [])]:
                if attempt.get("machine_result") is not None:
                    key = attempt.get("result_key") or f"results/{batch['id']}/{item_key}.json"
                    assert read_json(original, key)[0] == attempt["machine_result"]
    else:
        original, batch, approval = hydrate(inputs)
        reconciled, reconciliation = reconcile_private(inputs, monkeypatch)
        for key, (raw, _) in reconciled.items():
            if key in reconciliation["changed_existing_keys"] or key.startswith("operations/interrupted-reconciliation/"):
                put(original, key, json.loads(raw))
    batch = read_json(original, f"batches/{batch['id']}.json")[0]
    baseline = original.snapshot()
    consumed = copy.deepcopy(inputs["after"]["ledger"])
    assert read_json(original, real_pilot.BUDGET_KEY)[0] == consumed
    assert (len(consumed["executions"]), consumed["attempted"]["inference"],
            consumed["reserved"]["input_tokens"], consumed["reserved"]["output_tokens"]) == (3, 7, 165722, 14336)
    ready = datetime.now(timezone.utc)
    cases, artifacts, exports = [], [], []
    compactor = worker.compact_inference_prompt
    for mode in ("supported_pdf", "full_fallthrough"):
        store = WorkerMemoryStore(baseline)
        amendment = row_amendment(store, batch, approval, ready=ready)
        write_json(store, ROW_KEY, amendment)
        configure_offline(monkeypatch, approval, amendment)
        before_validation = store.snapshot()
        assert real_pilot.validate_row_rerun(amendment, approval, batch, consumed, store) == amendment
        assert store.snapshot() == before_validation
        queued = {**batch, "state": "queued"}
        put(store, f"batches/{batch['id']}.json", queued)
        before = store.snapshot()
        requests, calls = [], []

        def capture(user):
            payload = json.loads(user)
            item = next(item for item in batch["items"] if item["manifest"]["product"] == payload["product"])
            record = request_record(item["item_key"], extract.product_extraction_system_message,
                                    user, ExtractionResponse, inputs, compactor)
            requests.append(record)
            return json.dumps(record["user"], ensure_ascii=False, separators=(",", ":")), record["references"]

        class ReproductionClient:
            def __init__(self, **kwargs):
                self.sync_client, self.last_usage, self.last_response_sha256 = self, None, None

            def with_options(self, **kwargs):
                assert kwargs == {"max_retries": 0}
                return self

            def close(self):
                pass

            def complete_structured(self, system, user, schema, **kwargs):
                request = requests[-1]
                assert system == request["system"] and json.loads(user) == request["user"]
                assert kwargs["max_completion_tokens"] == 2048
                if mode == "full_fallthrough" and request["source_tier"] == "internal_pdf":
                    response = ExtractionResponse(candidates=[])
                else:
                    response = response_for(request["original_payload"], request["references"], inputs["fixture"])
                self.last_response_sha256 = sha(response.model_dump_json().encode())
                calls.append({"item_key": request["item_key"], "source_tier": request["source_tier"],
                              "response": response.model_dump(mode="json")})
                return response

        monkeypatch.setattr(worker, "compact_inference_prompt", capture)
        monkeypatch.setattr(worker, "LLMClient", ReproductionClient)
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
        assert len(calls) == len(requests) == 4
        assert [request["source_tier"] for request in requests] == ["internal_pdf", "vendor_table"] * 2
        assert [request["item_key"] for request in requests] == [
            key for key in amendment["selected_item_keys"] for _ in range(2)
        ]
        ledger = assert_history_preserved(store, batch, amendment, before, completed=True)
        requested = {name: sum(request[name] for request in requests) for name in ("input_tokens", "output_tokens")}
        assert requested["output_tokens"] == 8192
        assert requested["input_tokens"] <= amendment["effective_input_ceiling"] - consumed["reserved"]["input_tokens"] == 92146
        if mode == "full_fallthrough":
            assert requested["input_tokens"] == 92146
        assert ledger["attempted"]["inference"] == 11
        assert ledger["reserved"]["input_tokens"] == consumed["reserved"]["input_tokens"] + requested["input_tokens"]
        assert ledger["reserved"]["output_tokens"] == consumed["reserved"]["output_tokens"] + requested["output_tokens"]
        reservations = [value for key, value in ledger["reservations"].items() if key not in consumed["reservations"]]
        assert len(reservations) == 4
        for reservation, request in zip(reservations, requests, strict=True):
            assert reservation["reserved_usage"]["input_tokens"] == request["input_tokens"]
            assert reservation["reserved_usage"]["output_tokens"] == request["output_tokens"]
            assert request["input_tokens"] + request["output_tokens"] <= 30000
        summaries = []
        for item in batch["items"]:
            if item["item_key"] not in amendment["selected_item_keys"]:
                continue
            detail = BatchService(store).detail(batch["id"], item["item_key"], batch["owner"])
            assert any(entry.get("parsing") == "cache" for entry in detail["provenance"])
            assert not any(entry.get("parsing") == "fresh_analysis" for entry in detail["provenance"])
            assert len([entry for entry in detail["inference_provenance"] if entry.get("method") == "model"]) == 2
            from backend.models.enrichment import EnrichmentResult

            summaries.append({"item_key": item["item_key"],
                              **item_summary(EnrichmentResult.model_validate(detail["machine_result"]))})
        exported = verify_exports(store, batch, inputs)
        assert len(read_workbook(exported)["Attempts"]) >= 6
        after = store.snapshot()
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
        assert store.snapshot() == after
        cases.append({
            "mode": mode, "label": "REPRODUCTION ONLY; independent alternative, not cumulative executions",
            "decision": "GO_LOCAL_PRODUCTION_PATH", "live_calls": 0,
            "requests": requests, "representative_responses": calls, "request_totals": requested,
            "new_reservations": reservations, "resulting_ledger": ledger,
            "amendment": amendment, "amendment_sha256": real_pilot._sha256(amendment),
            "row_audit": read_json(store, ROW_AUDIT)[0], "products": summaries,
            "unchanged_original_approval": True, "old_final_audit_unchanged": True,
            "deferred_products_unchanged": True, "history_and_machine_results_immutable": True,
            "cached_pdf_only": True, "all_original_evidence_export_locators_preserved": True,
        })
        exports.append((mode, reproduction_workbook(exported)))
        store.connection.close()
    assert source_attestation(committed=source["committed_required"]) == source
    checks = {
        "production_run_batch": True, "real_guard_reservations": True, "full_four_prompt_match": True,
        "expected_token_reservations": True, "zero_new_analysis": True, "append_only_history": True,
        "immutable_results": True, "attempt_history_export": True, "owner_isolation": True,
        "status_only_reconciliation": True, "preserved_consumption": True, "unchanged_extraction": True,
    }
    assert set(checks) == row_helper.GATE_CHECKS
    gate = None
    if hosted:
        assert helper_bindings(inputs["root"], approval, source["revision"], hosted) == bindings
        assert all(sha(private_json(inputs["root"], name)[1]) == digest
                   for name, digest in hosted["input_files"].items())
        gate = helper_gate(bindings, hosted, approval, cases, source, checks)
    prefix = os.environ.get("DOCINTEL_TEST_ROW_RERUN_OUTPUT")
    if prefix:
        assert re.fullmatch(r"[A-Za-z0-9_-]+", prefix)
        if hosted:
            assert prefix.startswith(row_helper.PREFIX + "-")
        for mode, exported in exports:
            artifacts.append(write_private(inputs["root"], f"{prefix}-{mode}.xlsx", exported))
        if gate:
            artifacts.append(write_private(inputs["root"], prefix + "-gate.json", gate))
        receipt = write_private(inputs["root"], prefix + "-report.json", {
            "decision": "GO_LOCAL_PRODUCTION_PATH", "label": "REPRODUCTION ONLY", "live_calls": 0,
            "product_findings": False, "recovered_output": False, "hosted_ready_pins": False,
            "execution_start_bypassed": False, "reserve_mocked": False, "allowances_reset": False,
            "immutable_extraction_baseline": IMMUTABLE_BASELINE,
            "immutable_extraction_sha256": source["immutable_extraction_sha256"],
            "source_revision": source["revision"], "source_attestation": source,
            "guard_sha256": source["source_sha256"]["backend/real_pilot.py"],
            "gate_sha256": source["source_sha256"]["tests/test_row_rerun.py"],
            "private_input_sha256": inputs["source_hashes"],
            "original_consumed_ledger_sha256": real_pilot._sha256(consumed),
            "local_status_only_reconciliation": reconciliation,
            "original_input_ceiling": 200000, "approved_additional_input": 57868,
            "effective_input_ceiling": 257868, "available_input": 92146,
            "new_request_cap": 4, "new_output_cap": 8192,
            "cases_are_independent_offline_alternatives": True, "cases": cases, "artifacts": artifacts,
            "checks": checks, "helper_gate_emitted": gate is not None,
            "hosted_raw_pins_bound": hosted is not None, "hosted_verification_performed": False,
            "owner_isolation_scope": "Local BatchService detail/export assertions only; not hosted verification.",
            "limitation": "Preliminary reconstructed pins cannot authorize hosted activation. "
                          "Optional captured pins are replayed locally without re-reconciliation or hosted access. "
                          "Production run_batch, guard, cache, reservation and export were exercised "
                          "with labeled representative responses, no model or hosted calls.",
        })
        print(json.dumps({"decision": "GO_LOCAL_PRODUCTION_PATH", "receipt": receipt}))
    original.connection.close()
