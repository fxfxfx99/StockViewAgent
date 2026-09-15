from app.services import transaction_agent_service as svc


def test_score_views_returns_transaction_agent_schema():
    bundle = {
        "symbol": "300302.SZ",
        "stock_name": "同有科技",
        "kline_source": "test",
        "data_as_of": "2026-07-14",
        "technical": {
            "latest_close": 20,
            "previous_close": 19,
            "change_5d_pct": 6,
            "change_20d_pct": 12,
            "change_60d_pct": 18,
            "price_above_ma20": True,
            "price_above_ma60": True,
            "ma20_above_ma60": True,
            "volume_ratio_5d_20d": 1.4,
            "position_60d_pct": 65,
            "high60": 22,
            "low60": 12,
        },
        "flow": {
            "rows": 5,
            "latest_date": "2026-07-14",
            "main_net_inflow_5d": 12000000,
        },
        "fundamentals": {
            "pe_ttm": 32,
            "total_market_cap_yuan": 10000000000,
            "industry": "计算机、通信和其他电子设备制造业",
            "basic_info": {"short_name": "同有科技"},
            "mda_preview": "公司主要从事企业级存储系统研发。",
        },
    }

    views = svc._score_views(bundle)

    assert len(views) == len(svc.STRATEGIES)
    assert {v["strategy"] for v in views} == {s["name"] for s in svc.STRATEGIES}
    for view in views:
        assert view["direction"] in {"bullish", "neutral", "bearish"}
        assert 0 <= view["score"] <= 100
        assert 0 < view["confidence"] <= 1
        assert view["viewpoint"]
        assert len(view["reasons"]) >= 2
        assert len(view["do_not_do"]) >= 2
        assert view["metadata"]["model_version"] == "rule-based.transaction-agent-adapter.v1"
