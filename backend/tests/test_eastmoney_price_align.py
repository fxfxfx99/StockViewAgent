"""东财快照价与 K 线末根尺度对齐（避免 f43 等误解析导致最新价 14.x 而昨收 1416）。"""

from app.services.eastmoney_market import _align_snapshot_ohlc_to_candle, _build_metrics


def test_align_when_snapshot_like_wrong_scale_vs_candle():
    q = {"latest": 14.2, "open": 14.07, "high": 14.31, "low": None}
    last = {"c": 1416.0, "o": 1410.0, "h": 1420.0, "l": 1402.52}
    prev = 1416.02
    lc, lo, hi, lw = _align_snapshot_ohlc_to_candle(q, last, prev)
    assert abs(lc - 1416.0) < 1e-6
    assert abs(lo - 1410.0) < 1e-6
    assert abs(hi - 1420.0) < 1e-6
    assert abs(lw - 1402.52) < 1e-6


def test_align_when_snapshot_matches_candle_no_change():
    q = {"latest": 1416.0, "open": 1410.0, "high": 1420.0, "low": 1402.0}
    last = {"c": 1416.0, "o": 1410.0, "h": 1420.0, "l": 1402.0}
    lc, lo, hi, lw = _align_snapshot_ohlc_to_candle(q, last, 1416.02)
    assert lc == 1416.0
    assert lo == 1410.0


def test_build_metrics_uses_aligned_close():
    candles = [
        {"t": 1, "o": 1400.0, "h": 1410.0, "l": 1390.0, "c": 1405.0, "v": 100, "amount": 1.0},
        {"t": 2, "o": 1410.0, "h": 1420.0, "l": 1402.0, "c": 1416.0, "v": 100, "amount": 1.0},
    ]
    q = {
        "name": "茅台",
        "latest": 14.16,
        "open": 14.1,
        "high": 14.2,
        "low": 14.0,
        "prev_close": 1416.02,
        "shares_total": 10_000_000_000,
    }
    m = _build_metrics("600519.SS", candles, q, "1d", extended=None)
    assert abs(float(m["latest_close"]) - 1416.0) < 0.01
    assert m["change_pct"] is not None
    assert abs(m["change_pct"]) < 1.0

