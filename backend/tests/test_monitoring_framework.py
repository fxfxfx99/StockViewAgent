import asyncio
import json
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.deps.auth import get_current_user
from app.routers import monitoring
from app.schemas.monitoring import SHANGHAI
from app.security.user_context import current_user_id
from app.services import monitoring_engine as engine
from app.storage import company_fundamentals_store, decision_signal_store, kline_bundle_cache, monitoring_store as store, xueqiu_comments_store


def plan(**extra):
    return store.create_plan(1, {"title": "关注公司变化", "target_type": "stock", "target_code": "600519.SS",
                                 "goal": "跟踪公司", "watch_conditions": "核对经营变化", **extra})


def completed(target, text="公开评论甲", observed=None):
    queued = store.enqueue_run(1, target["id"])
    run = store.claim_run("test")
    assert run["id"] == queued["id"]
    row = engine._fact("forum:" + text, "论坛观点", text, observed=observed or time.time(), published=time.time()-30)
    row.update(factual_support=False, evidence_role="opinion")
    result = {"report": engine._empty_report(target), "evidence": [row], "coverage": []}
    return store.complete_run(1, run["id"], run["lease_token"], result)


def test_schema_migration_keeps_old_plan_and_adds_handling_table():
    target = plan()
    with store.connection() as conn:
        old = {key: value for key, value in target.items() if key not in {"channels", "research_link"}}
        conn.execute("UPDATE monitoring_plans SET payload=? WHERE id=?", (json.dumps(old), target["id"]))
        conn.execute("DROP TABLE monitoring_handling")
        conn.execute("PRAGMA user_version=1")
    read = store.get_plan(1, target["id"])
    assert read["channels"] == ["news", "price"] and read["research_link"] is None
    assert store.list_handling(1, target["id"]) == []
    assert store.update_plan(1, target["id"], {"revision": 1, "channels": ["forum"]})["channels"] == ["forum"]


def test_new_opinion_evidence_notifies_even_without_confirmed_condition_and_handling_is_immutable():
    target = plan(channels=["forum"])
    completed(target)
    assert store.list_notifications(1) == []
    second = completed(target, "公开评论乙")
    notice = store.list_notifications(1)[0]
    assert notice["kind"] == "evidence_change" and notice["handling_status"] == "pending"
    store.mark_notification(1, notice["id"])
    assert store.list_notifications(1)[0]["handling_status"] == "pending"
    body = {"action": "reviewed", "reason": "已核对原讨论，暂不改变判断", "run_id": second["id"]}
    assert store.record_handling(2, notice["id"], body) is None
    first = store.record_handling(1, notice["id"], body)
    assert first["handling_status"] == "reviewed"
    assert store.record_handling(1, notice["id"], body)["latest_handling"]["id"] == first["latest_handling"]["id"]
    history = store.list_handling(1, target["id"])
    assert len(history) == 1 and history[0]["source_snapshot"]["evidence"][0]["excerpt"] == "公开评论乙"
    with store.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE monitoring_handling SET reason='改写'")


def test_local_forum_and_strategy_are_account_scoped_opinions_and_channels_are_explicit():
    stamp = time.time()
    with xueqiu_comments_store.connect() as conn:
        for uid in (1, 2):
            conn.execute("INSERT INTO comment_snapshots(uid,symbol,result_json,status,updated_at) VALUES(?,?,?,?,?)",
                         (uid, "600519.SS", json.dumps({"items": [{"id": str(uid), "text": f"用户{uid}选择的讨论", "created_at": stamp-30}]}), "ready", stamp))
    for uid in (1, 2):
        decision_signal_store.create(uid, {"symbol": "600519.SS", "reason": f"用户{uid}的观点"})
    token = current_user_id.set(1)
    try:
        rows, coverage = engine._collect_evidence(plan(channels=["forum", "strategy"]))
    finally:
        current_user_id.reset(token)
    assert {row["source"] for row in coverage} == {"forum", "strategy"}
    assert len(rows) == 2 and all(row["factual_support"] is False for row in rows)
    assert all("用户2" not in row["excerpt"] for row in rows)


