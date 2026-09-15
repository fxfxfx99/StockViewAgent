"""
将多源新闻合并写入本地库：国际 RSS、A 股快讯、36氪/虎嗅 RSS。
供「关联新闻」按标的聚合；入库前按当前股票列表打 matched_symbols。
"""
from __future__ import annotations

import time
from typing import Any

from app.schemas.news import normalize_news
from app.services import china_tech_media_feeds, cn_market_news, news_watchlist_matcher, rss_fetcher
from app.services.news_dedup import deduplicate_news
from app.storage import news_store, watchlist_store


async def sync_all_sources_to_archive(
    symbols: list[str] | None = None,
    *,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    """
    拉取并合并：按标的 RSS 聚合、东方财富/新浪 A 股快讯、36氪+虎嗅 RSS。
    转载归并后按当前股票列表写入 match_evidence。
    """
    if lookback_days is not None and not 1 <= lookback_days <= 30:
        raise ValueError("lookback_days must be between 1 and 30")
    requested = symbols if symbols is not None else watchlist_store.load_all_symbols_union()
    sym_list = list(dict.fromkeys(str(s).strip().upper() for s in requested if str(s).strip()))
    if not sym_list:
        return {
            "upserted": 0,
            "merged_unique": 0,
            "symbols_queried": [],
            "archive": news_store.stats(),
        }

    rss_items = await rss_fetcher.fetch_news_for_symbols(sym_list)
    cn_pack = await cn_market_news.fetch_cn_headlines(limit=120, major_first=True)
    cn_items = cn_pack.get("items") or []
    tech_pack = await china_tech_media_feeds.fetch_tech_media_feeds(limit=100, per_feed=50)
    tech_items = tech_pack.get("items") or []

    cutoff = time.time() - lookback_days * 86400 if lookback_days is not None else None
    normalized: list[dict[str, Any]] = []
    for group in (rss_items, cn_items, tech_items):
        for item in group:
            if not isinstance(item, dict):
                continue
            row = normalize_news(item)
            if not row:
                continue
            if cutoff is not None:
                try:
                    stamp = float(row.get("published_ts"))
                except (TypeError, ValueError):
                    continue
                if stamp < cutoff or stamp > time.time():
                    continue
            normalized.append(row)

    watchlist, hints = news_watchlist_matcher.load_watchlist_match_hints()
    enriched = []
    for row in deduplicate_news(normalized):
        evidence = news_watchlist_matcher.article_match_evidence(row, watchlist, hints)
        row["matched_symbols"] = sorted(evidence)
        row["match_evidence"] = evidence
        enriched.append(row)
    n = news_store.upsert_many(enriched)
    st = news_store.stats()
    return {
        "upserted": n,
        "merged_unique": len(enriched),
        "symbols_queried": sym_list,
        "lookback_days": lookback_days,
        "source_counts": {
            "rss_and_symbol": len(rss_items),
            "cn_headlines": len(cn_items),
            "tech_media": len(tech_items),
        },
        "archive": st,
    }
