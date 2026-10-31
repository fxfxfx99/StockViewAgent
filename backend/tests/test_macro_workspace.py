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

    monkeypatch.setattr(service.global_market, "fetch_market_kline", public)
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
    assert {"STAR50", "US_NYSE", "US_NASDAQ", "US_AMEX", "FUT_GOLD", "FUT_WTI"} <= {row["key"] for row in client.get("/api/macro/indices").json()["items"]}
    response = client.get("/api/macro/index-kline?index_key=CYB&range=6mo&interval=1wk&refresh=true")
    assert response.status_code == 200
    assert calls == [(("CYB", "6mo", "1wk"), {"refresh": True})]
    for query in ("range=bad", "interval=1m"):
        assert client.get("/api/macro/index-kline?" + query).status_code == 422
    assert client.get("/api/macro/kline?series_id=bad").status_code == 400


def test_macro_routes_enforce_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    for path in ("indices", "catalog", "collection-status", "index-kline", "kline?series_id=sh"):
        assert client.get("/api/macro/" + path).status_code == 401


def test_global_baseline_preserves_session_date_unit_and_contract(monkeypatch):
    instrument = {"key": "FUT_GOLD", "label": "COMEX黄金连续期货", "region": "GLOBAL", "asset_class": "futures",
                  "timezone": "America/New_York", "unit": "美元/金衡盎司", "currency": "USD", "exchange": "COMEX", "symbol": "GC=F"}
    monkeypatch.setattr(service, "indices", lambda: [instrument])
    stamp = datetime(2026, 10, 30, 22, tzinfo=timezone.utc).timestamp()

    async def fetch(*args, **kwargs):
        return {**bundle(), "candles": [{"t": stamp, "o": 10, "h": 11, "l": 8, "c": 9, "v": 0}],
                "source_url": "https://finance.yahoo.com/quote/GC%3DF/", "data_as_of": "2026-10-30"}

    monkeypatch.setattr(service, "index_kline", fetch)
    result = asyncio.run(service.collect_market_baseline())
    assert result["status"] == "success"
    row = data_collection_store.records(source_id="public_market")["items"][0]
    assert row["period"] == "2026-10-30"  # Shanghai would already be October 31.
    assert row["region"] == "GLOBAL" and row["unit"] == "美元/金衡盎司"
    assert row["metadata"]["asset_class"] == "futures" and row["metadata"]["symbol"] == "GC=F"


def test_market_retry_skips_completed_instruments_and_honors_component_backoff(monkeypatch):
    monkeypatch.setattr(service, "indices", lambda: [{"key": "OK", "label": "正常"}, {"key": "FAIL", "label": "失败"}])
    calls = []

    async def fetch(key, *args, **kwargs):
        calls.append(key)
        if key == "FAIL":
            raise RuntimeError("sensitive upstream diagnostics")
        return bundle()

    monkeypatch.setattr(service, "index_kline", fetch)
    first = asyncio.run(service.collect_market_baseline())
    second = asyncio.run(service.collect_market_baseline())
    assert first["status"] == second["status"] == "partial"
    assert sorted(calls) == ["FAIL", "OK"]
    assert first["records_added"] == 1 and second["records_received"] == 0
    assert second["retry_after_seconds"] == 120
    rows = data_collection_store.get_state("public_market_components")
    failed = next(row for row in rows if row["key"] == "FAIL")
    assert failed["consecutive_failures"] == 1 and failed["next_retry_at"]
    assert "sensitive" not in str(second)
    failed["next_retry_at"] = "2000-01-01T00:00:00+00:00"
    data_collection_store.set_state("public_market_components", rows)
    asyncio.run(service.collect_market_baseline())
    assert calls.count("OK") == 1 and calls.count("FAIL") == 2


def test_cancelled_market_job_keeps_completed_checkpoint_and_resumes(monkeypatch):
    monkeypatch.setattr(service, "indices", lambda: [{"key": "OK", "label": "正常"}, {"key": "SLOW", "label": "慢源"}])
    calls = []

    async def scenario():
        async def fetch(key, *args, **kwargs):
            calls.append(key)
            if key == "SLOW" and calls.count("SLOW") == 1:
                await asyncio.Event().wait()
            return bundle()
        monkeypatch.setattr(service, "index_kline", fetch)
        task = asyncio.create_task(service.collect_market_baseline())
        for _ in range(100):
            await asyncio.sleep(0.01)
            rows = data_collection_store.get_state("public_market_components", [])
            if any(row["key"] == "OK" and row["status"] == "success" for row in rows):
                break
        else:
            pytest.fail("successful instrument checkpoint was not persisted")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        checkpoint = {row["key"]: row for row in data_collection_store.get_state("public_market_components")}
        assert checkpoint["OK"]["status"] == "success"
        assert checkpoint["SLOW"]["status"] == "pending"
        result = await service.collect_market_baseline()
        assert result["status"] == "success"
    asyncio.run(scenario())
    assert calls.count("OK") == 1 and calls.count("SLOW") == 2


def test_collection_status_merges_catalog_and_saved_health(client, monkeypatch):
    monkeypatch.setattr(settings, "enable_data_collection", True)
    data_collection_store.set_state("public_market_components", [{"key": "FUT_GOLD", "status": "failed",
        "consecutive_failures": 2, "last_attempt_at": "2026-10-31T12:00:00+00:00", "error_code": "source_timeout"}])
    result = client.get("/api/macro/collection-status").json()
    assert result["enabled"] and result["summary"]["failed"] == 1
    assert result["summary"]["pending"] == result["summary"]["total"] - 1
    gold = next(row for row in result["items"] if row["key"] == "FUT_GOLD")
    assert gold["asset_class"] == "futures" and gold["consecutive_failures"] == 2


def test_global_market_analysis_prompt_retains_units_and_stale_boundary(monkeypatch):
    from types import SimpleNamespace
    from app.routers import kline_learning as router

    async def fetch(*args):
        return {**bundle(), "symbol": "黄金连续期货", "index_key": "FUT_GOLD", "cache_stale": True,
                "timezone": "America/New_York", "instrument": {"asset_class": "futures", "exchange": "COMEX",
                "unit": "美元/金衡盎司", "timezone": "America/New_York", "description": "连续合约存在换月价差"}}
    monkeypatch.setattr(router.global_market, "fetch_market_kline", fetch)
    monkeypatch.setattr(router.event_calendar_context, "retrieve", lambda *args: {})
    monkeypatch.setattr(router.public_data_context, "retrieve", lambda *args: {})
    messages = []
    monkeypatch.setattr(router.kls, "llm_interpret", lambda system, user: (messages.append(user) or "test", None))
    result = asyncio.run(router.market_chat(router.MarketChatBody(index_key="FUT_GOLD", question="趋势如何"), SimpleNamespace(id=1)))
    assert "COMEX" in messages[0] and "美元/金衡盎司" in messages[0]
    assert "历史回退=是" in messages[0] and "换月" in messages[0]
    assert result["instrument"]["asset_class"] == "futures" and result["cache_stale"]
    assert result["kline_source"] == "tencent_index_fallback"
