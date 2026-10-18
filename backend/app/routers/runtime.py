"""Read-only, account-scoped operational metrics. No prompt or credential payloads."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.deps.auth import get_current_user
from app.services import runtime_telemetry
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.get("/overview")
def overview(user: Annotated[UserRecord, Depends(get_current_user)],
             period: Annotated[Literal["today", "7d", "all"], Query()] = "today"):
    return runtime_telemetry.overview(user.id, period)
