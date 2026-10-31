import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest

from app.config import settings
from app.schemas.tracking import today
from app.security.user_context import current_user_id
from app.services import monitoring as service, monitoring_engine as engine
from app.storage import kline_bundle_cache, monitoring_store as store, user_credentials_store, users_store


def create(uid):
    return store.create_plan(uid, {"title": "事件观察", "target_type": "topic", "keywords": ["Fed"],
        "goal": "观察利率决策", "watch_conditions": "宽松", "invalidation_conditions": "通胀上升",
        "review_at": str(today()+timedelta(days=7)), "interval_minutes": 360})


def report(run):
    return {"plan_snapshot": run["plan_snapshot"], "input_hash": "input-1", "model": "test-model", "evidence": [],
            "coverage": ["资料不足"], "report": {"summary": "等待更多资料", "assessment": "insufficient_data", "changes": "尚无新事实",
            "conditions": [], "risks": [], "next_steps": ["继续观察"], "review_summary": "", "evidence_ids": []}}


def test_background_runs_for_each_user_and_restores_context(monkeypatch):
    monkeypatch.setattr(settings, "enable_monitoring", True)
    users_store.init_db()
    one, two = create(1), create(2)
    calls = []
    async def generate(uid, run):
        calls.append((uid, current_user_id.get()))
        store.reserve_call(uid, run_id=run["id"], kind="observe")
        return report(run)
    monkeypatch.setattr(engine, "generate_report", generate)
    token = current_user_id.set(777)
    try:
        result = asyncio.run(service.run_cycle())
        assert result["processed"] == 2
        assert sorted(calls) == [(1, 1), (2, 2)]
        assert current_user_id.get() == 777
        assert len(store.list_runs(1, plan_id=one["id"])) == 1
        assert store.list_runs(2, plan_id=two["id"])[0]["status"] == "succeeded"
        assert store.usage(1)["used"] == store.usage(2)["used"] == 1
        assert asyncio.run(service.run_cycle())["processed"] == 0
    finally:
        current_user_id.reset(token)


def test_lock_and_disable_prevent_duplicate_worker(monkeypatch):
    monkeypatch.setattr(settings, "enable_monitoring", True)
    with service.worker_lock() as held:
        assert held
        assert asyncio.run(service.run_cycle())["status"] == "another_worker"
    monkeypatch.setattr(settings, "enable_monitoring", False)
    assert asyncio.run(service.run_cycle())["status"] == "disabled"


def test_execution_error_is_recorded_without_secret_or_context_leak(monkeypatch):
    monkeypatch.setattr(settings, "enable_monitoring", True)
    users_store.init_db()
    create(1)
    async def fail(uid, run):
        store.reserve_call(uid, run_id=run["id"], kind="observe")
        raise RuntimeError("Bearer should-never-appear")
    monkeypatch.setattr(engine, "generate_report", fail)
    asyncio.run(service.run_cycle())
    saved = store.list_runs(1)[0]
    assert saved["status"] == "failed" and saved["error_code"] == "observation_failed"
    assert "should-never-appear" not in str(saved)
    assert current_user_id.get() is None
    assert store.usage(1)["used"] == 1


def test_shutdown_drains_dispatched_call_before_unlocking(monkeypatch):
    monkeypatch.setattr(settings, "enable_monitoring", True)
    users_store.init_db()
    create(1)
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        async def generate(uid, run):
            store.reserve_call(uid, run_id=run["id"], kind="observe")
            started.set()
            await release.wait()
            return report(run)
        monkeypatch.setattr(engine, "generate_report", generate)
        task = asyncio.create_task(service.run_cycle())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert (await service.run_cycle())["status"] == "another_worker"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    store.recover_interrupted()
    assert store.list_runs(1)[0]["status"] == "failed"
    assert store.usage(1)["used"] == 1


def stock_list_fixture(monkeypatch):
    """Public cached facts and a fake, already verified account in pytest's data dir."""
    monkeypatch.setattr(settings, "enable_monitoring", True)
    monkeypatch.setattr(settings, "auth_required", False)
    users_store.init_db()
    user_credentials_store.merge_user_credentials(1, {"llm": {
        "api_key": "fake-stock-list-worker-key", "api_base": "https://api.openai.com/v1", "model": "test-model"}})
    token = current_user_id.set(1)
    try:
        credentials = engine._credentials(1)
    finally:
        current_user_id.reset(token)
    store.set_model_state(1, {"fingerprint": credentials["fingerprint"], "verified": True,
                              "checked_at": time.time(), "model": credentials["model"]})
    stamp = int(time.time()) - 3600
    for symbol in ("600519.SS", "000001.SZ", "600000.SS"):
        kline_bundle_cache.save_bundle(symbol, "1y", "1d", {
            "kline_source": "local-test-archive", "candles": [
                {"t": stamp - 86400, "o": 10, "h": 12, "l": 9, "c": 11, "v": 100},
                {"t": stamp, "o": 11, "h": 13, "l": 10, "c": 12, "v": 120}]})
    return store.create_plan(1, {"title": "三只股票观察", "target_type": "stock_list", "target_code": "",
        "target_symbols": ["600519.SS", "000001.SZ", "300750.SZ"], "goal": "逐股观察本地行情和数据缺口",
        "review_at": str(today() + timedelta(days=7)), "interval_minutes": 360})


