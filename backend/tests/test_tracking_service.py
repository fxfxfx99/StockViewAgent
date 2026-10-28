"""All tracking checks use temporary archives and no network/model credentials."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import sqlite3

import pytest

from app.config import settings
from app.services import local_autopilot, tracking_service as svc
from app.services.market_time import SHANGHAI_TZ
from app.storage import event_calendar_store as calendar, news_store, research_store, tracking_store as store

UID = 71
NOW = datetime(2026, 10, 29, 17, tzinfo=SHANGHAI_TZ).timestamp()


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.setattr(svc.news_watchlist_matcher, "load_watchlist_match_hints", lambda: ([], {}))
    monkeypatch.setattr(svc.news_watchlist_matcher, "_company_hints", lambda symbol: [])
    monkeypatch.setattr(svc.news_watchlist_matcher, "load_match_keywords", lambda: {})
    monkeypatch.setattr(svc, "_news_collection_health", lambda uid: {"status": "success", "finished_at": NOW})
    watchlist(["600519.SS"])


def watchlist(symbols, uid=UID):
    root = settings.data_dir / "watchlists"
    root.mkdir(exist_ok=True)
    (root / f"{uid}.json").write_text(json.dumps({"symbols": symbols}))


def article(key="first", *, text="公司公布年度报告，收入增加", observed=NOW, published=None, symbol="600519.SS"):
    value = {"title": f"{key} {symbol} 公司年度报告", "url": f"https://example.test/notice/{key}",
             "summary": key + " 公告摘要", "content": text, "source": "fixture",
             "published_ts": observed if published is None else published, "fetched_at": observed,
             "related_codes": [symbol]}
    news_store.upsert_many([value])
    return value["id"]


def changes(kind="news", uid=UID, **kwargs):
    return [r for r in store.list_changes(uid, **kwargs) if r["kind"] == kind]


def thesis(*, refs=None, review_at="2026-11-29", uid=UID):
    return store.create_thesis(uid, {"symbol": "600519.SS", "reason": "观察利润率改善", "review_at": review_at,
                                   "invalidation_conditions": "利润率继续下降", "evidence_refs": refs or []})


def event(*, scheduled=NOW + 86400, observed=NOW, actual=None):
    raw = {"source_id": "fixture", "upstream_id": "rates", "title": "政策利率安排",
           "source_url": "https://example.test/calendar/rates", "scheduled_at": scheduled,
           "timezone": "Asia/Shanghai", "region": "CN", "category": "central_bank", "actual": actual}
    calendar.ingest_events([raw], observed_at=observed)
    return calendar.visible_events(as_of=observed)[0]


def test_initial_recent_overview_and_old_correction_are_distinct_and_idempotent():
    aid = article("old", observed=NOW - 20 * 86400)
    article("recent", observed=NOW - 86400)
    result = svc.refresh(UID, now=NOW)
    assert result["status"] == "completed"
    assert len(changes()) == 1 and changes()[0]["overview"]
    article("old", text="正文更正，年度收入实际下降", observed=NOW + 10, published=NOW - 20 * 86400)
    svc.refresh(UID, now=NOW + 20)
    row = next(r for r in changes() if r["change_type"] == "news_correction")
    assert row["evidence_ref"]["id"] == aid
    assert row["published_at"] != row["observed_at"]
    assert row["previous_evidence_ref"]["snapshot"]["content"] != row["evidence_ref"]["snapshot"]["content"]
    assert svc.refresh(UID, now=NOW + 30)["inserted"] == 0
    assert len(changes()) == 2


def test_withdrawn_correction_returning_to_original_body_is_not_lost():
    article(text="原始业绩信息", observed=NOW, published=NOW - 20 * 86400)
    svc.refresh(UID, now=NOW)
    article(text="更正业绩信息", observed=NOW + 10, published=NOW - 20 * 86400)
    svc.refresh(UID, now=NOW + 11)
    article(text="原始业绩信息", observed=NOW + 20, published=NOW - 20 * 86400)
    svc.refresh(UID, now=NOW + 21)
    rows = changes()
    assert len(rows) == 3
    assert len({row["evidence_ref"]["revision_id"] for row in rows}) == 3
    assert len([row for row in rows if row["change_type"] == "news_correction"]) == 2
    assert svc.refresh(UID, now=NOW + 22)["inserted"] == 0


def test_source_supplement_is_low_priority_and_can_be_saved_as_evidence():
    aid = article()
    svc.refresh(UID, now=NOW)
    with news_store._connect() as conn:
        previous = dict(conn.execute("SELECT * FROM article_content_revisions WHERE article_id=?", (aid,)).fetchone())
        snapshot = json.loads(previous["snapshot_json"])
        snapshot["source_articles"] = [{"source": "second", "url": "https://example.test/reprint"}]
        conn.execute("""INSERT INTO article_content_revisions(article_id,content_hash,snapshot_json,published_ts,first_observed_at,recorded_at,origin)
            VALUES(?,?,?,?,?,?,?)""", (aid, "supplement", json.dumps(snapshot), NOW, NOW + 10, NOW + 10, "archive_write"))
    svc.refresh(UID, now=NOW + 20)
    row = next(r for r in changes() if r["change_type"] == "source_supplement")
    assert row["collapsed"] and row["priority"] == 10
    saved = thesis(refs=[row["evidence_ref"]])
    assert saved["evidence_refs"][0]["revision_id"] == row["evidence_ref"]["revision_id"]


def test_newly_added_stock_starts_with_overview_not_old_news_alerts():
    article("other", symbol="000001.SZ")
    svc.refresh(UID, now=NOW)
    assert not changes()
    watchlist(["600519.SS", "000001.SZ"])
    svc.refresh(UID, now=NOW + 1)
    assert len(changes()) == 1 and changes()[0]["overview"]
    assert changes()[0]["symbol"] == "000001.SZ"


def test_removed_then_readded_stock_receives_bounded_missed_overview():
    svc.refresh(UID, now=NOW)
    watchlist([])
    svc.refresh(UID, now=NOW + 1)
    article(observed=NOW + 2)
    svc.refresh(UID, now=NOW + 3)
    assert not changes()
    watchlist(["600519.SS"])
    svc.refresh(UID, now=NOW + 4)
    assert len(changes()) == 1 and changes()[0]["overview"]


def test_parallel_refresh_never_duplicates_or_loses_committed_revisions():
    svc.refresh(UID, now=NOW)
    article(observed=NOW + 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda _: svc.refresh(UID, now=NOW + 2), range(2)))
    assert all(row["status"] in {"completed", "partial"} for row in outputs)
    assert len(changes()) == 1
    assert store.get_scan_state(UID, "news")["cursor"] == store.get_scan_state(UID, "news")["high_watermark"]


def test_same_content_republication_uses_canonical_event():
    first = article()
    svc.refresh(UID, now=NOW)
    with news_store._connect() as conn:
        original = dict(conn.execute("SELECT * FROM articles WHERE id=?", (first,)).fetchone())
    copy = {"title": original["title"], "content": original["content"], "summary": original["summary"],
            "source": "second", "url": "https://example.test/republication", "related_codes": ["600519.SS"],
            "published_ts": original["published_ts"], "fetched_at": NOW + 10}
    news_store.upsert_many([copy])
    svc.refresh(UID, now=NOW + 20)
    assert {row["evidence_ref"]["id"] for row in changes()} == {first}
    assert not any(row["change_type"] == "news_new" for row in changes())


def test_correction_removing_qualified_relationship_still_notifies_with_stale_analysis_label():
    svc.refresh(UID, now=NOW)
    aid = article("supplier", symbol="000001.SZ", observed=NOW + 1)
    with news_store._connect() as conn:
        snapshot = json.loads(conn.execute("SELECT snapshot_json FROM article_content_revisions WHERE article_id=?", (aid,)).fetchone()[0])
    news_store.save_analysis(aid, {"related_stocks": [{"stock_code": "600519.SS", "status": "analyzed",
        "relevance_score": 95, "information_value": True, "relevance_type": "direct",
        "input_content_hash": svc.analysis_content_hash(svc.analysis_input_snapshot(snapshot))}]})
    svc.refresh(UID, now=NOW + 2)
    assert len(changes()) == 1 and changes()[0]["analysis"]["content_changed"] is False
    article("supplier", symbol="000001.SZ", text="更正稿移除了原先的供应信息", observed=NOW + 3, published=NOW + 1)
    svc.refresh(UID, now=NOW + 4)
    assert len(changes()) == 2
    correction = next(row for row in changes() if row["change_type"] == "news_correction")
    assert correction["analysis"]["content_changed"] is True
    assert correction["assessment"] == "unconfirmed"


def test_batches_freeze_watermark_and_retry_after_atomic_insert_failure():
    svc.refresh(UID, now=NOW)
    for n in range(3):
        article(f"different business update {n}", text=f"不同公告资料 {n}" + str(n) * 200, observed=NOW + n)
    first = svc.refresh(UID, now=NOW + 10, limit=1)
    assert first["status"] == "partial" and first["news"]["pending"]
    high = first["news"]["high_watermark"]
    article("new incoming release", text="另外事件新资料" * 60, observed=NOW + 11)
    second = svc.refresh(UID, now=NOW + 20, limit=1)
    assert second["news"]["high_watermark"] == high
    cursor = second["news"]["cursor"]
    with store.connection() as conn:
        conn.execute("""CREATE TRIGGER block_news BEFORE INSERT ON tracking_changes WHEN NEW.kind='news'
            BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
    failure = svc.refresh(UID, now=NOW + 30, limit=1)
    assert failure["errors"] == ["news"]
    assert store.get_scan_state(UID, "news")["cursor"] == cursor
    with store.connection() as conn:
        conn.execute("DROP TRIGGER block_news")
    final = svc.refresh(UID, now=NOW + 40, limit=1)
    assert final["news"]["cursor"] == high
    assert final["news"]["pending"] and final["status"] == "partial"
    svc.refresh(UID, now=NOW + 50, limit=1)
    assert len(changes()) == 4


