import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import global_market, market_catalog


def instrument(key="US_SP500"):
    return market_catalog.get_market_instrument(key)


def bar(day=None, **updates):
    day = day or (datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)).strftime("%Y-%m-%d")
    stamp = int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("America/New_York")).timestamp())
    return {"t": stamp, "o": 10, "h": 12, "l": 8, "c": 11, "v": 100, **updates}


def payload(rows, sym="^GSPC"):
    return {"chart": {"result": [{"meta": {"symbol": sym, "currency": "USD"},
        "timestamp": [r["t"] for r in rows], "indicators": {"quote": [{
            long: [r.get(short) for r in rows]
            for short, long in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}]}}]}}


def test_catalog_covers_markets_and_futures_without_spot_aliases():
    catalog = market_catalog.list_market_catalog()
    assert len(catalog) == 19
    assert {r["key"] for r in catalog if r["region"] == "CN"} == {
        "SH_COMP", "SZ_COMP", "HS300", "CYB", "SSE50", "STAR50"}
    assert {r["symbol"] for r in catalog if r["asset_class"] == "futures"} == {
        "GC=F", "SI=F", "CL=F", "BZ=F", "NG=F", "HG=F"}
    assert instrument("FUT_BRENT")["exchange"] == "NYMEX"
    assert {r["exchange"] for r in catalog if r["key"] in {"US_NYSE", "US_NASDAQ", "US_AMEX"}} == {
        "NYSE", "NASDAQ", "NYSE American"}
    assert all(not r["requires_token"] for r in catalog)
    catalog[0]["providers"][0]["symbol"] = "corrupt"
    assert market_catalog.list_market_catalog()[0]["providers"][0]["symbol"] != "corrupt"


@pytest.mark.parametrize("updates", [
    {"c": float("nan")}, {"h": float("inf")}, {"o": True}, {"v": -1},
    {"o": 13}, {"l": 11}, {"c": None}, {"t": True}, {"o": -1},
])
def test_invalid_bars_rejected(updates):
    with pytest.raises(ValueError):
        global_market.validate_daily_candles([bar(**updates)], instrument())


def test_negative_futures_and_missing_volume_preserved():
    row = bar("2020-04-20", o=17, h=18, l=-40, c=-37.63, v=None)
    got = global_market.parse_yahoo_daily(payload([row], "CL=F"), instrument("FUT_WTI"))
    assert got[0]["c"] == -37.63
    assert got[0]["v"] is None and got[0]["amount"] is None
    assert got[0]["date"] == "2020-04-20"


def test_duplicate_out_of_order_and_future_dates_rejected():
    for rows in ([bar("2020-01-02"), bar("2020-01-02")],
                 [bar("2020-01-03"), bar("2020-01-02")], [bar("2099-01-01")]):
        with pytest.raises(ValueError):
            global_market.validate_daily_candles(rows, instrument())


def test_yahoo_incomplete_bar_is_not_filled_and_wrong_symbol_is_rejected():
    with pytest.raises(ValueError):
        global_market.parse_yahoo_daily(payload([bar(o=None)]), instrument())
    with pytest.raises(ValueError):
        global_market.parse_yahoo_daily(payload([bar()], "SPY"), instrument())
    missing = bar(o=None, h=None, l=None, c=None)
    rows = global_market.parse_yahoo_daily(payload([bar("2020-01-02"), missing]), instrument())
    assert len(rows) == 1


def test_stooq_allows_no_volume_but_rejects_html_partial_and_inverted_ohlc():
    rows = global_market.parse_stooq_daily("Date,Open,High,Low,Close\n2020-01-02,10,12,8,11\n", instrument())
    assert rows[0]["v"] is None and rows[0]["date"] == "2020-01-02"
    for data in ("<!DOCTYPE html><html>challenge</html>", "Date,Open,High,Low,Close\n2020-01-02,,12,8,11\n",
                 "Date,Open,High,Low,Close\n2020-01-02,10,9,8,11\n"):
        with pytest.raises(ValueError):
            global_market.parse_stooq_daily(data, instrument())


