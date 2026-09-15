from app.services.market_extra_http import _tencent_mkline_rows_to_candles


def test_mkline_rows_to_candles():
    rows = [["2026-03-27 15:00:00", 1400.0, 1416.02, 1426.0, 1396.66, 30087.12]]
    candles = _tencent_mkline_rows_to_candles(rows, qt_close=None)
    assert len(candles) == 1
    assert candles[0]["c"] == 1416.02
    assert candles[0]["v"] == int(round(30087.12 * 100))
