import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import stocks
from app.security.jwt_tokens import create_access_token
from app.storage import users_store


@pytest.fixture
def client_and_calls(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_jwt_secret", "stock-route-tests-only-32-characters")
    users_store.init_db()
    calls = []

    def refresh():
        calls.append("refresh")
        return {"ok": True}

    def rebuild():
        calls.append("rebuild")
        return {"ok": True}

    monkeypatch.setattr(stocks.a_share_stocks, "fetch_and_save", refresh)
    monkeypatch.setattr(stocks.issuer_basic_info_universe, "rebuild_universe_from_upload_blocking", rebuild)
    app = FastAPI()
    app.include_router(stocks.router)
    with TestClient(app) as client:
        yield client, calls


@pytest.mark.parametrize("path", ["/refresh", "/universe/rebuild"])
@pytest.mark.parametrize("role, expected", [(None, 401), ("user", 403), ("admin", 200)])
def test_stock_maintenance_requires_admin(client_and_calls, path, role, expected):
    client, calls = client_and_calls
    headers = {}
    if role:
        user = users_store.get_by_id(1 if role == "admin" else 2)
        token = create_access_token(user_id=user.id, username=user.username, role=user.role)
        headers["Authorization"] = f"Bearer {token}"
    response = client.post("/api/stocks" + path, headers=headers)
    assert response.status_code == expected
    assert len(calls) == (1 if expected == 200 else 0)


@pytest.mark.parametrize("path", ["/refresh", "/universe/rebuild"])
def test_local_open_mode_preserves_default_admin(client_and_calls, monkeypatch, path):
    client, calls = client_and_calls
    monkeypatch.setattr(settings, "auth_required", False)
    assert client.post("/api/stocks" + path).status_code == 200
    assert len(calls) == 1
