import asyncio
import json
import os
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import data_center
from app.security.jwt_tokens import create_access_token
from app.security.user_context import current_user_id
from app.services import data_collection as service, runtime_telemetry
from app.storage import cache_lifecycle, data_collection_store as store, kline_bundle_cache, users_store


NOW = 1792800000.0


@pytest.fixture(autouse=True)
def service_runtime(monkeypatch):
    monkeypatch.setattr(service.time, "time", lambda: NOW)
    monkeypatch.setattr(service.shutil, "disk_usage", lambda root: SimpleNamespace(free=20 * 1024 ** 3))
    monkeypatch.setattr(runtime_telemetry, "record_collection", lambda *args, **kwargs: None)
    store.set_state("last_maintenance", NOW)


def row(source_id="public_kline"):
    return {"source_id": source_id, "upstream_id": "600000.SS:1", "title": "公开行情",
            "source_url": "https://example.org/data", "source": "Test", "value": 10,
            "text": "公开测试行情", "metadata": {"c": 10}}


def test_config_partial_merge_and_rag_switch():
    assert service.default_config()["rag_enabled"] is True
    value = service.update_config({"rag_enabled": False, "symbols": ["600000.ss", "600000.SS"], "sources": {"public_kline": {"interval_minutes": 720}}})
    assert value["rag_enabled"] is False and value["symbols"] == ["600000.SS"]
    assert value["sources"]["public_kline"] == {"enabled": True, "interval_minutes": 720}
    assert service.update_config({"enabled": False})["rag_enabled"] is False


@pytest.mark.parametrize("patch", [{"rag_enabled": 1}, {"enabled": "false"}, {"daily_job_limit": True},
                                   {"daily_job_limit": 1001}, {"min_free_disk_mb": 1}, {"symbols": ["AAPL"]},
                                   {"symbols": "600000.SS"}, {"sources": {"unknown": {}}},
                                   {"sources": {"public_kline": {"interval_minutes": 59}}},
                                   {"sources": {"public_kline": {"url": "https://private"}}}, {"api_key": "secret"}])
def test_invalid_config_rejected(patch):
    with pytest.raises(ValueError):
        service.update_config(patch)
    assert service.get_config()["rag_enabled"] is True


def test_symbols_rotate_deduplicate_and_do_not_seed_empty_watchlists(tmp_path):
    directory = tmp_path / "watchlists"
    directory.mkdir()
    (directory / "1.json").write_text(json.dumps({"symbols": []}))
    assert service._symbols("public_kline") == []
    assert json.loads((directory / "1.json").read_text()) == {"symbols": []}
    (directory / "2.json").write_text(json.dumps({"symbols": ["600000.SS", "000001.SZ", "invalid"]}))
    (directory / "3.json").symlink_to(directory / "2.json")
    service.update_config({"symbols": ["600000.SS", "600519.SS"], "max_symbols_per_cycle": 2})
    assert service._symbols("public_kline") == ["600000.SS", "600519.SS"]
    assert service._symbols("public_kline") == ["000001.SZ", "600000.SS"]
    assert service._symbols("public_company") == ["600000.SS", "600519.SS"]


def test_enqueue_deduplicates_and_rejects_arbitrary_sources():
    first = service.enqueue(["public_kline"])["jobs"][0]
    second = service.enqueue(["public_kline"])["jobs"][0]
    assert first["id"] == second["id"] and second["deduplicated"]
    with pytest.raises(ValueError):
        service.enqueue(["https://private.example/"])


def test_cycle_archives_and_honors_daily_job_budget(monkeypatch):
    calls = []
    async def collect(sid):
        calls.append((sid, current_user_id.get()))
        return {"status": "success", **store.ingest(sid, "market", [row(sid)])}
    monkeypatch.setattr(service, "collect_source", collect)
    service.update_config({"daily_job_limit": 1})
    service.enqueue(["public_kline", "public_company"])
    assert asyncio.run(service.run_cycle())["status"] == "processed"
    assert asyncio.run(service.run_cycle())["status"] == "idle"
    assert len(calls) == 1 and calls[0][1] == 0
    assert store.usage()["jobs_started"] == 1 and store.usage()["queued"] == 1
    assert store.dataset_stats()["records"] == 1
    assert store.get_state("worker")["reason"] == "daily_budget_exhausted"


