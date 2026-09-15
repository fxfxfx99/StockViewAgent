from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.security.jwt_tokens import decode_token
from app.security.user_context import current_user_id
from app.storage import users_store
from app.storage.users_store import UserRecord

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AsyncIterator[UserRecord]:
    user: UserRecord | None = None
    if creds and creds.credentials:
        try:
            payload = decode_token(creds.credentials)
            uid = int(payload.get("sub") or 0)
            user = users_store.get_by_id(uid)
            if not user:
                if settings.auth_required:
                    raise HTTPException(status_code=401, detail="用户不存在")
                user = users_store.get_open_local_user()
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            if settings.auth_required:
                raise HTTPException(status_code=401, detail="无效或已过期的 Token") from e
            user = users_store.get_open_local_user()
    elif not settings.auth_required:
        # 本地开放部署：无 Token 时使用本地默认用户，便于 GitHub 自托管
        user = users_store.get_open_local_user()
    else:
        raise HTTPException(status_code=401, detail="未登录或缺少 Token")

    token = current_user_id.set(user.id)
    try:
        yield user
    finally:
        current_user_id.reset(token)


async def require_admin(user: Annotated[UserRecord, Depends(get_current_user)]) -> UserRecord:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
