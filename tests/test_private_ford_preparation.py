"""Opt-in latest 29-record Ford PREPARATION rehearsal; SYNTHETIC SDK, never Azure."""

import ast
import base64
import copy
import json
import os
import subprocess
import zlib
from pathlib import Path

import pytest

from backend.batch_store import read_json, write_json
from scripts import ford_analysis_preparation as prep
from tests.test_ford_analysis_preparation import (
    ClosedStore, authorize, fake_credential, no_live, run, synthetic_sdk,  # noqa: F401
)


def method_tree(content, class_name, method):
    tree = ast.parse(content)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    return ast.dump(next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method))


def test_latest_private_preparation_gate(monkeypatch):
    value = os.environ.get("DOCINTEL_TEST_FORD_PREPARATION_WORK")
    if not value:
        pytest.skip("Exact latest private Ford preparation fixture is opt-in")
    work = Path(value).resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    assert work.is_dir() and not work.is_relative_to(root)
    output = os.environ["DOCINTEL_TEST_FORD_PREPARATION_OUTPUT"]
    assert Path(output).name == output
    source_revision = "1f6130e7fb9dfb00bcd9e1447300d8dded9895c2"
    config = prep.release.load_config(work / "target.json")
    snapshot_path, snapshot, records = prep.load_snapshot(work)
    assert len(records) == 29
    assert snapshot["observed_at"] == "2026-10-06T19:07:52.317123+00:00"
    initial_snapshot = snapshot_path.read_bytes()
    packet = prep.plan(work, config, source_revision)
    assert packet["reserved_microdollars"] == 50000
    assert prep.sha(initial_snapshot) == "caa2cefc13739ece9dd13fac81b8602775f9f23213c866414e6646e24f9b240d"
    assert packet["snapshot_hash_semantics"] == "records_as_parsed_canonical_json"
    source_proof = {}
    for filename, cls, method in (
        ("backend/batch_worker.py", "BatchProcessor", "cached"),
        ("backend/real_pilot.py", "RealPilotGuard", "_cost"),
        ("backend/core/docintel.py", "DocumentIntelligenceService", "extract_pdf_bytes"),
        ("backend/core/docintel.py", "DocumentIntelligenceService", "_to_parsed_document"),
    ):
        prior = subprocess.check_output(["git", "show", f"{source_revision}:{filename}"], cwd=root)
        current = (root / filename).read_bytes()
        assert method_tree(prior, cls, method) == method_tree(current, cls, method)
        source_proof[cls + "." + method] = "AST-equal to the existing API source"
    manifest = prep.release.private_json(work / "ford-local-parse-readiness.json")
    pdf = Path(manifest["approved_local_pdf"]["path"])
    content = pdf.read_bytes()
    assert prep.sha(content) == prep.SOURCE_SHA256 and len(content) == 156227
    store = ClosedStore()
    for key, raw in records.items():
        store.write_bytes(key, raw)
    store.write_bytes(prep.SOURCE_KEY, content)
    before = copy.deepcopy(store.records)
    original = json.loads(records[prep.APPROVAL_KEY])
    authorization = authorize(packet, original["approved_by"])
    large_synthetic_text = "SYNTHETIC ONLY; NOT FORD FINDINGS\n" + "\n".join(
        f"{index}: {prep.sha(str(index).encode())}" for index in range(120)
    )
    sdk = synthetic_sdk(monkeypatch, store, text=large_synthetic_text)
    local_work = work / (output.removesuffix(".json") + "-SYNTHETIC-local")
    Path(os.path.relpath(local_work)).mkdir(mode=0o700)
    frames, phases, captured_wires = {}, [], []
    sdk_tokens = []
    original_plan = prep.plan
    original_load = prep.load_snapshot
    monkeypatch.setattr(prep, "plan", lambda supplied, cfg, rev: packet if supplied == local_work else original_plan(supplied, cfg, rev))
    monkeypatch.setattr(prep, "load_snapshot", lambda supplied: (snapshot_path, snapshot, records)
                        if supplied == local_work else original_load(supplied))
    backend = {
        "identity": {"type": "SystemAssigned", "principalId": packet["api_principal_id"]},
        "properties": {
            "provisioningState": "Succeeded", "latestReadyRevisionName": packet["runtime"]["backend_revision"],
            "latestRevisionName": packet["runtime"]["backend_revision"],
            "template": {"containers": [{"name": "api", "image": packet["runtime"]["backend_image"], "env": []}]},
        },
    }
    job = {"properties": {"template": {"containers": [{"name": "worker", "image": packet["runtime"]["worker_image"], "env": []}]}}}
    monkeypatch.setattr(prep.release, "app", lambda *args, **kwargs: backend)
    monkeypatch.setattr(prep.release, "active_executions", lambda *args, **kwargs: (job, []))
    resource_id = (f"/subscriptions/{config['subscription']}/resourceGroups/{config['group']}"
                   "/providers/Microsoft.CognitiveServices/accounts/di-docintel-pilot-erik3")

    def readonly_fake(*args, **kwargs):
        if args == ("account", "show"):
            return {"id": config["subscription"], "tenantId": config["tenant"]}
        if args[:3] == ("ad", "signed-in-user", "show"):
            return {"id": packet["analysis_identity"]["principal_id"]}
        if args[:3] == ("cognitiveservices", "account", "show"):
            return {"id": resource_id, "kind": "FormRecognizer",
                    "properties": {"endpoint": packet["analysis_endpoint"], "disableLocalAuth": True}}
        assert args[:3] == ("role", "assignment", "list") and "--all" not in args
        return [{
            "principalId": principal, "scope": resource_id,
            "roleDefinitionId": "/roleDefinitions/a97b65f3-24c7-4388-baec-2e87135dc908",
        } for principal in (packet["analysis_identity"]["principal_id"], original["identities"]["worker_principal_id"])]

    monkeypatch.setattr(prep.release, "azure", readonly_fake)

    def local_credential(**options):
        assert options == {"tenant_id": config["tenant"]}
        wrapper = fake_credential(packet)
        sdk_tokens.append(wrapper._credential.get_token().token)
        return wrapper._credential

    monkeypatch.setattr("azure.identity.AzureCliCredential", local_credential)

    def api_storage_console(arguments, code, marker, **options):
        assert arguments[:3] == ["az", "containerapp", "exec"]
        packed = ast.parse(code).body[0].value.args[0].args[0].args[0].value
        program = zlib.decompress(base64.b85decode(packed)).decode()
        parsed = ast.parse(program)
        assignment = next(node for node in parsed.body if isinstance(node, ast.Assign)
                          and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "p")
        wire = json.loads(assignment.value.args[0].value)
        phase = wire["phase"]
        if phase != "claim":
            wire["packet"] = read_json(store, prep.PREFIX + "packet.json")[0]
            wire["authorization"] = read_json(store, prep.PREFIX + "authorization.json")[0]
            audit = read_json(store, prep.PREFIX + "storage-program.json")[0]
            assert audit["sha256"] == wire["storage_program_sha256"]
            wire["helper"] = audit["source"]
        assert prep.sha(prep.canonical(wire["packet"])) == wire["packet_sha256"]
        assert prep.sha(prep.canonical(wire["authorization"])) == wire["authorization_sha256"]
        assert wire["storage_program_sha256"] == wire["packet"]["storage_program_sha256"]
        assert prep.sha(wire["helper"].encode()) == wire["storage_program_sha256"]
        assert "RecordedPreparationParser" not in wire["helper"]
        assert "VerifiedOperatorCredential" not in wire["helper"] and "AzureCliCredential(" not in wire["helper"]
        assert not any(token in program for token in sdk_tokens)
        phases.append(phase)
        captured_wires.append(wire)
        frames[phase] = len(base64.b64encode(code.encode())) + 80
        assert frames[phase] <= 16384
        namespace = {"__name__": "synthetic_api_storage"}
        exec(compile(wire["helper"], "<reviewed-synthetic-api-storage>", "exec"), namespace)
        if phase == "claim":
            assert not sdk.calls
            value = namespace["claim_preparation"](store, wire["packet"], wire["authorization"], runtime_check=lambda: None)
            write_json(store, prep.PREFIX + "storage-program.json", {
                "source": wire["helper"], "sha256": wire["storage_program_sha256"],
            })
            write_json(store, prep.PREFIX + "packet.json", wire["packet"])
        else:
            if phase == "complete":
                assert (local_work / "ford-preparation-local-parsed.json").is_file()
                assert (local_work / "ford-preparation-local-analysis-receipt.json").is_file()
            function = namespace["persist_submitted" if phase == "submitted" else "complete_preparation"]
            value = function(store, wire["packet"], wire["authorization"], wire["value"], runtime_check=lambda: None)
        return ("DOCINTEL_FORD_PREPARATION_RESULT:" + base64.b64encode(json.dumps(value).encode()).decode()
                + "\n" + marker + "\n").encode()

    monkeypatch.setattr(prep.release, "console_code", api_storage_console)
    result = prep.execute(local_work, config, packet, authorization)
    assert phases == ["claim", "submitted", "complete"]
    assert len(sdk.calls) == 1 and sdk.calls[0][1] == content
    ledger = read_json(store, prep.BUDGET_KEY)[0]
    assert ledger["attempted"]["analysis"] == 2 and ledger["reserved"]["analysis_pages"] == 10
    assert ledger["reserved"]["microdollars"] == 1066296 and len(ledger["executions"]) == 4
    assert ledger["attempted"]["inference"] == 11 and ledger["attempted"]["search"] == 0
    assert all(store.read_bytes(key) == record for key, record in before.items() if key != prep.BUDGET_KEY)
    assert store.read_bytes(prep.PREFIX + "ledger-before.json")[0] == records[prep.BUDGET_KEY]
    with pytest.raises(ValueError):
        prep.execute(local_work, config, packet, authorization)
    assert len(sdk.calls) == 1
    assert snapshot_path.read_bytes() == initial_snapshot and pdf.read_bytes() == content
    assert all(json.loads(raw) is not None for key, (raw, _) in store.records.items() if key.endswith(".json"))
    assert not any(token.encode() in raw for token in sdk_tokens for raw, _ in store.records.values())
    for path in local_work.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert not any(token.encode() in path.read_bytes() for token in sdk_tokens)
    report = {
        "label": "SYNTHETIC SDK REPRODUCTION ONLY; NO ACTUAL FORD ANALYSIS OR AUTHORIZATION",
        "status": "offline_preparation_gate_passed", "snapshot_sha256": prep.sha(initial_snapshot),
        "snapshot_records": 29, "source_sha256": prep.SOURCE_SHA256, "source_bytes": len(content),
        "api_source_revision": source_revision, "production_contract_proof": source_proof,
        "runtime": snapshot["runtime"], "helper_sha256": packet["helper_sha256"],
        "provenance_source_sha256": packet["provenance_source_sha256"],
        "gate_sha256": prep.sha(Path(__file__).read_bytes()),
        "synthetic_sdk_fixture_sha256": prep.sha((root / "tests/test_ford_analysis_preparation.py").read_bytes()),
        "all_prior_records_preserved_except_append_only_ledger_charge": True,
        "original_ledger_bytes_preserved_separately": True,
        "prior_record_sha256": packet["history_sha256"],
        "prior_record_hash_semantics": "sha256_sorted_compact_ascii_json",
        "simulated_analysis_submissions": len(sdk.calls), "real_analysis_submissions": 0,
        "azure_calls": 0, "credential_calls": 0, "worker_executions": 0, "inference_calls": 0,
        "readiness_clock_started": False, "new_reserved_analysis": 1, "new_reserved_pages": 5,
        "new_reserved_microdollars": 50000, "measured_pages_simulated": 1,
        "cache_key": result["cache_key"], "cache_sha256_is_synthetic": result["cache_sha256"],
        "cache_canonical_sha256_is_synthetic": result["cache_canonical_sha256"],
        "ledger_after_canonical_sha256_is_reproduction": result["ledger_after_canonical_sha256"],
        "both_production_cached_checks_passed": True, "repeat_rejected": True,
        "console_frame_bytes": frames, "storage_phases": phases,
        "api_di_role_required": False, "operator_and_worker_existing_roles_only": True,
        "operator_token_never_transferred_or_stored": True,
        "local_mapped_parse_retained_before_cache": True,
        "new_audit_records_are_canonical_json": True,
        "synthetic_mapped_text_bytes": len(large_synthetic_text.encode()),
        "synthetic_local_evidence_directory": str(local_work),
        "synthetic_analysis_metadata": result["analysis_receipt"],
    }
    prep.release.save_once(work / output, report)
    assert (work / output).stat().st_mode & 0o777 == 0o600
