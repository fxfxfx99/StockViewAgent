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
    result = asyncio.run(svc.generate_strategy_views("300302.SZ", as_of="2026-07-10"))
    assert result["strategies"][0]["detailed_analysis"]["data_limitations"] == ["历史快照限制"]


def test_strategy_analysis_uses_account_scoped_rag_and_keeps_market_score(monkeypatch):
    from app.security.user_context import current_user_id

    calls = []

    def retrieve(user_id, strategy_id, **kwargs):
        calls.append((user_id, strategy_id, kwargs))
        return {
            "documents": [{"id": "note-1", "title": "估值假设修订", "content": "现金流变化后重新核验安全边际。",
                           "origin": "personal", "version": 2, "updated_at": "2026-11-08T01:00:00Z",
                           "source_url": "https://example.org/report"}],
            "config": {"enabled": True, "version": 3},
            "metadata": {"knowledge_version": "revision-2", "as_of": "2026-11-08"},
        }

    monkeypatch.setattr(svc.strategy_workspace_service, "retrieve_strategy_knowledge", retrieve)
    original = svc._base_view(svc._STRATEGY_BY_ID["value_investing"], 54, .44, "等待核验", [], [], "观察", [], {})
    token = current_user_id.set(17)
    try:
        result = svc._apply_knowledge_context(original, query="600519.SS 价值投资", as_of="2026-11-08")
    finally:
        current_user_id.reset(token)
    assert calls == [(17, "value_investing", {"query": "600519.SS 价值投资", "as_of": "2026-11-08"})]
    assert (result["score"], result["confidence"], result["direction"]) == (54, .44, "neutral")
    assert result["detailed_analysis"]["knowledge_points"] == ["现金流变化后重新核验安全边际。"]
    assert result["metadata"]["knowledge_version"] == "revision-2"
    assert result["metadata"]["rag_config_version"] == 3
    assert result["detailed_analysis"]["sources"][0]["version"] == 2
    assert result["detailed_analysis"]["sources"][0]["url"] == "https://example.org/report"
    assert original["metadata"]["rag_context"] == []


def test_rag_disabled_still_produces_complete_rule_analysis(monkeypatch):
    monkeypatch.setattr(svc.strategy_workspace_service, "retrieve_strategy_knowledge", lambda *a, **k: {
        "documents": [], "config": {"enabled": False, "version": 4},
        "metadata": {"knowledge_version": "disabled-v4", "limitations": ["当前策略已关闭知识检索。"]},
    })
    original = svc._base_view(svc.STRATEGIES[0], 65, .5, "趋势观察", ["价格信号"], [], "观察", [], {})
    result = svc._apply_knowledge_context(original)
    assert result["score"] == 65
    assert result["metadata"]["rag_enabled"] is False
    assert result["knowledge_context"] == []
    assert result["detailed_analysis"]["signal_checks"][0] == "价格信号"
    assert "当前策略已关闭知识检索。" in result["detailed_analysis"]["data_limitations"]


def test_unknown_strategy_rejected_before_collecting_market_data(monkeypatch):
    import asyncio
    import pytest

    async def collect(*a, **k):
        raise AssertionError("未知策略无需拉取行情")

    monkeypatch.setattr(svc, "collect_signal_bundle", collect)
    with pytest.raises(ValueError, match="未知策略"):
        asyncio.run(svc.generate_strategy_views("600519.SS", strategy="missing"))


def test_saved_knowledge_revision_changes_next_analysis_without_cross_strategy_leakage():
    from app.security.user_context import current_user_id

    workspace = svc.strategy_workspace_service
    bundle = {"symbol": "600519.SS", "stock_name": "测试", "technical": {}, "fundamentals": {}, "flow": {}}
    saved = workspace.save_document(23, "value_investing", {"title": "现金流复核", "content": "现金流与利润质量需要复核。"})
    doc_id = saved["documents"][0]["id"]
    workspace.save_config(23, "value_investing", {"include_builtin": False})
    token = current_user_id.set(23)
    try:
        first = svc._score_views(bundle)
        value = next(view for view in first if view["strategy_id"] == "value_investing")
        assert value["knowledge_context"][0]["id"] == doc_id
        assert value["detailed_analysis"]["knowledge_points"] == ["现金流与利润质量需要复核。"]
        assert all(doc_id not in [item["id"] for item in view["knowledge_context"]]
                   for view in first if view["strategy_id"] != "value_investing")
        workspace.save_document(23, "value_investing", {"title": "现金流复核", "content": "现金流与利润质量已有反向证据。"}, document_id=doc_id)
        second = next(view for view in svc._score_views(bundle) if view["strategy_id"] == "value_investing")
        assert second["detailed_analysis"]["knowledge_points"] == ["现金流与利润质量已有反向证据。"]
        assert second["metadata"]["knowledge_version"] != value["metadata"]["knowledge_version"]
        assert second["knowledge_context"][0]["version"] == 2
        assert (second["score"], second["confidence"]) == (value["score"], value["confidence"])
        workspace.save_config(23, "value_investing", {"enabled": False})
        disabled = next(view for view in svc._score_views(bundle) if view["strategy_id"] == "value_investing")
        assert disabled["knowledge_context"] == []
    finally:
        current_user_id.reset(token)
