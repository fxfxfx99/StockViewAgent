"""
多源 RSS 聚合：按标的源 + 全市场/宏观源 + 东财个股公告。
各站点可用性可能随时间变化；失败源自动跳过。
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, quote_plus

import feedparser
import httpx

from app.config import settings
from app.services import eastmoney_stock_news, news_watchlist_matcher
from app.services.news_fetch_http import DEFAULT_NEWS_TIMEOUT, NEWS_UA, fetch_bytes_with_retry

USER_AGENT = NEWS_UA
FETCH_CONCURRENCY = 2
SYMBOL_BATCH_SIZE = 2


def _code_from_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if "." in s:
        return s.split(".", 1)[0]
    return s


def _google_news_cn_url(symbol: str) -> str:
    code = _code_from_symbol(symbol)
    q = quote_plus(f"{code} A股 股票")
    return f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"


def _bing_news_cn_url(symbol: str) -> str:
    code = _code_from_symbol(symbol)
    q = quote(f"{code} A股", safe="")
    return f"https://www.bing.com/news/search?q={q}&format=rss"


GLOBAL_FEEDS: list[tuple[str, str]] = [
    ("新浪 · 财经焦点", "http://rss.sina.com.cn/news/allnews/finance.xml"),
    ("新浪 · 财经滚动", "http://rss.sina.com.cn/roll/finance/hot_roll.xml"),
    ("新浪 · 股票要闻", "https://rss.sina.com.cn/roll/stock/hot_roll.xml"),
    ("金融界 · 财经", "https://rss.jrj.com.cn/rss/finance.xml"),
]


def _item_id(link: str, title: str) -> str:
    h = hashlib.sha256(f"{link}|{title}".encode("utf-8", errors="ignore")).hexdigest()[:16]
    return h


def _parse_feed(
    content: bytes,
    source: str,
    symbol_hint: str | None,
    max_entries: int = 35,
) -> list[dict[str, Any]]:
    parsed = feedparser.parse(content)
    out: list[dict[str, Any]] = []
    for e in parsed.entries[:max_entries]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip() or "#"
        summary = (e.get("summary") or e.get("description") or "").strip()
        published = e.get("published") or e.get("updated") or ""
        ts = None
        if hasattr(e, "published_parsed") and e.published_parsed:
            try:
                ts = int(time.mktime(e.published_parsed))
            except (OverflowError, ValueError):
                ts = None
        out.append(
            {
                "id": _item_id(link, title),
                "title": title,
                "link": link,
                "summary": summary[:2000],
                "published": published,
                "published_ts": ts,
                "source": source,
                "symbol_hint": symbol_hint,
            }
        )
    return out


def _symbol_feed_jobs(symbols: list[str]) -> list[tuple[str, str, str | None]]:
    jobs: list[tuple[str, str, str | None]] = []
    for sym in symbols:
        s = sym.strip().upper()
        jobs.append((_google_news_cn_url(s), f"Google 资讯 · {s}", s))
        jobs.append((_bing_news_cn_url(s), f"Bing 资讯 · {s}", s))
    return jobs


async def fetch_news_for_symbols(
    symbols: list[str], *, collection_stats: dict | None = None,
    collector: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
) -> list[dict[str, Any]]:
    """Download small batches; ``collector`` archives every batch, independently
    of the display limit. Collector mode returns no accumulated display list.
    """
    sym_list = list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))
    _, name_hints = news_watchlist_matcher.load_watchlist_match_hints()
    counts = collection_stats if collection_stats is not None else {}
    counts["records_received"] = 0
    cap = max(80, min(settings.news_feed_max_items, 500))
    by_id: dict[str, dict[str, Any]] = {}

    async def accept(rows):
        counts["records_received"] += len(rows)
        # A response cap also needs per-item caps: one oversized title must not
        # survive in every slot of the retained display list across many feeds.
        rows = [{**row, **{field: str(row[field])[:limit]
                          for field, limit in (("title", 1000), ("summary", 2000), ("published", 200), ("source", 200))
                          if field in row}}
                for row in rows if len(str(row.get("link") or "")) <= 2000]
        if collector is not None:
            if rows:
                await collector(rows)
            return
        for row in rows:
            old = by_id.get(row["id"])
            if old is None or (row.get("published_ts") or 0) > (old.get("published_ts") or 0):
                by_id[row["id"]] = row
        if len(by_id) > cap:
            newest = sorted(by_id.values(), key=lambda row: row.get("published_ts") or 0, reverse=True)[:cap]
            by_id.clear()
            by_id.update((row["id"], row) for row in newest)

    async with httpx.AsyncClient(
        timeout=DEFAULT_NEWS_TIMEOUT, headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=FETCH_CONCURRENCY, max_keepalive_connections=FETCH_CONCURRENCY),
    ) as client:
        async def fetch_one(job):
            url, label, symbol = job
            body = await fetch_bytes_with_retry(client, url)
            if not body:
                return []
            # Parse before returning: no raw response survives until another
            # symbol finishes. Only these small parsed snippets reach accept().
            return _parse_feed(body, label, symbol, max_entries=18 if symbol else 30)

        async def feeds(jobs):
            for offset in range(0, len(jobs), FETCH_CONCURRENCY):
                groups = await asyncio.gather(*(fetch_one(job) for job in jobs[offset:offset + FETCH_CONCURRENCY]))
                for rows in groups:
                    await accept(rows)

        await feeds([(url, label, None) for label, url in GLOBAL_FEEDS])
        for offset in range(0, len(sym_list), SYMBOL_BATCH_SIZE):
            batch = sym_list[offset:offset + SYMBOL_BATCH_SIZE]
            await feeds(_symbol_feed_jobs(batch))
            await accept(await eastmoney_stock_news.fetch_symbols_news_items(
                batch, limit_per_symbol=14, name_hints=name_hints))

    return sorted(by_id.values(), key=lambda row: row.get("published_ts") or 0, reverse=True)
