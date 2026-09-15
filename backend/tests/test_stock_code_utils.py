from app.services.stock_code_utils import to_yahoo_from_mixed


def test_to_yahoo_ss_sz():
    assert to_yahoo_from_mixed("600519.SS") == "600519.SS"
    assert to_yahoo_from_mixed("SH600519") == "600519.SS"
    assert to_yahoo_from_mixed("000001.SZ") == "000001.SZ"
    assert to_yahoo_from_mixed("600519") == "600519.SS"
