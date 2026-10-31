"""Market workspace adapters: public indices plus opt-in licensed macro series."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
import math
import re
import time
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import global_market, market_catalog, macro_tushare_service
from app.storage import data_collection_store

RANGES = frozenset({"3mo", "6mo", "1y", "2y", "5y"})
INTERVALS = frozenset({"1d", "1wk", "1mo"})
PUBLIC_SERIES = {"sh": "SH_COMP", "sz": "SZ_COMP", "cyb": "CYB"}
_PROVIDER_ERRORS = frozenset({"source_rate_limited", "provider_cooldown", "source_unavailable"})


def indices():
    return market_catalog.list_market_catalog()


def catalog():
    configured = bool(settings.effective_tushare_token)
    return {"items": [{**row, "requires_token": row["id"] not in PUBLIC_SERIES,
                       "available": row["id"] in PUBLIC_SERIES or configured,
                       "provider_hint": "公开指数来源" if row["id"] in PUBLIC_SERIES else "Tushare（需相应接口权限）"}
                      for row in macro_tushare_service.catalog()],
            "tushare_configured": configured,
            "index_basic_doc": "https://tushare.pro/document/2?doc_id=94",
            "token_hint": "A股大盘指数使用公开来源；黄金、离岸人民币、布伦特及申万行业按当前账户的 Tushare 权限读取。"}


async def index_kline(index_key: str, range_param="1y", interval="1d", *, refresh=False):
    if range_param not in RANGES or interval not in INTERVALS:
        raise ValueError("区间支持3mo/6mo/1y/2y/5y，周期支持1d/1wk/1mo")
    return await global_market.fetch_market_kline(
        index_key, range_param, interval, force_refresh=refresh, public_only=True)


async def macro_kline(series_id: str, range_param="1y"):
    if range_param not in RANGES:
        raise ValueError("区间支持3mo/6mo/1y/2y/5y")
    sid = series_id.strip().lower()
    meta = next((row for row in macro_tushare_service.catalog() if row["id"] == sid), None)
    if meta is None:
        raise ValueError("未知宏观序列，请从目录选择")
    if sid in PUBLIC_SERIES:
        bundle = await index_kline(PUBLIC_SERIES[sid], range_param)
        return {**bundle, "series_id": sid, "name": meta["name"], "short": meta.get("short"),
                "ts_code": meta["ts_code"], "provider_hint": "公开指数来源"}
    return await asyncio.to_thread(macro_tushare_service.fetch_macro_kline, sid, range_param)


def _timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return 0.0


def _component_states(items, saved):
    """Load only bounded progress fields; legacy extras are not public status.

    The collector owns this state, but a stale schema or interrupted migration
    must not prevent future collection. Candle history belongs in the archive,
    never in the small status checkpoint or its authenticated read endpoint.
    """
    states = {item["key"]: {"key": item["key"], "status": "pending"} for item in items}
    for row in saved if isinstance(saved, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or row["key"] not in states:
            continue
        target = states[row["key"]]
        if isinstance(row.get("status"), str) and row["status"] in {"pending", "running", "success", "failed", "stale"}:
            target["status"] = row["status"]
        for field in ("last_attempt_at", "last_success_at", "next_retry_at", "next_due_at"):
            value = row.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > 40:
                continue
            try:
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if instant.tzinfo is not None and instant.utcoffset() is not None and math.isfinite(instant.timestamp()):
                    target[field] = value
            except (ValueError, TypeError, OverflowError, OSError):
                continue
        for field, limit in (("consecutive_failures", 1_000_000), ("records_received", 1_000_000_000), ("records_added", 1_000_000_000)):
            value = row.get(field)
            if type(value) is int and 0 <= value <= limit:
                target[field] = value
        for field, pattern in (("error_code", r"[a-z][a-z0-9_]{0,63}"), ("provider", r"[A-Za-z0-9_.:-]{1,80}")):
            value = row.get(field)
            if value is None:
                target[field] = None
            elif isinstance(value, str) and re.fullmatch(pattern, value):
                target[field] = value
        value = row.get("data_as_of")
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                target["data_as_of"] = date.fromisoformat(value).isoformat()
            except ValueError:
                pass
    return states


def collection_status():
    """Public, credential-free per-instrument progress, including restart checkpoints."""
    from app.services.data_collection import get_config

    config = get_config()
    source = config["sources"]["public_market"]
    instruments = indices()
    saved = _component_states(instruments, data_collection_store.get_state("public_market_components", []))
    items = [{**item, **saved[item["key"]]} for item in instruments]
    summary = {"total": len(items), **{state: sum(row["status"] == state for row in items)
               for state in ("success", "failed", "stale", "pending", "running")}}
    return {"items": items, "summary": summary,
            "enabled": bool(settings.enable_data_collection and config["enabled"] and source["enabled"]),
            "interval_minutes": source["interval_minutes"],
            "updated_at": max((row.get("last_attempt_at") or "" for row in items), default="") or None}


async def collect_market_baseline():
    """Collect global daily bars with per-instrument checkpoints and bounded retries.

    The shared worker owns the cross-process lease. Each completed instrument is
    persisted immediately, so a cancelled or time-limited job can resume without
    downloading healthy instruments again. All dates retain the market's session
    timezone; continuous futures retain their exact contract and quotation units.
    """
    from app.services.data_collection import durable_call, get_config

    config = await durable_call(get_config)
    interval_seconds = config["sources"]["public_market"]["interval_minutes"] * 60
    budget = max(1, config["job_timeout_seconds"] - 10)
    items = indices()
    saved = await durable_call(data_collection_store.get_state, "public_market_components", [])
    states = _component_states(items, saved)
    now = time.time()
    semaphore, checkpoint_lock = asyncio.Semaphore(2), asyncio.Lock()
    counts = {"records_received": 0, "records_added": 0}

    async def checkpoint(key, **updates):
        async with checkpoint_lock:
            states[key] = _component_states([{"key": key}], [{**states[key], **updates}])[key]
            await durable_call(data_collection_store.set_state, "public_market_components", list(states.values()))

    def due(item):
        state = states[item["key"]]
        if _timestamp(state.get("next_retry_at")) > now:
            return False
        return state["status"] != "success" or now - _timestamp(state.get("last_success_at")) >= interval_seconds

    async def collect(item):
        key = item["key"]
        async with semaphore:
            try:
                await checkpoint(key, status="running", last_attempt_at=data_collection_store.iso())
                bundle = await asyncio.wait_for(index_kline(key, "1y", "1d", refresh=True), timeout=min(70, budget))
                if bundle.get("cache_stale"):
                    code = bundle.get("error_code")
                    await fail(key, "stale", code if isinstance(code, str) and code in _PROVIDER_ERRORS else "stale_source",
                               provider_retry_at=bundle.get("provider_retry_at"), data_as_of=bundle.get("data_as_of"))
                    return
                rows = []
                tz = ZoneInfo(item.get("timezone", "Asia/Shanghai"))
                for bar in bundle.get("candles") or []:
                    period = bar.get("date") or datetime.fromtimestamp(bar["t"], tz).date().isoformat()
                    rows.append({"source_id": "public_market", "source": bundle.get("kline_source") or "公开市场行情",
                                 "upstream_id": f"{key}:{period}", "title": f"{item['label']} 日线行情",
                                 "source_url": bundle.get("source_url") or item.get("source_url") or "https://quote.eastmoney.com/center/",
                                 "category": "market", "symbols": [key], "region": item.get("region", "CN"), "period": period,
                                 "published_at": None, "value": bar["c"], "unit": item.get("unit", "指数点位"),
                                 "text": f"{item['label']} 开盘{bar['o']} 最高{bar['h']} 最低{bar['l']} 收盘{bar['c']}",
                                 "metadata": {**bar, "index_key": key, "range": "1y", "interval": "1d",
                                              "provider": bundle.get("kline_source"),
                                              "provider_detail": (bundle.get("metrics") or {}).get("data_source"),
                                              "asset_class": item.get("asset_class", "index"),
                                              "exchange": item.get("exchange"), "currency": item.get("currency"),
                                              "timezone": item.get("timezone", "Asia/Shanghai"),
                                              "symbol": item.get("symbol"), "description": item.get("description")}})
                if not rows:
                    await fail(key, "failed", "empty_source")
                    return
                result = await durable_call(data_collection_store.ingest, "public_market", "market", rows)
                for field in counts:
                    counts[field] += result.get(field, 0)
                completed = time.time()
                await checkpoint(key, status="success", data_as_of=bundle.get("data_as_of"),
                                 last_success_at=data_collection_store.iso(completed),
                                 next_retry_at=None, next_due_at=data_collection_store.iso(completed + interval_seconds),
                                 consecutive_failures=0, error_code=None, provider=bundle.get("kline_source"),
                                 records_received=result.get("records_received", 0), records_added=result.get("records_added", 0))
            except asyncio.CancelledError:
                # Keep the last successful observation; pending work gets priority
                # over already completed instruments at the next worker tick.
                await checkpoint(key, status="pending", error_code="collection_interrupted", next_retry_at=None)
                raise
            except Exception as exc:
                code = getattr(exc, "error_code", None)
                await fail(key, "failed", code if isinstance(code, str) and code in _PROVIDER_ERRORS else
                           "source_timeout" if isinstance(exc, TimeoutError) else "source_unavailable",
                           provider_retry_at=getattr(exc, "retry_at", None))

    async def fail(key, status, error_code, *, provider_retry_at=None, **fields):
        failures = int(states[key].get("consecutive_failures", 0)) + 1
        instant = time.time()
        # Provider throttling applies across symbols. Preserve its public retry
        # deadline while keeping malformed/remote dates bounded to six hours.
        delay = max(min(3600, 120 * 2 ** min(failures - 1, 5)), min(21600, _timestamp(provider_retry_at) - instant))
        await checkpoint(key, status=status, error_code=error_code, consecutive_failures=failures,
                         next_retry_at=data_collection_store.iso(instant + delay), **fields)

    # Oldest attempts first prevents slow first-listed markets starving the rest.
    candidates = sorted((item for item in items if due(item)), key=lambda item: _timestamp(states[item["key"]].get("last_attempt_at")))
    tasks = [asyncio.create_task(collect(item)) for item in candidates]
    try:
        if tasks:
            await asyncio.wait(tasks, timeout=budget)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        # Drain persistence before the shared worker releases its lease.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    results = list(states.values())
    successes = sum(result["status"] == "success" for result in results)
    # Wake when a component is eligible, instead of consuming the daily job
    # budget every two minutes while all failed providers are cooling down.
    completed_at = time.time()
    next_due = [(_timestamp(row.get("last_success_at")) + interval_seconds if row["status"] == "success"
                 else _timestamp(row.get("next_retry_at")) or completed_at + 120) for row in results]
    retry_after = max(30, min(21600, math.ceil(min(next_due, default=completed_at + 120) - completed_at)))
    return {"status": "success" if successes == len(results) else "partial" if successes else "failed",
            **counts, "retry_after_seconds": retry_after,
            "error_code": None if successes == len(results) else "partial_source_failure",
            "components": results}
