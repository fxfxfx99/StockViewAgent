"""JWT 访问令牌（HS256）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.config import settings


def create_access_token(*, user_id: int, username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=max(1, settings.auth_token_ttl_hours))
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": exp,
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.auth_jwt_secret, algorithms=["HS256"])
