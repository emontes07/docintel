"""Network-free private reconstruction and the unchanged production budget gate.

Opt in with DOCINTEL_TEST_PRIVATE_ROOT (an existing private release directory),
DOCINTEL_TEST_PRIVATE_DOCUMENTS (approved local copies), and optionally
DOCINTEL_TEST_PRIVATE_OUTPUT (a new filename prefix, never an allowance root).
The root contains retained snapshots, prompts, diagnostics, and the explicitly
REPRODUCTION-only verification-reproduction-responses.json. No customer fixture
belongs in Git or CI. A passing test can and must report a NO_GO capacity result.
"""

import ast
import base64
import copy
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend import batch_worker as worker, extract, real_pilot
from backend.batch import BatchService
from backend.batch_store import Missing, read_json, write_json
from backend.core.config import settings
from backend.models.enrichment import Evidence, ExtractionResponse, Manifest, OfflineBundle, OfflineSource
from backend.response_validation import ResponseValidationError, invalid, map_citations
from backend.workbooks import read_workbook
from tests.test_final_rerun import (
    ReproductionStore, all_records, network_blocked, replace,  # noqa: F401
)
from tests.test_interrupted_reconciliation import rehydrate_private_snapshot, request_for


TESTED_SOURCES = (
    "backend/extract.py", "backend/evidence_verification.py", "backend/batch_worker.py",
    "backend/real_pilot.py", "backend/core/instructions.py", "backend/models/enrichment.py",
    "backend/multisource.py", "backend/batch.py", "tests/test_private_verification.py",
    "backend/interrupted_reconciliation.py", "backend/batch_store.py",
    "tests/test_interrupted_reconciliation.py",
    "tests/test_final_rerun.py", "tests/test_real_pilot.py", "tests/test_real_batch_worker.py",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def code_fingerprints():
    workspace = Path(__file__).resolve().parents[1]
    return {name: sha((workspace / name).read_bytes()) for name in TESTED_SOURCES}


def stable(value):
    if isinstance(value, dict):
        return {key: stable(child) for key, child in value.items()
                if key not in {"observed_at", "provider_retrieved_at"}}
    if isinstance(value, list):
        return [stable(child) for child in value]
    return value


def private_write(root, filename, content):
    assert Path(filename).name == filename
    path = root / filename
    descriptor = os.open(os.path.relpath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    assert path.stat().st_mode & 0o777 == 0o600
    return {"file": filename, "sha256": sha(content), "bytes": len(content)}


def private_json(root, filename, content):
    return private_write(root, filename, json.dumps(content, indent=2, ensure_ascii=False).encode())


def assert_retained_shape(response, diagnostic):
    actual = response.model_dump(mode="json")
    retained = diagnostic["parsed_response"]
    assert set(actual) == set(retained)
    assert len(actual["candidates"]) == len(retained["candidates"])
    for candidate, shape in zip(actual["candidates"], retained["candidates"], strict=True):
        assert set(candidate) == set(shape)
        for field, value in candidate.items():
            if field == "evidence_ids":
                assert value == [entry["value"] for entry in shape[field]]
                assert all(not entry["redacted"] for entry in shape[field])
            else:
                assert type(value).__name__ == shape[field]["type"], field


def redacted_value_ambiguity(response, diagnostic):
    examples = []
    for index, candidate in enumerate(response.candidates):
        retained = diagnostic["parsed_response"]["candidates"][index]
        assert all(retained[field]["redacted"] is True for field in (
            "attribute_id", "value", "supporting_quote", "qualification", "confidence",
        ))
        alternatives = [False, True] if type(candidate.value) is bool else [
            "REPRODUCTION VALUE A", "REPRODUCTION VALUE B",
        ]
        for alternative in alternatives:
            variant = response.model_copy(deep=True)
            variant.candidates[index].value = alternative
            assert_retained_shape(variant, diagnostic)
        examples.append({
            "candidate_index": index, "retained_value_type": retained["value"]["type"],
            "structurally_compatible_alternatives": alternatives,
            "source_validation_claimed": False,
        })
    return {
        "label": "STRUCTURE-ONLY AMBIGUITY DEMONSTRATION",
        "original_values_available": False, "original_quotes_available": False,
        "limitation": "Different values preserve every retained type/reference. These artifacts cannot identify "
                      "the original values or quotations; response hashes are provenance, not recovered content.",
        "examples": examples,
    }


def fragment_record(reference, entry, compact):
    return {
        "reference": reference,
        "compact_row": next(row for row in compact["evidence"] if row[0] == reference),
        "full_fragment": entry,
        "canonical_full_fragment_sha256": sha(json.dumps(entry, sort_keys=True, ensure_ascii=True).encode()),
        "text_sha256": sha(entry["text"].encode()),
    }


def old_validator(revision):
    assert re.fullmatch(r"[a-f0-9]{40}", revision)
    source = subprocess.check_output(
        ["git", "show", f"{revision}:backend/extract.py"], cwd=Path(__file__).resolve().parents[1],
    )
    tree = ast.parse(source)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "validate_response")
    namespace = {
        "ExtractionResponse": ExtractionResponse, "Manifest": Manifest, "Evidence": Evidence,
        "invalid": invalid, "re": re,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<retained-verifier>", "exec"), namespace)
    return namespace["validate_response"], sha(source)


def validation_outcome(validator, candidate, manifest, evidence, index):
    try:
        verification = validator(ExtractionResponse(candidates=[candidate]), manifest, evidence)
    except ResponseValidationError as error:
        return {
            "accepted": False,
            "issues": [{**entry.model_dump(), "field_path": entry.field_path.replace(
                "candidates[0]", f"candidates[{index}]", 1,
            )} for entry in error.issues],
        }
    return {
        "accepted": True, "issues": [],
        "verification": [entry.model_dump(mode="json") for entry in (verification or [])],
    }


def old_literal_comparison(candidate):
    literal = str(candidate.value)
    if isinstance(candidate.value, float) and candidate.value.is_integer():
        literal = str(int(candidate.value))
    literals = ("true", "yes") if candidate.value is True else ("false", "no") if candidate.value is False else (literal,)
    pattern = rf"(?<!\w)(?:{'|'.join(re.escape(value) for value in literals)})(?!\w)"
    return {
        "literal_pattern": pattern, "flags": "IGNORECASE", "supporting_quote": candidate.supporting_quote,
        "literal_matches_quote": bool(re.search(pattern, candidate.supporting_quote, re.IGNORECASE)),
    }


@pytest.fixture
def private_inputs():
    root_value = os.environ.get("DOCINTEL_TEST_PRIVATE_ROOT")
    documents_value = os.environ.get("DOCINTEL_TEST_PRIVATE_DOCUMENTS")
    if not root_value and not documents_value:
        pytest.skip("Exact private release snapshots and approved local copies are opt-in")
    assert root_value and documents_value, "Incomplete private gate configuration"
    root = Path(root_value).resolve()
    assert root.is_dir() and not Path(root_value).is_symlink()
    assert not root.is_relative_to(Path(__file__).resolve().parents[1]), "Customer evidence must stay outside Git"
    names = {
        "before": "continuation-final-private-snapshot.json",
        "after": "final-postclosure-private-snapshot.json",
        "diagnostics": "final-retained-validation-diagnostics.json",
        "execution": "final-postclosure-execution.json",
        "worker": "final-deployed-images-and-stopped-worker.json",
        "timing": "final-worker-timing-observations.json",
        "prompts": "mueller-readiness-26c878d-budget/private-prompts.json",
        "responses": "verification-reproduction-responses.json",
        "counterfactual": "verification-counterfactual-normalization-responses.json",
    }
    raw = {key: (root / name).read_bytes() for key, name in names.items()}
    inputs = {key: json.loads(value) for key, value in raw.items()}
    inputs.update(root=root, documents=Path(documents_value), hashes={key: sha(value) for key, value in raw.items()})
    yield inputs
    assert all((root / names[key]).read_bytes() == value for key, value in raw.items())


def reconcile_private(inputs, monkeypatch):
    snapshot = inputs["after"]
    store = rehydrate_private_snapshot(snapshot)
    batch_id, actor = snapshot["batch"]["id"], snapshot["ledger"]["approved_by"]
    running = [key for key, detail in snapshot["details"].items() if detail["state"] == "running"]
    assert len(running) == 1
    item_key = running[0]
    evidence = {
        "execution_id": snapshot["final_audit"]["execution_id"],
        **{name: inputs[name] for name in ("execution", "worker", "timing")},
    }
    request = request_for(store, batch_id, item_key, evidence)
    before = store.snapshot()
    with monkeypatch.context() as isolated:
        isolated.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", actor)
        isolated.setenv("DOCINTEL_REAL_PILOT_ENABLED", "false")
        isolated.setenv("DOCINTEL_BATCH_LIVE_ENABLED", "false")
        receipt = BatchService(store).reconcile_interrupted(batch_id, item_key, actor, **request)
        after = store.snapshot()
        assert BatchService(store).reconcile_interrupted(batch_id, item_key, actor, **request) == receipt
        assert store.snapshot() == after
    state = read_json(store, f"items/{batch_id}/{item_key}.json")[0]
    assert state["state"] == "interrupted"
    assert state["previous_attempt"] == snapshot["details"][item_key]["previous_attempt"]
    changed = {key for key, value in before.items() if store.read_bytes(key) != value}
    assert changed == {f"items/{batch_id}/{item_key}.json", f"batches/{batch_id}.json"}
    assert store.read_bytes(real_pilot.BUDGET_KEY) == before[real_pilot.BUDGET_KEY]
    store.connection.close()
    return after, {
        "scope": "isolated local API-snapshot reconstruction only",
        "hosted_ready_pins": False,
        "limitation": "Versions/raw SHA256 pins are reconstructed locally, not original hosted bytes or ETags.",
        "item_key": item_key, "before_state": "running", "after_state": state["state"],
        "ledger_unchanged": True, "previous_attempt_unchanged": True, "idempotent": True,
        "request": request, "receipt": receipt, "changed_existing_keys": sorted(changed),
    }


def hydrate(inputs, reconciled):
    store = ReproductionStore()
    for key, value in inputs["before"]["records"].items():
        raw = base64.b64decode(value["base64"], validate=True)
        assert sha(raw) == value["sha256"]
        store.write_bytes(key, raw)
    after = inputs["after"]
    approval = read_json(store, real_pilot.APPROVAL_KEY)[0]
    batch = read_json(store, f"batches/{approval['batch_id']}.json")[0]
    assert real_pilot.binding_digest(batch) == real_pilot.binding_digest(after["batch"])
    write_json(store, real_pilot.FINAL_RERUN_KEY, after["final_audit"]["amendment"])
    for key, (raw, _) in reconciled.items():
        if key in store.records:
            previous, version = store.read_bytes(key)
            if key.startswith("results/"):
                assert json.loads(previous) == json.loads(raw)
                continue
            store.write_bytes(key, raw, version)
        else:
            store.write_bytes(key, raw)
    batch = read_json(store, f"batches/{approval['batch_id']}.json")[0]
    needed = {
        source["sha256"]: source["blob"]
        for item in batch["items"] if item["item_key"] in after["final_audit"]["amendment"]["selected_item_keys"]
        for source in item["sources"] if source["kind"] == "blob"
    }
    for path in inputs["documents"].iterdir():
        if path.is_file() and path.suffix.lower() in {".pdf", ".xlsx"}:
            raw = path.read_bytes()
            digest = sha(raw)
            if digest in needed and needed[digest] not in store.records:
                store.write_bytes(needed[digest], raw)
    assert all(key in store.records for key in needed.values()), "Hash-bound approved copies are required"
    return store, batch, approval


def reconstruct(inputs, batch):
    responses = inputs["responses"]
    assert responses["label"] == "REPRODUCTION" and responses["recovered_model_output"] is False
    baseline, baseline_hash = old_validator(inputs["diagnostics"]["application_revision"])
    assert inputs["diagnostics"]["raw_response_text_stored"] is False
    all_diagnostics = {
        (product["product"], diagnostic["source_tier"]): diagnostic
        for product in inputs["diagnostics"]["products"] for diagnostic in product["diagnostics"]
    }
    reports, by_request = [], {}
    for capture in inputs["prompts"]:
        item = next(item for item in batch["items"] if item["item_key"] == capture["item_key"])
        tier = capture["user"]["evidence"][0]["source_tier"]
        key = (item["item_key"], tier)
        response = ExtractionResponse.model_validate(responses["requests"][item["item_key"]][tier])
        by_request[key] = response
        compact, references = worker.compact_inference_prompt(json.dumps(capture["user"]))
        manifest = Manifest.model_validate(item["manifest"])
        evidence = [Evidence.model_validate(entry) for entry in capture["user"]["evidence"]]
        mapped = map_citations(response, references)
        diagnostic = all_diagnostics.get((manifest.product.item_id, tier))
        if diagnostic:
            assert list(references) == diagnostic["valid_references"]
            assert_retained_shape(response, diagnostic)
        supplied_references = list(dict.fromkeys(
            reference for candidate in response.candidates for reference in candidate.evidence_ids
        ))
        original_evidence = {entry["evidence_id"]: entry for entry in capture["user"]["evidence"]}
        comparisons = []
        for index, candidate in enumerate(mapped.candidates):
            old = validation_outcome(baseline, candidate, manifest, evidence, index)
            new = validation_outcome(extract.validate_response, candidate, manifest, evidence, index)
            cited = [entry for entry in evidence if entry.evidence_id in candidate.evidence_ids]
            comparisons.append({
                "candidate_index": index, "reconstructed_candidate": response.candidates[index].model_dump(mode="json"),
                "mapped_candidate": candidate.model_dump(mode="json"),
                "old": old, "new": new,
                "exact_old_literal_comparison": {
                    **old_literal_comparison(candidate),
                    "reached_in_old_validator": any(issue["field_path"].endswith(".value") for issue in old["issues"]),
                },
                "exact_old_quote_comparisons": [{
                    "evidence_id": entry.evidence_id, "source_locator": entry.source_locator,
                    "text": entry.text, "text_sha256": sha(entry.text.encode()),
                    "verbatim_quote_match": candidate.supporting_quote in entry.text,
                } for entry in cited],
            })
        if diagnostic:
            assert [issue["field_path"] for result in comparisons for issue in result["old"]["issues"]] == [
                issue["field_path"] for issue in diagnostic["issues"]
            ], "Every retained issue path must be reproduced, in original candidate order"
        expected = responses["expected_new"][item["item_key"]][tier]
        assert [result["candidate_index"] for result in comparisons if result["new"]["accepted"]] == expected["accepted"]
        assert [[issue["field_path"] for issue in result["new"]["issues"]] for result in comparisons] == expected["issues"]
        reports.append({
            "item_key": item["item_key"], "product": manifest.product.model_dump(), "source_tier": tier,
            "retained_record": diagnostic is not None,
            "retained_raw_response_sha256": diagnostic["raw_response_sha256"] if diagnostic else None,
            "retained_issues": diagnostic["issues"] if diagnostic else [],
            "redacted_content_ambiguity": redacted_value_ambiguity(response, diagnostic) if diagnostic else None,
            "original_prompt_sha256": sha(json.dumps(capture["user"], sort_keys=True).encode()),
            "compact_prompt_sha256": sha(compact.encode()), "compact_reference_map": references,
            "retained_reference_resolution": [
                fragment_record(reference, original_evidence[references[reference]], json.loads(compact))
                for reference in supplied_references
            ] if diagnostic else [],
            "comparisons": comparisons,
        })
    assert len(all_diagnostics) == sum(report["retained_record"] for report in reports)
    return by_request, {
        "label": "REPRODUCTION", "recovered_model_output": False, "product_findings": False,
        "claim_all_real_failures_fixed": "NO_GO",
        "limitation": "Redacted diagnostics retain types, references and issue paths, not values, attributes or quotes. "
                      "These representative responses are not recovered output and do not establish the actual failure cause.",
        "baseline_revision": inputs["diagnostics"]["application_revision"], "baseline_extract_sha256": baseline_hash,
        "source_hashes": inputs["hashes"], "requests": reports,
    }


def counterfactual_normalization(inputs, batch):
    fixture = inputs["counterfactual"]
    assert fixture["label"] == "COUNTERFACTUAL REPRODUCTION"
    assert fixture["retained_diagnostics_modified"] is False
    baseline, _ = old_validator(inputs["diagnostics"]["application_revision"])
    results = []
    for case in fixture["cases"]:
        capture = next(entry for entry in inputs["prompts"] if entry["item_key"] == case["item_key"]
                       and entry["user"]["evidence"][0]["source_tier"] == case["source_tier"])
        item = next(item for item in batch["items"] if item["item_key"] == case["item_key"])
        compact, references = worker.compact_inference_prompt(json.dumps(capture["user"]))
        entries = {entry["evidence_id"]: entry for entry in capture["user"]["evidence"]}
        for reference, expected in zip(case["corrected_references"], case["source_fragments"], strict=True):
            assert entries[references[reference]] == expected
        response = ExtractionResponse.model_validate(case["response"])
        assert response.candidates[0].evidence_ids == case["corrected_references"]
        assert "COUNTERFACTUAL REPRODUCTION" in response.candidates[0].qualification
        manifest = Manifest.model_validate(item["manifest"])
        evidence = [Evidence.model_validate(entry) for entry in capture["user"]["evidence"]]
        candidate = map_citations(response, references).candidates[0]
        old = validation_outcome(baseline, candidate, manifest, evidence, 0)
        new = validation_outcome(extract.validate_response, candidate, manifest, evidence, 0)
        assert old["issues"][0]["field_path"] == "candidates[0].supporting_quote"
        assert new["accepted"] and new["verification"][0]["quote"]["method"] == case["expected_quote_method"]
        header_only = response.model_copy(deep=True)
        header_only.candidates[0].evidence_ids = ["E1", "E2"]
        rejected = validation_outcome(
            extract.validate_response, map_citations(header_only, references).candidates[0], manifest, evidence, 0,
        )
        assert not rejected["accepted"] and rejected["issues"][0]["field_path"] == "candidates[0].supporting_quote"
        results.append({
            "label": "COUNTERFACTUAL REPRODUCTION", "name": case["name"], "item_key": case["item_key"],
            "retained_response": False, "response": response.model_dump(mode="json"),
            "corrected_source_fragments": [
                fragment_record(reference, entries[references[reference]], json.loads(compact))
                for reference in case["corrected_references"]
            ],
            "old": old, "new": new,
            "same_quote_with_header_only_references": rejected,
        })
    return {
        "label": "COUNTERFACTUAL REPRODUCTION", "cases": results, "model_calls": 0,
        "retained_references_changed": False, "all_real_failures_fixed": False,
        "limitation": fixture["limitation"],
    }


def verifier_only_export(records, batch, requests, responses):
    """Supplied-response evidence replay, not another inference or budget gate."""
    store = ReproductionStore(copy.deepcopy(records))
    ledger_before = store.read_bytes(real_pilot.BUDGET_KEY)
    results = []
    for item in batch["items"]:
        selected = [request for request in requests if request["item_key"] == item["item_key"]]
        if not selected:
            continue
        combined = None
        for request in selected:
            evidence = [Evidence.model_validate(entry) for entry in request["original_user"]["evidence"]]
            manifest = Manifest.model_validate(item["manifest"])
            source_ids = list(dict.fromkeys(entry.source_id for entry in evidence))
            manifest.source_ids = source_ids
            sources = [
                OfflineSource(
                    source_id=source_id, product=manifest.product, source_tier=request["source_tier"],
                    excerpts=[entry for entry in evidence if entry.source_id == source_id],
                ) for source_id in source_ids
            ]
            response = map_citations(responses[(item["item_key"], request["source_tier"])], request["references"])
            result = extract.run_enrichment(
                OfflineBundle(manifest=manifest, sources=sources, generated_response=response),
                qualified=True,
            )
            assert result.execution_mode == "offline_replay" and result.model_call_status == "not_attempted"
            assert result.candidate_source == "supplied_response"
            if combined is None:
                combined = result
                continue
            combined.evidence.extend(result.evidence)
            combined.retrieval.extend(result.retrieval)
            combined.validation_diagnostics.extend(result.validation_diagnostics)
            if result.extraction_error:
                combined.extraction_error, combined.failure = result.extraction_error, result.failure
            current = {attribute.attribute_id: attribute for attribute in result.attributes}
            for target in combined.attributes:
                other = current[target.attribute_id]
                target.candidates.extend(other.candidates)
                target.verification.extend(other.verification)
                values = {(type(value.value).__name__, value.value, value.unit) for value in target.candidates}
                if values:
                    target.status = "conflict" if len(values) > 1 else "proposed"
                elif other.status in {"extraction_failed", "retrieval_failed"}:
                    target.status = other.status
        result_key = f"results/{batch['id']}/{item['item_key']}/attempts/{sha(b'VERIFIER-ONLY-REPRODUCTION-NOT-MODEL-CALL')}.json"
        combined.manifest = Manifest.model_validate(item["manifest"])
        write_json(store, result_key, combined.model_dump(mode="json"))
        state_key = f"items/{batch['id']}/{item['item_key']}.json"
        previous = read_json(store, state_key)[0]
        replace(store, state_key, {
            **previous, "previous_attempt": previous, "result_key": result_key, "state": "unresolved",
            "error": "VERIFIER-ONLY REPRODUCTION: supplied responses, no model requests or capacity acceptance.",
        })
        proposals = [{
            "attribute_id": attribute.attribute_id, "candidate": candidate.model_dump(mode="json"),
            "verification": attribute.verification[index].model_dump(mode="json"),
        } for attribute in combined.attributes for index, candidate in enumerate(attribute.candidates)]
        results.append({
            "item_key": item["item_key"], "label": "VERIFIER-ONLY REPRODUCTION", "proposals": proposals,
            "unresolved_attributes": [attribute.attribute_id for attribute in combined.attributes
                                      if attribute.status not in {"existing", "proposed"}],
        })
    exported = BatchService(store).export(batch["id"], batch["owner"])
    rows = read_workbook(exported)["Results"]
    assert "Evidence verification JSON" in rows[0]
    expected_proposals = sum(len(result["proposals"]) for result in results)
    assert sum(bool(row["Evidence verification JSON"]) for row in rows) == expected_proposals
    assert store.read_bytes(real_pilot.BUDGET_KEY) == ledger_before
    return exported, results


def test_retained_shape_does_not_accept_changed_primitives_or_references():
    response = ExtractionResponse.model_validate({"candidates": [{
        "attribute_id": "Enabled", "value": True, "evidence_ids": ["E1"],
        "supporting_quote": "Enabled: yes", "qualification": "REPRODUCTION", "confidence": 0.5,
    }]})
    shape = {key: {"redacted": True, "type": type(value).__name__}
             for key, value in response.candidates[0].model_dump().items()}
    shape["evidence_ids"] = [{"value": "E1", "redacted": False}]
    diagnostic = {"parsed_response": {"candidates": [shape]}}
    assert_retained_shape(response, diagnostic)
    ambiguity = redacted_value_ambiguity(response, diagnostic)
    assert ambiguity["examples"][0]["structurally_compatible_alternatives"] == [False, True]
    for changed in ({"value": "yes"}, {"evidence_ids": ["E2"]}):
        altered = response.model_copy(deep=True)
        altered.candidates[0] = altered.candidates[0].model_copy(update=changed)
        with pytest.raises(AssertionError):
            assert_retained_shape(altered, diagnostic)


def test_exact_private_reconstruction_and_production_gate(private_inputs, monkeypatch):
    tested_source_sha256 = code_fingerprints()
    inputs = private_inputs
    reconciled, reconciliation = reconcile_private(inputs, monkeypatch)
    store, batch, approval = hydrate(inputs, reconciled)
    responses, reproduction = reconstruct(inputs, batch)
    counterfactual = counterfactual_normalization(inputs, batch)
    consumed = copy.deepcopy(inputs["after"]["ledger"])
    assert read_json(store, real_pilot.BUDGET_KEY)[0] == consumed
    remaining = {
        "inference": approval["limits"]["inference"] - consumed["attempted"]["inference"],
        **{name: approval["limits"][name] - consumed["reserved"][name]
           for name in ("input_tokens", "output_tokens")},
    }
    assert remaining == inputs["responses"]["expected_remaining"]
    for key, value in approval["environment"].items():
        monkeypatch.setenv(key, value)
        if hasattr(settings, key):
            monkeypatch.setattr(settings, key, value)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "true")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", approval["approved_by"])
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID", approval["identities"]["worker_principal_id"])
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_EXECUTION_SCOPE", "internal_only")
    amendment = inputs["after"]["final_audit"]["amendment"]
    simulated_time = datetime.fromisoformat(amendment["not_before"]) + timedelta(minutes=5)
    monkeypatch.setattr(real_pilot, "_now", lambda: simulated_time)
    guard = real_pilot.RealPilotGuard(store, batch)
    # OFFLINE SIMULATION ONLY: reuse the already consumed execution identity.
    # Do not grant a new execution, modify authority, or replace reserve/_fresh_ledger.
    guard._execution_id = consumed["final_rerun"]["execution_id"]
    before = all_records(store)
    requests, completions = [], []
    original_compact = worker.compact_inference_prompt
    active = {"item": None}

    def capture_request(user):
        compact, references = original_compact(user)
        payload = json.loads(user)
        tier = payload["evidence"][0]["source_tier"]
        capture = next(entry for entry in inputs["prompts"]
                       if entry["item_key"] == active["item"] and entry["user"]["evidence"][0]["source_tier"] == tier)
        assert stable(payload) == stable(capture["user"]), "Complete production evidence or attributes changed"
        schema = ExtractionResponse.model_json_schema()
        assert schema == capture["schema"], "Model response schema must remain unchanged"
        system = extract.product_extraction_system_message + worker.COMPACT_PROMPT_INSTRUCTIONS
        requests.append({
            "item_key": active["item"], "source_tier": tier, "system": system, "user": json.loads(compact),
            "original_user": payload,
            "schema": schema, "references": references,
            "input_tokens": len(system.encode()) + len(compact.encode()) + len(json.dumps(schema).encode()) + 4096,
            "output_tokens": 2048, "framing_reserve": 4096,
            "system_sha256": sha(system.encode()), "user_sha256": sha(compact.encode()),
        })
        return compact, references

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
            request = requests[-1]
            assert system == request["system"] and json.loads(user) == request["user"]
            assert kwargs["max_completion_tokens"] == request["output_tokens"]
            response = responses[(request["item_key"], request["source_tier"])]
            self.last_response_sha256 = sha(response.model_dump_json().encode())
            completions.append({key: request[key] for key in ("item_key", "source_tier", "input_tokens", "output_tokens")})
            return response.model_copy(deep=True)

    monkeypatch.setattr(worker, "compact_inference_prompt", capture_request)
    monkeypatch.setattr(worker, "LLMClient", Client)
    monkeypatch.setattr(worker, "ManagedIdentityCredential", lambda **kwargs: Mock())
    results = []
    for item in batch["items"]:
        if item["item_key"] not in amendment["selected_item_keys"]:
            continue
        active["item"] = item["item_key"]
        processor = worker.RealBatchProcessor(store, batch, guard)
        result, state = processor(item, "real_pilot")
        assert not any(entry.get("parsing") == "fresh_analysis" for entry in state["provenance"])
        assert any(entry.get("parsing") == "cache" for entry in state["provenance"])
        result_key = f"results/{batch['id']}/{item['item_key']}/attempts/{sha(b'OFFLINE-REPRODUCTION-NOT-APPROVAL')}.json"
        write_json(store, result_key, result.model_dump(mode="json"))
        state_key = f"items/{batch['id']}/{item['item_key']}.json"
        previous = read_json(store, state_key)[0]
        replace(store, state_key, {
            **previous, **state, "previous_attempt": previous, "result_key": result_key,
            "finished_at": simulated_time.isoformat(),
        })
        results.append({
            "item_key": item["item_key"], "machine_result": result.model_dump(mode="json"), **state,
        })
    assert len(requests) == len(inputs["prompts"]) == 4
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    for group in ("executions", "reservations"):
        assert all(ledger[group][key] == value for key, value in consumed[group].items())
    assert ledger["executions"] == consumed["executions"]
    assert ledger["attempted"]["analysis"] == consumed["attempted"]["analysis"]
    assert ledger["reserved"]["analysis_pages"] == consumed["reserved"]["analysis_pages"]
    for key, value in before.items():
        if key.startswith(("parses/", "results/", "operations/", "response-diagnostics/", "configuration/")):
            assert store.read_bytes(key) == value
    reservations = [value for key, value in ledger["reservations"].items() if key not in consumed["reservations"]]
    assert len(reservations) == len(completions)
    assert sum(value["reserved_usage"]["input_tokens"] for value in reservations) == sum(
        value["input_tokens"] for value in completions
    )
    assert sum(value["reserved_usage"]["output_tokens"] for value in reservations) == sum(
        value["output_tokens"] for value in completions
    )
    requested = {name: sum(entry[name] for entry in requests) for name in ("input_tokens", "output_tokens")}
    requested["inference"] = len(requests)
    requested["spend_microdollars"] = sum(guard._cost(
        approval, "inference", entry["input_tokens"], entry["output_tokens"], 0,
    ) for entry in requests)
    assert all(entry["input_tokens"] + entry["output_tokens"] <= 30000 for entry in requests)
    decision = "GO" if len(completions) == len(requests) else "NO_GO"
    assert decision == inputs["responses"]["expected_budget_decision"]
    if requested["input_tokens"] > remaining["input_tokens"]:
        assert decision == "NO_GO"
    service = BatchService(store)
    exported = service.export(batch["id"], batch["owner"])
    workbook = read_workbook(exported)
    assert {"Results", "Evidence", "Diagnostics", "Attempts", "Provenance"} <= set(workbook)
    assert len(workbook["Attempts"]) >= 4
    for row in results:
        assert service.detail(batch["id"], row["item_key"], batch["owner"])["machine_result"] == row["machine_result"]
    with pytest.raises(Missing):
        service.export(batch["id"], "unrelated-owner")
    verifier_export, verifier_results = verifier_only_export(before, batch, requests, responses)
    assert code_fingerprints() == tested_source_sha256, "Source changed during the private gate"
    prefix = os.environ.get("DOCINTEL_TEST_PRIVATE_OUTPUT")
    if prefix:
        assert re.fullmatch(r"[A-Za-z0-9_-]+", prefix)
        artifacts = [
            private_json(inputs["root"], prefix + "-reproduction.json", reproduction),
            private_json(inputs["root"], prefix + "-counterfactual-normalization.json", counterfactual),
            private_json(inputs["root"], prefix + "-payloads.json", requests),
            private_write(inputs["root"], prefix + "-export.xlsx", exported),
            private_write(inputs["root"], prefix + "-verifier-only-export.xlsx", verifier_export),
        ]
        workspace = Path(__file__).resolve().parents[1]
        report = {
            "label": "REPRODUCTION", "decision": decision, "live_calls": 0, "product_findings": False,
            "claim_all_real_failures_fixed": "NO_GO",
            "counterfactual_normalization_controls_passed": len(counterfactual["cases"]),
            "offline_simulation": {
                "execution_start_bypassed": True, "reused_consumed_execution": guard._execution_id,
                "clock_frozen_inside_existing_window": simulated_time.isoformat(),
                "reserve_mocked": False, "allowances_reset": False, "new_approval": False,
                "new_execution_exception": False, "cached_parsing_bypassed": False,
                "scope_reduced": False, "evidence_pruned": False,
            },
            "source_hashes": inputs["hashes"], "prior_ledger_sha256": real_pilot._sha256(consumed),
            "isolated_stale_reconciliation": reconciliation,
            "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip(),
            "tested_source_sha256": tested_source_sha256,
            "source_binding": "Source SHA256 values bind the tested working tree; source_revision is its base HEAD, not release authorization.",
            "remaining": remaining, "complete_request_requirements": requested,
            "remaining_spend_microdollars": approval["limits"]["spend_microdollars"] - consumed["reserved"]["microdollars"],
            "minimum_additional_input_for_four_requests": max(requested["input_tokens"] - remaining["input_tokens"], 0),
            "remaining_current_final_rerun_slots": consumed["final_rerun"]["inference_before"] + 4 - consumed["attempted"]["inference"],
            "smaller_scope": [{
                "item_key": key, "complete_requests": 2,
                "input_tokens": sum(entry["input_tokens"] for entry in requests if entry["item_key"] == key),
                "output_tokens": sum(entry["output_tokens"] for entry in requests if entry["item_key"] == key),
                "minimum_additional_input": max(sum(entry["input_tokens"] for entry in requests
                                                    if entry["item_key"] == key) - remaining["input_tokens"], 0),
            } for key in amendment["selected_item_keys"]],
            "reservations": reservations, "simulated_completions": completions, "results": results,
            "verifier_only_reproduction": {
                "no_model_calls": True, "not_budget_acceptance": True, "ledger_unchanged": True,
                "results": verifier_results,
            },
            "actual_consumed_ledger_unchanged": True, "isolated_resulting_ledger": ledger,
            "history_preserved": True, "owner_export_isolated": True, "artifacts": artifacts,
            "limitation": "NO_GO is not authorization to reset allowances, extend the expired execution window, "
                          "or create another execution. Direct verifier comparisons are not budget-approved model calls.",
        }
        receipt = private_json(inputs["root"], prefix + "-gate.json", report)
        print(json.dumps({"decision": decision, "receipt": receipt, "artifacts": artifacts}))
