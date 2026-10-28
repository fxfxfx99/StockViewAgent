import asyncio
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import events
from app.security.jwt_tokens import create_access_token
from app.services import event_calendar_service as service
from app.storage import event_calendar_store as store, users_store


NOW = datetime(2026, 10, 24, 4, tzinfo=timezone.utc).timestamp()


def event(source_id="fed_fomc", **patch):
    return {"source_id": source_id, "upstream_id": "2026-policy", "source": "官方来源",
            "source_url": "https://example.org/calendar", "title": "货币政策 利率 FOMC",
            "region": "US", "timezone": "America/New_York", "category": "central_bank", "importance": 3,
            "scheduled_at": "2026-10-28T14:00:00-04:00", **patch}


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(service.time, "time", lambda: NOW)


def test_refresh_partial_failure_keeps_existing_evidence_and_cooldown(clock, monkeypatch):
    catalog = service.sources.SOURCE_CATALOG[:2]
    monkeypatch.setattr(service.sources, "SOURCE_CATALOG", catalog)
    store.ingest_events([event(catalog[1]["id"])], observed_at=NOW - 3600)
    store.set_source_state(catalog[1]["id"], {"status": "success", "last_success_at": store.iso(NOW - 3600), "event_count": 1})
    calls = []

    async def fetch(sid, **kwargs):
        calls.append(sid)
        if sid == catalog[1]["id"]:
            raise httpx.ConnectError("private credential should never appear")
        return [event(sid)]

    monkeypatch.setattr(service.sources, "fetch_source", fetch)
    result = asyncio.run(service.refresh())
    assert [r["status"] for r in result["results"]] == ["success", "failed"]
    assert len(store.visible_events()) == 2
    assert "private credential" not in str(result)
    assert result["results"][1]["last_success_at"] == store.iso(NOW - 3600)
    again = asyncio.run(service.refresh())
    assert all(row["skipped"] for row in again["results"])
    assert len(calls) == 2


def test_concurrent_refresh_uses_one_public_source_request(clock, monkeypatch):
    monkeypatch.setattr(service.sources, "SOURCE_CATALOG", service.sources.SOURCE_CATALOG[:1])

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = []

        async def fetch(sid, **kwargs):
            calls.append(sid)
            entered.set()
            await finish.wait()
            return [event(sid)]

        monkeypatch.setattr(service.sources, "fetch_source", fetch)
        first = asyncio.create_task(service.refresh())
        await entered.wait()
        second = await service.refresh()
        assert second["status"] == "in_progress"
        finish.set()
        await first
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("code,expected", [("http_503", "http_503"), ("http_403; private", "source_unavailable"), (None, "source_unavailable")])
def test_refresh_keeps_only_safe_source_diagnostics(clock, monkeypatch, code, expected):
    monkeypatch.setattr(service.sources, "SOURCE_CATALOG", service.sources.SOURCE_CATALOG[:1])

    async def fetch(*args, **kwargs):
        error = RuntimeError("private response")
        error.error_code = code
        raise error

    monkeypatch.setattr(service.sources, "fetch_source", fetch)
    result = asyncio.run(service.refresh())
    assert result["results"][0]["error_code"] == expected
    assert "private" not in str(result)


def test_historical_context_does_not_include_current_source_health(clock):
    store.ingest_events([event()], observed_at=NOW - 60)
    before = service.build_agent_context("news", "FOMC", as_of=NOW, user_id=7)
    store.set_source_state("fed_fomc", {"status": "failed", "last_attempt_at": store.iso(NOW), "error": "new failure"})
    assert service.build_agent_context("news", "FOMC", as_of=NOW, user_id=7) == before


