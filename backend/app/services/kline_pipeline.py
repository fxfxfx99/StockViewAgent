"""A 股 K 线：东财主源 + 多源链式兜底（腾讯/新浪 HTTP、Baostock、Pytdx）。备案见 docs/KLINE_DATA_SOURCES.md。"""
from __future__ import annotations

import asyncio
import copy
import math
import time
import weakref
from datetime import datetime, timezone

from app.services import (
    eastmoney_market,
    free_stockdb_http,
    market_data_resilience,
    market_extra_http,
    market_source_policy,
    yahoo_market,
)
from app.storage import kline_bundle_cache, market_source_cache
from app.services.kline_baostock import (
    fetch_kline_bundle_baostock_minute_sync,
    fetch_kline_bundle_baostock_sync,
    is_baostock_installed,
)
from app.services.kline_pytdx import (
    fetch_kline_bundle_pytdx_minute_sync,
    fetch_kline_bundle_pytdx_sync,
    is_pytdx_installed,
)
from app.storage.platform_store import kline_sdk_enabled
from app.services.market_time import market_date


_FALLBACK_SOURCE_LABELS = {
    "tencent_fallback": "腾讯财经",
    "baidu_fallback": "百度股市通",
    "free_stockdb_local": "free-stockdb 本地 HTTP",
    "baostock_fallback": "Baostock",
    "sina_fallback": "新浪财经",
    "pytdx_fallback": "通达信",
    "tencent_mkline_fallback": "腾讯财经分钟线",
    "baostock_minute_fallback": "Baostock 分钟线",
    "pytdx_minute_fallback": "通达信分钟线",
    "klineshare": "KlineShare",
}

_MAJOR_INDEX_SYMBOLS = {
    "SH_COMP": "000001.SS",
    "SZ_COMP": "399001.SZ",
    "CYB": "399006.SZ",
    "HS300": "000300.SS",
    "SSE50": "000016.SS",
    "STAR50": "000688.SS",
}

_MAJOR_INDEX_PROVIDER_SYMBOLS = {
    "SH_COMP": "sh000001",
    "SZ_COMP": "sz399001",
    "CYB": "sz399006",
    "HS300": "sh000300",
    "SSE50": "sh000016",
    "STAR50": "sh000688",
}


def _record_source_health(source_id: str, ok: bool, detail: str = "") -> None:
    market_source_cache.record_health(
        source_id, ok=ok, detail="validated" if ok else "invalid_payload" if detail == "invalid_payload" else "request_failed"
    )


INDEX_CACHE_TTL_SECONDS = 300
_INDEX_FLIGHTS = weakref.WeakKeyDictionary()


class _IndexFlight:
    def __init__(self, task: asyncio.Task):
        self.task = task
        self.waiters = 0


def _index_cache_fresh(entry: dict | None) -> bool:
    try:
        saved = datetime.fromisoformat(str(entry["saved_at"]).replace("Z", "+00:00"))
        age = time.time() - saved.replace(tzinfo=saved.tzinfo or timezone.utc).timestamp()
        return 0 <= age < INDEX_CACHE_TTL_SECONDS
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _validate_index_bundle(bundle: dict) -> None:
    if not isinstance(bundle, dict):
        raise ValueError("指数来源格式无效")
    candles = bundle.get("candles")
    if not isinstance(candles, list) or not candles:
        raise ValueError("指数来源没有有效 K 线")
    today = market_date(time.time())
    previous = 0
    for row in candles:
        if not isinstance(row, dict) or any(isinstance(row.get(key), bool) for key in ("t", "o", "h", "l", "c", "v")):
            raise ValueError("指数 K 线字段无效")
        try:
            stamp, o, h, low, close = [float(row[key]) for key in ("t", "o", "h", "l", "c")]
            volume = float(row.get("v") or 0)
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ValueError("指数 K 线字段无效") from None
        if (not all(math.isfinite(value) for value in (stamp, o, h, low, close, volume))
                or stamp <= previous
                or min(o, h, low, close) <= 0 or volume < 0
                or h < max(o, low, close) or low > min(o, close)):
            raise ValueError("指数 K 线时间或 OHLC 数据无效")
        try:
            if market_date(stamp) > today:
                raise ValueError("指数 K 线不能晚于上海当前日期")
        except (OSError, OverflowError):
            raise ValueError("指数 K 线时间无效") from None
        previous = stamp


