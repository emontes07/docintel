"""Synthetic scale, accounting, and customer reviewer presentation; no providers."""

import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from backend.batch import BatchService
from backend.batch_store import read_json, write_json
from backend.batch_worker import run_batch
from backend.models.enrichment import (
    AttributeDefinition, AttributeResult, Candidate, EnrichmentResult, Evidence, Manifest, ProductKey,
)
from backend.multisource import run_cascade
from backend.reviewer_workbook import (
    REVIEW_COLUMNS, build_reviewer_package, build_snapshot_reviewer_package, reviewer_status,
)
from backend.telemetry import ItemTelemetry, TierTelemetry, record_accounting, summary_report
from backend.workbooks import read_workbook
from tests.test_multisource import Completion, source
from tests.test_real_batch_worker import configured, install_services


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Track B tests cannot contact any live service")

    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.setattr("socket.socket.connect_ex", blocked)
    monkeypatch.setattr("socket.getaddrinfo", blocked)


def telemetry():
    return ItemTelemetry(elapsed_ms=100, tiers=[
        TierTelemetry(source_tier="internal_pdf", outcome="completed", elapsed_ms=90),
    ])


def receipt(*, usage=None, method="model", new=True):
    return {
        "method": method, "source_tier": "internal_pdf", "new_model_call": new,
        "elapsed_ms": 50, "usage": usage,
    }


def reservation():
    return {
        "source_tier": "internal_pdf", "operation": "inference",
        "reserved_usage": {"input_tokens": 1000, "output_tokens": 2048},
        "reserved_microdollars": 30000,
    }


def test_missing_measured_usage_is_unknown_not_reservation_or_zero():
    observed = telemetry()
    record_accounting(observed, [receipt()], [reservation()], unit_prices={"input_token": "0.01", "output_token": "0.02"})
    tier = observed.tiers[0]
    assert observed.accounting_status == "partial"
    assert tier.model_requests == 1 and tier.unknown_usage_calls == 1
    assert tier.measured_input_tokens is None and tier.measured_output_tokens is None
    assert tier.estimated_model_cost_usd is None
    assert tier.known_input_tokens == tier.known_output_tokens == 0
    assert tier.reserved_input_tokens == 1000 and tier.reserved_output_tokens == 2048
    assert observed.measured_input_tokens is None and observed.estimated_model_cost_usd is None
    assert observed.reserved_input_tokens == 1000


def test_repository_fixture_adapter_does_not_relax_production_store_guard():
    from backend.batch_store import SQLiteStore

    with pytest.raises(ValueError, match="outside the repository"):
        SQLiteStore(Path.cwd() / "must-not-be-created-by-telemetry-test")


@pytest.mark.parametrize("shape", ["complete", "legacy", "missing_pair", "fabricated_usage", "missing_item"])
def test_release_export_verifier_accepts_only_valid_optional_telemetry(configured, tmp_path, monkeypatch, shape):
    from scripts import release_fixture

    store, _, _ = configured
    fixture = tmp_path / "synthetic-release"
    release_fixture.prepare(fixture)
    for path in (fixture / "seed").rglob("*"):
        if path.is_file():
            store.write_bytes(path.relative_to(fixture / "seed").as_posix(), path.read_bytes())
    service = BatchService(store)
    owner = "development:synthetic-release"
    batch = service.intake(
        (fixture / "manifest.xlsx").read_bytes(), (fixture / "definitions.xlsx").read_bytes(),
        "definitions.xlsx", owner,
    )
    service.submit(batch["id"], owner, "11111111-1111-4111-8111-111111111111", "evidence_only", False)
    run_batch(store, batch["id"], concurrency=1, item_limit=2)
    hashes = {str(row): service.detail(batch["id"], f"row-{row}", owner)["machine_sha256"] for row in (2, 3)}
    service.review(batch["id"], "row-2", owner, {
        "attribute_id": "Pressure Rating", "decision": "reject", "reason": "Synthetic acceptance only.",
    })
    content = service.export(batch["id"], owner)
    workbook = read_workbook(content)
    if shape == "legacy":
        workbook.pop("Telemetry")
        workbook.pop("Telemetry Summary")
    elif shape == "missing_pair":
        workbook.pop("Telemetry Summary")
    elif shape == "fabricated_usage":
        workbook["Telemetry"][0]["Measured input tokens"] = "0"
    elif shape == "missing_item":
        workbook["Telemetry"].pop()
    monkeypatch.setattr(release_fixture, "read_workbook", lambda _: workbook)
    if shape in {"complete", "legacy"}:
        assert release_fixture.verify_export(content, batch["id"], owner, hashes)
    else:
        with pytest.raises(AssertionError):
            release_fixture.verify_export(content, batch["id"], owner, hashes)


