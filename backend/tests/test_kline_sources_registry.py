from app.services import kline_sources_registry as reg


def test_registry_chains_order():
    assert "eastmoney" == reg.DAILY_FALLBACK_CHAIN[0]
    assert "baostock_fallback" in reg.DAILY_FALLBACK_CHAIN
    assert "pytdx_fallback" in reg.DAILY_FALLBACK_CHAIN
    assert reg.DAILY_FALLBACK_CHAIN[-1] == "pytdx_fallback"
    assert "baostock_minute_fallback" in reg.MINUTE_FALLBACK_CHAIN


def test_symbol_map_pytdx_bj_raises():
    from app.services.kline_pytdx import _symbol_to_market_code

    try:
        _symbol_to_market_code("920000.BJ")
    except ValueError as e:
        assert "北交所" in str(e) or "pytdx" in str(e).lower()
    else:
        raise AssertionError("expected ValueError for BJ")
