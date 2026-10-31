"""Read-only public evidence; account-owned model interpretation."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictBool

from app.config import settings
from app.deps.auth import get_current_user, require_admin
from app.services import merrill_clock as service
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/merrill-clock", tags=["merrill-clock"])
User = Annotated[UserRecord, Depends(get_current_user)]
Admin = Annotated[UserRecord, Depends(require_admin)]


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_analysis: StrictBool


class Analyze(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: Literal["US", "CN"] = "US"


def _enabled():
    if not getattr(settings, "enable_merrill_clock", True):
        raise HTTPException(409, "美林时钟后台已关闭，请启用 ENABLE_MERRILL_CLOCK 并重启后端")


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except service.RateLimit as exc:
        raise HTTPException(429, str(exc)) from exc
    except service.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def overview(user: User, region: Literal["US", "CN"] = "US"):
    return _call(service.overview, user.id, region)


@router.get("/series")
def series(user: User, ids: str = Query(min_length=1, max_length=1000), years: int = Query(default=10, ge=1, le=50)):
    return _call(service.series, [value.strip() for value in ids.split(",") if value.strip()], years)


@router.post("/refresh", status_code=202)
def refresh(user: Admin):
    _enabled()
    return {"job": _call(service.enqueue_refresh)}


@router.post("/analyze", status_code=202)
def analyze(user: User, body: Analyze = Analyze()):
    _enabled()
    return {"job": _call(service.enqueue_analysis, user.id, body.region)}


@router.put("/preferences")
def preferences(body: Preferences, user: User):
    if body.auto_analysis:
        _enabled()
    return _call(service.set_preferences, user.id, body.auto_analysis)
