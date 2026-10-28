"""Personal research tracking. Refresh scans local archives without paid calls."""
from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps.auth import get_current_user
from app.schemas.tracking import ChangePatch, ThesisCreate, ThesisPatch, ThesisReview, canonical_symbol
from app.storage import tracking_store as store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/tracking", tags=["tracking"])
User = Annotated[UserRecord, Depends(get_current_user)]


def _run(function, *args, **kwargs):
    try:
        result = function(*args, **kwargs)
    except (store.RevisionConflict, store.ScanConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "当前账户没有此跟踪记录")
    return result


@router.get("/theses")
def list_theses(user: User, status: Literal["active", "closed"] | None = None,
                symbol: str | None = None, limit: int = Query(500, ge=1, le=1000)):
    if symbol:
        symbol = _run(canonical_symbol, symbol)
    return {"items": store.list_theses(user.id, status=status, symbol=symbol, limit=limit)}


@router.post("/theses", status_code=201)
def create_thesis(body: ThesisCreate, user: User):
    return _run(store.create_thesis, user.id, body.model_dump(mode="json"))


@router.get("/theses/{thesis_id}")
def detail(thesis_id: str, user: User):
    thesis = _run(store.get_thesis, user.id, thesis_id)
    return {"thesis": thesis, "timeline": store.list_timeline(user.id, thesis_id),
            "changes": store.list_changes(user.id, thesis_id=thesis_id, include_resolved=True)}


@router.patch("/theses/{thesis_id}")
def patch_thesis(thesis_id: str, body: ThesisPatch, user: User):
    return _run(store.update_thesis, user.id, thesis_id, body.model_dump(mode="json", exclude_unset=True))


@router.post("/theses/{thesis_id}/reviews")
def review(thesis_id: str, body: ThesisReview, user: User):
    return _run(store.review_thesis, user.id, thesis_id, body.model_dump(mode="json", exclude_none=True))


@router.get("/changes")
def changes(user: User, symbol: str | None = None, unread_only: bool = False,
            limit: int = Query(200, ge=1, le=500)):
    from app.services import tracking_service

    if symbol:
        symbol = _run(canonical_symbol, symbol)
    return {**tracking_service.status(user.id),
            "items": store.list_changes(user.id, symbol=symbol, unread_only=unread_only, limit=limit)}


@router.patch("/changes/{change_id}")
def patch_change(change_id: str, body: ChangePatch, user: User):
    return _run(store.update_change, user.id, change_id, body.model_dump(exclude_unset=True))


@router.post("/refresh")
async def refresh(user: User):
    from app.services import tracking_service

    return await asyncio.to_thread(_run, tracking_service.refresh, user.id)
