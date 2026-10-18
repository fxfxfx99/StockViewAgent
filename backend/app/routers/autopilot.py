"""Local autopilot controls; the separate public-trial allowlist excludes these."""
from typing import Annotated

from fastapi import APIRouter, Depends

from app.deps.auth import get_current_user
from app.services import local_autopilot
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])
User = Annotated[UserRecord, Depends(get_current_user)]


@router.get("/status")
def status(user: User):
    return local_autopilot.status(user.id)


@router.post("/reconcile")
def reconcile(user: User):
    return local_autopilot.reconcile(user.id)