def test_fresh_opinion_cannot_confirm_a_condition():
    target = plan()
    fact = engine._fact("forum:1", "观点", "作者称现金流改善", published=time.time()-60, observed=time.time())
    fact.update(factual_support=False, evidence_role="opinion")
    raw = engine._empty_report(target)
    raw.update(summary="现金流已改善", assessment="watch", changes=["事实：现金流改善"], evidence_ids=[fact["id"]])
    raw["conditions"] = [{"condition": "观察条件", "status": "met", "evidence_ids": [fact["id"]]}]
    checked = engine._validate_report(json.dumps(raw), [fact], target)
    assert checked["assessment"] == "insufficient_data" and checked["conditions"][0]["status"] == "uncertain"
    assert "现金流已改善" not in checked["summary"]


def test_local_trend_indicators_use_validated_bars_without_network(monkeypatch):
    now = time.time()
    bars = [{"t": now - (60-index)*86400, "o": 10+index, "h": 12+index, "l": 9+index,
             "c": 11+index, "v": 1000+index} for index in range(60)]
    kline_bundle_cache.save_bundle("600519.SS", "1y", "1d", {"symbol": "600519.SS", "candles": bars, "kline_source": "test"})
    rows, coverage = engine._collect_evidence(plan(channels=["indicators"]))
    assert len(rows) == 1 and coverage[0]["source"] == "indicators"
    assert rows[0]["factual_support"] and rows[0]["evidence_role"] == "derived_indicator"
    value = json.loads(rows[0]["excerpt"])
    assert value["indicators"]["ma5"]["ma5"] == pytest.approx(sum(row["c"] for row in bars[-5:])/5)
    assert value["bars_used"] == 60 and value["input_hash"]
    # A racing price-cache refresh cannot mark the old calculation as fresh.
    for bar in bars:
        bar["t"] -= 30*86400
    kline_bundle_cache.save_bundle("600519.SS", "1y", "1d", {"symbol": "600519.SS", "candles": bars, "kline_source": "test"})
    monkeypatch.setattr(engine, "_candles", lambda *args: [{"stale": False}])
    rows, _ = engine._collect_evidence(plan(channels=["indicators"]))
    assert rows[0]["stale"] is True


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "enable_monitoring", True)
    monkeypatch.setattr(engine, "model_status", lambda uid: {"configured": True, "verified": True})
    app = FastAPI()
    app.include_router(monitoring.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        yield client


def test_context_and_symbol_filter_do_not_mix_accounts_or_trigger_work(api):
    one = plan()
    plan(title="另一只", target_code="000001.SZ")
    run = completed(one)
    view = api.get("/api/monitoring/overview?symbol=600519.SS").json()
    assert [value["id"] for value in view["plans"]] == [one["id"]]
    path = f"/api/monitoring/runs/{run['id']}/context"
    context = api.get(path).json()
    assert context["target_symbols"] == ["600519.SS"] and context["trigger_date_kind"] == "monitor_check_date"
    assert context["trigger_date"] == datetime.fromtimestamp(run["finished_at"], SHANGHAI).date().isoformat()
    assert "lease_token" not in context["run"] and store.usage(1)["used"] == 0
    api.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    assert api.get(path).status_code == 404


def test_research_handoff_checks_ownership_target_and_deduplicates(api, monkeypatch):
    from app.storage import investment_research_store
    origin = {"dossier_id": "d1", "result": {"dossier_snapshot": {"target_type": "stock", "target_code": "600519.SS"},
                                            "report": {"next_observations": [{"title": "观察现金流"}]}}}
    monkeypatch.setattr(investment_research_store, "get_report", lambda uid, rid: origin if uid == 1 and rid == "r1" else None)
    body = {"title": "研究后续观察", "target_type": "stock", "target_code": "600519.SS", "goal": "跟踪变化",
            "research_link": {"dossier_id": "d1", "report_id": "r1", "observation_index": 0}}
    first = api.post("/api/monitoring/plans", json=body)
    assert first.status_code == 201
    assert api.post("/api/monitoring/plans", json=body).json()["id"] == first.json()["id"]
    assert api.post("/api/monitoring/plans", json={**body, "target_code": "000001.SZ"}).status_code == 422
    api.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    assert api.post("/api/monitoring/plans", json=body).status_code == 404


def test_financial_history_preserves_units_period_and_provenance_without_network(tmp_path):
    assert company_fundamentals_store.financial_history("600519.SS")["items"] == []
    assert not (settings.data_dir / company_fundamentals_store.DB_NAME).exists()
    source = tmp_path / "financial.csv"
    source.write_text("FI_T1.Stkcd,FI_T1.Accper,FI_T1.Typrep,FI_T1.Source,FI_T1.F010101A,revenue,revenue_unit,margin,AnnDate,FI_T1.Annodt,AvailableAt\n"
                      "600519,2025-12-31,A,0,1.23,10000,元,15%,2026-04-17,20260417,1790000000\n"
                      "600519,2024-12-31,A,0,1.00,9000,元,14%,2025-04-17,20250417,1750000000\n"
                      "600519,2099-12-31,A,0,1.00,9000,元,14%,2099-04-17,,\n"
                      "600519,2026-02-31,A,0,1.00,9000,元,14%,2026-04-17,,\n", encoding="utf-8")
    company_fundamentals_store.import_financial_csv(source)
    response = company_fundamentals_store.financial_history("600519.SS")
    assert len(response["items"]) == 2
    row = response["items"][0]
    assert row["values"] == {"FI_T1.F010101A": 1.23, "revenue": 10000, "margin": 15}
    assert row["units"] == {"FI_T1.F010101A": None, "revenue": "元", "margin": "%"}
    assert row["report_date"] == "2025-12-31" and row["published_at"] == "2026-04-17"
    assert row["source_name"] == "financial.csv" and row["row_hash"]


def test_research_topic_origin_cannot_change_keywords_and_target_edit_clears_origin(api, monkeypatch):
    from app.storage import investment_research_store
    snapshot = {"target_type": "topic", "target_code": "", "keywords": ["订单"]}
    origin = {"dossier_id": "d1", "result": {"dossier_snapshot": snapshot,
              "report": {"next_observations": [{"title": "订单观察"}]}}}
    monkeypatch.setattr(investment_research_store, "get_report", lambda *args: origin)
    body = {**snapshot, "title": "后续观察", "goal": "核对变化", "research_link": {
        "dossier_id": "d1", "report_id": "r1", "observation_index": 0}}
    assert api.post("/api/monitoring/plans", json={**body, "keywords": ["铜价"]}).status_code == 422
    target = api.post("/api/monitoring/plans", json=body).json()
    updated = store.update_plan(1, target["id"], {"revision": 1, "keywords": ["铜价"]})
    assert updated["research_link"] is None
    # The original revision retains where the original observation came from.
    with store.connection() as db:
        original = db.execute("SELECT snapshot FROM monitoring_revisions WHERE plan_id=? AND revision=1", (target["id"],)).fetchone()
    assert json.loads(original[0])["research_link"] == body["research_link"]


def test_handling_research_requires_actual_same_run_import(api):
    from app.routers import investment_research
    api.app.include_router(investment_research.router)
    target = plan()
    completed(target)
    run = completed(target, "新增评论")
    notice = store.list_notifications(1)[0]
    created = api.post("/api/investment-research/dossiers", json={"title": "核对变化", "target_type": "stock",
                       "target_code": target["target_code"], "objective": "核对监控变更"})
    assert created.status_code == 201
    dossier = created.json()
    path = f"/api/monitoring/notifications/{notice['id']}/handling"
    body = {"action": "research", "reason": "需要结合原文进一步核对", "dossier_id": dossier["id"], "run_id": run["id"]}
    assert api.post(path, json={**body, "dossier_id": "foreign"}).status_code == 404
    assert api.post(path, json=body).status_code == 409
    imported = api.post(f"/api/investment-research/dossiers/{dossier['id']}/monitoring-updates", json={"monitor_run_id": run["id"]})
    assert imported.status_code == 201
    handled = api.post(path, json=body)
    assert handled.status_code == 200 and handled.json()["handling_status"] == "research"
    assert api.post(path, json=body).json()["latest_handling"]["id"] == handled.json()["latest_handling"]["id"]
    assert store.usage(1)["used"] == 0
    assert len(store.list_handling(1, target["id"])) == 1


def test_announcement_channel_distinguishes_catalog_from_cached_excerpt():
    from app.storage import company_updates_store, earnings_store
    now = time.time()
    earnings_store.save_discovery("600519.SS", {"items": [{"identity_key": "cninfo:sample", "symbol": "600519.SS",
        "title": "季度报告", "report_period": "2026-09-30", "report_type": "quarterly", "document_kind": "full_report",
        "source": "cninfo", "published_at": now-100, "announcement_date": "2026-10-30"}], "sources": []})
    company_updates_store.save("600519.SS", {"bundle": {"section_fetched_at": {"major_events": now},
        "major_events": [{"title": "经营公告", "message": "公告原文片段", "created_at": (now-100)*1000}]}})
    rows, coverage = engine._collect_evidence(plan(channels=["announcements"]))
    assert len(rows) == 2 and {row["evidence_role"] for row in rows} == {"announcement_metadata", "announcement_excerpt"}
    catalog = next(row for row in rows if row["evidence_role"] == "announcement_metadata")
    assert "尚未把财报正文" in catalog["excerpt"]
    assert all(row["factual_support"] and not row["stale"] for row in rows)
    old = next(row for row in rows if row["evidence_role"] == "announcement_excerpt")
    company_updates_store.save("600519.SS", {"bundle": {"section_fetched_at": {"major_events": now},
        "major_events": [{"title": "经营公告", "text_excerpt": "修订后的公开原文片段", "created_at": (now-100)*1000}]}})
    refreshed, _ = engine._collect_evidence(plan(channels=["announcements"]))
    new = next(row for row in refreshed if row["evidence_role"] == "announcement_excerpt")
    assert new["excerpt"] == "修订后的公开原文片段" and new["id"] != old["id"]


def test_real_research_to_monitor_and_back_preserves_current_judgment(api, monkeypatch):
    import httpx
    from app.routers import investment_research
    from app.services import investment_research as service, investment_research_engine
    from app.storage import investment_research_store as research, user_credentials_store, users_store
    monkeypatch.setattr(settings, "enable_investment_research", True)
    monkeypatch.setattr(settings, "auth_required", False)
    users_store.init_db()
    user_credentials_store.merge_user_credentials(1, {"llm": {"api_key": "fake-key", "api_base": "https://api.openai.com/v1", "model": "test"}})
    api.app.include_router(investment_research.router)
    calls, original = [], httpx.AsyncClient
    def response(request):
        context = json.loads(json.loads(request.content)["messages"][-1]["content"])
        calls.append(context)
        report = {"summary": "待核实的公司观察", "assessment": "insufficient_data", "findings": [
            {"kind": "interpretation", "text": "个人观点仍需资料验证", "evidence_ids": [context["evidence"][0]["id"]]}],
            "judgment_changes": [], "open_questions": [], "next_observations": [{"title": "回款观察", "goal": "持续核对回款",
            "watch_conditions": "新增回款披露", "invalidation_conditions": "回款恶化"}], "risks": [], "limitations": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(report)}}]})
    monkeypatch.setattr(investment_research_engine.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(response), **kw))
    dossier = api.post("/api/investment-research/dossiers", json={"title": "公司现金流", "target_type": "stock",
        "target_code": "600519.SS", "objective": "核对回款", "thesis": "个人假说"}).json()
    path = "/api/investment-research/dossiers/" + dossier["id"]
    assert api.post(path + "/questions", json={"text": "回款趋势是否持续？"}).status_code == 201
    assert api.post(path + "/materials", json={"title": "笔记", "text": "待核实的现金流判断"}).status_code == 201
    assert api.post(path + "/generate", json={}).status_code == 202
    asyncio.run(service.run_cycle())
    detail = api.get(path).json()
    report = detail["reports"][0]
    assert detail["dossier"]["adopted_report_id"] is None
    assert api.post(f"/api/investment-research/reports/{report['id']}/adopt", json={"revision": detail["dossier"]["revision"]}).status_code == 200
    observation = report["result"]["report"]["next_observations"][0]
    target = api.post("/api/monitoring/plans", json={**{key: observation[key] for key in ("title", "goal", "watch_conditions", "invalidation_conditions")},
        "target_type": "stock", "target_code": "600519.SS", "research_link": {
        "dossier_id": dossier["id"], "report_id": report["id"], "observation_index": 0}})
    assert target.status_code == 201
    completed(target.json())
    run = completed(target.json(), "回款新观点")
    notice = store.list_notifications(1)[0]
    assert api.post(path + "/monitoring-updates", json={"monitor_run_id": run["id"]}).status_code == 201
    assert api.post(f"/api/monitoring/notifications/{notice['id']}/handling", json={"action": "research", "reason": "复核最新观点",
        "dossier_id": dossier["id"], "run_id": run["id"]}).status_code == 200
    after = api.get(path).json()
    assert after["dossier"]["adopted_report_id"] == report["id"] and after["dossier"]["thesis"] == "个人假说"
    assert after["materials"][-1]["monitor_run_id"] == run["id"]
    assert len(calls) == research.usage(1)["used"] == 1
