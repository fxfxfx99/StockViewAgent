"""雪球可选补充接口（公司信息栏、讨论摘要等）。"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.deps.auth import get_current_user
from app.services import xueqiu_pipeline
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/xueqiu", tags=["xueqiu"])


@router.get("/company")
async def get_xueqiu_company(
    user: Annotated[UserRecord, Depends(get_current_user)],
    symbol: str = Query(..., description="Yahoo 格式 A 股代码，如 600519.SS"),
):
    """雪球公司信息：F10 简介、最近大事件、个股新闻。"""
    sym = symbol.strip().upper()
    return await asyncio.to_thread(xueqiu_pipeline.run_company_bundle, sym)


@router.get("/bundle")
async def get_xueqiu_bundle(
    user: Annotated[UserRecord, Depends(get_current_user)],
    symbol: str = Query(..., description="Yahoo 格式 A 股代码，如 600519.SS"),
):
    """雪球聚合：行情片段、讨论、热门用户等（非主 K 线源）。"""
    sym = symbol.strip().upper()
    return await asyncio.to_thread(xueqiu_pipeline.run_bundle, sym)
