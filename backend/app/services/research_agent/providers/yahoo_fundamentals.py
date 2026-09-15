"""Yahoo quoteSummary 扩展字段（估值等），带超时与空数据兜底。"""
from __future__ import annotations

from typing import Any

import httpx

from app.services.yahoo_market import UA, QUOTE_URL


def _num_raw(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, dict):
        raw = v.get("raw")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_quote_fundamentals_sync(symbol: str, timeout: float = 22.0) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    out: dict[str, Any] = {
        "symbol": sym,
        "trailing_pe": None,
        "forward_pe": None,
        "peg": None,
        "price_to_book": None,
        "enterprise_to_ebitda": None,
        "market_cap": None,
        "dividend_yield": None,
        "eps_ttm": None,
        "revenue_growth": None,
        "raw_error": None,
    }
    if not sym:
        out["raw_error"] = "empty symbol"
        return out
    modules = "summaryDetail,defaultKeyStatistics,financialData"
    try:
        with httpx.Client(timeout=timeout, headers={"User-Agent": UA}) as client:
            r = client.get(QUOTE_URL.format(symbol=sym), params={"modules": modules})
            if r.status_code != 200:
                out["raw_error"] = f"HTTP {r.status_code}"
                return out
            data = r.json()
    except Exception as e:  # noqa: BLE001
        out["raw_error"] = str(e)
        return out

    results = (data.get("quoteSummary") or {}).get("result") or []
    if not results:
        out["raw_error"] = "no result"
        return out
    r0 = results[0]
    sd = r0.get("summaryDetail") or {}
    dks = r0.get("defaultKeyStatistics") or {}
    fd = r0.get("financialData") or {}

    out["trailing_pe"] = _num_raw(sd.get("trailingPE"))
    out["forward_pe"] = _num_raw(sd.get("forwardPE"))
    out["peg"] = _num_raw(dks.get("pegRatio"))
    out["price_to_book"] = _num_raw(sd.get("priceToBook"))
    out["enterprise_to_ebitda"] = _num_raw(dks.get("enterpriseToEbitda"))
    out["market_cap"] = _num_raw(sd.get("marketCap"))
    out["dividend_yield"] = _num_raw(sd.get("dividendYield"))
    out["eps_ttm"] = _num_raw(dks.get("trailingEps"))
    rev = fd.get("revenueGrowth")
    out["revenue_growth"] = _num_raw(rev)
    return out
