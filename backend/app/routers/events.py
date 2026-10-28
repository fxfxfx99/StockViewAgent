"""Authenticated calendar browsing, refresh, evidence retrieval and preferences."""
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps.auth import get_current_user
from app.services import event_calendar_service as service
from app.storage import event_calendar_store as store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/events", tags=["economic-events"])
User = Annotated[UserRecord, Depends(get_current_user)]


@router.get("/calendar")
def calendar(user: User, start: str | None = None, end: str | None = None,
             region: str | None = None, min_importance: int = Query(2, ge=1, le=3),
             category: str | None = None, q: str = Query("", max_length=200)):
    if (region and region not in store.REGIONS) or (category and category not in store.CATEGORIES):
        raise HTTPException(400, "未知地区或事件类别")
    try:
        return service.calendar(start=start, end=end, region=region, min_importance=min_importance, category=category, q=q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/refresh")
async def refresh(user: User):
    return await service.refresh()


@router.get("/config")
def get_config(user: User):
    return store.get_config(user.id)


@router.patch("/config")
def update_config(body: dict[str, Any], user: User):
    try:
        return store.update_config(user.id, body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/search")
def search(user: User, q: str = Query("", max_length=2000), as_of: str | None = None,
           agent: Literal["news", "research", "strategies", "kline"] = "research"):
    try:
        return service.build_agent_context(agent, q, as_of=as_of, user_id=user.id)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(400, "查询时点须为带时区的有效时间") from exc


@router.get("/{event_id}")
def detail(event_id: str, user: User):
    event = store.get_event(event_id)
    if event is None:
        raise HTTPException(404, "事件不存在")
    return event
