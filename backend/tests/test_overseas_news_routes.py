"""Overseas wire reads stay local and shared writes require an administrator."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.routers import overseas_news as routes
from app.security.jwt_tokens import create_access_token
from app.services import overseas_news as service
from app.storage import users_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_jwt_secret", "overseas-test-secret-at-least-32-characters")
    users_store.init_db()
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as session:
        yield session


def headers(uid):
    user = users_store.get_by_id(uid)
    return {"Authorization": "Bearer " + create_access_token(
        user_id=user.id, username=user.username, role=user.role)}


@pytest.mark.parametrize("method,path", [("get", ""), ("get", "/status"), ("post", "/refresh")])
def test_auth_required(client, method, path):
    assert client.request(method, "/api/news/overseas" + path).status_code == 401


def test_read_does_not_collect_and_validates_pagination(client, monkeypatch):
    calls = []

    async def no_network(**kwargs):
        raise AssertionError("GET must never poll providers")

    monkeypatch.setattr(service, "collect_once", no_network)
    monkeypatch.setattr(service, "list_feed", lambda **kwargs: calls.append(kwargs) or {
        "items": [], "total": 0, "updated_at": None})
    response = client.get("/api/news/overseas?source=ft&query=rates&limit=20&offset=40", headers=headers(2))
    assert response.status_code == 200
    assert calls == [{"source": "ft", "query": "rates", "limit": 20, "offset": 40}]
    for query in ("limit=0", "limit=201", "offset=-1", "offset=100001", "query=" + "x" * 201):
        assert client.get("/api/news/overseas?" + query, headers=headers(2)).status_code == 422


def test_manual_refresh_admin_only(client, monkeypatch):
    calls = []

    async def collect(**kwargs):
        calls.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(service, "collect_once", collect)
    assert client.post("/api/news/overseas/refresh", headers=headers(2)).status_code == 403
    assert calls == []
    assert client.post("/api/news/overseas/refresh", headers=headers(1)).status_code == 200
    assert calls == [{"force": True}]


@pytest.mark.parametrize("enabled", [True, False])
def test_lifecycle_collects_without_other_schedulers_or_user_settings(monkeypatch, enabled):
    from app.main import lifespan
    from app.services import xueqiu_comments_service
    from app.storage import data_asset_manager

    for name in ("enable_scheduler", "enable_startup_data_check", "company_auto_refresh_enabled",
                 "enable_data_collection", "enable_research_worker"):
        monkeypatch.setattr(settings, name, False)
    monkeypatch.setattr(settings, "overseas_news_enabled", enabled)
    monkeypatch.setattr(data_asset_manager, "refresh_catalog_from_disk", lambda: None)
    started, stopped = set(), set()

    def loop(name):
        async def run():
            started.add(name)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(name)
        return run

    monkeypatch.setattr(service, "scheduler_loop", loop("overseas"))
    monkeypatch.setattr(xueqiu_comments_service, "worker_loop", loop("comments"))

    async def run():
        async with lifespan(FastAPI()):
            await asyncio.sleep(0)
            assert ("overseas" in started) is enabled
        assert stopped == started

    asyncio.run(run())
