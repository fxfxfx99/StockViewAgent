"""Bounded public-data collection, persistent jobs and shared worker ownership."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
import re
import shutil
import stat
import time
import uuid

from app.config import settings
from app.security.user_context import current_user_id
from app.storage import data_collection_store as store

logger = logging.getLogger(__name__)
_SYMBOL = re.compile(r"^\d{6}\.(?:SS|SZ|BJ)$")
_BUILTINS = (
    {"id": "public_kline", "name": "A股日线行情", "category": "market", "url": "https://web.ifzq.gtimg.cn/", "interval_minutes": 360,
     "description": "按股票轮询腾讯公开前复权日线，归档变动版本并预热行情缓存。"},
    {"id": "public_news", "name": "财经新闻与公司资讯", "category": "news", "url": "https://kuaixun.eastmoney.com/", "interval_minutes": 120,
     "description": "复用公开快讯、RSS和科技媒体归档，不自动调用模型。"},
    {"id": "public_company", "name": "公司简介与主营资料", "category": "company", "url": "https://emweb.securities.eastmoney.com/", "interval_minutes": 1440,
     "description": "轮询公开F10资料，保留本地观察到的版本。"},
    {"id": "public_capital_flow", "name": "个股资金流历史", "category": "capital_flow", "url": "https://push2his.eastmoney.com/", "interval_minutes": 360,
     "description": "按股票归档公开日资金流，并合并已有历史序列。"},
    {"id": "economic_calendar", "name": "全球经济事件日历", "category": "events", "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "interval_minutes": 60,
     "description": "更新五个官方日历源，保留事件修订和来源错误状态。"},
)


def catalog():
    from app.services.public_data_sources import SOURCE_CATALOG
    return [*map(dict, _BUILTINS), *[{**row, "interval_minutes": max(60, int(row.get("update_interval_seconds", 86400) / 60))} for row in SOURCE_CATALOG]]


def default_config():
    return {"enabled": True, "rag_enabled": True, "daily_job_limit": 120, "max_symbols_per_cycle": 10, "symbols": [], "use_watchlists": True,
            "min_free_disk_mb": 1024, "sources": {row["id"]: {"enabled": True, "interval_minutes": row["interval_minutes"]} for row in catalog()}}


def get_config():
    defaults = default_config()
    saved = store.get_state("config", {})
    return {**defaults, **saved, "sources": {**defaults["sources"], **saved.get("sources", {})}}


def update_config(patch):
    if not isinstance(patch, dict) or set(patch) - set(default_config()):
        raise ValueError("未知采集配置字段")

    def merge(saved):
        defaults = default_config()
        value = {**defaults, **(saved or {})}
        value["sources"] = {**defaults["sources"], **value.get("sources", {})}
        for key in ("enabled", "rag_enabled", "use_watchlists"):
            if key in patch:
                if type(patch[key]) is not bool:
                    raise ValueError("开关必须为布尔值")
                value[key] = patch[key]
        for key, low, high in (("daily_job_limit", 1, 1000), ("max_symbols_per_cycle", 1, 30), ("min_free_disk_mb", 128, 102400)):
            if key in patch:
                if type(patch[key]) is not int or not low <= patch[key] <= high:
                    raise ValueError(f"{key} 超出允许范围 {low}–{high}")
                value[key] = patch[key]
        if "symbols" in patch:
            if not isinstance(patch["symbols"], list) or len(patch["symbols"]) > 300 or any(not isinstance(s, str) or not _SYMBOL.fullmatch(s.upper()) for s in patch["symbols"]):
                raise ValueError("股票须为最多300个六位代码.SS/SZ/BJ")
            value["symbols"] = list(dict.fromkeys(s.upper() for s in patch["symbols"]))
        if "sources" in patch:
            changes = patch["sources"]
            if not isinstance(changes, dict) or set(changes) - set(defaults["sources"]):
                raise ValueError("未知数据来源")
            for sid, source in changes.items():
                if not isinstance(source, dict) or set(source) - {"enabled", "interval_minutes"}:
                    raise ValueError("无效来源设置")
                if "enabled" in source and type(source["enabled"]) is not bool:
                    raise ValueError("来源开关必须为布尔值")
                if "interval_minutes" in source and (type(source["interval_minutes"]) is not int or not 60 <= source["interval_minutes"] <= 43200):
                    raise ValueError("来源间隔须为60–43200分钟")
                value["sources"][sid] = {**value["sources"][sid], **source}
        return value

    return store.mutate_state("config", merge, {})


def sources():
    config = get_config()
    states = store.source_states()
    result = []
    for row in catalog():
        sid = row["id"]
        state = states.get(sid, {})
        interval = config["sources"][sid]["interval_minutes"] * 60
        retry_delay = min(21600, 60 * (2 ** min(state.get("failures", 0), 8)))
        last = state.get("last_attempt_at")
        next_run = datetime.fromisoformat(last).timestamp() + max(interval, retry_delay) if last else None
        result.append({**row, **config["sources"][sid], "status": "never", "records_received": 0, "records_added": 0,
                       "last_attempt_at": None, "last_success_at": None, "error_code": None, **state,
                       "next_run_at": store.iso(next_run) if next_run else None})
    return result


def enqueue(source_ids=None, *, scheduled=False):
    rows = sources()
    known = {row["id"] for row in rows}
    if source_ids is not None and (not isinstance(source_ids, list) or len(source_ids) > len(known) or any(not isinstance(sid, str) or sid not in known for sid in source_ids)):
        raise ValueError("请选择内置采集来源")
    selected = set(source_ids) if source_ids is not None else {row["id"] for row in rows if row["enabled"]}
    now = time.time()
    jobs = []
    for row in rows:
        if row["id"] not in selected:
            continue
        if scheduled and (not row["enabled"] or (row["next_run_at"] and datetime.fromisoformat(row["next_run_at"]).timestamp() > now)):
            continue
        jobs.append(store.enqueue(row["id"], {"scheduled": scheduled}, now=now))
    return {"status": "queued" if any(job["status"] in {"queued", "running"} for job in jobs) else "cooldown", "jobs": jobs}


def _symbols(source_id):
    config = get_config()
    pool = list(config["symbols"])
    if config["use_watchlists"]:
        # Read existing lists only: an intentionally empty list must stay empty.
        directory = settings.data_dir / "watchlists"
        if directory.exists() and not directory.is_symlink():
            for path in sorted(directory.glob("*.json")):
                if path.is_symlink():
                    continue
                try:
                    value = json.loads(path.read_text())
                    pool.extend(s for s in value.get("symbols", []) if isinstance(s, str) and _SYMBOL.fullmatch(s.upper()))
                except (OSError, ValueError, AttributeError):
                    continue
    pool = list(dict.fromkeys(s.upper() for s in pool))
    if not pool:
        return []
    selected = []

    def advance(cursor):
        start = int(cursor or 0) % len(pool)
        count = min(config["max_symbols_per_cycle"], len(pool))
        selected.extend(pool[(start + index) % len(pool)] for index in range(count))
        return (start + count) % len(pool)

    store.mutate_state("cursor:" + source_id, advance, 0)
    return selected


async def durable_call(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep the worker lease until a synchronous network/disk operation ends.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Cancellation must remain cancellation even when the draining thread
        # fails. Turning it into a normal error would let a stopping worker claim
        # more work while its caller believes it has cancelled the task.
        with suppress(Exception):
            task.result()
        raise


def _row(sid, symbol, identity, title, url, *, period=None, value=None, text="", metadata=None, category="market"):
    return {"source_id": sid, "source": next(row["name"] for row in _BUILTINS if row["id"] == sid),
            "upstream_id": f"{symbol}:{identity}", "title": title, "source_url": url, "category": category,
            "symbols": [symbol], "region": "CN", "period": period, "published_at": None,
            "value": value, "text": text, "metadata": metadata or {}}


async def collect_source(source_id):
    from app.services import public_data_sources
    if source_id in {row["id"] for row in public_data_sources.SOURCE_CATALOG}:
        rows = await public_data_sources.fetch_source(source_id)
        if not rows:
            return {"status": "failed", "error_code": "empty_source"}
        category = next(row["category"] for row in public_data_sources.SOURCE_CATALOG if row["id"] == source_id)
        return {"status": "success", **await durable_call(store.ingest, source_id, category, rows)}
    if source_id == "economic_calendar":
        from app.services import event_calendar_service
        output = await event_calendar_service.refresh(scheduled=True)
        results = [row for row in output.get("results", []) if not row.get("skipped")]
        failed = any(row.get("status") == "failed" for row in results)
        return {"status": "partial" if failed else "success" if results else "skipped",
                "records_received": sum(row.get("event_count", 0) for row in results if row.get("status") == "success"),
                "records_added": sum(row.get("revisions_added", 0) for row in results if row.get("status") == "success"),
                "error_code": "partial_source_failure" if failed else "source_cooldown" if not results else None}
    symbols = await durable_call(_symbols, source_id)
    if not symbols:
        return {"status": "skipped", "error_code": "empty_stock_pool"}
    if source_id == "public_news":
        from app.services.news_feed_sync import sync_all_sources_to_archive
        result = await sync_all_sources_to_archive(symbols, lookback_days=14)
        return {"status": "success" if result.get("records_received") else "failed",
                "records_received": result.get("records_received", 0), "records_added": result.get("records_added", 0),
                "error_code": None if result.get("records_received") else "empty_source"}
    from app.services import market_extra_http, company_profile_em
    from app.storage import kline_bundle_cache, market_history_cache
    category = next(row["category"] for row in _BUILTINS if row["id"] == source_id)
    counts = {"records_received": 0, "records_added": 0}
    failed = 0
    for symbol in symbols:
        try:
            if source_id == "public_kline":
                bundle = await durable_call(market_extra_http.fetch_kline_bundle_secondary_sync, symbol, "1y", "1d")
                rows = [_row(source_id, symbol, str(candle["t"]), f"{symbol} 日线行情", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                             period=datetime.fromtimestamp(candle["t"], timezone.utc).date().isoformat(), value=candle["c"],
                             text=f"{symbol} 前复权日线 OHLCV", metadata=candle) for candle in bundle.get("candles", [])]
            elif source_id == "public_capital_flow":
                values = await market_extra_http.fetch_stock_capital_flow(symbol)
                rows = [_row(source_id, symbol, item["trade_date"], f"{symbol} 资金流", "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                             period=item["trade_date"], value=item.get("main_net_inflow"), category=category,
                             text=f"{symbol} 主力净流入，公开订单口径不代表机构身份", metadata=item) for item in values]
            elif source_id == "public_company":
                company, error = await durable_call(company_profile_em.fetch_company_survey, symbol, timeout_sec=10, max_retries=0)
                if error or not any(company.values()):
                    raise ValueError("company_unavailable")
                rows = [_row(source_id, symbol, "profile", f"{symbol} 公司简介与主营资料", "https://emweb.securities.eastmoney.com/",
                             category=category, text="\n".join(f"{key}: {value}" for key, value in company.items() if value), metadata=company)]
            else:
                raise ValueError("unknown_source")
            if not rows:
                raise ValueError("empty_source")
            result = await durable_call(store.ingest, source_id, category, rows)
            for key in counts:
                counts[key] += result[key]
            # Validate and archive a nonempty batch before replacing a fallback
            # cache. An empty/error upstream response must retain the last good
            # chart and incrementally accumulated history.
            if source_id == "public_kline":
                await durable_call(kline_bundle_cache.save_bundle, symbol, "1y", "1d", bundle)
            elif source_id == "public_capital_flow":
                await durable_call(market_history_cache.merge_save_series, "stock_capital_flow", symbol, values)
        except asyncio.CancelledError:
            raise
        except Exception:
            failed += 1
            logger.warning("公共数据采集未完成 source=%s symbol=%s", source_id, symbol)
        await asyncio.sleep(1)
    return {**counts, "status": "partial" if failed and counts["records_received"] else "failed" if failed else "success",
            "error_code": "partial_source_failure" if failed else None}


def _safe_error(exc):
    code = getattr(exc, "error_code", "source_unavailable")
    return code if isinstance(code, str) and re.fullmatch(r"(?:http_[45]\d{2}|timeout|network_error|response_too_large|invalid_payload|invalid_data|unknown_source)", code) else "source_unavailable"


def overview():
    from app.storage import data_asset_manager
    result = data_asset_manager.scan_data_assets()
    worker = store.get_state("worker", {})
    beat = worker.get("heartbeat_at")
    fresh = bool(beat and time.time() - datetime.fromisoformat(beat).timestamp() < 90)
    result["collection"] = {"config": get_config(), "sources": sources(), "jobs": store.list_jobs(20),
                            "datasets": store.dataset_stats(), "usage": store.usage(),
                            "worker": {**worker, "enabled": settings.enable_data_collection,
                                       "scheduler_enabled": settings.enable_scheduler, "running": fresh and worker.get("running", False)}}
    return result


@contextmanager
def _worker_lease():
    directory = os.open(settings.data_dir, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    descriptor = None
    try:
        descriptor = os.open(".data-collection.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("采集锁必须是无链接的普通文件")
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
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def _update_worker(owner, **patch):
    def update(current):
        if current.get("owner") != owner:
            return current
        return {**current, **patch, "heartbeat_at": store.iso()}
    return store.mutate_state("worker", update, {})


async def run_cycle(*, owner=None):
    owner = owner or uuid.uuid4().hex
    with _worker_lease() as owned:
        if not owned:
            return {"status": "another_worker"}
        token = current_user_id.set(0)
        heartbeat = None
        final_state = {"running": True, "phase": "idle", "source_id": None, "reason": None}
        try:
            await durable_call(store.recover_interrupted)
            config = get_config()
            await durable_call(store.set_state, "worker", {"heartbeat_at": store.iso(), "running": True,
                                                         "phase": "preparing", "pid": os.getpid(), "owner": owner})
            async def beat():
                while True:
                    await asyncio.sleep(15)
                    await durable_call(_update_worker, owner)
            heartbeat = asyncio.create_task(beat())
            allowed = [sid for sid, source in config["sources"].items() if source["enabled"]] if settings.enable_scheduler and config["enabled"] else []
            await durable_call(store.cancel_disallowed_scheduled, allowed)
            if settings.enable_scheduler and config["enabled"]:
                await durable_call(enqueue, scheduled=True)
            if shutil.disk_usage(settings.data_dir).free < config["min_free_disk_mb"] * 1024 * 1024:
                final_state.update(running=False, reason="low_disk_space")
                return {"status": "low_disk_space"}
            job = await durable_call(store.claim_job, config["daily_job_limit"])
            if job:
                await durable_call(_update_worker, owner, phase="collecting", source_id=job["source_id"])
                try:
                    result = await asyncio.wait_for(collect_source(job["source_id"]), timeout=240)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result = {"status": "failed", "error_code": _safe_error(exc)}
                    logger.warning("公共数据任务失败 source=%s type=%s", job["source_id"], type(exc).__name__)
                await durable_call(store.finish_job, job["id"], result)
                from app.services.runtime_telemetry import record_collection
                # News/calendar already emit precise provider collection events.
                if job["source_id"] not in {"public_news", "economic_calendar"}:
                    record_collection("data_center", job["source_id"], records_received=result.get("records_received", 0),
                                      records_added=result.get("records_added", 0), status="success" if result["status"] == "success" else "failed",
                                      error_code=result.get("error_code"), user_id=0)
            elif store.usage()["jobs_started"] >= config["daily_job_limit"]:
                final_state["reason"] = "daily_budget_exhausted"
            last_maintenance = store.get_state("last_maintenance", 0)
            if time.time() - last_maintenance > 3600:
                from app.storage import cache_lifecycle
                await durable_call(cache_lifecycle.run_maintenance)
                await durable_call(store.set_state, "last_maintenance", time.time())
            return {"status": "processed" if job else "idle"}
        except asyncio.CancelledError:
            final_state.update(running=False, reason="interrupted")
            raise
        except Exception:
            final_state.update(running=False, reason="worker_cycle_failed")
            raise
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await durable_call(_update_worker, owner, **final_state)
            finally:
                current_user_id.reset(token)


async def worker_loop():
    owner = uuid.uuid4().hex
    await asyncio.sleep(10)
    try:
        while True:
            try:
                await run_cycle(owner=owner)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("数据中心后台轮询失败")
            await asyncio.sleep(5)
    finally:
        await durable_call(_update_worker, owner, running=False, phase="idle", reason="stopped")
