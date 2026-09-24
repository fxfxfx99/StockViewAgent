"""雪球可选补充接口（公司信息栏、讨论摘要等）。"""
from __future__ import annotations

import asyncio
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.deps.auth import get_current_user
from app.services import company_updates, xueqiu_pipeline
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/xueqiu", tags=["xueqiu"])


def _comments_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not re.fullmatch(r"[0-9]{6}\.(SS|SH|SZ|BJ)", symbol):
        raise ValueError("仅支持 A 股代码，例如 600519.SS、000001.SZ")
    return symbol[:-3] + ".SS" if symbol.endswith(".SH") else symbol


class CommentsRefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=32)
    mode: Literal["latest", "previous_day"] = "latest"

@router.get("/comments")
def get_comments(
    user: Annotated[UserRecord, Depends(get_current_user)],
    symbol: str = Query(..., min_length=1, max_length=32),
):
    """读取当前账户的评论精选与任务进度，不发起雪球或模型请求。"""
    from app.services import xueqiu_comments_service

    try:
        sym = _comments_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return xueqiu_comments_service.get_snapshot(user.id, sym)


@router.post("/comments/refresh", status_code=202)
def refresh_comments(
    body: CommentsRefreshBody,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """后台抓取最多 200 条讨论并由当前账户的大模型筛选；重复任务合并。"""
    from app.services import xueqiu_comments_service

    try:
        sym = _comments_symbol(body.symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return xueqiu_comments_service.enqueue_refresh(user.id, sym, mode=body.mode, source="manual")


@router.get("/company")
async def get_xueqiu_company(
    user: Annotated[UserRecord, Depends(get_current_user)],
    symbol: str = Query(..., description="Yahoo 格式 A 股代码，如 600519.SS"),
    force: bool = Query(False, description="主动更新；同标的最短间隔 30 秒"),
):
    """公司简介、公告和新闻：雪球优先、公开源补充、定期缓存更新。"""
    sym = symbol.strip().upper()
    return await asyncio.to_thread(company_updates.get_company_bundle, sym, force)


@router.get("/bundle")
async def get_xueqiu_bundle(
    user: Annotated[UserRecord, Depends(get_current_user)],
    symbol: str = Query(..., description="Yahoo 格式 A 股代码，如 600519.SS"),
):
    """雪球聚合：行情片段、讨论、热门用户等（非主 K 线源）。"""
    sym = symbol.strip().upper()
    return await asyncio.to_thread(xueqiu_pipeline.run_bundle, sym)