def test_measured_cost_is_exact_and_cache_failure_not_double_counted():
    observed = telemetry()
    events = [
        receipt(usage={"input_tokens": 11, "output_tokens": 3}),
        receipt(method="optional_failed"), receipt(method="compatible_response_cache", new=False),
    ]
    record_accounting(observed, events, [reservation()], unit_prices={"input_token": "0.0000012", "output_token": "0.000009"})
    tier = observed.tiers[0]
    assert tier.model_requests == 1 and tier.response_cache_hits == 1
    assert tier.measured_input_tokens == 11 and tier.measured_output_tokens == 3
    assert Decimal(tier.estimated_model_cost_usd) == Decimal("0.0000402")
    assert tier.reserved_cost_microdollars == 30000
    assert observed.accounting_status == "complete"
    assert tier.model_ms == 50
    assert observed.model_unit_prices_usd == {"input_token": "0.0000012", "output_token": "0.000009"}
    record_accounting(observed, events, [reservation()], unit_prices=None)
    assert tier.reserved_input_tokens == 1000
    assert tier.estimated_model_cost_usd is None


@pytest.mark.parametrize("usage", [{"input_tokens": 4}, {"input_tokens": True, "output_tokens": -1}, None])
def test_partial_or_invalid_usage_does_not_become_complete(usage):
    observed = telemetry()
    record_accounting(observed, [receipt(usage=usage)], [], unit_prices=None)
    assert observed.accounting_status == "partial"
    assert observed.tiers[0].estimated_model_cost_usd is None


def test_production_result_and_technical_export_include_measured_telemetry(configured, monkeypatch):
    store, record, _ = configured
    _, _, _, calls = install_services(monkeypatch)
    run_batch(store, record["id"], concurrency=1, item_limit=1)
    raw, _ = read_json(store, f"results/{record['id']}/row-2.json")
    result = EnrichmentResult.model_validate(raw)
    assert result.telemetry is not None and result.telemetry.accounting_status == "complete"
    assert len(result.telemetry.tiers) == 4
    report = summary_report([result])
    assert report["model_requests"] == len(calls) == 2
    assert report["measured_input_tokens"] == 100 and report["measured_output_tokens"] == 40
    assert Decimal(report["estimated_model_cost_usd"]) == Decimal("0.000140")
    assert report["reserved_input_tokens"] > report["measured_input_tokens"]
    assert report["reserved_output_tokens"] == 4096
    assert result.telemetry.model_requests == 2
    assert result.telemetry.measured_input_tokens == 100
    assert result.telemetry.estimated_model_cost_usd == report["estimated_model_cost_usd"]
    assert result.telemetry.elapsed_ms >= 0
    exported = read_workbook(BatchService(store).export(record["id"], record["owner"]))
    assert len(exported["Telemetry"]) == 4
    pdf = exported["Telemetry"][0]
    assert pdf["Measured input tokens"] == "50" and pdf["Reserved output tokens"] == "2048"
    assert any(row["Metric"] == "measured_input_tokens" and row["Value"] == "100"
               for row in exported["Telemetry Summary"])
    assert len(exported["Results"]) == 3


