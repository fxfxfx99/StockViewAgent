from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps.auth import get_current_user
from app.routers import runtime
from app.security.user_context import current_user_id
from app.services import runtime_telemetry as svc
from app.storage.users_store import UserRecord


def test_empty_ledger_has_real_zero_and_tracking_boundary():
    result = svc.overview(1)
    assert result["llm"]["calls"] == result["llm"]["total_tokens"] == 0
    assert result["collection"]["records_received"] == 0
    assert result["recent"] == [] and result["tracking_started_at"] > 0


def test_success_failure_unknown_and_pending_do_not_fabricate_usage():
    good = svc.start_event("llm", "chat", user_id=1)
    svc.finish_event(good, usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
    unknown = svc.start_event("llm", "chat", user_id=1)
    svc.finish_event(unknown, status="failed", error_code="http_429")
    partial = svc.start_event("llm", "embedding", user_id=1)
    svc.finish_event(partial, usage={"total_tokens": 5})
    svc.start_event("llm", "chat", user_id=1)
    summary = svc.overview(1)["llm"]
    assert summary == {"calls": 4, "success": 2, "failed": 1, "pending": 1, "cancelled": 0,
                       "input_tokens": 7, "output_tokens": 3, "total_tokens": 15,
                       "unknown_token_calls": 1, "reported_token_calls": 2}
    second = svc.start_event("llm", "chat", user_id=2)
    svc.finish_event(second, usage={"total_tokens": True})
    assert svc.overview(2)["llm"]["total_tokens"] is None
    assert svc.overview(2)["llm"]["unknown_token_calls"] == 1


def test_collection_shared_scope_and_idempotent_finish():
    handle = svc.record_collection("news_archive", "public", records_received=12, records_added=2, user_id=0, event_id="batch-a")
    svc.record_collection("news_archive", "public", records_received=12, records_added=2, user_id=0, event_id="batch-a")
    svc.finish_event(handle, records_received=9999, records_added=9999)
    svc.record_collection("market_history", "sina", records_received=20, records_added=3, user_id=1)
    svc.record_collection("market_history", "tencent", records_received=100, records_added=10, user_id=2)
    first = svc.overview(1)
    assert first["collection"]["requests"] == 2
    assert first["collection"]["records_received"] == 32
    assert first["collection"]["records_added"] == 5
    assert first["collection"]["shared_records_received"] == 12
    assert all(row["source"] != "tencent" for row in first["recent"])
    assert svc.overview(2)["collection"]["records_received"] == 112


def test_finishing_on_another_thread_preserves_actor():
    token = current_user_id.set(42)
    try:
        handle = svc.start_event("llm", "chat")
    finally:
        current_user_id.reset(token)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(svc.finish_event, handle, usage={"input_tokens": 6, "output_tokens": 4}).result()
    assert svc.overview(42)["llm"]["total_tokens"] == 10
    assert svc.overview(1)["llm"]["calls"] == 0


def test_days_are_shanghai_calendar_days_and_old_unfinished_is_not_failed(monkeypatch):
    stamp = datetime(2026, 10, 18, 0, 5, tzinfo=svc._CN).timestamp()
    monkeypatch.setattr(svc.time, "time", lambda: stamp - 600)
    svc.record_collection("news_archive", "public", records_received=10, user_id=0)
    monkeypatch.setattr(svc.time, "time", lambda: stamp - 7 * 86400)
    svc.start_event("llm", "chat", user_id=1)
    monkeypatch.setattr(svc.time, "time", lambda: stamp)
    svc.record_collection("news_archive", "public", records_received=3, user_id=0)
    assert svc.overview(1, "today", now=stamp)["collection"]["records_received"] == 3
    assert svc.overview(1, "7d", now=stamp)["collection"]["records_received"] == 13
    assert svc.overview(1, "7d", now=stamp)["llm"]["calls"] == 0
    all_time = svc.overview(1, "all", now=stamp)
    assert all_time["llm"]["pending"] == 1 and all_time["llm"]["failed"] == 0
    assert all_time["recent"][-1]["status"] == "unfinished"


def test_sensitive_fields_and_raw_errors_never_persist():
    handle = svc.start_event("llm", "chat", source="https://secret:password@api.example.test/v1?api_key=PRIVATE", user_id=1)
    svc.finish_event(handle, status="failed", usage={"total_tokens": 10, "messages": "PRIVATE"}, error_code="token=PRIVATE")
    result = svc.overview(1)
    text = json.dumps(result)
    assert "PRIVATE" not in text and "password" not in text
    assert result["recent"][0]["source"] == "api.example.test"
    assert result["recent"][0]["error_code"] is None


def test_metrics_write_failures_do_not_block_business(monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("disk unavailable")
    monkeypatch.setattr(svc.RuntimeLedger, "__init__", unavailable)
    assert svc.start_event("llm", "chat") == ""
    assert svc.finish_event("1:abc") is None
    assert svc.record_collection("news", "public", records_received=1) == ""


def test_concurrent_calls_do_not_lose_counts():
    def one(index):
        handle = svc.start_event("llm", "chat", user_id=1, event_id=f"request-{index}")
        svc.finish_event(handle, usage={"total_tokens": index})
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(one, range(20)))
    result = svc.overview(1)
    assert result["llm"]["calls"] == 20
    assert result["llm"]["total_tokens"] == sum(range(20))
    assert len(result["recent"]) == 20


def test_api_uses_authenticated_user_and_validates_period():
    app = FastAPI()
    app.include_router(runtime.router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(7, "test", "user", "", {})
    handle = svc.start_event("llm", "chat", user_id=8)
    svc.finish_event(handle, usage={"total_tokens": 900})
    with TestClient(app) as client:
        assert client.get("/api/runtime/overview?user_id=8").json()["llm"]["calls"] == 0
        assert client.get("/api/runtime/overview?period=bad").status_code == 422
        assert client.get("/api/runtime/overview?period=7d").json()["timezone"] == "Asia/Shanghai"
