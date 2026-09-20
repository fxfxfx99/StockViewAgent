"""公司资料接口参数与开放本地/登录访问链路。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import xueqiu
from app.security.jwt_tokens import create_access_token
from app.security.user_context import current_user_id
from app.storage import users_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_jwt_secret", "xueqiu-route-tests-only-32-characters")
    users_store.init_db()
    app = FastAPI()
    app.include_router(xueqiu.router)
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("force_query, expected_force", [(None, False), ("false", False), ("true", True)])
@pytest.mark.parametrize("open_local", [True, False])
def test_company_route_normalizes_symbol_passes_force_and_keeps_user_context(
    client, monkeypatch, force_query, expected_force, open_local
):
    monkeypatch.setattr(settings, "auth_required", not open_local)
    observed = []

    def fetch(symbol, force=False):
        observed.append((symbol, force, current_user_id.get()))
        return {"ok": True, "yahoo_symbol": symbol, "cached": not force, "company": {"name": "测试"}}

    monkeypatch.setattr(xueqiu.company_updates, "get_company_bundle", fetch)
    params = {"symbol": " 600519.ss "}
    if force_query is not None:
        params["force"] = force_query
    headers = {}
    user_id = 1 if open_local else 2
    if not open_local:
        user = users_store.get_by_id(user_id)
        token = create_access_token(user_id=user.id, username=user.username, role=user.role)
        headers["Authorization"] = f"Bearer {token}"
    response = client.get("/api/xueqiu/company", params=params, headers=headers)
    assert response.status_code == 200
    assert response.json()["yahoo_symbol"] == "600519.SS"
    assert observed == [("600519.SS", expected_force, user_id)]
    assert current_user_id.get() is None


def test_company_route_requires_auth_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)

    def should_not_run(*args, **kwargs):
        raise AssertionError("Unauthenticated requests must not fetch external data")

    monkeypatch.setattr(xueqiu.company_updates, "get_company_bundle", should_not_run)
    response = client.get("/api/xueqiu/company", params={"symbol": "600519.SS", "force": "true"})
    assert response.status_code == 401
    assert current_user_id.get() is None


@pytest.mark.parametrize("params", [{}, {"symbol": "600519.SS", "force": "invalid"}])
def test_company_route_rejects_invalid_parameters(client, params):
    assert client.get("/api/xueqiu/company", params=params).status_code == 422
