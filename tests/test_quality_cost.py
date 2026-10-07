import pytest

from backend.batch_store import SQLiteStore
from backend.quality_cost import CostLimitExceeded, QualityCostMeter


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
