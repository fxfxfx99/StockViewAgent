"""Local cache projection, bounded maintenance and inspectable version differences."""
from __future__ import annotations

import asyncio
import difflib
import json
import logging

from app.config import settings
from app.services import stock_features
from app.storage import knowledge_cache_jobs as jobs, knowledge_cache_store as cache, stock_feature_store

logger = logging.getLogger(__name__)
SCOPES = ("all", "stocks", "relevance", "prompts")


def _compact_snapshot(snapshot):
    """Readable projection; original quotes/price samples remain in source DB.

    Exclude repeated source prose and per-feature copies of it. Keep all actual
    feature values, their source IDs and the immutable source version identity.
    """
    result = {key: snapshot.get(key) for key in ("schema_version", "stock_code", "name", "version_id", "content_hash", "as_of", "created_at", "coverage")}
    result["features"] = [{k: row[k] for k in ("id", "category", "label", "name", "value", "unit", "status", "as_of", "source_ids", "missing") if k in row} for row in snapshot.get("features", [])]
    result["factor_links"] = [{k: v for k, v in row.items() if k != "evidence"} for row in snapshot.get("factor_links", [])]
    result["evidence_sources"] = [{**{k: row[k] for k in ("source_id", "kind", "title", "url", "report_date", "observed_at", "published_at", "availability") if k in row},
                                   "excerpt": str(row.get("text") or row.get("quote") or "")[:600]} for row in snapshot.get("evidence_sources", [])]
    stats = snapshot.get("stats") or {}
    result["stats"] = {"market_behavior": stats.get("market_behavior"),
                       "event_reactions": {k: v for k, v in (stats.get("event_reactions") or {}).items() if k != "items"}}
    result["source_record"] = {"database": "stock_features.sqlite3", "version_id": snapshot["version_id"],
                               "note": "此文件为紧凑可重建视图；完整原文与逐事件行情保留在个股特征数据库的对应版本。"}
    return result


def sync_stock(symbol, *, reason="本地资料同步", actor_id=None):
    symbol = stock_feature_store.symbol_key(symbol)
    snapshot = stock_features.get_snapshot(symbol)
    if snapshot is None:
        # The current job will publish both projections; do not recursively
        # enqueue a second per-stock job during a first-time full build.
        result = stock_features.refresh(symbol, sync_knowledge=False)
        snapshot = result.get("snapshot")
    if snapshot is None:
        raise ValueError("该股票尚无可用特征版本")
    return cache.put_document("stock_features", symbol, _compact_snapshot(snapshot), reason=reason, actor_id=actor_id,
                              provenance={"source": "stock_features.sqlite3", "source_version": snapshot["version_id"],
                                          "source_hash": snapshot.get("content_hash"), "title": snapshot.get("name") or symbol,
                                          "rebuildable": True, "projection_version": 1})


def _symbols():
    from app.services import a_share_stocks
    from app.storage import watchlist_store
    symbols = {s for row in a_share_stocks.load_stocks() if (s := row.get("symbol") or row.get("yahoo_symbol"))}
    symbols.update(watchlist_store.load_all_symbols_union())
    # Include removed/unlisted tickers that already have durable feature history.
    if (settings.data_dir / "stock_features.sqlite3").exists():
        with stock_feature_store.connection() as conn:
            symbols.update(row[0] for row in conn.execute("SELECT symbol FROM feature_refresh"))
    normalized = set()
    for symbol in symbols:
        try:
            normalized.add(stock_feature_store.symbol_key(symbol))
        except ValueError:
            continue
    return sorted(normalized)


def enqueue(scope="all", symbol=None, *, actor_id=None, reason="手动重建", periodic=False):
    if scope not in SCOPES:
        raise ValueError("无效重建范围")
    symbol = stock_feature_store.symbol_key(symbol) if symbol else None
    if symbol and scope == "prompts":
        raise ValueError("提示词为全局配置，不按股票重建")
    items = (["__prompts__"] if scope in {"all", "prompts"} else []) + ([] if scope == "prompts" else [symbol] if symbol else _symbols())
    result = jobs.enqueue(scope, symbol, items, actor_id=actor_id, reason=reason, periodic=periodic)
    return {**result, "worker_enabled": bool(settings.enable_knowledge_cache)} if result else None


