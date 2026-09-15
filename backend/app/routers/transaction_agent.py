from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps.auth import get_current_user
from app.deps.auth import require_admin
from app.services import strategy_knowledge_service, transaction_agent_service
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/transaction-agent", tags=["transaction-agent"])


@router.get("/strategies")
def transaction_agent_strategies(_: Annotated[UserRecord, Depends(get_current_user)]):
    """TransactionAgent 策略清单与本地资源状态。"""
    return transaction_agent_service.resources_status()


@router.get("/knowledge/status")
def transaction_agent_knowledge_status(_: Annotated[UserRecord, Depends(get_current_user)]):
    """策略知识库状态、来源数量与分策略覆盖。"""
    return strategy_knowledge_service.status()


@router.post("/knowledge/ingest-local")
def transaction_agent_knowledge_ingest_local(_: Annotated[UserRecord, Depends(require_admin)]):
    """导入已内置到后端资源目录的 TransactionAgent 本地策略笔记与内置来源目录。"""
    return strategy_knowledge_service.ingest_local_transaction_agent_kb()


@router.post("/knowledge/refresh-public")
async def transaction_agent_knowledge_refresh_public(
    _: Annotated[UserRecord, Depends(require_admin)],
    limit: int = Query(default=8, ge=1, le=30),
    retry_forbidden: bool = Query(default=False, description="是否重试已返回 403 的来源"),
):
    """抓取公开来源的短摘录并生成策略知识条目；不保存整篇文章或版权书籍全文。"""
    try:
        return await strategy_knowledge_service.refresh_public_sources(limit=limit, retry_forbidden=retry_forbidden)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"刷新公开策略知识失败：{exc!s}") from exc


@router.post("/knowledge/strengthen")
def transaction_agent_knowledge_strengthen(
    _: Annotated[UserRecord, Depends(require_admin)],
    strategy: str | None = Query(default=None, description="策略中文名；不传则强化全部策略"),
):
    """将已采集来源合成为策略强化规则，供多策略观点引用。"""
    if strategy:
        return strategy_knowledge_service.synthesize_strategy(strategy)
    return strategy_knowledge_service.strengthen_all_strategies()


@router.get("/views/{symbol}")
async def transaction_agent_views(
    symbol: str,
    _: Annotated[UserRecord, Depends(get_current_user)],
    strategy: str | None = Query(default=None, description="策略 id 或中文名；不传则返回全部策略"),
    as_of: str | None = Query(default=None, description="时间节点 YYYY-MM-DD；截取该日及之前数据再生成观点"),
):
    """按本系统数据接口生成 TransactionAgent 多策略交易观点。"""
    try:
        return await transaction_agent_service.generate_strategy_views(symbol, strategy=strategy, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"生成策略观点失败：{exc!s}") from exc
