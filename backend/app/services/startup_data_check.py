"""启动后数据源自检与轻量预热。

任务在后台运行，不阻塞 FastAPI 启动；外部源失败只记录状态，避免把应用拖到不可用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.config import settings
from app.services import a_share_stocks, cn_headline_source_registry, kline_pipeline, market_extra_http, news_feed_sync
from app.storage import data_asset_manager, kline_bundle_cache, market_history_cache, news_store, watchlist_store

logger = logging.getLogger(__name__)

_STATE_FILE = "startup_data_check_state.json"
_DEFAULT_RANGE = "1y"
_DEFAULT_INTERVAL = "1d"
_RUN_LOCK = asyncio.Lock()
_BACKGROUND_TASK: asyncio.Task | None = None


def _state_path() -> Path:
    return settings.data_dir / _STATE_FILE


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    data_asset_manager.atomic_write_json(
        _state_path(),
        state,
        kind="startup_data_check",
        key="latest",
        source="startup",
        item_count=len(state.get("steps") or []),
    )


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]


async def _capture_step(name: str, fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        out = await fn()
        status = str(out.pop("status", "ok"))
        return {
            "name": name,
            "status": status,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            **out,
        }
    except Exception as exc:
        logger.warning("启动数据源检查失败 step=%s error=%s", name, exc)
        return {
            "name": name,
            "status": "error",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "error": _short_error(exc),
        }


async def _refresh_stock_list_if_stale() -> dict[str, Any]:
    meta = a_share_stocks.meta()
    count = int(meta.get("count") or 0)
    updated_at = int(meta.get("updated_at") or 0)
    ttl_sec = max(1, int(settings.startup_stock_list_ttl_hours)) * 3600
    stale = count <= 0 or updated_at <= 0 or (time.time() - updated_at) > ttl_sec
    if not stale:
        return {"status": "skipped", "reason": "fresh", "count": count, "updated_at": updated_at}
    out = await asyncio.to_thread(a_share_stocks.fetch_and_save)
    return {"status": "ok", "reason": "refreshed", **out}


async def _probe_news_sources() -> dict[str, Any]:
    out = await cn_headline_source_registry.run_daily_maintenance_if_due()
    status = "skipped" if out.get("skipped") else "ok"
    return {"status": status, **out}


async def _sync_news_archive() -> dict[str, Any]:
    try:
        out = await asyncio.wait_for(news_feed_sync.sync_all_sources_to_archive(), timeout=75.0)
        return {"status": "ok", "upserted": out.get("upserted", 0), "merged_unique": out.get("merged_unique", 0), "archive": out.get("archive")}
    except asyncio.TimeoutError:
        return {"status": "warning", "warning": "多源新闻同步超过75秒，保留已有归档并在下次启动重试", "archive": news_store.stats()}


async def _refresh_asset_catalog() -> dict[str, Any]:
    out = await asyncio.to_thread(data_asset_manager.refresh_catalog_from_disk)
    return {"status": "ok", **(out.get("totals") or {})}


async def _warm_major_index() -> dict[str, Any]:
    bundle = await asyncio.wait_for(
        kline_pipeline.fetch_major_index_kline_with_fallbacks("SH_COMP", _DEFAULT_RANGE, _DEFAULT_INTERVAL),
        timeout=45.0,
    )
    return {
        "status": "ok",
        "index_key": "SH_COMP",
        "kline_source": bundle.get("kline_source"),
        "bar_count": len(bundle.get("candles") or []),
    }


async def _warm_watchlist_market_data() -> dict[str, Any]:
    symbols = watchlist_store.load_all_symbols_union()
    limit = max(0, int(settings.startup_data_check_watchlist_limit))
    previous = load_state()
    previous_step = next((x for x in (previous.get("steps") or []) if x.get("name") == "watchlist_market_data"), {})
    cursor = int(previous_step.get("next_cursor") or 0)
    if symbols and cursor >= len(symbols):
        cursor = 0
    selected = (symbols[cursor : cursor + limit] if symbols else [])
    if len(selected) < limit and cursor > 0:
        selected += symbols[: max(0, limit - len(selected))]
    next_cursor = (cursor + len(selected)) % len(symbols) if symbols else 0
    if not selected:
        return {"status": "skipped", "reason": "empty_watchlist", "symbols_checked": 0}

    async def one_symbol(sym: str) -> dict[str, Any]:
        item: dict[str, Any] = {"symbol": sym}
        async def get_kline():
            try:
                bundle = await asyncio.wait_for(kline_pipeline.fetch_a_share_kline_with_fallbacks(sym, _DEFAULT_RANGE, _DEFAULT_INTERVAL), timeout=22.0)
                kline_bundle_cache.save_bundle(sym, _DEFAULT_RANGE, _DEFAULT_INTERVAL, bundle)
                return {"status": "ok", "source": bundle.get("kline_source"), "bar_count": len(bundle.get("candles") or [])}
            except Exception as exc:
                cached = kline_bundle_cache.load_bundle(sym, _DEFAULT_RANGE, _DEFAULT_INTERVAL)
                return {"status": "cached", "warning": _short_error(exc)} if cached else {"status": "error", "error": _short_error(exc)}
        async def get_flow():
            try:
                flows = await asyncio.wait_for(market_extra_http.fetch_stock_capital_flow(sym), timeout=10.0)
                cached = market_history_cache.merge_save_series("stock_capital_flow", sym, flows)
                return {"status": "ok", "rows": len(cached.get("items") or [])}
            except Exception as exc:
                cached = market_history_cache.load_series("stock_capital_flow", sym)
                return {"status": "cached", "rows": len(cached.get("items") or []), "warning": _short_error(exc)} if cached else {"status": "error", "error": _short_error(exc)}
        item["kline"], item["capital_flow"] = await asyncio.gather(get_kline(), get_flow())
        return item

    semaphore = asyncio.Semaphore(3)
    async def limited(sym: str) -> dict[str, Any]:
        async with semaphore:
            return await one_symbol(sym)
    rows = await asyncio.gather(*(limited(sym) for sym in selected))

    errors = sum(
        1
        for row in rows
        for key in ("kline", "capital_flow")
        if (row.get(key) or {}).get("status") == "error"
    )
    return {
        "status": "warning" if errors else "ok",
        "symbols_total": len(symbols),
        "symbols_checked": len(selected),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "errors": errors,
        "items": rows,
    }


async def run_startup_data_check() -> dict[str, Any]:
    async with _RUN_LOCK:
        return await _run_startup_data_check_locked()


def schedule_background_refresh() -> dict[str, Any]:
    """提交一次后台刷新；已有任务运行时去重，避免重复抓取和写缓存。"""
    global _BACKGROUND_TASK
    if (_BACKGROUND_TASK is not None and not _BACKGROUND_TASK.done()) or _RUN_LOCK.locked():
        return {"status": "already_running"}
    _BACKGROUND_TASK = asyncio.create_task(run_startup_data_check(), name="data-center-refresh")
    _BACKGROUND_TASK.add_done_callback(_consume_background_result)
    return {"status": "scheduled"}


def is_refresh_running() -> bool:
    return (_BACKGROUND_TASK is not None and not _BACKGROUND_TASK.done()) or _RUN_LOCK.locked()


def _consume_background_result(task: asyncio.Task) -> None:
    """读取后台任务结果，避免异常变成未回收的事件循环告警。"""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("手动数据中心后台刷新异常")


async def _run_startup_data_check_locked() -> dict[str, Any]:
    started_at = _now_iso()
    steps = [
        await _capture_step("stock_list", _refresh_stock_list_if_stale),
        await _capture_step("news_sources", _probe_news_sources),
        await _capture_step("news_archive", _sync_news_archive),
        await _capture_step("major_index_kline", _warm_major_index),
        await _capture_step("watchlist_market_data", _warm_watchlist_market_data),
        await _capture_step("data_asset_catalog", _refresh_asset_catalog),
    ]
    status = "ok"
    if any(step.get("status") == "error" for step in steps):
        status = "error"
    elif any(step.get("status") == "warning" for step in steps):
        status = "warning"
    state = {
        "status": status,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "steps": steps,
    }
    _save_state(state)
    return state


async def delayed_startup_data_check() -> None:
    delay = max(0.0, float(settings.startup_data_check_delay_sec))
    if delay:
        await asyncio.sleep(delay)
    try:
        state = await run_startup_data_check()
        logger.info("启动数据源检查完成 status=%s", state.get("status"))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("启动数据源检查异常")
