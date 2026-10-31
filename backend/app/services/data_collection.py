"""Bounded public-data collection, persistent jobs and shared worker ownership."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import fcntl
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
_MAX_JOBS_PER_CYCLE = 8
_STOCK_SOURCES = {"public_kline", "public_company", "public_capital_flow"}
_current_job = ContextVar("public_collection_job", default=None)
_BUILTINS = (
    {"id": "public_market", "name": "大盘指数与市场基线", "category": "market", "url": "https://web.ifzq.gtimg.cn/", "interval_minutes": 60,
     "description": "5 个 A 股宽基指数公开日线，归档并预热宏观工作台行情。"},
    {"id": "public_kline", "name": "A股日线行情", "category": "market", "url": "https://web.ifzq.gtimg.cn/", "interval_minutes": 360,
     "description": "腾讯公开前复权日线。"},
    {"id": "public_news", "name": "财经新闻与公司资讯", "category": "news", "url": "https://kuaixun.eastmoney.com/", "interval_minutes": 120,
     "description": "公开快讯、RSS 与科技媒体归档。"},
    {"id": "public_company", "name": "公司简介与主营资料", "category": "company", "url": "https://emweb.securities.eastmoney.com/", "interval_minutes": 1440,
     "description": "公开 F10 公司资料。"},
    {"id": "public_capital_flow", "name": "个股资金流历史", "category": "capital_flow", "url": "https://push2his.eastmoney.com/", "interval_minutes": 360,
     "description": "公开日资金流，含已积累的历史序列。"},
    {"id": "economic_calendar", "name": "全球经济事件日历", "category": "events", "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "interval_minutes": 60,
     "description": "五个官方日历源的经济事件及修订。"},
)


def catalog():
    from app.services.public_data_sources import SOURCE_CATALOG
    return [*map(dict, _BUILTINS), *[{**row, "interval_minutes": max(60, int(row.get("update_interval_seconds", 86400) / 60))} for row in SOURCE_CATALOG]]


def default_config():
    return {"enabled": True, "rag_enabled": True, "daily_job_limit": 120, "max_symbols_per_cycle": 10, "symbols": [], "use_watchlists": True,
            "min_free_disk_mb": 1024, "max_concurrent_jobs": 1, "max_memory_mb": 768, "job_timeout_seconds": 240,
            "overseas_enabled": settings.overseas_news_enabled,
            "overseas_poll_interval_sec": max(60, min(3600, settings.overseas_news_poll_interval_sec)),
            "sources": {row["id"]: {"enabled": True, "interval_minutes": row["interval_minutes"],
                        "schedule_mode": "daily" if row["id"] in _STOCK_SOURCES else "interval", "daily_at": "17:30"}
                        for row in catalog()}}


def _merged_config(saved):
    defaults = default_config()
    saved = saved or {}
    value = {**defaults, **saved, "sources": dict(defaults["sources"])}
    for sid, source in saved.get("sources", {}).items():
        if sid in defaults["sources"]:
            # An existing deployment keeps its established interval schedule.
            value["sources"][sid] = {**defaults["sources"][sid], **source, "schedule_mode": source.get("schedule_mode", "interval")}
    return value


def get_config():
    return _merged_config(store.get_state("config", {}))


def update_config(patch):
    if not isinstance(patch, dict) or set(patch) - set(default_config()):
        raise ValueError("未知采集配置字段")

    def merge(saved):
        defaults = default_config()
        value = _merged_config(saved)
        for key in ("enabled", "rag_enabled", "use_watchlists", "overseas_enabled"):
            if key in patch:
                if type(patch[key]) is not bool:
                    raise ValueError("开关必须为布尔值")
                value[key] = patch[key]
        for key, low, high in (("daily_job_limit", 1, 1000), ("max_symbols_per_cycle", 1, 30), ("min_free_disk_mb", 128, 102400),
                               ("max_concurrent_jobs", 1, 3), ("max_memory_mb", 128, 8192), ("job_timeout_seconds", 30, 600),
                               ("overseas_poll_interval_sec", 60, 3600)):
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
                if not isinstance(source, dict) or set(source) - {"enabled", "interval_minutes", "schedule_mode", "daily_at"}:
                    raise ValueError("无效来源设置")
                if "enabled" in source and type(source["enabled"]) is not bool:
                    raise ValueError("来源开关必须为布尔值")
                if "interval_minutes" in source and (type(source["interval_minutes"]) is not int or not 60 <= source["interval_minutes"] <= 43200):
                    raise ValueError("来源间隔须为60–43200分钟")
                if "schedule_mode" in source and (not isinstance(source["schedule_mode"], str) or source["schedule_mode"] not in {"interval", "daily"}):
                    raise ValueError("来源调度须为interval或daily")
                if "daily_at" in source and (not isinstance(source["daily_at"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", source["daily_at"])):
                    raise ValueError("每日采集时间须为北京时间HH:MM")
                value["sources"][sid] = {**value["sources"][sid], **source}
        return value

    return store.mutate_state("config", merge, {})


def sources():
    config = get_config()
    states = store.source_states()
    rounds = store.round_states()
    now = time.time()
    result = []
    for row in catalog():
        sid = row["id"]
        state = states.get(sid, {})
        interval = config["sources"][sid]["interval_minutes"] * 60
        round_state = rounds.get(sid, {})
        last = state.get("last_round_finished_at") or state.get("last_finished_at") or state.get("last_attempt_at")
        source_config = config["sources"][sid]
        scheduled_for = None
        if source_config["schedule_mode"] == "daily":
            hour, minute = map(int, source_config["daily_at"].split(":"))
            due = datetime.fromtimestamp(now, store.SHANGHAI).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due.timestamp() > now:
                due -= timedelta(days=1)
            scheduled_for = due.timestamp()
            previous = round_state.get("last_scheduled_for") or state.get("last_scheduled_for")
            completed = round_state.get("round_status") == "complete" if round_state else state.get("status") == "success"
            done = bool(previous and datetime.fromisoformat(previous).timestamp() >= scheduled_for and completed)
            next_run = (due + timedelta(days=1)).timestamp() if done else scheduled_for
        else:
            next_run = datetime.fromisoformat(last).timestamp() + interval if last else now
            scheduled_for = next_run
        if round_state.get("round_status") == "active":
            next_run = now
        retry_at = state.get("retry_at")
        if state.get("status") in {"failed", "partial"} or (state.get("status") == "skipped" and retry_at):
            finished = state.get("last_finished_at") or state.get("last_attempt_at")
            retry = datetime.fromisoformat(retry_at).timestamp() if retry_at else datetime.fromisoformat(finished).timestamp() + store.retry_delay(state.get("failures", 0), state.get("error_code")) if finished else now
            retry_at = store.iso(retry)
            next_run = retry
        result.append({**row, **config["sources"][sid], "status": "never", "records_received": 0, "records_added": 0,
                       "last_attempt_at": None, "last_success_at": None, "error_code": None,
                       "total_symbols": 0, "completed_symbols": 0, "pending_symbols": 0, **state, **round_state,
                       "schedule_timezone": "Asia/Shanghai", "scheduled_for": store.iso(scheduled_for),
                       "retry_at": retry_at, "next_run_at": store.iso(next_run)})
    return result


def enqueue(source_ids=None, *, scheduled=False):
    rows = sources()
    known = {row["id"] for row in rows}
    if source_ids is not None and (not isinstance(source_ids, list) or len(source_ids) > len(known) or any(not isinstance(sid, str) or sid not in known for sid in source_ids)):
        raise ValueError("请选择内置采集来源")
    selected = set(source_ids) if source_ids is not None else {row["id"] for row in rows if row["enabled"]}
    now = time.time()
    jobs = []
    pool = _symbol_pool()
    for row in rows:
        if row["id"] not in selected:
            continue
        if scheduled and (not row["enabled"] or (row["id"] in _STOCK_SOURCES and not pool and not row["pending_symbols"])
                          or (row["next_run_at"] and datetime.fromisoformat(row["next_run_at"]).timestamp() > now)):
            continue
        if row["id"] in _STOCK_SOURCES and (pool or row["pending_symbols"]):
            if len(pool) > 3000 and row.get("round_status") != "active":
                store.block_source(row["id"], "symbol_pool_too_large")
                jobs.append({"source_id": row["id"], "status": "blocked", "error_code": "symbol_pool_too_large"})
                continue
            frozen = store.active_round_symbols(row["id"]) if row.get("round_status") == "active" else pool
            window = f"{row['schedule_mode']}:{row['scheduled_for']}" if scheduled else "manual:" + uuid.uuid4().hex
            jobs.append(store.enqueue_round(row["id"], frozen, window_key=window,
                        scheduled_for=datetime.fromisoformat(row["scheduled_for"]).timestamp() if scheduled else now,
                        scheduled=scheduled, batch_size=get_config()["max_symbols_per_cycle"], now=now))
        else:
            payload = {"scheduled": scheduled}
            if scheduled:
                payload.update(window_key=f"{row['schedule_mode']}:{row['scheduled_for']}", scheduled_for=row["scheduled_for"])
            jobs.append(store.enqueue(row["id"], payload, now=now))
    return {"status": "queued" if any(job["status"] in {"queued", "running"} for job in jobs) else "blocked" if any(job["status"] == "blocked" for job in jobs) else "cooldown", "jobs": jobs}


def enqueue_news(symbols=None, lookback_days=14):
    from app.services.stock_code_utils import normalize_symbol
    if symbols is not None and (not isinstance(symbols, list) or len(symbols) > 3000 or any(not isinstance(s, str) or not normalize_symbol(s) for s in symbols)):
        raise ValueError("新闻股票须为最多3000个规范代码")
    if lookback_days is not None and (type(lookback_days) is not int or not 1 <= lookback_days <= 30):
        raise ValueError("新闻回看范围须为1–30天")
    normalized = sorted(set(normalize_symbol(s) for s in symbols)) if symbols is not None else None
    job = store.enqueue("public_news", {"scheduled": False, "explicit_news": True,
                                        "news_symbols": normalized, "news_lookback_days": lookback_days})
    return {"status": "queued" if job["status"] in {"queued", "running"} else job["status"], "jobs": [job]}


def _symbol_pool():
    config = get_config()
    pool = list(config["symbols"])
    if config["use_watchlists"]:
        from app.storage import watchlist_store
        # The shared union is read-only, including an unmigrated legacy list.
        pool.extend(symbol for symbol in watchlist_store.load_all_symbols_union() if _SYMBOL.fullmatch(symbol))
    return list(dict.fromkeys(s.upper() for s in pool))


def _symbols(source_id):
    config = get_config()
    pool = _symbol_pool()
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
    if source_id == "public_market":
        from app.services.macro_workspace import collect_market_baseline
        return await collect_market_baseline()
    if source_id == "economic_calendar":
        from app.services import event_calendar_service
        output = await event_calendar_service.refresh(scheduled=True)
        results = [row for row in output.get("results", []) if not row.get("skipped")]
        failed = any(row.get("status") == "failed" for row in results)
        return {"status": "partial" if failed else "success" if results else "skipped",
                "records_received": sum(row.get("event_count", 0) for row in results if row.get("status") == "success"),
                "records_added": sum(row.get("revisions_added", 0) for row in results if row.get("status") == "success"),
                "error_code": "partial_source_failure" if failed else "source_cooldown" if not results else None}
    job = _current_job.get()
    payload = job.get("payload", {}) if job else {}
    symbols = (payload.get("news_symbols") if source_id == "public_news" and payload.get("explicit_news") else
               await durable_call(store.pending_job_symbols, job["id"]) if payload.get("round_id")
               else await durable_call(_symbols, source_id))
    if source_id == "public_news":
        from app.services.news_feed_sync import sync_all_sources_to_archive
        if payload.get("explicit_news"):
            symbols = payload.get("news_symbols")
        result = await sync_all_sources_to_archive(symbols, lookback_days=payload.get("news_lookback_days", 14))
        if result.get("skipped") in {"already_running", "high_memory"}:
            return {"status": "skipped", "error_code": "source_busy" if result["skipped"] == "already_running" else "memory_pressure", "records_received": 0, "records_added": 0}
        received = result.get("records_received", 0)
        partial = bool(result.get("failed_sources"))
        return {"status": "partial" if received and partial else "success" if received else "failed",
                "records_received": received, "records_added": result.get("records_added", 0),
                "error_code": "partial_source_failure" if partial else None if received else "empty_source"}
    if not symbols and payload.get("round_id"):
        return {"status": "success", "records_received": 0, "records_added": 0}
    if not symbols:
        return {"status": "skipped", "error_code": "empty_stock_pool"}
    from app.services import market_extra_http, company_profile_em
    from app.storage import kline_bundle_cache, market_history_cache
    category = next(row["category"] for row in _BUILTINS if row["id"] == source_id)
    counts = {"records_received": 0, "records_added": 0}
    failed = 0
    failure_code = None
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
            if payload.get("round_id"):
                await durable_call(store.checkpoint_symbol, job["id"], symbol)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failed += 1
            failure_code = _safe_error(exc)
            logger.warning("公共数据采集未完成 source=%s symbol=%s", source_id, symbol)
            if failure_code in {"http_401", "http_403", "http_429"}:
                break
        await asyncio.sleep(1)
    return {**counts, "status": "partial" if failed and counts["records_received"] else "failed" if failed else "success",
            "error_code": failure_code if failure_code in {"http_401", "http_403", "http_429"} else "partial_source_failure" if failed else None}


def _safe_error(exc):
    if isinstance(exc, TimeoutError):
        return "timeout"
    code = getattr(exc, "error_code", "source_unavailable")
    return code if isinstance(code, str) and re.fullmatch(r"(?:http_[45]\d{2}|timeout|network_error|response_too_large|invalid_payload|invalid_data|unknown_source)", code) else "source_unavailable"


def collection_overview():
    """Collector status without traversing files or loading the asset catalog."""
    worker = store.get_state("worker", {})
    beat = worker.get("heartbeat_at")
    fresh = bool(beat and time.time() - datetime.fromisoformat(beat).timestamp() < 90)
    config = get_config()
    return {"config": config, "sources": sources(), "jobs": store.list_jobs(20),
            "datasets": store.dataset_stats(), "usage": store.usage(),
            "worker": {**worker, "enabled": settings.enable_data_collection,
                       "scheduler_enabled": settings.enable_scheduler,
                       "automatic_enabled": settings.enable_data_collection and config["enabled"],
                       "max_concurrent_jobs": config["max_concurrent_jobs"],
                       "running": bool(settings.enable_data_collection and fresh and worker.get("running", False))}}


def overview():
    from app.storage import data_asset_manager
    result = data_asset_manager.scan_data_assets()
    result["collection"] = collection_overview()
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


def _continue_rounds():
    config = get_config()
    for item in store.active_rounds():
        sid = item["source_id"]
        if item["scheduled"] and (not config["enabled"] or not config["sources"].get(sid, {}).get("enabled")):
            continue
        store.enqueue_round(sid, item["symbols"], window_key=item["window_key"], scheduled_for=item["scheduled_for"],
                            scheduled=item["scheduled"], batch_size=config["max_symbols_per_cycle"])


async def run_cycle(*, owner=None):
    if not settings.enable_data_collection:
        return {"status": "disabled", "jobs_processed": 0}
    owner = owner or uuid.uuid4().hex
    with _worker_lease() as owned:
        if not owned:
            return {"status": "another_worker"}
        token = current_user_id.set(0)
        heartbeat = None
        final_state = {"running": True, "phase": "idle", "source_id": None, "active_jobs": [], "reason": None}
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
            automatic = settings.enable_data_collection and config["enabled"]
            allowed = [sid for sid, source in config["sources"].items() if source["enabled"]] if automatic else []
            await durable_call(store.cancel_disallowed_scheduled, allowed)
            await durable_call(_continue_rounds)
            if automatic:
                await durable_call(enqueue, scheduled=True)
            if shutil.disk_usage(settings.data_dir).free < config["min_free_disk_mb"] * 1024 * 1024:
                final_state.update(running=False, reason="low_disk_space")
                return {"status": "low_disk_space"}
            # Independent sources progress in separate lanes. The existing file
            # lease still owns all jobs; SQLite atomically enforces the daily cap.
            active: dict[str, dict] = {}
            claimed = 0
            claim_lock = asyncio.Lock()

            async def lane(lane_index):
                nonlocal claimed
                while True:
                    async with claim_lock:
                        if claimed >= _MAX_JOBS_PER_CYCLE:
                            return
                        # Honor a pause or source toggle before replenishing a
                        # lane, even while another source is still fetching.
                        latest_config = await durable_call(get_config)
                        if not settings.enable_data_collection or lane_index >= latest_config["max_concurrent_jobs"]:
                            return
                        from app.services import collector_resources
                        resources = await durable_call(collector_resources.snapshot, latest_config["max_memory_mb"])
                        await durable_call(_update_worker, owner, resources=resources)
                        if resources["pressure"]:
                            final_state["reason"] = "memory_pressure"
                            return
                        permitted = [sid for sid, source in latest_config["sources"].items() if source["enabled"]]
                        if not settings.enable_data_collection or not latest_config["enabled"]:
                            permitted = []
                        await durable_call(store.cancel_disallowed_scheduled, permitted)
                        job = await durable_call(store.claim_job, latest_config["daily_job_limit"])
                        if job is None:
                            return
                        claimed += 1
                        active[job["id"]] = {"job_id": job["id"], "source_id": job["source_id"], "started_at": job["started_at"]}
                        await durable_call(_update_worker, owner, phase="collecting", source_id=job["source_id"],
                                           active_jobs=list(active.values()))
                    job_context = _current_job.set(job)
                    try:
                        try:
                            result = await asyncio.wait_for(collect_source(job["source_id"]), timeout=latest_config["job_timeout_seconds"])
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            result = {"status": "failed", "error_code": _safe_error(exc)}
                            logger.warning("公共数据任务失败 source=%s type=%s", job["source_id"], type(exc).__name__)
                        try:
                            await durable_call(store.finish_job, job["id"], result)
                        except ValueError:
                            # A bad adapter result is a recorded source failure,
                            # not an interrupted job that retries every five seconds.
                            result = {"status": "failed", "error_code": "invalid_payload"}
                            await durable_call(store.finish_job, job["id"], result)
                        await durable_call(_continue_rounds)
                        from app.services.runtime_telemetry import record_collection
                        # News/calendar emit their own provider collection events.
                        if job["source_id"] not in {"public_news", "economic_calendar"}:
                            record_collection("data_center", job["source_id"], records_received=result.get("records_received", 0),
                                              records_added=result.get("records_added", 0), status="success" if result["status"] == "success" else "failed",
                                              error_code=result.get("error_code"), user_id=0)
                    finally:
                        _current_job.reset(job_context)
                        async with claim_lock:
                            active.pop(job["id"], None)
                            await durable_call(_update_worker, owner, active_jobs=list(active.values()),
                                               source_id=next((value["source_id"] for value in active.values()), None))

            workers = [asyncio.create_task(lane(index)) for index in range(config["max_concurrent_jobs"])]
            try:
                await asyncio.gather(*workers)
            finally:
                # Cancellation may be draining synchronous calls in several
                # lanes; never release ownership while any lane can still write.
                for task in workers:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            if store.usage()["jobs_started"] >= get_config()["daily_job_limit"]:
                final_state["reason"] = "daily_budget_exhausted"
            last_maintenance = store.get_state("last_maintenance", 0)
            if time.time() - last_maintenance > 3600:
                from app.storage import cache_lifecycle
                await durable_call(cache_lifecycle.run_maintenance)
                await durable_call(store.set_state, "last_maintenance", time.time())
            return {"status": "processed" if claimed else "idle", "jobs_processed": claimed}
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
