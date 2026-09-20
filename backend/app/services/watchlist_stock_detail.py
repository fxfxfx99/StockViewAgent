"""股票列表标的：详情（扩展行情 + 板块 + 本地公司简介）。

日 K 线图由 ``kline_pipeline`` / ``GET /api/market/kline`` 提供；本模块负责公司资料与行情摘要。
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.services import eastmoney_market, market_data_resilience
from app.storage import profile_store, universe_company_store

# 东财接口串行间隔，降低并发被断开概率
_EM_GAP = 0.38
# 扩展快照单独外层重试：内层 httpx 重试在同一连接上较密，东财偶发直接断流时换一轮间隔更稳
_SNAPSHOT_OUTER_ATTEMPTS = 3


async def _fetch_extended_quote_outer(sym: str) -> dict[str, Any]:
    last: BaseException | None = None
    for i in range(_SNAPSHOT_OUTER_ATTEMPTS):
        try:
            return await eastmoney_market.fetch_extended_quote_snapshot(sym)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < _SNAPSHOT_OUTER_ATTEMPTS - 1:
                await asyncio.sleep(0.9 + i * 0.35)
    assert last is not None
    raise last


async def build_stock_detail(symbol: str) -> dict[str, Any]:
    sym = symbol.strip().upper()
    secid = eastmoney_market.yahoo_symbol_to_secid(sym)
    if not secid:
        return {"symbol": sym, "error": "仅支持 A 股：6 位代码 + .SS/.SH/.SZ/.BJ"}

    entry = profile_store.get_entry(sym)
    merged = profile_store.merge_display(entry)
    merged = universe_company_store.merge_into_display(merged, sym)
    profile = {
        "name": merged.get("name") or "",
        "org_name": merged.get("org_name") or "",
        "main_business": merged.get("main_business") or "",
        "industry": merged.get("industry") or "",
        "intro": merged.get("intro") or "",
    }

    market: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    partial_errors: list[str] = []

    try:
        market = await market_data_resilience.fetch_quote_snapshot_resilient(sym)
        if market.get("served_from_cache"):
            partial_errors.append(f"行情快照：实时源不可用，已使用缓存（{market.get('cache_saved_at') or '未知时间'}）")
    except Exception as e:  # noqa: BLE001
        partial_errors.append(f"行情快照：{e!s}")

    await asyncio.sleep(_EM_GAP)

    try:
        blocks = await market_data_resilience.fetch_stock_blocks_resilient(secid, sym)
        if any(row.get("_served_from_cache") for row in blocks):
            saved_at = next((row.get("_cache_saved_at") for row in blocks if row.get("_cache_saved_at")), "")
            partial_errors.append(f"所属板块：实时源不可用，已使用缓存（{saved_at or '未知时间'}）")
    except Exception as e:  # noqa: BLE001
        partial_errors.append(f"所属板块：{e!s}")

    return {
        "symbol": sym,
        "profile": profile,
        "market": market,
        "blocks": blocks,
        "partial_errors": partial_errors,
        "data_source": "行情快照/板块：东方财富 push2、slist（失败自动切本地最近成功缓存）；公司简介：本地 watchlist_profiles（日 K 见 /api/market/kline）",
    }
