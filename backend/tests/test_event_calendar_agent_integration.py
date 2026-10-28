"""Actual agent payloads must contain retrievable, dated calendar evidence."""
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app.services
from app.security.user_context import current_user_id
from app.services import analysis_context, event_calendar_context, kline_learning_service as kls
from app.services import research_generation, transaction_agent_service as transaction


NOW = 1_788_926_400
SYMBOL = "600519.SS"


def evidence():
    source = {
        "source_id": "E1", "kind": "economic_event", "event_id": "fed-1", "revision_id": "revision-before",
        "title": "央行政策会议", "text": "已公布会议日程，结果待公布。", "source": "官方日程",
        "published_at": "2026-09-01T00:00:00Z", "observed_at": "2026-09-01T01:00:00Z",
        "url": "https://example.org/calendar/fed",
    }
    return {"enabled": True, "items": [{"id": "fed-1", "source_id": "E1", "snippet": source["text"]}],
            "evidence_sources": [source], "retrieval": {"method": "fts", "as_of": NOW}, "limitations": []}


@pytest.fixture
def calendar(monkeypatch):
    calls = []

    def build(agent, query="", **kwargs):
        calls.append({"agent": agent, "query": query, "current_user_id": current_user_id.get(), **kwargs})
        return deepcopy(evidence())

    monkeypatch.setattr(app.services, "event_calendar_service", SimpleNamespace(build_agent_context=build), raising=False)
    return calls


