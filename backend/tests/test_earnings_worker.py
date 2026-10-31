import asyncio
import hashlib

import pytest

from app.config import settings
from app.security.user_context import current_user_id
from app.services import earnings as service, earnings_analysis as engine, earnings_sources as sources
from app.storage import earnings_store as store, users_store


@pytest.fixture
def public_report(monkeypatch):
    monkeypatch.setattr(settings, "enable_earnings", True)
    users_store.init_db()
    row = {"identity_key": "fixture:annual", "symbol": "600519.SS", "title": "测试年度报告", "report_period": "2025-12-31",
           "published_at": 1776384000, "announcement_date": "2026-04-17", "document_kind": "full_report"}
    store.save_discovery(row["symbol"], {"items": [row]})
    return store.list_reports(row["symbol"])[0]


def fake_document(monkeypatch, *, status="ready"):
    pdf = b"%PDF-1.7\nexplicit unit test fixture"
    sha = hashlib.sha256(pdf).hexdigest()
    calls = []
    async def download(report):
        calls.append(report["id"])
        return {"content": pdf, "sha256": sha, "url": "https://static.cninfo.com.cn/finalpage/2026-04-17/test.PDF"}
    def extract(path):
        assert path.read_bytes() == pdf
        return {"sha256": sha, "status": status, "total_pages": 1, "text_pages": 1 if status == "ready" else 0,
                "pages": [{"page": 1, "status": "text", "text": "测试财报：营业收入100元"}], "coverage_ratio": 1, "warnings": []}
    monkeypatch.setattr(sources, "download_report_pdf", download)
    monkeypatch.setattr(engine, "extract_pdf", extract)
    monkeypatch.setattr(engine, "analysis_cache_key", lambda uid, digest, focus: f"{uid}:{digest}:{focus}")
    return calls


def test_prepare_persists_original_and_analysis_reuses_without_more_paid_calls(public_report, monkeypatch):
    downloads = fake_document(monkeypatch)
    report = public_report
    calls = []
    async def analyze(uid, document, extraction, *, focus, before_dispatch):
        assert current_user_id.get() == uid
        before_dispatch()
        calls.append(uid)
        return {"model": "test", "report": {"summary": "模拟解读"}, "model_called": True}
    monkeypatch.setattr(engine, "analyze_document", analyze)
    store.enqueue(1, report["symbol"], "prepare", report_id=report["id"])
    assert asyncio.run(service.run_cycle()) == 1
    assert len(downloads) == 1 and store.usage(1)["used"] == 0
    assert service.pdf_path(report["id"]).is_file()
    for _ in range(2):
        store.enqueue(1, report["symbol"], "analyze", report_id=report["id"], focus="利润质量")
        assert asyncio.run(service.run_cycle()) == 1
    assert calls == [1] and store.usage(1)["used"] == 1 and len(downloads) == 1
    assert store.list_jobs(1)[0]["result"]["reused"] is True
    assert len(store.list_analyses(1, report["id"])) == 1
    assert service.report_detail(1, report["id"])["report"]["pdf_url"].startswith("https://static.cninfo.com.cn/")
    assert "pages" not in service.report_detail(1, report["id"])["document"]
    assert current_user_id.get() is None


def test_no_text_keeps_download_but_never_dispatches_a_model(public_report, monkeypatch):
    fake_document(monkeypatch, status="no_text")
    monkeypatch.setattr(engine, "analyze_document", lambda *a, **kw: pytest.fail("no text must not call model"))
    store.enqueue(1, public_report["symbol"], "analyze", report_id=public_report["id"])
    asyncio.run(service.run_cycle())
    job = store.list_jobs(1)[0]
    assert job["status"] == "failed" and "扫描" in job["error"]
    assert store.usage(1)["used"] == 0
    assert service.pdf_path(public_report["id"]).is_file()


def test_source_failure_and_unexpected_errors_keep_history_without_secrets(public_report, monkeypatch):
    async def discover(symbol):
        return {"items": [], "sources": [{"source": "cninfo", "status": "error", "error": "暂不可用"}], "partial": True}
    monkeypatch.setattr(sources, "discover_reports", discover)
    store.enqueue(1, public_report["symbol"], "refresh")
    asyncio.run(service.run_cycle())
    assert store.list_jobs(1)[0]["status"] == "failed"
    assert len(store.list_reports(public_report["symbol"])) == 1
    async def download(report):
        raise RuntimeError("secret-key-never-publish")
    monkeypatch.setattr(sources, "download_report_pdf", download)
    store.enqueue(1, public_report["symbol"], "prepare", report_id=public_report["id"])
    asyncio.run(service.run_cycle())
    assert "secret-key" not in str(store.list_jobs(1))


def test_worker_mutex_and_shutdown_keep_paid_task_from_repeating(public_report, monkeypatch):
    fake_document(monkeypatch)
    store.enqueue(1, public_report["symbol"], "analyze", report_id=public_report["id"])
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        async def analyze(uid, document, extraction, *, focus, before_dispatch):
            before_dispatch()
            started.set()
            await release.wait()
            return {"model": "fixture", "report": {}}
        monkeypatch.setattr(engine, "analyze_document", analyze)
        task = asyncio.create_task(service.run_cycle())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert await service.run_cycle() == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    assert asyncio.run(service.run_cycle()) == 0
    assert store.usage(1)["used"] == 1
    assert store.list_jobs(1)[0]["status"] == "failed"
    assert "结果未知" in store.list_jobs(1)[0]["error"]


def test_invalid_cached_file_is_refetched(public_report, monkeypatch):
    downloads = fake_document(monkeypatch)
    report = public_report
    store.enqueue(1, report["symbol"], "prepare", report_id=report["id"])
    asyncio.run(service.run_cycle())
    service.pdf_path(report["id"]).write_bytes(b"corrupted")
    store.enqueue(1, report["symbol"], "prepare", report_id=report["id"])
    asyncio.run(service.run_cycle())
    assert len(downloads) == 2
    assert service.pdf_path(report["id"]).read_bytes().startswith(b"%PDF-")


def test_disabled_worker_leaves_requests_queued(public_report, monkeypatch):
    store.enqueue(1, public_report["symbol"], "prepare", report_id=public_report["id"])
    monkeypatch.setattr(settings, "enable_earnings", False)
    assert asyncio.run(service.run_cycle()) == 0
    assert store.list_jobs(1)[0]["status"] == "queued"
