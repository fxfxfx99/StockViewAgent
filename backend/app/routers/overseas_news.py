"""Read the persistent overseas wire without triggering external requests."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps.auth import get_current_user, require_admin
from app.services import overseas_news
from app.storage.users_store import UserRecord

router = APIRouter(
    prefix="/api/news/overseas",
    tags=["news"],
    dependencies=[Depends(get_current_user)],
)


@router.get("")
def feed(
    source: str | None = Query(default=None, max_length=80),
    query: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
):
    return overseas_news.list_feed(source=source, query=query, limit=limit, offset=offset)


@router.get("/status")
def source_status():
    return overseas_news.status()


@router.post("/refresh")
async def refresh(_admin: Annotated[UserRecord, Depends(require_admin)]):
    # Administrative refresh still observes source rate-limit cooldowns and
    # competes for the same lease as the automatic collector.
    return await overseas_news.collect_once(force=True)
