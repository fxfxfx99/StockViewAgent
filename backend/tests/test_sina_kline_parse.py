from app.services.market_extra_http import _sina_rows_to_candles


def test_sina_rows_to_candles_parses_sample():
    rows = [
        {
            "day": "2026-03-25",
            "open": "1410.110",
            "high": "1417.870",
            "low": "1401.010",
            "close": "1410.270",
            "volume": "2609346",
        }
    ]
    candles = _sina_rows_to_candles(rows)
    assert len(candles) == 1
    assert candles[0]["c"] == 1410.27
    assert candles[0]["v"] == 2609346
