"""HTTP 层验证鉴权、个人凭据上下文和股票列表范围。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import admin, macro, market, news
from app.security.jwt_tokens import create_access_token
from app.security.user_context import current_user_id
from app.storage import user_credentials_store, users_store, watchlist_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_jwt_secret", "request-context-tests-only-32-characters")
    users_store.init_db()
    app = FastAPI()
    for router in (market.router, macro.router, news.router, admin.router):
        app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


def _headers(user_id=2):
    user = users_store.get_by_id(user_id)
    token = create_access_token(user_id=user.id, username=user.username, role=user.role)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("get", "/api/market/kline/600519.SS", None),
        ("get", "/api/macro/kline?series_id=sh_index", None),
        ("post", "/api/news/analyze", {"items": []}),
        ("post", "/api/news/analyze-symbol-archive", {"symbol": "600519.SS"}),
        ("post", "/api/news/sync-archive-feeds", {}),
    ],
)
def test_external_data_routes_require_authentication(client, method, url, body):
    kwargs = {"json": body} if body is not None else {}
    response = client.request(method, url, **kwargs)
    assert response.status_code == 401
    assert current_user_id.get() is None


@pytest.mark.parametrize("open_local", [False, True])
def test_async_market_route_uses_account_credentials(client, monkeypatch, open_local):
    monkeypatch.setattr(settings, "auth_required", not open_local)
    user_id = 1 if open_local else 2
    user_credentials_store.merge_user_credentials(user_id, {"tushare_token": "account-test-token"})
    observed = []

    async def fetch(symbol, range_param, interval):
        observed.append((current_user_id.get(), settings.effective_tushare_token))
        return {"symbol": symbol, "candles": [], "metrics": {}}

    monkeypatch.setattr(market.kline_pipeline, "fetch_a_share_kline_with_fallbacks", fetch)
    response = client.get("/api/market/kline/600519.SS", headers={} if open_local else _headers())
    assert response.status_code == 200
    assert observed == [(user_id, "account-test-token")]
    assert current_user_id.get() is None


def test_sync_macro_route_uses_account_credentials(client, monkeypatch):
    user_credentials_store.merge_user_credentials(2, {"tushare_token": "macro-test-token"})
    observed = []

    def fetch(series_id, range_param):
        observed.append((current_user_id.get(), settings.effective_tushare_token))
        return {"series_id": series_id, "candles": []}

    monkeypatch.setattr(macro.macro_tushare_service, "fetch_macro_kline", fetch)
    response = client.get("/api/macro/kline?series_id=sh_index", headers=_headers())
    assert response.status_code == 200
    assert observed == [(2, "macro-test-token")]
    assert current_user_id.get() is None


def test_news_analysis_uses_current_account_list_and_credentials(client, monkeypatch, tmp_path):
    from app.storage import llm_runtime

    monkeypatch.setattr(llm_runtime, "_FILE", tmp_path / "llm_runtime.json")
    watchlist_store.save_symbols(1, ["600519.SS"])
    watchlist_store.save_symbols(2, ["000001.SZ"])
    user_credentials_store.merge_user_credentials(2, {"llm": {"api_key": "analysis-test-key"}})
    observed = []

    def analyze(items, symbols):
        observed.append((current_user_id.get(), settings.effective_openai_key, symbols))
        return [{"ok": True} for _ in items]

    monkeypatch.setattr(news.impact_analyzer, "analyze_batch", analyze)
    response = client.post("/api/news/analyze", json={"items": [{"title": "新闻"}]}, headers=_headers())
    assert response.status_code == 200
    assert response.json()["watchlist"] == ["000001.SZ"]
    assert observed == [(2, "analysis-test-key", ["000001.SZ"])]
    denied = client.post(
        "/api/news/analyze-symbol-archive", json={"symbol": "600519.SS"}, headers=_headers()
    )
    assert denied.status_code == 200
    assert denied.json()["ok"] is False
    assert current_user_id.get() is None


def test_news_sync_defaults_to_current_account_watchlist(client, monkeypatch):
    watchlist_store.save_symbols(1, ["600519.SS"])
    watchlist_store.save_symbols(2, ["000001.SZ"])
    observed = []

    async def sync(symbols, *, lookback_days):
        observed.append((current_user_id.get(), symbols, lookback_days))
        return {"symbols_queried": symbols}

    monkeypatch.setattr(news.news_feed_sync, "sync_all_sources_to_archive", sync)
    response = client.post("/api/news/sync-archive-feeds", json={"lookback_days": 7}, headers=_headers())
    assert response.status_code == 200
    assert observed == [(2, ["000001.SZ"], 7)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"symbols": [" 600519.ss "]}, ["600519.SS"]),
        ({"offset": 1, "limit": 1}, ["000001.SZ"]),
    ],
)
def test_admin_company_refresh_accepts_symbols_or_paging(client, monkeypatch, body, expected):
    monkeypatch.setattr(
        admin.company_universe_service, "all_listed_symbols", lambda: ["600519.SS", "000001.SZ"]
    )

    async def refresh(symbols, *, skip_fresh_days):
        return {"refreshed": symbols, "skip_fresh_days": skip_fresh_days}

    monkeypatch.setattr(admin.company_universe_service, "refresh_symbols", refresh)
    response = client.post("/api/admin/company-universe/refresh", json=body, headers=_headers(1))
    assert response.status_code == 200
    assert response.json()["refreshed"] == expected
    assert response.json()["skip_fresh_days"] == 60.0


def test_company_refresh_remains_admin_only(client):
    response = client.post("/api/admin/company-universe/refresh", json={}, headers=_headers())
    assert response.status_code == 403
