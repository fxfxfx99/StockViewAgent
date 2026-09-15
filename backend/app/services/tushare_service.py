"""Tushare Pro：stock_basic（doc_id=25）、daily（doc_id=27）。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from app.config import settings

_pro_cache: tuple[str, str, object] | None = None


def _get_pro():
    global _pro_cache
    token = (settings.effective_tushare_token or "").strip()
    data_url = (settings.tushare_data_api_url or "").strip()
    if not token:
        _pro_cache = None
        return None
    if _pro_cache and _pro_cache[0] == token and _pro_cache[1] == data_url:
        return _pro_cache[2]
    import tushare as ts

    pro = ts.pro_api(token)
    if data_url:
        # 自建/镜像网关（与官方 ts.pro_api(token); pro._DataApi__http_url = "..." 一致）
        pro._DataApi__http_url = data_url.rstrip("/")
    _pro_cache = (token, data_url, pro)
    return pro


def is_configured() -> bool:
    return bool((settings.effective_tushare_token or "").strip())


def yahoo_symbol_to_ts_code(symbol: str) -> str | None:
    """600519.SS -> 600519.SH；000001.SZ -> 000001.SZ；920xxx.BJ -> 920xxx.BJ。"""
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    code, suf = s.rsplit(".", 1)
    code = code.zfill(6)
    if not code.isdigit():
        return None
    if suf == "SS":
        return f"{code}.SH"
    if suf == "SZ":
        return f"{code}.SZ"
    if suf == "BJ":
        return f"{code}.BJ"
    return None


def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:  # noqa: BLE001
            return str(v)
    return v


def _row_to_json(row: pd.Series) -> dict[str, Any]:
    return {str(k): _cell(v) for k, v in row.items()}


def fetch_stock_basic(ts_code: str) -> tuple[dict[str, Any] | None, str | None]:
    pro = _get_pro()
    if not pro:
        return None, "未配置 TUSHARE_TOKEN"
    try:
        df = pro.stock_basic(ts_code=ts_code, list_status="L")
        if df is None or df.empty:
            df = pro.stock_basic(ts_code=ts_code)
        if df is None or df.empty:
            return None, "stock_basic 无返回"
        return _row_to_json(df.iloc[0]), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def fetch_daily_bars(ts_code: str, max_rows: int = 60) -> tuple[list[dict[str, Any]], str | None]:
    pro = _get_pro()
    if not pro:
        return [], "未配置 TUSHARE_TOKEN"
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=max(400, int(max_rows * 1.65)))).strftime("%Y%m%d")
    try:
        df = pro.daily(ts_code=ts_code, start_date=start, end_date=end)
        if df is None or df.empty:
            return [], None
        df = df.sort_values("trade_date", ascending=False).head(max_rows)
        out: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            out.append(_row_to_json(row))
        return out, None
    except Exception as e:  # noqa: BLE001
        return [], str(e)


def fetch_kline_bundle(yahoo_symbol: str, range_param: str = "1y", interval: str = "1d") -> dict[str, Any]:
    """把 Tushare Pro daily 标准化为本系统 K 线格式，仅用于公开源失败后的日线兜底。"""
    if interval not in {"1d", "daily", "day"}:
        raise ValueError("Tushare 兜底当前仅支持日线")
    code = yahoo_symbol_to_ts_code(yahoo_symbol)
    if not code:
        raise ValueError("无法转换为 Tushare 股票代码")
    rows_by_range = {"3mo": 80, "6mo": 150, "1y": 280, "2y": 550, "5y": 1300, "10y": 2600, "max": 5000}
    max_rows = rows_by_range.get(range_param, 550)
    bars, err = fetch_daily_bars(code, max_rows=max_rows)
    if err:
        raise RuntimeError(err)
    if not bars:
        raise ValueError("Tushare daily 无返回")
    candles = []
    for row in reversed(bars):
        date_text = str(row.get("trade_date") or "")
        if len(date_text) != 8:
            continue
        ts = int(datetime.strptime(date_text, "%Y%m%d").timestamp())
        candles.append({"t": ts, "o": _cell(row.get("open")), "h": _cell(row.get("high")), "l": _cell(row.get("low")), "c": _cell(row.get("close")), "v": _cell(row.get("vol")), "amount": _cell(row.get("amount"))})
    if len(candles) < 2:
        raise ValueError("Tushare 可用 K 线不足")
    return {"symbol": yahoo_symbol.upper(), "interval": "1d", "range": range_param, "candles": candles,
            "kline_source": "tushare_pro_fallback", "data_as_of": candles[-1]["t"]}


def fetch_and_pack_for_yahoo_symbol(yahoo_sym: str) -> tuple[dict[str, Any] | None, str | None]:
    """拉取 basic + 日线，写入 profile_store 用的结构。"""
    ts_code = yahoo_symbol_to_ts_code(yahoo_sym)
    if not ts_code:
        return None, "无法解析 Tushare ts_code（需形如 600519.SS）"

    basic, err_b = fetch_stock_basic(ts_code)
    bars, err_d = fetch_daily_bars(ts_code, max_rows=60)

    errs = [x for x in (err_b, err_d) if x]
    err_msg = "；".join(errs) if errs else None
    if basic is None and not bars:
        return None, err_msg or "无数据"

    payload: dict[str, Any] = {
        "ts_code": ts_code,
        "basic": basic,
        "daily_bars": bars,
        "daily_recent": bars[:5] if bars else [],
    }
    return payload, err_msg
