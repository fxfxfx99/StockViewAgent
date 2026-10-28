"""Durable note edits, evidence provenance and scan transactions use real SQLite."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import sqlite3

import pytest

from app.schemas import tracking
from app.services import tracking_evidence
from app.storage import decision_signal_store, event_calendar_store, news_store, tracking_store as store


def thesis(**changes):
    return {"symbol": "600519.SH", "reason": "未来两个季度利润率改善", "watch_conditions": "季报利润率",
            "invalidation_conditions": "连续两个季度下降", **changes}


def future(days=60):
    return (tracking.today() + timedelta(days=days)).isoformat()


def change(identity="news:1", **updates):
    return {"id": identity, "kind": "news", "symbol": "600519.SS", "title": "正文修订", "summary": "原文内容有变更",
            "thesis_ids": [], "evidence_refs": [], "priority": 80, **updates}


def test_default_review_date_account_isolation_and_immutable_history():
    saved = store.create_thesis(1, thesis())
    assert saved["symbol"] == "600519.SS"
    assert saved["review_at"] == future(30)
    assert saved["status"] == "active" and saved["revision"] == 1
    assert store.get_thesis(2, saved["id"]) is None
    assert store.list_theses(2) == []
    assert store.update_thesis(2, saved["id"], {"revision": 1, "reason": "不能修改"}) is None
    updated = store.update_thesis(1, saved["id"], {"revision": 1, "reason": "缩短观察窗口"})
    assert updated["revision"] == 2
    with pytest.raises(store.RevisionConflict):
        store.update_thesis(1, saved["id"], {"revision": 1, "reason": "过期编辑"})
    history = store.list_timeline(1, saved["id"])
    assert [row["kind"] for row in history] == ["created", "revised"]
    assert history[0]["payload"]["thesis"]["reason"] == saved["reason"]
    assert store.list_timeline(2, saved["id"]) == []
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connection() as conn:
            conn.execute("UPDATE thesis_events SET payload='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connection() as conn:
            conn.execute("DELETE FROM thesis_events")


def test_simultaneous_edit_has_one_winner_without_lost_history():
    saved = store.create_thesis(1, thesis())

    def edit(reason):
        try:
            return store.update_thesis(1, saved["id"], {"revision": 1, "reason": reason})
        except store.RevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["观点甲", "观点乙"]))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_timeline(1, saved["id"])) == 2


def test_review_changes_are_atomic_and_resolve_prior_reminders():
    saved = store.create_thesis(1, thesis(review_at=tracking.today().isoformat()))
    due = change("due:1", kind="review_due", thesis_ids=[saved["id"]], thesis_revision=1,
                 review_at=saved["review_at"])
    store.put_changes(1, [due])
    revised = store.review_thesis(1, saved["id"], {"revision": 1, "action": "revise", "reason": "观察到反证，修改条件",
        "evidence_assessment": "contradicts", "review_at": future(), "changes": {"reason": "等待利润率止跌"}})
    assert revised["review_at"] == future() and revised["reason"] == "等待利润率止跌"
    assert not store.list_changes(1)
    assert store.get_change(1, "due:1")["resolved"] is True
    # A scan that took its thesis snapshot before the review cannot resurrect it.
    assert store.put_changes(1, [{**due, "id": "stale-scan"}]) == 0
    assert store.list_timeline(1, saved["id"])[-1]["payload"]["review"]["evidence_assessment"] == "contradicts"
    closed = store.review_thesis(1, saved["id"], {"revision": 2, "action": "close", "reason": "结束关注",
        "evidence_assessment": "uncertain"})
    assert closed["status"] == "closed"
    with pytest.raises(ValueError, match="重新开启"):
        store.review_thesis(1, saved["id"], {"revision": 3, "action": "continue", "reason": "继续", "evidence_assessment": "uncertain"})


def test_signal_reference_is_user_and_symbol_scoped():
    signal = decision_signal_store.create(7, {"symbol": "600519.SS"})
    assert store.create_thesis(7, thesis(signal_id=signal["id"]))["signal_id"] == signal["id"]
    with pytest.raises(ValueError, match="当前账户"):
        store.create_thesis(8, thesis(signal_id=signal["id"]))
    with pytest.raises(ValueError, match="同一标的"):
        store.create_thesis(7, thesis(symbol="000001.SZ", signal_id=signal["id"]))
    assert not store.list_theses(8)


def add_news_revision(identity, content, timestamp):
    news_store.init_db()
    with news_store._connect() as conn:
        cursor = conn.execute("""INSERT INTO article_content_revisions
          (article_id,content_hash,snapshot_json,published_ts,first_observed_at,recorded_at,origin)
          VALUES(?,?,?,?,?,?,?)""", (identity, content, json.dumps({"title": content, "content": content,
          "source": "测试来源", "url": "https://example.org/news"}), timestamp - 100, timestamp, timestamp, "archive_write"))
        return str(cursor.lastrowid)


def test_saved_evidence_uses_requested_archive_revision_and_ignores_claimed_content():
    first = add_news_revision("article", "旧正文", 1700000000)
    add_news_revision("article", "新正文", 1700001000)
    saved = store.create_thesis(1, thesis(evidence_refs=[{"kind": "news", "id": "article", "revision_id": first,
        "title": "伪造标题", "snapshot": {"content": "不可信输入"}}]))
    ref = saved["evidence_refs"][0]
    assert ref["title"] == "旧正文" and ref["revision_id"] == first
    assert ref["snapshot"]["content"] == "旧正文"
    assert "不可信输入" not in json.dumps(saved, ensure_ascii=False)
    with pytest.raises(ValueError, match="不存在"):
        store.create_thesis(1, thesis(evidence_refs=[{"kind": "news", "id": "missing"}]))
    with pytest.raises(ValueError, match="不存在"):
        store.create_thesis(1, thesis(evidence_refs=[{"kind": "news", "id": "article", "revision_id": "unknown"}]))
    # A content hash can recur after a correction is reverted. It is not a
    # unique observation/version identity and cannot substitute for row id.
    with pytest.raises(ValueError, match="不存在"):
        store.create_thesis(1, thesis(evidence_refs=[{"kind": "news", "id": "article", "revision_id": "旧正文"}]))


def test_calendar_freezes_original_schedule_and_strategy_is_only_opinion():
    event = {"source_id": "test", "upstream_id": "meeting", "title": "政策会议", "source_url": "https://example.org/calendar",
             "scheduled_at": "2026-01-01T12:00:00+00:00", "timezone": "UTC"}
    event_calendar_store.ingest_events([event], observed_at="2025-12-01T00:00:00+00:00")
    old = event_calendar_store.visible_events(as_of="2025-12-01T00:00:00+00:00")[0]
    event_calendar_store.ingest_events([{**event, "scheduled_at": "2026-01-02T12:00:00+00:00"}], observed_at="2025-12-02T00:00:00+00:00")
    refs = tracking_evidence.freeze_evidence_refs(1, [
        {"kind": "calendar", "id": old["id"], "revision_id": old["revision_id"]},
        {"kind": "strategy", "id": "view-1", "snapshot": {"independent_source": True, "score": 70}},
    ])
    assert refs[0]["snapshot"]["scheduled_at"] == event["scheduled_at"]
    assert refs[0]["snapshot"]["actual"] is None
    assert refs[1]["snapshot"]["independent_source"] is False
    assert "not_independent" in refs[1]["snapshot"]["evidence_role"]
    for ref in refs:
        tracking.EvidenceRef.model_validate(ref)


@pytest.mark.parametrize("patch", [
    {"review_at": "2026-02-30"}, {"review_at": "2026-1-01"}, {"review_at": 1},
    {"reason": " "}, {"reason": "a" * 4001}, {"evidence_refs": [{"kind": "news", "id": "x", "url": "javascript:alert(1)"}]},
    {"evidence_refs": [{"kind": "strategy", "id": "x", "snapshot": {"body": "x" * 33000}}]},
])
def test_input_limits_and_dates(patch):
    with pytest.raises(ValueError):
        store.create_thesis(1, thesis(**patch))


def test_invalid_patch_and_review_leave_state_unchanged():
    saved = store.create_thesis(1, thesis())
    for patch in ({"reason": "missing revision"}, {"revision": True, "reason": "bad"}, {"revision": 1, "reason": None}, {"revision": 1}):
        with pytest.raises(ValueError):
            store.update_thesis(1, saved["id"], patch)
    with pytest.raises(ValueError, match="晚于今天"):
        store.review_thesis(1, saved["id"], {"revision": 1, "action": "continue", "reason": "继续观察",
            "evidence_assessment": "uncertain", "review_at": tracking.today().isoformat()})
    assert store.get_thesis(1, saved["id"])["revision"] == 1


def test_scan_commit_rolls_back_changes_with_cursor_and_cas_metadata():
    with pytest.raises(ValueError):
        store.commit_scan(1, "news", 0, 2, [change(), change("bad", kind="unknown")], {}, expected_version=0)
    assert store.get_scan_state(1, "news") == {"cursor": 0, "version": 0}
    assert not store.list_changes(1)
    result = store.commit_scan(1, "news", 0, 2, [change()], {"baseline": ["a"]}, expected_version=0)
    assert result["inserted"] == 1 and result["version"] == 1
    with pytest.raises(store.ScanConflict):
        store.commit_scan(1, "news", 2, 2, [], {"baseline": ["stale"]}, expected_version=0)
    assert store.get_scan_state(1, "news")["baseline"] == ["a"]
    assert store.commit_scan(1, "news", 2, 2, [change()], {}, expected_version=1)["inserted"] == 0
    assert store.get_scan_state(2, "news") == {"cursor": 0, "version": 0}


def test_change_feedback_preserves_evidence_and_other_user_cannot_read_or_edit():
    saved = store.create_thesis(1, thesis())
    item = change(thesis_ids=[saved["id"]], evidence_ref={"id": "frozen"}, analysis={"source": "existing"})
    assert store.put_changes(1, [item]) == 1
    result = store.update_change(1, item["id"], {"is_read": True, "feedback": "useful"})
    assert result["is_read"] and result["evidence_ref"] == {"id": "frozen"}
    assert result["analysis"] == {"source": "existing"}
    assert store.update_change(1, item["id"], {"feedback": None})["feedback"] is None
    assert store.get_change(2, item["id"]) is None
    assert store.update_change(2, item["id"], {"is_read": True}) is None
    assert store.list_changes(2) == []
    assert store.list_changes(1, unread_only=True) == []
    with pytest.raises(ValueError, match="当前账户"):
        store.put_changes(2, [item])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.connection() as conn:
            conn.execute("UPDATE tracking_changes SET payload='{}'")


def test_health_recovery_resolves_old_warning_without_rewriting_evidence():
    issue = change("health:1", kind="data_issue")
    store.commit_scan(1, "health", 0, 0, [issue], {"fingerprint": "failed"}, expected_version=0)
    store.commit_scan(1, "health", 0, 0, [], {"fingerprint": "failed"}, expected_version=1)
    assert store.get_change(1, "health:1")["resolved"] is False
    store.commit_scan(1, "health", 0, 0, [], {"fingerprint": "healthy"}, expected_version=2)
    assert store.get_change(1, "health:1")["resolved"] is True
    assert store.get_change(1, "health:1")["title"] == issue["title"]


def test_resaving_an_opinion_preserves_its_observation_time(monkeypatch):
    monkeypatch.setattr(tracking_evidence.time, "time", lambda: 1700000000)
    saved = store.create_thesis(1, thesis(evidence_refs=[{"kind": "strategy", "id": "trend", "snapshot": {"score": 70}}]))
    original = saved["evidence_refs"][0]
    monkeypatch.setattr(tracking_evidence.time, "time", lambda: 1700010000)
    updated = store.update_thesis(1, saved["id"], {"revision": 1, "evidence_refs": saved["evidence_refs"]})
    assert updated["evidence_refs"][0] == original
    reviewed = store.review_thesis(1, saved["id"], {"revision": 2, "action": "revise", "reason": "补充观察条件",
        "evidence_assessment": "uncertain", "changes": {"watch_conditions": "季报", "evidence_refs": updated["evidence_refs"]}})
    assert reviewed["evidence_refs"][0] == original


def test_read_and_irrelevant_high_priority_items_cannot_hide_new_unread_changes():
    store.put_changes(1, [change("old-read", priority=100), change("old-irrelevant", priority=100),
                          change("new-unread", priority=60)])
    store.update_change(1, "old-read", {"is_read": True})
    store.update_change(1, "old-irrelevant", {"feedback": "irrelevant"})
    assert [row["id"] for row in store.list_changes(1, limit=1)] == ["new-unread"]
