"""Authenticated API contracts, optimistic edits and purely local refresh."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.deps.auth import get_current_user
from app.routers.tracking import router
from app.services import tracking_service
from app.storage import tracking_store


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as test_client:
        yield test_client


def test_create_read_revise_review_and_foreign_account(client):
    response = client.post("/api/tracking/theses", json={"symbol": "600519.SS", "reason": "观察盈利改善"})
    assert response.status_code == 201
    saved = response.json()
    path = "/api/tracking/theses/" + saved["id"]
    assert client.get("/api/tracking/theses").json()["items"] == [saved]
    detail = client.get(path).json()
    assert detail["thesis"] == saved and detail["timeline"][0]["kind"] == "created"
    assert client.patch(path, json={"reason": "缺少版本"}).status_code == 422
    assert client.patch(path, json={"revision": 1, "reason": "更新判断"}).status_code == 200
    assert client.patch(path, json={"revision": 1, "reason": "过期编辑"}).status_code == 409
    assert client.post(path + "/reviews", json={"revision": 2, "action": "continue", "reason": "资料仍不足",
        "evidence_assessment": "uncertain"}).json()["revision"] == 3
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2)
    assert client.get(path).status_code == 404
    assert client.patch(path, json={"revision": 3, "reason": "尝试修改"}).status_code == 404
    assert client.post(path + "/reviews", json={"revision": 3, "action": "close", "reason": "尝试结束",
        "evidence_assessment": "uncertain"}).status_code == 404
    assert client.get("/api/tracking/theses").json() == {"items": []}


def test_validation_and_feedback_contract(client):
    assert client.post("/api/tracking/theses", json={"symbol": "BAD", "reason": "test"}).status_code == 422
    assert client.post("/api/tracking/theses", json={"symbol": "600519.SS", "reason": "test", "review_at": "2026-02-30"}).status_code == 422
    assert client.post("/api/tracking/theses", json={"symbol": "600519.SS", "reason": "test", "user_id": 2}).status_code == 422
    assert client.post("/api/tracking/theses", json={"symbol": "600519.SS", "reason": "test", "signal_id": "missing"}).status_code == 400
    tracking_store.put_changes(1, [{"id": "news1", "kind": "news", "title": "新资料", "thesis_ids": []}])
    result = client.patch("/api/tracking/changes/news1", json={"is_read": True, "feedback": "irrelevant"})
    assert result.status_code == 200 and result.json()["feedback"] == "irrelevant"
    assert client.patch("/api/tracking/changes/news1", json={"feedback": None}).json()["feedback"] is None
    assert client.patch("/api/tracking/changes/news1", json={}).status_code == 422
    assert client.patch("/api/tracking/changes/news1", json={"is_read": "false"}).status_code == 422


def test_get_changes_reads_status_only_and_refresh_calls_local_service(client, monkeypatch):
    calls = []
    monkeypatch.setattr(tracking_service, "status", lambda uid: {"coverage": {"status": "limited"}, "automatic_enabled": False})
    monkeypatch.setattr(tracking_service, "refresh", lambda uid: calls.append(uid) or {"inserted": 0})
    response = client.get("/api/tracking/changes")
    assert response.status_code == 200 and response.json()["coverage"]["status"] == "limited"
    assert calls == []
    assert client.post("/api/tracking/refresh").json() == {"inserted": 0}
    assert calls == [1]
