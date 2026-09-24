"""Comment routes are authenticated, user scoped and enqueue without network work."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import xueqiu
from app.security.jwt_tokens import create_access_token
from app.security.user_context import current_user_id
from app.services import xueqiu_comments_service as service
from app.storage import users_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_jwt_secret", "comments-route-tests-secret-32-characters")
    users_store.init_db()
    app = FastAPI()
    app.include_router(xueqiu.router)
    with TestClient(app) as client:
        yield client


def headers(uid=2):
    user = users_store.get_by_id(uid)
    token = create_access_token(user_id=user.id, username=user.username, role=user.role)
    return {"Authorization": f"Bearer {token}"}


def test_get_returns_only_callers_snapshot_and_normalizes_symbol(client, monkeypatch):
    seen = []

    def snapshot(uid, symbol):
        seen.append((uid, symbol, current_user_id.get()))
        return {"symbol": symbol, "items": [], "status": "not_started"}

    monkeypatch.setattr(service, "get_snapshot", snapshot)
    result = client.get("/api/xueqiu/comments", params={"symbol": " 600519.sh "}, headers=headers())
    assert result.status_code == 200
    assert seen == [(2, "600519.SS", 2)]
    assert current_user_id.get() is None


@pytest.mark.parametrize("mode", ["latest", "previous_day"])
def test_post_only_enqueues_for_current_user(client, monkeypatch, mode):
    seen = []

    def enqueue(uid, symbol, **kwargs):
        seen.append((uid, symbol, kwargs, current_user_id.get()))
        return {"symbol": symbol, "status": "queued", "job": {"mode": mode}}

    monkeypatch.setattr(service, "enqueue_refresh", enqueue)
    result = client.post("/api/xueqiu/comments/refresh", json={"symbol": "000001.sz", "mode": mode}, headers=headers())
    assert result.status_code == 202
    assert seen == [(2, "000001.SZ", {"mode": mode, "source": "manual"}, 2)]


def test_unauthenticated_access_never_reads_or_enqueues(client, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Unauthenticated request reached the comment service")

    monkeypatch.setattr(service, "get_snapshot", fail)
    monkeypatch.setattr(service, "enqueue_refresh", fail)
    assert client.get("/api/xueqiu/comments", params={"symbol": "600519.SS"}).status_code == 401
    assert client.post("/api/xueqiu/comments/refresh", json={"symbol": "600519.SS"}).status_code == 401


@pytest.mark.parametrize("body", [
    {"symbol": "../users.db"},
    {"symbol": "６００５１９.SS"},
    {"symbol": "600519.SS", "mode": "all"},
    {"symbol": "600519.SS", "count": 10000},
    {"symbol": "600519.SS", "user_id": 1},
    {"symbol": "600519.SS", "target_date": "2000-01-01"},
    {},
])
def test_invalid_or_privilege_overriding_request_is_rejected(client, body):
    assert client.post("/api/xueqiu/comments/refresh", json=body, headers=headers()).status_code == 422


def test_invalid_query_symbol_rejected(client):
    assert client.get("/api/xueqiu/comments", params={"symbol": "bad"}, headers=headers()).status_code == 422


def test_local_open_mode_still_resolves_local_user(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(service, "get_snapshot", lambda uid, symbol: {"uid": uid, "symbol": symbol})
    response = client.get("/api/xueqiu/comments", params={"symbol": "600519.SS"})
    assert response.status_code == 200
    assert response.json()["uid"] == 1
