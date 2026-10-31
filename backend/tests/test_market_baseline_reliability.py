"""Global collection checkpoints stay bounded, resumable and public-data-only."""
import asyncio
from datetime import date, timedelta
import json

import pytest

from app.services import macro_workspace as service
from app.storage import data_collection_store as store


NOW = 1793400000.0


def bundle():
    return {"candles": [{"t": NOW - 86400, "date": "2026-10-29", "o": 10, "h": 11, "l": 8, "c": 9, "v": 123}],
            "kline_source": "yahoo", "cache_stale": False, "data_as_of": "2026-10-29"}


@pytest.mark.parametrize("saved", [None, {}, "old-format", [None, 42, [], {"key": []},
    {"key": "SH_COMP", "status": [], "consecutive_failures": "bad", "last_success_at": "bad", "next_retry_at": "bad"}]])
def test_invalid_checkpoint_schema_does_not_break_status_or_future_collection(monkeypatch, saved):
    monkeypatch.setattr(service, "indices", lambda: [{"key": "SH_COMP", "label": "上证指数"}])
    store.set_state("public_market_components", saved)
    assert service.collection_status()["summary"] == {
        "total": 1, "success": 0, "failed": 0, "stale": 0, "pending": 1, "running": 0}

    async def fetch(*args, **kwargs):
        return bundle()

    monkeypatch.setattr(service, "index_kline", fetch)
    assert asyncio.run(service.collect_market_baseline())["status"] == "success"
    assert store.get_state("public_market_components")[0]["status"] == "success"


def test_status_only_exposes_typed_progress_fields_not_legacy_metadata():
    # Emulate an older writer; bypass current set_state's own credential filter.
    legacy = [{"key": "FUT_GOLD", "status": "failed", "label": "private label",
               "api_key": "private-key-marker", "debug": "private-upstream-marker",
               "provider": "https://private.example/?token=private-token-marker",
               "error_code": "private-exception-marker", "consecutive_failures": True,
               "last_attempt_at": "2026-10-31T00:00:00+00:00", "last_success_at": "2026-10-30T00:00:00",
               "data_as_of": "not-a-date", "records_received": -1, "records_added": 1e20,
               "candles": [{"secret": "private-history-marker"}]}]
    with store.connection() as conn:
        conn.execute("INSERT INTO dc_settings VALUES(?,?)", ("public_market_components", store.canonical(legacy)))
    status = service.collection_status()
    serialized = json.dumps(status, ensure_ascii=False)
    assert "private-" not in serialized and "private label" not in serialized
    row = next(item for item in status["items"] if item["key"] == "FUT_GOLD")
    assert row["status"] == "failed" and row["last_attempt_at"] == "2026-10-31T00:00:00+00:00"
    assert not ({"consecutive_failures", "records_received", "records_added", "last_success_at", "data_as_of", "candles"} & row.keys())


def test_full_catalog_with_long_histories_keeps_checkpoint_and_status_under_50k(monkeypatch):
    candles = [{"t": NOW - (1500 - index) * 86400, "date": (date(2022, 1, 1) + timedelta(days=index)).isoformat(),
                "o": 10, "h": 11, "l": 8, "c": 9, "v": index} for index in range(1500)]
    archived = []

    async def fetch(*args, **kwargs):
        return {**bundle(), "candles": candles}

    def ingest(source_id, category, rows):
        archived.append(len(rows))
        assert all(row["source_id"] == "public_market" for row in rows)
        return {"records_received": len(rows), "records_added": len(rows)}

    monkeypatch.setattr(service, "index_kline", fetch)
    monkeypatch.setattr(store, "ingest", ingest)
    result = asyncio.run(service.collect_market_baseline())
    assert result["status"] == "success"
    assert len(archived) == len(service.indices()) == 19
    assert result["records_received"] == result["records_added"] == 19 * 1500
    checkpoint = store.get_state("public_market_components")
    assert len(store.canonical(checkpoint).encode()) < 50_000
    assert len(json.dumps(service.collection_status(), ensure_ascii=False).encode()) < 50_000
    assert all("candles" not in row for row in checkpoint)