def test_aggregation_uses_exchange_calendar_and_real_ohlcv():
    rows = global_market.validate_daily_candles([
        bar("2020-12-31", o=10, h=12, l=8, c=11, v=100),
        bar("2021-01-01", o=11, h=15, l=10, c=14, v=200),
        bar("2021-01-04", o=14, h=16, l=12, c=15, v=300),
    ], instrument())
    weekly = global_market.aggregate_daily(rows, "1wk", instrument())
    assert len(weekly) == 2
    assert (weekly[0]["o"], weekly[0]["h"], weekly[0]["l"], weekly[0]["c"], weekly[0]["v"]) == (10, 15, 8, 14, 300)
    assert weekly[0]["period_end"] == "2021-01-01"
    monthly = global_market.aggregate_daily(rows, "1mo", instrument())
    assert len(monthly) == 2 and monthly[1]["v"] == 500
    assert monthly[1]["date"] == "2021-01-01"


def test_yahoo_ny_session_date_does_not_shift_to_shanghai_next_day():
    # 16:00 NY = the next calendar day in Shanghai during winter.
    row = bar("2020-01-02")
    row["t"] += 16 * 3600
    got = global_market.parse_yahoo_daily(payload([row]), instrument())
    assert got[0]["date"] == "2020-01-02"


def test_failed_primary_falls_back_exact_stooq_symbol(monkeypatch):
    calls = []

    async def fetch(meta, provider, range_param):
        calls.append((provider["id"], provider["symbol"]))
        if provider["id"].startswith("yahoo"):
            raise RuntimeError("private diagnostic")
        return [bar()]

    monkeypatch.setattr(global_market, "_fetch_provider_daily", fetch)
    result = asyncio.run(global_market.fetch_market_kline("US_SP500"))
    assert calls == [("yahoo", "^GSPC"), ("yahoo_query2", "^GSPC"), ("stooq", "^spx")]
    assert result["kline_source"] == "stooq"
    assert result["provider_symbol"] == "^spx"
    assert result["source_url"].startswith("https://stooq.com/")
    assert result["instrument"]["key"] == "US_SP500"
    assert result["timezone"] == "America/New_York" and not result["cache_stale"]


def test_daily_cache_shared_by_week_month_force_refresh_and_stale(monkeypatch):
    calls = []
    fail = False

    async def fetch(*args):
        calls.append(args)
        if fail:
            raise RuntimeError("private provider diagnostic")
        return [bar()]

    monkeypatch.setattr(global_market, "_fetch_provider_daily", fetch)

    async def scenario():
        nonlocal fail
        first = await global_market.fetch_market_kline("FUT_GOLD")
        second = await global_market.fetch_market_kline("FUT_GOLD", interval="1wk")
        assert len(calls) == 1 and second["served_from_cache"]
        assert second["cache_saved_at"] == first["cache_saved_at"]
        fail = True
        third = await global_market.fetch_market_kline("FUT_GOLD", force_refresh=True)
        assert len(calls) == 3
        assert third["cache_stale"] and third["served_from_cache"]
        assert third["cache_saved_at"] == first["cache_saved_at"]
        assert third["data_as_of"] == first["data_as_of"]
        assert third["kline_source"] == first["kline_source"]
        assert "private provider diagnostic" not in str(third)

    asyncio.run(scenario())


