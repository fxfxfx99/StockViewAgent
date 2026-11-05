import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.routers import stock_features as routes
from app.services import stock_features as service
from app.storage import stock_feature_store as store

NOW = datetime.fromisoformat("2026-11-04T10:00:00+08:00").timestamp()
SYMBOL = "600519.SS"


def snapshot(now=NOW, value="白酒"):
    return {"stock_code": SYMBOL, "as_of": store.iso(now), "name": "测试公司",
            "features": [{"id": "business", "label": "主营", "category": "business", "value": value,
                          "status": "reported", "source_ids": ["source1"]}],
            "factor_links": [], "evidence_sources": [{"source_id": "source1", "text": value}],
            "coverage": {"status": "partial"}, "stats": {}}


def publish(now=NOW, value="白酒"):
    token, _ = store.claim(SYMBOL, now=now, force=True)
    return store.publish(SYMBOL, snapshot(now, value), token=token, now=now,
                         next_due=service.next_weekly_due(now))[0]


def test_point_in_time_versions_never_backfill_and_are_immutable():
    first = publish()
    second = publish(NOW + 3600, "白酒及其他业务")
    assert store.get_snapshot(SYMBOL, as_of=NOW - 1) is None
    assert store.get_snapshot(SYMBOL, as_of=NOW + 1)["version_id"] == first["version_id"]
    assert store.get_snapshot(SYMBOL, as_of=NOW + 3601)["version_id"] == second["version_id"]
    assert second["changes"]["changed"][0]["before"] == "白酒"
    assert store.get_snapshot("000001.SZ", version_id=first["version_id"]) is None
    with pytest.raises(sqlite3.IntegrityError), store.connection() as conn:
        conn.execute("UPDATE feature_versions SET symbol='000001.SZ'")
    with pytest.raises(sqlite3.IntegrityError), store.connection() as conn:
        conn.execute("DELETE FROM feature_versions")


def test_backdated_asof_cannot_make_new_snapshot_visible_in_past():
    token, _ = store.claim(SYMBOL, now=NOW)
    store.publish(SYMBOL, snapshot(NOW - 10000), token=token, now=NOW,
                  next_due=service.next_weekly_due(NOW))
    assert store.get_snapshot(SYMBOL, as_of=NOW - 1) is None


def test_refresh_reuses_same_week_but_retains_unchanged_weekly_observation():
    first = publish()
    assert publish(NOW + 60)["version_id"] == first["version_id"]
    next_week = publish(NOW + 7 * 86400)
    assert next_week["version_id"] == first["version_id"]
    assert len(store.history(SYMBOL)) == 1
    assert len(store.checks(SYMBOL, as_of=NOW + 8 * 86400)) == 3
    historical = service.get_snapshot(SYMBOL, as_of=NOW + 1)
    assert historical["last_checked_at"] == store.iso(NOW)
    assert service.get_snapshot(SYMBOL, as_of=NOW + 8 * 86400)["last_checked_at"] == store.iso(NOW + 7 * 86400)


def test_weekly_boundary_uses_shanghai_and_resumes_due_queue():
    assert store.iso(service.next_weekly_due(NOW)) == "2026-11-07T01:00:00+00:00"
    exact = datetime.fromisoformat("2026-11-07T09:00:00+08:00").timestamp()
    assert service.next_weekly_due(exact) == exact + 7 * 86400
    store.register([SYMBOL, "000001.SZ"])
    publish()
    assert store.due_symbols(now=NOW + 1) == ["000001.SZ"]
    assert set(store.due_symbols(now=NOW + 14 * 86400)) == {SYMBOL, "000001.SZ"}


def test_concurrent_claims_and_expired_owner_cannot_publish():
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: store.claim(SYMBOL, now=NOW), range(4)))
    assert len([token for token, _ in claims if token]) == 1
    old = next(token for token, _ in claims if token)
    newer, _ = store.claim(SYMBOL, now=NOW + 601)
    assert newer and store.publish(SYMBOL, snapshot(), token=old, now=NOW + 602,
                                  next_due=service.next_weekly_due(NOW))[1] == "superseded"
    assert store.get_snapshot(SYMBOL) is None


