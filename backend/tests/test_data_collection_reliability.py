"""Regressions for independent, recoverable automatic public-data collection."""
import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.security.user_context import current_user_id
from app.services import data_collection as service, news_feed_sync
from app.storage import data_collection_store as store


@pytest.fixture(autouse=True)
def isolated_worker(monkeypatch):
    monkeypatch.setattr(settings, "enable_data_collection", True)
    service.update_config({"enabled": False})
    monkeypatch.setattr(service.shutil, "disk_usage", lambda root: SimpleNamespace(free=20 * 1024 ** 3))
    monkeypatch.setattr("app.services.runtime_telemetry.record_collection", lambda *args, **kwargs: None)
    store.set_state("last_maintenance", time.time())


def test_automatic_collection_ignores_legacy_scheduler_flag(monkeypatch):
    monkeypatch.setattr(settings, "enable_scheduler", False)
    monkeypatch.setattr(settings, "enable_data_collection", True)
    service.update_config({"enabled": True, "symbols": [], "use_watchlists": False,
                           "sources": {row["id"]: {"enabled": row["id"] in {"public_news", "public_market", "public_kline"}}
                                       for row in service.catalog()}})
    called = []

    async def collect(source_id):
        called.append((source_id, current_user_id.get()))
        return {"status": "success", "records_received": 1, "records_added": 0}

    monkeypatch.setattr(service, "collect_source", collect)
    result = asyncio.run(service.run_cycle())
    assert result["jobs_processed"] == 2
    assert set(called) == {("public_news", 0), ("public_market", 0)}
    assert store.usage()["jobs_started"] == 2  # Empty stock-dependent jobs use no quota.


def test_slow_source_does_not_block_other_lanes_or_later_queued_jobs(monkeypatch):
    service.update_config({"max_concurrent_jobs": 3})
    async def scenario():
        slow_started, release_slow, fast_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        counts = {"running": 0, "max": 0, "fast": 0}
        source_ids = [row["id"] for row in service.catalog()][:6]
        slow_source = source_ids[0]
        for index, source_id in enumerate(source_ids):
            store.enqueue(source_id, {"scheduled": False}, now=time.time() - 100 + index)

        async def collect(source_id):
            counts["running"] += 1
            counts["max"] = max(counts["max"], counts["running"])
            try:
                assert current_user_id.get() == 0
                if source_id == slow_source:
                    slow_started.set()
                    await release_slow.wait()
                else:
                    await asyncio.sleep(0)
                    counts["fast"] += 1
                    if counts["fast"] == 5:
                        fast_finished.set()
                return {"status": "success"}
            finally:
                counts["running"] -= 1

        monkeypatch.setattr(service, "collect_source", collect)
        task = asyncio.create_task(service.run_cycle(owner="parallel-worker"))
        try:
            await asyncio.wait_for(slow_started.wait(), 2)
            await asyncio.wait_for(fast_finished.wait(), 2)
            assert not task.done()
            worker = store.get_state("worker")
            assert worker["active_jobs"][0]["source_id"] == slow_source
            assert (await service.run_cycle(owner="duplicate"))["status"] == "another_worker"
        finally:
            release_slow.set()
            result = await task
        assert result["jobs_processed"] == 6
        assert counts["max"] <= 3
        assert store.get_state("worker")["active_jobs"] == []

    asyncio.run(scenario())


def test_parallel_claims_remain_bounded_by_cycle_and_daily_budget(monkeypatch):
    for source in service.catalog()[:10]:
        store.enqueue(source["id"])
    service.update_config({"daily_job_limit": 4, "max_concurrent_jobs": 3})

    async def collect(source_id):
        await asyncio.sleep(0)
        return {"status": "success"}

    monkeypatch.setattr(service, "collect_source", collect)
    result = asyncio.run(service.run_cycle())
    assert result["jobs_processed"] == store.usage()["jobs_started"] == 4
    assert store.usage()["queued"] == 6
    assert store.get_state("worker")["reason"] == "daily_budget_exhausted"


