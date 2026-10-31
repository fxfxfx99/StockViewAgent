"""Read-only, authenticated daily K-line practice data."""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps.auth import get_current_user
from app.services import kline_replay
from app.storage.users_store import UserRecord

router = APIRouter(prefix="/api/kline-replay", tags=["kline-replay"])


@router.get("/{symbol}")
async def replay_data(
    symbol: str,
    user: Annotated[UserRecord, Depends(get_current_user)],
    start_date: date | None = Query(None, description="区间开始日期，默认近两年"),
    end_date: date | None = Query(None, description="区间结束日期，含当日；仅完整日线"),
    refresh: bool = Query(False, description="重新获取历史数据"),
):
    try:
        return await kline_replay.fetch_replay(symbol, start_date, end_date, user_id=user.id, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except kline_replay.ReplayDataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
