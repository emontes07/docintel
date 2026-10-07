"""All-four production worker reproduction using only synthetic caches/providers."""

import copy
import hashlib
import json

import pytest

from backend import batch_worker as worker, real_pilot
from backend.batch import BatchService
from backend.batch_store import read_json
from backend.core.websearch_webiq import WebIQSearchClient
from backend.workbooks import read_workbook, write_workbook
from tests.test_four_product_continuation import (
    four_case, final_case, network_blocked, put, row_case,
)
from tests.test_four_product_private_gate import (
    assert_preserved, assert_sdk_failure_before_reservation, profile_worker, reproduction,
)
from tests.test_real_batch_worker import configured as base_configured, recovery_case as base_recovery_case
from tests.test_row_rerun import all_records


@pytest.fixture
def configured(tmp_path, monkeypatch):
    store, batch, approval = base_configured.__wrapped__(tmp_path, monkeypatch)
    first = batch["items"][0]
    products = [{**first["manifest"]["product"], "item_id": f"00{index}", "mpn": f"PART-00{index}"}
                for index in range(1, 5)]
    scopes = [{
        "product": product, "identity_terms": ["Synthetic"],
        "attribute_ids": [entry["attribute_id"] for entry in first["manifest"]["attributes"]],
        "qualification": "REPRODUCTION ONLY; synthetic exact-row associations.",
    } for product in products]
    for source in first["sources"]:
        source["products"] = copy.deepcopy(products)
        source["applicability"] = copy.deepcopy(scopes)
    table = write_workbook({"Catalog": [["MPN", "Outlet"], *[[p["mpn"], "synthetic"] for p in products]]})
    store.write_bytes("documents/synthetic-table.xlsx", table)
    first["sources"].append({
        "reference": "synthetic-table.xlsx", "source_id": "vendor", "kind": "blob", "format": "xlsx",
        "source_tier": "vendor_table", "blob": "documents/synthetic-table.xlsx",
        "sha256": hashlib.sha256(table).hexdigest(), "products": products, "applicability": scopes,
        "table": {"sheet": "Catalog", "mpn_column": "MPN", "expected_vendor": "Synthetic"},
    })
    first["manifest"]["source_ids"].append("vendor")
    approval["batch_sha256"] = real_pilot.binding_digest(batch)
    put(store, real_pilot.APPROVAL_KEY, approval)
    put(store, f"batches/{batch['id']}.json", batch)
    return store, batch, approval


@pytest.fixture
def recovery_case(configured, monkeypatch):
    from backend.core.docintel import ParsedDocument, ParsedParagraph

    case = base_recovery_case.__wrapped__(configured, monkeypatch)
    store, batch, _, recovery, _ = case
    for key in recovery["cached_documents"]:
        cache = read_json(store, key)[0]
        document = ParsedDocument.model_validate(cache["document"])
        paragraphs = [ParsedParagraph(
            text=f"Synthetic {item['manifest']['product']['mpn']}; Body Material: brass", page_number=1,
        ) for item in batch["items"]]
        document = document.model_copy(update={
            "paragraphs": paragraphs, "raw_text": "\n".join(paragraph.text for paragraph in paragraphs),
        })
        cache.update(document=document.model_dump(mode="json"),
                     document_sha256=hashlib.sha256(document.model_dump_json().encode()).hexdigest())
        put(store, key, cache)
        recovery["cached_documents"][key] = hashlib.sha256(store.read_bytes(key)[0]).hexdigest()
    put(store, real_pilot.RECOVERY_KEY, recovery)
    return case