def test_failed_daily_source_retries_after_backoff_from_completion():
    now = time.time()
    store.enqueue("public_company", now=now - 300)
    job = store.claim_job(120, now=now - 240)
    store.finish_job(job["id"], {"status": "failed", "error_code": "network_error"}, now=now)
    state = next(row for row in service.sources() if row["id"] == "public_company")
    assert state["duration_seconds"] == 240
    assert state["last_finished_at"] == store.iso(now)
    assert datetime.fromisoformat(state["next_run_at"]).timestamp() == pytest.approx(now + 120)


def test_skips_are_not_source_failures_and_partial_fetch_updates_success_time():
    now = time.time()
    store.enqueue("public_company", now=now)
    job = store.claim_job(120, now=now)
    store.finish_job(job["id"], {"status": "skipped", "error_code": "empty_stock_pool"}, now=now + 1)
    assert store.source_states()["public_company"].get("failures", 0) == 0
    store.enqueue("public_company", now=now + 120)
    job = store.claim_job(120, now=now + 120)
    store.finish_job(job["id"], {"status": "partial", "records_received": 1, "records_added": 0,
                                "error_code": "partial_source_failure"}, now=now + 121)
    state = store.source_states()["public_company"]
    assert state["failures"] == 1
    assert state["last_success_at"] == store.iso(now + 121)


def test_recovered_job_does_not_starve_unattempted_sources():
    now = time.time()
    interrupted = store.enqueue("slow_source", now=now - 100)
    untouched = store.enqueue("fast_source", now=now - 90)
    assert store.claim_job(120, now=now - 80)["id"] == interrupted["id"]
    store.recover_interrupted()
    assert store.claim_job(120, now=now)["id"] == untouched["id"]
    assert store.claim_job(120, now=now + 1)["id"] == interrupted["id"]
    assert store.usage(now=now)["jobs_started"] == 3


def test_public_news_collects_broad_sources_with_empty_stock_pool(monkeypatch):
    calls = []
    stamp = time.time() - 60
    headline = {"title": "Public macro release", "url": "https://example.org/story", "source": "Public source",
                "published_ts": stamp, "summary": "Published public data"}

    async def rss(symbols, collection_stats=None, collector=None):
        assert symbols == []
        calls.append("rss")
        collection_stats["records_received"] = 1
        return [headline]

    async def cn(**kwargs):
        calls.append("cn")
        return {"items": [], "records_received": 0}

    async def tech(collection_stats=None, **kwargs):
        calls.append("tech")
        collection_stats["records_received"] = 0
        return {"network_items": []}

    monkeypatch.setattr(news_feed_sync.rss_fetcher, "fetch_news_for_symbols", rss)
    monkeypatch.setattr(news_feed_sync.cn_market_news, "fetch_cn_headlines", cn)
    monkeypatch.setattr(news_feed_sync.china_tech_media_feeds, "fetch_tech_media_feeds", tech)
    service.update_config({"symbols": [], "use_watchlists": False})
    result = asyncio.run(service.collect_source("public_news"))
    assert result["status"] == "success"
    assert result["records_received"] == result["records_added"] == 1
    assert calls == ["rss", "cn", "tech"]


def test_timeouts_have_specific_safe_diagnostic():
    assert service._safe_error(TimeoutError("token=never-store-this")) == "timeout"


def test_background_union_does_not_create_default_holdings(tmp_path):
    from app.storage import watchlist_store

    assert watchlist_store.load_all_symbols_union() == []
    assert not (tmp_path / "watchlists").exists()
    assert watchlist_store.load_symbols(1) == list(watchlist_store.DEFAULT_SYMBOLS)
    assert (tmp_path / "watchlists" / "1.json").exists()