def test_explicit_calendar_link_only_and_reschedule_is_not_release():
    existing = event()
    unlinked = thesis()
    linked = thesis(refs=[{"kind": "calendar", "id": existing["id"]}])
    foreign = thesis(refs=[{"kind": "calendar", "id": existing["id"]}], uid=UID + 1)
    svc.refresh(UID, now=NOW)
    event(scheduled=NOW + 2 * 86400, observed=NOW + 10)
    svc.refresh(UID, now=NOW + 20)
    row = next(r for r in changes("calendar") if not r["overview"])
    assert row["thesis_ids"] == [linked["id"]]
    assert unlinked["id"] not in row["thesis_ids"] and foreign["id"] not in row["thesis_ids"]
    assert row["change_type"] == "calendar_schedule" and "尚未取得公布值" in row["summary"]
    assert row["assessment"] == "unconfirmed"


@pytest.mark.parametrize("all_day,expected,excluded", [
    (False, "2026-10-31 04:15（北京时间）", "2026-10-30 20:15"),
    (True, "2026-10-30（来源日期，全天／准确时间未定；America/New_York）", "04:15"),
])
def test_calendar_display_crosses_utc_midnight_without_changing_source_snapshot(all_day, expected, excluded):
    raw = {"source_id": "fixture", "upstream_id": "overnight", "title": "跨日发布安排",
           "source_url": "https://example.test/calendar/overnight", "scheduled_at": "2026-10-30T20:15:00+00:00",
           "timezone": "America/New_York", "all_day": all_day, "region": "US", "category": "growth"}
    calendar.ingest_events([raw], observed_at=NOW)
    saved_event = calendar.visible_events(as_of=NOW)[0]
    note = thesis(refs=[{"kind": "calendar", "id": saved_event["id"]}])
    svc.refresh(UID, now=NOW)
    row = changes("calendar")[0]
    for rendered in (row["summary"], row["evidence_ref"]["excerpt"], note["evidence_refs"][0]["excerpt"]):
        assert expected in rendered
        assert excluded not in rendered
        assert raw["scheduled_at"] not in rendered
    assert row["evidence_ref"]["snapshot"]["scheduled_at"] == raw["scheduled_at"]
    assert note["evidence_refs"][0]["snapshot"]["scheduled_date"] == "2026-10-30"