@pytest.mark.parametrize("web", [True, False])
def test_synthetic_all_four_real_worker_preserves_prior_results(four_case, monkeypatch, web):
    store, batch, approval, candidate = four_case
    # Synthetic maximum-only reservations are not a real measured capacity grant.
    # The opt-in private gate requires the independently approved measured plan.
    maximum = 8 * 27952 + 4 * 26000
    candidate["capacity_approval"].update(max_input_tokens=maximum, additional_input_tokens=maximum)
    put(store, real_pilot.FOUR_PRODUCT_KEY, candidate)
    before = {key: json.loads(raw) for key, (raw, _) in all_records(store).items()
              if key.endswith(".json") and key != real_pilot.FOUR_PRODUCT_KEY}
    baseline = read_json(store, real_pilot.BUDGET_KEY)[0]
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    if web:
        captures, timeline, measurements = profile_worker(monkeypatch, store, batch, candidate, response_seconds=22)
        calls, requests, searches, pages = captures
        assert measurements["simulated_worker_elapsed_seconds"] == 512
        assert measurements["measured_local_worker_seconds"] > 0
        assert measurements["worker_elapsed_seconds"] > 512
        assert measurements["forecast_including_local_seconds"] > 548
        from scripts import four_product_continuation as helper
        from tests.test_four_product_continuation import pacing_fixture

        proof = pacing_fixture()[0]
        actual_plan = [
            helper.payload_entry(key, tier, requests[index * 3 + offset])
            for index, key in enumerate(candidate["execution_order"])
            for offset, tier in enumerate(("internal_pdf", "vendor_table", "manufacturer_web"))
        ]
        preflights = measurements.pop("provider_preflights")
        assert len(preflights["web_retrieval"]) == 6
        assert [entry["item_key"] for entry in preflights["web_retrieval"]] == [
            key for key in candidate["execution_order"] for _ in range(candidate["page_limits"][key])
        ]
        for entry, prepared in zip(preflights["model"], actual_plan):
            entry.update(item_key=prepared["item_key"], tier=prepared["tier"])
        helper.validate_provider_preflights(preflights, actual_plan, candidate["public_web_policy"])
        for fault in (
            "send", "sdk", "payload", "missing_web", "missing_page", "page_item",
            "page_url", "page_sdk", "page_dns", "page_content", "page_headers",
            "page_wire", "page_raw_url", "duplicate_page",
        ):
            broken = copy.deepcopy(preflights)
            if fault == "send":
                broken["model"][0]["receipt"]["provider_send_performed"] = True
            elif fault == "sdk":
                broken["model"][0]["receipt"]["sdk_version"] = "0.0.0"
            elif fault == "payload":
                broken["model"][0]["payload_sha256"] = "0" * 64
            elif fault == "missing_web":
                broken["webiq"].pop()
            elif fault == "missing_page":
                broken["web_retrieval"].pop()
            else:
                page = broken["web_retrieval"][0]
                if fault == "page_item":
                    page["item_key"] = candidate["execution_order"][-1]
                elif fault == "page_url":
                    page["url_sha256"] = "0" * 64
                elif fault == "page_sdk":
                    page["receipt"]["sdk_version"] = "0.0.0"
                elif fault == "page_dns":
                    page["receipt"]["dns_calls"] = 1
                elif fault == "page_content":
                    page["receipt"]["source_content_verified"] = True
                elif fault == "page_headers":
                    page["receipt"]["header_names"].append("authorization")
                elif fault == "page_wire":
                    page["receipt"]["wire_request_sha256"] = "not-a-digest"
                elif fault == "page_raw_url":
                    page["receipt"]["url"] = "https://example.com/private"
                elif fault == "duplicate_page":
                    broken["web_retrieval"][1] = copy.deepcopy(page)
            with pytest.raises(ValueError):
                helper.validate_provider_preflights(broken, actual_plan, candidate["public_web_policy"])
        proof.update(measurements)
        proof["requests"] = [
            {"item_key": entry["item_key"], "tier": entry["tier"], "payload_sha256": entry["payload_sha256"],
             "input_byte_bound": entry["input_tokens"], "output_tokens": entry["output_tokens"], **timing}
            for entry, timing in zip(actual_plan, timeline)
        ]
        helper.validate_pacing(
            proof, actual_plan, execution_order=candidate["execution_order"], interval_seconds=31,
            duration_assumptions=candidate["capacity_approval"]["duration_assumptions"],
        )
    else:
        calls, requests, searches, pages = reproduction(monkeypatch, batch, candidate)
    if not web:
        from backend.core.config import settings
        monkeypatch.setattr(settings, "WEBIQ_API_KEY", None)
        monkeypatch.delenv("WEBIQ_API_KEY", raising=False)
        monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient", WebIQSearchClient)
        monkeypatch.setattr(WebIQSearchClient, "search", lambda *a, **k: pytest.fail("Missing key must not search"))
        monkeypatch.setattr("backend.core.websearch.fetch_original_page",
                            lambda *a, **k: pytest.fail("Missing key must not fetch"))
        from backend.extract import ExecutionConfigurationError

        with pytest.raises(ExecutionConfigurationError, match="Native WebIQ request validation"):
            worker.run_batch(store, batch["id"], concurrency=1, item_limit=4)
        assert len(calls) == len(requests) == 2 and not searches and not pages
        ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
        assert ledger["attempted"]["inference"] == baseline["attempted"]["inference"] + 2
        assert ledger["attempted"]["analysis"] == baseline["attempted"]["analysis"]
        assert ledger["reserved"]["analysis_pages"] == baseline["reserved"]["analysis_pages"]
        assert ledger["reserved"]["input_tokens"] == baseline["reserved"]["input_tokens"] + sum(
            request.input_bound for request in requests)
        assert ledger["reserved"]["output_tokens"] == baseline["reserved"]["output_tokens"] + 4096
        assert ledger["attempted"]["search"] == baseline["attempted"]["search"]
        assert ledger["attempted"]["web_retrieval"] == baseline["attempted"]["web_retrieval"]
        assert len(ledger["executions"]) == len(baseline["executions"]) + 1
        assert all(ledger["executions"][key] == value for key, value in baseline["executions"].items())
        assert all(ledger["reservations"][key] == value for key, value in baseline["reservations"].items())
        for item_key in candidate["execution_order"]:
            state = read_json(store, f"items/{batch['id']}/{item_key}.json")[0]
            assert state["previous_attempt"] == before[f"items/{batch['id']}/{item_key}.json"]
        assert read_json(store, f"items/{batch['id']}/{candidate['execution_order'][0]}.json")[0]["state"] == "failed"
        for item_key in candidate["execution_order"][1:]:
            assert read_json(store, f"items/{batch['id']}/{item_key}.json")[0]["state"] == "recovery_ready"
        for key, value in before.items():
            if key != real_pilot.BUDGET_KEY and not key.startswith(("items/", "batches/")):
                assert read_json(store, key)[0] == value
        return
    assert len(calls) == len(requests) == 12
    assert len(searches) == 4 and len(pages) == 6
    assert [payload["evidence_groups"][0]["source_tier"] for payload in calls] == [
        "internal_pdf", "vendor_table", "manufacturer_web"] * 4
    expected_products = [
        next(item["manifest"]["product"] for item in batch["items"] if item["item_key"] == key)
        for key in candidate["execution_order"] for _ in range(3)
    ]
    assert [payload["product"] for payload in calls] == expected_products
    ledger = assert_preserved(store, before, baseline, batch, candidate)
    assert ledger["reserved"]["input_tokens"] - baseline["reserved"]["input_tokens"] == sum(
        request.input_bound for request in requests)
    assert ledger["reserved"]["output_tokens"] - baseline["reserved"]["output_tokens"] == len(requests) * 2048
    assert len(read_workbook(BatchService(store).export(batch["id"], batch["owner"]))["Attempts"]) >= 8
    after = all_records(store)
    worker.run_batch(store, batch["id"], concurrency=1, item_limit=4)
    assert all_records(store) == after


def test_four_product_fatal_global_guard_stops_remaining_three(four_case, monkeypatch):
    store, batch, _, candidate = four_case
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    reproduction(monkeypatch, batch, candidate)
    called = []

    def denied(self, *args, **kwargs):
        called.append(kwargs["item_key"])
        raise ValueError("REPRODUCTION global authority denial")

    monkeypatch.setattr(real_pilot.RealPilotGuard, "reserve", denied)
    from backend.extract import ExecutionConfigurationError
    with pytest.raises(ExecutionConfigurationError):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=4)
    assert called and set(called) == {batch["items"][0]["item_key"]}
    for item in batch["items"][1:]:
        assert read_json(store, f"items/{batch['id']}/{item['item_key']}.json")[0]["state"] == "recovery_ready"


@pytest.mark.parametrize("provider", ["model", "webiq", "web_retrieval"])
def test_native_sdk_preflight_failure_is_fatal_before_provider_reservation(four_case, monkeypatch, provider):
    store, batch, _, candidate = four_case
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    assert_sdk_failure_before_reservation(monkeypatch, store, batch, candidate, provider)