def status():
    return {**jobs.status(), "worker_enabled": bool(settings.enable_knowledge_cache),
            "auto_sync_enabled": bool(settings.enable_knowledge_cache and settings.enable_scheduler)}


def overview():
    from app.services import prompt_registry
    prompt_registry.ensure_registry()
    return {**cache.overview(), "maintenance": status(), "schedule_note": "个股周更新后同步；每日核对本地目录，提示词变更即时生效。"}


def document(kind, key, revision_id=None, *, include_diff=True):
    value = cache.get_document(kind, key, revision_id)
    if value is None:
        raise FileNotFoundError("未找到知识条目或版本")
    if include_diff:
        identifier = value["metadata"]["revision_id"]
        previous_meta = cache.previous_revision(kind, key, identifier)
        previous = cache.get_document(kind, key, previous_meta["revision_id"]) if previous_meta else None
        def lines(content):
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2)
            return text.splitlines()
        before = lines(previous["content"]) if previous else []
        after = lines(value["content"])
        diff = list(difflib.unified_diff(before[:10000], after[:10000],
                                       fromfile="上一版本" if previous else "新建", tofile="所选版本", lineterm="", n=2))
        text = "\n".join(diff[:300])
        value["diff"] = {"from_revision": previous["metadata"]["revision_id"] if previous else None,
                         "to_revision": identifier, "text": text[:80000],
                         "truncated": len(diff) > 300 or len(text) > 80000 or max(len(before), len(after)) > 10000}
    return value


def _process(job, item):
    from app.services import news_relevance_knowledge, prompt_registry
    if not jobs.renew(job):
        return False
    error = None
    try:
        if item["symbol"] == "__prompts__":
            prompt_registry.ensure_registry()
        else:
            if job["scope"] in {"all", "stocks"}:
                sync_stock(item["symbol"], reason=job["reason"], actor_id=job["actor_id"])
            if job["scope"] in {"all", "relevance"}:
                # Stock source reads may take time. Verify ownership again before
                # the next independent publication, outside any registry lock.
                if not jobs.renew(job):
                    return False
                news_relevance_knowledge.ensure_stock_knowledge(item["symbol"], force=True, actor_id=job["actor_id"])
    except Exception as exc:
        if not jobs.renew(job):
            return False
        error = "knowledge_sync_failed"
        logger.warning("知识缓存同步失败 kind=%s type=%s", job["scope"], type(exc).__name__)
        try:
            cache.record_event("prompts" if item["symbol"] == "__prompts__" else "stock_features" if job["scope"] == "stocks" else "news_relevance",
                               "system_prompt" if item["symbol"] == "__prompts__" else item["symbol"], "sync_failed",
                               reason="同步未完成，已有有效版本保留", details={"job_id": job["id"], "error_code": error})
        except Exception:
            pass
    return jobs.complete_item(job, item["ordinal"], error_code=error)


async def run_batch(limit=10):
    if not await stock_features.durable_call(stock_features._resource_ready):
        return {"status": "resource_wait", "processed": 0}
    job = await stock_features.durable_call(jobs.claim)
    if not job:
        return {"status": "idle", "processed": 0}
    processed = 0
    try:
        items = await stock_features.durable_call(jobs.pending, job, limit=limit)
        for item in items:
            if not await stock_features.durable_call(_process, job, item):
                break
            processed += 1
    finally:
        await stock_features.durable_call(jobs.release, job)
    return {"status": "processed", "processed": processed, "job_id": job["id"]}


async def worker_loop():
    from app.services import prompt_registry
    await asyncio.sleep(5)
    counter = 0
    while True:
        try:
            if counter % 12 == 0:
                await stock_features.durable_call(prompt_registry.ensure_registry)
                if settings.enable_scheduler:
                    await stock_features.durable_call(enqueue, periodic=True, reason="每日本地知识核对")
            await run_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("知识缓存维护暂缓 type=%s", type(exc).__name__)
        counter += 1
        await asyncio.sleep(5)
