"""市场研判：全球公开指数/商品期货及授权宏观行业行情。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps.auth import get_current_user
from app.services import macro_tushare_service, macro_workspace

router = APIRouter(prefix="/api/macro", tags=["macro"], dependencies=[Depends(get_current_user)])


@router.get("/catalog")
def get_macro_catalog():
    return macro_workspace.catalog()


@router.get("/indices")
def get_indices():
    return {"items": macro_workspace.indices()}


@router.get("/collection-status")
def get_collection_status():
    return macro_workspace.collection_status()


@router.get("/index-kline")
async def get_index_kline(
    index_key: str = Query("SH_COMP", max_length=32),
    range_param: str = Query("1y", alias="range", pattern="^(3mo|6mo|1y|2y|5y)$"),
    interval: str = Query("1d", pattern="^(1d|1wk|1mo)$"),
    refresh: bool = Query(False),
):
    try:
        return await macro_workspace.index_kline(index_key, range_param, interval, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, "公开市场行情来源暂不可用，请稍后重试") from exc


@router.get("/kline")
async def get_macro_kline(
    series_id: str = Query(..., description="见 GET /api/macro/catalog 中 id"),
    range_param: str = Query("1y", alias="range", pattern="^(3mo|6mo|1y|2y|5y)$"),
):
    try:
        return await macro_workspace.macro_kline(series_id, range_param=range_param)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail="宏观行情源暂不可用，请稍后重试") from e


@router.get("/sw-daily/indices")
def get_sw_daily_indices(
    trade_date: str | None = Query(None, description="YYYYMMDD，不传则自动回溯最近有数据的一日"),
):
    """申万行业指数列表（单日 sw_daily 全量 ts_code + name），见 doc_id=327。"""
    try:
        return macro_tushare_service.fetch_sw_daily_indices(trade_date=trade_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Tushare: {e!s}") from e


@router.get("/sw-daily/kline")
def get_sw_daily_kline(
    ts_code: str = Query(..., description="申万行业指数代码，如 801010.SI"),
    range_param: str = Query("1y", alias="range", pattern="^(3mo|6mo|1y|2y|5y)$"),
):
    """申万行业指数日线 K 线（sw_daily）。"""
    try:
        return macro_tushare_service.fetch_sw_daily_kline(ts_code, range_param=range_param)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Tushare: {e!s}") from e
