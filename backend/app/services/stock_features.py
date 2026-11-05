"""Weekly issuer-specific features, built from archived public evidence only."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import shutil
import time
from zoneinfo import ZoneInfo

from app.config import settings
from app.storage import stock_feature_store as store

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def next_weekly_due(now):
    local = datetime.fromtimestamp(now, SHANGHAI)
    target = local.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=(5 - local.weekday()) % 7)
    if target <= local:
        target += timedelta(days=7)
    return target.timestamp()


def get_snapshot(symbol, as_of=None):
    snapshot = store.get_snapshot(symbol, as_of=as_of)
    if snapshot:
        checks = store.checks(symbol, as_of=as_of, version_id=snapshot["version_id"], limit=1)
        snapshot["last_checked_at"] = checks[0]["checked_at"] if checks else snapshot["created_at"]
    return snapshot


def get_snapshot_identity(symbol, as_of=None):
    return store.snapshot_identity(symbol, as_of=as_of)


def response(symbol, *, refresh_status=None):
    symbol = store.symbol_key(symbol)
    state = store.state(symbol)
    now = time.time()
    snapshot = get_snapshot(symbol)
    pending = not state.get("last_success")
    status = ("in_progress" if state.get("lease_until", 0) > now else
              "failed" if state.get("error_code") else "pending" if pending else "ready")
    return {"stock_code": symbol, "snapshot": snapshot, "history": store.history(symbol), "checks": store.checks(symbol),
            "status": status, "refresh_status": refresh_status,
            "schedule": {"enabled": bool(settings.enable_stock_features and settings.enable_scheduler),
                         "cadence": "weekly", "timezone": "Asia/Shanghai", "weekday": 6, "hour": 9,
                         "next_due_at": store.iso(state.get("next_due") or now),
                         "last_attempt_at": store.iso(state.get("last_attempt")),
                         "last_success_at": store.iso(state.get("last_success")),
                         "last_error": state.get("error_code"),
                         "scope": "本地全市场名单、所有自选和已访问股票", "uses_model": False,
                         "note": "每周六09:00分批更新；首次建立和停机错过后补跑，来源缺失会保留缺口。"}}


def read(symbol):
    symbol = store.symbol_key(symbol)
    store.register([symbol])
    return response(symbol)


def refresh(symbol, *, force=True, sync_knowledge=True):
    from app.services.stock_feature_engine import build_snapshot

    symbol = store.symbol_key(symbol)
    now = time.time()
    token, status = store.claim(symbol, now=now, force=force)
    if token:
        try:
            snapshot = build_snapshot(symbol)
            completed = time.time()
            if ((snapshot.get("coverage") or {}).get("source_errors") or
                    (not snapshot.get("features") and (get_snapshot(symbol) or {}).get("features"))):
                store.fail(symbol, token=token, now=completed, error_code="source_evidence_unavailable")
                status = "failed"
            else:
                _, status = store.publish(symbol, snapshot, token=token, now=completed,
                                          next_due=next_weekly_due(completed))
        except Exception as exc:
            # Preserve previous successful versions, never expose provider paths
            # or raw errors from source files through the public API.
            logger.warning("个股特征构建失败 symbol=%s type=%s", symbol, type(exc).__name__)
            store.fail(symbol, token=token, now=time.time())
            status = "failed"
    if sync_knowledge and status in {"updated", "unchanged"}:
        try:
            from app.services import knowledge_cache_manager
            if settings.enable_knowledge_cache:
                knowledge_cache_manager.enqueue("all", symbol, reason="个股特征更新后的知识同步")
        except Exception as exc:
            # Projection failure cannot roll back committed feature evidence.
            # Daily reconciliation will retry any cache work missed here.
            logger.warning("个股知识同步待重试 symbol=%s type=%s", symbol, type(exc).__name__)
    return response(symbol, refresh_status=status)


def register_universe():
    from app.services import a_share_stocks
    from app.storage import watchlist_store

    raw = [row.get("yahoo_symbol") or row.get("symbol") for row in a_share_stocks.load_stocks()]
    raw.extend(watchlist_store.load_all_symbols_union())
    symbols = []
    for value in raw:
        if value:
            try:
                symbols.append(store.symbol_key(value))
            except ValueError:
                continue
    if symbols:
        store.register(symbols)
    return len(set(symbols))


async def durable_call(fn, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Repeated cancellation must not release a writer's lease/lifetime
            # lock while its underlying synchronous operation is still active.
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()  # Consume a failure even when the caller was cancelled.
        raise asyncio.CancelledError
    return task.result()


def _resource_ready():
    from app.services import collector_resources
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return (not collector_resources.snapshot()["pressure"] and
            shutil.disk_usage(settings.data_dir).free >= 1024 ** 3)


async def run_batch(*, limit=10):
    if not await durable_call(_resource_ready):
        return {"status": "resource_wait", "processed": 0}
    symbols = await durable_call(store.due_symbols, limit=limit)
    results = []
    for symbol in symbols:
        result = await durable_call(refresh, symbol, force=False)
        results.append({"stock_code": symbol, "status": result["refresh_status"]})
    return {"status": "completed", "processed": len(results), "results": results}


async def worker_loop():
    await asyncio.sleep(15)
    registered_at = 0
    while True:
        try:
            if time.time() - registered_at >= 3600:
                await durable_call(register_universe)
                registered_at = time.time()
            await run_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("个股特征周更新暂缓 type=%s", type(exc).__name__)
        await asyncio.sleep(30)
