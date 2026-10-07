"""Opt-in actual post-PREP snapshot, REAL worker and REAL guard, zero network.

DOCINTEL_TEST_FOUR_PRODUCT_WORK: existing owner-only retained release root.
DOCINTEL_TEST_FOUR_PRODUCT_SNAPSHOT: actual post-PREP root JSON snapshot filename.
DOCINTEL_TEST_FOUR_PRODUCT_SCOPE: root JSON containing amendment_scope/payload_plan.
DOCINTEL_TEST_FOUR_PRODUCT_DOCUMENTS: existing approved local document directory.
DOCINTEL_TEST_FOUR_PRODUCT_OUTPUT: optional create-once root gate filename.

Nothing here installs authority or creates capacity approval. The supplied scope
must already contain the independently approved exact measured capacity receipt.
All completions/pages are REPRODUCTION ONLY, never findings or real outputs.
"""

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import pytest

from backend import batch_worker as worker, real_pilot
from backend.batch import BatchService
from backend.batch_store import Missing, read_json, write_json
from backend.core.websearch_webiq import WebIQSearchClient
from backend.core.websearch import WebIQSearchResult, preflight_original_page as native_page_preflight
from backend.core.llm import preflight_structured_request as native_model_preflight
from backend.workbooks import read_workbook
from scripts import four_product_continuation as helper
from tests.test_gapfill_rerun import install_reproduction, runtime
from tests.test_row_rerun import WorkerMemoryStore, all_records, network_blocked, put


LABEL = "REPRODUCTION ONLY: invented offline responses/pages; not product findings or recovered output."


def private_file(root, name):
    assert name and Path(name).name == name
    candidate = root / name
    assert candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_mode & 0o077 == 0
    return helper.release.private_json(candidate, max_bytes=64 * 1024 * 1024)


def hydrate_documents(store, batch, root):
    needed = {source["blob"]: source["sha256"] for item in batch["items"]
              for source in item["sources"] if source["kind"] == "blob"}
    for candidate in root.iterdir():
        if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.lower() not in {".pdf", ".xlsx"}:
            continue
        raw = candidate.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        for key, expected in needed.items():
            if expected == digest:
                try:
                    existing, _ = store.read_bytes(key)
                except Missing:
                    store.write_bytes(key, raw)
                else:
                    assert hashlib.sha256(existing).hexdigest() == expected
    assert all(hashlib.sha256(store.read_bytes(key)[0]).hexdigest() == digest for key, digest in needed.items())


