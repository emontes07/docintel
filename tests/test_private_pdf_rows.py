"""Private PDF-row acceptance is opt-in, network-free, and never live authority.

Set DOCINTEL_TEST_PDF_ROWS_WORK and DOCINTEL_TEST_PDF_ROWS_DOCUMENTS to existing
private inputs. DOCINTEL_TEST_PDF_ROWS_OUTPUT optionally names a new artifact
prefix inside WORK. DOCINTEL_TEST_PDF_ROWS_REQUIRE_COMMITTED=true binds a clean
committed backend/gate. Customer-specific recipes are private JSON, not literals.
"""

import ast
import base64
import copy
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urldefrag

import pytest

from backend import batch_worker as worker, extract, real_pilot
from backend.batch import BatchService
from backend.batch_store import Missing, SQLiteStore, read_json, write_json
from backend.core.config import settings
from backend.models.enrichment import Evidence, ExtractionResponse, Manifest
from backend.multisource import run_cascade
from backend.pdf_presentation import pdf_items
from backend.response_validation import ResponseValidationError, map_citations
from backend.workbooks import read_workbook
from tests.test_private_verification import reconcile_private


BASELINE = "5785c4c51b2df861e4ffbc5feb6817fd4580c6e1"
SOURCE_FILES = (
    "backend/pdf_presentation.py", "backend/batch_worker.py", "backend/extract.py",
    "backend/evidence_verification.py", "backend/response_validation.py",
    "backend/core/instructions.py", "backend/models/enrichment.py",
    "backend/multisource.py", "backend/real_pilot.py", "backend/batch.py",
    "backend/batch_store.py", "backend/interrupted_reconciliation.py",
    "tests/test_private_pdf_rows.py", "tests/test_private_verification.py",
    "tests/test_interrupted_reconciliation.py", "scripts/final_continuation.py",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def stable(value):
    if isinstance(value, dict):
        return {key: stable(child) for key, child in value.items()
                if key not in {"observed_at", "provider_retrieved_at"}}
    if isinstance(value, list):
        return [stable(child) for child in value]
    return value


class MemoryStore(SQLiteStore):
    def __init__(self, records=None):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE records (key TEXT PRIMARY KEY, value BLOB NOT NULL, version TEXT NOT NULL)")
        for key, (raw, _) in (records or {}).items():
            self.write_bytes(key, raw)

    def connect(self):
        return self.connection

    def snapshot(self):
        return {key: self.read_bytes(key) for key in self.keys("") if not key.startswith("locks/")}


def put(store, key, value):
    try:
        _, version = store.read_bytes(key)
    except Missing:
        version = None
    write_json(store, key, value, version)


def write_private(root, filename, content):
    assert Path(filename).name == filename
    raw = content if isinstance(content, bytes) else json.dumps(content, indent=2, ensure_ascii=False).encode()
    path = root / filename
    descriptor = os.open(os.path.relpath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
    assert path.stat().st_mode & 0o777 == 0o600
    return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("PDF-row tests cannot contact services or perform fresh analysis")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(worker, "DocumentIntelligenceService", forbidden)
    monkeypatch.setattr(worker, "ManagedIdentityCredential", lambda **kwargs: None)
    monkeypatch.setattr(worker, "get_bearer_token_provider", lambda *args: None)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient", forbidden)
    monkeypatch.setattr("backend.core.websearch.fetch_original_page", forbidden)
    monkeypatch.setattr("backend.core.pilot_sources.retrieve_document", forbidden)
    clock = [0.0]
    monkeypatch.setattr(worker, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))


def synthetic_evidence():
    return [
        Evidence(
            evidence_id=f"synthetic:{row}:{column}", source_id="synthetic",
            source_locator=f"fixture.pdf#page=1&table=0&row={row}&column={column}",
            source_version="synthetic-v1", source_tier="internal_pdf", content_kind="source_excerpt",
            text=text, observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        for row, cells in enumerate((
            ("No.", "DESCRIPTION", "MATERIAL"),
            ("7", "BODY", "demonstration alloy"),
        ))
        for column, text in enumerate(cells)
    ]


def test_row_projection_expands_headers_without_changing_originals():
    evidence = synthetic_evidence()
    before = [entry.model_dump() for entry in evidence]
    items = pdf_items(evidence)
    assert len(items) == 1 and items[0].kind == "table_row"
    assert {entry.evidence_id for entry in items[0].originals} == {entry.evidence_id for entry in evidence}
    assert [entry.text for entry in items[0].values] == ["BODY", "demonstration alloy"]
    user, references = worker.compact_inference_prompt(json.dumps({
        "attributes": [], "evidence": [entry.model_dump(mode="json") for entry in evidence],
    }))
    compact = json.loads(user)
    assert compact["prompt_format"] == "real-evidence-rows-v3"
    assert len(compact["evidence"][0]) == len(compact["evidence_columns"]) == 5
    response = ExtractionResponse.model_validate({"candidates": [{
        "attribute_id": "Primary Material", "value": "demonstration alloy", "evidence_ids": [" e1 "],
        "supporting_quote": items[0].text, "qualification": "SYNTHETIC REPRODUCTION",
    }]})
    mapped = map_citations(response, references)
    assert set(mapped.candidates[0].evidence_ids) == {entry.evidence_id for entry in evidence}
    assert [entry.model_dump() for entry in evidence] == before


def test_row_quote_requires_all_originals_and_part_numbers_are_not_values():
    evidence = synthetic_evidence()
    item = pdf_items(evidence)[0]
    manifest = Manifest.model_validate({
        "product": {"item_id": "synthetic", "vendor": "synthetic", "mpn": "synthetic",
                    "hierarchy_node": "synthetic"},
        "attributes": [{"attribute_id": "Primary Material", "description": "Synthetic material",
                        "value_type": "string", "unit": None}],
        "source_ids": ["synthetic"],
    })
    response = ExtractionResponse.model_validate({"candidates": [{
        "attribute_id": "Primary Material", "value": "demonstration alloy",
        "evidence_ids": [entry.evidence_id for entry in item.originals],
        "supporting_quote": item.text, "qualification": "SYNTHETIC REPRODUCTION",
    }]})
    assert extract.validate_response(response, manifest, evidence)[0].quote.method == "reconstructed_row"
    incomplete = response.model_copy(deep=True)
    incomplete.candidates[0].evidence_ids.pop(0)
    with pytest.raises(ResponseValidationError):
        extract.validate_response(incomplete, manifest, evidence)
    metadata_value = response.model_copy(deep=True)
    metadata_value.candidates[0].value = "7"
    with pytest.raises(ResponseValidationError) as caught:
        extract.validate_response(metadata_value, manifest, evidence)
    assert caught.value.issues[0].field_path.endswith(".value")


@pytest.fixture
def private_inputs():
    work = os.environ.get("DOCINTEL_TEST_PDF_ROWS_WORK")
    documents = os.environ.get("DOCINTEL_TEST_PDF_ROWS_DOCUMENTS")
    if not work and not documents:
        pytest.skip("Private PDF-row snapshots, recipes and approved local documents are opt-in")
    assert work and documents, "Partial private configuration must fail rather than skip"
    root = Path(work).resolve()
    workspace = Path(__file__).resolve().parents[1]
    assert root.is_dir() and not Path(work).is_symlink() and not root.is_relative_to(workspace)
    fixture_name = "pdf-row-presentation-fixture.json"
    fixture_raw = (root / fixture_name).read_bytes()
    fixture = json.loads(fixture_raw)
    assert fixture["label"].startswith("REPRODUCTION") and fixture["recovered_model_output"] is False
    raw = {name: (root / name).read_bytes() for name in fixture["source_sha256"]}
    assert {name: sha(value) for name, value in raw.items()} == fixture["source_sha256"]
    stopped = {
        "execution": "final-postclosure-execution.json",
        "worker": "final-deployed-images-and-stopped-worker.json",
        "timing": "final-worker-timing-observations.json",
    }
    raw.update({name: (root / name).read_bytes() for name in stopped.values()})
    result = {
        "root": root, "documents": Path(documents), "fixture": fixture,
        "source_hashes": {**{name: sha(value) for name, value in raw.items()}, fixture_name: sha(fixture_raw)},
        "before": json.loads(raw["continuation-final-private-snapshot.json"]),
        "after": json.loads(raw["final-postclosure-private-snapshot.json"]),
        "captures": json.loads(raw["mueller-readiness-26c878d-budget/private-prompts.json"]),
        "diagnostics": json.loads(raw["final-retained-validation-diagnostics.json"]),
        "legacy_responses": json.loads(raw["verification-reproduction-responses.json"]),
        **{key: json.loads(raw[name]) for key, name in stopped.items()},
    }
    yield result
    assert (root / fixture_name).read_bytes() == fixture_raw
    assert all((root / name).read_bytes() == value for name, value in raw.items())


def hydrate(inputs):
    store = MemoryStore()
    for key, value in inputs["before"]["records"].items():
        raw = base64.b64decode(value["base64"], validate=True)
        assert sha(raw) == value["sha256"]
        store.write_bytes(key, raw)
    after = inputs["after"]
    approval = read_json(store, real_pilot.APPROVAL_KEY)[0]
    old_batch = read_json(store, f"batches/{approval['batch_id']}.json")[0]
    batch = after["batch"]
    assert real_pilot.binding_digest(batch) == real_pilot.binding_digest(old_batch)
    put(store, f"batches/{batch['id']}.json", batch)
    put(store, real_pilot.BUDGET_KEY, after["ledger"])
    put(store, real_pilot.FINAL_RERUN_KEY, after["final_audit"]["amendment"])
    put(store, real_pilot.FINAL_RERUN_AUDIT_KEY, after["final_audit"])
    derived = {"attempt_history", "machine_result", "machine_sha256", "reviewed_result",
               "reviewer_identity", "original", "row", "validation_diagnostics"}
    for item_key, detail in after["details"].items():
        put(store, f"items/{batch['id']}/{item_key}.json", {
            key: value for key, value in detail.items() if key not in derived
        })
        machines = {entry["result_key"]: entry["machine_result"] for entry in detail.get("attempt_history", [])
                    if entry["machine_result"] is not None}
        if detail.get("machine_result") is not None:
            machines[detail["result_key"]] = detail["machine_result"]
        for key, machine in machines.items():
            if key in store.keys("results/"):
                assert read_json(store, key)[0] == machine
            else:
                put(store, key, machine)
        for index, diagnostic in enumerate(detail.get("validation_diagnostics", [])):
            put(store, f"response-diagnostics/{batch['id']}/{item_key}/retained-{index}.json", diagnostic)
    selected = after["final_audit"]["amendment"]["selected_item_keys"]
    needed = {source["sha256"]: source["blob"] for item in batch["items"] if item["item_key"] in selected
              for source in item["sources"] if source["kind"] == "blob"}
    for path in inputs["documents"].iterdir():
        if path.is_file() and path.suffix.lower() in {".pdf", ".xlsx"}:
            raw = path.read_bytes()
            if sha(raw) in needed and needed[sha(raw)] not in store.keys(""):
                store.write_bytes(needed[sha(raw)], raw)
    assert all(key in store.keys("") for key in needed.values()), "Approved hash-bound local copies are required"
    return store, batch, approval


def configure_offline(monkeypatch, approval, amendment):
    for name, value in approval["environment"].items():
        monkeypatch.setenv(name, value)
        if hasattr(settings, name):
            monkeypatch.setattr(settings, name, value)
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_ENABLED", "true")
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_OPERATOR_IDS", approval["approved_by"])
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_WORKER_PRINCIPAL_ID", approval["identities"]["worker_principal_id"])
    monkeypatch.setenv("DOCINTEL_REAL_PILOT_EXECUTION_SCOPE", "internal_only")
    simulated_time = datetime.fromisoformat(amendment["not_before"]) + timedelta(minutes=5)
    monkeypatch.setattr(real_pilot, "_now", lambda: simulated_time)
    return simulated_time


def expanded(value):
    return [value] if isinstance(value, str) else value


def position(entry):
    return {key: int(value[0]) for key, value in parse_qs(urldefrag(entry.source_locator)[1]).items()
            if len(value) == 1 and value[0].isdigit()}


def response_for(payload, references, fixture):
    evidence = [Evidence.model_validate(entry) for entry in payload["evidence"]]
    tier = evidence[0].source_tier
    pending = {entry["attribute_id"] for entry in payload["attributes"]}
    candidates = []
    projected = pdf_items(evidence) if tier == "internal_pdf" else []
    recipes = fixture["pdf_recipes"] if tier == "internal_pdf" else fixture["vendor_recipes"]
    for recipe in recipes:
        if recipe["attribute_id"] not in pending:
            continue
        if tier == "internal_pdf":
            selector = recipe["selector"]
            item = next(item for item in projected if item.kind == selector["kind"] and all(
                position(item.anchor).get(key) == value for key, value in selector.items() if key != "kind"
            ))
            original_ids = [entry.evidence_id for entry in item.originals]
            quote = item.text if recipe["quote"] == "presentation_text" else recipe["quote"]
        else:
            assert len(evidence) == 1
            original_ids = [evidence[0].evidence_id]
            cell = next(cell for cell in json.loads(evidence[0].text)["cells"] if cell["column"] == recipe["column"])
            quote = cell["value"]
        reference = next(key for key, value in references.items() if expanded(value) == original_ids)
        candidates.append({
            "attribute_id": recipe["attribute_id"], "value": recipe["value"], "unit": None,
            "evidence_ids": [reference], "supporting_quote": quote, "qualification": recipe["qualification"],
        })
    return ExtractionResponse.model_validate({"candidates": candidates})


def request_record(item_key, system, user, schema, inputs, compactor):
    original = json.loads(user)
    tier = original["evidence"][0]["source_tier"]
    captured = next(entry for entry in inputs["captures"] if entry["item_key"] == item_key
                    and entry["user"]["evidence"][0]["source_tier"] == tier)["user"]
    assert stable(original["evidence"]) == stable(captured["evidence"]), "Original evidence must remain complete"
    pending = {entry["attribute_id"] for entry in original["attributes"]}
    assert original["attributes"] == [entry for entry in captured["attributes"] if entry["attribute_id"] in pending]
    assert original["product"] == captured["product"]
    compact, references = compactor(user)
    payload = json.loads(compact)
    assert payload["prompt_format"] == "real-evidence-rows-v3"
    assert payload["evidence_columns"][-1] == "presentation" and all(len(row) == 5 for row in payload["evidence"])
    full_system = system + worker.COMPACT_PROMPT_INSTRUCTIONS
    schema_json = schema.model_json_schema()
    expected_schema = next(entry["schema"] for entry in inputs["captures"] if entry["item_key"] == item_key)
    assert schema_json == expected_schema, "Model response schema must remain unchanged"
    parts = {
        "system_bytes": len(full_system.encode()), "user_bytes": len(compact.encode()),
        "schema_bytes": len(json.dumps(schema_json).encode()), "framing_reserve": 4096,
        "output_reserve": 2048,
    }
    evidence = [Evidence.model_validate(entry) for entry in original["evidence"]]
    projection = pdf_items(evidence)
    return {
        "item_key": item_key, "source_tier": tier, "parts": parts,
        "input_tokens": sum(parts[key] for key in ("system_bytes", "user_bytes", "schema_bytes", "framing_reserve")),
        "output_tokens": parts["output_reserve"],
        "definition_bytes": len(json.dumps(original["attributes"], ensure_ascii=False, separators=(",", ":")).encode()),
        "attribute_ids": [entry["attribute_id"] for entry in original["attributes"]],
        "original_evidence_count": len(evidence), "presentation_count": len(payload["evidence"]),
        "table_row_count": sum(item.kind == "table_row" for item in projection),
        "paragraph_count": sum(item.kind == "paragraph" for item in projection),
        "context_only_count": sum(item.context_only for item in projection),
        "system": full_system, "user": payload, "schema": schema_json, "references": references,
        "original_payload": original,
    }


def cached_loader(processor, item, provenance):
    def load(tier, pending):
        sources = []
        for binding in item["sources"]:
            if binding["source_tier"] != tier or not processor.internal_copy(binding):
                continue
            scope = next(scope for scope in binding["applicability"] if scope["product"] == item["manifest"]["product"])
            if not set(scope["attribute_ids"]) & set(pending):
                continue
            entry = {"source_id": binding["source_id"], "source_tier": tier}
            sources.append(processor.real_document(binding, item, scope, entry))
            provenance.append(entry)
        return sources
    return load


def item_summary(result):
    return {
        "proposed_attributes": [entry.attribute_id for entry in result.attributes if entry.status == "proposed"],
        "conflicts": [entry.attribute_id for entry in result.attributes if entry.status == "conflict"],
        "candidate_count": sum(len(entry.candidates) for entry in result.attributes),
        "proposals": [
            {"attribute_id": entry.attribute_id, "status": entry.status,
             "candidate": candidate.model_dump(mode="json"),
             "verification": entry.verification[index].model_dump(mode="json")}
            for entry in result.attributes for index, candidate in enumerate(entry.candidates)
        ],
        "unresolved_attributes": [entry.attribute_id for entry in result.attributes
                                  if entry.status not in {"existing", "proposed"}],
    }


def persist_replay(store, batch, item, result, label, provenance):
    result = result.model_copy(update={
        "execution_mode": "offline_replay", "candidate_source": "supplied_response",
        "model_call_status": "not_attempted",
    })
    result_key = f"results/{batch['id']}/{item['item_key']}/attempts/{sha(label.encode())}.json"
    put(store, result_key, result.model_dump(mode="json"))
    state_key = f"items/{batch['id']}/{item['item_key']}.json"
    previous = read_json(store, state_key)[0]
    put(store, state_key, {
        **previous, "previous_attempt": previous, "result_key": result_key, "state": "unresolved",
        "error": label + ": supplied responses only, never recovered output or execution authority.",
        "provenance": provenance,
    })
    return result


def verify_exports(store, batch, inputs):
    workbook = BatchService(store).export(batch["id"], batch["owner"])
    sheets = read_workbook(workbook)
    for item in batch["items"]:
        captures = [entry for entry in inputs["captures"] if entry["item_key"] == item["item_key"]]
        if not captures:
            continue
        expected = {(entry["evidence_id"], entry["source_locator"])
                    for capture in captures for entry in capture["user"]["evidence"]}
        actual = {(entry["Evidence ID"], entry["Locator"]) for entry in sheets["Evidence"]
                  if str(entry["Row"]) == str(item["row"])}
        assert actual == expected, "Every original evidence ID/export locator must survive presentation"
        result = BatchService(store).detail(batch["id"], item["item_key"], batch["owner"])["machine_result"]
        originals = [entry for capture in captures for entry in capture["user"]["evidence"]]
        assert stable(result["evidence"]) == stable(originals)
    with pytest.raises(Missing):
        BatchService(store).export(batch["id"], "unrelated-owner")
    return workbook


def legacy_header_negatives(inputs, batch):
    source = subprocess.check_output(["git", "show", f"{BASELINE}:backend/batch_worker.py"], text=True)
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                and node.name == "compact_inference_prompt")
    namespace = {"json": json, "os": os, "COMPACT_PROMPT_FORMAT": "real-evidence-groups-v2"}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<immutable-v2-compaction>", "exec"), namespace)
    results = []
    for capture in inputs["captures"]:
        if capture["user"]["evidence"][0]["source_tier"] != "internal_pdf":
            continue
        _, old_references = namespace["compact_inference_prompt"](json.dumps(capture["user"]))
        response = ExtractionResponse.model_validate(
            inputs["legacy_responses"]["requests"][capture["item_key"]]["internal_pdf"],
        )
        mapped = map_citations(response, old_references)
        item = next(item for item in batch["items"] if item["item_key"] == capture["item_key"])
        evidence = [Evidence.model_validate(entry) for entry in capture["user"]["evidence"]]
        failures = []
        for index, candidate in enumerate(mapped.candidates):
            with pytest.raises(ResponseValidationError) as caught:
                extract.validate_response(ExtractionResponse(candidates=[candidate]), Manifest.model_validate(item["manifest"]), evidence)
            assert caught.value.issues[0].field_path.endswith(".supporting_quote")
            failures.append({"candidate_index": index, "issues": [issue.model_dump() for issue in caught.value.issues]})
        references = list(dict.fromkeys(reference for candidate in response.candidates for reference in candidate.evidence_ids))
        results.append({
            "item_key": item["item_key"], "legacy_format": "real-evidence-groups-v2",
            "interpretation": "Original v2 row references, never reinterpreted as new v3 E1/E2.",
            "references": {reference: old_references[reference] for reference in references},
            "full_original_fragments": [entry.model_dump(mode="json") for entry in evidence
                                        if entry.evidence_id in {old_references[key] for key in references}],
            "rejected_representatives": failures,
        })
    assert sum(len(entry["rejected_representatives"]) for entry in results) == 14
    return {"baseline": BASELINE, "source_sha256": sha(source.encode()), "cases": results}


def test_private_pdf_row_presentation_cascade_and_unchanged_capacity(private_inputs, monkeypatch):
    inputs, fixture = private_inputs, private_inputs["fixture"]
    workspace = Path(__file__).resolve().parents[1]
    sources = {name: sha((workspace / name).read_bytes()) for name in SOURCE_FILES}
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if os.environ.get("DOCINTEL_TEST_PDF_ROWS_REQUIRE_COMMITTED") == "true":
        paths = list(SOURCE_FILES)
        subprocess.check_output(["git", "ls-files", "--error-unmatch", "--", *paths])
        subprocess.check_call(["git", "diff", "--quiet", "HEAD"])
        assert not subprocess.check_output(["git", "status", "--porcelain", "--", "backend", *paths], text=True).strip()
    original, batch, approval = hydrate(inputs)
    reconciled, reconciliation = reconcile_private(inputs, monkeypatch)
    for key, (raw, _) in reconciled.items():
        if key in reconciliation["changed_existing_keys"] or key.startswith("operations/interrupted-reconciliation/"):
            put(original, key, json.loads(raw))
    batch = read_json(original, f"batches/{batch['id']}.json")[0]
    baseline = original.snapshot()
    consumed = copy.deepcopy(inputs["after"]["ledger"])
    assert read_json(original, real_pilot.BUDGET_KEY)[0] == consumed
    remaining = {
        "inference": approval["limits"]["inference"] - consumed["attempted"]["inference"],
        **{name: approval["limits"][name] - consumed["reserved"][name] for name in ("input_tokens", "output_tokens")},
    }
    assert remaining == fixture["expected_remaining"]
    amendment = inputs["after"]["final_audit"]["amendment"]
    simulated_time = configure_offline(monkeypatch, approval, amendment)
    selected = [item for item in batch["items"] if item["item_key"] in amendment["selected_item_keys"]]
    replay = MemoryStore(baseline)
    replay_ledger_before = replay.read_bytes(real_pilot.BUDGET_KEY)
    replay_guard = real_pilot.RealPilotGuard(replay, batch)
    planned, replay_results, pdf_results = [], [], []
    compactor = worker.compact_inference_prompt
    for item in selected:
        processor = worker.RealBatchProcessor(replay, batch, replay_guard)
        provenance = []
        load = cached_loader(processor, item, provenance)

        class SuppliedCompletion:
            def complete_structured(self, system, user, schema):
                request = request_record(item["item_key"], system, user, schema, inputs, compactor)
                planned.append(request)
                response = response_for(request["original_payload"], request["references"], fixture)
                mapped = map_citations(response, request["references"])
                if request["source_tier"] == "internal_pdf":
                    assert len(response.candidates) == fixture["expected_pdf_proposals"]
                    assert not set(fixture["pdf_withheld_attributes"]) & {entry.attribute_id for entry in response.candidates}
                    checks = extract.validate_response(
                        mapped, Manifest.model_validate(item["manifest"]),
                        [Evidence.model_validate(entry) for entry in request["original_payload"]["evidence"]],
                    )
                    pdf_results.append({
                        "item_key": item["item_key"], "label": "REPRODUCTION: supplied correctly cited PDF recipes",
                        "response": response.model_dump(mode="json"), "mapped_response": mapped.model_dump(mode="json"),
                        "verification": [check.model_dump(mode="json") for check in checks],
                    })
                return mapped

        result = run_cascade(Manifest.model_validate(item["manifest"]), load, SuppliedCompletion())
        assert not result.validation_diagnostics and result.extraction_error is None
        for definition in Manifest.model_validate(item["manifest"]).attributes:
            if not definition.unit_resolved:
                assert next(attribute for attribute in result.attributes if attribute.attribute_id == definition.attribute_id).status == "definition_clarification_needed"
        pdf_names = {recipe["attribute_id"] for recipe in fixture["pdf_recipes"]}
        pdf_attributes = [attribute for attribute in result.attributes if attribute.attribute_id in pdf_names]
        assert len(pdf_attributes) == fixture["expected_pdf_attributes"]
        assert sum(attribute.status == "proposed" for attribute in pdf_attributes) == fixture["expected_pdf_proposed_attributes"]
        assert [attribute.attribute_id for attribute in pdf_attributes if attribute.status == "conflict"] == fixture["expected_pdf_conflicts"]
        vendor_request = planned[-1]
        proposed_pdf = {attribute.attribute_id for attribute in pdf_attributes if attribute.status == "proposed"}
        assert not proposed_pdf & set(vendor_request["attribute_ids"])
        assert set(fixture["expected_pdf_conflicts"]) <= set(vendor_request["attribute_ids"])
        assert any(entry.get("parsing") == "cache" for entry in provenance)
        result = persist_replay(replay, batch, item, result, "REPRODUCTION: verifier-only PDF-row cascade", provenance)
        replay_results.append({"item_key": item["item_key"], **item_summary(result)})
    assert len(planned) == 4
    assert replay.read_bytes(real_pilot.BUDGET_KEY) == replay_ledger_before
    replay_export = verify_exports(replay, batch, inputs)
    full_fallthrough = []
    for item in selected:
        processor = worker.RealBatchProcessor(replay, batch, replay_guard)

        class EmptyPdfCompletion:
            def complete_structured(self, system, user, schema):
                request = request_record(item["item_key"], system, user, schema, inputs, compactor)
                full_fallthrough.append(request)
                if request["source_tier"] == "internal_pdf":
                    return ExtractionResponse(candidates=[])
                return map_citations(response_for(request["original_payload"], request["references"], fixture), request["references"])

        run_cascade(Manifest.model_validate(item["manifest"]), cached_loader(processor, item, []), EmptyPdfCompletion())
    assert len(full_fallthrough) == 4
    assert replay.read_bytes(real_pilot.BUDGET_KEY) == replay_ledger_before
    legacy = legacy_header_negatives(inputs, batch)
    gate = MemoryStore(baseline)
    guard = real_pilot.RealPilotGuard(gate, batch)
    ledger_before = gate.read_bytes(real_pilot.BUDGET_KEY)
    with pytest.raises(ValueError, match="execution budget exhausted"):
        guard.before_execution("REPRODUCTION-NOT-A-NEW-AUTHORIZATION")
    assert gate.read_bytes(real_pilot.BUDGET_KEY) == ledger_before
    # Offline reservation simulation reuses an already consumed execution, never a new exception.
    guard._execution_id = consumed["final_rerun"]["execution_id"]
    requests, completions, gated_results = [], [], []
    active = {}

    def capture_prompt(user):
        request = request_record(active["item"]["item_key"], extract.product_extraction_system_message,
                                 user, ExtractionResponse, inputs, compactor)
        requests.append(request)
        return json.dumps(request["user"], ensure_ascii=False, separators=(",", ":")), request["references"]

    class Client:
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
            assert kwargs["max_completion_tokens"] == request["output_tokens"]
            response = response_for(request["original_payload"], request["references"], fixture)
            self.last_response_sha256 = sha(response.model_dump_json().encode())
            completions.append({"item_key": request["item_key"], "source_tier": request["source_tier"],
                                "request_index": len(requests) - 1})
            return response

    monkeypatch.setattr(worker, "compact_inference_prompt", capture_prompt)
    monkeypatch.setattr(worker, "LLMClient", Client)
    for item in selected:
        active["item"] = item
        processor = worker.RealBatchProcessor(gate, batch, guard)
        result, state = processor(item, "real_pilot")
        assert any(entry.get("parsing") == "cache" for entry in state["provenance"])
        assert not any(entry.get("parsing") == "fresh_analysis" for entry in state["provenance"])
        persist_replay(gate, batch, item, result, "REPRODUCTION: actual unchanged reservation guard", state["provenance"])
        gated_results.append({"item_key": item["item_key"], **state, "summary": item_summary(result)})
    ledger = read_json(gate, real_pilot.BUDGET_KEY)[0]
    assert len(requests) == 4 and len(completions) < 4
    assert ledger["executions"] == consumed["executions"]
    assert all(ledger["reservations"][key] == value for key, value in consumed["reservations"].items())
    assert ledger["attempted"]["analysis"] == consumed["attempted"]["analysis"]
    assert ledger["reserved"]["analysis_pages"] == consumed["reserved"]["analysis_pages"]
    reservations = [value for key, value in ledger["reservations"].items() if key not in consumed["reservations"]]
    assert len(reservations) == len(completions)
    for completion, reservation in zip(completions, reservations, strict=True):
        request = requests[completion["request_index"]]
        assert reservation["reserved_usage"]["input_tokens"] == request["input_tokens"]
        assert reservation["reserved_usage"]["output_tokens"] == request["output_tokens"]
    for store in (replay, gate):
        for key, (raw, _) in baseline.items():
            if key.startswith(("results/", "parses/", "response-diagnostics/", "operations/", "configuration/")):
                assert store.read_bytes(key)[0] == raw
        for item in batch["items"]:
            if item["item_key"] not in amendment["selected_item_keys"]:
                key = f"items/{batch['id']}/{item['item_key']}.json"
                assert store.read_bytes(key)[0] == baseline[key][0]
    gate_export = verify_exports(gate, batch, inputs)
    planned_input = sum(request["input_tokens"] for request in planned)
    planned_output = sum(request["output_tokens"] for request in planned)
    lower_bound = sum(request["parts"]["schema_bytes"] + request["parts"]["framing_reserve"] for request in planned)
    lower_bound += sum(request["definition_bytes"] for request in planned if request["source_tier"] == "internal_pdf")
    assert lower_bound > remaining["input_tokens"] and planned_input > remaining["input_tokens"]
    assert {name: sha((workspace / name).read_bytes()) for name in SOURCE_FILES} == sources, "Core changed during the draft"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == revision
    if os.environ.get("DOCINTEL_TEST_PDF_ROWS_REQUIRE_COMMITTED") == "true":
        subprocess.check_call(["git", "diff", "--quiet", "HEAD"])
    prefix = os.environ.get("DOCINTEL_TEST_PDF_ROWS_OUTPUT")
    if prefix:
        assert re.fullmatch(r"[A-Za-z0-9_-]+", prefix)
        artifacts = [
            write_private(inputs["root"], prefix + "-planned-payloads.json", planned),
            write_private(inputs["root"], prefix + "-guarded-payloads.json", requests),
            write_private(inputs["root"], prefix + "-full-fallthrough-payloads.json", full_fallthrough),
            write_private(inputs["root"], prefix + "-verifier-only.xlsx", replay_export),
            write_private(inputs["root"], prefix + "-guarded.xlsx", gate_export),
        ]
        report = {
            "label": "REPRODUCTION: PDF row presentation", "decision": "NO_GO",
            "recovered_output": False, "product_findings": False, "live_calls": 0,
            "source_revision": revision, "backend_sha256": sources, "input_sha256": inputs["source_hashes"],
            "committed_source_required": os.environ.get("DOCINTEL_TEST_PDF_ROWS_REQUIRE_COMMITTED") == "true",
            "verifier_only": {"not_budget_acceptance": True, "pdf": pdf_results, "cascade": replay_results},
            "legacy_v2_diagnostics_unchanged": legacy,
            "remaining_original_allowance": remaining,
            "planned_four_complete_requests": {"input_tokens": planned_input, "output_tokens": planned_output},
            "four_requests_if_pdf_returns_no_candidates": {
                "input_tokens": sum(request["input_tokens"] for request in full_fallthrough),
                "output_tokens": sum(request["output_tokens"] for request in full_fallthrough),
                "not_actual_model_output": True,
            },
            "input_deficit": planned_input - remaining["input_tokens"],
            "zero_system_zero_vendor_definitions_zero_evidence_lower_bound": lower_bound,
            "lower_bound_is_not_a_proposed_scope_reduction": True,
            "requests_below_25k": [request["input_tokens"] < 25000 for request in planned],
            "guard": {
                "new_execution_denied": True, "original_executions": len(consumed["executions"]),
                "original_final_requests_consumed": consumed["attempted"]["inference"] - consumed["final_rerun"]["inference_before"],
                "per_final_request_limit": 4, "reservations_mocked": False, "allowances_reset": False,
                "execution_start_bypass": "OFFLINE ONLY: existing consumed execution identity at historical timestamp",
                "simulated_time": simulated_time.isoformat(), "new_reservations": reservations,
                "simulated_provider_completions": completions, "results": gated_results,
                "actual_ledger_unchanged": True, "isolated_resulting_ledger": ledger,
            },
            "all_original_evidence_and_export_locators_preserved": True,
            "isolated_stale_reconciliation": reconciliation,
            "hosted_bytes_or_etags_claimed": False, "scope_pruned": False,
            "limitations": fixture["limitations"], "artifacts": artifacts,
        }
        receipt = write_private(inputs["root"], prefix + "-report.json", report)
        print(json.dumps({"decision": "NO_GO", "receipt": receipt}))
    for store in (original, replay, gate):
        store.connection.close()
