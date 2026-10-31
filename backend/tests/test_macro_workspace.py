import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.routers import macro
from app.services import macro_workspace as service
from app.storage import data_collection_store, users_store


def bundle():
    return {"candles": [{"t": 1783900800, "o": 10, "h": 11, "l": 8, "c": 9, "v": 123}],
            "kline_source": "tencent_index_fallback", "cache_stale": False, "data_as_of": "2026-07-13"}


def test_public_macro_series_does_not_need_token_or_paid_provider(monkeypatch):
    calls = []

    async def public(*args, **kwargs):
        calls.append((args, kwargs))
        return bundle()

    monkeypatch.setattr(service.kline_pipeline, "fetch_major_index_kline_with_fallbacks", public)
    monkeypatch.setattr(service.macro_tushare_service, "fetch_macro_kline", lambda *args: pytest.fail("paid provider called"))
    result = asyncio.run(service.macro_kline("sh"))
    assert result["name"] == "上证综指"
    assert calls == [(("SH_COMP", "1y", "1d"), {"force_refresh": False, "public_only": True})]


def test_market_baseline_archives_only_fresh_results_and_keeps_partial_health(monkeypatch):
    monkeypatch.setattr(service, "indices", lambda: [{"key": "SH_COMP", "label": "上证指数"}, {"key": "SZ_COMP", "label": "深证成指"}])

    async def fetch(key, *args, **kwargs):
        return {**bundle(), "cache_stale": key == "SZ_COMP"}

    monkeypatch.setattr(service, "index_kline", fetch)
    result = asyncio.run(service.collect_market_baseline())
    assert result["status"] == "partial"
    assert result["records_received"] == result["records_added"] == 1
    assert len(data_collection_store.get_state("public_market_components")) == 2
    records = data_collection_store.records(source_id="public_market")
    assert records["total"] == 1
    assert records["items"][0]["symbols"] == ["SH_COMP"]
    assert records["items"][0]["source"] == "tencent_index_fallback"
    assert records["items"][0]["published_at"] is None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    users_store.init_db()
    app = FastAPI()
    app.include_router(macro.router)
    with TestClient(app) as client:
        yield client


def test_macro_routes_validation_and_refresh_contract(client, monkeypatch):
    calls = []

    async def public(*args, **kwargs):
        calls.append((args, kwargs))
        return bundle()

    monkeypatch.setattr(service, "index_kline", public)
    assert len(client.get("/api/macro/indices").json()["items"]) == 5
    response = client.get("/api/macro/index-kline?index_key=CYB&range=6mo&interval=1wk&refresh=true")
    assert response.status_code == 200
    assert calls == [(("CYB", "6mo", "1wk"), {"refresh": True})]
    for query in ("range=bad", "interval=1m"):
        assert client.get("/api/macro/index-kline?" + query).status_code == 422
    assert client.get("/api/macro/kline?series_id=bad").status_code == 400


def test_macro_routes_enforce_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    for path in ("indices", "catalog", "index-kline", "kline?series_id=sh"):
        assert client.get("/api/macro/" + path).status_code == 401
