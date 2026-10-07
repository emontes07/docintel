"""One usage-based monetary meter for the bounded quality run."""

from decimal import Decimal
import logging
from time import monotonic

from backend.batch_store import Missing, read_json, write_json

logger = logging.getLogger(__name__)


class CostLimitExceeded(RuntimeError):
    pass


def _amount(value) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Cost and elapsed-time amounts must be finite and nonnegative")
    return number


class QualityCostMeter:
    def __init__(
        self, store, batch_id, run_id, *, run_base_cost_usd=0,
        overnight_prior_cost_usd=0, worker_usd_per_second=0,
        search_usd="0.0125", browse_usd="0.0125", clock=monotonic,
    ):
        self.store = store
        self.key = f"quality-runs/{batch_id}/{run_id}/cost.json"
        self.clock = clock
        self.started = clock()
        self.run_base = _amount(run_base_cost_usd)
        self.overnight_prior = _amount(overnight_prior_cost_usd)
        self.worker_rate = _amount(worker_usd_per_second)
        self.search_rate, self.browse_rate = _amount(search_usd), _amount(browse_usd)
        try:
            self.state, self.version = read_json(store, self.key)
        except Missing:
            self.state, self.version = {
                "model_cost_usd": 0, "web_cost_usd": 0, "worker_seconds": 0,
                "worker_cost_usd": 0, "model_calls": 0, "searches": 0, "browses": 0,
                "unpriced_model_calls": 0,
            }, None
        self.previous_seconds = _amount(self.state["worker_seconds"])
        self.previous_worker_cost = _amount(self.state["worker_cost_usd"])

    def summary(self) -> dict:
        elapsed = _amount(self.clock() - self.started)
        worker = self.previous_worker_cost + elapsed * self.worker_rate
        total = self.run_base + _amount(self.state["model_cost_usd"]) + _amount(self.state["web_cost_usd"]) + worker
        self.state.update(
            run_base_cost_usd=float(self.run_base),
            worker_seconds=float(self.previous_seconds + elapsed),
            worker_cost_usd=float(worker),
            known_run_cost_usd=float(total),
            known_overnight_cost_usd=float(self.overnight_prior + total),
            run_cap_usd=10, overnight_cap_usd=40,
            qualification="Price-basis estimate from logged usage and elapsed compute, not finalized billing. Unpriced calls are disclosed separately.",
        )
        self.version = write_json(self.store, self.key, self.state, self.version)
        return dict(self.state)

    def before_call(self, context=None) -> None:
        current = self.summary()
        if current["known_run_cost_usd"] >= 10 or current["known_overnight_cost_usd"] >= 40:
            raise CostLimitExceeded(
                f"Monetary cap reached: run ${current['known_run_cost_usd']:.4f}; "
                f"overnight ${current['known_overnight_cost_usd']:.4f}"
            )

    def record(self, record: dict) -> dict:
        entry = dict(record)
        operation = entry["operation"]
        if operation == "model":
            amount = entry.get("cost_usd")
            if amount is None:
                amount = entry.get("estimated_cost_usd")
            charge = None if amount is None else _amount(amount)
            self.state["model_calls"] += 1
            if charge is None:
                self.state["unpriced_model_calls"] += 1
                logger.warning("Model call has no measured price basis; retaining unknown cost, not reporting it as free")
            else:
                self.state["model_cost_usd"] = float(_amount(self.state["model_cost_usd"]) + charge)
        elif operation in {"search", "webiq_search", "web_search"}:
            charge = self.search_rate
            self.state["searches"] += 1
            self.state["web_cost_usd"] = float(_amount(self.state["web_cost_usd"]) + charge)
        elif operation in {"browse", "webiq_browse", "web_browse"}:
            charge = self.browse_rate
            self.state["browses"] += 1
            self.state["web_cost_usd"] = float(_amount(self.state["web_cost_usd"]) + charge)
        elif operation in {"direct_page", "page", "web_retrieval"}:
            charge = Decimal(0)
        else:
            raise ValueError(f"Unknown usage operation: {operation}")
        current = self.summary()
        entry.update(
            cost_usd=None if charge is None else float(charge),
            run_cost_usd=current["known_run_cost_usd"],
            overnight_cost_usd=current["known_overnight_cost_usd"],
        )
        return entry
