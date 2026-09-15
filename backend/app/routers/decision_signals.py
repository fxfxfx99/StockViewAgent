from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from app.deps.auth import get_current_user
from app.services import decision_signal_service
from app.storage import decision_signal_store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/decision-signals", tags=["decision-signals"])

class StatusBody(BaseModel): status: str

@router.get("")
def list_items(user: Annotated[UserRecord, Depends(get_current_user)], symbol: str | None = None, status: str | None = None, limit: int = Query(100, ge=1, le=200)):
    return {"items": decision_signal_store.list_signals(user.id, symbol=symbol, status=status, limit=limit)}

@router.post("/generate/{symbol}")
async def generate(symbol: str, user: Annotated[UserRecord, Depends(get_current_user)]):
    try: return await decision_signal_service.generate_from_transaction_agent(user.id, symbol)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc

@router.post("/evaluate")
async def evaluate(user: Annotated[UserRecord, Depends(get_current_user)], symbol: str | None = None):
    return await decision_signal_service.evaluate(user.id, symbol=symbol)

@router.patch("/{signal_id}/status")
def patch_status(signal_id: str, body: StatusBody, user: Annotated[UserRecord, Depends(get_current_user)]):
    try: ok = decision_signal_store.update_status(user.id, signal_id, body.status)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
    if not ok: raise HTTPException(404, "信号不存在")
    return {"ok": True}
