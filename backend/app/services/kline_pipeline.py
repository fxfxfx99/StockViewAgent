"""A 股 K 线：东财主源 + 多源链式兜底（腾讯/新浪 HTTP、Baostock、Pytdx）。备案见 docs/KLINE_DATA_SOURCES.md。"""
from __future__ import annotations

import asyncio

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
}

_MAJOR_INDEX_PROVIDER_SYMBOLS = {
    "SH_COMP": "sh000001",
    "SZ_COMP": "sz399001",
    "CYB": "sz399006",
    "HS300": "sh000300",
    "SSE50": "sh000016",
}


def _record_source_health(source_id: str, ok: bool, detail: str = "") -> None:
    market_source_cache.record_health(source_id, ok=ok, detail=(detail or "")[:500])


async def fetch_major_index_kline_with_fallbacks(index_key: str, range_param: str, interval: str) -> dict:
    """大盘指数缓存优先；无缓存时 Baostock，随后多公开 HTTP 源兜底。"""
    key = (index_key or "").strip().upper()
    symbol = _MAJOR_INDEX_SYMBOLS.get(key)
    provider_symbol = _MAJOR_INDEX_PROVIDER_SYMBOLS.get(key)
    if not symbol:
        allowed = ", ".join(sorted(_MAJOR_INDEX_SYMBOLS))
        raise ValueError(f"未知指数代码，支持：{allowed}")
    cache_key = f"INDEX_{key}"
    cached = kline_bundle_cache.load_bundle(cache_key, range_param, interval)
    if cached and (cached.get("payload") or {}).get("candles"):
        out = dict(cached["payload"])
        out["kline_source"] = "local_cache"
        out["cache_saved_at"] = cached.get("saved_at")
        return out

    label = eastmoney_market.MAJOR_INDEX_META[key][1]
    if is_baostock_installed() and kline_sdk_enabled("baostock"):
        try:
            out = await asyncio.to_thread(fetch_kline_bundle_baostock_sync, symbol, range_param, interval)
            out["index_key"] = key
            out["symbol"] = label
            out["kline_source"] = "baostock_index"
            metrics = dict(out.get("metrics") or {})
            metrics["name"] = label
            out["metrics"] = metrics
            kline_bundle_cache.save_bundle(cache_key, range_param, interval, out)
            return out
        except Exception as baostock_error:
            pass
    else:
        baostock_error = RuntimeError("未启用")

    try:
        out = await asyncio.wait_for(
            eastmoney_market.fetch_major_index_kline_bundle(key, range_param, interval),
            timeout=25.0,
        )
        out["kline_source"] = "eastmoney"
    except Exception as primary_error:
        try:
            out = await asyncio.wait_for(
                asyncio.to_thread(
                    market_extra_http.fetch_kline_bundle_secondary_sync,
                    symbol,
                    range_param,
                    interval,
                    provider_symbol=provider_symbol,
                ),
                timeout=25.0,
            )
            out["kline_source"] = "tencent_index_fallback"
        except Exception as tencent_error:
            try:
                out = await asyncio.wait_for(
                    asyncio.to_thread(
                        market_extra_http.fetch_kline_bundle_sina_sync,
                        symbol,
                        range_param,
                        interval,
                        provider_symbol=provider_symbol,
                    ),
                    timeout=25.0,
                )
                out["kline_source"] = "sina_index_fallback"
            except Exception as sina_error:
                try:
                    out = await asyncio.wait_for(
                        yahoo_market.fetch_kline_bundle(symbol, range_param, interval), timeout=40.0
                    )
                    out["kline_source"] = "yahoo_stooq_fallback"
                except Exception as fallback_error:
                    def _reason(exc: Exception) -> str:
                        text = str(exc).strip()
                        return text or exc.__class__.__name__

                    raise RuntimeError(
                        "大盘行情源不可用："
                        f"Baostock {_reason(baostock_error)}；"
                        f"东财 {_reason(primary_error)}；"
                        f"腾讯 {_reason(tencent_error)}；"
                        f"新浪 {_reason(sina_error)}；"
                        f"Yahoo/Stooq {_reason(fallback_error)}"
                    ) from fallback_error
        out["index_key"] = key
        out["symbol"] = label
        metrics = dict(out.get("metrics") or {})
        metrics["name"] = label
        out["metrics"] = metrics

    kline_bundle_cache.save_bundle(cache_key, range_param, interval, out)
    return out


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

    async def _try_daily_http_chain(note: str) -> dict | None:
        if not market_extra_http.interval_allows_secondary_kline(interval):
            return None
        n = (note or "")[:600]
        try:
            out = await asyncio.to_thread(
                market_extra_http.fetch_kline_bundle_secondary_sync, sym, range_param, interval
            )
            _record_source_health("tencent_kline_fallback", True)
            return await _attach_source_and_enrich(out, "tencent_fallback", n, sym)
        except Exception as te:
            _record_source_health("tencent_kline_fallback", False, str(te))
            n = f"{n}; 腾讯: {te!s}"[:800]
        try:
            out = await asyncio.to_thread(free_stockdb_http.fetch_kline_bundle_sync, sym, range_param, interval)
            _record_source_health("free_stockdb_local", True)
            return await _attach_source_and_enrich(out, "free_stockdb_local", n, sym)
        except Exception as fe:
            _record_source_health("free_stockdb_local", False, str(fe))
            n = f"{n}; free-stockdb: {fe!s}"[:800]
        if is_baostock_installed() and kline_sdk_enabled("baostock"):
            try:
                out = await asyncio.to_thread(fetch_kline_bundle_baostock_sync, sym, range_param, interval)
                _record_source_health("baostock_kline_fallback", True)
                return await _attach_source_and_enrich(out, "baostock_fallback", n, sym)
            except Exception as bse:
                _record_source_health("baostock_kline_fallback", False, str(bse))
                n = f"{n}; Baostock: {bse!s}"[:800]
        try:
            out = await asyncio.to_thread(
                market_extra_http.fetch_kline_bundle_sina_sync, sym, range_param, interval
            )
            _record_source_health("sina_kline_fallback", True)
            return await _attach_source_and_enrich(out, "sina_fallback", n, sym)
        except Exception as se:
            _record_source_health("sina_kline_fallback", False, str(se))
            n = f"{n}; 新浪: {se!s}"[:800]
        if is_pytdx_installed() and kline_sdk_enabled("pytdx"):
            try:
                out = await asyncio.to_thread(fetch_kline_bundle_pytdx_sync, sym, range_param, interval)
                _record_source_health("pytdx_kline_fallback", True)
                return await _attach_source_and_enrich(out, "pytdx_fallback", n, sym)
            except Exception as pe:
                _record_source_health("pytdx_kline_fallback", False, str(pe))
                n = f"{n}; Pytdx: {pe!s}"[:800]
        raise ValueError(n[:900])

    async def _try_minute_chain(note: str) -> dict | None:
        if not market_extra_http.interval_allows_tencent_minute_kline(interval):
            return None
        n = (note or "")[:600]
        try:
            out = await asyncio.to_thread(
                market_extra_http.fetch_kline_bundle_tencent_minute_sync, sym, range_param, interval
            )
            _record_source_health("tencent_minute_kline_fallback", True)
            return await _attach_source_and_enrich(out, "tencent_mkline_fallback", n, sym)
        except Exception as te:
            _record_source_health("tencent_minute_kline_fallback", False, str(te))
            n = f"{n}; 腾讯mkline: {te!s}"[:800]
        if is_baostock_installed() and kline_sdk_enabled("baostock"):
            try:
                out = await asyncio.to_thread(
                    fetch_kline_bundle_baostock_minute_sync, sym, range_param, interval
                )
                _record_source_health("baostock_minute_kline_fallback", True)
                return await _attach_source_and_enrich(out, "baostock_minute_fallback", n, sym)
            except Exception as bse:
                _record_source_health("baostock_minute_kline_fallback", False, str(bse))
                n = f"{n}; Baostock分钟: {bse!s}"[:800]
        if is_pytdx_installed() and kline_sdk_enabled("pytdx"):
            try:
                out = await asyncio.to_thread(
                    fetch_kline_bundle_pytdx_minute_sync, sym, range_param, interval
                )
                _record_source_health("pytdx_minute_kline_fallback", True)
                return await _attach_source_and_enrich(out, "pytdx_minute_fallback", n, sym)
            except Exception as pe:
                _record_source_health("pytdx_minute_kline_fallback", False, str(pe))
                n = f"{n}; Pytdx分钟: {pe!s}"[:800]
        raise ValueError(n[:900])

    async def _try_one(source: str, note: str) -> dict | None:
        n = (note or "")[:600]
        if source == "eastmoney":
            try:
                out = await eastmoney_market.fetch_kline_bundle(sym, range_param, interval)
                _record_source_health("eastmoney_kline", True)
                out.setdefault("kline_source", "eastmoney")
                return out
            except Exception as exc:  # noqa: BLE001
                _record_source_health("eastmoney_kline", False, str(exc))
                raise
        if source == "tencent" and market_extra_http.interval_allows_secondary_kline(interval):
            out = await asyncio.to_thread(market_extra_http.fetch_kline_bundle_secondary_sync, sym, range_param, interval)
            _record_source_health("tencent_kline_fallback", True)
            return await _attach_source_and_enrich(out, "tencent_fallback", n, sym)
        if source == "baidu" and market_extra_http.interval_allows_secondary_kline(interval):
            out = await asyncio.to_thread(market_extra_http.fetch_kline_bundle_baidu_sync, sym, range_param, interval)
            _record_source_health("baidu_kline_fallback", True)
            return await _attach_source_and_enrich(out, "baidu_fallback", n, sym)
        if source == "free_stockdb" and market_extra_http.interval_allows_secondary_kline(interval):
            out = await asyncio.to_thread(free_stockdb_http.fetch_kline_bundle_sync, sym, range_param, interval)
            _record_source_health("free_stockdb_local", True)
            return await _attach_source_and_enrich(out, "free_stockdb_local", n, sym)
        if source == "baostock" and market_extra_http.interval_allows_secondary_kline(interval):
            if not (is_baostock_installed() and kline_sdk_enabled("baostock")):
                raise RuntimeError("Baostock 未安装或未启用")
            out = await asyncio.to_thread(fetch_kline_bundle_baostock_sync, sym, range_param, interval)
            _record_source_health("baostock_kline_fallback", True)
            return await _attach_source_and_enrich(out, "baostock_fallback", n, sym)
        if source == "sina" and market_extra_http.interval_allows_secondary_kline(interval):
            out = await asyncio.to_thread(market_extra_http.fetch_kline_bundle_sina_sync, sym, range_param, interval)
            _record_source_health("sina_kline_fallback", True)
            return await _attach_source_and_enrich(out, "sina_fallback", n, sym)
        if source == "klineshare" and market_extra_http.interval_allows_secondary_kline(interval):
            from app.services import klineshare_service

            if not klineshare_service.should_use_in_chain():
                raise RuntimeError("KlineShare 未配置或未选为行情提供商")
            out = await asyncio.to_thread(klineshare_service.fetch_kline_bundle, sym, range_param, interval)
            _record_source_health("klineshare_kline", True)
            return await _attach_source_and_enrich(out, "klineshare", n, sym)
        if source == "tushare" and market_extra_http.interval_allows_secondary_kline(interval):
            from app.services import tushare_service
            if not tushare_service.is_configured():
                raise RuntimeError("Tushare Token 未配置")
            out = await asyncio.to_thread(tushare_service.fetch_kline_bundle, sym, range_param, interval)
            _record_source_health("tushare_kline_fallback", True)
            return await _attach_source_and_enrich(out, "tushare_pro_fallback", n, sym)
        if source == "pytdx" and market_extra_http.interval_allows_secondary_kline(interval):
            if not (is_pytdx_installed() and kline_sdk_enabled("pytdx")):
                raise RuntimeError("Pytdx 未安装或未启用")
            out = await asyncio.to_thread(fetch_kline_bundle_pytdx_sync, sym, range_param, interval)
            _record_source_health("pytdx_kline_fallback", True)
            return await _attach_source_and_enrich(out, "pytdx_fallback", n, sym)
        if source == "tencent_minute" and market_extra_http.interval_allows_tencent_minute_kline(interval):
            out = await asyncio.to_thread(
                market_extra_http.fetch_kline_bundle_tencent_minute_sync, sym, range_param, interval
            )
            _record_source_health("tencent_minute_kline_fallback", True)
            return await _attach_source_and_enrich(out, "tencent_mkline_fallback", n, sym)
        if source == "baostock_minute" and market_extra_http.interval_allows_tencent_minute_kline(interval):
            if not (is_baostock_installed() and kline_sdk_enabled("baostock")):
                raise RuntimeError("Baostock 未安装或未启用")
            out = await asyncio.to_thread(fetch_kline_bundle_baostock_minute_sync, sym, range_param, interval)
            _record_source_health("baostock_minute_kline_fallback", True)
            return await _attach_source_and_enrich(out, "baostock_minute_fallback", n, sym)
        if source == "pytdx_minute" and market_extra_http.interval_allows_tencent_minute_kline(interval):
            if not (is_pytdx_installed() and kline_sdk_enabled("pytdx")):
                raise RuntimeError("Pytdx 未安装或未启用")
            out = await asyncio.to_thread(fetch_kline_bundle_pytdx_minute_sync, sym, range_param, interval)
            _record_source_health("pytdx_minute_kline_fallback", True)
            return await _attach_source_and_enrich(out, "pytdx_minute_fallback", n, sym)
        return None

    chain = (
        market_source_policy.kline_minute_chain()
        if market_extra_http.interval_allows_tencent_minute_kline(interval)
        and not market_extra_http.interval_allows_secondary_kline(interval)
        else market_source_policy.kline_daily_chain()
    )
    detail = ""
    first_error: Exception | None = None
    health_ids = {
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
                return out
        except Exception as exc:  # noqa: BLE001
            if first_error is None:
                first_error = exc
            if source in health_ids:
                _record_source_health(health_ids[source], False, str(exc))
            label = source
            detail = f"{detail}; {label}: {exc!s}"[:900] if detail else f"{label}: {exc!s}"[:900]
            continue
    if first_error is None:
        raise ValueError("没有可用于当前周期的 K 线数据源")
    if isinstance(first_error, ValueError):
        raise ValueError(detail or str(first_error)) from first_error
    raise RuntimeError(detail or str(first_error)) from first_error