def _index_data_recent(bundle: dict, interval: str) -> bool:
    # A-share holiday gaps can exceed a week; requests are current, not as-of.
    days = {"1d": 14, "1wk": 21, "1mo": 45}.get(interval, 14)
    return time.time() - float(bundle["candles"][-1]["t"]) <= days * 86400


def _index_cached_response(cached: dict, *, stale: bool, error: str = "") -> dict:
    if stale:
        return kline_bundle_cache.merge_stale_response(cached, fetch_error=error)
    out = dict(cached["payload"])
    out.update(served_from_cache=True, cache_stale=False, cache_saved_at=cached.get("saved_at"),
               data_as_of=kline_bundle_cache.last_bar_date_str(out.get("candles") or []))
    return out


async def fetch_major_index_kline_with_fallbacks(
    index_key: str, range_param: str, interval: str, *, force_refresh: bool = False, public_only: bool = False,
) -> dict:
    """Six public A-share indices with expiring cache and validated failover.

    Per-event-loop single flight avoids duplicate UI/collector requests. A stale
    cache is retained if every live source fails, with its original observation time.
    """
    key = (index_key or "").strip().upper()
    if key not in _MAJOR_INDEX_SYMBOLS:
        raise ValueError(f"未知指数代码，支持：{', '.join(sorted(_MAJOR_INDEX_SYMBOLS))}")
    loop = asyncio.get_running_loop()
    flights = _INDEX_FLIGHTS.setdefault(loop, {})
    flight_key = (key, range_param, interval, public_only)
    flight = flights.get(flight_key)
    if flight is None:
        cached = kline_bundle_cache.load_bundle(f"INDEX_{key}", range_param, interval)
        if cached:
            try:
                _validate_index_bundle(cached.get("payload") or {})
            except ValueError:
                cached = None
        if (not force_refresh and cached and _index_cache_fresh(cached)
                and _index_data_recent(cached["payload"], interval)):
            return _index_cached_response(cached, stale=False)
        flight = _IndexFlight(asyncio.create_task(
            _refresh_major_index(key, range_param, interval, public_only=public_only, cached=cached)))
        flights[flight_key] = flight

        def finished(task: asyncio.Task) -> None:
            if flights.get(flight_key) is flight:
                flights.pop(flight_key)
            # The last waiter may have gone away before a provider failed.
            if not task.cancelled():
                task.exception()

        flight.task.add_done_callback(finished)
    flight.waiters += 1
    try:
        # A cancelled HTTP request must not cancel another caller's refresh.
        return copy.deepcopy(await asyncio.shield(flight.task))
    finally:
        flight.waiters -= 1
        if flight.waiters == 0 and not flight.task.done():
            if flights.get(flight_key) is flight:
                flights.pop(flight_key)
            flight.task.cancel()
            await asyncio.gather(flight.task, return_exceptions=True)


