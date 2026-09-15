from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.deps.auth import get_current_user
from app.services import stock_picker_agent_service
from app.storage import stock_picker_agent_store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/stock-picker-agents", tags=["stock-picker-agents"])

class AgentBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    definition: dict[str, Any] = Field(default_factory=dict)

class RunBody(BaseModel): symbols: list[str] = Field(default_factory=list, max_length=30)

@router.get("")
def list_agents(user: Annotated[UserRecord, Depends(get_current_user)]): return {"items": stock_picker_agent_store.list_agents(user.id)}

@router.post("")
def create_agent(body: AgentBody, user: Annotated[UserRecord, Depends(get_current_user)]): return stock_picker_agent_store.create_agent(user.id, body.model_dump())

@router.put("/{agent_id}")
def update_agent(agent_id: str, body: AgentBody, user: Annotated[UserRecord, Depends(get_current_user)]):
    out = stock_picker_agent_store.update_agent(user.id, agent_id, body.model_dump())
    if not out: raise HTTPException(404, "选股 Agent 不存在")
    return out

@router.delete("/{agent_id}")
def delete_agent(agent_id: str, user: Annotated[UserRecord, Depends(get_current_user)]):
    if not stock_picker_agent_store.delete_agent(user.id, agent_id): raise HTTPException(404, "选股 Agent 不存在")
    return {"ok": True}

@router.post("/{agent_id}/run")
async def run_agent(agent_id: str, body: RunBody, user: Annotated[UserRecord, Depends(get_current_user)]):
    try: return await stock_picker_agent_service.run_agent(user.id, agent_id, body.symbols)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc

@router.get("/runs/history")
def list_runs(user: Annotated[UserRecord, Depends(get_current_user)], limit: int = Query(30, ge=1, le=100)): return {"items": stock_picker_agent_store.list_runs(user.id, limit)}
