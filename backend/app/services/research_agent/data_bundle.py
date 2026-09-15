"""并发拉取多源数据，统一写入 fact bundle（供分析器与 LLM）。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.services import kline_pipeline, tushare_service
from app.services.research_agent.normalizer import is_a_share
from app.services.research_agent.providers.yahoo_fundamentals import fetch_quote_fundamentals_sync
from app.services.yahoo_market import fetch_kline_bundle
from app.storage import news_store
from app.services.issuer_rag_service import search_with_names


async def _safe_kline_ashare(symbol: str, log: dict[str, Any]) -> dict[str, Any]:
    try:
        bundle = await kline_pipeline.fetch_a_share_kline_with_fallbacks(symbol, "1y", "1d")
        log["data_sources_used"].append("eastmoney_kline_chain")
        return bundle
    except Exception as e:  # noqa: BLE001
        log["errors"].append(f"A 股 K 线: {e!s}")
        return {"symbol": symbol, "candles": [], "metrics": {}, "error": str(e)}


async def _safe_kline_yahoo(symbol: str, log: dict[str, Any]) -> dict[str, Any]:
    try:
        bundle = await fetch_kline_bundle(symbol, "1y", "1d")
        log["data_sources_used"].append("yahoo_chart")
        return bundle
    except Exception as e:  # noqa: BLE001
        log["errors"].append(f"Yahoo K 线: {e!s}")
        return {"symbol": symbol, "candles": [], "metrics": {}, "error": str(e)}


def _tushare_pack(symbol: str, log: dict[str, Any]) -> dict[str, Any] | None:
    if not tushare_service.is_configured():
        return None
    pack, err = tushare_service.fetch_and_pack_for_yahoo_symbol(symbol)
    if err:
        log["errors"].append(f"Tushare: {err}")
    if pack:
        log["data_sources_used"].append("tushare_pro")
    return pack


def _news(symbol: str, log: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        rows = news_store.list_recent_for_symbol(symbol, 28)
        if rows:
            log["data_sources_used"].append("news_sqlite")
        return rows
    except Exception as e:  # noqa: BLE001
        log["errors"].append(f"新闻库: {e!s}")
        return []


def _issuer_rag(symbol: str, query: str, log: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        chunks = search_with_names(symbol, query or "财报 业绩 风险 指引", top_k=6)
        if chunks:
            log["data_sources_used"].append("issuer_rag")
        return chunks
    except Exception as e:  # noqa: BLE001
        log["errors"].append(f"上传资料 RAG: {e!s}")
        return []


def _yahoo_fundamentals(symbol: str, log: dict[str, Any]) -> dict[str, Any]:
    if is_a_share(symbol):
        return {}
    try:
        f = fetch_quote_fundamentals_sync(symbol)
        if not f.get("raw_error"):
            log["data_sources_used"].append("yahoo_quote_summary")
        elif f.get("raw_error"):
            log["errors"].append(f"Yahoo 估值: {f.get('raw_error')}")
        return f
    except Exception as e:  # noqa: BLE001
        log["errors"].append(f"Yahoo 估值异常: {e!s}")
        return {}


class DataCollectionLog(dict[str, Any]):
    """data_sources_used: list[str]; errors: list[str]"""


async def collect_for_symbol(
    symbol: str,
    user_query: str,
    *,
    include_secondary: str | None = None,
) -> dict[str, Any]:
    log: dict[str, Any] = {"data_sources_used": [], "errors": [], "started_at": time.time()}
    sym = symbol.strip().upper()

    if is_a_share(sym):
        kline_task = _safe_kline_ashare(sym, log)
    else:
        kline_task = _safe_kline_yahoo(sym, log)

    kline = await kline_task

    tushare_task = asyncio.to_thread(_tushare_pack, sym, log)
    news_task = asyncio.to_thread(_news, sym, log)
    rag_task = asyncio.to_thread(_issuer_rag, sym, user_query, log)
    yf_task = asyncio.to_thread(_yahoo_fundamentals, sym, log)

    tushare_pack, news_rows, rag_chunks, yf = await asyncio.gather(tushare_task, news_task, rag_task, yf_task)

    secondary_kline: dict[str, Any] | None = None
    if include_secondary and include_secondary.strip().upper() != sym:
        s2 = include_secondary.strip().upper()
        if is_a_share(s2):
            secondary_kline = await _safe_kline_ashare(s2, log)
        else:
            secondary_kline = await _safe_kline_yahoo(s2, log)

    log["finished_at"] = time.time()

    metrics = (kline.get("metrics") or {}) if isinstance(kline, dict) else {}
    return {
        "primary_symbol": sym,
        "kline": kline,
        "kline_metrics": metrics,
        "tushare_pack": tushare_pack,
        "news": news_rows,
        "issuer_chunks": rag_chunks,
        "yahoo_fundamentals": yf,
        "secondary": (
            {
                "symbol": include_secondary.strip().upper(),
                "kline": secondary_kline,
                "metrics": (secondary_kline or {}).get("metrics") or {},
            }
            if include_secondary
            else None
        ),
        "collection_log": log,
    }
