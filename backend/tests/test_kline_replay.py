"""Replay causality, units and endpoint behavior; providers are deterministic fakes."""
import asyncio
import copy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest

from app.deps.auth import get_current_user
from app.routers import kline_replay as router
from app.services import kline_replay as replay
from app.services.market_time import market_timestamp
from app.storage import kline_bundle_cache


def bars(start="2024-05-01", count=12):
    day = date.fromisoformat(start)
    return [{"t": market_timestamp(datetime.combine(day + timedelta(days=i), datetime.min.time())),
             "o": 10, "h": 12, "l": 9, "c": 11, "v": (i + 1) * 100, "amount": (i + 1) * 1000}
            for i in range(count)]


def bundle(rows=None):
    return {"candles": rows or bars(), "source": "test historical source", "price_basis": "raw", "volume_unit": "shares",
            "metrics": {"pe_ttm": 9999, "total_market_cap_yuan": 9e15, "shares_total": 12345678}}


def build(data=None, **kwargs):
    return replay.build_replay(data or bundle(), "600519.SS", date(2024, 5, 6), date(2024, 5, 12), **kwargs)


def test_replay_never_reuses_current_quote_and_volume_ratio_excludes_today():
    result = build()
    row = result["candles"][0]
    assert row["date"] == "2024-05-06"
    assert row["volume_ratio"] == 600 / 300
    assert row["market_cap"] is None
    assert row["profit_ttm"] is None
    assert row["pe_ttm"] is None
    assert row["turnover_rate"] is None
    assert "metrics" not in {key: val for key, val in result.items() if key != "candles"}
    assert row["metrics"]["market_cap"]["status"] == "missing"


def test_future_changes_cannot_rewrite_past_rows():
    original = bundle()
    changed = copy.deepcopy(original)
    changed["candles"][-1].update(o=1000, h=1200, l=900, c=1100, v=1e9)
    before, after = build(original), build(changed)
    assert before["candles"][:-1] == after["candles"][:-1]


def report(period, published, profit, **extra):
    return {"end_date": period, "ann_date": published, "report_type": "1", "n_income_attr_p": profit, **extra}


def test_report_only_visible_after_announcement_and_revisions_do_not_backdate():
    reports = [report("20231231", "20240430", 100),
               report("20231231", "20240430", 900, f_ann_date="20240508")]
    assert replay._report_ttm(reports, date(2024, 4, 30)) is None
    assert replay._report_ttm(reports, date(2024, 5, 1))["value"] == 100
    assert replay._report_ttm(reports, date(2024, 5, 8))["value"] == 100
    assert replay._report_ttm(reports, date(2024, 5, 9))["value"] == 900


def test_ttm_uses_current_ytd_plus_prior_annual_minus_prior_ytd():
    reports = [report("20231231", "20240401", 100), report("20230331", "20230420", 20),
               report("20240331", "20240429", 30), report("20240630", "20240830", 999)]
    value = replay._report_ttm(reports, date(2024, 5, 6))
    assert value["value"] == 110
    assert value["report_period"] == "2024-03-31"
    result = build(reports=reports)["candles"][0]
    assert result["profit_ttm"] == 110
    assert result["metrics"]["profit_ttm"]["status"] == "calculated"
    assert replay._report_ttm(reports[1:], date(2024, 5, 6)) is None


def test_unannounced_and_non_consolidated_reports_never_used():
    reports = [report("20231231", None, 100), report("20231231", "20240401", 200, report_type="4"),
               report("20231231", "20240401", 900, f_ann_date="invalid")]
    assert replay._report_ttm(reports, date(2024, 5, 6)) is None


def test_valuation_is_exact_date_only_and_estimation_is_explicit():
    result = build(daily_basic={"2024-05-06": {"market_cap": 1000, "pe_ttm": 10, "source": "dated valuation"}})
    first, second = result["candles"][:2]
    assert first["profit_ttm"] == 100
    assert first["metrics"]["profit_ttm"]["status"] == "estimated"
    assert second["market_cap"] is None
    assert second["pe_ttm"] is None


def test_zero_volume_and_missing_warmup_produce_no_infinite_ratio():
    zero = bars()
    for row in zero[:5]:
        row["v"] = 0
    assert build(bundle(zero))["candles"][0]["volume_ratio"] is None
    assert build(bundle(bars("2024-05-06", 4)))["candles"][0]["volume_ratio"] is None


