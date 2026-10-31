"""Admin-only local file management and bounded public collection controls."""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.deps.auth import require_admin
from app.services import data_collection
from app.storage import cache_lifecycle, data_asset_manager, data_collection_store as store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/data-center", tags=["data-center"])
Admin = Annotated[UserRecord, Depends(require_admin)]


def checked(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/overview")
def overview(user: Admin):
    return data_collection.overview()


@router.get("/collection/status")
def collection_status(user: Admin):
    """Small runtime snapshot; polling never scans files or starts collection."""
    return data_collection.collection_overview()


@router.get("/inventory/summary")
def inventory_summary(user: Admin):
    return data_asset_manager.scan_data_assets(include_items=False)


@router.get("/files")
def files(user: Admin, category: str = "", q: str = Query("", max_length=200), source: str = "", lifecycle: str = "",
          offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), sort: str = "updated_at", descending: bool = True):
    return checked(data_asset_manager.query_data_assets, category=category, q=q, source=source, lifecycle=lifecycle,
                   offset=offset, limit=limit, sort=sort, descending=descending)


@router.get("/records")
def records(user: Admin, source_id: str | None = None, category: str | None = None, q: str = Query("", max_length=500),
            offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return checked(store.records, source_id=source_id, category=category, q=q, offset=offset, limit=limit)


@router.get("/config")
def config(user: Admin):
    return data_collection.get_config()


@router.patch("/config")
def update_config(body: dict[str, Any], user: Admin):
    return checked(data_collection.update_config, body)


@router.get("/sources")
def sources(user: Admin):
    return {"items": data_collection.sources()}


@router.get("/jobs")
def jobs(user: Admin, limit: int = Query(50, ge=1, le=200)):
    return {"items": store.list_jobs(limit), "usage": store.usage()}


class CollectBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_ids: list[str] | None = Field(None, max_length=50)


@router.post("/collect")
def collect(body: CollectBody, user: Admin):
    return checked(data_collection.enqueue, body.source_ids)


@router.get("/cache/policy")
def policy(user: Admin):
    return cache_lifecycle.get_policy()


@router.patch("/cache/policy")
def update_policy(body: dict[str, Any], user: Admin):
    return checked(cache_lifecycle.update_policy, body)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/cleanup/preview")
def preview(body: EmptyBody, user: Admin):
    return checked(cache_lifecycle.preview_cleanup)


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plan_id: str = Field(min_length=1, max_length=80)


@router.post("/cleanup/apply")
def apply(body: ApplyBody, user: Admin):
    return checked(cache_lifecycle.execute_cleanup, body.plan_id)


@router.get("/trash")
def trash(user: Admin, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return checked(cache_lifecycle.list_quarantine, offset=offset, limit=limit)


@router.post("/trash/{batch_id}/restore")
def restore(batch_id: str, body: EmptyBody, user: Admin):
    return checked(cache_lifecycle.restore_quarantine, batch_id)


class PurgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: bool


@router.delete("/trash/{batch_id}")
def purge(batch_id: str, body: PurgeBody, user: Admin):
    if not body.confirm:
        raise HTTPException(400, "永久删除回收缓存需要明确确认")
    return checked(cache_lifecycle.purge_quarantine, batch_id)
