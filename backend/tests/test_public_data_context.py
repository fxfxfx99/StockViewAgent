import json

from app.services import analysis_context, data_collection, public_data_context, research_generation
from app.storage import data_collection_store as store

NOW = 1792800000


def seed(value=3.5, *, period="2025", observed=NOW - 100, source="wb_inflation"):
    return store.ingest(source, "macro", [{
        "source_id": source, "source": "公开统计", "upstream_id": f"CN:{period}", "category": "macro",
        "title": "中国年度通胀", "source_url": "https://example.org/inflation", "period": period,
        "text": "年度通胀 CPI 不是月度数据", "value": value, "unit": "%", "region": "CN", "symbols": [],
    }], observed_at=observed)


def test_public_rag_has_real_local_versions_and_citations_without_future_revision():
    seed(0)
    before = public_data_context.retrieve("通胀", as_of=NOW)
    seed(9.2, observed=NOW + 100)
    after = public_data_context.retrieve("通胀", as_of=NOW)
    assert before == after
    assert before["items"][0]["value"] == 0
    assert before["items"][0]["source_id"] == "D1"
    assert before["evidence_sources"][0]["citation"].startswith("dataset:")
    assert before["items"][0]["published_at"] is None


def test_rag_switch_is_separate_from_collection_switch(monkeypatch):
    seed()
    data_collection.update_config({"enabled": False, "rag_enabled": True})
    assert public_data_context.retrieve("通胀", as_of=NOW)["items"]
    data_collection.update_config({"enabled": True, "rag_enabled": False})
    def forbidden(**kwargs):
        raise AssertionError("disabled retrieval must not query")
    monkeypatch.setattr(store, "records", forbidden)
    result = public_data_context.retrieve("通胀", as_of=NOW)
    assert result["enabled"] is False and result["items"] == []
    assert result["retrieval"]["method"] != "unavailable"


def test_invalid_cutoff_never_falls_back_to_current_data():
    seed()
    result = public_data_context.retrieve("通胀", as_of="invalid")
    assert result["items"] == [] and result["retrieval"]["method"] == "unavailable"


def test_latest_statistical_period_is_used_despite_later_old_period_revision():
    seed(2, period="2024", observed=NOW - 300)
    seed(3, period="2025", observed=NOW - 200)
    seed(2.1, period="2024", observed=NOW - 100)
    result = public_data_context.retrieve("通胀", as_of=NOW)
    assert len(result["items"]) == 1
    assert result["items"][0]["period"] == "2025"


def test_news_and_real_research_messages_contain_dated_public_facts():
    seed(3.5)
    seed(9.2, observed=NOW + 100)
    context = analysis_context.build_analysis_context({"id": "article", "title": "通胀变化", "published_ts": NOW,
        "content": "通胀变化影响经济形势", "source": "测试", "url": "https://example.org/news"}, [])
    assert context["public_data"]["items"][0]["value"] == 3.5
    assert any(row["source_id"] == "D1" for row in context["evidence_sources"])
    messages = research_generation._messages(1, {"goal": "通胀环境", "development_period": {"end": NOW}})
    facts = json.loads(messages[1]["content"])["context"]["public_data"]
    assert facts["items"][0]["value"] == 3.5
    invalid = research_generation._messages(1, {"goal": "通胀环境", "development_period": {"start": NOW}})
    assert json.loads(invalid[1]["content"])["context"]["public_data"]["items"] == []


def test_stock_picker_shares_public_evidence_across_candidates():
    from app.services.stock_picker_agent_service import _model_payload
    seed()
    context = public_data_context.retrieve("通胀", as_of=NOW)
    bundles = [{"symbol": f"{600000 + n}.SS", "signals": {"public_data": context}, "strategies": []} for n in range(30)]
    payload = _model_payload({}, bundles, [])
    assert len(payload["candidate_data"]) == 30 and len(payload["public_evidence"]) == 1
    assert all(row["public_data"]["evidence"][0]["evidence_id"] == "U1" for row in payload["candidate_data"])
    assert payload["public_evidence"][0]["value"] == 3.5
    assert len(json.dumps(payload, ensure_ascii=False)) < 50_000


def test_kline_insight_persists_exact_public_evidence_per_user():
    from app.storage import kline_insight_store
    seed()
    context = public_data_context.retrieve("通胀", as_of=NOW)
    kline_insight_store.upsert_insight(1, "600519.SS", "2026-10-25", "分析", source="manual", model="test", public_data=context)
    assert kline_insight_store.load_raw(1)["symbols"]["600519.SS"][0]["public_data"] == context
    assert kline_insight_store.load_raw(2)["symbols"] == {}


def test_real_public_retrieval_reaches_kline_model_and_saved_history(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.routers import kline_learning as router
    from app.security.user_context import current_user_id
    from app.services import kline_learning_service as kls
    from app.storage import kline_bundle_cache, kline_insight_store

    seed()
    monkeypatch.setattr(kline_bundle_cache, "load_bundle", lambda *args: {
        "payload": {"candles": [{"t": NOW, "c": 10}], "metrics": {}}})
    messages = []

    def interpret(system, user):
        messages.append(user)
        return kls.normalize_structured_analysis({}), None

    monkeypatch.setattr(kls, "llm_structured_interpret", interpret)
    token = current_user_id.set(19)
    try:
        result = asyncio.run(router.analyze(router.AnalyzeBody(symbol="600519.SS"), SimpleNamespace(id=19)))
    finally:
        current_user_id.reset(token)
    evidence = result["public_data"]
    assert evidence["items"][0]["value"] == 3.5
    assert evidence["items"][0]["citation"] in messages[0]
    assert "中国年度通胀" in messages[0]
    saved = kline_insight_store.history_for_symbol(19, "600519.SS")["entries"]
    assert next(iter(saved.values()))["public_data"] == evidence
    data_collection.update_config({"rag_enabled": False})
    result = asyncio.run(kls.analyze_a_share_symbol("600519.SS"))
    assert result["public_data"]["items"] == []
    assert "中国年度通胀" not in messages[-1]
