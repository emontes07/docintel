import pytest

from backend.batch_store import SQLiteStore
from backend.quality_cost import CostLimitExceeded, QualityCostMeter, maximum_tool_cost


def test_meter_charges_actual_calls_compute_and_carries_a_retry(tmp_path):
    store = SQLiteStore(tmp_path / "cost")
    now = [0]
    meter = QualityCostMeter(store, "batch", "run", run_base_cost_usd=".02", worker_usd_per_second=".001", clock=lambda: now[0])
    assert meter.record({"operation": "model", "estimated_cost_usd": ".25"})["cost_usd"] == .25
    meter.record({"operation": "search"})
    meter.record({"operation": "browse"})
    now[0] = 10
    assert meter.summary()["known_run_cost_usd"] == pytest.approx(.305)
    retry = QualityCostMeter(store, "batch", "run", run_base_cost_usd=".02", worker_usd_per_second=".001", clock=lambda: now[0])
    now[0] = 20
    assert retry.summary()["known_run_cost_usd"] == pytest.approx(.315)
    assert retry.summary()["model_calls"] == 1


@pytest.mark.parametrize(("base", "prior"), [("10", "0"), ("1", "39")])
def test_only_monetary_caps_stop_calls(tmp_path, base, prior):
    meter = QualityCostMeter(SQLiteStore(tmp_path / "cost"), "b", "r", run_base_cost_usd=base, overnight_prior_cost_usd=prior)
    with pytest.raises(CostLimitExceeded, match="Monetary cap"):
        meter.before_call({"phase": "extract"})


def test_unknown_model_cost_is_visible_and_not_free(tmp_path, caplog):
    meter = QualityCostMeter(SQLiteStore(tmp_path / "cost"), "b", "r")
    assert meter.record({"operation": "model", "estimated_cost_usd": None})["cost_usd"] is None
    assert meter.summary()["unpriced_model_calls"] == 1
    assert "not reporting it as free" in caplog.text


def test_invalid_cost_is_not_silently_coerced(tmp_path):
    meter = QualityCostMeter(SQLiteStore(tmp_path / "cost"), "b", "r")
    with pytest.raises(ValueError):
        meter.record({"operation": "model", "estimated_cost_usd": -1})
    with pytest.raises(ValueError, match="Unknown usage"):
        meter.record({"operation": "unexpected"})


def test_next_tool_maximum_obeys_both_existing_global_caps(tmp_path):
    meter = QualityCostMeter(SQLiteStore(tmp_path / "cost"), "b", "r",
                             run_base_cost_usd="9.80", overnight_prior_cost_usd="30", clock=lambda: 0)
    meter.before_call({"maximum_cost_usd": ".19"})
    with pytest.raises(CostLimitExceeded):
        meter.before_call({"maximum_cost_usd": ".21"})
    assert meter.summary()["model_calls"] == 0


def test_tool_maximum_prices_complete_effective_request_and_opaque_reasoning():
    from decimal import Decimal

    prices = {"input": 2, "cache_write": 2.5, "output": 10}
    request = {"max_output_tokens": 16000, "input": [{"role": "user", "content": "product"}],
               "instructions": "instructions", "tools": [{"name": "search"}],
               "text": {"schema": {"description": "complete schema"}}}
    baseline = maximum_tool_cost({"operation": "model", "request": request}, prices, {})
    assert baseline >= Decimal(".16")
    cached = {**request, "extra_body": {"input": [
        {"role": "user", "content": "shared source " * 1000},
        {"type": "reasoning", "encrypted_content": "opaque"},
    ]}}
    full = maximum_tool_cost({"operation": "model", "request": cached}, prices, {})
    assert full > baseline + Decimal(".04")
    assert maximum_tool_cost({"operation": "model", "request": cached},
                             {**prices, "cached_input": 0}, {}) == full
    with pytest.raises(CostLimitExceeded, match="pricing"):
        maximum_tool_cost({"operation": "model", "request": request}, {**prices, "output": None}, {})
    with pytest.raises(CostLimitExceeded, match="image/file"):
        maximum_tool_cost({"operation": "model", "request": {
            **request, "input": [{"type": "input_image", "image_url": "data:..."}],
        }}, prices, {})


def test_tool_maximum_uses_configured_web_prices_and_actual_ocr_page_bound():
    from decimal import Decimal

    rates = {"search_usd": ".013", "browse_usd": ".014", "di_usd_per_page": ".02"}
    assert maximum_tool_cost({"operation": "web_search"}, {}, rates) == Decimal(".013")
    assert maximum_tool_cost({"operation": "web_browse"}, {}, rates) == Decimal(".014")
    assert maximum_tool_cost({"operation": "document_intelligence", "requested_pages": 5}, {}, rates) == Decimal(".10")
    with pytest.raises(CostLimitExceeded):
        maximum_tool_cost({"operation": "document_intelligence"}, {}, rates)


def test_tool_cost_does_not_count_transport_escaping_as_model_tokens():
    def estimate(content):
        return maximum_tool_cost({
            "operation": "model", "request": {
                "max_output_tokens": 16000, "tools": [],
                "input": [{"role": "user", "content": content}],
            },
        }, {"input": 2, "cache_write": 2.5, "output": 10}, {})

    assert estimate('"' * 1024) == estimate("a" * 1024)
    assert estimate("\\" * 1024) == estimate("a" * 1024)
    assert estimate("\u00e9" * 512) == estimate("a" * 1024)


@pytest.mark.parametrize("operation", ["web_search", "webiq_search", "web_browse", "webiq_browse"])
def test_provider_aliases_charge_the_same_meter(tmp_path, operation):
    meter = QualityCostMeter(SQLiteStore(tmp_path / "cost"), "b", "r")
    assert meter.record({"operation": operation})["cost_usd"] == .0125
    assert meter.summary()["web_cost_usd"] == .0125


def test_direct_original_page_fetch_does_not_charge_webiq(tmp_path):
    meter = QualityCostMeter(SQLiteStore(tmp_path / "cost"), "b", "r")
    assert meter.record({"operation": "direct_page"})["cost_usd"] == 0
    assert meter.summary()["web_cost_usd"] == 0


def test_di_pages_cache_and_unknown_usage_share_the_existing_meter(tmp_path, caplog):
    meter = QualityCostMeter(SQLiteStore(tmp_path / "cost"), "b", "r", clock=lambda: 0)
    assert meter.record({"operation": "document_intelligence", "analyzed_pages": 2, "cache_hit": False})["cost_usd"] == .02
    assert meter.record({"operation": "document_intelligence", "analyzed_pages": 0, "cache_hit": True})["cost_usd"] == 0
    assert meter.record({"operation": "document_intelligence", "analyzed_pages": None, "cache_hit": False})["cost_usd"] is None
    assert meter.record({"operation": "document_intelligence", "analyzed_pages": 0, "cache_hit": False,
                         "analysis_attempted": False, "status": "failed"})["cost_usd"] == 0
    result = meter.summary()
    assert result["known_run_cost_usd"] == result["di_cost_usd"] == .02
    assert result["di_calls"] == 2 and result["di_pages"] == 2 and result["di_cache_hits"] == 1
    assert result["unpriced_di_calls"] == 1 and result["model_calls"] == 0
    assert "unknown cost" in caplog.text
    with pytest.raises(ValueError, match="analyzed pages"):
        meter.record({"operation": "document_intelligence", "analyzed_pages": True})
