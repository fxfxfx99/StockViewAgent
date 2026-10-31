"""Durable global-market scheduling with partial providers and resource pressure."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import data_collection as service
from app.storage import data_collection_store as store


@pytest.fixture(autouse=True)
def collection_runtime(monkeypatch):
    monkeypatch.setattr(settings, "enable_data_collection", True)
    monkeypatch.setattr(settings, "enable_scheduler", False)
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(free=20 * 1024 ** 3))
    monkeypatch.setattr("app.services.runtime_telemetry.record_collection", lambda *args, **kwargs: None)
    service.update_config({"enabled": False})
    store.set_state("last_maintenance", service.time.time())


def test_market_cadence_default_uses_environment_but_preserves_saved_preference(monkeypatch):
    monkeypatch.setattr(settings, "data_collection_market_interval_minutes", 15)
    store.set_state("config", {})
    assert service.get_config()["sources"]["public_market"]["interval_minutes"] == 15
    service.update_config({"sources": {"public_market": {"interval_minutes": 5}}})
    monkeypatch.setattr(settings, "data_collection_market_interval_minutes", 30)
    assert service.get_config()["sources"]["public_market"]["interval_minutes"] == 5
    source = next(row for row in service.sources() if row["id"] == "public_market")
    assert source["min_interval_minutes"] == 5
    assert source["interval_minutes"] == 5
    assert "期货" in source["description"]


@pytest.mark.parametrize("source_id,interval", [("public_market", 4), ("public_market", True),
                                              ("public_market", 43201), ("public_kline", 59)])
def test_market_cadence_keeps_source_specific_rate_limits(source_id, interval):
    with pytest.raises(ValueError):
        service.update_config({"sources": {source_id: {"interval_minutes": interval}}})


def test_repeated_partial_markets_retry_without_starving_healthy_refreshes(monkeypatch):
    clock = [1793400000.0]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    store.set_state("last_maintenance", clock[0])
    service.update_config({"enabled": True, "symbols": [], "use_watchlists": False,
                           "sources": {row["id"]: {"enabled": row["id"] == "public_market"}
                                       for row in service.catalog()}})
    calls = []

    async def partial(source_id):
        calls.append(source_id)
        return {"status": "partial", "records_received": 1, "records_added": 0,
                "error_code": "partial_source_failure", "retry_after_seconds": 120}

    monkeypatch.setattr(service, "collect_source", partial)
    for attempt in range(8):
        assert asyncio.run(service.run_cycle())["jobs_processed"] == 1
        source = next(row for row in service.sources() if row["id"] == "public_market")
        assert source["failures"] == attempt + 1
        assert datetime.fromisoformat(source["next_run_at"]).timestamp() == clock[0] + 120
        assert store.enqueue("public_market", now=clock[0] + 119)["status"] == "cooldown"
        clock[0] += 120
    assert calls == ["public_market"] * 8
    assert store.usage()["jobs_started"] == 8

    async def complete(source_id):
        return {"status": "success", "records_received": 1, "records_added": 0}

    monkeypatch.setattr(service, "collect_source", complete)
    assert asyncio.run(service.run_cycle())["jobs_processed"] == 1
    source = next(row for row in service.sources() if row["id"] == "public_market")
    assert source["failures"] == 0 and source["retry_at"] is None
    assert datetime.fromisoformat(source["next_run_at"]).timestamp() == clock[0] + source["interval_minutes"] * 60


@pytest.mark.parametrize("delay", [True, 0, 29, 21601, 120.5, "120"])
def test_invalid_adapter_retry_does_not_modify_durable_job(delay):
    store.enqueue("public_market")
    job = store.claim_job(120)
    with pytest.raises(ValueError):
        store.finish_job(job["id"], {"status": "partial", "retry_after_seconds": delay})
    assert store.list_jobs()[0]["status"] == "running"
    assert "public_market" not in store.source_states()


def test_disk_pressure_between_jobs_preserves_queue_and_daily_budget(monkeypatch):
    disk = {"free": 20 * 1024 ** 3}
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(**disk))
    service.enqueue(["public_market", "public_news"])
    calls = []

    async def collect(source_id):
        calls.append(source_id)
        disk["free"] = 100 * 1024 ** 2
        return {"status": "success"}

    monkeypatch.setattr(service, "collect_source", collect)
    assert asyncio.run(service.run_cycle())["jobs_processed"] == 1
    assert len(calls) == 1
    assert store.usage()["jobs_started"] == store.usage()["queued"] == 1
    assert store.get_state("worker")["reason"] == "low_disk_space"
    disk["free"] = 20 * 1024 ** 3
    assert asyncio.run(service.run_cycle())["jobs_processed"] == 1
    assert len(calls) == store.usage()["jobs_started"] == 2
    assert store.usage()["queued"] == 0


def test_worker_uses_configurable_queue_polling_independent_of_provider_cadence(monkeypatch):
    monkeypatch.setattr(settings, "data_collection_startup_delay_sec", 3.0)
    monkeypatch.setattr(settings, "data_collection_poll_interval_sec", 17.0)
    sleeps, owners = [], []

    async def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    async def cycle(*, owner):
        owners.append(owner)

    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    monkeypatch.setattr(service, "run_cycle", cycle)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.worker_loop())
    assert sleeps == [3.0, 17.0]
    assert len(owners) == 1
