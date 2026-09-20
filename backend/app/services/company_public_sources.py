"""公司栏的公开资料补充；保留来源和获取时间，不把缓存伪装成新数据。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.services import company_profile_em, eastmoney_stock_news
from app.storage import news_store, profile_store, universe_company_store

logger = logging.getLogger(__name__)


def fetch_company(symbol: str) -> tuple[dict | None, str, int | None, str | None]:
    company, error = company_profile_em.fetch_company_survey(symbol, timeout_sec=10, max_retries=0)
    if not error and any(company.values()):
        return company, "东方财富", int(time.time()), None
    # 只读取公共自动资料；用户手动覆盖继续由前端单独合并。
    entries = [profile_store.get_entry(symbol), universe_company_store.get_symbol(symbol) or {}]
    entries.sort(key=lambda row: row.get("em_fetched_at") or 0, reverse=True)
    for entry in entries:
        auto = entry.get("auto") or {}
        if any(auto.values()):
            return auto, "本地公司资料", entry.get("em_fetched_at") or None, "公开公司资料更新暂时失败，已保留缓存"
    return None, "东方财富", None, "公开公司资料暂不可用，将自动重试"


def fetch_news(symbol: str, stock_name: str | None) -> tuple[list[dict], str, int | None, str | None]:
    try:
        items = asyncio.run(eastmoney_stock_news.fetch_symbol_news_items(
            symbol, limit=30, stock_name=stock_name, strict=True, timeout_sec=10, max_retries=0,
        ))
        return items, "东方财富", int(time.time()), None
    except Exception:
        logger.info("公开公司资讯暂不可用，读取本地新闻库 symbol=%s", symbol)
    items, _ = news_store.list_archive(limit=30, symbol=symbol, lookback_days=30)
    fetched_at = max((item.get("fetched_at") or 0 for item in items), default=0)
    return items, "本地新闻库", int(fetched_at) or None, "公开资讯更新暂时失败，将自动重试"


def as_feed_item(item: dict[str, Any]) -> dict[str, Any]:
    source = str(item.get("source") or "公开资讯")
    return {
        "id": item.get("id"),
        "title": item.get("title") or "",
        "text_excerpt": item.get("summary") or "",
        "created_at": item.get("published_ts") or item.get("published") or None,
        "fetched_at": item.get("fetched_at"),
        "url": item.get("link"),
        "source_label": source,
        "event_type": "公司公告" if "公告" in source else "",
    }
