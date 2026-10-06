"""Synthetic, network-free import contract tests. No private fixtures in CI."""

import base64
import copy
import json
import socket
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from backend.batch_store import Missing
from backend.batch_worker import BatchProcessor
from backend.core.docintel import ParsedDocument, ParsedParagraph
from backend.parse_import import (
    Artifact, ParseImportError, append_import_records, canonical,
    prepare_parse_import, sha256, snapshot_records,
)
from backend.pilot import PARSER_VERSION
from scripts import import_cached_parse as cli
from tests.test_real_pilot import MemoryStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline import must not access a service")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr("backend.core.docintel.DocumentIntelligenceService._analyze", forbidden)


def artifact(name, content):
    return Artifact("/synthetic/" + name, sha256(content), content)


@pytest.fixture
def inputs():
    source = artifact("source.pdf", b"%PDF-1.4\nsynthetic only\n%%EOF")
    document = ParsedDocument(
        source=source.path, cache_key="sha256:" + source.expected_sha256,
        parsed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        paragraphs=[ParsedParagraph(text="Synthetic valve", page_number=1)],
        raw_text="Synthetic valve",
    )
    parsed = artifact("parsed.json", document.model_dump_json(indent=2).encode())
    cache = artifact("cache.json", canonical({
        "parser_version": PARSER_VERSION, "origin": "imported_verified_pilot",
        "document": json.loads(parsed.content),
        "document_sha256": sha256(document.model_dump_json().encode()),
    }))
    model, api_version, mapping = PARSER_VERSION.split(":")
    receipt = artifact("receipt.json", canonical({"analysis": {
        "outcome": "succeeded", "submissions": 1, "sdk_retries": 0, "model": model,
        "api_version": api_version, "mapping_version": mapping, "request_options": {},
        "source": source.path, "source_sha256": source.expected_sha256,
        "parsed_sha256": parsed.expected_sha256,
    }}))
    return dict(source=source, parsed=parsed, cache=cache, receipt=receipt, request_options={})


def edited(input_artifact, value):
    content = canonical(value)
    return replace(input_artifact, content=content, expected_sha256=sha256(content))


def test_preserves_bytes_original_source_time_receipt_and_production_cache_contract(inputs):
    prepared = prepare_parse_import(**inputs)
    prior = {
        "configuration/real-pilot-consumption.json": b'{"analysis":1,"state":"consumed"}',
        "results/old.json": b'{"failed":"prior immutable result"}',
    }
    saved = copy.deepcopy(prior)
    records = append_import_records(prior, prepared)
    assert prior == saved
    assert all(records[key] == value for key, value in prior.items())
    store = MemoryStore()
    for key, raw in records.items():
        store.write_bytes(key, raw)
    document, origin = BatchProcessor(store).cached(
        prepared.cache_key, {"sha256": inputs["source"].expected_sha256}, inputs["source"].path,
    )
    assert document.source == inputs["source"].path
    assert document.parsed_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert origin == "imported_verified_pilot"
    assert records[prepared.cache_key] == inputs["cache"].content
    for name in ("source", "parsed", "cache", "receipt"):
        assert records[f"parse-imports/{prepared.fingerprint}/{name}"] == inputs[name].content
    assert prepared.provenance["freshly_analyzed"] is False
    assert prepared.provenance["original_analysis_submissions"] == 1
    assert prepared.provenance["new_analysis_submissions"] == 0
    assert prepared.provenance["hosted_ingestion_verified"] is False
    assert prepare_parse_import(**inputs).fingerprint == prepared.fingerprint


def test_import_does_not_forge_blob_source_compatibility(inputs):
    prepared = prepare_parse_import(**inputs)
    store = MemoryStore()
    store.write_bytes(prepared.cache_key, inputs["cache"].content)
    with pytest.raises(ValueError, match="Incompatible parse cache or source association"):
        BatchProcessor(store).cached(
            prepared.cache_key, {"sha256": inputs["source"].expected_sha256},
            "batchblob:///documents/copied.pdf",
        )
    with pytest.raises(Missing):
        store.read_bytes("parses/unrelated.json")


@pytest.mark.parametrize("name", ["source", "parsed", "cache", "receipt"])
def test_every_original_byte_stream_is_independently_pinned(inputs, name):
    inputs[name] = replace(inputs[name], content=inputs[name].content + b"\n")
    with pytest.raises(ParseImportError) as caught:
        prepare_parse_import(**inputs)
    assert caught.value.codes == [name + "_hash_mismatch"]


@pytest.mark.parametrize("field", [
    "api_version", "mapping_version", "request_options", "source", "source_sha256", "parsed_sha256",
])
def test_legacy_wrapper_cannot_attest_missing_historical_request_fields(inputs, field):
    record = json.loads(inputs["receipt"].content)
    del record["analysis"][field]
    inputs["receipt"] = edited(inputs["receipt"], record)
    with pytest.raises(ParseImportError) as caught:
        prepare_parse_import(**inputs)
    assert caught.value.codes == ["receipt_missing_" + field]


