"""公司资料缓存与定期更新；来源暂不可用时保留各部分最后成功的数据。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import threading
import time
from typing import Any

from app.config import settings
from app.services import xueqiu_http, xueqiu_pipeline
from app.storage import company_updates_store, watchlist_store

logger = logging.getLogger(__name__)
_SECTIONS = ("company", "major_events", "news")
_FORCE_MIN_INTERVAL_SEC = 30
_locks_guard = threading.Lock()
_symbol_locks: dict[str, threading.Lock] = {}


def refresh_interval_sec() -> int:
    return max(60, int(getattr(settings, "company_refresh_interval_sec", 900)))


def _symbol_lock(symbol: str) -> threading.Lock:
    with _locks_guard:
        return _symbol_locks.setdefault(symbol, threading.Lock())


def _fingerprint() -> str:
    # 凭证变动立即使缓存过期；缓存文件中绝不写入 Cookie 本身。
    raw = xueqiu_http.effective_xueqiu_cookies()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _response(record: dict[str, Any], *, cached: bool) -> dict[str, Any]:
    result = copy.deepcopy(record["bundle"])
    result.update(
        cached=cached,
        refresh_interval_sec=refresh_interval_sec(),
        auto_refresh_enabled=bool(getattr(settings, "company_auto_refresh_enabled", True)),
    )
    return result


def _merge_result(
    fresh: dict[str, Any], previous: dict[str, Any], now: int
) -> dict[str, Any]:
    """只有失败的部分沿用旧值；成功返回空列表时应清空旧资讯。"""
    result = dict(fresh)
    has_section_errors = isinstance(fresh.get("section_errors"), dict)
    errors = fresh.get("section_errors") or {}
    fresh_times = fresh.get("section_fetched_at") or {}
    previous_times = previous.get("section_fetched_at") or {}
    section_times: dict[str, int | None] = {}
    sources = dict(fresh.get("sources") or {})
    previous_sources = previous.get("sources") or {}
    stale_fields = set(fresh.get("stale_fields") or []) & set(_SECTIONS)
    updated_fields: list[str] = []
    for field in _SECTIONS:
        value = fresh.get(field)
        failed = bool(errors.get(field))
        if not has_section_errors and fresh.get("errors") and not value:
            # 没有逐部分错误信息时，使用聚合错误判断空结果是否失败。
            failed = True
        older_fallback = (
            field in stale_fields
            and previous_times.get(field) is not None
            and (
                fresh_times.get(field) is None
                or previous_times[field] > fresh_times[field]
            )
        )
        if failed and (not value or (previous.get(field) and older_fallback)):
            if previous.get(field):
                result[field] = previous[field]
                stale_fields.add(field)
                if field in previous_sources:
                    sources[field] = previous_sources[field]
                section_times[field] = previous_times.get(field)
            else:
                section_times[field] = fresh_times.get(field)
        else:
            # 流水线可能沿用旧公开资料；显式 None 表示采集时间未知，不能标成刚更新。
            section_times[field] = fresh_times[field] if field in fresh_times else now
            if value and field not in stale_fields:
                updated_fields.append(field)
        if not result.get(field):
            stale_fields.discard(field)

    has_data = any(result.get(field) for field in _SECTIONS)
    data_errors = any(errors.values()) if has_section_errors else bool(fresh.get("errors"))
    if not has_data:
        status = "unavailable"
    elif stale_fields and not updated_fields:
        status = "stale"
    elif stale_fields or data_errors:
        status = "partial"
    else:
        status = "ready"
    result.update(
        ok=has_data,
        status=status,
        sources=sources,
        stale_fields=[field for field in _SECTIONS if field in stale_fields],
        section_fetched_at=section_times,
        fetched_at=max(
            (section_times.get(field) or 0 for field in _SECTIONS if result.get(field)),
            default=0,
        ) or None,
        last_attempt_at=now,
        next_refresh_at=now + refresh_interval_sec(),
    )
    return result


def get_company_bundle(symbol: str, force: bool = False) -> dict[str, Any]:
    """返回共享公司资料；同标的并发刷新合并，手动刷新最短间隔 30 秒。"""
    symbol = symbol.strip().upper()
    if not xueqiu_pipeline.yahoo_to_xq_symbol(symbol):
        # 无效代码不创建文件或锁，不进入后台任务。
        return xueqiu_pipeline.run_company_bundle(symbol)

    with _symbol_lock(symbol):
        now = int(time.time())
        fingerprint = _fingerprint()
        record = company_updates_store.load(symbol)
        previous = record.get("bundle") or {}
        last_attempt = int(previous.get("last_attempt_at") or 0)
        minimum_age = _FORCE_MIN_INTERVAL_SEC if force else refresh_interval_sec()
        if (
            record.get("fingerprint") == fingerprint
            and last_attempt
            and now - last_attempt < minimum_age
        ):
            return _response(record, cached=True)

        try:
            fresh = xueqiu_pipeline.run_company_bundle(symbol)
        except Exception:
            # 上游异常细节仅留在服务日志，接口仍返回上次成功内容。
            logger.exception("公司资料更新失败 symbol=%s", symbol)
            message = "公司资料来源暂不可用，将自动重试"
            fresh = {
                "yahoo_symbol": symbol,
                "xq_symbol": xueqiu_pipeline.yahoo_to_xq_symbol(symbol),
                "cookies_configured": bool(xueqiu_http.effective_xueqiu_cookies()),
                "company": None,
                "major_events": [],
                "news": [],
                "errors": [message],
                "section_errors": {field: message for field in _SECTIONS},
            }
        record = {
            "fingerprint": fingerprint,
            "bundle": _merge_result(fresh, previous, int(time.time())),
        }
        try:
            company_updates_store.save(symbol, record)
        except OSError:
            logger.exception("公司资料缓存写入失败 symbol=%s", symbol)
        return _response(record, cached=False)


async def refresh_watchlist_once() -> None:
    """共享公司资料只按所有账户股票列表的并集刷新一次，不调用 LLM。"""
    symbols = await asyncio.to_thread(watchlist_store.load_all_symbols_union)
    for symbol in symbols:
        if not xueqiu_pipeline.yahoo_to_xq_symbol(symbol):
            continue
        try:
            await asyncio.to_thread(get_company_bundle, symbol)
        except Exception:
            logger.exception("公司资料后台更新失败 symbol=%s", symbol)
        # 单次批量串行，避免一次扫完大量标的压垮外部源；此处可及时取消。
        await asyncio.sleep(1)


async def company_updates_loop() -> None:
    """服务运行期间定期扫描到期缓存；取消只终止循环，已发出的请求按超时结束。"""
    await asyncio.sleep(10)
    while True:
        if getattr(settings, "company_auto_refresh_enabled", True):
            try:
                await refresh_watchlist_once()
            except Exception:
                logger.exception("公司资料自动更新调度异常")
        await asyncio.sleep(60)
