"""Opt-in exact two-product Ford gate; all real inputs/receipts remain private.

Set DOCINTEL_TEST_PARSE_IMPORT_ROOT to the retained release root and
DOCINTEL_TEST_PARSE_IMPORT_OUTPUT to a new private output prefix. This is an
offline readiness gate, not authorization and not an extraction-quality test.
"""

import json
import os
import socket
from pathlib import Path

import pytest

from backend.batch_store import Missing, SQLiteStore
from backend.batch_worker import BatchProcessor
from backend.parse_import import (
    Artifact, ParseImportError, append_import_records, canonical, prepare_parse_import, sha256, snapshot_records,
)
from backend.pilot import PARSER_VERSION
from scripts.import_cached_parse import main, persist_isolated_store
from tests.test_parse_import import inputs, no_network  # noqa: F401
from tests.test_real_pilot import MemoryStore


def private_write(root, name, value):
    assert Path(name).name == name
    path = root / name
    descriptor = os.open(os.path.relpath(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))
    return path


def test_exact_two_ford_import_gate_preserves_all_prior_records(monkeypatch, capsys):
    root_value = os.environ.get("DOCINTEL_TEST_PARSE_IMPORT_ROOT")
    if not root_value:
        pytest.skip("Private Ford artifacts are opt-in, never CI fixtures")
    root = Path(root_value).resolve(strict=True)
    assert root.is_dir() and not root.is_relative_to(Path(__file__).resolve().parents[1])
    prefix = os.environ["DOCINTEL_TEST_PARSE_IMPORT_OUTPUT"]
    assert Path(prefix).name == prefix
    workspace = Path(__file__).resolve().parents[1]
    source_hashes = {name: sha256((workspace / name).read_bytes()) for name in (
        "backend/parse_import.py", "scripts/import_cached_parse.py",
        "tests/test_parse_import.py", "tests/test_private_parse_import.py",
        "backend/batch_worker.py", "backend/core/docintel.py", "backend/pilot.py",
    )}
    blocked = []

    def forbidden(*args, **kwargs):
        blocked.append("unexpected_service_attempt")
        raise AssertionError("No network, credential, analysis or inference is authorized")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for target in (
        "backend.batch_worker.DocumentIntelligenceService",
        "backend.batch_worker.ManagedIdentityCredential", "backend.batch_worker.LLMClient",
        "backend.core.pilot_sources.retrieve_document",
    ):
        monkeypatch.setattr(target, forbidden)
    manifest_path = root / "ford-local-parse-readiness.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    entries = {
        "source": (manifest["approved_local_pdf"], "sha256"),
        "parsed": (manifest["parsed_artifact"], "file_sha256"),
        "cache": (manifest["retained_cache_envelope"], "file_sha256"),
        "receipt": (manifest["prior_success_receipt"], "file_sha256"),
    }
    artifacts = {
        name: Artifact(entry["path"], entry[hash_key], Path(entry["path"]).read_bytes())
        for name, (entry, hash_key) in entries.items()
    }
    for name, artifact in artifacts.items():
        artifact.verify(name)
    snapshot_receipt_path = root / "row-rerun-snapshot.json"
    snapshot_receipt_raw = snapshot_receipt_path.read_bytes()
    snapshot_receipt = json.loads(snapshot_receipt_raw)
    snapshot_path = root / snapshot_receipt["records_file"]
    assert snapshot_path.resolve().is_relative_to(root)
    snapshot_raw = snapshot_path.read_bytes()
    assert sha256(snapshot_raw) == snapshot_receipt["records_sha256"]
    originals = snapshot_records(snapshot_raw)
    store = MemoryStore()
    for key, value in originals.items():
        store.write_bytes(key, value)
    all_records_before = dict(store.records)
    batches = [json.loads(raw) for key, raw in originals.items() if key.startswith("batches/")]
    assert len(batches) == 1
    batch = batches[0]
    selected = [
        (item, binding) for item in batch["items"] for binding in item["sources"]
        if binding["sha256"] == artifacts["source"].expected_sha256 and binding["format"] == "pdf"
    ]
    assert len(selected) == 2 and len({item["item_key"] for item, _ in selected}) == 2
    with pytest.raises(ParseImportError) as rejected:
        prepare_parse_import(**artifacts, request_options={})
    assert "receipt_missing_api_version" in rejected.value.codes
    assert "receipt_missing_request_options" in rejected.value.codes
    original_source = artifacts["source"].path
    local_key = "parses/" + sha256((
        original_source + artifacts["source"].expected_sha256 + PARSER_VERSION
    ).encode()) + ".json"
    retained_store = MemoryStore()
    retained_store.write_bytes(local_key, artifacts["cache"].content)
    rows = []
    for item, binding in selected:
        location = "batchblob:///" + binding["blob"]
        document, origin = BatchProcessor(retained_store).cached(local_key, binding, original_source)
        assert document.source == original_source and origin == "imported_verified_pilot"
        with pytest.raises(ValueError, match="Incompatible parse cache or source association"):
            BatchProcessor(retained_store).cached(local_key, binding, location)
        keys = [
            "parses/" + sha256((location + binding["sha256"] + PARSER_VERSION + suffix).encode()) + ".json"
            for suffix in ("", ":pages=1-5")
        ]
        for key in keys:
            with pytest.raises(Missing):
                BatchProcessor(store).cached(key, binding, location)
        rows.append({
            "item_key": item["item_key"], "product": item["manifest"]["product"],
            "source_id": binding["source_id"], "approved_sha256": binding["sha256"],
            "production_location": location, "production_cache_keys": keys,
            "retained_local_cache_accepted": True, "blob_source_relabel_accepted": False,
            "status": "NO_GO_IMPORT_REFUSED",
        })
    spec = {
        **{name: {"path": artifact.path, "sha256": artifact.expected_sha256}
           for name, artifact in artifacts.items()},
        "request_options": {},
    }
    specification = private_write(root, prefix + "-specification.json", spec)
    destination = root / (prefix + "-isolated-store")
    assert not destination.exists()
    assert main([
        "--specification", str(specification), "--new-isolated-store", str(destination),
        "--snapshot", str(snapshot_path), "--snapshot-sha256", sha256(snapshot_raw),
    ]) == 2
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["blockers"] == rejected.value.codes
    assert not destination.exists()
    assert not blocked and store.records == all_records_before
    assert all(store.read_bytes(key)[0] == raw for key, raw in originals.items())
    for artifact in artifacts.values():
        assert Path(artifact.path).read_bytes() == artifact.content
    assert snapshot_path.read_bytes() == snapshot_raw
    assert snapshot_receipt_path.read_bytes() == snapshot_receipt_raw
    assert manifest_path.read_bytes() == manifest_raw
    report = {
        "status": "NO_GO", "gate": "exact_two_ford_offline_parse_import",
        "tested_sources_sha256": source_hashes,
        "snapshot_observed_at": snapshot_receipt["observed_at"],
        "snapshot_sha256": sha256(snapshot_raw), "snapshot_records": len(originals),
        "all_snapshot_records_unchanged": True,
        "prior_record_sha256": {key: sha256(raw) for key, raw in originals.items()},
        "original_artifacts": {name: artifact.metadata() for name, artifact in artifacts.items()},
        "parser_version_claim_in_legacy_wrapper": PARSER_VERSION,
        "historical_request_api_and_options_independently_proven": False,
        "retained_local_cache_key": local_key, "products": rows,
        "blockers": rejected.value.codes, "cli": cli_report,
        "network_attempts": blocked, "new_analysis_submissions": 0, "new_inference_requests": 0,
        "live_ledger_accessed": False, "historical_allowances_replenished": False,
        "private_fixture_store": "isolated_in_memory_copy",
        "production_blob_cache_imported": False,
        "scope": "Historical offline snapshot; not current hosted readiness or authorization.",
    }
    private_write(root, prefix + "-gate.json", report)


