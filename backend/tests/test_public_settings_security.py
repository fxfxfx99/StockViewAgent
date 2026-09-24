import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.deps.auth import get_current_user
from app.routers import kline_learning, settings_integrations, settings_llm, watchlist
from app.security.llm_endpoints import validate_llm_endpoints
from app.services.llm_provider_presets import PROVIDER_PRESETS
from app.storage import user_credentials_store, users_store
from app.storage.users_store import UserRecord


@pytest.mark.parametrize("field", ["api_base", "fallback_api_base", "embedding_api_base"])
@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1:11434/v1",
        "http://169.254.169.254/latest/meta-data",
        "https://localhost/v1",
        "https://10.0.0.1/v1",
        "https://[::1]/v1",
        "https://api.openai.com.evil.example/v1",
        "https://api.openai.com@evil.example/v1",
        "https://user:password@api.openai.com/v1",
        "https://api.openai.com:8443/v1",
        "https://api.openai.com/v1/another-path",
        "https://api.openai.com/v1?target=localhost",
        "https://api.openai.com/v1?",
        "https://api.openai.com/v1#fragment",
        "https://api.openai.com/v1#",
        "https://api.openai.com/\nv1",
        "https://api.openai.com/v1/../internal",
        "https://api.openai.com/%76%31",
    ],
)
def test_public_llm_bases_reject_untrusted_destinations(monkeypatch, field, base):
    monkeypatch.setattr(settings, "auth_required", True)
    with pytest.raises(HTTPException) as exc:
        validate_llm_endpoints({field: base})
    assert exc.value.status_code == 400


@pytest.mark.parametrize("preset", [p for p in PROVIDER_PRESETS if p["api_base"].startswith("https://")])
def test_public_llm_presets_keep_supported_normalization(monkeypatch, preset):
    monkeypatch.setattr(settings, "auth_required", True)
    validate_llm_endpoints({
        "api_base": preset["api_base"] + "/chat/completions/",
        "fallback_api_base": " " + preset["api_base"] + "/ ",
        "embedding_api_base": "",
    })


def test_local_llm_bases_keep_custom_gateways(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    validate_llm_endpoints({"api_base": "http://127.0.0.1:11434/v1"})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    app = FastAPI()
    user = UserRecord(id=2, username="tester", role="user", display_name="", preferences={})
    app.dependency_overrides[get_current_user] = lambda: user
    for router in (settings_llm.router, settings_integrations.router, kline_learning.router, watchlist.router):
        app.include_router(router)
    monkeypatch.setattr(settings_llm, "llm_public_dict", lambda uid: {"ok": True})
    monkeypatch.setattr(settings_integrations, "integrations_payload", lambda uid: {"ok": True})
    with TestClient(app) as test_client:
        yield test_client, user


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/settings/llm", {"api_base": "http://127.0.0.1:11434/v1"}),
        ("/api/settings/llm", {"fallback_api_base": "http://127.0.0.1:11434/v1"}),
        ("/api/settings/llm", {"embedding_api_base": "http://127.0.0.1:11434/v1"}),
        ("/api/settings/integrations", {"llm": {"api_base": "http://127.0.0.1:11434/v1"}}),
        ("/api/settings/integrations", {"llm": {"embedding_api_base": "http://127.0.0.1:11434/v1"}}),
    ],
)
def test_settings_routes_reject_ssrf_before_writing(client, path, body):
    test_client, user = client
    response = test_client.put(path, json=body)
    assert response.status_code == 400
    assert user_credentials_store.load_user_credentials(user.id) == {}


def test_integrations_rejection_does_not_partially_save_other_credentials(client):
    test_client, user = client
    response = test_client.put("/api/settings/integrations", json={
        "tushare_token": "test-token",
        "llm": {"api_base": "https://evil.example/v1"},
    })
    assert response.status_code == 400
    assert user_credentials_store.load_user_credentials(user.id) == {}


@pytest.mark.parametrize("path", ["/api/settings/llm", "/api/settings/integrations"])
def test_settings_routes_validate_existing_bases_when_changing_keys(client, path):
    test_client, user = client
    original = {"llm": {"api_base": "http://127.0.0.1:11434/v1"}}
    user_credentials_store.merge_user_credentials(user.id, original)
    patch = {"api_key": "test-key"}
    response = test_client.put(path, json={"llm": patch} if path.endswith("integrations") else patch)
    assert response.status_code == 400
    assert user_credentials_store.load_user_credentials(user.id) == original


@pytest.mark.parametrize("path", ["/api/settings/llm", "/api/settings/integrations"])
@pytest.mark.parametrize("public", [True, False])
def test_settings_routes_preserve_supported_configuration(client, monkeypatch, path, public):
    test_client, user = client
    monkeypatch.setattr(settings, "auth_required", public)
    base = "https://api.openai.com/v1" if public else "http://127.0.0.1:11434/v1"
    patch = {"api_base": base + "/chat/completions/", "api_key": "test-key", "model": "test-model"}
    response = test_client.put(path, json={"llm": patch} if path.endswith("integrations") else patch)
    assert response.status_code == 200
    stored = user_credentials_store.load_user_credentials(user.id)["llm"]
    assert stored["api_base"] == base
    assert stored["api_key"] == "test-key"
    assert stored["model"] == "test-model"


@pytest.mark.parametrize("role, status", [("user", 403), ("admin", 200)])
@pytest.mark.parametrize("operation", ["upload", "delete"])
def test_shared_material_writes_require_admin(client, monkeypatch, role, status, operation):
    test_client, user = client
    user.role = role
    calls = []
    monkeypatch.setattr(kline_learning.kls, "add_material", lambda *args: calls.append("upload") or {"id": "test"})
    monkeypatch.setattr(kline_learning.kls, "delete_material", lambda *args: calls.append("delete") or True)
    if operation == "upload":
        response = test_client.post("/api/kline-learning/materials", files={"file": ("test.txt", b"test")})
    else:
        response = test_client.delete("/api/kline-learning/materials/test")
    assert response.status_code == status
    assert calls == ([operation] if status == 200 else [])


@pytest.mark.parametrize("public, role, status", [(True, "user", 403), (True, "admin", 200), (False, "user", 200)])
def test_shared_profile_edits_require_admin_only_in_public_mode(client, monkeypatch, public, role, status):
    test_client, user = client
    user.role = role
    monkeypatch.setattr(settings, "auth_required", public)
    monkeypatch.setattr(watchlist.watchlist_store, "load_symbols", lambda uid: ["600519.SS"])
    monkeypatch.setattr(watchlist, "_profile_view", lambda symbol: {})
    calls = []
    monkeypatch.setattr(watchlist.profile_store, "patch_manual", lambda *args: calls.append(args))
    response = test_client.patch("/api/watchlist/profiles/manual", json={"symbol": "600519.SS", "intro": "test"})
    assert response.status_code == status
    assert bool(calls) is (status == 200)


def test_local_default_admin_keeps_material_upload(client, monkeypatch):
    test_client, user = client
    user.role = "admin"
    test_client.app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(users_store, "get_open_local_user", lambda: user)
    monkeypatch.setattr(kline_learning.kls, "add_material", lambda *args: {"id": "test"})
    response = test_client.post("/api/kline-learning/materials", files={"file": ("test.txt", b"test")})
    assert response.status_code == 200