def test_manual_jobs_continue_when_automatic_collection_is_disabled(monkeypatch):
    calls = []
    async def collect(sid):
        calls.append(sid)
        return {"status": "success", "records_received": 0}
    monkeypatch.setattr(service, "collect_source", collect)
    service.update_config({"enabled": False, "sources": {"public_kline": {"enabled": False}}})
    service.enqueue(["public_kline"])
    asyncio.run(service.run_cycle())
    assert calls == ["public_kline"]


def test_disabled_automatic_queue_is_cancelled_without_spending_budget(monkeypatch):
    service.update_config({"enabled": False})
    store.enqueue("public_kline", {"scheduled": True})
    async def collect(sid):
        pytest.fail("Disabled scheduled job must not fetch")
    monkeypatch.setattr(service, "collect_source", collect)
    assert asyncio.run(service.run_cycle())["status"] == "idle"
    job = store.list_jobs()[0]
    assert job["status"] == "skipped" and job["error_code"] == "automatic_collection_disabled"
    assert store.usage()["jobs_started"] == 0


def test_disabled_source_cancels_only_its_automatic_job(monkeypatch):
    monkeypatch.setattr(settings, "enable_scheduler", True)
    service.update_config({"sources": {row["id"]: {"enabled": row["id"] == "public_company"} for row in service.catalog()}})
    store.enqueue("public_kline", {"scheduled": True})
    async def collect(sid):
        assert sid == "public_company"
        return {"status": "success"}
    monkeypatch.setattr(service, "collect_source", collect)
    asyncio.run(service.run_cycle())
    assert {job["source_id"]: job["status"] for job in store.list_jobs()} == {"public_kline": "skipped", "public_company": "success"}


def test_low_disk_does_not_claim_or_fetch(monkeypatch):
    service.enqueue(["public_kline"])
    monkeypatch.setattr(service.shutil, "disk_usage", lambda root: SimpleNamespace(free=100 * 1024 ** 2))
    result = asyncio.run(service.run_cycle())
    assert result["status"] == "low_disk_space"
    assert store.usage()["jobs_started"] == 0 and store.usage()["queued"] == 1
    assert store.get_state("worker")["reason"] == "low_disk_space"


def test_restart_recovers_interrupted_job_with_a_new_counted_attempt(monkeypatch):
    queued = store.enqueue("public_kline")
    store.claim_job(120)
    async def collect(sid):
        return {"status": "success"}
    monkeypatch.setattr(service, "collect_source", collect)
    asyncio.run(service.run_cycle())
    job = store.list_jobs()[0]
    assert job["id"] == queued["id"] and job["status"] == "success"
    assert store.usage()["jobs_started"] == 2


def test_failures_are_sanitized_and_prior_evidence_retained(monkeypatch):
    store.ingest("public_kline", "market", [row()])
    service.enqueue(["public_kline"])
    async def collect(sid):
        error = RuntimeError("credential secret do not display")
        error.error_code = "http_403"
        raise error
    monkeypatch.setattr(service, "collect_source", collect)
    asyncio.run(service.run_cycle())
    assert store.list_jobs()[0]["error_code"] == "http_403"
    assert "credential" not in json.dumps(service.sources())
    assert store.dataset_stats()["records"] == 1


