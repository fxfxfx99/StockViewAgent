"""
将多源新闻合并写入本地库：国际 RSS、A 股快讯、36氪/虎嗅 RSS。
供「关联新闻」按标的聚合；入库前按当前股票列表打 matched_symbols。
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import os
import stat
import time
from typing import Any

from app.schemas.news import normalize_news
from app.config import settings
from app.security.user_context import current_user_id
from app.services import china_tech_media_feeds, cn_market_news, news_watchlist_matcher, rss_fetcher
from app.services.news_dedup import deduplicate_news
from app.storage import news_store, watchlist_store


@contextmanager
def _collection_lock():
    """One public news collection across API workers, startup and scheduler."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(settings.data_dir / "news-feed-sync.lock",
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("新闻同步锁文件无效")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


async def _durable_write(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancellation cannot stop a SQLite writer thread. Retain the process
        # lock until it exits, so the next collector cannot overlap its writes.
        # A second cancellation (for example shutdown after a request timeout)
        # must not cancel this drain and release ownership ahead of the thread.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Retrieve a failed writer while preserving cancellation.
        raise


def _skipped(reason, lookback_days):
    return {"status": "skipped", "skipped": reason, "upserted": 0,
            "records_received": 0, "records_added": 0, "merged_unique": 0,
            "symbols_queried": [], "lookback_days": lookback_days, "failed_sources": [],
            "source_counts": {"rss_and_symbol": 0, "cn_headlines": 0, "tech_media": 0}}


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
    with _collection_lock() as held:
        if not held:
            return _skipped("already_running", lookback_days)
        from app.services import collector_resources, data_collection
        resource = await asyncio.to_thread(collector_resources.snapshot, data_collection.get_config()["max_memory_mb"])
        if resource["pressure"]:
            return {**_skipped("high_memory", lookback_days), "resources": resource}
        return await _sync_locked(symbols, lookback_days=lookback_days)


async def _sync_locked(symbols, *, lookback_days):
    requested = symbols if symbols is not None else watchlist_store.load_all_symbols_union()
    sym_list = list(dict.fromkeys(str(s).strip().upper() for s in requested if str(s).strip()))
    # Public headlines and broad RSS do not depend on owning any stocks. Empty
    # symbol lists only disable per-symbol searches; shared collection still runs.

    from app.services.runtime_telemetry import record_collection
    uid = current_user_id.get() or 0
    counts = {}
    media_counts = {}
    cutoff = time.time() - lookback_days * 86400 if lookback_days is not None else None
    watchlist, hints = news_watchlist_matcher.load_watchlist_match_hints()
    canonical_ids = set()
    saved = {"records_added": 0}
    archive_error = None
    rss_count = 0

    async def archive_batch(items):
        nonlocal archive_error
        normalized = []
        for item in items:
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
                if not cutoff <= stamp <= time.time():
                    continue
            normalized.append(row)
        enriched = []
        for row in deduplicate_news(normalized):
            evidence = news_watchlist_matcher.article_match_evidence(row, watchlist, hints)
            row["matched_symbols"] = sorted(evidence)
            row["match_evidence"] = evidence
            enriched.append(row)
        batch_stats = {}
        try:
            await _durable_write(news_store.upsert_many, enriched, collection_stats=batch_stats)
        except Exception as exc:
            archive_error = exc
            raise
        # The store deduplicates against earlier batches/sources and writes each
        # canonical id back into the item. Retain ids, never whole old articles.
        canonical_ids.update(row["id"] for row in enriched)
        saved["records_added"] += batch_stats.get("records_added", 0)

    async def archive_rss(items):
        nonlocal rss_count
        rss_count += len(items)
        await archive_batch(items)

    # Provider failures are independent: a failed RSS search must not discard
    # healthy public headlines or technology feeds from this cycle.
    outcomes = await asyncio.gather(
        rss_fetcher.fetch_news_for_symbols(sym_list, collection_stats=counts, collector=archive_rss),
        cn_market_news.fetch_cn_headlines(limit=120, major_first=True, record_activity=False),
        china_tech_media_feeds.fetch_tech_media_feeds(limit=100, per_feed=50, record_activity=False, collection_stats=media_counts),
        return_exceptions=True,
    )
    failed_sources = [name for name, result in zip(("rss_and_symbol", "cn_headlines", "tech_media"), outcomes)
                      if isinstance(result, BaseException)]
    rss_items = outcomes[0] if isinstance(outcomes[0], list) else []
    cn_pack = outcomes[1] if isinstance(outcomes[1], dict) else {}
    tech_pack = outcomes[2] if isinstance(outcomes[2], dict) else {}
    received = (counts.get("records_received", rss_count + len(rss_items))
                + cn_pack.get("records_received", len(cn_pack.get("items") or []))
                + media_counts.get("records_received", tech_pack.get("records_received", 0)))
    cn_items = cn_pack.get("items") or []
    # The display payload includes old media cache entries; archive only rows
    # explicitly identified by the collector as coming from this network call.
    tech_items = tech_pack.get("network_items") or []

    try:
        if archive_error is not None:
            raise archive_error
        # Remaining provider lists are bounded. RSS was already committed in
        # small batches; this also accepts list-returning compatibility adapters.
        for group in (rss_items, cn_items, tech_items):
            for offset in range(0, len(group), 60):
                await archive_batch(group[offset:offset + 60])
    except Exception:
        record_collection("news_archive", "public_news", records_received=received, status="failed", error_code="storage_failed", user_id=uid)
        raise
    record_collection("news_archive", "public_news", records_received=received, user_id=uid, **saved,
                      status="failed" if failed_sources else "success",
                      error_code="partial_source_failure" if failed_sources else None)
    st = news_store.stats()
    return {
        "status": "partial" if failed_sources and canonical_ids else "failed" if failed_sources else "success",
        "upserted": len(canonical_ids),
        "records_received": received,
        "records_added": saved.get("records_added", 0),
        "merged_unique": len(canonical_ids),
        "symbols_queried": sym_list,
        "lookback_days": lookback_days,
        "failed_sources": failed_sources,
        "source_counts": {
            "rss_and_symbol": rss_count + len(rss_items),
            "cn_headlines": len(cn_items),
            "tech_media": len(tech_items),
        },
        "archive": st,
    }