@pytest.mark.parametrize("size,concurrency", [(10, 1), (20, 4)])
def test_synthetic_multiitem_worker_keeps_timing_and_usage_isolated(configured, size, concurrency):
    store, template, _ = configured
    record = copy.deepcopy(template)
    record.update(id="c" * 64, mode="evidence_only", product_count=size)
    record["items"] = []
    for index in range(size):
        item = copy.deepcopy(template["items"][0])
        item.update(item_key=f"row-{index + 2}", row=index + 2)
        item["manifest"]["product"].update(item_id=f"{index:04}", mpn=f"PART-{index:04}")
        item["original"].update({"PIMITEM Number": f"{index:04}", "MPN": f"PART-{index:04}"})
        record["items"].append(item)
    write_json(store, f"batches/{record['id']}.json", record)

    def synthetic_processor(item, mode):
        manifest = Manifest.model_validate(item["manifest"])
        completion = Completion()
        texts = {"internal_pdf": "Body Material: brass", "manufacturer_web": "Outlet: swivel"}
        result = run_cascade(
            manifest, lambda tier, _: [source(manifest, tier, texts[tier])] if tier in texts else [], completion,
        )
        events, receipts = [], []
        for index, call in enumerate(completion.calls):
            tier = call["evidence"][0]["source_tier"]
            missing = int(manifest.product.item_id) % 4 == 0 and index == 1
            events.append({**receipt(usage=None if missing else {"input_tokens": 12, "output_tokens": 4}), "source_tier": tier})
            receipts.append({**reservation(), "source_tier": tier})
        record_accounting(result.telemetry, events, receipts, unit_prices={"input_token": "0.01", "output_token": "0.02"})
        return result, {"state": "unresolved", "inference_provenance": events}

    run_batch(store, record["id"], concurrency=concurrency, item_limit=size, processor=synthetic_processor)
    results = [EnrichmentResult.model_validate(read_json(store, f"results/{record['id']}/{item['item_key']}.json")[0])
               for item in record["items"]]
    assert len(results) == size
    assert all(len(result.telemetry.tiers) == 4 for result in results)
    report = summary_report(results)
    missing_count = (size + 3) // 4
    assert report["known_model_requests"] == size * 2
    assert report["unknown_usage_calls"] == missing_count
    assert report["measured_input_tokens"] is None and report["measured_output_tokens"] is None
    assert report["estimated_model_cost_usd"] is None
    assert report["known_input_tokens"] == (size * 2 - missing_count) * 12
    assert report["known_output_tokens"] == (size * 2 - missing_count) * 4
    assert report["reserved_input_tokens"] == size * 2000
    assert report["reserved_output_tokens"] == size * 4096
    assert report["literal_candidates"] == size * 2 and report["unresolved_attributes"] == size
    assert read_json(store, f"batches/{record['id']}.json")[0]["state"] == "completed"
    exported = read_workbook(BatchService(store).export(record["id"], record["owner"]))
    assert len(exported["Telemetry"]) == size * 4
    assert len(exported["Results"]) == size * 3
    metrics = {row["Metric"]: row["Value"] for row in exported["Telemetry Summary"]}
    assert metrics["accounting_complete"] == "False"
    assert metrics["known_model_requests"] == str(size * 2)
    assert metrics["unknown_usage_calls"] == str(missing_count)
    assert metrics["known_input_tokens"] == str((size * 2 - missing_count) * 12)
    assert metrics["known_output_tokens"] == str((size * 2 - missing_count) * 4)
    assert metrics["reserved_input_tokens"] == str(size * 2000)
    assert metrics["reserved_output_tokens"] == str(size * 4096)
    assert all(metrics[name] == "" for name in (
        "measured_input_tokens", "measured_output_tokens", "estimated_model_cost_usd",
    ))
    assert all(read_json(store, f"items/{record['id']}/{item['item_key']}.json")[0]["elapsed_ms"] >= 0 for item in record["items"])
    before = [store.read_bytes(f"results/{record['id']}/{item['item_key']}.json") for item in record["items"]]
    run_batch(store, record["id"], concurrency=concurrency, item_limit=size, processor=synthetic_processor)
    assert before == [store.read_bytes(f"results/{record['id']}/{item['item_key']}.json") for item in record["items"]]


def result_for_review():
    product = ProductKey(item_id="PUBLIC-001", mpn="SYN-001", vendor="Synthetic", hierarchy_node="Valves")
    evidence = Evidence(
        evidence_id="private-source:" + "d" * 64, source_id="private-source", source_version="sha256:" + "e" * 64,
        source_locator="https://manufacturer.example/product?secret=PRIVATE#section=visible-text",
        source_tier="manufacturer_web", content_kind="source_excerpt", text="Body Material: brass",
        observed_at=datetime.now(timezone.utc), provider_retrieved_at=datetime.now(timezone.utc),
        qualification="Exact SYN-001 product; unrelated variants excluded.",
    )
    return EnrichmentResult(
        execution_mode="offline_replay", candidate_source="supplied_response", model_call_status="not_attempted",
        manifest=Manifest(product=product, attributes=[
            AttributeDefinition(attribute_id=name, description=name, value_type="string")
            for name in ("Body Material", "Unsupported", "Verify", "Unavailable")
        ]),
        observed_at=datetime.now(timezone.utc), retrieval=[], evidence=[evidence],
        extraction_error="invalid_response", attributes=[
            AttributeResult(attribute_id="Body Material", status="proposed", candidates=[Candidate(
                attribute_id="Body Material", value="brass", evidence_ids=[evidence.evidence_id],
                supporting_quote="Body Material: brass", qualification="Exact product.",
            )]),
            AttributeResult(attribute_id="Unsupported", status="missing_evidence"),
            AttributeResult(attribute_id="Verify", status="extraction_failed"),
            AttributeResult(attribute_id="Unavailable", status="retrieval_failed"),
        ],
    )