def model_list_report(context):
    """Simulated model output references only each member's actual prompt facts."""
    members = []
    for symbol in context["plan"]["target_symbols"]:
        identifiers = [row["id"] for row in context["evidence"] if row.get("symbol") == symbol and not row["stale"]]
        members.append({"symbol": symbol, "assessment": "watch" if identifiers else "insufficient_data",
                        "summary": "已有本股本地行情，继续观察。" if identifiers else "本股资料不足，尚不能判断。",
                        "evidence_ids": identifiers})
    identifiers = list(dict.fromkeys(identifier for member in members for identifier in member["evidence_ids"]))
    return {"summary": "已逐股核对本地资料；缺失资料的股票仍需补充证据。",
            "assessment": "insufficient_data" if any(not member["evidence_ids"] for member in members) else "watch",
            "changes": ["事实：已核对本地归档行情的覆盖情况。"], "conditions": [], "members": members,
            "risks": ["缺少资料不能说明该股没有变化。"], "next_steps": ["继续补充本地公开资料。"],
            "review_summary": "按当前名单逐股检查，有证据的判断与资料缺口分别记录。", "evidence_ids": identifiers}


def mock_list_http(monkeypatch, before_response=None):
    requests = []
    original = httpx.AsyncClient

    async def respond(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fake-stock-list-worker-key"
        assert current_user_id.get() == 1
        body = json.loads(request.content)
        context = json.loads(body["messages"][-1]["content"])
        requests.append(context)
        if before_response:
            await before_response(context)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(model_list_report(context), ensure_ascii=False)}}],
                                         "usage": {"total_tokens": 50}})

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(engine.httpx, "AsyncClient", client)
    return requests


def test_stock_list_real_worker_charges_once_reuses_unchanged_and_keeps_revisions(monkeypatch):
    target = stock_list_fixture(monkeypatch)
    requests = mock_list_http(monkeypatch)
    assert asyncio.run(service.run_cycle())["processed"] == 1
    first = store.list_runs(1, plan_id=target["id"])[0]
    assert first["status"] == "succeeded", first["error_code"]
    assert len(requests) == store.usage(1)["used"] == 1
    result = first["result"]
    assert result["model_called"] is True
    members = {row["symbol"]: row for row in result["report"]["members"]}
    assert set(members) == set(target["target_symbols"])
    assert members["300750.SZ"]["assessment"] == "insufficient_data"
    assert members["300750.SZ"]["evidence_ids"] == []
    by_id = {row["id"]: row for row in result["evidence"]}
    for symbol in ("600519.SS", "000001.SZ"):
        assert members[symbol]["evidence_ids"]
        assert all(by_id[identifier]["symbol"] == symbol for identifier in members[symbol]["evidence_ids"])
    missing = next(row for row in result["coverage"] if row.get("source") == "stock_list" and row.get("symbol") == "300750.SZ")
    assert missing["count"] == missing["fresh_count"] == 0

    queued = store.enqueue_run(1, target["id"])
    assert asyncio.run(service.run_cycle())["processed"] == 1
    unchanged = next(row for row in store.list_runs(1, plan_id=target["id"]) if row["id"] == queued["id"])
    assert unchanged["status"] == "succeeded", unchanged["error_code"]
    assert unchanged["result"]["skipped_unchanged"] is True
    assert unchanged["result"]["input_hash"] == result["input_hash"]
    assert unchanged["result"]["model_called"] is False and unchanged["dispatched"] is False
    assert len(requests) == store.usage(1)["used"] == 1
    assert store.list_notifications(1) == []

    replacement = ["000001.SZ", "600000.SS"]
    updated = store.update_plan(1, target["id"], {"revision": 1, "target_symbols": replacement})
    assert asyncio.run(service.run_cycle())["processed"] == 1
    runs = store.list_runs(1, plan_id=target["id"])
    revised = next(row for row in runs if row["plan_revision"] == updated["revision"])
    assert revised["status"] == "succeeded", revised["error_code"]
    assert revised["result"]["input_hash"] != result["input_hash"]
    assert revised["result"]["model_called"] is True and not revised["result"].get("skipped_unchanged")
    assert {row["symbol"] for row in revised["result"]["report"]["members"]} == set(replacement)
    assert requests[-1]["plan"]["target_symbols"] == replacement
    assert len(requests) == store.usage(1)["used"] == 2
    historical = next(row for row in runs if row["id"] == first["id"])
    assert historical["plan_snapshot"]["target_symbols"] == target["target_symbols"]
    assert historical["result"] == result
    assert not [row for row in store.list_notifications(1) if row["kind"] == "change"]
    assert current_user_id.get() is None


def test_stock_list_edit_during_paid_http_preserves_old_result_without_overwriting_new_plan(monkeypatch):
    target = stock_list_fixture(monkeypatch)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_response(context):
            entered.set()
            await release.wait()

        requests = mock_list_http(monkeypatch, before_response=hold_response)
        operation = asyncio.create_task(service.run_cycle())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            old_run = store.list_runs(1, plan_id=target["id"])[0]
            assert old_run["dispatched"] and store.usage(1)["used"] == 1
            updated = store.update_plan(1, target["id"], {"revision": 1, "target_symbols": ["600000.SS"]})
            assert updated["revision"] == 2 and updated["last_success_at"] is None
        finally:
            release.set()
            await asyncio.wait_for(operation, timeout=5)
        current = store.get_plan(1, target["id"])
        assert current == updated
        historical = next(row for row in store.list_runs(1, plan_id=target["id"]) if row["id"] == old_run["id"])
        assert historical["status"] == "succeeded", historical["error_code"]
        assert historical["plan_snapshot"]["target_symbols"] == target["target_symbols"]
        assert historical["result"]["plan_snapshot"]["revision"] == 1
        assert {row["symbol"] for row in historical["result"]["report"]["members"]} == set(target["target_symbols"])
        assert len(requests) == store.usage(1)["used"] == 1
        assert store.list_notifications(1) == []

    asyncio.run(scenario())
    assert current_user_id.get() is None