def test_failure_retains_previous_snapshot_and_retries_after_one_hour(monkeypatch):
    from app.services import stock_feature_engine
    first = publish()
    monkeypatch.setattr(service.time, "time", lambda: NOW + 60)
    def fail(*args, **kwargs):
        raise RuntimeError("secret path and provider credentials")
    monkeypatch.setattr(stock_feature_engine, "build_snapshot", fail)
    result = service.refresh(SYMBOL)
    assert result["refresh_status"] == "failed"
    assert result["snapshot"]["version_id"] == first["version_id"]
    assert "secret" not in str(result)
    assert store.due_symbols(now=NOW + 3600) == []
    assert store.due_symbols(now=NOW + 3660) == [SYMBOL]


def test_initial_build_and_immediate_repeat_do_not_duplicate_work(monkeypatch):
    from app.services import stock_feature_engine
    calls = []
    monkeypatch.setattr(service.time, "time", lambda: NOW)
    monkeypatch.setattr(stock_feature_engine, "build_snapshot", lambda symbol: calls.append(symbol) or snapshot())
    first = service.refresh("SH600519")
    again = service.refresh(SYMBOL)
    assert first["refresh_status"] == "updated"
    assert again["refresh_status"] == "cooldown" and calls == [SYMBOL]


@pytest.mark.parametrize("value", ["../../600519", "https://site/600519", "600519.SS/x", "000001.SZ;", ""])
def test_invalid_symbol_never_creates_registry_entry(value):
    with pytest.raises(ValueError):
        service.read(value)


@pytest.mark.parametrize("cancel_count", [1, 2])
def test_cancellation_drains_writer_before_returning(cancel_count):
    started, finish = threading.Event(), threading.Event()
    results = []
    def writer():
        started.set()
        assert finish.wait(3)
        results.append("committed")
    async def scenario():
        task = asyncio.create_task(service.durable_call(writer))
        await asyncio.to_thread(started.wait, 2)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert results == ["committed"]


@pytest.mark.parametrize("missing", [True, False])
def test_partial_read_failure_or_disappeared_evidence_retains_last_version(monkeypatch, missing):
    from app.services import stock_feature_engine
    first = publish()
    monkeypatch.setattr(service.time, "time", lambda: NOW + 60)
    changed = snapshot(NOW + 60, "不能覆盖上次证据")
    if missing:
        changed["features"] = []
    else:
        changed["coverage"]["source_errors"] = ["fundamentals_read_failed"]
    monkeypatch.setattr(stock_feature_engine, "build_snapshot", lambda symbol: changed)
    result = service.refresh(SYMBOL)
    assert result["refresh_status"] == "failed"
    assert result["snapshot"]["version_id"] == first["version_id"]


