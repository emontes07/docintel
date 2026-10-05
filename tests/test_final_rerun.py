"""Append-only final-attempt regressions; private fixtures are strictly opt-in.

Private acceptance uses DOCINTEL_TEST_FINAL_SNAPSHOT, DOCINTEL_TEST_FINAL_PROMPTS,
and DOCINTEL_TEST_FINAL_DOCUMENTS (approved local PDF/XLSX directory). Optional
DOCINTEL_TEST_FINAL_RECEIPT is an exclusively created, private JSON receipt.
DOCINTEL_TEST_FINAL_BASELINE binds that receipt to an existing release baseline
and requires the tested backend and this gate to match its committed revision.
All model responses are REPRODUCTION ONLY, never product findings or live output.
This historical pre-final snapshot does not establish current postclosure capacity
or authorize another execution. Receipts stay beside that private snapshot.
"""

import base64
import copy
import hashlib
import json
import os
import re
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend import batch_worker as worker, real_pilot
from backend.batch import BatchService
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.core.config import settings
from backend.core.instructions import product_extraction_system_message
from backend.core.llm import LLM_API_VERSION
from backend.extract import ExecutionConfigurationError
from backend.models.enrichment import EnrichmentResult
from backend.real_pilot import RealPilotGuard
from backend.workbooks import read_workbook
from tests.test_real_batch_worker import configured, recovery_case  # noqa: F401
from tests.test_real_pilot import MemoryStore


FINAL_KEY = "configuration/real-pilot-final-rerun.json"
FINAL_AUDIT_KEY = "operations/real-pilot-final-rerun.json"
REDACTION_SENTINEL = "REPRODUCTION-SECRET-MUST-NOT-PERSIST"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ReproductionStore(MemoryStore):
    def read_bytes(self, key, max_bytes=None):
        value = super().read_bytes(key)
        if max_bytes is not None and len(value[0]) > max_bytes:
            raise ValueError("Read limit exceeded")
        return value

    def keys(self, prefix):
        return sorted(key for key in self.records if key.startswith(prefix))


def replace(store, key, value):
    _, version = read_json(store, key)
    write_json(store, key, value, version)


def all_records(store):
    return {key: store.read_bytes(key) for key in store.keys("") if not key.startswith("locks/")}


@pytest.fixture(autouse=True)
def network_blocked(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Final-rerun tests must not contact any service")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(worker, "DocumentIntelligenceService", forbidden)
    monkeypatch.setattr(worker, "ManagedIdentityCredential", lambda **kwargs: Mock())
    monkeypatch.setattr(worker, "get_bearer_token_provider", lambda *args: None)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient", forbidden)
    monkeypatch.setattr("backend.core.websearch.fetch_original_page", forbidden)
    monkeypatch.setattr("backend.core.pilot_sources.retrieve_document", forbidden)
    clock = [0.0]
    monkeypatch.setattr(worker, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))


def final_amendment(store, batch, approval, *, confirmed=None):
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    recovery = read_json(store, real_pilot.RECOVERY_KEY)[0]
    confirmed = confirmed or datetime.now(timezone.utc)
    selected = recovery["selected_item_keys"]
    return {
        "schema_version": 1, "approved": True, "approved_by": approval["approved_by"],
        "approval_sha256": real_pilot._sha256(approval),
        "batch_sha256": real_pilot.binding_digest(batch),
        "ledger_sha256": real_pilot._sha256(ledger),
        "prior_recovery_sha256": real_pilot._sha256(recovery),
        "prior_execution_ids": list(ledger["executions"]),
        "selected_item_keys": selected,
        "item_sha256": {
            key: sha(store.read_bytes(f"items/{batch['id']}/{key}.json")[0])
            for key in selected
        },
        "result_sha256": {
            key: sha(store.read_bytes(f"results/{batch['id']}/{key}.json")[0])
            for key in selected
        },
        "cached_documents": recovery["cached_documents"],
        "readiness_at": confirmed.isoformat(),
        "operating_expires_at": (confirmed + timedelta(minutes=90)).isoformat(),
        "not_before": (confirmed + timedelta(seconds=1)).isoformat(),
        "expires_at": (confirmed + timedelta(minutes=20)).isoformat(),
    }


