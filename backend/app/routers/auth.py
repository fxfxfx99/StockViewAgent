from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.deps.auth import get_current_user
from app.security import create_access_token, verify_password
from app.storage import users_store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class MePatchBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    preferences: dict[str, Any] | None = None


@router.post("/login")
def login(body: LoginBody):
    row = users_store.get_by_username(body.username)
    if not row:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    user, salt, ph = row
    if not verify_password(body.password, salt, ph):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
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
