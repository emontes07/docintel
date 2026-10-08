"""One usage-based monetary meter for the bounded quality run."""

from decimal import Decimal
import json
import logging
from time import monotonic

from backend.batch_store import Missing, read_json, write_json

logger = logging.getLogger(__name__)
CostAmount = str | int | float | Decimal


class CostLimitExceeded(RuntimeError):
    quality_budget_stop = True


def _amount(value: CostAmount) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Cost and elapsed-time amounts must be finite and nonnegative")
    return number


class QualityCostMeter:
    def __init__(
        self, store, batch_id, run_id, *, run_base_cost_usd: CostAmount = 0,
        overnight_prior_cost_usd: CostAmount = 0, worker_usd_per_second: CostAmount = 0,
        search_usd: CostAmount = "0.0125", browse_usd: CostAmount = "0.0125",
        di_usd_per_page: CostAmount = "0.01", clock=monotonic,
        run_cap_usd: CostAmount = 10, overnight_cap_usd: CostAmount = 40,
    ):
        self.store = store
        self.key = f"quality-runs/{batch_id}/{run_id}/cost.json"
        self.clock = clock
        self.started = clock()
        self.run_base = _amount(run_base_cost_usd)
        self.overnight_prior = _amount(overnight_prior_cost_usd)
        self.worker_rate = _amount(worker_usd_per_second)
        self.search_rate, self.browse_rate = _amount(search_usd), _amount(browse_usd)
        self.di_rate = _amount(di_usd_per_page)
        self.run_cap, self.overnight_cap = _amount(run_cap_usd), _amount(overnight_cap_usd)
        if self.run_cap <= 0 or self.overnight_cap <= 0:
            raise ValueError("Spend caps must be positive")
        try:
            self.state, self.version = read_json(store, self.key)
        except Missing:
            self.state, self.version = {
                "model_cost_usd": 0, "web_cost_usd": 0, "worker_seconds": 0,
                "worker_cost_usd": 0, "model_calls": 0, "searches": 0, "browses": 0,
                "unpriced_model_calls": 0,
            }, None
        for key in ("di_cost_usd", "di_calls", "di_pages", "di_cache_hits", "unpriced_di_calls"):
            self.state.setdefault(key, 0)
        self.previous_seconds = _amount(self.state["worker_seconds"])
        self.previous_worker_cost = _amount(self.state["worker_cost_usd"])

    def summary(self) -> dict:
        elapsed = _amount(self.clock() - self.started)
        worker = self.previous_worker_cost + elapsed * self.worker_rate
        total = (self.run_base + _amount(self.state["model_cost_usd"]) + _amount(self.state["web_cost_usd"])
                 + _amount(self.state["di_cost_usd"]) + worker)
        self.state.update(
            run_base_cost_usd=float(self.run_base),
            worker_seconds=float(self.previous_seconds + elapsed),
            worker_cost_usd=float(worker),
            known_run_cost_usd=float(total),
            known_overnight_cost_usd=float(self.overnight_prior + total),
            run_cap_usd=float(self.run_cap), overnight_cap_usd=float(self.overnight_cap),
            qualification="Price-basis estimate from logged usage and elapsed compute, not finalized billing. Unpriced calls are disclosed separately.",
        )
        self.version = write_json(self.store, self.key, self.state, self.version)
        return dict(self.state)

    def before_call(self, context=None) -> None:
        current = self.summary()
        maximum = _amount((context or {}).get("maximum_cost_usd", 0))
        if (Decimal(str(current["known_run_cost_usd"])) + maximum >= self.run_cap
                or Decimal(str(current["known_overnight_cost_usd"])) + maximum >= self.overnight_cap):
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
        elif operation == "document_intelligence":
            if entry.get("cache_hit") is True:
                charge = Decimal(0)
                self.state["di_cache_hits"] += 1
            elif entry.get("analysis_attempted") is False:
                charge = Decimal(0)
            else:
                pages = entry.get("analyzed_pages")
                if pages is not None and (type(pages) is not int or pages < 0):
                    raise ValueError("DI analyzed pages must be a nonnegative integer or unknown")
                self.state["di_calls"] += 1
                charge = None if pages is None else _amount(pages) * self.di_rate
                if charge is None:
                    self.state["unpriced_di_calls"] += 1
                    logger.warning("DI call has no reported page usage; retaining unknown cost, not reporting it as free")
                else:
                    self.state["di_pages"] += pages
                    self.state["di_cost_usd"] = float(_amount(self.state["di_cost_usd"]) + charge)
            entry["pricing_basis"] = f"Configured DI prebuilt-layout estimate, ${self.di_rate}/page; not final billing"
        else:
            raise ValueError(f"Unknown usage operation: {operation}")
        current = self.summary()
        entry.update(
            cost_usd=None if charge is None else float(charge),
            run_cost_usd=current["known_run_cost_usd"],
            overnight_cost_usd=current["known_overnight_cost_usd"],
        )
        return entry


def _model_text_bytes(value) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, dict):
        return 4 + sum(len(key.encode("utf-8")) + _model_text_bytes(entry) + 8
                       for key, entry in value.items())
    if isinstance(value, list):
        return 4 + sum(_model_text_bytes(entry) + 4 for entry in value)
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def maximum_tool_cost(context: dict, model_prices: dict, prices: dict) -> Decimal:
    operation = context["operation"]
    if operation in {"web_search", "web_browse", "document_intelligence"}:
        rate = {"web_search": "search_usd", "web_browse": "browse_usd",
                "document_intelligence": "di_usd_per_page"}[operation]
        units = context.get("requested_pages") if operation == "document_intelligence" else 1
        if type(units) is not int or units < 1 or rate not in prices:
            raise CostLimitExceeded("Tool request has no complete page/price basis")
        return _amount(prices[rate]) * units
    if operation == "direct_page":
        return Decimal(0)
    if operation != "model":
        raise ValueError(f"Unknown priced tool operation: {operation}")
    if any(model_prices.get(name) is None for name in ("input", "cache_write", "output")):
        raise CostLimitExceeded("Tool model input/write/output pricing is not configured")
    request = dict(context["request"])
    request.update(request.pop("extra_body", {}))
    limit = request.get("max_output_tokens")
    if type(limit) is not int or limit <= 0:
        raise CostLimitExceeded("Tool request has no positive output-token limit")
    encoded = json.dumps(request, ensure_ascii=False, allow_nan=False)
    if '"input_image"' in encoded or '"input_file"' in encoded:
        raise CostLimitExceeded("Tool cost estimation supports retrieved text, not image/file inputs")
    inputs = request.get("input", [])
    if not isinstance(inputs, list):
        raise CostLimitExceeded("Tool request must expose its complete input history")
    # Count model-visible text, not additional HTTP JSON escaping around it.
    # Replay may contain compressed reasoning, so also budget the entire prior
    # output limit for each opaque reasoning item.
    reasoning = sum(item.get("type") == "reasoning" for item in inputs if isinstance(item, dict))
    tokens = _model_text_bytes(request) + 1024 + 256 * (len(inputs) + len(request.get("tools", [])))
    tokens += reasoning * limit
    input_rate = max(_amount(model_prices["input"]), _amount(model_prices["cache_write"]))
    return (tokens * input_rate + limit * _amount(model_prices["output"])) / 1_000_000