async def _refresh_major_index(
    key: str, range_param: str, interval: str, *, public_only: bool, cached: dict | None,
) -> dict:
    cache_key = f"INDEX_{key}"
    symbol = _MAJOR_INDEX_SYMBOLS[key]
    provider_symbol = _MAJOR_INDEX_PROVIDER_SYMBOLS[key]
    label = eastmoney_market.MAJOR_INDEX_META[key][1]
    providers = [
        ("eastmoney", lambda: eastmoney_market.fetch_major_index_kline_bundle(key, range_param, interval), 12),
        ("tencent_index_fallback", lambda: asyncio.to_thread(
            market_extra_http.fetch_kline_bundle_secondary_sync, symbol, range_param, interval,
            provider_symbol=provider_symbol), 15),
        ("sina_index_fallback", lambda: asyncio.to_thread(
            market_extra_http.fetch_kline_bundle_sina_sync, symbol, range_param, interval,
            provider_symbol=provider_symbol), 15),
    ]
    if not public_only and is_baostock_installed() and kline_sdk_enabled("baostock"):
        providers.append(("baostock_index", lambda: asyncio.to_thread(
            fetch_kline_bundle_baostock_sync, symbol, range_param, interval), 20))
    providers.append(("yahoo_stooq_fallback", lambda: yahoo_market.fetch_kline_bundle(symbol, range_param, interval), 20))
    failures = []
    outdated = None
    for provider, fetch, timeout in providers:
        try:
            out = await asyncio.wait_for(fetch(), timeout=timeout)
            _validate_index_bundle(out)
            if out.get("cache_stale"):
                raise ValueError("上游返回过期缓存")
            if cached and float(out["candles"][-1]["t"]) < float(cached["payload"]["candles"][-1]["t"]):
                raise ValueError("上游返回比已保存记录更旧的行情")
            if not _index_data_recent(out, interval):
                if not outdated or float(out["candles"][-1]["t"]) > float(outdated["candles"][-1]["t"]):
                    outdated = {**out, "kline_source": provider}
                raise ValueError("指数来源已长时间未更新")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append(f"{provider}: {type(exc).__name__}")
            _record_source_health("index_" + provider, False)
            continue
        out = dict(out)
        out.update(index_key=key, symbol=label, range=range_param, interval=interval,
                   kline_source=provider, served_from_cache=False, cache_stale=False,
                   data_as_of=kline_bundle_cache.last_bar_date_str(out["candles"]))
        out["metrics"] = {**(out.get("metrics") if isinstance(out.get("metrics"), dict) else {}), "name": label}
        if provider == "yahoo_stooq_fallback":
            out["source_url"] = ("https://stooq.com/q/d/l/" if "stooq" in str(out["metrics"].get("data_source", "")).lower()
                                 else "https://finance.yahoo.com/")
        kline_bundle_cache.save_bundle(cache_key, range_param, interval, out)
        entry = kline_bundle_cache.load_bundle(cache_key, range_param, interval)
        out["cache_saved_at"] = (entry or {}).get("saved_at")
        _record_source_health("index_" + provider, True)
        return out
    error = "大盘行情源暂不可用；" + "；".join(failures)
    if cached:
        return _index_cached_response(cached, stale=True, error=error)
    if outdated:
        return {**outdated, "index_key": key, "symbol": label, "range": range_param, "interval": interval,
                "served_from_cache": False, "cache_stale": True,
                "data_as_of": kline_bundle_cache.last_bar_date_str(outdated["candles"]),
                "cache_warning": "来源返回的最新行情已较长时间未更新，仅供查看历史。"}
    raise RuntimeError(error)


def _attach_source(out: dict, source_id: str, _fallback_note: str) -> dict:
    out["kline_source"] = source_id
    m = dict(out.get("metrics") or {})
    source_label = _FALLBACK_SOURCE_LABELS.get(source_id, "备用行情源")
    # 备用源已成功返回时属于正常降级，不把主源的底层连接异常误报成页面错误。
    m["fallback_note"] = f"主行情源暂不可用，已自动切换至{source_label}，当前行情数据可正常使用。"
    out["metrics"] = m
    return out


async def _attach_source_and_enrich(out: dict, source_id: str, fallback_note: str, symbol: str) -> dict:
    return await market_data_resilience.enrich_kline_bundle_resilient(
        _attach_source(out, source_id, fallback_note),
        symbol,
    )