def test_private_isolated_writer_persists_synthetic_records(inputs):
    root_value = os.environ.get("DOCINTEL_TEST_PARSE_IMPORT_ROOT")
    if not root_value:
        pytest.skip("Private destination persistence test is opt-in")
    root = Path(root_value).resolve(strict=True)
    assert root.is_dir() and not root.is_relative_to(Path(__file__).resolve().parents[1])
    prefix = os.environ["DOCINTEL_TEST_PARSE_IMPORT_OUTPUT"]
    assert Path(prefix).name == prefix
    prepared = prepare_parse_import(**inputs)
    prior = {
        "synthetic-history/consumed-ledger": b'{"consumed":1}',
        "synthetic-history/failed-result": b'{"immutable":"failure"}',
    }
    records = append_import_records(prior, prepared)
    destination = root / (prefix + "-SYNTHETIC-isolated-store")
    persist_isolated_store(destination, records)
    stored = SQLiteStore(destination)
    assert set(stored.keys("")) == set(records)
    assert all(stored.read_bytes(key)[0] == value for key, value in records.items())
    document, origin = BatchProcessor(stored).cached(
        prepared.cache_key, {"sha256": inputs["source"].expected_sha256}, inputs["source"].path,
    )
    assert document.source == inputs["source"].path and origin == "imported_verified_pilot"
    with pytest.raises(ParseImportError, match="destination_must_be_new"):
        persist_isolated_store(destination, records)
    assert all(stored.read_bytes(key)[0] == value for key, value in records.items())
    assert destination.stat().st_mode & 0o777 == 0o700
    assert stored.path.stat().st_mode & 0o777 == 0o600
    private_write(root, prefix + "-SYNTHETIC-persistence.json", {
        "label": "SYNTHETIC PERSISTENCE VALIDATION; NOT A FORD IMPORT",
        "records": len(records), "prior_records_unchanged": True,
        "raw_import_artifacts_unchanged": True, "production_cached_accepted": True,
        "existing_store_rejected": True, "new_analysis_submissions": 0,
        "store": str(destination),
    })
