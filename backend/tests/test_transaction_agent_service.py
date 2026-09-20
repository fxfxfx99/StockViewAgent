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


def test_historical_bundle_excludes_future_snapshots_and_undated_flows(monkeypatch):
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def bar(day, close):
        return {"t": int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()), "c": close, "v": 100}

    async def kline(*args):
        return {
            "candles": [bar("2026-07-10", 10), bar("2026-07-13", 20)],
            "metrics": {"name": "测试股票", "pe_ttm": 99, "total_market_cap_yuan": 1000000, "previous_close": 18},
            "data_as_of": "2026-07-13",
        }

    def fundamentals(*args):
        raise AssertionError("历史节点不应读取缺少公告日的最新财务快照")

    def series(kind, key):
        if kind == "stock_capital_flow":
            return {"items": [
                {"date": "20260713", "main_net_inflow": 900},
                {"date": "2026-07-10", "main_net_inflow": 10},
                {"main_net_inflow": 800},
            ]}
        return {"items": [
            {"trade_date": "20260713", "net_tgt": 900, "rzrqyecz": 900},
            {"trade_date": "20260710", "net_tgt": 10, "rzrqyecz": 10},
        ]}

    monkeypatch.setattr(svc.kline_pipeline, "fetch_a_share_kline_with_fallbacks", kline)
    monkeypatch.setattr(svc.company_fundamentals_store, "latest_for_symbol", fundamentals)
    monkeypatch.setattr(svc.market_history_cache, "load_series", series)
    result = asyncio.run(svc.collect_signal_bundle("300302.SZ", as_of="2026-07-12"))
    assert result["data_as_of"] == "2026-07-10"  # 周末节点应报告实际行情日
    assert result["technical"]["latest_close"] == 10
    assert result["technical"]["previous_close"] is None
    assert result["fundamentals"]["pe_ttm"] is None
    assert result["fundamentals"]["financial"] is None
    assert result["metrics"] == {"name": "测试股票"}
    assert result["flow"]["rows"] == 1
    assert result["flow"]["main_net_inflow_5d"] == 10
    assert result["market_institution"]["north_net_tgt"] == 10
    assert result["market_institution"]["margin_balance_change"] == 10
    assert result["data_limitations"]


def test_as_of_rejects_suffixes_and_noncanonical_dates():
    import asyncio
    import pytest

    for value in ("2026-07-10junk", "2026-7-1", "2026-02-30"):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            asyncio.run(svc.collect_signal_bundle("300302.SZ", as_of=value))


def test_historical_details_explain_snapshot_limitations(monkeypatch):
    import asyncio

    async def collect(*args, **kwargs):
        return {"symbol": "300302.SZ", "stock_name": "测试", "data_limitations": ["历史快照限制"]}

    monkeypatch.setattr(svc, "collect_signal_bundle", collect)
    monkeypatch.setattr(svc, "_score_views", lambda _: [{
        "direction": "neutral", "score": 50, "detailed_analysis": {"data_limitations": []},
    }])
    monkeypatch.setattr(svc.strategy_knowledge_service, "status", lambda: {})
    result = asyncio.run(svc.generate_strategy_views("300302.SZ", as_of="2026-07-10"))
    assert result["strategies"][0]["detailed_analysis"]["data_limitations"] == ["历史快照限制"]
