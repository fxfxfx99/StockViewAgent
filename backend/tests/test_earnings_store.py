from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time

import pytest

from app.config import settings
from app.storage import earnings_store as store


def filing(**extra):
    return {"identity_key": "cninfo:600519.SS:123", "symbol": "600519.SS", "title": "2025年年度报告",
            "report_period": "2025-12-31", "report_year": 2025, "report_type": "annual", "document_kind": "full_report",
            "source": "cninfo", "source_id": "123", "announcement_date": "2026-04-17", "published_at": 1776384000,
            "announcement_url": "https://www.cninfo.com.cn/new/disclosure/detail?announcementId=123",
            "pdf_url": "https://static.cninfo.com.cn/finalpage/2026-04-17/123.PDF", **extra}


def seed(**extra):
    store.save_discovery("600519.SS", {"items": [filing(**extra)], "sources": [{"source": "cninfo", "status": "ok"}]})
    return store.list_reports("600519.SS")[0]


def running(uid=1, kind="analyze", **kwargs):
    report = seed()
    job = store.enqueue(uid, report["symbol"], kind, report_id=report["id"], **kwargs)
    return report, store.claim("worker")


def test_discovery_versions_are_preserved_and_failure_does_not_erase():
    first = seed()
    seed()
    assert len(store.list_reports("600519.SS")) == 1
    seed(title="2025年年度报告（修订版）", is_revision=True)
    assert len(store.list_reports("600519.SS")) == 2
    store.save_discovery("600519.SS", {"items": [], "sources": [{"source": "cninfo", "status": "error"}], "partial": True})
    assert store.get_report(first["id"])["title"] == "2025年年度报告"
    assert store.source_status("600519.SS")["partial"] is True
    assert store.list_reports("000001.SZ") == []


def test_jobs_and_interpretations_are_account_owned_and_cache_is_immutable():
    report, job = running()
    assert store.get_job(2, job["id"]) is None
    assert store.list_jobs(2) == []
    store.reserve_call(1, job["id"], "worker")
    store.save_document(report["id"], "a" * 64, {"status": "ready"})
    result = {"model": "fixture", "report": {"summary": "本地测试"}}
    store.finish(1, job["id"], "worker", analysis={"document_sha": "a" * 64, "cache_key": "cache-one", "result": result})
    assert store.cached_analysis(2, report["id"], "cache-one") is None
    assert store.list_analyses(2, report["id"]) == []
    assert store.cached_analysis(1, report["id"], "cache-one")["result"] == result
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection() as db:
            db.execute("DELETE FROM earnings_analyses")


def test_duplicate_jobs_merge_but_different_focus_is_not_swallowed():
    report = seed()
    one = store.enqueue(1, report["symbol"], "analyze", report_id=report["id"], focus="现金流")
    assert store.enqueue(1, report["symbol"], "analyze", report_id=report["id"], focus="现金流")["id"] == one["id"]
    with pytest.raises(store.EarningsConflict):
        store.enqueue(1, report["symbol"], "analyze", report_id=report["id"], focus="收入")
    two = store.enqueue(2, report["symbol"], "analyze", report_id=report["id"], focus="收入")
    assert two["id"] != one["id"]


def test_refresh_cooldown_and_wrong_report_symbol():
    report = seed()
    job = store.enqueue(1, report["symbol"], "refresh")
    assert store.enqueue(1, report["symbol"], "refresh")["id"] == job["id"]
    store.claim("worker")
    store.finish(1, job["id"], "worker", result={})
    with pytest.raises(store.EarningsConflict):
        store.enqueue(2, report["symbol"], "refresh")
    with pytest.raises(ValueError):
        store.enqueue(1, "000001.SZ", "prepare", report_id=report["id"])


def test_daily_budget_is_atomic_and_one_job_is_never_dispatched_twice(monkeypatch):
    monkeypatch.setattr(settings, "earnings_daily_call_limit", 1)
    report, one = running()
    store.enqueue(1, report["symbol"], "prepare", report_id=report["id"])
    # Two concurrent reservations of the same paid call cannot both pass.
    def reserve():
        try:
            store.reserve_call(1, one["id"], "worker")
            return "accepted"
        except store.EarningsConflict:
            return "duplicate"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: reserve(), range(2))) == ["accepted", "duplicate"]
    assert store.usage(1)["used"] == 1 and store.usage(2)["used"] == 0
    store.finish(1, one["id"], "worker", error="模拟失败仍计入额度")
    store.enqueue(1, report["symbol"], "analyze", report_id=report["id"])
    prepare = store.claim("worker")
    store.finish(1, prepare["id"], "worker", result={})
    paid = store.claim("worker")
    with pytest.raises(store.DailyLimit):
        store.reserve_call(1, paid["id"], "worker")


def test_recovery_only_requeues_work_not_yet_dispatched():
    report, job = running()
    store.recover_interrupted()
    assert store.get_job(1, job["id"])["status"] == "queued"
    claimed = store.claim("new-worker")
    with pytest.raises(store.LeaseLost):
        store.finish(1, job["id"], "worker", result={})
    store.reserve_call(1, claimed["id"], "new-worker")
    store.recover_interrupted()
    saved = store.get_job(1, job["id"])
    assert saved["status"] == "failed" and "结果未知" in saved["error"]
    assert store.claim("third-worker") is None
    assert store.usage(1)["used"] == 1


def test_expired_lease_cannot_reserve_or_publish():
    _, job = running()
    with store.connection() as db:
        db.execute("UPDATE earnings_jobs SET lease_until=? WHERE id=?", (time.time() - 1, job["id"]))
    with pytest.raises(store.LeaseLost):
        store.reserve_call(1, job["id"], "worker")
    with pytest.raises(store.LeaseLost):
        store.finish(1, job["id"], "worker", result={})


def test_parser_metadata_can_recover_without_overwriting_public_filing():
    report = seed()
    store.save_document(report["id"], "a" * 64, {"status": "timeout", "pages": []})
    store.save_document(report["id"], "a" * 64, {"status": "ready", "text_pages": 10, "pages": [{"text": "private huge text"}]})
    document = store.get_document("a" * 64)
    assert document["status"] == "ready" and "pages" not in document
    assert store.get_report(report["id"])["title"] == report["title"]


def test_identical_pdf_from_another_announcement_reuses_personal_analysis():
    report, job = running()
    sha = "b" * 64
    store.save_document(report["id"], sha, {"status": "ready"})
    result = {"model": "fixture", "report": {"summary": "仅测试"}}
    store.finish(1, job["id"], "worker", analysis={"document_sha": sha, "cache_key": "same-content", "result": result})
    store.save_discovery("600519.SS", {"items": [filing(identity_key="eastmoney:other", source="eastmoney")]})
    other = next(row for row in store.list_reports("600519.SS") if row["source"] == "eastmoney")
    store.save_document(other["id"], sha, {"status": "ready"})
    cached = store.cached_analysis(1, other["id"], "same-content")
    assert cached["report_id"] == report["id"]
    store.enqueue(1, other["symbol"], "analyze", report_id=other["id"])
    second = store.claim("worker")
    store.finish(1, second["id"], "worker", result={"analysis_id": cached["id"], "reused": True})
    assert store.list_analyses(1, other["id"])[0]["id"] == cached["id"]
    assert store.list_analyses(2, other["id"]) == []
