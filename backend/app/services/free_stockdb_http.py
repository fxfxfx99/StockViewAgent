"""Optional free-stockdb local HTTP adapter.

free-stockdb ships as a Windows local service. This module treats it as an
optional HTTP data source so macOS/Linux users can point StockViewAgent at a
reachable Windows/VM/CrossOver service without making the app depend on it.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.services.eastmoney_market import _A_SHARE_RE, _build_metrics
from app.services.market_time import SHANGHAI_TZ, market_timestamp

_SOURCE_ID = "free_stockdb_local"
_SOURCE_LABEL = "free-stockdb 本地 HTTP（可选）"
_RANGE_DAYS: dict[str, int] = {
    "1d": 10,
    "5d": 15,
    "1mo": 45,
    "3mo": 120,
    "6mo": 220,
    "1y": 420,
    "2y": 760,
    "5y": 1900,
    "10y": 3800,
    "ytd": 380,
    "max": 9500,
}


def is_enabled() -> bool:
    return bool(settings.free_stockdb_enabled)


def _base_url() -> str:
    return (settings.free_stockdb_base_url or "http://127.0.0.1:7899").rstrip("/")


def _timeout() -> float:
    try:
        return max(0.5, float(settings.free_stockdb_timeout_sec or 3.0))
    except (TypeError, ValueError):
        return 3.0


def _num(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _code_from_symbol(symbol: str) -> str:
    m = _A_SHARE_RE.match((symbol or "").strip().upper())
    if not m:
        raise ValueError("free-stockdb 仅支持 A 股 6 位代码")
    return m.group(1)


def _date_range(range_param: str) -> tuple[str, str]:
    today = datetime.now(SHANGHAI_TZ)
    if (range_param or "").strip().lower() == "ytd":
        start = datetime(today.year, 1, 1)
    else:
        days = _RANGE_DAYS.get((range_param or "").strip().lower(), 420)
        start = today - timedelta(days=days)
    # free-stockdb examples use start<end; add a day to include today.
    end = today + timedelta(days=1)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _request_json(t: str) -> Any:
    params = {"cmd": "get", "t": t}
    with httpx.Client(timeout=_timeout()) as client:
        resp = client.get(_base_url() + "/", params=params)
        resp.raise_for_status()
        return resp.json()


def _flatten_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "rows", "result"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        if any(k in payload for k in ("open", "close", "high", "low", "date")):
            return [payload]
        rows: list[dict[str, Any]] = []
        for value in payload.values():
            if isinstance(value, dict):
                rows.append(value)
            elif isinstance(value, list):
                rows.extend(r for r in value if isinstance(r, dict))
        return rows
    return []


def _date_to_ts(value: Any) -> int | None:
    s = str(value or "").strip()
    for fmt, width in (("%Y%m%d", 8), ("%Y-%m-%d", 10), ("%Y%m%d%H%M%S", 14), ("%Y-%m-%d %H:%M:%S", 19)):
        try:
            dt = datetime.strptime(s[:width], fmt)
            return market_timestamp(dt)
        except ValueError:
            continue
    return None


def _rows_to_candles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in rows:
        ts = _date_to_ts(row.get("date") or row.get("time"))
        o = _num(row.get("open"))
        h = _num(row.get("high"))
        low = _num(row.get("low"))
        c = _num(row.get("close"))
        if ts is None or o is None or h is None or low is None or c is None:
            continue
        volume = _num(row.get("volume"))
        amount = _num(row.get("amount"))
        turnover = _num(row.get("turnover"))
        candle: dict[str, Any] = {
            "t": ts,
            "o": round(o, 4),
            "h": round(h, 4),
            "l": round(low, 4),
            "c": round(c, 4),
            "v": int(volume or 0),
            "amount": round(amount or 0.0, 2),
            "turnover_rate": round(turnover, 4) if turnover is not None else None,
        }
        candles.append(candle)
    candles.sort(key=lambda x: x["t"])
    return candles


def _quote_from_last_row(row: dict[str, Any]) -> dict[str, Any]:
    pe = _num(row.get("pe_ttm"))
    return {
        "name": str(row.get("name") or "").strip(),
        "prev_close": _num(row.get("pre_close")),
        "total_market_cap_yuan": _num(row.get("total_mv")),
        "float_market_cap_yuan": _num(row.get("float_mv")),
        "pe_ttm": round(pe, 4) if pe is not None else None,
        "turnover_rate_snapshot": _num(row.get("turnover")),
        "shares_total": int(_num(row.get("total_share")) or 0) or None,
    }


def health_check() -> dict[str, Any]:
    if not is_enabled():
        return {
            "ok": False,
            "enabled": False,
            "source": _SOURCE_ID,
            "detail": "FREE_STOCKDB_ENABLED=false，未启用可选本地数据源",
            "base_url": _base_url(),
        }
    try:
        code = "600519"
        start, end = _date_range("1mo")
        payload = _request_json(f"日k:{code}:{start}<{end}")
        rows = _flatten_rows(payload)
        return {
            "ok": bool(rows),
            "enabled": True,
            "source": _SOURCE_ID,
            "detail": "连接成功" if rows else "连接成功但未返回日 K 数据",
            "base_url": _base_url(),
            "row_count": len(rows),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "enabled": True,
            "source": _SOURCE_ID,
            "detail": str(exc)[:500] or exc.__class__.__name__,
            "base_url": _base_url(),
        }


def fetch_kline_bundle_sync(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    if not is_enabled():
        raise RuntimeError("free-stockdb 本地数据源未启用")
    if (interval or "").strip().lower() not in {"1d", "5d"}:
        raise RuntimeError("free-stockdb HTTP 兜底当前仅接入日 K")
    sym = (symbol or "").strip().upper()
    code = _code_from_symbol(sym)
    start, end = _date_range(range_param)
    payload = _request_json(f"日k:{code}:{start}<{end}")
    rows = _flatten_rows(payload)
    candles = _rows_to_candles(rows)
    if not candles:
        raise RuntimeError("free-stockdb 未返回可用 K 线")
    last_row = rows[-1] if rows else {}
    metrics = _build_metrics(sym, candles, _quote_from_last_row(last_row), "1d")
    metrics["data_source"] = _SOURCE_LABEL
    notes = list(metrics.get("data_source_notes") or [])
    notes.append("K 线来自 free-stockdb 本地 HTTP；该源需单独运行 Windows 本地服务")
    metrics["data_source_notes"] = list(dict.fromkeys(notes))
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": "1d",
        "candles": candles,
        "metrics": metrics,
        "kline_source": _SOURCE_ID,
    }