def test_reviewer_workbook_actionable_statuses_and_private_binding_not_leaked():
    result = result_for_review()
    before = result.model_dump_json()
    package = build_reviewer_package([result], attempt_metadata={"PUBLIC-001": {"attempt_id": "PRIVATE-ATTEMPT", "ledger": "f" * 64}})
    sheets = read_workbook(package.workbook)
    assert list(sheets["Review"][0]) == REVIEW_COLUMNS
    assert sheets["Review"][0]["Decision"] == ""
    assert "Candidate index" not in sheets["Review"][0]
    assert sheets["Review"][1]["Status"] == "Supporting evidence needed"
    assert sheets["Review"][2]["Status"] == "Evidence verification needs attention"
    assert sheets["Review"][3]["Status"] == "Source unavailable"
    evidence = sheets["Evidence"][0]
    assert evidence["Source URL"] == "https://manufacturer.example/product"
    assert evidence["Retrieved at"] == result.evidence[0].provider_retrieved_at.isoformat()
    assert "Exact SYN-001" in evidence["Applicability"]
    text = json.dumps(sheets)
    for forbidden in ("Extraction failed", "extraction_failed", "private-source", "PRIVATE-ATTEMPT", "secret=", "d" * 64, "e" * 64, "f" * 64):
        assert forbidden not in text
    assert "PRIVATE-ATTEMPT" in json.dumps(package.private_binding)
    assert result.model_dump_json() == before
    assert package.summary["attributes"] == 4
    assert package.private_binding["workbook_sha256"]
    assert package.private_binding["rows"][0]["candidate_index"] == 0
    assert "Candidate index" not in json.dumps(sheets)


def test_reviewer_inferred_literal_separation_missing_timestamp_and_formula_safety():
    result = result_for_review()
    result.evidence[0].provider_retrieved_at = None
    inferred = Candidate(
        attribute_id="Lead-Free", value=True, evidence_ids=[result.evidence[0].evidence_id],
        evidence_basis="inferred_from_description", inference_rule="lead_free_description_v1",
        supporting_quote="Lead-free brass valve", qualification="Exact product.",
    )
    result.attributes.append(AttributeResult(attribute_id="Lead-Free", status="proposed", candidates=[inferred]))
    result.attributes[0].candidates[0].value = "=HYPERLINK(\"https://bad.invalid\")"
    package = build_reviewer_package([result])
    sheets = read_workbook(package.workbook)
    assert sheets["Review"][-1]["Status"] == "Descriptive inference — review required"
    assert sheets["Review"][-1]["Evidence basis"] == "inferred_from_description"
    assert sheets["Evidence"][0]["Retrieved at"] == "Not recorded"
    assert sheets["Review"][0]["Proposed value"].startswith("=HYPERLINK")
    with ZipFile(BytesIO(package.workbook)) as archive:
        assert all(b"<f>" not in archive.read(name) for name in archive.namelist() if name.endswith(".xml"))
    report = summary_report([result])
    assert report["measured_input_tokens"] is None
    assert report["literal_candidates"] == 1 and report["inferred_review_candidates"] == 1
    assert report["unresolved_attributes"] == 4


def test_reviewer_rejects_duplicate_attempts_and_does_not_overwrite_files():
    result = result_for_review()
    with pytest.raises(ValueError, match="one selected attempt"):
        build_reviewer_package([result, result])
    attribute = result.attributes[2]
    result.extraction_error = "model_failed"
    assert reviewer_status(attribute, result)[0] == "Processing unavailable"


