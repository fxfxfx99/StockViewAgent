"""验证浏览器跨域请求在触发业务前被拒绝，CLI 与明确登记的前端保持可用。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.middlewares.trusted_origin import TrustedOriginMiddleware


def make_client(allowed_origins="", base_url="http://127.0.0.1:8001"):
    app = FastAPI()
    called = []

    @app.api_route("/api/probe", methods=["GET", "POST"])
    def probe():
        called.append(True)
        return {"ok": True}

    app.add_middleware(TrustedOriginMiddleware, allowed_origins=allowed_origins)
    return TestClient(app, base_url=base_url), called


@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS"])
@pytest.mark.parametrize("origin", ["https://untrusted.example", "null", "http://localhost.untrusted.example"])
def test_untrusted_origin_cannot_reach_business_handler(method, origin):
    client, called = make_client()
    headers = {"Origin": origin}
    if method == "OPTIONS":
        headers.update({"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    response = client.request(method, "/api/probe", headers=headers)
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert called == []


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:5175", "http://localhost:5180", "https://localhost", "http://127.0.0.1:8001", "https://127.0.0.1:8443", "http://[::1]:5175"],
)
def test_loopback_and_local_same_origin_requests_are_allowed(origin):
    client, called = make_client()
    response = client.post("/api/probe", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "Origin" in response.headers["vary"]
    assert called == [True]


def test_trusted_preflight_allows_authorization_and_json_without_running_handler():
    client, called = make_client()
    response = client.options("/api/probe", headers={
        "Origin": "http://localhost:5175",
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5175"
    assert "PUT" in response.headers["access-control-allow-methods"]
    assert response.headers["access-control-allow-headers"] == "authorization,content-type"
    assert called == []


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_cli_without_origin_is_unchanged(method):
    client, called = make_client()
    response = client.request(method, "/api/probe")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert called == [True]


@pytest.mark.parametrize("origin", ["https://stocks.example", "http://192.168.1.20:5175"])
def test_explicit_deployment_and_lan_proxy_origins_are_allowed(origin):
    client, called = make_client(" https://stocks.example:443 , http://192.168.1.20:5175 ")
    # 代理可以把 Host 改为后端地址；授权仍依据明确登记的浏览器 Origin。
    response = client.get("/api/probe", headers={"Origin": origin, "Host": "127.0.0.1:8001"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert called == [True]


@pytest.mark.parametrize("origin", ["http://stocks.example", "https://stocks.example:444", "https://stocks.example.untrusted.example"])
def test_explicit_origins_do_not_trust_other_schemes_ports_or_suffixes(origin):
    client, called = make_client("https://stocks.example")
    assert client.post("/api/probe", headers={"Origin": origin}).status_code == 403
    assert called == []


def test_matching_host_and_forwarded_host_cannot_bypass_origin_policy():
    client, called = make_client(base_url="https://untrusted.example")
    response = client.post("/api/probe", headers={
        "Origin": "https://untrusted.example",
        "X-Forwarded-Host": "localhost:5175",
        "X-Forwarded-Proto": "http",
    })
    assert response.status_code == 403
    assert called == []


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unknown_host_without_origin_cannot_access_local_api(method):
    client, called = make_client(base_url="http://untrusted.example")
    response = client.request(method, "/api/probe", headers={"X-Forwarded-Host": "localhost:5175"})
    assert response.status_code == 403
    assert called == []


@pytest.mark.parametrize("origin", [None, "https://stocks.example"])
def test_registered_deployment_host_supports_cli_and_same_origin_browser(origin):
    client, called = make_client("https://stocks.example", base_url="https://stocks.example")
    response = client.get("/api/probe", headers={"Origin": origin} if origin else {})
    assert response.status_code == 200
    assert called == [True]


@pytest.mark.parametrize("host", ["localhost.untrusted.example", "localhost:65536", "user@localhost", "localhost/path", "[::1]untrusted.example"])
def test_malformed_or_unknown_host_is_rejected_even_with_local_origin(host):
    client, called = make_client()
    response = client.get("/api/probe", headers={"Host": host, "Origin": "http://localhost"})
    assert response.status_code == 403
    assert called == []


@pytest.mark.parametrize("origin", ["", "http://localhost/path", "http://localhost?", "http://localhost#", "http://user@localhost", "http://localhost:65536", "http://localhost:0", "http://localhost:", "\x00http://localhost", "file://localhost", "https://localhost,https://untrusted.example"])
def test_malformed_origins_are_not_treated_as_loopback(origin):
    client, called = make_client()
    assert client.get("/api/probe", headers={"Origin": origin}).status_code == 403
    assert called == []


def test_duplicate_origin_headers_are_rejected():
    client, called = make_client()
    response = client.get("/api/probe", headers=[("Origin", "http://localhost"), ("Origin", "https://untrusted.example")])
    assert response.status_code == 403
    assert called == []


@pytest.mark.parametrize("origins", ["*", "https://*.example.com", "https://stocks.example/path", "null"])
def test_invalid_allowlist_configuration_fails_closed(origins):
    with pytest.raises(ValueError, match="CORS_ALLOWED_ORIGINS"):
        TrustedOriginMiddleware(FastAPI(), allowed_origins=origins)


def test_allowed_origins_configuration_loads_from_environment(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://stocks.example,http://192.168.1.20:5175")
    assert Settings(_env_file=None).cors_allowed_origins == "https://stocks.example,http://192.168.1.20:5175"
