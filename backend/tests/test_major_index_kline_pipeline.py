import asyncio

from app.services import kline_pipeline


def test_major_index_uses_cached_bundle(monkeypatch):
    cached = {
        "saved_at": "2026-07-03T00:00:00+00:00",
        "payload": {"candles": [{"t": 1, "c": 3000}], "metrics": {}, "index_key": "SH_COMP"},
    }
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: cached)

    async def fail_live(*args, **kwargs):
        raise AssertionError("cache should avoid live fetch")

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", fail_live)
    out = asyncio.run(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d"))
    assert out["kline_source"] == "local_cache"
    assert out["cache_saved_at"] == cached["saved_at"]


def test_major_index_falls_back_to_tencent_with_index_symbol(monkeypatch):
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "load_bundle", lambda *args: None)
    monkeypatch.setattr(kline_pipeline.kline_bundle_cache, "save_bundle", lambda *args: None)
    monkeypatch.setattr(kline_pipeline, "is_baostock_installed", lambda: False)

    async def fail_primary(*args, **kwargs):
        raise ValueError("primary down")

    def tencent_ok(symbol, range_param, interval, *, provider_symbol=None):
        assert provider_symbol == "sh000001"
        return {"candles": [{"t": 1, "c": 3000}], "metrics": {}, "symbol": symbol}

    monkeypatch.setattr(kline_pipeline.eastmoney_market, "fetch_major_index_kline_bundle", fail_primary)
    monkeypatch.setattr(kline_pipeline.market_extra_http, "fetch_kline_bundle_secondary_sync", tencent_ok)
    out = asyncio.run(kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", "1y", "1d"))
    assert out["index_key"] == "SH_COMP"
    assert out["kline_source"] == "tencent_index_fallback"