def test_reviewer_internal_locator_coordinates_are_preserved_without_private_path():
    result = result_for_review()
    evidence = result.evidence[0]
    evidence.source_tier = "internal_pdf"
    evidence.source_locator = "batchblob:///private/customer/a.pdf#page=2&table=0&row=3&column=1"
    package = build_reviewer_package([result], source_labels={"private-source": "/Users/private/customer/a.pdf"})
    row = read_workbook(package.workbook)["Evidence"][0]
    assert row["Source URL"] == ""
    assert row["Location"] == "page 2, table 0, row 3, column 1"
    assert "private/customer" not in json.dumps(row)


@pytest.mark.parametrize(("quote", "location"), [
    ('"LOW LEAD BRASS"', "row 7; quote matched cells D7, F7"),
    ('"3/4" COPPER SERVICE INLET"', "row 7; quote matched cells E7"),
    ("Paraphrased material description", "row 7; row-level citation"),
])
def test_reviewer_vendor_quote_matched_cells_are_precise(quote, location):
    result = result_for_review()
    evidence = result.evidence[0]
    evidence.source_tier = "vendor_table"
    evidence.source_locator = "batchblob:///private/customer/table.xlsx#row=7&cells=C7,D7,E7,F7"
    evidence.text = json.dumps({"sheet": "Vendor", "row": 7, "cells": [
        {"cell": "C7", "column": "Part", "value": "SYN-001"},
        {"cell": "D7", "column": "Material", "value": "Low  lead brass"},
        {"cell": "E7", "column": "Connection", "value": '3/4" COPPER SERVICE INLET'},
        {"cell": "F7", "column": "Description", "value": "LOW LEAD BRASS"},
    ]})
    result.attributes[0].candidates[0].supporting_quote = quote
    before = result.model_dump_json()
    row = read_workbook(build_reviewer_package([result]).workbook)["Evidence"][0]
    assert row["Location"] == location
    assert row["Quote"] == quote
    assert "private/customer" not in json.dumps(row)
    assert result.model_dump_json() == before


def test_telemetry_source_failure_is_recorded_without_model_or_retry():
    from backend.models.enrichment import OfflineSource

    result = result_for_review()
    completion = Completion()
    failed = OfflineSource(source_id="missing", product=result.manifest.product, error_code="not_found")
    observed = run_cascade(
        result.manifest, lambda tier, _: [failed] if tier == "internal_pdf" else [], completion,
    )
    assert observed.telemetry.tiers[0].outcome == "failed"
    assert not completion.calls


def test_older_results_default_to_unknown_telemetry_and_schema_does_not_leak_it():
    from backend.models.enrichment import ExtractionResponse

    raw = result_for_review().model_dump(mode="json")
    raw.pop("telemetry")
    result = EnrichmentResult.model_validate(raw)
    assert result.telemetry is None
    assert "telemetry" not in json.dumps(ExtractionResponse.model_json_schema())
    report = summary_report([], expected_items=10)
    assert report["items_without_telemetry"] == 10 and report["items_with_result"] == 0
    assert report["measured_input_tokens"] is None and report["estimated_model_cost_usd"] is None


def test_reviewer_literal_support_is_not_hidden_by_earlier_descriptive_candidate():
    result = result_for_review()
    inferred = Candidate(
        attribute_id="Lead-Free", value=True, evidence_ids=[result.evidence[0].evidence_id],
        evidence_basis="inferred_from_description", inference_rule="lead_free_description_v1",
        supporting_quote="Lead-free brass valve", qualification="Exact product.",
    )
    literal = Candidate(
        attribute_id="Lead-Free", value=True, evidence_ids=[result.evidence[0].evidence_id],
        supporting_quote="Lead-Free: Yes", qualification="Exact product.",
    )
    attribute = AttributeResult(attribute_id="Lead-Free", status="proposed", candidates=[inferred, literal])
    assert reviewer_status(attribute, result)[0] == "Proposal ready for review"
    result.attributes = [attribute]
    package = build_reviewer_package([result])
    sheets = read_workbook(package.workbook)
    assert "Candidate index" not in sheets["Review"][0]
    assert package.private_binding["rows"][0]["candidate_index"] is None
    assert "inferred_from_description" in sheets["Review"][0]["Evidence basis"]
    assert "literal" in sheets["Review"][0]["Evidence basis"]
    assert "requires review" in sheets["Review"][0]["Applicability"]
    result.quality_run_id = "synthetic-quality"
    quality_sheets = read_workbook(build_reviewer_package([result]).workbook)
    assert any("does not include an attribute definition" in row["Question"] for row in quality_sheets["Questions"])


