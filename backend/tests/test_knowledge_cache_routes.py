"""Permissions, version conflicts and usable local knowledge management APIs."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.deps.auth import get_current_user
from app.routers import knowledge_cache as routes
from app.services import knowledge_cache_manager as manager, prompt_registry
from app.storage import knowledge_cache_store as cache
from app.storage.users_store import UserRecord

BASE = "/api/data-center/knowledge"


def client_for(role=None):
    app = FastAPI()
    app.include_router(routes.router)
    if role:
        app.dependency_overrides[get_current_user] = lambda: UserRecord(7, "tester", role, "Test", {})
    return TestClient(app)


@pytest.mark.parametrize("role,expected", [(None, 401), ("user", 403)])
def test_knowledge_data_and_prompt_mutations_are_administrator_only(monkeypatch, role, expected):
    monkeypatch.setattr(settings, "auth_required", True)
    with client_for(role) as client:
        for path in ("overview", "documents", "prompts", "status", "events"):
            assert client.get(f"{BASE}/{path}").status_code == expected
        for method, path, body in (
            ("post", "rebuild", {"scope": "all"}),
            ("put", "prompts/news_relevance", {"content": "new", "reason": "edit", "expected_revision": "old"}),
            ("post", "prompts/news_relevance/reset", {"reason": "reset", "expected_revision": "old"}),
        ):
            assert client.request(method, f"{BASE}/{path}", json=body).status_code == expected
    assert not (settings.data_dir / "knowledge_cache").exists()


def test_initial_documents_initialize_prompts_and_document_history_is_paginated():
    with client_for("admin") as client:
        initial = client.get(f"{BASE}/documents", params={"kind": "prompts"}).json()
        assert initial["total"] == 7
        for i in range(12):
            cache.put_document("news_relevance", "600519.SS", {"value": i})
        params = {"kind": "news_relevance", "key": "600519.SS", "offset": 8, "limit": 4}
        history = client.get(f"{BASE}/history", params=params).json()
        assert history["total"] == 12 and len(history["items"]) == 4
        assert history["offset"] == 8
        params = {"kind": "news_relevance", "key": "600519.SS", "revision_id": history["items"][-1]["revision_id"]}
        doc = client.get(f"{BASE}/document", params=params).json()
        assert doc["content"] == {"value": 0}
        assert doc["diff"]["from_revision"] is None
        assert client.get(f"{BASE}/document", params={"kind": "news_relevance", "key": "000001.SZ"}).status_code == 404
        assert client.get(f"{BASE}/document", params={"kind": "prompts", "key": "../../secret"}).status_code == 422
        assert client.get(f"{BASE}/history", params={**params, "offset": -1}).status_code == 422


def test_prompt_edit_conflict_rollback_and_reset_keep_auditable_actor():
    with client_for("admin") as client:
        initial = client.get(f"{BASE}/prompts").json()["items"]
        initial = next(p for p in initial if p["name"] == "news_relevance")
        body = {"content": "必须根据同股证据判断新闻相关性。", "reason": "核实暴露", "expected_revision": initial["revision_id"]}
        edited = client.put(f"{BASE}/prompts/news_relevance", json=body)
        assert edited.status_code == 200
        current = edited.json()
        assert current["mode"] == "custom" and current["actor_id"] == "7"
        assert client.put(f"{BASE}/prompts/news_relevance", json=body).status_code == 409
        rollback = client.post(f"{BASE}/prompts/news_relevance/activate", json={
            "revision_id": initial["revision_id"], "expected_revision": current["revision_id"], "reason": "恢复历史稿"})
        assert rollback.status_code == 200
        assert rollback.json()["mode"] == "rollback" and rollback.json()["content"] == initial["content"]
        reset = client.post(f"{BASE}/prompts/news_relevance/reset", json={
            "expected_revision": rollback.json()["revision_id"], "reason": "跟随默认"})
        assert reset.status_code == 200 and reset.json()["mode"] == "default"
        audit = client.get(f"{BASE}/events", params={"kind": "prompts", "key": "news_relevance"}).json()["items"]
        assert any(e["actor_id"] == "7" and e["reason"] == "核实暴露" for e in audit)
        assert client.put(f"{BASE}/prompts/default.news_relevance", json=body).status_code == 422
        assert client.put(f"{BASE}/prompts/news_relevance", json={**body, "content": " "}).status_code == 422
        assert client.put(f"{BASE}/prompts/news_relevance", json={**body, "reason": "x" * 401}).status_code == 422
        assert prompt_registry.get_prompt("news_relevance")["mode"] == "default"


def test_rebuild_is_queued_deduplicated_and_scope_limited(monkeypatch):
    monkeypatch.setattr(manager, "_symbols", lambda: ["600519.SS", "000001.SZ"])
    with client_for("admin") as client:
        result = client.post(f"{BASE}/rebuild", json={"scope": "all"}).json()
        assert result["job"]["total"] == 3 and result["job"]["actor_id"] == 7
        again = client.post(f"{BASE}/rebuild", json={"scope": "all"}).json()
        assert again["deduplicated"] and again["job"]["id"] == result["job"]["id"]
        assert client.get(f"{BASE}/status").json()["active_job"]["id"] == result["job"]["id"]
        for body in ({"scope": "delete"}, {"scope": "prompts", "symbol": "600519.SS"},
                     {"scope": "stocks", "symbol": "../a"}, {"scope": "stocks", "force_delete": True}):
            assert client.post(f"{BASE}/rebuild", json=body).status_code == 422
