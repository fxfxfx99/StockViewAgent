from app.services import mytt_compute


def test_ma5_length_matches_candles():
    candles = [
        {"t": i, "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.0 + i * 0.01, "v": 1e6}
        for i in range(30)
    ]
    series, unknown = mytt_compute.compute_series(candles, ["ma5", "macd"])
    assert not unknown
    assert len(series["ma5"]["ma5"]) == 30
    assert len(series["macd"]["dif"]) == 30
