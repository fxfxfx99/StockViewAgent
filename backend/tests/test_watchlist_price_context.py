import asyncio

from app.services import watchlist_price_context


def test_price_context_uses_unified_fallback_pipeline(monkeypatch):
    async def fake_fetch(symbol: str, range_param: str, interval: str):
        assert (range_param, interval) == ("3mo", "1d")
        return {
            "kline_source": "tencent_fallback",
            "candles": [
                {"t": 1, "o": 10, "h": 11, "l": 9, "c": 10, "v": 100},
                {"t": 2, "o": 10, "h": 12, "l": 10, "c": 11, "v": 200},
            ],
            "metrics": {
                "latest_close": 11,
                "change_pct": 10,
                "data_source": "腾讯财经",
            },
        }

    monkeypatch.setattr(
        watchlist_price_context.kline_pipeline,
        "fetch_a_share_kline_with_fallbacks",
        fake_fetch,
    )

    result = asyncio.run(watchlist_price_context.price_context_for_symbols(["000001.SZ"]))
    row = result["000001.SZ"]
    assert row["latest_price"] == 11
    assert row["return_5d_pct"] == 10
    assert row["kline_source"] == "tencent_fallback"
    assert row["refreshed_at"]
    assert "error" not in row
