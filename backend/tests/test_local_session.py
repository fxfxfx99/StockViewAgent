"""The launcher lifecycle is private, opt-in, and counts live pages exactly."""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.middlewares.trusted_origin import TrustedOriginMiddleware
from app.routers.local_session import create_local_session_router

_TOKEN = "test-launcher-session-token"
_STATUS = "/api/local-session/status"
_SOCKET_URL = "ws://127.0.0.1:8001/api/local-session/ws"
_SOCKET = f"{_SOCKET_URL}?token={_TOKEN}"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def make_client(monkeypatch, token=_TOKEN, allowed_origins=""):
    if token is None:
        monkeypatch.delenv("STOCKVIEW_LOCAL_SESSION_TOKEN", raising=False)
    else:
        monkeypatch.setenv("STOCKVIEW_LOCAL_SESSION_TOKEN", token)
    app = FastAPI()
    app.include_router(create_local_session_router())
    app.add_middleware(TrustedOriginMiddleware, allowed_origins=allowed_origins)
    return TestClient(app, base_url="http://127.0.0.1:8001")


@pytest.mark.parametrize("token", [None, "", "   "])
def test_disabled_without_a_process_token(monkeypatch, token):
    with make_client(monkeypatch, token) as client:
        assert client.get(_STATUS, headers=_AUTH).status_code == 404
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(_SOCKET):
                pytest.fail("Disabled local session accepted a page")
        assert error.value.code == 1008


def test_counts_multiple_tabs_disconnects_and_reconnects(monkeypatch):
    with make_client(monkeypatch) as client:
        initial = client.get(_STATUS, headers=_AUTH).json()
        assert initial == {"clients": 0, "connected_once": False, "instance_id": initial["instance_id"]}
        assert isinstance(initial["instance_id"], str) and initial["instance_id"]
        with client.websocket_connect(_SOCKET, headers={"Origin": "http://localhost:5173"}) as first:
            assert first.receive_json() == {"type": "ready", "instance_id": initial["instance_id"]}
            assert client.get(_STATUS, headers=_AUTH).json()["clients"] == 1
            with client.websocket_connect(_SOCKET) as second:
                second.receive_json()
                assert client.get(_STATUS, headers=_AUTH).json()["clients"] == 2
                # Payloads do not create extra clients or require a heartbeat.
                second.send_text("still here")
                second.send_bytes(b"still here")
                assert client.get(_STATUS, headers=_AUTH).json()["clients"] == 2
            assert client.get(_STATUS, headers=_AUTH).json()["clients"] == 1
        assert client.get(_STATUS, headers=_AUTH).json() == {
            "clients": 0, "connected_once": True, "instance_id": initial["instance_id"],
        }
        with client.websocket_connect(_SOCKET) as reopened:
            reopened.receive_json()
            assert client.get(_STATUS, headers=_AUTH).json()["clients"] == 1
        assert client.get(_STATUS, headers=_AUTH).json()["clients"] == 0


def test_status_requires_launcher_token_and_hides_routes_from_schema(monkeypatch):
    with make_client(monkeypatch) as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {_TOKEN}"}):
            assert client.get(_STATUS, headers=headers).status_code == 403
        assert client.get(_STATUS, headers=[("Authorization", f"Bearer {_TOKEN}"), ("Authorization", "Bearer wrong")]).status_code == 403
        assert client.get(_STATUS, headers=_AUTH).status_code == 200
        assert _STATUS not in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("query", ["", "?token=wrong", "?token=%E9%94%99", f"?token={_TOKEN}&token={_TOKEN}"])
def test_rejected_sockets_do_not_register_clients(monkeypatch, query):
    with make_client(monkeypatch) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(_SOCKET_URL + query):
                pytest.fail("Invalid token accepted")
        assert error.value.code == 1008
        status = client.get(_STATUS, headers=_AUTH).json()
        assert status["clients"] == 0
        assert status["connected_once"] is False


@pytest.mark.parametrize("headers", [
    {"Origin": "https://untrusted.example"},
    {"Origin": "null"},
    {"Origin": "http://localhost.untrusted.example"},
    {"Host": "untrusted.example", "Origin": "http://localhost:5173"},
    {"Host": "localhost:65536"},
    [("Origin", "http://localhost:5173"), ("Origin", "https://untrusted.example")],
    [("Host", "localhost:8001"), ("Host", "untrusted.example")],
])
def test_websockets_enforce_existing_host_and_origin_rules(monkeypatch, headers):
    with make_client(monkeypatch) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(_SOCKET, headers=httpx.Headers(headers)):
                pytest.fail("Untrusted origin or host accepted")
        assert error.value.code == 1008
        assert client.get(_STATUS, headers=_AUTH).json()["connected_once"] is False


def test_explicitly_allowed_origin_can_connect(monkeypatch):
    with make_client(monkeypatch, allowed_origins="https://stocks.example") as client:
        with client.websocket_connect(_SOCKET, headers={"Origin": "https://stocks.example"}) as socket:
            socket.receive_json()
            assert client.get(_STATUS, headers=_AUTH).json()["clients"] == 1


def test_new_instance_has_independent_counts_and_identity(monkeypatch):
    with make_client(monkeypatch) as first, make_client(monkeypatch) as second:
        with first.websocket_connect(_SOCKET) as socket:
            socket.receive_json()
            first_status = first.get(_STATUS, headers=_AUTH).json()
            second_status = second.get(_STATUS, headers=_AUTH).json()
            assert first_status["clients"] == 1
            assert second_status["clients"] == 0
            assert second_status["connected_once"] is False
            assert first_status["instance_id"] != second_status["instance_id"]