def test_singleflight_shared_across_intervals_and_cancellation(monkeypatch):
    calls = []

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def fetch(*args):
            calls.append(args)
            started.set()
            await release.wait()
            return [bar()]

        monkeypatch.setattr(global_market, "_fetch_provider_daily", fetch)
        first = asyncio.create_task(global_market.fetch_market_kline("FUT_GOLD", interval="1d"))
        await started.wait()
        second = asyncio.create_task(global_market.fetch_market_kline("FUT_GOLD", interval="1wk"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await second
        assert len(calls) == 1 and result["interval"] == "1wk"

    asyncio.run(scenario())


def test_invalid_primary_cannot_poison_cache_and_old_source_does_not_overwrite(monkeypatch):
    async def fetch(meta, provider, range_param):
        return [bar(c=100)] if provider["id"] == "yahoo" else [bar()]

    monkeypatch.setattr(global_market, "_fetch_provider_daily", fetch)
    first = asyncio.run(global_market.fetch_market_kline("FUT_WTI"))
    assert first["kline_source"] == "yahoo_query2"

    async def old(*args):
        return [bar("2020-04-20", o=17, h=18, l=-40, c=-37.63)]

    monkeypatch.setattr(global_market, "_fetch_provider_daily", old)
    second = asyncio.run(global_market.fetch_market_kline("FUT_WTI", force_refresh=True))
    assert second["cache_stale"] and second["data_as_of"] == first["data_as_of"]
    assert second["cache_saved_at"] == first["cache_saved_at"]


def test_cn_delegates_existing_pipeline_and_attaches_real_source(monkeypatch):
    calls = []

    async def fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return {"candles": [bar()], "kline_source": "tencent_index_fallback"}

    monkeypatch.setattr(global_market.kline_pipeline, "fetch_major_index_kline_with_fallbacks", fetch)
    result = asyncio.run(global_market.fetch_market_kline("STAR50", force_refresh=True, public_only=True))
    assert calls[0] == (("STAR50", "1y", "1d"), {"force_refresh": True, "public_only": True})
    assert result["instrument"]["symbol"] == "000688.SS"
    assert result["source_url"] == "https://gu.qq.com/"
    assert result["currency"] == "CNY"


def test_unknown_symbol_or_interval_rejected_without_request():
    for key, period in (("SPY", "1d"), ("FUT_GOLD", "1h")):
        with pytest.raises(ValueError):
            asyncio.run(global_market.fetch_market_kline(key, interval=period))


def test_rate_limit_persists_across_symbols_and_honors_retry_after(monkeypatch):
    import httpx

    calls = []

    async def limited(meta, provider, range_param):
        calls.append((meta["key"], provider["id"]))
        request = httpx.Request("GET", "https://example.invalid/private")
        response = httpx.Response(429, headers={"Retry-After": "900"}, request=request)
        raise httpx.HTTPStatusError("private diagnostic", request=request, response=response)

    monkeypatch.setattr(global_market, "_fetch_provider_daily", limited)
    with pytest.raises(global_market.GlobalMarketUnavailable) as first:
        asyncio.run(global_market.fetch_market_kline("FUT_GOLD"))
    assert first.value.error_code == "source_rate_limited"
    retry = datetime.fromisoformat(first.value.retry_at).timestamp()
    assert 890 < retry - global_market.time.time() <= 900
    with pytest.raises(global_market.GlobalMarketUnavailable) as second:
        asyncio.run(global_market.fetch_market_kline("FUT_SILVER"))
    assert second.value.error_code == "provider_cooldown"
    assert second.value.retry_at == first.value.retry_at
    assert calls == [("FUT_GOLD", "yahoo"), ("FUT_GOLD", "yahoo_query2")]
    assert "private" not in str(first.value)
    assert "private" not in str(second.value)


@pytest.mark.parametrize("retry_after,expected", [("1", 300), ("100000", 3600), ("NaN", 300)])
def test_rate_limit_cooldown_is_bounded(monkeypatch, retry_after, expected):
    import httpx

    monkeypatch.setattr(global_market.time, "time", lambda: 1_800_000_000)
    response = httpx.Response(429, headers={"Retry-After": retry_after})
    until = global_market._save_provider_cooldown(instrument()["providers"][0], response)
    assert until == 1_800_000_000 + expected


def test_rate_limit_http_date_and_expiry(monkeypatch):
    from email.utils import format_datetime
    import httpx

    now = 1_800_000_000
    monkeypatch.setattr(global_market.time, "time", lambda: now)
    response = httpx.Response(429, headers={"Retry-After": format_datetime(datetime.fromtimestamp(now + 600, timezone.utc))})
    provider = instrument()["providers"][0]
    assert global_market._save_provider_cooldown(provider, response) == now + 600
    assert global_market._cooldown_until(provider) == now + 600
    now += 601
    assert global_market._cooldown_until(provider) is None


def test_concurrent_symbols_stop_calling_rate_limited_host(monkeypatch):
    import httpx
    calls = []

    async def fetch(meta, provider, range_param):
        calls.append(provider["id"])
        await asyncio.sleep(0.01)
        request = httpx.Request("GET", "https://example.invalid/")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("limited", request=request, response=response)

    monkeypatch.setattr(global_market, "_fetch_provider_daily", fetch)

    async def scenario():
        results = await asyncio.gather(global_market.fetch_market_kline("FUT_GOLD"),
            global_market.fetch_market_kline("FUT_SILVER"), return_exceptions=True)
        assert all(isinstance(row, global_market.GlobalMarketUnavailable) for row in results)

    asyncio.run(scenario())
    assert calls == ["yahoo", "yahoo_query2"]


def test_cached_read_does_not_advance_derived_cache_saved_at(monkeypatch):
    writes = []
    save = global_market.kline_bundle_cache.save_bundle

    def record(*args, **kwargs):
        writes.append(args[0])
        return save(*args, **kwargs)

    async def fetch(*args):
        return [bar()]

    monkeypatch.setattr(global_market.kline_bundle_cache, "save_bundle", record)
    monkeypatch.setattr(global_market, "_fetch_provider_daily", fetch)
    first = asyncio.run(global_market.fetch_market_kline("FUT_GOLD"))
    second = asyncio.run(global_market.fetch_market_kline("FUT_GOLD", interval="1wk"))
    assert writes == ["GLOBAL_DAILY_FUT_GOLD", "INDEX_FUT_GOLD"]
    assert second["served_from_cache"] and second["cache_saved_at"] == first["cache_saved_at"]


RANGE_NOW = datetime(2026, 10, 30, 10, tzinfo=ZoneInfo("America/New_York"))


def _freeze_range_clock(monkeypatch):
    monkeypatch.setattr(global_market.time, "time", lambda: RANGE_NOW.timestamp())


def _range_cache(monkeypatch, *, first="2025-10-30", saved_at=None, source_range="1y", last="2026-10-30"):
    start, end = datetime.fromisoformat(first), datetime.fromisoformat(last)
    rows = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            rows.append(bar(day.date().isoformat()))
        day += timedelta(days=1)
    entry = {"saved_at": saved_at or RANGE_NOW.isoformat(), "payload": {
        "index_key": "FUT_GOLD", "symbol": "GC=F", "range": source_range,
        "candles": rows, "kline_source": "yahoo_query2", "source_url": "https://query2.finance.yahoo.com/",
        "fetched_at": "2026-10-30T21:59:00+00:00", "provider_symbol": "GC=F"}}
    writes, requests = [], []
    monkeypatch.setattr(global_market.kline_bundle_cache, "load_bundle",
                        lambda key, selected_range, interval: entry if selected_range == source_range else None)
    monkeypatch.setattr(global_market.kline_bundle_cache, "save_bundle", lambda *args: writes.append(args))
    monkeypatch.setattr(global_market, "_record_health", lambda *args: None)

    async def unavailable(meta, provider, selected_range):
        requests.append((provider["id"], selected_range))
        raise RuntimeError("offline")

    monkeypatch.setattr(global_market, "_fetch_provider_daily", unavailable)
    return entry, writes, requests


@pytest.mark.parametrize("interval,dates,expected_first", [
    ("1d", ["2024-10-30", "2025-10-29", "2025-10-30", "2026-10-29", "2026-10-30"], "2025-10-30"),
    ("1wk", ["2024-10-28", "2025-10-20", "2025-10-27", "2026-10-19", "2026-10-26"], "2025-10-27"),
    ("1mo", ["2024-10-01", "2025-09-01", "2025-10-01", "2026-09-01", "2026-10-01"], "2025-10-01"),
])
def test_cn_calendar_range_trims_excess_history_and_rebuilds_period_metrics(monkeypatch, interval, dates, expected_first):
    _freeze_range_clock(monkeypatch)
    rows = [bar(day, amount=1000, turnover_rate=0.5) for day in dates]
    rows[-2].update(c=10)
    rows[-1].update(o=11, h=14, l=10, c=12, v=200, amount=2000)
    original = {"candles": rows, "range": "1y", "kline_source": "eastmoney", "data_as_of": "2026-10-30",
                "cache_saved_at": "2026-10-30T21:00:00Z", "served_from_cache": True,
                "metrics": {"latest_close": 999, "previous_close": 999, "latest_high": 999, "change_pct": 999,
                            "data_source": "东方财富"}}

    async def native(*args, **kwargs):
        return original

    monkeypatch.setattr(global_market.kline_pipeline, "fetch_major_index_kline_with_fallbacks", native)
    result = asyncio.run(global_market.fetch_market_kline("SH_COMP", "1y", interval))
    assert global_market._local_date(result["candles"][0]["t"], instrument("SH_COMP")).isoformat() == expected_first
    assert len(result["candles"]) == 3
    assert result["metrics"]["latest_close"] == 12 and result["metrics"]["previous_close"] == 10
    assert result["metrics"]["latest_high"] == 14 and result["metrics"]["latest_amount"] == 2000
    assert result["metrics"]["change_pct"] == 20
    assert result["metrics"]["interval_effective"] == interval
    assert result["data_as_of"] == "2026-10-30" and result["cache_saved_at"] == original["cache_saved_at"]
    assert result["range_boundary_semantics"] == "include_overlapping_period"
    assert len(original["candles"]) == 5 and original["metrics"]["latest_close"] == 999


def test_cn_explicit_period_end_keeps_only_actual_overlapping_candle(monkeypatch):
    _freeze_range_clock(monkeypatch)
    rows = [bar("2025-10-27", period_end="2025-10-29"),
            bar("2025-11-03", period_end="2025-11-07"), bar("2026-10-26", period_end="2026-10-30")]

    async def native(*args, **kwargs):
        return {"candles": rows, "data_as_of": "2026-10-30"}

    monkeypatch.setattr(global_market.kline_pipeline, "fetch_major_index_kline_with_fallbacks", native)
    first = asyncio.run(global_market.fetch_market_kline("SH_COMP", "1y", "1wk"))
    assert len(first["candles"]) == 2  # Oct 27's last actual observation predates Oct 30's cutoff.
    rows[0]["period_end"] = "2025-10-31"
    second = asyncio.run(global_market.fetch_market_kline("SH_COMP", "1y", "1wk"))
    assert len(second["candles"]) == 3
    assert second["candles"][0]["period_end"] == "2025-10-31"
    assert second["data_as_of"] == "2026-10-30"


@pytest.mark.parametrize("target,first_day", [("3mo", "2026-07-30"), ("6mo", "2026-04-30")])
def test_fresh_longer_cache_serves_shorter_range_with_original_provenance(monkeypatch, target, first_day):
    _freeze_range_clock(monkeypatch)
    entry, writes, requests = _range_cache(monkeypatch)
    out = asyncio.run(global_market.fetch_market_kline("FUT_GOLD", target))
    assert out["range"] == target and out["cache_source_range"] == "1y"
    assert out["candles"][0]["date"] == first_day and out["data_as_of"] == "2026-10-30"
    assert out["served_from_cache"] and not out["cache_stale"]
    assert out["cache_saved_at"] == entry["saved_at"] and out["fetched_at"] == entry["payload"]["fetched_at"]
    assert out["kline_source"] == "yahoo_query2" and out["source_url"] == entry["payload"]["source_url"]
    assert not writes and not requests
    assert entry["payload"]["range"] == "1y"


def test_cross_range_monthly_slice_preserves_boundary_period_and_last_daily_asof(monkeypatch):
    _freeze_range_clock(monkeypatch)
    _, writes, requests = _range_cache(monkeypatch)
    out = asyncio.run(global_market.fetch_market_kline("FUT_GOLD", "3mo", "1mo"))
    assert out["candles"][0]["date"] == "2026-07-01"  # Retain the July period overlapping July 30.
    assert out["candles"][0]["period_end"] == "2026-07-31"
    assert out["candles"][0]["v"] == 2300  # Full source month, not only July 30 and 31.
    assert out["candles"][-1]["date"] == "2026-10-01"
    assert out["data_as_of"] == "2026-10-30" and out["range"] == "3mo"
    assert not writes and not requests


@pytest.mark.parametrize("force,expired", [(True, False), (False, True)])
def test_cross_range_failed_refresh_is_stale_and_does_not_rewrite_success(monkeypatch, force, expired):
    _freeze_range_clock(monkeypatch)
    saved = (RANGE_NOW - timedelta(hours=1)).isoformat() if expired else RANGE_NOW.isoformat()
    entry, writes, requests = _range_cache(monkeypatch, saved_at=saved)
    out = asyncio.run(global_market.fetch_market_kline("FUT_GOLD", "3mo", force_refresh=force))
    assert len(requests) == 2 and all(request[1] == "3mo" for request in requests)
    assert out["served_from_cache"] and out["cache_stale"] and out["error_code"] == "source_unavailable"
    assert out["cache_saved_at"] == entry["saved_at"] and out["data_as_of"] == "2026-10-30"
    assert out["cache_source_range"] == "1y" and out["range"] == "3mo"
    assert not writes


@pytest.mark.parametrize("target,first,last", [
    ("5y", "2025-10-30", "2026-10-30"),  # One year cannot establish five years.
    ("6mo", "2026-08-01", "2026-10-30"),  # A file labelled 1y may contain only a short tail.
    ("3mo", "2025-10-30", "2026-06-30"),  # Old history has no observations in the requested window.
    ("max", "2025-10-30", "2026-10-30"),
])
def test_cross_range_cache_requires_actual_coverage(monkeypatch, target, first, last):
    _freeze_range_clock(monkeypatch)
    _, writes, requests = _range_cache(monkeypatch, first=first, last=last)
    with pytest.raises(global_market.GlobalMarketUnavailable):
        asyncio.run(global_market.fetch_market_kline("FUT_GOLD", target))
    assert len(requests) == 2 and not writes