def test_failed_refresh_preserves_last_success_and_dynamic_component_cooldown(monkeypatch):
    clock, calls = [NOW], []
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    monkeypatch.setattr(service, "indices", lambda: [{"key": "SH_COMP", "label": "上证指数"}])
    store.set_state("public_market_components", [{"key": "SH_COMP", "status": "success", "provider": "eastmoney",
        "last_success_at": store.iso(NOW - 7200), "data_as_of": "2026-10-29", "consecutive_failures": 0}])

    async def failed(*args, **kwargs):
        calls.append(clock[0])
        raise RuntimeError("private-upstream-error access_token=private-secret")

    monkeypatch.setattr(service, "index_kline", failed)
    first = asyncio.run(service.collect_market_baseline())
    assert first["retry_after_seconds"] == 120
    clock[0] += 120
    second = asyncio.run(service.collect_market_baseline())
    assert second["retry_after_seconds"] == 240
    skipped = asyncio.run(service.collect_market_baseline())
    assert skipped["retry_after_seconds"] == 240 and len(calls) == 2
    assert "private-" not in str(second)
    state = store.get_state("public_market_components")[0]
    assert state["last_success_at"] == store.iso(NOW - 7200)
    assert state["provider"] == "eastmoney" and state["data_as_of"] == "2026-10-29"


def test_next_retry_wakes_for_healthy_component_before_long_failure_cooldown(monkeypatch):
    monkeypatch.setattr(service.time, "time", lambda: NOW)
    monkeypatch.setattr(service, "indices", lambda: [{"key": "OK", "label": "正常"}, {"key": "FAIL", "label": "失败"}])
    store.set_state("public_market_components", [
        {"key": "OK", "status": "success", "last_success_at": store.iso(NOW - 3500)},
        {"key": "FAIL", "status": "failed", "next_retry_at": store.iso(NOW + 1800)}])

    async def unexpected(*args, **kwargs):
        pytest.fail("no component is due yet")

    monkeypatch.setattr(service, "index_kline", unexpected)
    result = asyncio.run(service.collect_market_baseline())
    assert result["status"] == "partial" and result["retry_after_seconds"] == 100


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("provider_delay,expected", [(900, 900), (86400, 21600)])
def test_provider_cooldown_survives_exception_or_stale_fallback(monkeypatch, stale, provider_delay, expected):
    monkeypatch.setattr(service.time, "time", lambda: NOW)
    monkeypatch.setattr(service, "indices", lambda: [{"key": "FUT_GOLD", "label": "黄金"}])

    async def throttled(*args, **kwargs):
        if stale:
            return {**bundle(), "cache_stale": True, "error_code": "provider_cooldown",
                    "provider_retry_at": store.iso(NOW + provider_delay)}
        error = RuntimeError("private upstream Retry-After response")
        error.error_code = "source_rate_limited"
        error.retry_at = store.iso(NOW + provider_delay)
        raise error

    monkeypatch.setattr(service, "index_kline", throttled)
    result = asyncio.run(service.collect_market_baseline())
    assert result["retry_after_seconds"] == expected
    state = store.get_state("public_market_components")[0]
    assert state["error_code"] == ("provider_cooldown" if stale else "source_rate_limited")
    assert state["next_retry_at"] == store.iso(NOW + expected)
    assert "private" not in str(result)
    assert store.dataset_stats()["records"] == 0


def test_cancellation_prioritizes_never_started_markets_and_bounds_active_requests(monkeypatch):
    monkeypatch.setattr(service.time, "time", lambda: NOW)
    monkeypatch.setattr(service, "indices", lambda: [{"key": key, "label": key} for key in ("A", "B", "C")])
    store.set_state("public_market_components", [
        {"key": key, "status": "failed", "last_attempt_at": store.iso(NOW - delay)}
        for key, delay in (("A", 30), ("B", 20), ("C", 10))])

    async def scenario():
        attempts, active = [[], []], {"count": 0, "max": 0}
        phase, started = [0], asyncio.Event()

        async def slow(key, *args, **kwargs):
            attempts[phase[0]].append(key)
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            if len(attempts[phase[0]]) == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active["count"] -= 1

        monkeypatch.setattr(service, "index_kline", slow)
        for index in range(2):
            phase[0] = index
            started.clear()
            task = asyncio.create_task(service.collect_market_baseline())
            try:
                await asyncio.wait_for(started.wait(), 2)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert active["count"] == 0
        assert attempts == [["A", "B"], ["C", "A"]]
        assert active["max"] == 2
        assert all(row["status"] == "pending" for row in store.get_state("public_market_components"))

    asyncio.run(scenario())