async def fetch_a_share_kline_with_fallbacks(symbol: str, range_param: str, interval: str) -> dict:
    """
    返回与 GET /api/market/kline 相同的 bundle 结构；
    kline_source：eastmoney | tencent_fallback | free_stockdb_local | baostock_fallback | sina_fallback | pytdx_fallback |
                  tencent_mkline_fallback | baostock_minute_fallback | pytdx_minute_fallback
    """
    sym = symbol.strip().upper()

    async def _try_one(source: str, note: str) -> dict | None:
        n = (note or "")[:600]
        if source == "eastmoney":
            out = await eastmoney_market.fetch_kline_bundle(sym, range_param, interval)
            out.setdefault("kline_source", "eastmoney")
            return out
        if source == "tencent" and market_extra_http.interval_allows_secondary_kline(interval):
            out = await asyncio.to_thread(market_extra_http.fetch_kline_bundle_secondary_sync, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "tencent_fallback", n, sym)
        if source == "baidu" and market_extra_http.interval_allows_secondary_kline(interval):
            out = await asyncio.to_thread(market_extra_http.fetch_kline_bundle_baidu_sync, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "baidu_fallback", n, sym)
        if source == "free_stockdb" and market_extra_http.interval_allows_secondary_kline(interval):
            out = await asyncio.to_thread(free_stockdb_http.fetch_kline_bundle_sync, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "free_stockdb_local", n, sym)
        if source == "baostock" and market_extra_http.interval_allows_secondary_kline(interval):
            if not (is_baostock_installed() and kline_sdk_enabled("baostock")):
                raise RuntimeError("Baostock 未安装或未启用")
            out = await asyncio.to_thread(fetch_kline_bundle_baostock_sync, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "baostock_fallback", n, sym)
        if source == "sina" and market_extra_http.interval_allows_secondary_kline(interval):
            out = await asyncio.to_thread(market_extra_http.fetch_kline_bundle_sina_sync, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "sina_fallback", n, sym)
        if source == "klineshare" and market_extra_http.interval_allows_secondary_kline(interval):
            from app.services import klineshare_service

            if not klineshare_service.should_use_in_chain():
                raise RuntimeError("KlineShare 未配置或未选为行情提供商")
            out = await asyncio.to_thread(klineshare_service.fetch_kline_bundle, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "klineshare", n, sym)
        if source == "tushare" and market_extra_http.interval_allows_secondary_kline(interval):
            from app.services import tushare_service
            if not tushare_service.is_configured():
                raise RuntimeError("Tushare Token 未配置")
            out = await asyncio.to_thread(tushare_service.fetch_kline_bundle, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "tushare_pro_fallback", n, sym)
        if source == "pytdx" and market_extra_http.interval_allows_secondary_kline(interval):
            if not (is_pytdx_installed() and kline_sdk_enabled("pytdx")):
                raise RuntimeError("Pytdx 未安装或未启用")
            out = await asyncio.to_thread(fetch_kline_bundle_pytdx_sync, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "pytdx_fallback", n, sym)
        if source == "tencent_minute" and market_extra_http.interval_allows_tencent_minute_kline(interval):
            out = await asyncio.to_thread(
                market_extra_http.fetch_kline_bundle_tencent_minute_sync, sym, range_param, interval
            )
            return await _attach_source_and_enrich(out, "tencent_mkline_fallback", n, sym)
        if source == "baostock_minute" and market_extra_http.interval_allows_tencent_minute_kline(interval):
            if not (is_baostock_installed() and kline_sdk_enabled("baostock")):
                raise RuntimeError("Baostock 未安装或未启用")
            out = await asyncio.to_thread(fetch_kline_bundle_baostock_minute_sync, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "baostock_minute_fallback", n, sym)
        if source == "pytdx_minute" and market_extra_http.interval_allows_tencent_minute_kline(interval):
            if not (is_pytdx_installed() and kline_sdk_enabled("pytdx")):
                raise RuntimeError("Pytdx 未安装或未启用")
            out = await asyncio.to_thread(fetch_kline_bundle_pytdx_minute_sync, sym, range_param, interval)
            return await _attach_source_and_enrich(out, "pytdx_minute_fallback", n, sym)
        return None

    chain = (
        market_source_policy.kline_minute_chain()
        if market_extra_http.interval_allows_tencent_minute_kline(interval)
        and not market_extra_http.interval_allows_secondary_kline(interval)
        else market_source_policy.kline_daily_chain()
    )
    from app.services import source_autopilot
    capability = "daily" if market_extra_http.interval_allows_secondary_kline(interval) else "minute"
    chain = source_autopilot.ordered_sources(capability, chain)
    detail = ""
    first_error: Exception | None = None
    health_ids = {
        "eastmoney": "eastmoney_kline",
        "tencent": "tencent_kline_fallback",
        "baidu": "baidu_kline_fallback",
        "free_stockdb": "free_stockdb_local",
        "baostock": "baostock_kline_fallback",
        "sina": "sina_kline_fallback",
        "klineshare": "klineshare_kline",
        "tushare": "tushare_kline_fallback",
        "pytdx": "pytdx_kline_fallback",
        "tencent_minute": "tencent_minute_kline_fallback",
        "baostock_minute": "baostock_minute_kline_fallback",
        "pytdx_minute": "pytdx_minute_kline_fallback",
    }
    for source in chain:
        try:
            out = await _try_one(source, detail)
            if out is not None:
                source_autopilot.validate_kline_bundle(out)
                source_autopilot.record_outcome(capability, source, ok=True)
                _record_source_health(health_ids[source], True)
                return out
        except Exception as exc:  # noqa: BLE001
            reason = "invalid_payload" if isinstance(exc, ValueError) else "request_failed"
            source_autopilot.record_outcome(capability, source, ok=False, reason=reason)
            if first_error is None:
                first_error = exc
            if source in health_ids:
                _record_source_health(health_ids[source], False, reason)
            label = source
            detail = f"{detail}; {label}: {reason}" if detail else f"{label}: {reason}"
            continue
    if first_error is None:
        raise ValueError("没有可用于当前周期的 K 线数据源")
    if isinstance(first_error, ValueError):
        raise ValueError(detail) from None
    raise RuntimeError(detail) from None