def test_background_union_reads_legacy_without_copying_and_respects_empty_admin(tmp_path):
    import json
    from app.storage import watchlist_store

    legacy = tmp_path / "watchlist.json"
    legacy.write_text(json.dumps({"symbols": ["600000.SS"]}))
    assert watchlist_store.load_all_symbols_union() == ["600000.SS"]
    assert not (tmp_path / "watchlists").exists()
    directory = tmp_path / "watchlists"
    directory.mkdir()
    (directory / "1.json").write_text('{"symbols":[]}')
    assert watchlist_store.load_all_symbols_union() == []


def test_broad_news_archive_does_not_seed_watchlists(tmp_path, monkeypatch):
    from app.storage import news_store

    item = {"title": "Global publication", "url": "https://example.org/story", "source": "Public",
            "published_ts": time.time() - 60}
    assert news_store.upsert_many([item]) == 1
    assert not (tmp_path / "watchlists").exists()


def test_news_provider_failure_keeps_other_provider_results(monkeypatch):
    stamp = time.time() - 60
    item = {"title": "Healthy provider headline", "url": "https://example.org/healthy", "source": "Public",
            "published_ts": stamp}

    async def rss(*args, **kwargs):
        raise RuntimeError("RSS network unavailable")

    async def cn(**kwargs):
        return {"items": [item], "records_received": 1}

    async def tech(**kwargs):
        return {"network_items": [], "records_received": 0}

    monkeypatch.setattr(news_feed_sync.rss_fetcher, "fetch_news_for_symbols", rss)
    monkeypatch.setattr(news_feed_sync.cn_market_news, "fetch_cn_headlines", cn)
    monkeypatch.setattr(news_feed_sync.china_tech_media_feeds, "fetch_tech_media_feeds", tech)
    service.update_config({"symbols": [], "use_watchlists": False})
    result = asyncio.run(service.collect_source("public_news"))
    assert result["status"] == "partial"
    assert result["records_received"] == result["records_added"] == 1
    assert result["error_code"] == "partial_source_failure"


def test_legacy_watchlist_participates_in_scheduled_symbol_pool_without_migration(tmp_path):
    import json

    (tmp_path / "watchlist.json").write_text(json.dumps({"symbols": ["600000.SS", "AAPL"]}))
    assert service._symbols("public_company") == ["600000.SS"]
    assert not (tmp_path / "watchlists").exists()


def test_pause_stops_queued_automatic_jobs_while_other_sources_are_running(monkeypatch):
    monkeypatch.setattr(settings, "enable_data_collection", True)
    source_ids = [row["id"] for row in service.catalog() if row["id"] not in service._STOCK_SOURCES][:4]
    service.update_config({"enabled": True, "max_concurrent_jobs": 3, "sources": {row["id"]: {"enabled": row["id"] in source_ids} for row in service.catalog()}})

    async def scenario():
        three_started, release = asyncio.Event(), asyncio.Event()
        called = []

        async def collect(source_id):
            called.append(source_id)
            if len(called) == 3:
                three_started.set()
            await release.wait()
            return {"status": "success"}

        monkeypatch.setattr(service, "collect_source", collect)
        task = asyncio.create_task(service.run_cycle())
        try:
            await asyncio.wait_for(three_started.wait(), 2)
            service.update_config({"enabled": False})
        finally:
            release.set()
            await task
        assert len(called) == 3
        assert store.usage()["jobs_started"] == 3
        skipped = [job for job in store.list_jobs() if job["status"] == "skipped"]
        assert len(skipped) == 1 and skipped[0]["error_code"] == "automatic_collection_disabled"

    asyncio.run(scenario())


def test_invalid_adapter_result_is_finished_with_backoff_not_recovered_forever(monkeypatch):
    store.enqueue("public_market")

    async def malformed(source_id):
        return {"status": "success", "records_received": 1, "records_added": 2}

    monkeypatch.setattr(service, "collect_source", malformed)
    assert asyncio.run(service.run_cycle())["status"] == "processed"
    job = store.list_jobs()[0]
    assert job["status"] == "failed" and job["error_code"] == "invalid_payload"
    assert store.usage()["running"] == 0
    state = next(row for row in service.sources() if row["id"] == "public_market")
    assert state["next_run_at"] is not None and state["failures"] == 1
