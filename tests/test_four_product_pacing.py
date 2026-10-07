"""Clock simulations, not provider-latency or quota guarantees."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.batch_worker import RealBatchProcessor


class Clock:
    def __init__(self):
        self.seconds = 0

    def advance(self, seconds):
        assert seconds >= 0
        self.seconds += seconds


def paced_processor(monkeypatch, clock, *, four_product):
    monkeypatch.setattr("backend.batch_worker.monotonic", lambda: clock.seconds)
    monkeypatch.setattr("backend.batch_worker.sleep", clock.advance)
    starts = []

    def reserve(*args, **kwargs):
        starts.append(clock.seconds)
        return {"reserved_usage": kwargs}

    guard = SimpleNamespace(
        recovery={"synthetic": True},
        four_product={"inference_interval_seconds": 31} if four_product else None,
        reserve=reserve,
    )
    return RealBatchProcessor(Mock(), {}, guard), starts


@pytest.mark.parametrize("four_product,expected_last_start", [(False, 671), (True, 341)])
def test_twelve_request_schedule_preserves_legacy_pacing(monkeypatch, four_product, expected_last_start):
    clock = Clock()
    processor, starts = paced_processor(monkeypatch, clock, four_product=four_product)
    for index in range(12):
        reservation = processor.reserve_real(
            "inference", f"synthetic-{index}", max_input_tokens=26000, max_output_tokens=2048,
        )
        assert reservation["reserved_usage"] == {"max_input_tokens": 26000, "max_output_tokens": 2048}
        clock.advance(22)
    assert starts[-1] == expected_last_start
    assert all(sum(start <= other < start + 60 for other in starts) <= 2 for start in starts)


def test_interleaved_twelve_request_clock_forecast_includes_startup_and_web_delays(monkeypatch):
    clock = Clock()
    processor, starts = paced_processor(monkeypatch, clock, four_product=True)
    clock.advance(35)
    order = ["row-2", "row-4", "row-3", "row-5"]
    schedule = []
    for item_key in order:
        for tier in ["internal_pdf", "vendor_table", "manufacturer_web"]:
            if tier == "manufacturer_web":
                clock.advance(37.5)
            processor.reserve_real("inference", f"{item_key}:{tier}")
            schedule.append((item_key, tier, starts[-1]))
            clock.advance(22)
    assert len(starts) == 12
    assert clock.seconds == 512
    assert clock.seconds < 600
    assert [row for row, tier, _ in schedule if tier == "manufacturer_web"] == order
    assert all(right - left >= 31 for left, right in zip(starts, starts[1:]))


def test_slow_responses_do_not_add_another_full_pacing_interval(monkeypatch):
    clock = Clock()
    processor, starts = paced_processor(monkeypatch, clock, four_product=True)
    processor.reserve_real("inference", "first")
    clock.advance(45)
    processor.reserve_real("inference", "second")
    assert starts == [0, 45]


def test_non_inference_reservations_do_not_reset_inference_spacing(monkeypatch):
    clock = Clock()
    processor, starts = paced_processor(monkeypatch, clock, four_product=True)
    processor.reserve_real("inference", "first")
    clock.advance(10)
    processor.reserve_real("search", "search")
    processor.reserve_real("inference", "second")
    assert starts == [0, 10, 31]
