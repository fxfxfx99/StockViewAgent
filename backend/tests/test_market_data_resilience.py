from app.services import market_data_resilience


def test_enrich_metrics_with_quote_snapshot_fills_fallback_metrics():
    bundle = {
        "candles": [{"v": 1000, "turnover_rate": None}],
        "metrics": {
            "name": "",
            "pe_ttm": None,
            "total_market_cap_yuan": None,
            "fallback_note": "主行情源暂不可用，已自动切换至腾讯财经，当前行情数据可正常使用。",
        },
    }
    snapshot = {
        "name": "同有科技",
        "pe_ttm": 88.2,
        "total_market_cap_yuan": 12_000_000_000,
        "float_market_cap_yuan": 8_000_000_000,
        "shares_total": 100_000,
        "limit_up": 12.34,
        "limit_down": 10.1,
        "volume_ratio": 1.8,
        "served_from_cache": False,
        "source": "eastmoney_push2",
    }

    out = market_data_resilience.enrich_metrics_with_quote_snapshot(bundle, snapshot)

    metrics = out["metrics"]
    assert metrics["name"] == "同有科技"
    assert metrics["pe_ttm"] == 88.2
    assert metrics["total_market_cap_yuan"] == 12_000_000_000
    assert metrics["float_market_cap_yuan"] == 8_000_000_000
    assert metrics["limit_up"] == 12.34
    assert metrics["volume_ratio"] == 1.8
    assert out["candles"][0]["turnover_rate"] == 1.0
    assert "快照类指标已自动补齐" in metrics["fallback_note"]