@pytest.fixture
def final_case(recovery_case, monkeypatch):
    store, batch, approval, recovery, _ = recovery_case
    # Synthetic original approval has ample total allowance, never a raised limit.
    approval["limits"].update(inference=16, input_tokens=200000, output_tokens=32768)
    replace(store, real_pilot.APPROVAL_KEY, approval)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    ledger["approval_sha256"] = real_pilot._sha256(approval)
    replace(store, real_pilot.BUDGET_KEY, ledger)
    recovery["approval_sha256"] = real_pilot._sha256(approval)
    recovery["ledger_sha256"] = real_pilot._sha256(ledger)
    replace(store, real_pilot.RECOVERY_KEY, recovery)
    guard = RealPilotGuard(store, batch)
    guard.before_execution("synthetic-consumed-recovery")
    guard.prepare_recovery(lambda: None)
    for index in range(4):
        item = batch["items"][index // 2]
        key = guard.operation_key(item, tier="inference", source_version=str(index), prompt_version="old")
        reserved = guard.reserve(
            "inference", key, item_key=item["item_key"],
            max_input_tokens=23214 if index < 2 else 23213, max_output_tokens=2048,
        )
        guard.record_usage(reserved["reservation_id"], input_tokens=10, output_tokens=5)
    for item in batch["items"][:2]:
        result = EnrichmentResult(
            execution_mode="live_inference", candidate_source="llm",
            model_call_status="failed", manifest=item["manifest"],
            observed_at=datetime.now(timezone.utc), retrieval=[], evidence=[],
            attributes=[], extraction_error="invalid_response",
        )
        write_json(store, f"results/{batch['id']}/{item['item_key']}.json", result.model_dump(mode="json"))
        path = f"items/{batch['id']}/{item['item_key']}.json"
        state = read_json(store, path)[0]
        state.update(state="unresolved", error="REPRODUCTION ONLY: prior invalid response")
        replace(store, path, state)
    batch["state"] = "queued"
    replace(store, f"batches/{batch['id']}.json", batch)
    confirmed = datetime.fromisoformat(recovery["expires_at"]) + timedelta(minutes=1)
    amendment = final_amendment(store, batch, approval, confirmed=confirmed)
    write_json(store, FINAL_KEY, amendment)
    monkeypatch.setattr(real_pilot, "_now", lambda: confirmed + timedelta(seconds=2))
    return store, batch, approval, amendment


def test_final_validation_is_read_only_and_normal_recovery_cannot_overrun(final_case):
    store, batch, approval, amendment = final_case
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    before = all_records(store)
    assert real_pilot.validate_final_rerun(amendment, approval, batch, ledger, store) == amendment
    assert before == all_records(store)
    plain = ReproductionStore({key: value for key, value in before.items() if key != FINAL_KEY})
    with pytest.raises((ValueError, Conflict)):
        RealPilotGuard(plain, batch).before_execution("unauthorized-third-worker")
    assert len(read_json(plain, real_pilot.BUDGET_KEY)[0]["executions"]) == 2


@pytest.mark.parametrize("field", [
    "approved", "approved_by", "approval_sha256", "batch_sha256", "ledger_sha256",
    "prior_recovery_sha256", "prior_execution_ids", "selected_item_keys",
    "item_sha256", "result_sha256", "cached_documents", "expires_at",
    "operating_expires_at", "unknown",
])
def test_final_validation_rejects_changed_authority_without_writes(final_case, field):
    store, batch, approval, amendment = final_case
    candidate = copy.deepcopy(amendment)
    if field == "approved":
        candidate[field] = False
    elif field == "approved_by":
        candidate[field] = "99999999-9999-4999-8999-999999999999"
    elif field in {"prior_execution_ids", "selected_item_keys"}:
        candidate[field] = list(reversed(candidate[field])) if field == "prior_execution_ids" else ["row-4", "row-5"]
    elif field in {"item_sha256", "result_sha256", "cached_documents"}:
        candidate[field][next(iter(candidate[field]))] = "f" * 64
    elif field == "expires_at":
        candidate[field] = (datetime.fromisoformat(candidate["not_before"]) + timedelta(seconds=1201)).isoformat()
    elif field == "operating_expires_at":
        candidate[field] = candidate["readiness_at"]
    else:
        candidate[field] = "f" * 64
    before = all_records(store)
    with pytest.raises((ValueError, Conflict)):
        real_pilot.validate_final_rerun(
            candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store,
        )
    assert before == all_records(store)


def test_final_guard_one_extra_execution_four_requests_no_analysis_or_deferred_products(final_case):
    store, batch, approval, amendment = final_case
    before = read_json(store, real_pilot.BUDGET_KEY)[0]
    guard = RealPilotGuard(store, batch)
    guard.before_execution("synthetic-final-worker")
    key = guard.operation_key(batch["items"][0], tier="analysis", source_version="x", prompt_version="new")
    unchanged = store.read_bytes(real_pilot.BUDGET_KEY)
    for operation in ("analysis", "search", "web_retrieval", "retrieval", "unknown"):
        with pytest.raises(ValueError):
            guard.reserve(operation, key, item_key="row-2", analysis_pages=1 if operation == "analysis" else 0)
        assert store.read_bytes(real_pilot.BUDGET_KEY) == unchanged
    with pytest.raises(ValueError):
        guard.operation_key(batch["items"][2], tier="inference", source_version="x", prompt_version="new")
    for index in range(4):
        key = guard.operation_key(batch["items"][index // 2], tier="inference",
                                  source_version=f"final-{index}", prompt_version="new")
        guard.reserve("inference", key, item_key=f"row-{2 + index // 2}",
                      max_input_tokens=100, max_output_tokens=2048)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert len(ledger["executions"]) == 3 and ledger["attempted"]["inference"] == 8
    assert ledger["final_rerun"]["sha256"] == real_pilot._sha256(amendment)
    assert all(ledger["reservations"][key] == value for key, value in before["reservations"].items())
    assert all(ledger["executions"][key] == value for key, value in before["executions"].items())
    assert read_json(store, real_pilot.APPROVAL_KEY)[0] == approval
    before_denial = store.read_bytes(real_pilot.BUDGET_KEY)
    key = guard.operation_key(batch["items"][0], tier="inference", source_version="fifth", prompt_version="new")
    with pytest.raises(real_pilot.RealPilotBudgetExceeded):
        guard.reserve("inference", key, item_key="row-2", max_input_tokens=100, max_output_tokens=2048)
    assert store.read_bytes(real_pilot.BUDGET_KEY) == before_denial
    with pytest.raises((ValueError, Conflict)):
        RealPilotGuard(store, batch).before_execution("fourth-worker-forbidden")
    assert len(read_json(store, real_pilot.BUDGET_KEY)[0]["executions"]) == 3


@pytest.mark.parametrize("input_bound,output_bound,successful,dimension", [
    (28000, 2048, 0, "recovery_request_tokens"),
    (27000, 2048, 3, "input_tokens"),
    (100, 13000, 1, "output_tokens"),
])
def test_final_capacity_denials_preserve_original_totals(
    final_case, input_bound, output_bound, successful, dimension,
):
    store, batch, approval, _ = final_case
    guard = RealPilotGuard(store, batch)
    guard.before_execution("capacity-reproduction")
    for index in range(successful):
        key = guard.operation_key(batch["items"][0], tier="inference",
                                  source_version=f"capacity-{index}", prompt_version="new")
        guard.reserve("inference", key, item_key="row-2",
                      max_input_tokens=input_bound, max_output_tokens=output_bound)
    before = store.read_bytes(real_pilot.BUDGET_KEY)
    key = guard.operation_key(batch["items"][0], tier="inference",
                              source_version="capacity-denied", prompt_version="new")
    with pytest.raises(real_pilot.RealPilotBudgetExceeded) as error:
        guard.reserve("inference", key, item_key="row-2",
                      max_input_tokens=input_bound, max_output_tokens=output_bound)
    assert error.value.dimension == dimension
    assert store.read_bytes(real_pilot.BUDGET_KEY) == before
    assert read_json(store, real_pilot.APPROVAL_KEY)[0] == approval


@pytest.mark.parametrize("target", ["status", "result", "cache", "ledger", "audit"])
def test_final_preflight_rejects_changed_persisted_bindings_read_only(final_case, target):
    store, batch, approval, amendment = final_case
    keys = {
        "status": f"items/{batch['id']}/row-2.json",
        "result": f"results/{batch['id']}/row-2.json",
        "cache": next(iter(amendment["cached_documents"])),
        "ledger": real_pilot.BUDGET_KEY,
        "audit": real_pilot.RECOVERY_AUDIT_KEY,
    }
    value = read_json(store, keys[target])[0]
    value["reproduction_changed"] = True
    if target == "audit":
        value["recovery_sha256"] = "f" * 64
    replace(store, keys[target], value)
    before = all_records(store)
    with pytest.raises((ValueError, Conflict)):
        real_pilot.validate_final_rerun(
            amendment, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store,
        )
    assert all_records(store) == before


def install_reproduction_client(monkeypatch, captures=None, *, invalid=False):
    calls = []

    def stable(value):
        if isinstance(value, dict):
            return {key: stable(entry) for key, entry in value.items()
                    if key not in {"observed_at", "provider_retrieved_at"}}
        if isinstance(value, list):
            return [stable(entry) for entry in value]
        return value

    class Client:
        def __init__(self, **kwargs):
            self.sync_client = self
            self.last_usage = None
            self.last_response_sha256 = None

        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self

        def close(self):
            pass

        def complete_structured(self, system, user, schema, **kwargs):
            payload = json.loads(user)
            schema_bytes = len(json.dumps(schema.model_json_schema()).encode())
            assert system == product_extraction_system_message + worker.COMPACT_PROMPT_INSTRUCTIONS
            if captures is not None:
                capture = captures[len(calls)]
                expected, _ = worker.compact_inference_prompt(json.dumps(capture["user"]))
                unchanged = stable(payload) == stable(json.loads(expected))
                assert unchanged, "Full real prompt changed beyond regenerated timestamps"
                assert len(user.encode()) == len(expected.encode())
                assert schema.model_json_schema() == capture["schema"]
            calls.append({"system_sha256": sha(system.encode()), "user_sha256": sha(user.encode()),
                          "user_bytes": len(user.encode()), "system_bytes": len(system.encode()),
                          "schema_bytes": schema_bytes, "framing_reserve": 4096,
                          "input_tokens": len(system.encode()) + len(user.encode()) + schema_bytes + 4096,
                          "output_tokens": kwargs["max_completion_tokens"],
                          "captured_system_sha256": sha(capture["system"].encode()) if captures is not None else None,
                          "source_tier": payload["evidence_groups"][0]["source_tier"]})
            candidates = []
            # Keep all PDF-tier attributes unresolved so the next exact captured
            # vendor prompt is preserved, rather than substituting a smaller one.
            if invalid or payload["evidence_groups"][0]["source_tier"] == "vendor_table":
                attribute = next(entry for entry in payload["attributes"]
                                 if entry["value_type"] == "string" and not entry["unit"]
                                 and not entry["allowed_values"]
                                 and not re.search(r"material|max", entry["attribute_id"], re.I))
                def source_texts(row):
                    text = payload["evidence_texts"][row[2]]
                    if payload["evidence_groups"][row[1]]["source_tier"] == "vendor_table" and text.lstrip().startswith("{"):
                        return [cell["value"] for cell in json.loads(text)["cells"]]
                    return [text]

                evidence, text = next(
                    (row, text) for row in payload["evidence"]
                    if (payload["evidence_groups"][row[1]]["attribute_ids"] is None
                        or attribute["attribute_id"] in payload["evidence_groups"][row[1]]["attribute_ids"])
                    for text in source_texts(row) if re.search(r"\b[A-Za-z]{4,}\b", text)
                )
                candidates.append({
                    "attribute_id": attribute["attribute_id"],
                    "value": re.search(r"\b[A-Za-z]{4,}\b", text).group(),
                    "evidence_ids": [REDACTION_SENTINEL] if invalid else [" " + evidence[0].lower() + " "],
                    "supporting_quote": text,
                    "qualification": "REPRODUCTION ONLY: fabricated response, not a product finding.",
                })
            raw = json.dumps({"candidates": candidates})
            self.last_usage = {"input_tokens": 12, "output_tokens": 8}
            self.last_response_sha256 = sha(raw.encode())
            return schema.model_validate_json(raw)

    monkeypatch.setattr(worker, "LLMClient", Client)
    return calls


def test_reproduction_vendor_quote_uses_cell_content_and_full_request_bound(monkeypatch):
    from backend.models.enrichment import ExtractionResponse

    calls = install_reproduction_client(monkeypatch)
    text = json.dumps({
        "sheet": "Synthetic", "row": 2,
        "cells": [{"cell": "A2", "column": "Connection", "value": "threaded"}],
    })
    payload = {
        "attributes": [{"attribute_id": "Connection", "value_type": "string", "unit": None, "allowed_values": []}],
        "evidence": [["E1", 0, 0, "row=2"]],
        "evidence_groups": [{"source_tier": "vendor_table", "attribute_ids": ["Connection"]}],
        "evidence_texts": [text],
    }
    system = product_extraction_system_message + worker.COMPACT_PROMPT_INSTRUCTIONS
    user = json.dumps(payload)
    response = worker.LLMClient().complete_structured(system, user, ExtractionResponse, max_completion_tokens=2048)
    assert response.candidates[0].supporting_quote == response.candidates[0].value == "threaded"
    assert calls[0]["input_tokens"] == (
        len(system.encode()) + len(user.encode()) + len(json.dumps(ExtractionResponse.model_json_schema()).encode()) + 4096
    )
    assert calls[0]["output_tokens"] == 2048


def assert_append_only(store, batch, amendment, before, *, invalid=False):
    selected = amendment["selected_item_keys"]
    for key, original in before.items():
        if key.startswith(("results/", "operations/", "response-diagnostics/", "parses/")) or key in {
            real_pilot.APPROVAL_KEY, real_pilot.RECOVERY_KEY,
        }:
            assert store.read_bytes(key) == original
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    old_ledger = json.loads(before[real_pilot.BUDGET_KEY][0])
    for group in ("executions", "reservations"):
        assert all(ledger[group][key] == value for key, value in old_ledger[group].items())
    assert len(ledger["executions"]) == 3
    assert ledger["attempted"]["analysis"] == old_ledger["attempted"]["analysis"]
    assert ledger["reserved"]["analysis_pages"] == old_ledger["reserved"]["analysis_pages"]
    audit = read_json(store, FINAL_AUDIT_KEY)[0]
    assert audit
    service = BatchService(store)
    for item in batch["items"]:
        item_key = item["item_key"]
        path = f"items/{batch['id']}/{item_key}.json"
        if item_key not in selected:
            assert store.read_bytes(path) == before[path]
            assert not store.keys(f"results/{batch['id']}/{item_key}")
            continue
        detail = service.detail(batch["id"], item_key, batch["owner"])
        result_path = f"results/{batch['id']}/{item_key}/attempts/{real_pilot._sha256(amendment)}.json"
        assert detail["result_key"] == result_path
        assert detail["machine_result"] == read_json(store, result_path)[0]
        assert detail["attempt_history"]
        assert detail["previous_attempt"] == json.loads(before[path][0])
        assert base64.b64decode(audit["prior_states"][item_key]["base64"]) == before[path][0]
        prior_result_path = f"results/{batch['id']}/{item_key}.json"
        previous = next(entry for entry in detail["attempt_history"]
                        if entry["machine_sha256"] == sha(before[prior_result_path][0]))
        assert previous["machine_result"] == json.loads(before[prior_result_path][0])
        history = json.dumps(detail["attempt_history"])
        assert "previous_attempt" in history or "interrupted" in history
        assert "REPRODUCTION-SECRET" not in json.dumps(detail)
        if invalid:
            assert detail["validation_diagnostics"]
            assert detail["machine_result"]["validation_diagnostics"]
            assert not any(entry["candidates"] for entry in detail["machine_result"]["attributes"])
            assert all(value["raw_response_sha256"] for value in detail["validation_diagnostics"])
        with pytest.raises(Missing):
            service.detail(batch["id"], item_key, "unrelated-owner")
        raw = store.read_bytes(result_path)
        with pytest.raises(Conflict):
            write_json(store, result_path, {"replacement": "forbidden"})
        assert store.read_bytes(result_path) == raw
    exported = read_workbook(service.export(batch["id"], batch["owner"]))
    assert "Attempts" in exported and len(exported["Attempts"]) >= 4
    if invalid:
        assert exported["Diagnostics"]
        assert REDACTION_SENTINEL not in json.dumps(exported)
    with pytest.raises(Missing):
        service.export(batch["id"], "unrelated-owner")
    return ledger


@pytest.mark.parametrize("invalid", [False, True])
def test_final_worker_preserves_history_and_persists_safe_failure_diagnostics(final_case, monkeypatch, invalid):
    store, batch, _, amendment = final_case
    before = all_records(store)
    calls = install_reproduction_client(monkeypatch, invalid=invalid)
    worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
    assert len(calls) == 2
    assert_append_only(store, batch, amendment, before, invalid=invalid)
    after = all_records(store)
    try:
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
    except (ValueError, Conflict):
        pass
    assert after == all_records(store)


def test_final_fatal_guard_stops_before_later_product(final_case, monkeypatch):
    store, batch, _, _ = final_case
    before = all_records(store)
    calls = install_reproduction_client(monkeypatch)
    original = worker.RealBatchProcessor.reserve_real

    def exhausted(processor, operation, *args, **kwargs):
        if operation == "inference":
            raise ExecutionConfigurationError("REPRODUCTION ONLY: fatal capacity stop")
        return original(processor, operation, *args, **kwargs)

    monkeypatch.setattr(worker.RealBatchProcessor, "reserve_real", exhausted)
    with pytest.raises(ExecutionConfigurationError):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
    assert calls == []
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert len(ledger["executions"]) == 3
    assert ledger["reservations"] == json.loads(before[real_pilot.BUDGET_KEY][0])["reservations"]
    second = read_json(store, f"items/{batch['id']}/row-3.json")[0]
    assert "started_at" not in second
    assert all(store.read_bytes(key) == value for key, value in before.items()
               if key.startswith(("results/", "operations/")))


def test_final_review_and_export_are_bound_to_new_attempt(final_case, monkeypatch):
    from tests.test_real_batch_worker import install_services

    store, batch, _, amendment = final_case
    before = all_records(store)
    analyses, _, _, _ = install_services(monkeypatch)
    worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
    assert not analyses
    service = BatchService(store)
    item_key = amendment["selected_item_keys"][0]
    detail = service.detail(batch["id"], item_key, batch["owner"])
    attribute = next(entry for entry in detail["machine_result"]["attributes"] if entry["candidates"])
    service.review(batch["id"], item_key, batch["owner"], {
        "attribute_id": attribute["attribute_id"], "decision": "approve", "candidate_index": 0,
        "reason": "Synthetic review isolation regression only",
    })
    expected = detail["result_key"].replace("results/", "reviews/", 1)
    assert store.keys(f"reviews/{batch['id']}/{item_key}") == [expected]
    exported = read_workbook(service.export(batch["id"], batch["owner"]))
    assert len(exported["Reviews"]) == 1 and exported["Reviews"][0]["Decision"] == "approve"
    assert exported["Reviews"][0]["Attribute"] == attribute["attribute_id"]
    assert_append_only(store, batch, amendment, before)


@pytest.mark.parametrize("seconds", [4500, 5399, 5401])
def test_final_window_cannot_shrink_or_expand_its_readiness_anchor(final_case, seconds):
    store, batch, approval, amendment = final_case
    candidate = copy.deepcopy(amendment)
    candidate["operating_expires_at"] = (
        datetime.fromisoformat(candidate["readiness_at"]) + timedelta(seconds=seconds)
    ).isoformat()
    before = all_records(store)
    with pytest.raises(ValueError, match="readiness-anchored 90-minute"):
        real_pilot.validate_final_rerun(
            candidate, approval, batch, read_json(store, real_pilot.BUDGET_KEY)[0], store,
        )
    assert before == all_records(store)


def release_bindings(path, approval):
    path = Path(path)
    baseline = json.loads(path.read_bytes())
    root = path.parent.resolve()
    assert root == Path(baseline["work_root"]).resolve()
    assert baseline["approval_sha256"] == real_pilot._sha256(approval)
    assert re.fullmatch(r"[a-f0-9]{64}", baseline["target"])
    assert real_pilot._sha256(baseline["history"]) == baseline["history_sha256"]
    for name, expected in baseline["history"].items():
        retained = root / name
        assert not retained.is_symlink() and retained.resolve().is_relative_to(root)
        assert sha(retained.read_bytes()) == expected, "Release history changed since baseline"
    workspace = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workspace, text=True,
    ).strip()
    assert revision == baseline["source_revision"]
    changed = subprocess.check_output([
        "git", "status", "--porcelain", "--untracked-files=all", "--",
        "backend", "tests/test_final_rerun.py",
    ], cwd=workspace, text=True)
    assert not changed.strip(), "Commit the exact tested backend and gate before release binding"
    return {key: baseline[key] for key in ("target", "history_sha256", "source_revision")}


def test_release_gate_binding_checks_real_history_and_clean_commit(tmp_path, monkeypatch):
    approval = {"approved": True}
    retained = tmp_path / "original.json"
    retained.write_bytes(b'{"synthetic":true}')
    history = {retained.name: sha(retained.read_bytes())}
    baseline = {
        "work_root": str(tmp_path), "target": "1" * 64, "source_revision": "2" * 40,
        "approval_sha256": real_pilot._sha256(approval), "history": history,
        "history_sha256": real_pilot._sha256(history),
    }
    path = tmp_path / "final-baseline.json"
    path.write_text(json.dumps(baseline))
    monkeypatch.setattr(subprocess, "check_output", lambda command, **kwargs:
                        "2" * 40 if command[1] == "rev-parse" else "")
    assert release_bindings(path, approval)["source_revision"] == "2" * 40
    retained.write_bytes(b'{"synthetic":false}')
    with pytest.raises(AssertionError, match="history changed"):
        release_bindings(path, approval)
    retained.write_bytes(b'{"synthetic":true}')
    monkeypatch.setattr(subprocess, "check_output", lambda command, **kwargs:
                        "2" * 40 if command[1] == "rev-parse" else " M backend/real_pilot.py")
    with pytest.raises(AssertionError, match="Commit the exact"):
        release_bindings(path, approval)


def test_exact_private_final_rerun_acceptance(monkeypatch):
    paths = {key: os.environ.get("DOCINTEL_TEST_FINAL_" + key) for key in ("SNAPSHOT", "PROMPTS", "DOCUMENTS")}
    if not any(paths.values()):
        pytest.skip("Opt-in exact private snapshot, four captures, and hash-bound approved copies required")
    assert all(paths.values()), "Partial private gate configuration must fail, not skip"
    snapshot_bytes = Path(paths["SNAPSHOT"]).read_bytes()
    prompt_bytes = Path(paths["PROMPTS"]).read_bytes()
    records = json.loads(snapshot_bytes)["records"]
    captures = json.loads(prompt_bytes)
    assert len(captures) == 4 and len({entry["item_key"] for entry in captures}) == 2
    original = ReproductionStore()
    for key, value in records.items():
        raw = base64.b64decode(value["base64"], validate=True)
        assert sha(raw) == value["sha256"]
        original.write_bytes(key, raw)
    approval = read_json(original, real_pilot.APPROVAL_KEY)[0]
    batch = read_json(original, f"batches/{approval['batch_id']}.json")[0]
    consumed = read_json(original, real_pilot.BUDGET_KEY)[0]
    assert len(consumed["executions"]) == 2 and consumed["attempted"]["inference"] == 4
    assert consumed["reserved"]["input_tokens"] == 92854
    assert consumed["reserved"]["output_tokens"] == 8192
    selected = read_json(original, real_pilot.RECOVERY_KEY)[0]["selected_item_keys"]
    assert list(dict.fromkeys(entry["item_key"] for entry in captures)) == selected
    needed = {
        source["sha256"]: source["blob"]
        for item in batch["items"] if item["item_key"] in selected
        for source in item["sources"] if source["kind"] == "blob"
    }
    for path in Path(paths["DOCUMENTS"]).iterdir():
        if path.is_file() and path.suffix.lower() in {".pdf", ".xlsx"}:
            raw = path.read_bytes()
            content_sha = sha(raw)
            if content_sha in needed and needed[content_sha] not in original.records:
                original.write_bytes(needed[content_sha], raw)
    assert all(key in original.records for key in needed.values()), "Hash-verified approved local copies missing"
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "true")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", approval["approved_by"])
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID", approval["identities"]["worker_principal_id"])
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_EXECUTION_SCOPE", "internal_only")
    for key, value in approval["environment"].items():
        if value is not None:
            monkeypatch.setenv(key, value)
        if hasattr(settings, key):
            monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(real_pilot, "current_environment", lambda: copy.deepcopy(approval["environment"]))
    assert approval["environment"]["AOAI_API_VERSION"] == LLM_API_VERSION
    outcomes = []
    for invalid in (False, True):
        store = ReproductionStore(copy.deepcopy(original.records))
        confirmed = datetime(2026, 10, 5, 4, 44, 32, 504000, tzinfo=timezone.utc)
        amendment = final_amendment(store, batch, approval, confirmed=confirmed)
        write_json(store, FINAL_KEY, amendment)
        monkeypatch.setattr(real_pilot, "_now", lambda: confirmed + timedelta(minutes=5))
        before = all_records(store)
        assert real_pilot.validate_final_rerun(amendment, approval, batch, consumed, store) == amendment
        assert before == all_records(store)
        replace(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
        calls = install_reproduction_client(monkeypatch, captures, invalid=invalid)
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=2)
        assert len(calls) == 4, "Only complete production-generated captures count as acceptance"
        ledger = assert_append_only(store, batch, amendment, before, invalid=invalid)
        reserved = {key: ledger["reserved"][key] - consumed["reserved"][key]
                    for key in ("input_tokens", "output_tokens", "analysis_pages", "microdollars")}
        assert reserved["input_tokens"] == sum(call["input_tokens"] for call in calls)
        assert reserved["output_tokens"] == sum(call["output_tokens"] for call in calls) == 8192
        assert reserved["analysis_pages"] == 0 and ledger["attempted"]["inference"] == 8
        for item_key in selected:
            detail = BatchService(store).detail(batch["id"], item_key, batch["owner"])
            machine = detail["machine_result"]
            candidates = [candidate for attribute in machine["attributes"] for candidate in attribute["candidates"]]
            if invalid:
                assert len(machine["validation_diagnostics"]) == 2
            else:
                assert len(candidates) == 1 and not machine["validation_diagnostics"]
                candidate = candidates[0]
                assert "REPRODUCTION ONLY" in candidate["qualification"]
                evidence = {entry["evidence_id"]: entry for entry in machine["evidence"]}
                assert candidate["evidence_ids"] and all(
                    evidence[reference]["source_tier"] == "vendor_table"
                    for reference in candidate["evidence_ids"]
                )
                assert any(
                    candidate["supporting_quote"] == cell["value"]
                    for reference in candidate["evidence_ids"]
                    for cell in json.loads(evidence[reference]["text"])["cells"]
                )
            assert any(source["parsing"] == "cache" for source in detail["provenance"])
            assert not any(source["parsing"] == "fresh_analysis" for source in detail["provenance"])
        for dimension in ("input_tokens", "output_tokens"):
            assert ledger["reserved"][dimension] <= approval["limits"][dimension]
        assert ledger["reserved"]["microdollars"] <= approval["limits"]["spend_microdollars"]
        requests = [
            {"reservation_id": key, "item_key": value["item_key"], **value["reserved_usage"]}
            for key, value in ledger["reservations"].items()
            if key not in consumed["reservations"]
        ]
        assert len(requests) == 4
        assert all(value["input_tokens"] + value["output_tokens"] <= 30000 for value in requests)
        outcomes.append({
            "mode": "unknown_citation" if invalid else "accepted_vendor_response",
            "new_requests": len(calls), "new_reservations": reserved,
            "full_request_reservations": requests, "execution_count": len(ledger["executions"]),
            "execution_exception": ledger["final_rerun"]["additional_execution_exception"],
            "total_reserved": ledger["reserved"], "full_production_prompts": calls,
            "append_only_history": True, "owner_isolation": True,
            "safe_failure_diagnostics": invalid, "attempt_export_verified": True,
        })
    assert Path(paths["SNAPSHOT"]).read_bytes() == snapshot_bytes
    assert Path(paths["PROMPTS"]).read_bytes() == prompt_bytes
    receipt_path = os.environ.get("DOCINTEL_TEST_FINAL_RECEIPT")
    if receipt_path:
        path = Path(receipt_path)
        private_root = Path(paths["SNAPSHOT"]).resolve().parent
        assert path.parent.resolve() == private_root, "Receipt must stay beside the private snapshot"
        assert not private_root.is_relative_to(Path(__file__).resolve().parents[1]), "Private receipts must stay outside Git"
        scope = {key: value for key, value in amendment.items()
                 if key not in {"not_before", "expires_at", "readiness_at", "operating_expires_at"}}
        receipt = {
            "schema_version": 1, "passed": True, "no_live_operations": True,
            "historical_pre_final_reproduction": True, "current_capacity_acceptance": False,
            "window_policy": {"publication_seconds": 2700, "operating_seconds": 5400, "processing_seconds": 1200},
            "reproduction_only": True, "product_findings": False, "live_calls": 0,
            "production_run_batch": True, "real_guard_reservations": True,
            "snapshot_sha256": sha(snapshot_bytes), "prompts_sha256": sha(prompt_bytes),
            "approval_sha256": real_pilot._sha256(approval),
            "prior_ledger_sha256": real_pilot._sha256(consumed),
            "scope_sha256": real_pilot._sha256(scope), "amendment_scope": scope,
            "checks": {
                "production_run_batch": True, "real_guard_reservations": True,
                "full_four_prompt_match": True, "expected_token_reservations": True,
                "zero_new_analysis": True, "append_only_history": True,
                "immutable_results": True, "attempt_history_export": True,
                "owner_isolation": True, "unknown_citation_diagnostics": True,
            },
            "cases": outcomes,
        }
        baseline_path = os.environ.get("DOCINTEL_TEST_FINAL_BASELINE")
        if baseline_path:
            receipt.update(release_bindings(baseline_path, approval))
        descriptor = os.open(os.path.relpath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2)
