"""不同主机时区下，各行情源与历史节点都应使用相同的上海交易日。"""
import asyncio
import time
from datetime import date, datetime, timezone

import pytest

from app.services import (
    eastmoney_market,
    free_stockdb_http,
    kline_baostock,
    kline_learning_service,
    kline_pytdx,
    klineshare_service,
    macro_tushare_service,
    market_extra_http,
    market_fund_flow_service,
    transaction_agent_service,
    tushare_service,
)
from app.services.market_time import SHANGHAI_TZ, market_timestamp
from app.storage import kline_bundle_cache, tushare_daily_cache


@pytest.fixture(params=["UTC", "Asia/Tokyo", "America/Los_Angeles"])
def host_timezone(request, monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("当前平台不支持动态切换进程时区")
    try:
        with monkeypatch.context() as env:
            env.setenv("TZ", request.param)
            time.tzset()
            yield request.param
    finally:
        time.tzset()


def test_daily_sources_agree_on_market_day(host_timezone, monkeypatch):
    day = "2026-07-13"
    expected = int(datetime(2026, 7, 13, tzinfo=SHANGHAI_TZ).timestamp())
    common = {"open": 10, "high": 11, "low": 9, "close": 10, "volume": 100}
    timestamps = {
        "eastmoney": eastmoney_market._parse_kline_lines([f"{day},10,10,11,9,100,1000"], None)[0]["t"],
        "tencent": market_extra_http._tencent_rows_to_candles([[day, 10, 10, 11, 9, 100]], "600519.SS")[0]["t"],
        "sina": market_extra_http._sina_rows_to_candles([{**common, "day": day}])[0]["t"],
        "baidu": market_extra_http._baidu_rows_to_candles(
            ["time", "open", "close", "high", "low", "volume"], f"{day},10,10,11,9,100"
        )[0]["t"],
        "baostock": kline_baostock._rows_to_daily_candles([[day, 10, 11, 9, 10, 100]])[0]["t"],
        "pytdx": kline_pytdx._bar_to_dict({**common, "datetime": f"{day} 00:00"})["t"],
        "free_stockdb": free_stockdb_http._rows_to_candles([{**common, "date": day}])[0]["t"],
        "klineshare": klineshare_service._row_to_candle({**common, "trade_date": "20260713"})["t"],
    }
    monkeypatch.setattr(tushare_service, "fetch_daily_bars", lambda *args, **kwargs: ([
        {**common, "trade_date": "20260713"}, {**common, "trade_date": "20260710"},
    ], None))
    timestamps["tushare"] = tushare_service.fetch_kline_bundle("600519.SS")["candles"][-1]["t"]
    assert timestamps == {source: expected for source in timestamps}


def test_minute_and_macro_sources_use_market_time(host_timezone):
    expected = int(datetime(2026, 7, 13, 9, 35, tzinfo=SHANGHAI_TZ).timestamp())
    assert market_extra_http._parse_tencent_mkline_time("202607130935") == expected
    assert market_extra_http._parse_tencent_mkline_time("2026-07-13 09:35:00") == expected
    assert kline_baostock._parse_minute_datetime("2026-07-13", "20260713093500000") == expected
    assert eastmoney_market._parse_kline_lines(["2026-07-13 09:35,10,10,11,9,100,1000"], None)[0]["t"] == expected
    assert kline_pytdx._bar_to_dict({
        "year": 2026, "month": 7, "day": 13, "hour": 9, "minute": 35,
        "open": 10, "high": 11, "low": 9, "close": 10,
    })["t"] == expected
    noon = int(datetime(2026, 7, 13, 12, tzinfo=SHANGHAI_TZ).timestamp())
    assert macro_tushare_service._trade_date_to_ts("20260713") == noon
    # 已是绝对时刻的 Unix 时间戳不应再平移。
    assert klineshare_service._row_to_candle({"timestamp": expected * 1000, "close": 10})["t"] == expected
    assert market_timestamp(datetime.fromtimestamp(expected, timezone.utc)) == expected


def test_cached_and_llm_dates_keep_the_trading_day(host_timezone):
    timestamp = int(datetime(2026, 7, 13, tzinfo=SHANGHAI_TZ).timestamp())
    candles = [{"t": timestamp, "c": 10}]
    assert kline_bundle_cache.last_bar_date_str(candles) == "2026-07-13"
    assert tushare_daily_cache.last_bar_date_from_candles(candles) == date(2026, 7, 13)
    assert kline_learning_service.format_candles_summary(candles).startswith("2026-07-13 ")
    assert market_fund_flow_service._dt_from_f124(timestamp) == "2026-07-13"


def test_historical_cutoff_excludes_the_next_market_day(host_timezone, monkeypatch):
    async def fetch(*args):
        return {"candles": eastmoney_market._parse_kline_lines([
            "2026-07-10,10,10,10,10,100,1000",
            "2026-07-13,20,20,20,20,100,2000",
        ], None)}

    monkeypatch.setattr(transaction_agent_service.kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    monkeypatch.setattr(transaction_agent_service.market_history_cache, "load_series", lambda *args: {
        "items": [{"trade_date": "2026-07-10", "main_net_inflow": 1}],
    })
    historical = asyncio.run(transaction_agent_service.collect_signal_bundle("600519.SS", "2026-07-12"))
    assert historical["data_as_of"] == "2026-07-10"
    assert historical["technical"]["latest_close"] == 10
    next_day = asyncio.run(transaction_agent_service.collect_signal_bundle("600519.SS", "2026-07-13"))
    assert next_day["data_as_of"] == "2026-07-13"
    assert next_day["technical"]["latest_close"] == 20
