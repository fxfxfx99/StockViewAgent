"""Daily windows, bounded durable pool rounds, and queue resource protection."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import collector_resources, data_collection as service, news_feed_sync
from app.storage import data_collection_store as store


AT = datetime(2026, 10, 31, 18, 0, tzinfo=store.SHANGHAI).timestamp()
POOL = ["600000.SS", "600519.SS", "000001.SZ", "300750.SZ", "000002.SZ"]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(settings, "enable_data_collection", True)
    monkeypatch.setattr(service.time, "time", lambda: AT)
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _: SimpleNamespace(free=20 * 1024**3))
    monkeypatch.setattr(collector_resources, "snapshot", lambda limit: {"rss_mb": 40, "limit_mb": limit, "pressure": False, "supported": True, "reason": None, "scope": "current_process"})
    monkeypatch.setattr("app.services.runtime_telemetry.record_collection", lambda *a, **kw: None)
    store.set_state("last_maintenance", AT)


def configure(**changes):
    return service.update_config({"use_watchlists": False, "symbols": POOL, "max_symbols_per_cycle": 2,
        "sources": {row["id"]: {"enabled": row["id"] == "public_company"} for row in service.catalog()}, **changes})


def finish_batch(job, *, now=AT, fail=None):
    claimed = store.claim_job(120, now=now)
    assert claimed["id"] == job["id"]
    for symbol in store.pending_job_symbols(job["id"]):
        if symbol != fail:
            store.checkpoint_symbol(job["id"], symbol)
    store.finish_job(job["id"], {"status": "partial" if fail else "success", "error_code": "http_429" if fail else None}, now=now)


def state():
    return next(row for row in service.sources() if row["id"] == "public_company")


def test_defaults_and_saved_legacy_intervals():
    config = service.get_config()
    assert (config["max_concurrent_jobs"], config["max_memory_mb"], config["job_timeout_seconds"]) == (1, 768, 240)
    assert config["sources"]["public_company"]["schedule_mode"] == "daily"
    assert config["sources"]["public_company"]["daily_at"] == "17:30"
    store.set_state("config", {"sources": {"public_company": {"interval_minutes": 720, "enabled": False}}})
    assert service.get_config()["sources"]["public_company"] == {"enabled": False, "interval_minutes": 720, "schedule_mode": "interval", "daily_at": "17:30"}
    service.update_config({"max_concurrent_jobs": 2})
    assert service.get_config()["sources"]["public_company"]["schedule_mode"] == "interval"


@pytest.mark.parametrize("patch", [
    {"max_concurrent_jobs": 0}, {"max_concurrent_jobs": 4}, {"max_concurrent_jobs": True},
    {"max_memory_mb": 127}, {"max_memory_mb": 8193}, {"job_timeout_seconds": 29}, {"job_timeout_seconds": 601},
    {"overseas_poll_interval_sec": 59}, {"overseas_poll_interval_sec": 3601}, {"overseas_enabled": 1},
    {"sources": {"public_company": {"schedule_mode": []}}}, {"sources": {"public_company": {"daily_at": "24:00"}}},
])
def test_bounded_config_validation(patch):
    with pytest.raises(ValueError):
        service.update_config(patch)


def test_daily_round_freezes_full_members_and_only_latest_window(monkeypatch):
    configure()
    first = service.enqueue(scheduled=True)["jobs"][0]
    scheduled = datetime(2026, 10, 31, 17, 30, tzinfo=store.SHANGHAI).timestamp()
    assert first["payload"]["symbols"] == POOL[:2]
    assert first["payload"]["scheduled_for"] == store.iso(scheduled)
    finish_batch(first)
    service.update_config({"symbols": ["000333.SZ"]})
    service._continue_rounds()
    second = next(job for job in store.list_jobs() if job["status"] == "queued")
    assert second["payload"]["symbols"] == POOL[2:4]
    finish_batch(second)
    service._continue_rounds()
    third = next(job for job in store.list_jobs() if job["status"] == "queued")
    assert third["payload"]["symbols"] == POOL[4:]
    finish_batch(third)
    assert state()["completed_symbols"] == state()["total_symbols"] == 5
    assert state()["pending_symbols"] == 0
    assert service.enqueue(scheduled=True)["jobs"] == []
    assert service.enqueue(scheduled=True)["jobs"] == []  # Fresh read, durable window dedup.
    monkeypatch.setattr(service.time, "time", lambda: AT + 7 * 86400)
    new = service.enqueue(scheduled=True)["jobs"]
    assert len(new) == 1 and new[0]["payload"]["symbols"] == ["000333.SZ"]
    assert new[0]["payload"]["scheduled_for"] == store.iso(scheduled + 7 * 86400)


def test_shanghai_pre_schedule_uses_latest_yesterday_then_todays_window(monkeypatch):
    configure(symbols=POOL[:1])
    before = datetime(2026, 11, 1, 0, 5, tzinfo=store.SHANGHAI).timestamp()
    monkeypatch.setattr(service.time, "time", lambda: before)
    job = service.enqueue(scheduled=True)["jobs"][0]
    assert datetime.fromisoformat(job["payload"]["scheduled_for"]).astimezone(store.SHANGHAI).day == 31
    finish_batch(job, now=before)
    assert service.enqueue(scheduled=True)["jobs"] == []
    after = datetime(2026, 11, 1, 17, 30, tzinfo=store.SHANGHAI).timestamp()
    monkeypatch.setattr(service.time, "time", lambda: after)
    new = service.enqueue(scheduled=True)["jobs"][0]
    assert new["payload"]["scheduled_for"] == store.iso(after)


def test_checkpoint_survives_interrupted_restart_and_batch_members_do_not_rotate():
    configure()
    job = service.enqueue(scheduled=True)["jobs"][0]
    store.claim_job(120)
    store.checkpoint_symbol(job["id"], POOL[0])
    store.recover_interrupted()
    recovered = store.claim_job(120)
    assert recovered["id"] == job["id"]
    assert store.pending_job_symbols(job["id"]) == [POOL[1]]
    store.checkpoint_symbol(job["id"], POOL[1])
    store.finish_job(job["id"], {"status": "success"})
    service._continue_rounds()
    assert next(job for job in store.list_jobs() if job["status"] == "queued")["payload"]["symbols"] == POOL[2:4]
    assert state()["completed_symbols"] == 2


def test_failure_preserves_pending_members_manual_obeys_publisher_backoff(monkeypatch):
    configure()
    first = service.enqueue(scheduled=True)["jobs"][0]
    finish_batch(first, fail=POOL[1])
    assert state()["completed_symbols"] == 1 and state()["pending_symbols"] == 4
    assert service.enqueue(["public_company"])["jobs"][0]["status"] == "cooldown"
    service._continue_rounds()
    assert store.usage()["queued"] == 0
    monkeypatch.setattr(service.time, "time", lambda: AT + 301)
    retry = service.enqueue(["public_company"])["jobs"][0]
    assert retry["payload"]["symbols"] == POOL[1:3]
    assert retry["payload"]["round_id"] == first["payload"]["round_id"]


def test_manual_promoted_round_can_continue_when_automatic_is_paused():
    configure()
    first = service.enqueue(scheduled=True)["jobs"][0]
    promoted = service.enqueue(["public_company"])["jobs"][0]
    assert promoted["id"] == first["id"] and promoted["payload"]["scheduled"] is False
    service.update_config({"enabled": False})
    finish_batch(promoted)
    service._continue_rounds()
    assert store.usage()["queued"] == 1


def test_default_single_lane_whole_round_finishes_in_batches(monkeypatch):
    configure()
    active = 0
    maximum = 0
    seen = []
    async def collect(source):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        job = service._current_job.get()
        for symbol in store.pending_job_symbols(job["id"]):
            seen.append(symbol)
            store.checkpoint_symbol(job["id"], symbol)
        await asyncio.sleep(0)
        active -= 1
        return {"status": "success"}
    monkeypatch.setattr(service, "collect_source", collect)
    result = asyncio.run(service.run_cycle())
    assert result["jobs_processed"] == 3
    assert maximum == 1 and seen == POOL
    assert store.usage()["jobs_started"] == 3


def test_memory_pressure_leaves_queue_and_budget_untouched(monkeypatch):
    service.update_config({"enabled": False})
    queued = service.enqueue(["public_news"])["jobs"][0]
    pressure = {"rss_mb": 900, "limit_mb": 768, "pressure": True, "supported": True, "reason": "rss_limit", "scope": "current_process"}
    monkeypatch.setattr(collector_resources, "snapshot", lambda limit: pressure)
    async def forbidden(_):
        pytest.fail("Memory pressure must prevent fetching")
    monkeypatch.setattr(service, "collect_source", forbidden)
    assert asyncio.run(service.run_cycle())["jobs_processed"] == 0
    assert store.usage()["jobs_started"] == 0
    assert store.list_jobs()[0]["id"] == queued["id"] and store.list_jobs()[0]["status"] == "queued"
    assert service.collection_overview()["worker"]["reason"] == "memory_pressure"


def test_global_switch_stops_manual_queue_without_spending(monkeypatch):
    service.enqueue_news([], None)
    monkeypatch.setattr(settings, "enable_data_collection", False)
    assert asyncio.run(service.run_cycle())["status"] == "disabled"
    assert store.usage()["queued"] == 1 and store.usage()["jobs_started"] == 0


def test_large_pool_is_visible_block_and_never_silently_truncated(monkeypatch):
    monkeypatch.setattr(service, "_symbol_pool", lambda: [f"{n:06d}.SZ" for n in range(3001)])
    result = service.enqueue(["public_company"])
    assert result["status"] == "blocked"
    assert state()["error_code"] == "symbol_pool_too_large"
    assert store.usage()["queued"] == 0


@pytest.mark.parametrize("running", [False, True])
def test_explicit_news_payload_dedup_and_conflict(running):
    first = service.enqueue_news(["600519.ss", "000001.SZ"], None)["jobs"][0]
    if running:
        store.claim_job(120)
    same = service.enqueue_news(["000001.SZ", "600519.SS"], None)["jobs"][0]
    assert first["id"] == same["id"]
    with pytest.raises(store.CollectionConflict):
        service.enqueue_news(["600519.SS"], None)
    with pytest.raises(store.CollectionConflict):
        service.enqueue_news(["000001.SZ", "600519.SS"], 14)


def test_automatic_news_not_falsely_deduplicated_as_explicit_request():
    store.enqueue("public_news", {"scheduled": True})
    with pytest.raises(store.CollectionConflict):
        service.enqueue_news([], 14)


@pytest.mark.parametrize("skip,code", [("already_running", "source_busy"), ("high_memory", "memory_pressure")])
def test_news_task_payload_reaches_adapter_and_busy_is_not_failure(monkeypatch, skip, code):
    service.update_config({"enabled": False})
    job = service.enqueue_news(["000001.SZ"], None)["jobs"][0]
    seen = []
    async def fetch(symbols, *, lookback_days):
        seen.append((symbols, lookback_days))
        return {"status": "skipped", "skipped": skip}
    monkeypatch.setattr(news_feed_sync, "sync_all_sources_to_archive", fetch)
    asyncio.run(service.run_cycle())
    assert seen == [(["000001.SZ"], None)]
    saved = next(row for row in store.list_jobs() if row["id"] == job["id"])
    assert saved["status"] == "skipped" and saved["error_code"] == code
    assert store.source_states()["public_news"].get("failures", 0) == 0


def test_collection_overview_never_scans_assets(monkeypatch):
    from app.storage import data_asset_manager
    monkeypatch.setattr(data_asset_manager, "scan_data_assets", lambda: pytest.fail("Full inventory scan"))
    result = service.collection_overview()
    assert {"config", "sources", "jobs", "datasets", "usage", "worker"} <= result.keys()
    assert result["worker"]["max_concurrent_jobs"] == 1


def test_real_company_adapter_checkpoints_success_and_retries_only_failed_member(monkeypatch):
    from app.services import company_profile_em
    configure(symbols=POOL[:3])
    failures = {POOL[1]}
    seen = []
    def fetch(symbol, **kwargs):
        seen.append(symbol)
        return ({"主营": "公开主营资料"}, None) if symbol not in failures else ({}, "unavailable")
    monkeypatch.setattr(company_profile_em, "fetch_company_survey", fetch)
    original_sleep = asyncio.sleep
    async def fast_sleep(delay):
        await original_sleep(0 if delay == 1 else delay)
    monkeypatch.setattr(service.asyncio, "sleep", fast_sleep)
    asyncio.run(service.run_cycle())
    assert seen == POOL[:2]
    assert state()["completed_symbols"] == 1 and state()["pending_symbols"] == 2
    failures.clear()
    monkeypatch.setattr(service.time, "time", lambda: AT + 121)
    asyncio.run(service.run_cycle())
    assert seen == [POOL[0], POOL[1], POOL[1], POOL[2]]
    assert state()["completed_symbols"] == 3 and state()["pending_symbols"] == 0
    assert store.dataset_stats()["records"] == 3


def test_interval_round_finishes_all_members_before_waiting_next_interval(monkeypatch):
    configure(symbols=POOL[:3])
    service.update_config({"sources": {"public_company": {"schedule_mode": "interval", "interval_minutes": 60}}})
    first = service.enqueue(scheduled=True)["jobs"][0]
    finish_batch(first)
    service._continue_rounds()
    second = next(job for job in store.list_jobs() if job["status"] == "queued")
    finish_batch(second)
    assert service.enqueue(scheduled=True)["jobs"] == []
    monkeypatch.setattr(service.time, "time", lambda: AT + 3600)
    assert service.enqueue(scheduled=True)["jobs"][0]["payload"]["symbols"] == POOL[:2]


def test_per_job_timeout_honors_config_and_records_backoff(monkeypatch):
    service.update_config({"enabled": False, "job_timeout_seconds": 30})
    service.enqueue(["public_market"])
    seen = []
    async def collect(_):
        return {"status": "success"}
    async def timeout(awaitable, *, timeout):
        seen.append(timeout)
        awaitable.close()
        raise TimeoutError()
    monkeypatch.setattr(service, "collect_source", collect)
    monkeypatch.setattr(service.asyncio, "wait_for", timeout)
    asyncio.run(service.run_cycle())
    assert seen == [30]
    assert store.list_jobs()[0]["error_code"] == "timeout"
    assert service.enqueue(["public_market"])["jobs"][0]["status"] == "cooldown"


def test_news_payload_supports_a_hk_us_without_collapsing_none_and_empty():
    job = service.enqueue_news(["AAPL", "700.hk", "600519.sh", "aapl"], 7)["jobs"][0]
    assert job["payload"]["news_symbols"] == ["0700.HK", "600519.SS", "AAPL"]
    with pytest.raises(store.CollectionConflict):
        service.enqueue_news(None, 7)
    with pytest.raises(store.CollectionConflict):
        service.enqueue_news([], 7)


def test_busy_daily_news_is_not_marked_complete_and_retries_without_failure(monkeypatch):
    service.update_config({"use_watchlists": False, "sources": {row["id"]: {"enabled": row["id"] == "public_news", "schedule_mode": "daily", "daily_at": "17:30"} for row in service.catalog()}})
    first = service.enqueue(scheduled=True)["jobs"][0]
    store.claim_job(120)
    store.finish_job(first["id"], {"status": "skipped", "error_code": "source_busy"})
    assert service.enqueue(scheduled=True)["jobs"] == []
    monkeypatch.setattr(service.time, "time", lambda: AT + 61)
    retry = service.enqueue(scheduled=True)["jobs"][0]
    assert retry["id"] != first["id"]
    assert retry["payload"]["window_key"] == first["payload"]["window_key"]
    assert store.source_states()["public_news"].get("failures", 0) == 0