@pytest.mark.parametrize("mutation", ["duplicate", "nan", "negative_volume", "invalid_ohlc", "adjusted", "unknown_units"])
def test_invalid_history_is_rejected(mutation):
    data = bundle()
    if mutation == "duplicate":
        data["candles"][1]["t"] = data["candles"][0]["t"]
    elif mutation == "nan":
        data["candles"][0]["c"] = float("nan")
    elif mutation == "negative_volume":
        data["candles"][0]["v"] = -1
    elif mutation == "invalid_ohlc":
        data["candles"][0]["h"] = 1
    elif mutation == "adjusted":
        data["price_basis"] = "qfq"
    else:
        data["volume_unit"] = "unknown"
    with pytest.raises(ValueError):
        build(data)


def test_tushare_units_and_same_symbol_filter(monkeypatch):
    class Pro:
        def daily(self, **kwargs):
            return pd.DataFrame([{"ts_code": "600519.SH", "trade_date": "20240506", "open": 10, "high": 12, "low": 9,
                                  "close": 11, "vol": 2, "amount": 3}, {"ts_code": "000001.SZ", "trade_date": "20240506"}])

        def daily_basic(self, **kwargs):
            return pd.DataFrame([{"ts_code": "600519.SH", "trade_date": "20240506", "total_mv": 5, "pe_ttm": 10}])

        def income(self, **kwargs):
            assert kwargs["report_type"] == "1"
            return pd.DataFrame([])

    monkeypatch.setattr(replay.tushare_service, "_get_pro", lambda: Pro())
    data = replay._tushare_history("600519.SS", date(2024, 5, 1), date(2024, 5, 10))
    assert len(data["candles"]) == 1
    assert data["candles"][0]["v"] == 200
    assert data["candles"][0]["amount"] == 3000
    fundamentals = replay._tushare_fundamentals("600519.SS", date(2024, 5, 1), date(2024, 5, 10))
    assert fundamentals["daily_basic"]["2024-05-06"]["market_cap"] == 50000


def test_date_bounds_and_bare_symbol_normalization():
    for code, expected in [("600519", "600519.SS"), ("000001", "000001.SZ"), ("920001", "920001.BJ"), ("600519.SH", "600519.SS")]:
        assert replay.normalize_request(code, date(2024, 1, 1), date(2024, 5, 1))[0] == expected
    with pytest.raises(ValueError):
        replay.normalize_request("invalid", None, None)
    with pytest.raises(ValueError):
        replay.normalize_request("600519", date(2000, 1, 1), date(2024, 1, 1))
    with pytest.raises(ValueError):
        replay.normalize_request("600519", date(2024, 5, 1), date(2024, 1, 1))


def test_cached_replay_recovers_when_provider_offline(monkeypatch):
    monkeypatch.setattr(replay.tushare_service, "is_configured", lambda: False)
    monkeypatch.setattr(replay.kline_baostock, "is_baostock_installed", lambda: False)

    async def history(*args):
        return bundle()

    monkeypatch.setattr(replay, "_eastmoney_history", history)
    result = asyncio.run(replay.fetch_replay("600519", date(2024, 5, 6), date(2024, 5, 12), user_id=7))
    assert result["served_from_cache"] is False

    async def offline(*args):
        raise httpx.ConnectError("private endpoint or token must not leak")

    import httpx
    monkeypatch.setattr(replay, "_eastmoney_history", offline)
    saved = asyncio.run(replay.fetch_replay("600519", date(2024, 5, 6), date(2024, 5, 12), user_id=7, refresh=True))
    assert saved["candles"] == result["candles"]
    assert saved["cache_stale"] is True
    with pytest.raises(replay.ReplayDataUnavailable, match="所选区间历史行情暂不可用"):
        asyncio.run(replay.fetch_replay("600519", date(2024, 5, 6), date(2024, 5, 12), user_id=8))


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=42)
    with TestClient(app) as client:
        yield client


def test_endpoint_validation_and_user_id(client, monkeypatch):
    calls = []

    async def fetch(symbol, start, end, **kwargs):
        calls.append((symbol, start, end, kwargs))
        return build()

    monkeypatch.setattr(replay, "fetch_replay", fetch)
    response = client.get("/api/kline-replay/600519?start_date=2024-05-06&end_date=2024-05-12")
    assert response.status_code == 200
    assert calls[0][3]["user_id"] == 42
    assert client.get("/api/kline-replay/600519?start_date=bad").status_code == 422


def test_endpoint_invalid_symbol_range_and_auth(client, monkeypatch):
    assert client.get("/api/kline-replay/invalid").status_code == 400
    assert client.get("/api/kline-replay/600519?start_date=2024-05-06&end_date=2024-01-01").status_code == 400
    from app.config import settings
    monkeypatch.setattr(settings, "auth_required", True)
    client.app.dependency_overrides.clear()
    assert client.get("/api/kline-replay/600519").status_code == 401
