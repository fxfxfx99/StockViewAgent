"""市场研判 · 宏观与行业：Tushare 指数/外汇/黄金/国际原油等。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps.auth import get_current_user
from app.services import macro_tushare_service

router = APIRouter(prefix="/api/macro", tags=["macro"], dependencies=[Depends(get_current_user)])


@router.get("/catalog")
def get_macro_catalog():
    """可展示的宏观序列清单（含 Tushare 文档 doc_id 引用）。"""
    return {
        "items": macro_tushare_service.catalog(),
        "index_basic_doc": "https://tushare.pro/document/2?doc_id=94",
        "token_hint": "优先使用当前账户 Tushare Token，再回退 integrations.json 或 .env",
    }


@router.get("/kline")
def get_macro_kline(
    series_id: str = Query(..., description="见 GET /api/macro/catalog 中 id"),
    range_param: str = Query("1y", alias="range", description="3mo,6mo,1y,2y,5y"),
):
    try:
        return macro_tushare_service.fetch_macro_kline(series_id, range_param=range_param)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Tushare: {e!s}") from e


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
    range_param: str = Query("1y", alias="range", description="3mo,6mo,1y,2y,5y"),
):
    """申万行业指数日线 K 线（sw_daily）。"""
    try:
        return macro_tushare_service.fetch_sw_daily_kline(ts_code, range_param=range_param)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Tushare: {e!s}") from e