def test_cross_process_lease_prevents_duplicate_collection(monkeypatch):
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def collect(sid):
            calls.append(sid)
            entered.set()
            await finish.wait()
            return {"status": "success"}
        monkeypatch.setattr(service, "collect_source", collect)
        service.enqueue(["public_kline"])
        first = asyncio.create_task(service.run_cycle(owner="first"))
        await entered.wait()
        assert (await service.run_cycle(owner="second"))["status"] == "another_worker"
        assert store.get_state("worker")["owner"] == "first"
        finish.set()
        await first
        assert len(calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("thread_fails", [False, True])
def test_cancellation_keeps_lease_until_durable_thread_finishes(monkeypatch, thread_fails):
    entered, finish = threading.Event(), threading.Event()
    def blocking():
        entered.set()
        assert finish.wait(5)
        if thread_fails:
            raise RuntimeError("thread failed during shutdown")
    async def collect(sid):
        await service.durable_call(blocking)
        return {"status": "success"}
    monkeypatch.setattr(service, "collect_source", collect)
    service.enqueue(["public_kline"])
    async def scenario():
        task = asyncio.create_task(service.run_cycle(owner="first"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert (await service.run_cycle(owner="second"))["status"] == "another_worker"
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.get_state("worker")["reason"] == "interrupted"
        assert store.usage()["running"] == 1  # Recover under the next owner's lease.
        with service._worker_lease() as owned:
            assert owned
    asyncio.run(scenario())


def test_old_worker_shutdown_cannot_mark_new_owner_stopped():
    store.set_state("worker", {"owner": "new", "running": True, "pid": os.getpid()})
    service._update_worker("old", running=False, reason="stopped")
    assert store.get_state("worker")["running"] is True


def test_worker_lock_refuses_symlink_and_hardlink(tmp_path):
    target = tmp_path / "private"
    target.write_text("sensitive")
    lock = tmp_path / ".data-collection.lock"
    lock.symlink_to(target)
    with pytest.raises(OSError):
        asyncio.run(service.run_cycle())
    lock.unlink()
    os.link(target, lock)
    with pytest.raises(ValueError):
        asyncio.run(service.run_cycle())
    assert target.read_text() == "sensitive"


def test_cache_maintenance_runs_at_most_hourly(monkeypatch):
    calls = []
    monkeypatch.setattr(cache_lifecycle, "run_maintenance", lambda: calls.append("maintenance"))
    store.set_state("last_maintenance", 0)
    asyncio.run(service.run_cycle())
    asyncio.run(service.run_cycle())
    assert calls == ["maintenance"]


def test_empty_kline_source_does_not_replace_good_cache(monkeypatch):
    from app.services import market_extra_http
    service.update_config({"symbols": ["600000.SS"], "use_watchlists": False})
    kline_bundle_cache.save_bundle("600000.SS", "1y", "1d", {"candles": [{"t": 1, "c": 10}], "kline_source": "good"})
    monkeypatch.setattr(market_extra_http, "fetch_kline_bundle_secondary_sync", lambda *args: {"candles": []})
    result = asyncio.run(service.collect_source("public_kline"))
    assert result["status"] == "failed"
    assert kline_bundle_cache.load_bundle("600000.SS", "1y", "1d")["payload"]["kline_source"] == "good"
    assert store.dataset_stats()["records"] == 0


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_jwt_secret", "data-center-auth-tests-32-characters")
    users_store.init_db()
    app = FastAPI()
    app.include_router(data_center.router)
    with TestClient(app) as client:
        yield client


def headers(uid):
    user = users_store.get_by_id(uid)
    return {"Authorization": "Bearer " + create_access_token(user_id=user.id, username=user.username, role=user.role)}


@pytest.mark.parametrize("method,path,body", [("get", "/overview", None), ("get", "/files", None), ("get", "/records", None),
    ("get", "/config", None), ("patch", "/config", {}), ("get", "/sources", None), ("get", "/jobs", None),
    ("post", "/collect", {}), ("get", "/cache/policy", None), ("patch", "/cache/policy", {}),
    ("post", "/cleanup/preview", {}), ("post", "/cleanup/apply", {"plan_id": "x"}), ("get", "/trash", None),
    ("post", "/trash/x/restore", {}), ("delete", "/trash/x", {"confirm": True})])
def test_every_data_center_route_requires_admin(client, method, path, body):
    assert client.request(method, "/api/data-center" + path, json=body).status_code == 401
    assert client.request(method, "/api/data-center" + path, json=body, headers=headers(2)).status_code == 403


def test_admin_route_config_and_validation_contract(client):
    auth = headers(1)
    assert client.patch("/api/data-center/config", json={"rag_enabled": False}, headers=auth).json()["rag_enabled"] is False
    assert client.post("/api/data-center/collect", json={"source_ids": ["private"]}, headers=auth).status_code == 400
    assert client.get("/api/data-center/files?sort=invalid", headers=auth).status_code == 400
    assert client.request("delete", "/api/data-center/trash/" + "a" * 32, json={"confirm": False}, headers=auth).status_code == 400