@pytest.mark.parametrize(("field", "value"), [
    ("api_version", "2023-07-31"), ("mapping_version", "mapping-v2"),
    ("model", "prebuilt-read"), ("request_options", {"pages": "1-5"}),
    ("source", "batchblob:///documents/renamed.pdf"), ("source_sha256", "0" * 64),
    ("parsed_sha256", "0" * 64), ("outcome", "submission_authorized"),
    ("submissions", True), ("sdk_retries", 3),
])
def test_different_request_or_failed_receipt_is_not_compatible(inputs, field, value):
    record = json.loads(inputs["receipt"].content)
    record["analysis"][field] = value
    inputs["receipt"] = edited(inputs["receipt"], record)
    with pytest.raises(ParseImportError) as caught:
        prepare_parse_import(**inputs)
    assert caught.value.codes == ["receipt_mismatch_" + field]


def test_bounded_pages_are_distinct_from_full_document_cache(inputs):
    full = prepare_parse_import(**inputs)
    inputs["request_options"] = {"pages": "1-5"}
    record = json.loads(inputs["receipt"].content)
    record["analysis"]["request_options"] = inputs["request_options"]
    inputs["receipt"] = edited(inputs["receipt"], record)
    bounded = prepare_parse_import(**inputs)
    assert bounded.cache_key != full.cache_key
    assert bounded.fingerprint != full.fingerprint
    assert bounded.cache_key == "parses/" + sha256((
        inputs["source"].path + inputs["source"].expected_sha256 + PARSER_VERSION + ":pages=1-5"
    ).encode()) + ".json"


@pytest.mark.parametrize("options", [{"locale": "en-US"}, {"pages": "1"}, {"features": ["ocrHighResolution"]}, None])
def test_unrepresented_options_cannot_alias_existing_cache(inputs, options):
    inputs["request_options"] = options
    with pytest.raises(ParseImportError, match="request_options_not_representable"):
        prepare_parse_import(**inputs)


def test_document_relabel_or_mutation_is_rejected(inputs):
    envelope = json.loads(inputs["cache"].content)
    envelope["document"]["source"] = "batchblob:///documents/copied.pdf"
    inputs["cache"] = edited(inputs["cache"], envelope)
    with pytest.raises(ParseImportError, match="cache_does_not_preserve_original_document"):
        prepare_parse_import(**inputs)


def test_corrupted_digest_and_incompatible_parser_are_rejected(inputs):
    envelope = json.loads(inputs["cache"].content)
    original = inputs["cache"]
    for name, value, error in (
        ("document_sha256", "f" * 64, "cache_document_digest_mismatch"),
        ("parser_version", "prebuilt-layout:wrong:mapping-v1", "incompatible_parser_version"),
    ):
        inputs["cache"] = edited(original, {**envelope, name: value})
        with pytest.raises(ParseImportError, match=error):
            prepare_parse_import(**inputs)


def test_reimport_never_overwrites_even_identical_records(inputs):
    prepared = prepare_parse_import(**inputs)
    records = prepared.records()
    before = dict(records)
    with pytest.raises(ParseImportError, match="import_record_already_exists"):
        append_import_records(records, prepared)
    assert records == before


def test_snapshot_checks_all_records_without_interpreting_or_resetting_ledger():
    originals = {"ledger.json": b'{"consumed":7}', "history/old.json": b"original failure"}
    snapshot = {"records": {
        key: {"base64": base64.b64encode(value).decode(), "sha256": sha256(value)}
        for key, value in originals.items()
    }}
    assert snapshot_records(canonical(snapshot)) == originals
    snapshot["records"]["history/old.json"]["sha256"] = "0" * 64
    with pytest.raises(ParseImportError, match="snapshot_record_integrity_failed"):
        snapshot_records(canonical(snapshot))


def test_duplicate_json_keys_do_not_override_contract(inputs):
    raw = b'{"analysis": {}, "analysis": ' + canonical(json.loads(inputs["receipt"].content)["analysis"]) + b"}"
    inputs["receipt"] = replace(inputs["receipt"], content=raw, expected_sha256=sha256(raw))
    with pytest.raises(ParseImportError, match="invalid_receipt_json"):
        prepare_parse_import(**inputs)


def test_cli_refusal_creates_no_store(inputs, monkeypatch, capsys):
    record = json.loads(inputs["receipt"].content)
    del record["analysis"]["request_options"]
    inputs["receipt"] = edited(inputs["receipt"], record)
    monkeypatch.setattr(cli, "read_specification", lambda _: inputs)
    calls = []
    monkeypatch.setattr(cli, "persist_isolated_store", lambda *args: calls.append(args))
    assert cli.main(["--specification", "synthetic-spec", "--new-isolated-store", "synthetic-store"]) == 2
    assert calls == []
    result = json.loads(capsys.readouterr().out)
    assert result["blockers"] == ["receipt_missing_request_options"]
    assert result["new_analysis_submissions"] == 0


def test_cli_success_passes_unchanged_records_to_isolated_writer(inputs, monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_specification", lambda _: inputs)
    calls = []
    monkeypatch.setattr(cli, "persist_isolated_store", lambda path, records: calls.append(records))
    assert cli.main(["--specification", "synthetic-spec", "--new-isolated-store", "synthetic-store"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "IMPORTED_OFFLINE_ONLY"
    assert result["freshly_analyzed"] is False
    assert calls[0][result["cache_key"]] == inputs["cache"].content


def test_writer_rejects_existing_and_repository_destinations():
    from pathlib import Path

    with pytest.raises(ParseImportError, match="destination_must_be_new"):
        cli.persist_isolated_store(Path.cwd(), {})
    with pytest.raises(ParseImportError, match="private_destination_must_be_outside_repository"):
        cli.persist_isolated_store(Path.cwd() / ".never-create-parse-import-test", {})
