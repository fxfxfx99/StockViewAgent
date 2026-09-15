import asyncio

from app.routers import market


def test_stock_capital_flow_returns_empty_when_source_fails_without_cache(monkeypatch):
    async def fail_fetch(*args, **kwargs):
        raise RuntimeError("dns unavailable")

    monkeypatch.setattr(market.market_extra_http, "fetch_stock_capital_flow", fail_fetch)
    monkeypatch.setattr(market.market_history_cache, "load_series", lambda *args: None)

    out = asyncio.run(market.stock_adata_capital_flow("300302.SZ"))

    assert out["symbol"] == "300302.SZ"
    assert out["items"] == []
    assert out["served_from_cache"] is False
    assert "资金流向接口异常" in out["source_warning"]
