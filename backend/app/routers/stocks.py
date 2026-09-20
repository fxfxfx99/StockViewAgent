import asyncio

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps.auth import get_current_user, require_admin
from app.services import a_share_stocks, issuer_basic_info_universe
from app.storage import company_fundamentals_store, universe_company_store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("/search")
async def stocks_search(
    q: str = Query(default="", min_length=1, description="中文简称或代码"),
    limit: int = Query(default=15, ge=1, le=50),
):
    # 大 CSV 首次解析在线程中执行，避免阻塞事件循环
    await asyncio.to_thread(issuer_basic_info_universe.ensure_cached_blocking)
    items = await asyncio.to_thread(a_share_stocks.search_stocks, q, limit)
    return {"items": items}


@router.get("/meta")
def stocks_meta():
    m = a_share_stocks.meta()
    m.update(issuer_basic_info_universe.cache_meta())
    return m


@router.post("/universe/rebuild")
async def stocks_universe_rebuild(_: Annotated[UserRecord, Depends(require_admin)]):
    """管理员从本地上传 CSV 重建股票列表（大文件可能耗时数分钟）。"""
    return await asyncio.to_thread(issuer_basic_info_universe.rebuild_universe_from_upload_blocking)


@router.post("/refresh")
def stocks_refresh(_: Annotated[UserRecord, Depends(require_admin)]):
    """管理员从东方财富拉取沪深京 A 股列表并覆盖本地文件。"""
    try:
        return a_share_stocks.fetch_and_save()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"拉取失败: {e!s}") from e


@router.get("/company-universe/status")
def stocks_company_universe_status(_: Annotated[UserRecord, Depends(get_current_user)]):
    """全市场公司基本信息（F10）本地缓存体量；与自选股 profile 中的变动信息分离存储。"""
    return universe_company_store.meta()


@router.get("/{symbol}/fundamentals")
def stocks_company_fundamentals(
    symbol: str,
    _: Annotated[UserRecord, Depends(get_current_user)],
):
    """读取结构化公司基础库中的最新基本信息、财务指标和管理层讨论记录。"""
    return company_fundamentals_store.latest_for_symbol(symbol)