def test_news_calendar_is_an_actual_citable_source_at_news_publication(calendar, monkeypatch):
    monkeypatch.setattr(analysis_context, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(analysis_context.news_store, "analysis_background", lambda *args, **kwargs: [])
    article = {"id": "news-1", "title": "公司经营情况", "summary": "公司披露经营资料。", "published_ts": NOW - 3600}
    stock = {"stock_code": SYMBOL, "stock_name": "测试", "industry": "食品", "main_business": "食品制造"}
    result = analysis_context.build_analysis_context(article, [stock])
    assert calendar[0]["as_of"] == NOW - 3600
    assert calendar[0]["symbols"] == [SYMBOL]
    assert calendar[0]["agent"] == "news"
    assert result["economic_calendar"]["items"]
    assert result["evidence_sources"][-1] == evidence()["evidence_sources"][0]
    assert result["quality"]["confidence_cap"] == 60  # More sources cannot upgrade thin reporting.


def test_retrieval_failure_is_explicit_and_does_not_expose_exception(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("secret-token-and-private-path")

    monkeypatch.setattr(app.services, "event_calendar_service", SimpleNamespace(build_agent_context=fail), raising=False)
    result = event_calendar_context.retrieve("news", as_of=NOW)
    assert result["evidence_sources"] == []
    assert result["limitations"] and result["retrieval"]["method"] == "unavailable"
    assert "secret-token" not in json.dumps(result)


def test_strategy_calendar_changes_explanation_without_manufacturing_direction(monkeypatch):
    monkeypatch.setattr(transaction.strategy_knowledge_service, "query_strategy_knowledge", lambda *args, **kwargs: [])
    bundle = {"symbol": SYMBOL, "stock_name": "测试", "technical": {}, "fundamentals": {}, "flow": {}}
    before = transaction._score_views(bundle)
    after = transaction._score_views({**bundle, "economic_calendar": evidence()})
    for old, new in zip(before, after):
        assert (old["score"], old["confidence"], old["direction"]) == (new["score"], new["confidence"], new["direction"])
        assert new["metadata"]["economic_calendar"]["revision_ids"] == ["revision-before"]
        assert new["metadata"]["rag_context"][0].startswith("[E1]")
        assert new["detailed_analysis"]["sources"][0]["url"] == "https://example.org/calendar/fed"
        assert any("预定日程不等于结果" in line for line in new["risks"])


def test_historical_strategy_retrieves_by_shanghai_day_end(calendar, monkeypatch):
    bar_time = int(datetime(2026, 7, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    async def kline(*args):
        return {"candles": [{"t": bar_time, "c": 10, "v": 100}], "metrics": {}}

    monkeypatch.setattr(transaction.kline_pipeline, "fetch_a_share_kline_with_fallbacks", kline)
    monkeypatch.setattr(transaction.market_history_cache, "load_series", lambda *args: {"items": [{"date": "2026-07-10"}]})
    result = asyncio.run(transaction.collect_signal_bundle(SYMBOL, as_of="2026-07-10"))
    expected = datetime(2026, 7, 10, 23, 59, 59, 999999, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert calendar[0]["agent"] == "strategies" and calendar[0]["as_of"] == expected
    assert result["economic_calendar"]["evidence_sources"]


@pytest.mark.parametrize("end", [NOW, "2026-07-10", "2026-07-10T08:00:00+08:00"])
def test_real_research_proposal_receives_development_only_evidence(calendar, end):
    messages = research_generation._messages(27, {
        "goal": "研究利率传导", "symbols": [SYMBOL], "development_period": {"start": 100, "end": end},
        "holdout": "SECRET_HOLDOUT", "economic_calendar": {"text": "UNTRUSTED_CALLER_EVENTS"},
    })
    assert calendar[0]["agent"] == "research" and calendar[0]["user_id"] == 27
    assert calendar[0]["as_of"] == event_calendar_context.as_of_cutoff(end)
    data = json.loads(messages[1]["content"])
    assert data["context"]["economic_calendar"]["evidence_sources"][0]["revision_id"] == "revision-before"
    assert "SECRET_HOLDOUT" not in str(messages) and "UNTRUSTED_CALLER_EVENTS" not in str(messages)


@pytest.mark.parametrize("development", [{}, {"end": "bad-date"}, {"end": float("nan")}, "bad-period"])
def test_invalid_development_cutoff_never_retrieves_current_events(calendar, development):
    messages = research_generation._messages(27, {"development_period": development})
    assert not calendar
    context = json.loads(messages[1]["content"])["context"]["economic_calendar"]
    assert not context["items"] and context["limitations"]


def test_kline_stock_model_and_return_payload_share_evidence(calendar, monkeypatch):
    from app.storage import kline_bundle_cache

    monkeypatch.setattr(kline_bundle_cache, "load_bundle", lambda *args: {"payload": {"candles": [{"t": NOW, "c": 10}], "metrics": {}}})
    calls = []

    def interpret(system, user):
        calls.append(user)
        return kls.normalize_structured_analysis({}), None

    monkeypatch.setattr(kls, "llm_structured_interpret", interpret)
    token = current_user_id.set(19)
    try:
        result = asyncio.run(kls.analyze_a_share_symbol(SYMBOL))
    finally:
        current_user_id.reset(token)
    assert calendar[0]["agent"] == "kline" and calendar[0]["current_user_id"] == 19
    assert "revision-before" in calls[0] and "结果待公布" in calls[0]
    assert result["economic_calendar"]["evidence_sources"][0]["source_id"] == "E1"


@pytest.mark.parametrize("chat", [False, True])
def test_market_model_and_chat_receive_calendar_rag(calendar, monkeypatch, chat):
    from app.routers import kline_learning as router

    async def kline(*args):
        return {"index_key": "SH_COMP", "symbol": "上证指数", "candles": [], "metrics": {}}

    monkeypatch.setattr(router.kline_pipeline, "fetch_major_index_kline_with_fallbacks", kline)
    messages = []

    def interpret(system, user):
        messages.append(user)
        return ("测试解读" if chat else kls.normalize_structured_analysis({})), None

    monkeypatch.setattr(kls, "llm_interpret" if chat else "llm_structured_interpret", interpret)
    if chat:
        result = asyncio.run(router.market_chat(router.MarketChatBody(question="利率风险有哪些"), SimpleNamespace(id=19)))
    else:
        result = asyncio.run(router.analyze_market(router.AnalyzeMarketBody(), SimpleNamespace(id=19)))
    assert calendar[0]["agent"] == "kline"
    assert "revision-before" in messages[0]
    assert result["economic_calendar"]["evidence_sources"]


def test_legacy_research_preserves_programmatic_calendar_citation():
    from app.services.research_agent.memo_markdown import build_programmatic_citations

    citations = build_programmatic_citations({"economic_calendar": evidence()}, {}, {})
    assert citations[0]["id"] == "E1"
    assert citations[0]["url"] == "https://example.org/calendar/fed"
    assert citations[0]["excerpt"] == "已公布会议日程，结果待公布。"


def test_manual_kline_history_retains_exact_evidence_and_stays_user_scoped(calendar, monkeypatch):
    from app.routers import kline_learning as router
    from app.storage import kline_bundle_cache, kline_insight_store

    monkeypatch.setattr(kline_bundle_cache, "load_bundle", lambda *args: {"payload": {"candles": [{"t": NOW, "c": 10}], "metrics": {}}})
    monkeypatch.setattr(kls, "llm_structured_interpret", lambda *args: (kls.normalize_structured_analysis({}), None))
    token = current_user_id.set(19)
    try:
        result = asyncio.run(router.analyze(router.AnalyzeBody(symbol=SYMBOL), SimpleNamespace(id=19)))
    finally:
        current_user_id.reset(token)
    entries = kline_insight_store.history_for_symbol(19, SYMBOL)["entries"]
    assert next(iter(entries.values()))["economic_calendar"] == result["economic_calendar"]
    assert kline_insight_store.history_for_symbol(20, SYMBOL)["entries"] == {}


def test_scheduled_kline_history_retains_each_accounts_evidence(monkeypatch):
    from app.services import kline_insight_scheduler as scheduler
    from app.storage import kline_insight_store, user_credentials_store, watchlist_store

    for user_id in (19, 20):
        watchlist_store.save_symbols(user_id, [SYMBOL])
        kline_insight_store.set_opt_in(user_id, SYMBOL)
        user_credentials_store.merge_user_credentials(user_id, {"llm": {"api_key": "test-key"}})

    async def analyze(*args, **kwargs):
        calendar = evidence()
        calendar["evidence_sources"][0]["revision_id"] = f"account-{current_user_id.get()}"
        return {"interpretation": "测试", "economic_calendar": calendar}

    async def no_delay(*args):
        pass

    monkeypatch.setattr(kls, "analyze_a_share_symbol", analyze)
    monkeypatch.setattr(scheduler.asyncio, "sleep", no_delay)
    asyncio.run(scheduler.run_daily_batch_if_configured())
    for user_id in (19, 20):
        entries = kline_insight_store.history_for_symbol(user_id, SYMBOL)["entries"]
        row = next(iter(entries.values()))
        assert row["economic_calendar"]["evidence_sources"][0]["revision_id"] == f"account-{user_id}"
    assert current_user_id.get() is None


def _seed_real_calendar_revisions():
    from app.storage import event_calendar_store as store

    row = {
        "source_id": "official-test", "upstream_id": "rate-decision", "title": "测试央行利率决议",
        "source_url": "https://example.org/official/rates", "scheduled_at": store.iso(NOW + 3600),
        "timezone": "Asia/Shanghai", "region": "US", "category": "central_bank", "importance": 3,
        "description": "BEFORE_RELEASE 利率决议日程已确认，实际结果待公布。", "forecast": "4.0", "unit": "%",
    }
    store.ingest_events([row], observed_at=NOW - 7200)
    first = store.visible_events(as_of=NOW)[0]
    store.ingest_events([{**row, "description": "AFTER_RELEASE 实际利率已经公布。", "actual": "9.0", "status": "released"}],
                        observed_at=NOW + 86400)
    return store, first


def test_real_fts_evidence_reaches_news_and_proposals_without_future_revisions(monkeypatch):
    import httpx
    from app.services import event_calendar_service  # Load the actual service, without the fake calendar fixture.

    store, first = _seed_real_calendar_revisions()

    def no_network(*args, **kwargs):
        raise AssertionError("Calendar evidence retrieval must remain local")

    monkeypatch.setattr(httpx, "Client", no_network)
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    monkeypatch.setattr(analysis_context, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(analysis_context.news_store, "analysis_background", lambda *args, **kwargs: [])
    token = current_user_id.set(31)
    try:
        news = analysis_context.build_analysis_context(
            {"title": "测试央行利率决议影响", "summary": "利率变动可能影响融资成本。", "published_ts": NOW - 3600},
            [{"stock_code": SYMBOL, "stock_name": "测试", "industry": "金融"}],
        )
    finally:
        current_user_id.reset(token)
    retrieved = news["economic_calendar"]
    assert retrieved["evidence_sources"] and retrieved["items"]
    assert retrieved["evidence_sources"][0]["revision_id"] == first["revision_id"]
    assert "BEFORE_RELEASE" in json.dumps(retrieved, ensure_ascii=False)
    assert "AFTER_RELEASE" not in json.dumps(news, ensure_ascii=False)
    assert any(source["kind"] == "economic_event" for source in news["evidence_sources"])
    messages = research_generation._messages(31, {
        "goal": "测试央行利率决议", "symbols": [SYMBOL], "development_period": {"start": NOW - 86400, "end": NOW},
    })
    fact = json.loads(messages[1]["content"])["context"]["economic_calendar"]
    assert fact["evidence_sources"][0]["revision_id"] == first["revision_id"]
    assert "AFTER_RELEASE" not in str(messages)
    assert store.matching_chunks("利率", as_of=NOW)[first["revision_id"]]["snippet"]


def test_real_account_switches_disable_only_that_accounts_agent_retrieval(monkeypatch):
    from app.services import event_calendar_service

    store, first = _seed_real_calendar_revisions()
    store.update_config(32, {"agents": {"news": False, "research": False, "strategies": False, "kline": False}})
    results = {}
    for user_id in (31, 32):
        token = current_user_id.set(user_id)
        try:
            results[user_id] = {agent: event_calendar_context.retrieve(agent, "利率", as_of=NOW)
                                for agent in ("news", "research", "strategies", "kline")}
        finally:
            current_user_id.reset(token)
    for value in results[31].values():
        assert value["enabled"] and value["evidence_sources"][0]["revision_id"] == first["revision_id"]
    for value in results[32].values():
        assert not value["enabled"] and not value["evidence_sources"]
    assert current_user_id.get() is None


def test_stock_picker_keeps_thirty_candidates_and_shared_event_references(monkeypatch):
    from app.services import stock_picker_agent_service as picker
    from app.storage import stock_picker_agent_store as store

    agent = store.create_agent(19, {"name": "日历风险筛选", "definition": {
        "system_prompt": "审慎筛选", "output_instruction": "返回 JSON", "min_score": 65,
        "dimensions": [{"key": "risk", "weight": 10}],
    }})
    symbols = [f"{600000 + index}.SS" for index in range(30)]
    calendar = evidence()
    calendar["evidence_sources"] = [{**calendar["evidence_sources"][0], "source_id": f"E{index}",
        "event_id": f"event-{index}", "revision_id": str(index) * 64, "text": "来源正文" * 500}
        for index in range(1, 9)]
    calendar["items"] = [{"revision_id": source["revision_id"], "status": "released", "actual": "3.5",
                          "forecast": "4.0", "previous": "4.2", "unit": "%"}
                         for source in calendar["evidence_sources"]]
    strategies = [{"strategy_id": strategy["id"], "score": 70, "confidence": .6, "direction": "bullish",
                   "metadata": {"rag_context": ["重复正文" * 1000]},
                   "detailed_analysis": {"sources": calendar["evidence_sources"]}}
                  for strategy in transaction.STRATEGIES]

    async def collect(symbol):
        return {"symbol": symbol, "stock_name": "测试", "strategies": strategies,
                "signals": {"technical": {"latest_close": 10}, "flow": {"rows": 5, "main_net_inflow_5d": 1234},
                            "fundamentals": {"industry": "食品", "pe_ttm": 20}, "economic_calendar": calendar},
                "summary": {"average_score": 70}}

    requests = []

    def complete(messages, **kwargs):
        requests.append(messages)
        return {"selected_stocks": [], "watchlist": []}, None

    monkeypatch.setattr(picker.transaction_agent_service, "generate_strategy_views", collect)
    monkeypatch.setattr(picker.llm_client, "chat_completion_json_array", complete)
    result = asyncio.run(picker.run_agent(19, agent["id"], symbols))
    payload = json.loads(requests[0][1]["content"].split("\n", 1)[1])
    assert [candidate["symbol"] for candidate in payload["candidate_data"]] == symbols
    assert len(requests[0][1]["content"]) < 80_000
    assert len(payload["event_evidence"]) == 8  # No repeated corpus for every strategy or candidate.
    assert "重复正文" not in requests[0][1]["content"]
    assert payload["agent_definition"]["dimensions"] == [{"key": "risk", "weight": 10}]
    assert "system_prompt" not in payload["agent_definition"]
    refs = {source["evidence_id"]: source for source in payload["event_evidence"]}
    for candidate in payload["candidate_data"]:
        assert len(candidate["strategy_signals"]) == len(transaction.STRATEGIES)
        assert candidate["technical"]["latest_close"] == 10
        assert candidate["flow"]["main_net_inflow_5d"] == 1234
        assert candidate["fundamentals"]["pe_ttm"] == 20
        for reference in candidate["economic_calendar"]["evidence"]:
            source = refs[reference["evidence_id"]]
            assert source["revision_id"] and source["url"] == "https://example.org/calendar/fed"
            assert source["status"] == "released" and source["actual"] == "3.5" and source["forecast"] == "4.0"
    assert result["result"]["event_evidence"] == payload["event_evidence"]
    saved = store.list_runs(19)[0]["result"]
    assert set(saved["candidate_calendar_contexts"]) == set(symbols)
    assert store.list_runs(20) == []


def test_strategy_details_reference_shared_calendar_without_copying_full_documents(monkeypatch):
    monkeypatch.setattr(transaction.strategy_knowledge_service, "query_strategy_knowledge", lambda *args, **kwargs: [])
    calendar = evidence()
    calendar["evidence_sources"][0]["text"] = "资料正文" * 1000
    views = transaction._score_views({"symbol": SYMBOL, "technical": {}, "fundamentals": {}, "flow": {},
                                     "economic_calendar": calendar})
    for view in views:
        source = view["detailed_analysis"]["sources"][0]
        assert source["revision_id"] == "revision-before" and source["url"]
        assert "text" not in source
        assert len(view["metadata"]["rag_context"][0]) < 300
