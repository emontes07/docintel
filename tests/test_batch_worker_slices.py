"""Queue mechanics only; production authority and real-cache gates are separate."""

import copy
from types import SimpleNamespace

import pytest

from backend import batch_worker as worker
from backend.batch_store import read_json, write_json
from backend.extract import ExecutionConfigurationError, run_offline
from backend.models.enrichment import Manifest, OfflineBundle
from tests.test_real_batch_worker import configured, no_network


@pytest.fixture
def sliced_queue(configured, monkeypatch):
    store, batch, _ = configured
    batch = copy.deepcopy(batch)
    batch["items"] = [
        {**copy.deepcopy(batch["items"][0]), "item_key": f"row-{index}"}
        for index in range(2, 6)
    ]
    _, current = read_json(store, f"batches/{batch['id']}.json")
    write_json(store, f"batches/{batch['id']}.json", batch, current)
    slices = [["row-2", "row-4"], ["row-3", "row-5"]]
    calls, executions = [], []
    selected = [slices[0]]

    class QueueGuard:
        def __init__(self, supplied, record):
            assert supplied is store and record["id"] == batch["id"]
            self.four_product = {
                "worker_slices": slices, "execution_order": sum(slices, []),
                "selected_item_keys": sum(slices, []),
            }
            self.recovery = self.active_recovery = self.four_product
            self.recovery_sha256 = "synthetic-queue-scope"

        def before_execution(self, key):
            executions.append(key)
            self.active_slice_item_keys = selected[0]

        def prepare_recovery(self, fence):
            fence()

        def result_key(self, key):
            return f"results/{batch['id']}/{key}/attempts/synthetic-slice.json"

    monkeypatch.setattr(worker, "RealPilotGuard", QueueGuard)
    for item in batch["items"]:
        key = item["item_key"]
        write_json(store, f"items/{batch['id']}/{key}.json", {
            "id": key, "batch_id": batch["id"], "state": "recovery_ready",
            "recovery_sha256": "synthetic-queue-scope",
        })

    def process(item, mode):
        assert mode == "real_pilot"
        calls.append(item["item_key"])
        result = run_offline(OfflineBundle(
            manifest=Manifest.model_validate(item["manifest"]), sources=[],
        ))
        return result, {"state": "unresolved"}

    return SimpleNamespace(
        store=store, batch=batch, calls=calls, executions=executions,
        selected=selected, slices=slices, process=process,
    )


def run(case, limit=2):
    worker.run_batch(case.store, case.batch["id"], concurrency=1, item_limit=limit, processor=case.process)


def test_two_slices_keep_second_queued_and_first_results_immutable(sliced_queue):
    case = sliced_queue
    run(case)
    assert case.calls == ["row-2", "row-4"]
    first = read_json(case.store, f"batches/{case.batch['id']}.json")[0]
    assert first["state"] == "queued" and first["progress"]["finished"] == 2
    for key in case.slices[1]:
        assert read_json(case.store, f"items/{case.batch['id']}/{key}.json")[0]["state"] == "recovery_ready"
    retained = {key: case.store.read_bytes(key) for key in case.store.keys(f"results/{case.batch['id']}/")}
    case.selected[0] = case.slices[1]
    run(case)
    assert case.calls == ["row-2", "row-4", "row-3", "row-5"] and len(case.executions) == 2
    final = read_json(case.store, f"batches/{case.batch['id']}.json")[0]
    assert final["state"] == "completed" and final["progress"]["finished"] == 4
    assert all(case.store.read_bytes(key) == value for key, value in retained.items())


@pytest.mark.parametrize("limit", [1, 3, 4])
def test_wrong_slice_size_rejects_before_charging_execution(sliced_queue, limit):
    with pytest.raises(ValueError, match="exact authorized worker slice"):
        run(sliced_queue, limit=limit)
    assert not sliced_queue.calls and not sliced_queue.executions


@pytest.mark.parametrize("selection", [None, [], ["row-2"], ["row-2", "row-2"], ["row-2", "row-9"]])
def test_malformed_guard_selection_cannot_process_unapproved_items(sliced_queue, selection):
    sliced_queue.selected[0] = selection
    with pytest.raises(ExecutionConfigurationError, match="exact authorized product slice"):
        run(sliced_queue)
    assert not sliced_queue.calls


def test_reselecting_attempted_slice_never_overwrites_or_reruns(sliced_queue):
    case = sliced_queue
    run(case)
    retained = {key: case.store.read_bytes(key) for key in case.store.keys(f"results/{case.batch['id']}/")}
    with pytest.raises(ExecutionConfigurationError, match="attempted or unavailable"):
        run(case)
    assert case.calls == ["row-2", "row-4"]
    assert all(case.store.read_bytes(key) == value for key, value in retained.items())
