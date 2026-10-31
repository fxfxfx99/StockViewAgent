import asyncio
import time
from datetime import datetime, timezone

import pytest

from app.services import kline_pipeline
from app.services.market_time import SHANGHAI_TZ

NOW = int(time.time()) - 86400


def test_major_index_uses_cached_bundle(monkeypatch):
    cached = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"candles": [{"t": NOW, "o": 3000, "h": 3100, "l": 2900, "c": 3000}],
                    "metrics": {}, "index_key": "SH_COMP", "kline_source": "eastmoney"},
    }
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: cached)

    async def fail_live(*args, **kwargs):
        raise AssertionError("cache should avoid live fetch")

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", fail_live)
    out = asyncio.run(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d"))
    assert out["kline_source"] == "eastmoney"  # Cache retains actual provider provenance.
    assert out["cache_saved_at"] == cached["saved_at"] and out["served_from_cache"]
    assert not out["cache_stale"]


def test_major_index_falls_back_to_tencent_with_index_symbol(monkeypatch):
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: None)
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: None)
    monkeypatch.setattr(kline_pipeline, "is_baostock_installed", lambda: False)

    async def fail_primary(*args, **kwargs):
        raise ValueError("primary down")

    def tencent_ok(symbol, range_param, interval, *, provider_symbol=None):
        assert provider_symbol == "sh000001"
        return {"candles": [{"t": NOW, "o": 3000, "h": 3100, "l": 2900, "c": 3000}], "metrics": {}, "symbol": symbol}

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", fail_primary)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_secondary_sync", tencent_ok)
    out = asyncio.run(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d"))
    assert out["index_key"] == "SH_COMP"
    assert out["kline_source"] == "tencent_index_fallback"


def test_expired_index_cache_refreshes_and_failed_refresh_preserves_observation(monkeypatch):
    cached = {"saved_at": "2026-01-01T00:00:00+00:00", "payload": {
        "candles": [{"t": NOW, "o": 3000, "h": 3100, "l": 2900, "c": 3000}], "kline_source": "eastmoney"}}
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: cached)
    writes, calls = [], []
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: writes.append(args))
    monkeypatch.setattr(kline_pipeline, "is_baostock_installed", lambda: False)

    async def fail(*args):
        calls.append("async")
        raise RuntimeError("private provider diagnostic")

    def fail_sync(*args, **kwargs):
        calls.append("sync")
        raise RuntimeError("private provider diagnostic")

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", fail)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_secondary_sync", fail_sync)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_sina_sync", fail_sync)
    monkeypatch.setattr(kline_pipeline.yahoo_market, "fetch_kline_bundle", fail)
    result = asyncio.run(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d"))
    assert len(calls) == 4
    assert result["cache_stale"] and result["cache_saved_at"] == cached["saved_at"]
    assert "private provider diagnostic" not in str(result)
    assert writes == []


@pytest.mark.parametrize("bar", [
    {"t": NOW, "o": 10, "h": 9, "l": 8, "c": 10},
    {"t": NOW, "o": 10, "h": 11, "l": 8, "c": float("nan")},
])
def test_bad_primary_bars_fall_back_without_poisoning_cache(monkeypatch, bar):
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: None)
    writes = []
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: writes.append(args))

    async def primary(*args):
        return {"candles": [bar]}

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", primary)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_secondary_sync",
                        lambda *args, **kwargs: {"candles": [{"t": NOW, "o": 10, "h": 11, "l": 8, "c": 9}]})
    out = asyncio.run(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d", public_only=True))
    assert out["kline_source"] == "tencent_index_fallback"
    assert len(writes) == 1 and writes[0][-1]["candles"][0]["c"] == 9


def test_concurrent_index_reads_share_one_refresh(monkeypatch):
    calls = []

    async def primary(*args):
        calls.append(args)
        await asyncio.sleep(0.02)
        return {"candles": [{"t": NOW, "o": 10, "h": 11, "l": 8, "c": 9}]}

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", primary)

    async def scenario():
        return await asyncio.gather(*(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d") for _ in range(3)))

    result = asyncio.run(scenario())
    assert len(calls) == 1
    assert all(not row["served_from_cache"] for row in result)
    assert result[0] == result[1] == result[2]
    result[0]["candles"][0]["c"] = 100
    assert result[1]["candles"][0]["c"] == 9


@pytest.mark.parametrize("has_cache", [False, True])
def test_concurrent_failed_refreshes_share_fallback_and_next_batch_can_retry(monkeypatch, has_cache):
    cached = {"saved_at": "2026-01-01T00:00:00+00:00", "payload": {
        "candles": [{"t": NOW, "o": 10, "h": 11, "l": 8, "c": 9}], "kline_source": "eastmoney"}}
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: cached if has_cache else None)
    writes, calls = [], []
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: writes.append(args))

    async def fail(*args):
        calls.append("async")
        await asyncio.sleep(0.01)
        raise RuntimeError("private diagnostic")

    def fail_sync(*args, **kwargs):
        calls.append("sync")
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", fail)
    monkeypatch.setattr(kline_pipeline.yahoo_market, "fetch_kline_bundle", fail)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_secondary_sync", fail_sync)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_sina_sync", fail_sync)

    async def scenario():
        for batch in range(2):
            results = await asyncio.gather(*(kline_pipeline.fetch_major_index_kline_with_fallbacks(
                "SH_COMP", "1y", "1d", public_only=True) for _ in range(3)), return_exceptions=True)
            assert len(calls) == (batch + 1) * 4
            if has_cache:
                assert all(row["cache_stale"] and row["cache_saved_at"] == cached["saved_at"] for row in results)
            else:
                assert all(isinstance(row, RuntimeError) for row in results)
            assert all("private diagnostic" not in str(row) for row in results)

    asyncio.run(scenario())
    assert writes == []


