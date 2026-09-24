from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.deps.auth import get_current_user
from app.security import create_access_token, verify_password
from app.security.auth_limits import AuthRoute, check_auth_attempt, password_work, registration_lock
from app.storage import users_store
from app.storage.user_credentials_store import merge_user_credentials
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/auth", tags=["auth"], route_class=AuthRoute)


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(..., min_length=10, max_length=128)
    display_name: str | None = Field(default=None, max_length=64)


class MePatchBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    preferences: dict[str, Any] | None = None


def _session_response(user: UserRecord):
    token = create_access_token(user_id=user.id, username=user.username, role=user.role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "display_name": user.display_name,
            "preferences": user.preferences,
        },
    }


@router.get("/config")
def auth_config():
    return {
        "auth_required": settings.auth_required,
        "registration_enabled": settings.auth_required and settings.auth_registration_enabled,
    }


@router.post("/login")
def login(body: LoginBody, request: Request):
    check_auth_attempt(request, "login")
    with password_work():
        row = users_store.get_by_username(body.username)
        # Missing accounts do the same expensive work and receive the same error.
        salt, ph = (row[1], row[2]) if row else ("0" * 32, "0" * 64)
        valid = verify_password(body.password, salt, ph)
    if not row or not valid:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return _session_response(row[0])


@router.post("/register", status_code=201)
def register(body: RegisterBody, request: Request):
    if not (settings.auth_required and settings.auth_registration_enabled):
        raise HTTPException(status_code=403, detail="注册未开放")
    check_auth_attempt(request, "register")
    with password_work(), registration_lock:
        if users_store.get_by_username(body.username):
            raise HTTPException(status_code=409, detail="用户名不可用")
        if len(users_store.list_users()) >= max(0, settings.auth_registration_max_users):
            raise HTTPException(status_code=503, detail="注册暂不可用")
        try:
            user = users_store.create_user(
                username=body.username,
                password=body.password,
                role="user",
                display_name=body.display_name or "",
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="用户名不可用") from exc
        merge_user_credentials(user.id, {"market_data_provider": "public"})
    return _session_response(user)


@router.get("/me")
def me(user: Annotated[UserRecord, Depends(get_current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "display_name": user.display_name,
        "preferences": user.preferences,
    }


@router.patch("/me")
def patch_me(user: Annotated[UserRecord, Depends(get_current_user)], body: MePatchBody):
    if body.display_name is not None:
        users_store.update_user(user.id, display_name=body.display_name)
    if body.preferences is not None:
        users_store.save_preferences(user.id, dict(body.preferences))
    u = users_store.get_by_id(user.id)
    assert u
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "display_name": u.display_name,
        "preferences": u.preferences,
    }
