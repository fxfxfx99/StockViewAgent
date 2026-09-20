from __future__ import annotations

import asyncio
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.deps.auth import require_admin
from app.services import company_universe_service
from app.storage import company_fundamentals_store, data_asset_manager, users_store
from app.storage.platform_store import load_platform, save_platform_merge
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/admin", tags=["admin"])


class CreateUserBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=4, max_length=256)
    role: str = Field(default="user", description="admin 或 user")
    display_name: str = ""


class PatchUserBody(BaseModel):
    role: str | None = None
    display_name: str | None = Field(default=None, max_length=128)


class ResetPasswordBody(BaseModel):
    password: str = Field(..., min_length=4, max_length=256)


class PlatformPatchBody(BaseModel):
    """管理员可调 K 线可选 SDK 开关等；未传字段不修改。"""

    kline_sdk: dict[str, bool] | None = None


class CompanyUniverseRefreshBody(BaseModel):
    """批量写入全市场 F10 基本信息缓存（较慢；可多次调用 offset 分页）。"""

    offset: int = Field(0, ge=0, description="在 A 股列表中的起始下标")
    limit: int = Field(80, ge=1, le=400, description="本批最多处理只数")
    skip_fresh_days: float = Field(
        60.0,
        ge=0,
        le=3650,
        description="若某代码缓存未过期则跳过；0=每次都拉",
    )
    symbols: list[str] | None = Field(
        default=None,
        description="若提供则只刷新这些代码（忽略 offset/limit 切片）",
    )


class FundamentalsImportBody(BaseModel):
    """导入根目录三份 2025 CSV；force=false 时相同源文件会跳过。"""

    force: bool = False


@router.get("/users")
def admin_list_users(_: Annotated[UserRecord, Depends(require_admin)]):
    return {"items": [u.__dict__ for u in users_store.list_users()]}


@router.post("/users")
def admin_create_user(_: Annotated[UserRecord, Depends(require_admin)], body: CreateUserBody):
    try:
        u = users_store.create_user(body.username, body.password, role=body.role, display_name=body.display_name)
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=400, detail="用户名已存在") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "user": u.__dict__}


@router.patch("/users/{user_id}")
def admin_patch_user(
    user_id: int,
    actor: Annotated[UserRecord, Depends(require_admin)],
    body: PatchUserBody,
):
    if user_id == actor.id and body.role is not None and body.role != "admin":
        raise HTTPException(status_code=400, detail="不能取消自己的管理员权限")
    try:
        u = users_store.update_user(user_id, role=body.role, display_name=body.display_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "user": u.__dict__}


@router.post("/users/{user_id}/password")
def admin_reset_password(
    user_id: int,
    _: Annotated[UserRecord, Depends(require_admin)],
    body: ResetPasswordBody,
):
    if not users_store.get_by_id(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    users_store.set_password(user_id, body.password)
    return {"ok": True}


@router.delete("/users/{user_id}")
def admin_delete_user(user_id: int, actor: Annotated[UserRecord, Depends(require_admin)]):
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    if not users_store.delete_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True}


@router.get("/platform")
def admin_get_platform(_: Annotated[UserRecord, Depends(require_admin)]):
    return load_platform()


@router.put("/platform")
def admin_put_platform(_: Annotated[UserRecord, Depends(require_admin)], body: PlatformPatchBody):
    patch: dict[str, Any] = {}
    if body.kline_sdk is not None:
        patch["kline_sdk"] = body.kline_sdk
    if patch:
        save_platform_merge(**patch)
    return load_platform()


@router.get("/data-assets")
def admin_data_assets(_: Annotated[UserRecord, Depends(require_admin)]):
    """查看本地数据资产、缓存与配置文件的统一索引。"""
    return data_asset_manager.scan_data_assets()


@router.post("/data-assets/refresh")
def admin_refresh_data_assets(_: Annotated[UserRecord, Depends(require_admin)]):
    """重新扫描 backend/data 并刷新 backend/data/_managed/catalog.json。"""
    return data_asset_manager.refresh_catalog_from_disk()


@router.get("/company-fundamentals/status")
def admin_company_fundamentals_status(_: Annotated[UserRecord, Depends(require_admin)]):
    """查看上市公司结构化数据库状态、源文件和最近导入批次。"""
    return company_fundamentals_store.status()


@router.post("/company-fundamentals/import-local-2025")
async def admin_import_company_fundamentals_2025(
    _: Annotated[UserRecord, Depends(require_admin)],
    body: FundamentalsImportBody | None = None,
):
    """导入项目根目录的财务指标/管理层讨论/上市公司基本信息 2025 CSV。"""
    b = body or FundamentalsImportBody()
    return await asyncio.to_thread(
        company_fundamentals_store.import_local_2025_files,
        force=b.force,
    )


@router.post("/company-universe/refresh")
async def admin_company_universe_refresh(
    _: Annotated[UserRecord, Depends(require_admin)],
    body: CompanyUniverseRefreshBody | None = None,
):
    """
    从东方财富 F10 拉取公司概况并写入 `company_basic_universe.json`。
    与「股票列表」自选股 profile 分离：用于全市场预缓存，接口展示时对空白字段回退到本缓存。
    """
    b = body or CompanyUniverseRefreshBody()
    if b.symbols:
        targets = [s.strip().upper() for s in b.symbols if (s or "").strip()]
    else:
        all_syms = company_universe_service.all_listed_symbols()
        targets = all_syms[b.offset : b.offset + b.limit]
    if not targets:
        raise HTTPException(status_code=400, detail="无可用代码：请先执行 POST /api/stocks/refresh 生成本地 A 股列表")
    return await company_universe_service.refresh_symbols(
        targets,
        skip_fresh_days=b.skip_fresh_days,
    )