def test_cancelled_refresh_holds_lease_until_writer_finishes(clock, monkeypatch):
    monkeypatch.setattr(service.sources, "SOURCE_CATALOG", service.sources.SOURCE_CATALOG[:1])
    started, finish = threading.Event(), threading.Event()
    original_ingest = store.ingest_events

    def ingest(rows, **kwargs):
        started.set()
        if not finish.wait(5):
            raise AssertionError("writer was not released")
        return original_ingest(rows, **kwargs)

    async def fetch(sid, **kwargs):
        return [event(sid)]

    monkeypatch.setattr(service.sources, "fetch_source", fetch)
    monkeypatch.setattr(store, "ingest_events", ingest)

    async def scenario():
        task = asyncio.create_task(service.refresh())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert (await service.refresh())["status"] == "in_progress"
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(store.visible_events()) == 1

    asyncio.run(scenario())


def test_calendar_uses_shanghai_day_but_preserves_date_only_source_day(clock):
    store.ingest_events([event(), event(upstream_id="date-only", all_day=True,
                                      scheduled_at="2026-10-28T00:00:00+09:00", timezone="Asia/Tokyo", region="JP")], observed_at=NOW)
    oct28 = service.calendar(start="2026-10-28", end="2026-10-28")
    oct29 = service.calendar(start="2026-10-29", end="2026-10-29")
    assert oct28["items"][0]["all_day"]
    assert oct29["items"][0]["scheduled_at"] == "2026-10-28T18:00:00+00:00"
    assert service.calendar(start="2026-10-01", end="2026-10-31", region="CN")["items"] == []
    with pytest.raises(ValueError):
        service.calendar(start="2026-11-01", end="2026-10-01")


def test_agent_filters_are_applied_before_topk_and_cancelled_events_are_excluded(clock):
    store.ingest_events([event(), event(upstream_id="cancelled", status="cancelled"),
                         event(upstream_id="china", region="CN", category="inflation", title="中国居民消费价格 CPI")], observed_at=NOW)
    store.update_config(7, {"regions": ["CN"], "categories": ["inflation"], "top_k": 1})
    context = service.build_agent_context("news", "通胀", user_id=7)
    assert len(context["items"]) == 1 and context["items"][0]["region"] == "CN"
    assert context["items"][0]["match_reason"] == "全文检索命中"
    assert context["evidence_sources"][0]["revision_id"] == context["items"][0]["revision_id"]
    assert len(service.build_agent_context("news", user_id=8)["items"]) == 2


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_jwt_secret", "calendar-auth-tests-only-32-characters")
    users_store.init_db()
    app = FastAPI()
    app.include_router(events.router)
    with TestClient(app) as client:
        yield client


def headers(uid):
    user = users_store.get_by_id(uid)
    return {"Authorization": "Bearer " + create_access_token(user_id=user.id, username=user.username, role=user.role)}


@pytest.mark.parametrize("method,path", [("get", "/calendar"), ("get", "/config"), ("patch", "/config"), ("post", "/refresh"), ("get", "/search"), ("get", "/unknown")])
def test_event_routes_require_auth(client, method, path):
    response = client.request(method, "/api/events" + path, json={} if method in {"patch", "post"} else None)
    assert response.status_code == 401


def test_preferences_and_search_are_scoped_to_authenticated_account(client, clock):
    store.ingest_events([event()], observed_at=NOW)
    response = client.patch("/api/events/config", json={"agents": {"news": False}}, headers=headers(2))
    assert response.status_code == 200 and response.json()["agents"]["news"] is False
    assert client.get("/api/events/search?agent=news", headers=headers(2)).json()["items"] == []
    assert client.get("/api/events/search?agent=news", headers=headers(1)).json()["items"]
    assert client.patch("/api/events/config", json={"user_id": 1}, headers=headers(2)).status_code == 400
    assert client.patch("/api/events/config", json={"top_k": True}, headers=headers(2)).status_code == 400
    assert client.get("/api/events/calendar?start=invalid", headers=headers(2)).status_code == 400
    assert client.get("/api/events/calendar?category=unknown", headers=headers(2)).status_code == 400
    assert client.get("/api/events/search?as_of=2026-01-01", headers=headers(2)).status_code == 400
    event_id = store.visible_events()[0]["id"]
    assert client.get(f"/api/events/{event_id}", headers=headers(2)).json()["revisions"]
    assert client.get("/api/events/missing", headers=headers(2)).status_code == 404