def snapshot_for_review():
    result = result_for_review()
    definitions = {definition.attribute_id: definition for definition in result.manifest.attributes}
    evidence = {entry.evidence_id: entry.model_dump(mode="json") for entry in result.evidence}
    slots = [{
        "key": str(index), "item_key": "PRIVATE-ITEM-KEY", "product": result.manifest.product.model_dump(),
        "attribute_id": attribute.attribute_id,
        "definition": definitions[attribute.attribute_id].model_dump(mode="json"),
        "attempt_key": "results/PRIVATE-ATTEMPT.json", "result_sha256": "c" * 64,
        "status": attribute.status, "existing_value": None, "extraction_error": result.extraction_error,
        "review": {"decision": "approve", "reason": "Old review must not prefill new key"},
        "candidates": [{
            **candidate.model_dump(mode="json"), "index": offset, "tiers": ["manufacturer_web"],
            "evidence": [evidence[key] for key in candidate.evidence_ids],
        } for offset, candidate in enumerate(attribute.candidates)],
    } for index, attribute in enumerate(result.attributes)]
    absent = copy.deepcopy(slots[-1])
    absent.update(key="new", attribute_id="No result", status="no_result", result_sha256="unavailable")
    absent["definition"]["attribute_id"] = "No result"
    slots.append(absent)
    body = {"owner": "PRIVATE-OWNER", "batch_id": "b" * 64, "slots": slots}
    return {"id": hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest(), "body": body}


def test_snapshot_adapter_includes_missing_result_and_blank_human_inputs():
    snapshot = snapshot_for_review()
    package = build_snapshot_reviewer_package(snapshot)
    sheets = read_workbook(package.workbook)
    assert len(sheets["Review"]) == len(snapshot["body"]["slots"]) == 5
    assert list(sheets["Review"][0])[:7] == [
        "Product ID", "MPN", "Attribute", "Decision", "Correction", "Correction unit", "Reason",
    ]
    assert all(
        row[name] == "" for row in sheets["Review"]
        for name in ("Decision", "Correction", "Correction unit", "Reason")
    )
    assert sheets["Review"][-1]["Status"] == "No result available"
    assert sheets["Review"][1]["Status"] == "Supporting evidence needed"
    assert sheets["Evidence"][0]["Source tier"] == "manufacturer_web"
    assert "Candidate index" not in json.dumps(sheets)
    serialized = json.dumps(sheets)
    for forbidden in (snapshot["id"], "PRIVATE-ITEM-KEY", "PRIVATE-ATTEMPT", "PRIVATE-OWNER", "c" * 64, "b" * 64):
        assert forbidden not in serialized
    assert package.private_binding["snapshot_id"] == snapshot["id"]
    assert package.private_binding["rows"][0]["attempt_key"] == "results/PRIVATE-ATTEMPT.json"
    assert package.private_binding["rows"][0]["result_sha256"] == "c" * 64
    assert package.private_binding["rows"][0]["candidate_index"] == 0


def test_snapshot_adapter_rejects_mutation_and_duplicate_visible_identity():
    snapshot = snapshot_for_review()
    snapshot["body"]["slots"][0]["status"] = "missing_evidence"
    with pytest.raises(ValueError, match="integrity"):
        build_snapshot_reviewer_package(snapshot)
    result = result_for_review()
    second = result.model_copy(deep=True)
    second.manifest.product.mpn = "OTHER-MPN"
    with pytest.raises(ValueError, match="one selected attempt"):
        build_reviewer_package([result, second])


@pytest.mark.parametrize("url", [
    "https://private.sharepoint.com/sites/customer/doc.pdf", "https://127.0.0.1/product",
    "https://internal/product", "https://manufacturer.example/" + "a" * 64,
])
def test_reviewer_hides_internal_or_private_identifiers_in_urls(url):
    result = result_for_review()
    result.evidence[0].source_locator = url
    result.evidence[0].qualification = "See " + url
    sheets = read_workbook(build_reviewer_package([result]).workbook)
    assert sheets["Evidence"][0]["Source URL"] == ""
    assert url not in json.dumps(sheets)
