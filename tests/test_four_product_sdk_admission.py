"""No-send failures stop the four-product slice before provider reservations."""

import pytest

from backend import batch_worker as worker, real_pilot
from backend.batch_store import read_json
from backend.extract import ExecutionConfigurationError
from backend.sdk_preflight import SDKPreflightError
from tests.test_four_product_worker import (
    configured, recovery_case, four_case, final_case, row_case, network_blocked,
)
from tests.test_four_product_private_gate import reproduction
from tests.test_four_product_continuation import put


def queue(four_case, monkeypatch):
    store, batch, _, scope = four_case
    captures = reproduction(monkeypatch, batch, scope)
    put(store, f"batches/{batch['id']}.json", {**batch, "state": "queued"})
    return store, batch, read_json(store, real_pilot.BUDGET_KEY)[0], captures


def test_invalid_native_model_request_never_reserves_or_constructs_live_client(four_case, monkeypatch):
    store, batch, before, captures = queue(four_case, monkeypatch)
    seen = []

    def invalid(system, user, schema, **options):
        seen.append((system, user, schema, options))
        assert read_json(store, real_pilot.BUDGET_KEY)[0]["attempted"] == before["attempted"]
        raise SDKPreflightError("model", "TypeError")

    monkeypatch.setattr(worker, "preflight_structured_request", invalid)
    with pytest.raises(ExecutionConfigurationError, match="Native model request validation"):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=4)
    after = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert after["attempted"] == before["attempted"]
    assert after["reserved"]["input_tokens"] == before["reserved"]["input_tokens"]
    assert seen and seen[0][3]["sdk_max_retries"] == 0
    calls, _, searches, pages = captures
    assert not calls and not searches and not pages


def test_invalid_native_web_request_is_fatal_before_search_reservation(four_case, monkeypatch):
    store, batch, before, captures = queue(four_case, monkeypatch)
    from backend.core import websearch_webiq

    def invalid(self, query, allowed_domains=None, *, authorized=False):
        assert authorized and allowed_domains
        assert read_json(store, real_pilot.BUDGET_KEY)[0]["attempted"]["search"] == before["attempted"]["search"]
        raise SDKPreflightError("webiq", "TypeError")

    monkeypatch.setattr(websearch_webiq.WebIQSearchClient, "preflight_search", invalid)
    with pytest.raises(ExecutionConfigurationError, match="Native WebIQ request validation"):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=4)
    after = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert after["attempted"]["inference"] == before["attempted"]["inference"] + 2
    assert after["attempted"]["search"] == before["attempted"]["search"]
    assert after["attempted"]["web_retrieval"] == before["attempted"]["web_retrieval"]
    calls, _, searches, pages = captures
    assert len(calls) == 2 and not searches and not pages
    next_item = read_json(store, f"items/{batch['id']}/row-4.json")[0]
    assert next_item["state"] == "recovery_ready"


def test_last_moment_native_failure_is_not_reported_as_a_model_send(four_case, monkeypatch):
    store, batch, before, captures = queue(four_case, monkeypatch)
    instances = []
    real_processor = worker.RealBatchProcessor

    def capture_processor(*args, **kwargs):
        instance = real_processor(*args, **kwargs)
        instances.append(instance)
        return instance

    def blocked(self, *args, **kwargs):
        self.last_usage = None
        raise SDKPreflightError("model", "RequestChanged")

    monkeypatch.setattr(worker, "RealBatchProcessor", capture_processor)
    monkeypatch.setattr(worker.LLMClient, "complete_structured", blocked)
    with pytest.raises(ExecutionConfigurationError, match="Native model request revalidation"):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=4)
    after = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert after["attempted"]["inference"] == before["attempted"]["inference"] + 1
    assert after["actual_usage"] == before["actual_usage"]
    entry = instances[0].inference_provenance[-1]
    assert entry["method"] == "native_sdk_preflight_blocked"
    assert entry["new_model_call"] is False and entry["usage"] is None
    assert entry["sdk_preflight"]["provider_send_performed"] is False
    calls, _, searches, pages = captures
    assert not calls and not searches and not pages


def test_invalid_native_page_request_is_fatal_before_retrieval_reservation(four_case, monkeypatch):
    store, batch, before, captures = queue(four_case, monkeypatch)

    def invalid(url, **kwargs):
        assert kwargs["authorized"] is True
        ledger = read_json(store, real_pilot.BUDGET_KEY)[0]
        assert ledger["attempted"]["web_retrieval"] == before["attempted"]["web_retrieval"]
        raise SDKPreflightError("web_retrieval", "TypeError")

    monkeypatch.setattr("backend.core.websearch.preflight_original_page", invalid)
    with pytest.raises(ExecutionConfigurationError, match="Native original-page request validation"):
        worker.run_batch(store, batch["id"], concurrency=1, item_limit=4)
    after = read_json(store, real_pilot.BUDGET_KEY)[0]
    assert after["attempted"]["search"] == before["attempted"]["search"] + 1
    assert after["attempted"]["web_retrieval"] == before["attempted"]["web_retrieval"]
    calls, _, searches, pages = captures
    assert len(calls) == 2 and len(searches) == 1 and not pages
