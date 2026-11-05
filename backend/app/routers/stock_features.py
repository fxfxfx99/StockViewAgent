from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps.auth import get_current_user
from app.services import stock_features as service
from app.storage import stock_feature_store as store
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("/{symbol}/features")
def stock_features(symbol: str, _: Annotated[UserRecord, Depends(get_current_user)]):
    try:
        return service.read(symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{symbol}/features/history")
def stock_feature_history(symbol: str, _: Annotated[UserRecord, Depends(get_current_user)],
                          limit: int = Query(default=12, ge=1, le=52),
                          version_id: str | None = Query(default=None, max_length=64)):
    try:
        if version_id:
            snapshot = store.get_snapshot(symbol, version_id=version_id)
            if snapshot is None:
                raise HTTPException(404, "未找到该股票的特征版本")
            return {"snapshot": snapshot}
        return {"history": store.history(symbol, limit=limit), "checks": store.checks(symbol, limit=limit)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{symbol}/features/refresh")
async def refresh_stock_features(symbol: str, _: Annotated[UserRecord, Depends(get_current_user)]):
    try:
        return await service.durable_call(service.refresh, symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