def test_routes_require_authentication(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        for method, suffix in [("GET", ""), ("GET", "/history"), ("POST", "/refresh")]:
            assert client.request(method, f"/api/stocks/{SYMBOL}/features{suffix}").status_code == 401


def test_symbol_registry_includes_universe_and_all_accounts(monkeypatch):
    from app.services import a_share_stocks
    from app.storage import watchlist_store
    monkeypatch.setattr(a_share_stocks, "load_stocks", lambda: [{"symbol": SYMBOL}, {"symbol": "bad/path"}])
    monkeypatch.setattr(watchlist_store, "load_all_symbols_union", lambda: [SYMBOL, "000001.SZ"])
    assert service.register_universe() == 2
    assert set(store.due_symbols()) == {SYMBOL, "000001.SZ"}


def test_resource_pressure_leaves_due_work_for_later(monkeypatch):
    store.register([SYMBOL])
    main_thread = threading.get_ident()
    def pressure():
        assert threading.get_ident() != main_thread
        return False
    monkeypatch.setattr(service, "_resource_ready", pressure)
    assert asyncio.run(service.run_batch()) == {"status": "resource_wait", "processed": 0}
    assert store.due_symbols() == [SYMBOL]
    assert store.state(SYMBOL)["last_attempt"] is None


def test_feature_change_reopens_only_relevant_news_and_preserves_prior_analysis(monkeypatch):
    from app.services.analysis_context import analysis_features_current, feature_context_signature
    from app.services.news_relevance_knowledge import current_analysis_signature
    from app.storage import news_store
    monkeypatch.setattr(service.time, "time", lambda: NOW + 1000)
    publish()
    article = {"title": "600519公司发布经营情况", "content": "公司发布最新经营情况。", "source": "fixture",
               "link": "https://example.org/stock-feature-test", "published_ts": NOW + 100,
               "related_codes": [SYMBOL]}
    news_store.upsert_many([article])
    old = {"stock_code": SYMBOL, "status": "analyzed", "relevance_score": 90,
           "information_value": True, "relevance_type": "direct", "reasoning": "旧分析",
           "analysis_logic_signature": current_analysis_signature(SYMBOL)}
    news_store.save_analysis(article["id"], {"related_stocks": [old]})
    assert news_store.list_pending([SYMBOL])[0]["analysis_symbols"] == [SYMBOL]
    current = {**old, "stock_feature_signature": feature_context_signature(article, [SYMBOL])[SYMBOL]}
    news_store.save_analysis(article["id"], {"related_stocks": [current]})
    assert news_store.list_pending([SYMBOL]) == []
    # Later facts never replace the historical event's feature snapshot. A new
    # relevance-knowledge revision independently requests a logic recheck.
    publish(NOW + 200, "新业务")
    assert analysis_features_current(article, current)
    assert news_store.list_pending([SYMBOL])[0]["analysis_symbols"] == [SYMBOL]
    news_store.save_analysis(article["id"], {"related_stocks": [
        {**current, "analysis_logic_signature": current_analysis_signature(SYMBOL)}]})
    assert news_store.list_pending([SYMBOL]) == []
    with news_store._connect() as conn:
        prior = conn.execute("SELECT analysis_json,reason FROM article_stock_analysis_history").fetchone()
    assert "旧分析" in prior[0] and prior[1] == "feature_context_changed"


def test_routes_return_latest_and_exact_stock_version(monkeypatch):
    from app.deps.auth import get_current_user
    monkeypatch.setattr(service.time, "time", lambda: NOW + 1000)
    first = publish()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_current_user] = lambda: object()
    with TestClient(app) as client:
        current = client.get(f"/api/stocks/{SYMBOL}/features").json()
        assert current["snapshot"]["version_id"] == first["version_id"]
        assert current["schedule"]["timezone"] == "Asia/Shanghai"
        assert client.get(f"/api/stocks/{SYMBOL}/features/history", params={"version_id": first["version_id"]}).status_code == 200
        assert client.get("/api/stocks/000001.SZ/features/history", params={"version_id": first["version_id"]}).status_code == 404
        assert client.get("/api/stocks/invalid!symbol/features").status_code == 422


def test_failed_feature_recheck_keeps_model_only_association_retryable(monkeypatch):
    from app.storage import news_store
    monkeypatch.setattr(service.time, "time", lambda: NOW + 1000)
    publish()
    article = {"title": "某项行业资料变动", "content": "这条材料没有公司代码或发行方关联。", "source": "fixture",
               "link": "https://example.org/model-only-association", "published_ts": NOW + 100}
    news_store.upsert_many([article])
    old = {"stock_code": SYMBOL, "status": "analyzed", "relevance_score": 90,
           "information_value": True, "relevance_type": "indirect", "reasoning": "旧判断"}
    news_store.save_analysis(article["id"], {"related_stocks": [old]})
    for attempt in range(2):
        moment = NOW + 1100 + attempt * 2000
        monkeypatch.setattr(service.time, "time", lambda: moment)
        news_store.save_analysis(article["id"], {"related_stocks": [{"stock_code": SYMBOL, "status": "error", "relevance_score": None}]})
        assert news_store.list_pending([SYMBOL]) == []  # Existing failure cooldown.
        page, total = news_store.list_archive(symbol=SYMBOL)
        assert total == 1 and page[0]["analysis_status"] == "error"
        assert page[0]["analysis"]["relevance_from_previous_analysis"]
        monkeypatch.setattr(service.time, "time", lambda: moment + 1801)
        assert news_store.list_pending([SYMBOL])[0]["analysis_symbols"] == [SYMBOL]
