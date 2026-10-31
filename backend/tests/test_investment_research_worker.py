import asyncio
import hashlib
import json

import httpx
import pytest

from app.config import settings
from app.security.user_context import current_user_id
from app.services import earnings_analysis, investment_research as service, investment_research_engine as engine
from app.storage import investment_research_store as store, user_credentials_store, users_store


@pytest.fixture
def dossier(monkeypatch):
    monkeypatch.setattr(settings, "enable_investment_research", True)
    monkeypatch.setattr(settings, "auth_required", False)
    users_store.init_db()
    user_credentials_store.merge_user_credentials(1, {"llm": {"api_key": "fake-research-key",
        "api_base": "https://api.openai.com/v1", "model": "test-model"}})
    value = store.create_dossier(1, {"title": "现金流研究", "target_type": "stock", "target_code": "600519.SS", "objective": "现金流能否持续", "thesis": "我的待验证判断"})
    service.add_material(1, value["id"], {"kind": "note", "title": "访谈笔记", "text": "用户认为回款将改善，需要公开财报核实。"})
    return store.get_dossier(1, value["id"])


def mock_http(monkeypatch, *, before_response=None, code=200):
    original, requests = httpx.AsyncClient, []

    async def response(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fake-research-key"
        assert current_user_id.get() == 1
        body = json.loads(request.content)
        context = json.loads(body["messages"][-1]["content"])
        requests.append(context)
        if before_response:
            await before_response()
        if code != 200:
            return httpx.Response(code, json={"error": "fake provider failure"})
        ids = [row["id"] for row in context["evidence"]]
        report = {"summary": "用户判断尚待公开资料验证", "assessment": "insufficient_data",
            "findings": [{"kind": "interpretation", "text": "用户笔记提出回款改善假说，尚未核实。", "evidence_ids": ids[:1]}],
            "judgment_changes": [], "open_questions": [], "next_observations": [{"title": "核对现金流", "goal": "观察回款资料",
            "watch_conditions": "出现新的现金流数据", "invalidation_conditions": "回款明显恶化", "evidence_ids": ids[:1]}],
            "risks": ["用户材料不是独立核实事实"], "limitations": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(report, ensure_ascii=False)}}]})

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(response), **kwargs)
    monkeypatch.setattr(engine.httpx, "AsyncClient", client)
    return requests


def test_real_worker_single_request_cache_and_explicit_adoption(dossier, monkeypatch):
    requests = mock_http(monkeypatch)
    job = store.enqueue(1, dossier["id"])
    assert asyncio.run(service.run_cycle())["processed"] == 1
    saved = store.get_job(1, job["id"])
    assert saved["status"] == "succeeded", saved["error_code"]
    report_id = saved["result"]["report_id"]
    assert len(requests) == store.usage(1)["used"] == 1
    assert store.get_dossier(1, dossier["id"])["adopted_report_id"] is None
    second = store.enqueue(1, dossier["id"])
    asyncio.run(service.run_cycle())
    reused = store.get_job(1, second["id"])
    assert reused["status"] == "succeeded", reused["error_code"]
    assert reused["result"] == {"report_id": report_id, "reused": True}
    assert len(requests) == store.usage(1)["used"] == 1
    assert len(store.list_reports(1, dossier["id"])) == 1
    adopted = store.adopt_report(1, report_id, dossier["revision"])
    assert adopted["adopted_report_id"] == report_id
    assert adopted["thesis"] == "我的待验证判断"
    assert current_user_id.get() is None


def test_provider_failure_has_one_dispatch_and_no_automatic_retry(dossier, monkeypatch):
    requests = mock_http(monkeypatch, code=401)
    job = store.enqueue(1, dossier["id"])
    asyncio.run(service.run_cycle())
    assert store.get_job(1, job["id"])["error_code"] == "authentication_failed"
    assert len(requests) == store.usage(1)["used"] == 1
    assert asyncio.run(service.run_cycle())["processed"] == 0
    assert len(requests) == 1


def test_edit_during_paid_call_keeps_old_report_as_history(dossier, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
        requests = mock_http(monkeypatch, before_response=wait)
        job = store.enqueue(1, dossier["id"])
        task = asyncio.create_task(service.run_cycle())
        await entered.wait()
        latest = store.update_dossier(1, dossier["id"], {"revision": dossier["revision"], "thesis": "人工更新后的判断"})
        with pytest.raises(store.ResearchConflict):
            store.cancel_job(1, job["id"])
        release.set()
        await task
        saved = store.get_job(1, job["id"])
        assert saved["status"] == "succeeded"
        report = store.get_report(1, saved["result"]["report_id"])
        assert report["result"]["dossier_snapshot"]["revision"] == dossier["revision"]
        assert store.get_dossier(1, dossier["id"])["thesis"] == latest["thesis"]
        assert store.get_dossier(1, dossier["id"])["adopted_report_id"] is None
        assert len(requests) == store.usage(1)["used"] == 1
    asyncio.run(exercise())


def test_shutdown_drains_paid_operation_and_recovery_marks_unknown(dossier, monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
        requests = mock_http(monkeypatch, before_response=wait)
        job = store.enqueue(1, dossier["id"])
        task = asyncio.create_task(service.run_cycle())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert (await service.run_cycle())["status"] == "another_worker"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(requests) == 1
        store.recover_interrupted()
        assert store.get_job(1, job["id"])["error_code"] == "interrupted_after_dispatch"
        assert (await service.run_cycle())["processed"] == 0
    asyncio.run(exercise())


def test_pdf_parser_job_is_account_owned_and_never_uses_model(dossier, monkeypatch):
    def extract(path):
        content = path.read_bytes()
        return {"sha256": hashlib.sha256(content).hexdigest(), "status": "partial", "pages": [
            {"page": 1, "text": "封面"}, {"page": 2, "text": "", "status": "scan_suspected"}, {"page": 3, "text": "现金流表"}],
            "total_pages": 3, "text_pages": 2, "coverage_ratio": 2/3, "warnings": ["扫描页未OCR"]}
    monkeypatch.setattr(earnings_analysis, "extract_pdf", extract)
    material = service.upload_material(1, dossier["id"], "report.pdf", b"%PDF-1.4\nfixture")
    assert asyncio.run(service.run_cycle())["processed"] == 1
    saved = store.get_material(1, material["id"])
    assert saved["status"] == "partial" and saved["pages"][2]["page"] == 3
    assert store.get_material(2, material["id"]) is None
    assert store.usage(1)["used"] == 0


def test_disable_lock_and_deleted_account_do_not_dispatch(dossier, monkeypatch):
    job = store.enqueue(1, dossier["id"])
    with service.worker_lock() as held:
        assert held and asyncio.run(service.run_cycle())["status"] == "another_worker"
    monkeypatch.setattr(settings, "enable_investment_research", False)
    assert asyncio.run(service.run_cycle())["status"] == "disabled"
    assert store.get_job(1, job["id"])["status"] == "queued"
    monkeypatch.setattr(settings, "enable_investment_research", True)
    monkeypatch.setattr(users_store, "get_by_id", lambda uid: None)
    asyncio.run(service.run_cycle())
    assert store.get_job(1, job["id"])["status"] == "failed"
    assert store.usage(1)["used"] == 0