def reproduction(monkeypatch, batch, scope, *, timeline=None, response_seconds=0,
                 web_timeline=None, web_network_seconds=0, preflight_log=None):
    calls, requests, discovery, retrieval = install_reproduction(monkeypatch, batch)
    if preflight_log is None:
        preflight_log = {"model": [], "webiq": [], "web_retrieval": []}

    def model_preflight(system, user, schema, **kwargs):
        assert requests[-1].system == system and requests[-1].user == user
        receipt = native_model_preflight(system, user, schema, **kwargs)
        preflight_log["model"].append({"payload_sha256": requests[-1].version, "receipt": receipt})
        return receipt

    def search_preflight(self, query, allowed_domains=None, *, authorized=False):
        client = WebIQSearchClient(
            endpoint=scope["web_environment"]["WEBIQ_ENDPOINT"],
            api_key="SYNTHETIC-FOUR-PRODUCT-NO-SEND-KEY",
        )
        receipt = client.preflight_search(query, allowed_domains, authorized=authorized)
        preflight_log["webiq"].append({
            "item_key": scope["execution_order"][len(preflight_log["webiq"])],
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(), "receipt": receipt,
        })
        return receipt

    def page_preflight(url, **kwargs):
        receipt = native_page_preflight(url, **kwargs)
        preflight_log["web_retrieval"].append({
            "item_key": scope["execution_order"][len(discovery) - 1],
            "url_sha256": hashlib.sha256(url.encode()).hexdigest(), "receipt": receipt,
        })
        return receipt

    monkeypatch.setattr(worker, "preflight_structured_request", model_preflight)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.preflight_search", search_preflight, raising=False)
    monkeypatch.setattr("backend.core.websearch.preflight_original_page", page_preflight)

    def search(self, query, allowed_domains=None, **kwargs):
        discovery.append(query)
        item_key = scope["execution_order"][len(discovery) - 1]
        if web_timeline is not None:
            start = worker.monotonic()
            worker.sleep(web_network_seconds / 4)
            web_timeline.append({"item_key": item_key, "start_seconds": start, "end_seconds": worker.monotonic()})
        return [WebIQSearchResult(
            url="https://" + allowed_domains[0] + f"/reproduction-only-{index}",
            title=LABEL, content="DISCOVERY ONLY, NEVER ATTRIBUTE EVIDENCE",
            retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ) for index in range(scope["page_limits"][item_key])]

    def fetch(url, **kwargs):
        retrieval.append(url)
        text = LABEL + " " + " ".join(
            f"{public['manufacturer']} {public['mpn']}" for public in scope["public_web_policy"]["items"].values())
        return SimpleNamespace(
            final_url=url, text=text, media_type="text/html",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    monkeypatch.setattr("backend.core.websearch.fetch_original_page", fetch)
    monkeypatch.setattr("backend.core.websearch_webiq.WebIQSearchClient.search", search)
    if timeline is not None:
        complete = worker.LLMClient.complete_structured

        def timed(self, *args, **kwargs):
            start = worker.monotonic()
            result = complete(self, *args, **kwargs)
            worker.sleep(response_seconds)
            timeline.append({"start_seconds": start, "end_seconds": worker.monotonic()})
            return result

        monkeypatch.setattr(worker.LLMClient, "complete_structured", timed)
    return calls, requests, discovery, retrieval


def assert_sdk_failure_before_reservation(monkeypatch, store, batch, scope, provider):
    from backend.extract import ExecutionConfigurationError
    from backend.sdk_preflight import SDKPreflightError

    before = read_json(store, real_pilot.BUDGET_KEY)[0]
    records = {key: json.loads(raw) for key, (raw, _) in all_records(store).items() if key.endswith(".json")}
    with monkeypatch.context() as context:
        calls, requests, searches, pages = reproduction(context, batch, scope)

        def incompatible(*args, **kwargs):
            raise SDKPreflightError(provider, "SyntheticNativeContractFailure")

        if provider == "model":
            context.setattr(worker, "preflight_structured_request", incompatible)
        elif provider == "webiq":
            context.setattr("backend.core.websearch_webiq.WebIQSearchClient.preflight_search", incompatible)
        else:
            assert provider == "web_retrieval"
            context.setattr("backend.core.websearch.preflight_original_page", incompatible)
        with pytest.raises(ExecutionConfigurationError):
            worker.run_batch(store, batch["id"], concurrency=1, item_limit=2 if scope.get("worker_slices") else 4)
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert ledger["attempted"]["inference"] - before["attempted"]["inference"] == (0 if provider == "model" else 2)
    assert ledger["attempted"]["search"] - before["attempted"]["search"] == (1 if provider == "web_retrieval" else 0)
    assert ledger["attempted"]["web_retrieval"] == before["attempted"]["web_retrieval"]
    assert ledger["attempted"]["analysis"] == before["attempted"]["analysis"]
    assert ledger["reserved"]["analysis_pages"] == before["reserved"]["analysis_pages"]
    assert ledger["reserved"]["input_tokens"] == before["reserved"]["input_tokens"] + sum(
        request.input_bound for request in requests[:len(calls)])
    assert ledger["reserved"]["output_tokens"] == before["reserved"]["output_tokens"] + 2048 * len(calls)
    assert len(searches) == (1 if provider == "web_retrieval" else 0) and not pages
    assert len(calls) == (0 if provider == "model" else 2)
    assert all(ledger["reservations"][key] == value for key, value in before["reservations"].items())
    assert all(ledger["executions"][key] == value for key, value in before["executions"].items())
    for item_key in scope["execution_order"][1:]:
        assert read_json(store, f"items/{batch['id']}/{item_key}.json")[0]["state"] == "recovery_ready"
    for key, value in records.items():
        if key != real_pilot.BUDGET_KEY and not key.startswith(("items/", "batches/")):
            assert read_json(store, key)[0] == value


def profile_worker(monkeypatch, store, batch, scope, *, response_seconds):
    assumptions = scope["capacity_approval"]["duration_assumptions"]
    clock, timeline, web_timeline = [assumptions["startup_bookkeeping_seconds"]], [], []
    preflights = {"model": [], "webiq": [], "web_retrieval": []}
    monkeypatch.setattr(worker, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    captures = reproduction(
        monkeypatch, batch, scope, timeline=timeline, response_seconds=response_seconds,
        web_timeline=web_timeline, web_network_seconds=assumptions["web_network_allowance_seconds"],
        preflight_log=preflights,
    )
    write = worker.write_json
    item_paths = {f"items/{batch['id']}/{key}.json": key for key in scope["execution_order"]}
    item_starts, item_seconds = {}, {}

    def measured_write(write_store, key, value, *args, **kwargs):
        item_key = item_paths.get(key) if write_store is store else None
        if item_key and value.get("state") == "running":
            item_starts.setdefault(item_key, perf_counter())
        result = write(write_store, key, value, *args, **kwargs)
        if item_key in item_starts and value.get("state") != "running" and value.get("finished_at"):
            item_seconds[item_key] = perf_counter() - item_starts.pop(item_key)
        return result

    monkeypatch.setattr(worker, "write_json", measured_write)
    sliced = scope.get("worker_slices")
    slices = []
    local_seconds, simulated_seconds = 0, 0
    for index, items in enumerate(sliced or [scope["execution_order"]]):
        clock[0] = assumptions["startup_bookkeeping_seconds"]
        model_offset, web_offset = len(timeline), len(web_timeline)
        started = perf_counter()
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=len(items))
        measured = perf_counter() - started
        local_seconds += measured
        simulated_seconds += clock[0]
        if sliced:
            planned = [
                helper.payload_entry(key, tier, captures[1][model_offset + item_index * 3 + offset])
                for item_index, key in enumerate(items)
                for offset, tier in enumerate(("internal_pdf", "vendor_table", "manufacturer_web"))
            ]
            forecast = (5 * max(31, assumptions["model_response_allowance_seconds"])
                        + assumptions["model_response_allowance_seconds"] + assumptions["startup_bookkeeping_seconds"]
                        + assumptions["web_network_allowance_seconds"] / 2 + measured)
            slices.append({
                "slice_index": index, "item_keys": items,
                "requests": [
                    {"item_key": entry["item_key"], "tier": entry["tier"], "payload_sha256": entry["payload_sha256"],
                     "input_byte_bound": entry["input_tokens"], "output_tokens": entry["output_tokens"], **timing}
                    for entry, timing in zip(planned, timeline[model_offset:])
                ],
                "web_delay_events": copy.deepcopy(web_timeline[web_offset:]),
                "simulated_worker_elapsed_seconds": clock[0], "measured_local_worker_seconds": measured,
                "worker_elapsed_seconds": clock[0] + measured, "forecast_including_local_seconds": forecast,
                "remaining_margin_seconds": 600 - forecast,
                "local_measurement": "perf_counter_whole_real_worker_with_memory_store",
                "remote_storage_latency_measured": False,
            })
    return captures, timeline, {
        **({"slices": slices} if sliced else {}),
        "web_delay_events": web_timeline, "simulated_worker_elapsed_seconds": simulated_seconds,
        "measured_local_worker_seconds": local_seconds,
        "worker_elapsed_seconds": simulated_seconds + local_seconds,
        "forecast_including_local_seconds": (sum(value["forecast_including_local_seconds"] for value in slices)
                                             if sliced else assumptions["forecast_seconds"] + local_seconds),
        "local_measurement": "perf_counter_whole_real_worker_with_memory_store",
        "remote_storage_latency_measured": False,
        "web_allowance_is_hard_deadline": False, "phase_timeouts_bound_dns": False,
        "measured_item_local_seconds": dict(item_seconds),
        "measured_four_item_subtotal_seconds": sum(item_seconds.values()),
        "measured_prior_two_item_subtotal_seconds": sum(item_seconds[key] for key in ("row-2", "row-3")),
        "measured_shared_local_seconds": local_seconds - sum(item_seconds.values()),
        "prior_two_comparison": "same_run_mueller_item_subtotal_not_historical_replay",
        "remaining_margin_seconds": (min(value["remaining_margin_seconds"] for value in slices)
                                     if sliced else 600 - assumptions["forecast_seconds"] - local_seconds),
        "provider_preflights": copy.deepcopy(preflights),
    }


def assert_preserved(store, original, baseline, batch, candidate):
    ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert len(ledger["executions"]) == len(baseline["executions"]) + (2 if candidate.get("worker_slices") else 1)
    assert all(ledger["executions"][key] == value for key, value in baseline["executions"].items())
    assert all(ledger["reservations"][key] == value for key, value in baseline["reservations"].items())
    assert ledger["attempted"]["analysis"] == baseline["attempted"]["analysis"]
    assert ledger["reserved"]["analysis_pages"] == baseline["reserved"]["analysis_pages"]
    for key, value in original.items():
        if key != real_pilot.BUDGET_KEY and not key.startswith(("items/", "batches/")):
            assert read_json(store, key)[0] == value
    service = BatchService(store)
    for item_key in candidate["selected_item_keys"]:
        state = read_json(store, f"items/{batch['id']}/{item_key}.json")[0]
        assert state["previous_attempt"] == original[f"items/{batch['id']}/{item_key}.json"]
        assert state["result_key"].endswith("/attempts/" + real_pilot._sha256(candidate) + ".json")
        detail = service.detail(batch["id"], item_key, batch["owner"])
        assert detail["attempt_history"]
        with pytest.raises(Exception):
            service.detail(batch["id"], item_key, "REPRODUCTION-OTHER-OWNER")
    assert "Attempts" in read_workbook(service.export(batch["id"], batch["owner"]))
    with pytest.raises(Exception):
        service.export(batch["id"], "REPRODUCTION-OTHER-OWNER")
    return ledger


def test_actual_postprep_four_product_private_gate(monkeypatch):
    root_value = os.environ.get("DOCINTEL_TEST_FOUR_PRODUCT_WORK")
    if not root_value:
        pytest.skip("Actual post-PREP private snapshot and exact capacity approval not supplied")
    root = Path(root_value).resolve()
    assert root.is_dir() and root.stat().st_mode & 0o077 == 0
    snapshot_name = os.environ["DOCINTEL_TEST_FOUR_PRODUCT_SNAPSHOT"]
    snapshot = private_file(root, snapshot_name)
    bundle = private_file(root, os.environ["DOCINTEL_TEST_FOUR_PRODUCT_SCOPE"])
    scope = bundle["amendment_scope"]
    assert set(scope) == ((real_pilot.FOUR_PRODUCT_FIELDS | real_pilot.FOUR_PRODUCT_SLICE_FIELDS)
                          if scope.get("worker_slices") else real_pilot.FOUR_PRODUCT_FIELDS) - helper.TIME_FIELDS
    if scope.get("worker_slices"):
        assert scope["slice_authorization"] == private_file(root, helper.CAPACITY_OWNER_AUTHORIZATION_FILE)
    assert scope["capacity_approval"]["plan_sha256"] == helper.sha(bundle["payload_plan"])
    assert len(bundle["payload_plan"]) == 12
    original = helper.decode_records(snapshot)
    records = {key: (base64.b64decode(value["base64"], validate=True), value["version"])
               for key, value in snapshot["records"].items()}
    store = WorkerMemoryStore(records)
    approval = original[real_pilot.APPROVAL_KEY]
    batch = original[f"batches/{approval['batch_id']}.json"]
    identity_authorization, _ = helper.preparation_identity_authorization(
        root, approval, private_file(root, "target.json"),
    )
    assert helper.instant(identity_authorization["recorded_after_confirmation_at"]) <= helper.instant(
        original[real_pilot.FOUR_PRODUCT_PREP_PREFIX + "authorization.json"]["approved_at"],
    )
    assert [item["manifest"]["product"]["item_id"] for item in batch["items"]] == [
        "PIMITEM-213030", "PIMITEM-245747", "PIMITEM-225830", "PIMITEM-221315",
    ]
    hydrate_documents(store, batch, Path(os.environ["DOCINTEL_TEST_FOUR_PRODUCT_DOCUMENTS"]))
    completed = original[scope["prep_receipt_key"]]
    assert completed["cache_key"] == real_pilot.FOUR_PRODUCT_PREP_CACHE_KEY
    for item in batch["items"][2:]:
        binding = next(source for source in item["sources"] if source["source_id"] == "av-source-4")
        document, origin = worker.BatchProcessor(store).cached(
            completed["cache_key"], binding, "batchblob:///documents/av-source-4.pdf",
        )
        assert origin == "ford_preparation_analysis_first_five_pages"
        assert document.model_dump(mode="json") == original[real_pilot.FOUR_PRODUCT_PREP_PREFIX + "parsed.json"]
    ready = max(datetime.now(timezone.utc),
                datetime.fromisoformat(original[real_pilot.ROW_RERUN_KEY]["operating_expires_at"]) + timedelta(seconds=1))
    candidate = {
        **scope, "readiness_at": ready.isoformat(),
        "operating_expires_at": (ready + timedelta(seconds=5400)).isoformat(),
        "not_before": ready.isoformat(), "expires_at": (ready + timedelta(seconds=1200)).isoformat(),
    }
    write_json(store, real_pilot.FOUR_PRODUCT_KEY, candidate)
    runtime(monkeypatch, approval, candidate)
    helper.configuration_packet(approval, candidate)
    baseline = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert real_pilot.validate_four_product(candidate, approval, batch, baseline, store) == candidate
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    ready_records = all_records(store)
    pacing = copy.deepcopy(bundle["pacing_profile"])
    assert pacing["duration_assumptions"] == candidate["capacity_approval"]["duration_assumptions"]
    assumptions = pacing["duration_assumptions"]
    simulated_response = pacing["simulation_response_seconds"]
    assert type(simulated_response) in (int, float) and 0 < simulated_response <= assumptions["model_response_allowance_seconds"]
    captures, timeline, measurements = profile_worker(
        monkeypatch, store, batch, scope, response_seconds=simulated_response,
    )
    calls, requests, discovery, retrieval = captures
    completed_requests = list(requests)
    execution_items = [next(item for item in batch["items"] if item["item_key"] == key)
                       for key in candidate["execution_order"]]
    assert len(calls) == len(requests) == 12
    assert [payload["evidence_groups"][0]["source_tier"] for payload in calls] == [
        "internal_pdf", "vendor_table", "manufacturer_web",
    ] * 4
    assert len(discovery) == 4 and len(retrieval) == 6
    for query, item in zip(discovery, execution_items):
        public = scope["public_web_policy"]["items"][item["item_key"]]
        assert public["manufacturer"] in query and public["mpn"] in query
        assert approval["owner"] not in query and approval["batch_id"] not in query
    assert all("DISCOVERY ONLY" not in request.user for request in requests)
    actual_plan = [
        helper.payload_entry(item["item_key"], tier, requests[index * 3 + offset])
        for index, item in enumerate(execution_items)
        for offset, tier in enumerate(("internal_pdf", "vendor_table", "manufacturer_web"))
    ]
    preflights = measurements.pop("provider_preflights")
    for entry, prepared in zip(preflights["model"], actual_plan):
        entry.update(item_key=prepared["item_key"], tier=prepared["tier"])
    helper.validate_provider_preflights(preflights, actual_plan, scope["public_web_policy"])
    planned = {(entry["item_key"], entry["tier"]): entry for entry in bundle["payload_plan"]}
    assert all(entry["input_tokens"] <= planned[(entry["item_key"], entry["tier"])]["input_tokens"]
               and entry["output_tokens"] == planned[(entry["item_key"], entry["tier"])]["output_tokens"]
               for entry in actual_plan)
    assert sum(request.input_bound for request in requests) <= scope["capacity_approval"]["max_input_tokens"]
    assert sum(request.accounting["max_output_tokens"] for request in requests) == 24576
    assert len(timeline) == 12
    pacing["requests"] = [
        {"item_key": entry["item_key"], "tier": entry["tier"], "payload_sha256": entry["payload_sha256"],
         "input_byte_bound": entry["input_tokens"], "output_tokens": entry["output_tokens"], **timing}
        for entry, timing in zip(actual_plan, timeline)
    ]
    pacing.update(measurements)
    helper.validate_pacing(pacing, actual_plan, execution_order=candidate["execution_order"],
                           interval_seconds=candidate["inference_interval_seconds"],
                           duration_assumptions=candidate["capacity_approval"]["duration_assumptions"],
                           worker_slices=candidate.get("worker_slices"))
    ledger = assert_preserved(store, original, baseline, batch, candidate)
    assert ledger["attempted"]["inference"] - baseline["attempted"]["inference"] == 12
    assert ledger["reserved"]["input_tokens"] - baseline["reserved"]["input_tokens"] == sum(
        request.input_bound for request in requests)
    assert ledger["reserved"]["output_tokens"] - baseline["reserved"]["output_tokens"] == 24576
    after = all_records(store)
    worker.run_batch(store, batch["id"], concurrency=1, item_limit=2 if scope.get("worker_slices") else 4)
    assert all_records(store) == after
    missing_store = WorkerMemoryStore(ready_records)
    with monkeypatch.context() as context:
        missing_calls, _, searches, pages = reproduction(context, batch, scope)
        from backend.core.config import settings
        context.setattr(settings, "WEBIQ_API_KEY", None)
        context.delenv("WEBIQ_API_KEY", raising=False)
        context.setattr("backend.core.websearch_webiq.WebIQSearchClient", WebIQSearchClient)
        context.setattr(WebIQSearchClient, "search", lambda *a, **k: pytest.fail("Absent key cannot search"))
        context.setattr("backend.core.websearch.fetch_original_page", lambda *a, **k: pytest.fail("Absent key cannot fetch"))
        from backend.extract import ExecutionConfigurationError

        with pytest.raises(ExecutionConfigurationError, match="Native WebIQ request validation"):
            worker.run_batch(missing_store, batch["id"], concurrency=1, item_limit=2 if scope.get("worker_slices") else 4)
    assert len(missing_calls) == 2 and not searches and not pages
    missing_ledger = read_json(missing_store, real_pilot.BUDGET_KEY)[0]
    assert missing_ledger["attempted"]["inference"] == baseline["attempted"]["inference"] + 2
    assert missing_ledger["attempted"]["analysis"] == baseline["attempted"]["analysis"]
    assert missing_ledger["reserved"]["analysis_pages"] == baseline["reserved"]["analysis_pages"]
    assert missing_ledger["attempted"]["search"] == missing_ledger["attempted"]["web_retrieval"] == 0
    assert missing_ledger["reserved"]["output_tokens"] == baseline["reserved"]["output_tokens"] + 4096
    assert len(missing_ledger["executions"]) == len(baseline["executions"]) + 1
    assert all(missing_ledger["executions"][key] == value for key, value in baseline["executions"].items())
    assert all(missing_ledger["reservations"][key] == value for key, value in baseline["reservations"].items())
    for item_key in scope["execution_order"]:
        state = read_json(missing_store, f"items/{batch['id']}/{item_key}.json")[0]
        assert state["previous_attempt"] == original[f"items/{batch['id']}/{item_key}.json"]
    assert read_json(missing_store, f"items/{batch['id']}/{scope['execution_order'][0]}.json")[0]["state"] == "failed"
    for item_key in scope["execution_order"][1:]:
        assert read_json(missing_store, f"items/{batch['id']}/{item_key}.json")[0]["state"] == "recovery_ready"
    for key, value in original.items():
        if key != real_pilot.BUDGET_KEY and not key.startswith(("items/", "batches/")):
            assert read_json(missing_store, key)[0] == value
    fatal_store, fatal = WorkerMemoryStore(ready_records), []
    with monkeypatch.context() as context:
        def deny(self, *args, **kwargs):
            fatal.append(kwargs["item_key"])
            raise ValueError("REPRODUCTION fatal global guard")
        context.setattr(real_pilot.RealPilotGuard, "reserve", deny)
        from backend.extract import ExecutionConfigurationError
        with pytest.raises(ExecutionConfigurationError):
            worker.run_batch(fatal_store, batch["id"], concurrency=1, item_limit=2 if scope.get("worker_slices") else 4)
    assert fatal and set(fatal) == {batch["items"][0]["item_key"]}
    for item in batch["items"][1:]:
        assert read_json(fatal_store, f"items/{batch['id']}/{item['item_key']}.json")[0]["state"] == "recovery_ready"
    for provider in ("model", "webiq", "web_retrieval"):
        assert_sdk_failure_before_reservation(
            monkeypatch, WorkerMemoryStore(ready_records), batch, scope, provider,
        )
    output = os.environ.get("DOCINTEL_TEST_FOUR_PRODUCT_OUTPUT")
    if output:
        assert Path(output).name == output and output.startswith("four-product-")
        revision = helper.release.command(["git", "rev-parse", "HEAD"], cwd=helper.release.ROOT).decode().strip()
        helper.verify_running_source(revision)
        baseline_receipt = private_file(root, helper.baseline_path(root).name)
        helper.source_boundary(revision, baseline_receipt["source_review"])
        helper.release.save_once(root / output, {
            "passed": True, "no_live_operations": True, "label": LABEL, "source_revision": revision,
            "owner": batch["owner"], "batch_id": batch["id"],
            "postprep_snapshot_sha256": helper.digest_file(root / snapshot_name),
            "scope_sha256": helper.sha(scope), "amendment_scope": scope,
            "payload_plan": bundle["payload_plan"], "remote_object_sha256": {key: helper.sha(value) for key, value in original.items()},
            "actual_payload_plan": actual_plan,
            "provider_preflights": preflights,
            "checks": {name: True for name in helper.GATE_CHECKS}, "historical_forecasts_stacked": False,
            "actual_complete_input_tokens": sum(request.input_bound for request in completed_requests),
            "actual_complete_output_tokens": 24576,
            "pacing_evidence": pacing,
        })