def test_active_thesis_is_tracked_after_removing_watchlist_and_due_is_resolved():
    note = thesis(review_at="2026-10-29")
    watchlist([])
    svc.refresh(UID, now=NOW)
    assert len(changes("review_due")) == 1
    article(observed=NOW + 1)
    svc.refresh(UID, now=NOW + 2)
    assert changes()[0]["thesis_ids"] == [note["id"]]
    store.update_thesis(UID, note["id"], {"revision": note["revision"], "review_at": "2026-11-29"})
    svc.refresh(UID, now=NOW + 3)
    assert not changes("review_due")
    assert changes("review_due", include_resolved=True)[0]["resolved"]


def test_missing_failed_and_stale_sources_never_mean_no_change(monkeypatch):
    monkeypatch.setattr(svc, "_news_collection_health", lambda uid: None)
    result = svc.refresh(UID, now=NOW)
    assert result["coverage"]["status"] == "limited"
    assert result["news"]["status"] != "current"
    first = len(changes("data_issue"))
    svc.refresh(UID, now=NOW + 1)
    assert len(changes("data_issue")) == first
    monkeypatch.setattr(svc, "_news_collection_health", lambda uid: {"status": "failed", "finished_at": NOW})
    result = svc.refresh(UID, now=NOW + 2)
    assert result["news"]["status"] == "failed"
    monkeypatch.setattr(calendar, "source_states", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("unavailable")))
    assert svc.status(UID, now=NOW + 3)["calendar"]["status"] == "failed"


