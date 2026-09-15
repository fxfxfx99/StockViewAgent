from app.services import tushare_service


def test_tushare_bundle_normalizes_daily_rows(monkeypatch):
    monkeypatch.setattr(tushare_service, "fetch_daily_bars", lambda code, max_rows=60: ([
        {"trade_date": "20260715", "open": 10, "high": 11, "low": 9.5, "close": 10.8, "vol": 1000, "amount": 10800},
        {"trade_date": "20260714", "open": 9.8, "high": 10.2, "low": 9.6, "close": 10, "vol": 900, "amount": 9000},
    ], None))
    bundle = tushare_service.fetch_kline_bundle("000001.SZ", "1y", "1d")
    assert bundle["kline_source"] == "tushare_pro_fallback"
    assert len(bundle["candles"]) == 2
    assert bundle["candles"][0]["c"] == 10


def test_tushare_bundle_rejects_minute_interval():
    try:
        tushare_service.fetch_kline_bundle("000001.SZ", "1y", "5m")
    except ValueError as exc:
        assert "仅支持日线" in str(exc)
    else:
        raise AssertionError("expected ValueError")
