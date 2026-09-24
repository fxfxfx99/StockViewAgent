from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.deps.auth import require_admin
from app.routers.auth import router
from app.security import decode_token, verify_password
from app.security import auth_limits
from app.security.auth_limits import AuthAttemptLimiter, password_work
from app.storage import users_store
from app.storage.user_credentials_store import load_user_credentials, merge_user_credentials
from app.storage.users_store import UserRecord

PASSWORD = "trial-password-123"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_registration_enabled", True)
    monkeypatch.setattr(settings, "auth_registration_max_users", 200)
    monkeypatch.setattr(auth_limits, "auth_attempt_limiter", AuthAttemptLimiter())
    users_store.init_db()
    app = FastAPI()
    app.include_router(router)

    @app.get("/admin-test")
    def admin_test(user: Annotated[UserRecord, Depends(require_admin)]):
        return {"id": user.id}

    return TestClient(app)


def signup(client, **overrides):
    body = {"username": "trial_user", "password": PASSWORD, **overrides}
    return client.post("/api/auth/register", json=body)


def test_registration_disabled_by_default():
    assert Settings(_env_file=None).auth_registration_enabled is False


@pytest.mark.parametrize("required,enabled", [(False, False), (False, True), (True, False)])
def test_registration_requires_both_flags(client, monkeypatch, required, enabled):
    monkeypatch.setattr(settings, "auth_required", required)
    monkeypatch.setattr(settings, "auth_registration_enabled", enabled)
    assert client.get("/api/auth/config").json() == {
        "auth_required": required,
        "registration_enabled": False,
    }
    assert signup(client).status_code == 403
    assert len(users_store.list_users()) == 2


def test_success_has_user_role_and_existing_session_shape(client):
    assert client.get("/api/auth/config").json() == {
        "auth_required": True,
        "registration_enabled": True,
    }
    response = signup(client, username="Trial_User", display_name="试用用户")
    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {"access_token", "token_type", "user"}
    assert payload["token_type"] == "bearer"
    assert payload["user"] == {
        "id": 3,
        "username": "trial_user",
        "role": "user",
        "display_name": "试用用户",
        "preferences": {},
    }
    token = payload["access_token"]
    assert decode_token(token)["role"] == "user"
    assert PASSWORD not in response.text
    user, salt, password_hash = users_store.get_by_username("trial_user")
    assert salt not in response.text
    assert password_hash not in response.text
    assert verify_password(PASSWORD, salt, password_hash)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).json() == payload["user"]
    assert client.get("/admin-test", headers=headers).status_code == 403
    assert client.get("/api/auth/me").status_code == 401
    login = client.post("/api/auth/login", json={"username": user.username, "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["user"] == payload["user"]


def test_registration_duplicate_is_case_insensitive(client):
    assert signup(client, username="Trial_User").status_code == 201
    response = signup(client, username="TRIAL_USER")
    assert response.status_code == 409
    assert response.json() == {"detail": "用户名不可用"}
    assert len(users_store.list_users()) == 3


def test_registration_defaults_to_public_data_without_copying_secrets(client):
    merge_user_credentials(1, {"market_data_provider": "tushare", "tushare_token": "admin-secret"})
    response = signup(client)
    assert response.status_code == 201
    assert load_user_credentials(response.json()["user"]["id"]) == {"market_data_provider": "public"}
    assert load_user_credentials(1)["tushare_token"] == "admin-secret"


@pytest.mark.parametrize("overrides", [
    {"username": "ab"},
    {"username": "a" * 33},
    {"username": "试用账户"},
    {"username": "user-name"},
    {"username": "with space"},
    {"username": "trial_user\n"},
    {"password": "short"},
    {"password": "x" * 129},
    {"display_name": "x" * 65},
    {"role": "admin"},
    {"preferences": {"role": "admin"}},
])
def test_invalid_signup_rejected_without_echoing_input(client, overrides):
    response = signup(client, **overrides)
    assert response.status_code == 422
    assert response.json() == {"detail": "请求参数无效"}
    assert len(users_store.list_users()) == 2


def test_maximum_field_lengths_are_accepted(client):
    assert signup(client, username="U" * 32, password="x" * 128, display_name="名" * 64).status_code == 201


def test_account_cap_blocks_new_accounts(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_registration_max_users", 3)
    assert signup(client).status_code == 201
    response = signup(client, username="second_user")
    assert response.status_code == 503
    assert response.json() == {"detail": "注册暂不可用"}
    assert len(users_store.list_users()) == 3


def test_login_failure_is_generic_and_hashes_unknown_accounts(client, monkeypatch):
    calls = []

    def failing_verifier(*args):
        calls.append(args)
        return False

    monkeypatch.setattr("app.routers.auth.verify_password", failing_verifier)
    unknown = client.post("/api/auth/login", json={"username": "missing", "password": "wrong"})
    known = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert unknown.status_code == known.status_code == 401
    assert unknown.json() == known.json() == {"detail": "用户名或密码错误"}
    assert len(calls) == 2


def test_login_rate_limit_cannot_be_bypassed_with_forwarded_headers(client, monkeypatch):
    calls = []

    def failing_verifier(*args):
        calls.append(args)
        return False

    monkeypatch.setattr("app.routers.auth.verify_password", failing_verifier)
    for i in range(10):
        response = client.post(
            "/api/auth/login",
            json={"username": "missing", "password": PASSWORD},
            headers={"X-Forwarded-For": f"198.51.100.{i}", "CF-Connecting-IP": f"192.0.2.{i}"},
        )
        assert response.status_code == 401
    response = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert len(calls) == 10


def test_registration_rate_limit_applies_before_hashing(client, monkeypatch):
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return UserRecord(99, kwargs["username"], "user", "", {})

    monkeypatch.setattr(users_store, "create_user", fake_create)
    for i in range(5):
        assert signup(client, username=f"trial_{i}").status_code == 201
    response = signup(client, username="trial_six")
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) <= 3600
    assert len(calls) == 5


def test_local_open_mode_still_works(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["username"] == "admin"


@pytest.mark.parametrize("action,total,window", [("login", 120, 60), ("register", 30, 3600)])
def test_global_limit_and_window_expiry(action, total, window):
    now = [100.0]
    limiter = AuthAttemptLimiter(clock=lambda: now[0])
    for i in range(total):
        limiter.check(action, f"client-{i}")
    with pytest.raises(HTTPException) as caught:
        limiter.check(action, "fresh-client")
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == str(window)
    now[0] += window
    limiter.check(action, "fresh-client")


def test_client_bucket_memory_is_bounded_and_recovers_after_expiry():
    now = [0.0]
    limiter = AuthAttemptLimiter(clock=lambda: now[0])
    limiter.MAX_BUCKETS = 3
    limiter.check("login", "a")
    limiter.check("login", "b")
    with pytest.raises(HTTPException) as caught:
        limiter.check("login", "c")
    assert caught.value.status_code == 429
    assert len(limiter._buckets) == 3
    now[0] += 60
    limiter.check("login", "c")
    assert len(limiter._buckets) <= 3


def test_password_work_has_bounded_concurrency_and_releases_slots():
    with password_work(), password_work():
        with pytest.raises(HTTPException) as caught:
            with password_work():
                pytest.fail("extra password worker must not start")
        assert caught.value.status_code == 429
    with password_work():
        pass