def test_one_cancelled_waiter_does_not_cancel_shared_forced_refresh(monkeypatch):
    cached = {"saved_at": datetime.now(timezone.utc).isoformat(), "payload": {
        "candles": [{"t": NOW, "o": 10, "h": 11, "l": 8, "c": 9}], "kline_source": "eastmoney"}}
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: cached)
    writes, calls = [], []
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: writes.append(args))

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def primary(*args):
            calls.append(args)
            started.set()
            await release.wait()
            return {"candles": [{"t": NOW, "o": 10, "h": 11, "l": 8, "c": 10}]}

        monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", primary)
        first = asyncio.create_task(kline_pipeline.fetch_major_index_kline_with_fallbacks(
            "SH_COMP", "1y", "1d", force_refresh=True))
        await started.wait()
        second = asyncio.create_task(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        release.set()
        result = await second
        assert result["candles"][0]["c"] == 10
        assert not result["served_from_cache"]

    asyncio.run(scenario())
    assert len(calls) == len(writes) == 1


def test_all_cancelled_waiters_stop_refresh_without_cache_write_and_allow_retry(monkeypatch):
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: None)
    writes = []
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: writes.append(args))

    async def scenario():
        started, cancelled = asyncio.Event(), asyncio.Event()
        calls = []

        async def primary(*args):
            calls.append(args)
            if len(calls) == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return {"candles": [{"t": NOW, "o": 10, "h": 11, "l": 8, "c": 9}]}

        monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", primary)
        requests = [asyncio.create_task(kline_pipeline.fetch_major_index_kline_with_fallbacks(
            "SH_COMP", "1y", "1d")) for _ in range(2)]
        await started.wait()
        for request in requests:
            request.cancel()
        results = await asyncio.gather(*requests, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert cancelled.is_set() and writes == []
        assert not kline_pipeline._INDEX_FLIGHTS[asyncio.get_running_loop()]
        result = await kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d")
        assert len(calls) == 2 and not result["cache_stale"]

    asyncio.run(scenario())
    assert len(writes) == 1


@pytest.mark.parametrize("now_hour", [0, 9, 23])
def test_index_date_rejects_tomorrow_in_shanghai_but_allows_later_today(monkeypatch, now_hour):
    now = datetime(2026, 10, 31, now_hour, 1, tzinfo=SHANGHAI_TZ).timestamp()
    monkeypatch.setattr(kline_pipeline.time, "time", lambda: now)
    today_later = datetime(2026, 10, 31, 23, 59, tzinfo=SHANGHAI_TZ).timestamp()
    tomorrow = datetime(2026, 11, 1, 0, 0, tzinfo=SHANGHAI_TZ).timestamp()
    bar = {"t": today_later, "o": 10, "h": 11, "l": 8, "c": 9}
    kline_pipeline._validate_index_bundle({"candles": [bar]})
    with pytest.raises(ValueError):
        kline_pipeline._validate_index_bundle({"candles": [{**bar, "t": tomorrow}]})


def test_valid_but_older_provider_cannot_replace_more_recent_cache(monkeypatch):
    cached = {"saved_at": "2026-01-01T00:00:00+00:00", "payload": {
        "candles": [{"t": NOW, "o": 10, "h": 11, "l": 8, "c": 9}], "kline_source": "eastmoney"}}
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: cached)
    writes = []
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: writes.append(args))
    old = {"candles": [{"t": NOW - 30 * 86400, "o": 10, "h": 11, "l": 8, "c": 10}]}

    async def stale(*args):
        return old

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", stale)
    monkeypatch.setattr(kline_pipeline.yahoo_market, "fetch_kline_bundle", stale)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_secondary_sync", lambda *args, **kwargs: old)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_sina_sync", lambda *args, **kwargs: old)
    out = asyncio.run(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d", public_only=True))
    assert out["cache_stale"] and out["candles"][0]["t"] == NOW
    assert out["cache_saved_at"] == cached["saved_at"]
    assert not writes


def test_broken_cache_payload_is_replaced_by_valid_live_result(monkeypatch):
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: {"payload": "bad", "saved_at": "bad"})
    writes = []
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: writes.append(args))

    async def primary(*args):
        return {"candles": [{"t": NOW, "o": 10, "h": 11, "l": 8, "c": 9}], "metrics": "broken"}

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", primary)
    out = asyncio.run(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d"))
    assert out["metrics"] == {"name": "上证指数"}
    assert not out["cache_stale"] and len(writes) == 1
