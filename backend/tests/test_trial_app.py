from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.middlewares.error_handler import ErrorHandlerMiddleware, register_exception_handlers
from app.middlewares.trusted_origin import TrustedOriginMiddleware


@pytest.fixture
def trial_module(monkeypatch):
    # Load the deploy file directly without importing app.main and starting DBs.
    fake_main = ModuleType("app.main")
    fake_main.app = FastAPI()
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    path = Path(__file__).resolve().parents[2] / "deploy" / "trial_app.py"
    spec = importlib.util.spec_from_file_location("trial_app_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def frontend_dist(tmp_path):
    dist = tmp_path / "frontend" / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>trial app</body></html>", encoding="utf-8")
    (dist / "private.js").write_text("private-root-file", encoding="utf-8")
    (assets / "app-abc123.js").write_text("window.TRIAL = true", encoding="utf-8")
    (assets / "app-abc123.css").write_text("body { color: black; }", encoding="utf-8")
    (assets / "app.js.map").write_text("private-source-map", encoding="utf-8")
    (assets / ".private.js").write_text("private-hidden-file", encoding="utf-8")
    return dist


def echo_backend():
    backend = FastAPI()

    @backend.api_route("/{path:path}", methods=["GET", "PUT", "POST", "PATCH", "DELETE", "OPTIONS"])
    def echo(request: Request):
        return {"path": request.url.path, "method": request.method, "client": request.client.host}

    return backend


def client_from_peer(app, peer):
    async def with_peer(scope, receive, send):
        await app({**scope, "client": (peer, 12345)}, receive, send)

    return TestClient(with_peer)


@pytest.fixture
def client(trial_module, frontend_dist):
    return TestClient(trial_module.create_trial_app(echo_backend(), frontend_dist))


@pytest.mark.parametrize("path", ["/", "/setup"])
def test_spa_entry_points_and_head(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "trial app" in response.text
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    response = client.head(path)
    assert response.status_code == 200
    assert response.content == b""


def test_only_existing_built_assets_are_served(client):
    response = client.get("/assets/app-abc123.js")
    assert response.status_code == 200
    assert response.text == "window.TRIAL = true"
    assert "javascript" in response.headers["content-type"]
    assert client.get("/assets/app-abc123.css").status_code == 200


@pytest.mark.parametrize("path", [
    "/missing", "/setup/missing", "/index.html", "/private.js", "/.env",
    "/assets/missing.js", "/assets/app.js.map", "/assets/.private.js",
    "/assets/%2e%2e/private.js", "/assets/%2e%2e/%2e%2e/.env", "/docs",
    "/openapi.json", "/docs/oauth2-redirect", "/redoc", "/api/docs", "/api",
    "/api/admin/users", "/api/ready", "/api/settings/llm", "/api/quant/orders",
    "/api/market/history/export", "/api/watchlist/profiles/unknown", "/api/xueqiu/bundle",
    "/api/news/source-config", "/api/transaction-agent/knowledge/ingest-local",
    "/api/market/kline/some%2Fother%2Fpath",
])
def test_unlisted_paths_never_fall_back_to_spa(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_symlink_cannot_escape_static_directory(client, frontend_dist, tmp_path):
    secret = tmp_path / "secret.js"
    secret.write_text("secret-outside-dist", encoding="utf-8")
    (frontend_dist / "assets" / "linked.js").symlink_to(secret)
    (frontend_dist / "assets" / "root.js").symlink_to(frontend_dist / "private.js")
    assert client.get("/assets/linked.js").status_code == 404
    assert client.get("/assets/root.js").status_code == 404


def test_allowlist_covers_frontend_api_contract(client):
    api_file = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api.js"
    source = api_file.read_text(encoding="utf-8")
    calls = re.findall(r'client\.(get|post|put|patch|delete)\(\s*(["`])([^"`]+)\2', source)
    assert len(calls) >= 25
    for method, _quote, path in calls:
        path = path.replace("${encodeURIComponent(symbol)}", "000001.SZ")
        response = client.request(method.upper(), "/api" + path)
        assert response.status_code == 200, (method, path, response.text)
        assert response.json()["path"] == "/api" + path
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path", [
    ("DELETE", "/api/watchlist"), ("PATCH", "/api/auth/me"),
    ("POST", "/api/market/kline/000001.SZ"), ("PUT", "/api/auth/login"),
    ("POST", "/"), ("POST", "/assets/app-abc123.js"),
])
def test_unlisted_methods_are_not_exposed(client, method, path):
    assert client.request(method, path).status_code == 404


def test_only_allowlisted_cors_preflights_reach_backend(client):
    headers = {"Access-Control-Request-Method": "POST", "Origin": "https://trial.example"}
    assert client.options("/api/auth/login", headers=headers).status_code == 200
    assert client.options("/api/auth/me", headers=headers).status_code == 404
    assert client.options("/api/admin/users", headers=headers).status_code == 404


@pytest.mark.parametrize("path", ["/", "/assets/app-abc123.js", "/api/health", "/api/admin/users", "/missing"])
def test_security_headers_cover_success_and_failures(client, path):
    response = client.get(path)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "max-age=" in response.headers["strict-transport-security"]
    if path.startswith("/api/"):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize("peer,headers,expected", [
    ("127.0.0.1", {"CF-Connecting-IP": "203.0.113.17"}, "203.0.113.17"),
    ("::1", {"CF-Connecting-IP": "2001:db8::7"}, "2001:db8::7"),
    ("198.51.100.9", {"CF-Connecting-IP": "203.0.113.17"}, "198.51.100.9"),
    ("127.0.0.1", {"X-Forwarded-For": "203.0.113.17"}, "127.0.0.1"),
    ("127.0.0.1", {"CF-Connecting-IP": "not-an-ip"}, "127.0.0.1"),
    ("127.0.0.1", {"CF-Connecting-IP": "203.0.113.17, 203.0.113.18"}, "127.0.0.1"),
    ("127.0.0.1", {"CF-Connecting-IP": "fe80::1%eth0"}, "127.0.0.1"),
    ("127.0.0.1", {}, "127.0.0.1"),
])
def test_cf_client_ip_only_trusted_from_loopback(trial_module, frontend_dist, peer, headers, expected):
    app = trial_module.create_trial_app(echo_backend(), frontend_dist)
    client = client_from_peer(app, peer)
    response = client.get("/api/health", headers=headers)
    assert response.json()["client"] == expected


def test_duplicate_cf_ip_headers_are_ignored(trial_module, frontend_dist):
    client = client_from_peer(trial_module.create_trial_app(echo_backend(), frontend_dist), "127.0.0.1")
    response = client.get("/api/health", headers=[("CF-Connecting-IP", "203.0.113.1"), ("CF-Connecting-IP", "203.0.113.2")])
    assert response.json()["client"] == "127.0.0.1"


def test_backend_origin_checks_are_preserved(trial_module, frontend_dist):
    backend = echo_backend()
    backend.add_middleware(TrustedOriginMiddleware, allowed_origins="https://trial.example")
    client = TestClient(trial_module.create_trial_app(backend, frontend_dist), base_url="https://trial.example")
    assert client.get("/api/health", headers={"Origin": "https://trial.example"}).status_code == 200
    rejected = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert rejected.status_code == 403
    assert rejected.headers["cache-control"] == "no-store"
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403


def test_errors_hide_private_values_and_preserve_retry_after(trial_module, frontend_dist, caplog):
    backend = FastAPI()
    backend.add_middleware(ErrorHandlerMiddleware)
    register_exception_handlers(backend)

    @backend.get("/api/health")
    def failure():
        raise RuntimeError("PRIVATE EXCEPTION with token=super-secret")

    @backend.post("/api/auth/login")
    def throttled():
        raise HTTPException(429, detail="请求过于频繁，请稍后重试", headers={"Retry-After": "30"})

    class ConfigBody(BaseModel):
        token: int

    @backend.put("/api/settings/integrations")
    def invalid(body: ConfigBody):
        return {"ok": True}

    client = TestClient(trial_module.create_trial_app(backend, frontend_dist))
    response = client.get("/api/health")
    assert response.status_code == 500
    assert response.json() == {"detail": "服务暂时不可用，请稍后重试"}
    assert "PRIVATE EXCEPTION" not in caplog.text
    assert response.headers["x-content-type-options"] == "nosniff"
    response = client.post("/api/auth/login")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    response = client.put("/api/settings/integrations", json={"token": "PRIVATE INPUT"})
    assert response.status_code == 422
    assert response.json() == {"detail": "请求参数无效"}
    assert "PRIVATE INPUT" not in caplog.text


def test_backend_lifespan_and_websocket_rejection(trial_module, frontend_dist):
    from starlette.websockets import WebSocketDisconnect

    events = []

    @asynccontextmanager
    async def lifespan(_app):
        events.append("startup")
        yield
        events.append("shutdown")

    backend = FastAPI(lifespan=lifespan)
    app = trial_module.create_trial_app(backend, frontend_dist)
    with TestClient(app) as client:
        assert events == ["startup"]
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect("/api/health"):
                pytest.fail("trial must not expose WebSockets")
        assert caught.value.code == 1008
    assert events == ["startup", "shutdown"]


def test_swagger_routes_are_removed_from_backend(trial_module, frontend_dist):
    backend = FastAPI()
    trial_module.create_trial_app(backend, frontend_dist)
    assert backend.docs_url is None
    assert backend.openapi_url is None
    assert not {"/docs", "/redoc", "/openapi.json"} & {route.path for route in backend.routes}
