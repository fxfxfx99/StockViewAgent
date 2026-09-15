from app.services import klineshare_service


def test_yahoo_symbol_to_klineshare():
    assert klineshare_service.yahoo_symbol_to_klineshare("600519.SS") == "600519.SH"
    assert klineshare_service.yahoo_symbol_to_klineshare("000001.SZ") == "000001.SZ"


def test_row_to_candle_dict():
    row = {
        "timestamp": 1700000000000,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": 10.5,
        "volume": 1000,
    }
    c = klineshare_service._row_to_candle(row)
    assert c is not None
    assert c["c"] == 10.5
    assert c["t"] == 1700000000


def test_extract_klines_nested():
    data = {"success": True, "data": {"klines": [{"timestamp": 1700000000000, "close": 1.0, "open": 1.0}]}}
    rows = klineshare_service._extract_klines(data)
    assert len(rows) == 1