def test_manual_scan_works_with_disabled_worker_and_no_model_budget(monkeypatch):
    monkeypatch.setattr(settings, "enable_research_worker", False)
    research_store.update_config(UID, {"enabled": False, "paused": True})
    article()
    result = svc.refresh(UID, now=NOW)
    assert result["status"] == "completed" and not result["automatic_enabled"]
    assert changes()
    assert research_store.budget_status(UID)["used"]["calls"] == 0


def test_close_scheduler_restarts_priority_pause_and_continuation(monkeypatch):
    monkeypatch.setattr(settings, "enable_research_worker", True)
    research_store.update_config(UID, {"paused": True})
    morning = datetime(2026, 10, 29, 9, tzinfo=SHANGHAI_TZ)
    first = local_autopilot.reconcile(UID, now=morning, startup=True)
    job = research_store.claim_job("worker", user_id=UID)
    assert job["kind"] == "watch_review" and "2026-10-28" in job["idempotency_key"]
    research_store.complete_job(UID, job["id"], job["lease_token"], {"status": "partial", "errors": []})
    again = local_autopilot.reconcile(UID, now=morning)
    assert again["jobs"]["watch_review"]["id"] != first["jobs"]["watch_review"]["id"]
    continued = research_store.claim_job("worker", user_id=UID, kinds=["watch_review"])
    research_store.complete_job(UID, continued["id"], continued["lease_token"], {"status": "completed"})
    assert local_autopilot.reconcile(UID, now=morning)["jobs"]["watch_review"]["id"] == continued["id"]
    evening = local_autopilot.reconcile(UID, now=morning.replace(hour=17))
    assert evening["jobs"]["watch_review"]["id"] != continued["id"]
    research_store.update_config(UID, {"enabled": False})
    assert research_store.claim_job("worker", user_id=UID) is None


@pytest.mark.anyio
async def test_worker_watch_review_dispatch_does_not_import_research_dependencies(monkeypatch):
    from app import research_worker
    monkeypatch.setattr(svc, "refresh", lambda uid: {"status": "completed", "user_id": uid})
    assert await research_worker.dispatch({"user_id": UID, "kind": "watch_review", "payload": {}}) == {"status": "completed", "user_id": UID}


@pytest.fixture
def anyio_backend():
    return "asyncio"
