import asyncio
import time

from app.services import startup_data_check
from app.services import collector_resources, data_collection


def test_refresh_stock_list_skips_when_fresh(monkeypatch):
    monkeypatch.setattr(
        startup_data_check.a_share_stocks,
        "meta",
        lambda: {"count": 10, "updated_at": int(time.time())},
    )

    def fail_refresh():
        raise AssertionError("fresh stock list should not refresh")

    monkeypatch.setattr(startup_data_check.a_share_stocks, "fetch_and_save", fail_refresh)

    out = asyncio.run(startup_data_check._refresh_stock_list_if_stale())

    assert out["status"] == "skipped"
    assert out["reason"] == "fresh"
    assert out["count"] == 10


def test_refresh_stock_list_runs_when_stale(monkeypatch):
    monkeypatch.setattr(
        startup_data_check.a_share_stocks,
        "meta",
        lambda: {"count": 10, "updated_at": 1},
    )
    monkeypatch.setattr(
        startup_data_check.a_share_stocks,
        "fetch_and_save",
        lambda: {"count": 11, "updated_at": 2},
    )

    out = asyncio.run(startup_data_check._refresh_stock_list_if_stale())

    assert out["status"] == "ok"
    assert out["reason"] == "refreshed"
    assert out["count"] == 11


def warm_fixture(monkeypatch, *, cursor=3, limit=3):
    symbols = ["600000.SS", "600001.SS", "600002.SS", "600003.SS"]
    state = {"steps": [{"name": "watchlist_market_data", "next_cursor": cursor}]}
    calls = []
    monkeypatch.setattr(startup_data_check.watchlist_store, "load_all_symbols_union", lambda: symbols)
    monkeypatch.setattr(startup_data_check.settings, "startup_data_check_watchlist_limit", limit)
    monkeypatch.setattr(startup_data_check, "load_state", lambda: state)
    monkeypatch.setattr(data_collection, "get_config", lambda: {"max_concurrent_jobs": 1, "max_memory_mb": 768})

    async def kline(symbol, *args):
        calls.append(symbol)
        return {"candles": [{"close": 1}], "kline_source": "fixture"}

    async def flow(symbol):
        return []

    monkeypatch.setattr(startup_data_check.kline_pipeline, "fetch_a_share_kline_with_fallbacks", kline)
    monkeypatch.setattr(startup_data_check.market_extra_http, "fetch_stock_capital_flow", flow)
    monkeypatch.setattr(startup_data_check.kline_bundle_cache, "save_bundle", lambda *args: None)
    monkeypatch.setattr(startup_data_check.market_history_cache, "merge_save_series", lambda *args: {"items": []})
    return symbols, state, calls


def test_partial_memory_skip_retries_first_unchecked_member_across_wraparound(monkeypatch):
    symbols, state, calls = warm_fixture(monkeypatch)
    pressure = iter([False, True, True])
    monkeypatch.setattr(collector_resources, "snapshot", lambda *args: {"pressure": next(pressure)})

    result = asyncio.run(startup_data_check._warm_watchlist_market_data())

    assert calls == [symbols[3]]
    assert result["status"] == "warning" and result["reason"] == "high_memory"
    assert result["symbols_checked"] == 1 and result["symbols_skipped"] == 2
    assert result["cursor"] == 3 and result["next_cursor"] == 0
    assert [row["symbol"] for row in result["items"] if row.get("status") == "skipped"] == symbols[:2]

    state["steps"] = [{"name": "watchlist_market_data", **result}]
    monkeypatch.setattr(collector_resources, "snapshot", lambda *args: {"pressure": False})
    resumed = asyncio.run(startup_data_check._warm_watchlist_market_data())
    assert calls == [symbols[3], *symbols[:3]]
    assert resumed["status"] == "ok" and resumed["symbols_checked"] == 3
    assert resumed["symbols_skipped"] == 0 and resumed["next_cursor"] == 3


def test_all_members_skipped_keep_cursor_and_do_not_report_checks(monkeypatch):
    _, _, calls = warm_fixture(monkeypatch, cursor=2)
    monkeypatch.setattr(collector_resources, "snapshot", lambda *args: {"pressure": True})
    result = asyncio.run(startup_data_check._warm_watchlist_market_data())
    assert calls == []
    assert result["status"] == "skipped" and result["reason"] == "high_memory"
    assert result["symbols_checked"] == 0 and result["symbols_skipped"] == 3
    assert result["next_cursor"] == result["cursor"] == 2


def test_whole_startup_memory_gate_preserves_retry_position_and_warns(monkeypatch):
    _, state, calls = warm_fixture(monkeypatch, cursor=2)
    monkeypatch.setattr(collector_resources, "snapshot", lambda *args: {"pressure": True})
    monkeypatch.setattr(startup_data_check, "_save_state", lambda value: state.update(value))

    result = asyncio.run(startup_data_check._run_startup_data_check_locked())

    assert calls == [] and result["status"] == "warning"
    assert all(step["status"] == "skipped" for step in result["steps"])
    watchlist = next(step for step in state["steps"] if step["name"] == "watchlist_market_data")
    assert watchlist["symbols_checked"] == 0 and watchlist["next_cursor"] == 2


def test_limit_larger_than_pool_never_duplicates_members_in_one_pass(monkeypatch):
    symbols, _, calls = warm_fixture(monkeypatch, cursor=2, limit=100)
    monkeypatch.setattr(collector_resources, "snapshot", lambda *args: {"pressure": False})
    result = asyncio.run(startup_data_check._warm_watchlist_market_data())
    assert calls == symbols[2:] + symbols[:2]
    assert result["symbols_checked"] == len(symbols)
    assert result["next_cursor"] == 2
