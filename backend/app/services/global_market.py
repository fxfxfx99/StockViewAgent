"""Validated public global K-lines with daily cache, explicit provenance and failover.

Only catalog symbols are accepted. Weekly/monthly bars are aggregated from real
observations in the instrument timezone; missing sessions are never filled.
"""
from __future__ import annotations

import asyncio
import calendar
import copy
import csv
import io
import math
import time
import weakref
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.services import kline_pipeline
from app.services.market_catalog import get_market_instrument
from app.storage import kline_bundle_cache, market_source_cache

CACHE_TTL_SECONDS = 300
PROVIDER_TIMEOUT_SECONDS = 12
SUPPORTED_RANGES = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
_FLIGHTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_PROVIDER_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class GlobalMarketUnavailable(RuntimeError):
    """Safe failure details for background scheduling; no network secrets."""

    def __init__(self, message: str, *, error_code: str = "source_unavailable", retry_at: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.retry_at = retry_at


class _ProviderCooldown(RuntimeError):
    def __init__(self, until: float, *, newly_limited: bool = False):
        self.until = until
        self.error_code = "source_rate_limited" if newly_limited else "provider_cooldown"
        super().__init__(self.error_code)


def _provider_host(provider: dict) -> str:
    return {"yahoo": "query1.finance.yahoo.com", "yahoo_query2": "query2.finance.yahoo.com",
            "stooq": "stooq.com"}[provider["id"]]


def _cooldown_until(provider: dict) -> float | None:
    entry = market_source_cache.load_payload("global_provider_cooldown", _provider_host(provider))
    try:
        stamp = _number((entry or {}).get("payload", {}).get("until"))
        return stamp if stamp > time.time() else None
    except (ValueError, TypeError, AttributeError):
        return None


def _save_provider_cooldown(provider: dict, response: httpx.Response) -> float:
    delay = 300.0 if response.status_code == 429 else 1800.0
    retry_after = response.headers.get("Retry-After", "")
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_date = parsedate_to_datetime(retry_after)
                delay = retry_date.replace(tzinfo=retry_date.tzinfo or timezone.utc).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                pass
    delay = min(3600.0, max(300.0, delay)) if math.isfinite(delay) else 300.0
    until = time.time() + delay
    market_source_cache.save_payload("global_provider_cooldown", _provider_host(provider),
        {"until": until, "status_code": response.status_code}, source=provider["id"])
    return until


async def _request_provider_daily(instrument: dict, provider: dict, range_param: str) -> list[dict]:
    # Serialize a host across symbols so the next request observes a new 429.
    locks = _PROVIDER_LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.setdefault(_provider_host(provider), asyncio.Lock())
    async with lock:
        until = _cooldown_until(provider)
        if until:
            raise _ProviderCooldown(until)
        try:
            return await _fetch_provider_daily(instrument, provider, range_param)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403, 429):
                until = _save_provider_cooldown(provider, exc.response)
                raise _ProviderCooldown(until, newly_limited=exc.response.status_code == 429) from None
            raise


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("无效行情数值")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("无效行情数值") from None
    if not math.isfinite(result):
        raise ValueError("行情包含非有限数值")
    return result


def _local_date(stamp: Any, instrument: dict) -> date:
    try:
        return datetime.fromtimestamp(_number(stamp), ZoneInfo(instrument["timezone"])).date()
    except (OSError, OverflowError, ValueError):
        raise ValueError("行情日期无效") from None


def validate_daily_candles(candles: list[dict], instrument: dict) -> list[dict]:
    """Reject corrupt OHLC/dates, duplicates and disorder; permit negative futures."""
    if not isinstance(candles, list) or not candles:
        raise ValueError("来源没有有效 K 线")
    today = _local_date(time.time(), instrument)
    previous = None
    normalized = []
    tz = ZoneInfo(instrument["timezone"])
    for row in candles:
        if not isinstance(row, dict):
            raise ValueError("K 线格式无效")
        try:
            day = _local_date(row["t"], instrument)
            o, h, low, c = (_number(row[key]) for key in ("o", "h", "l", "c"))
            volume = _number(row["v"]) if row.get("v") is not None else None
        except KeyError:
            raise ValueError("K 线缺少 OHLC 或日期") from None
        if day > today or day < date(1900, 1, 1) or (previous and day <= previous):
            raise ValueError("K 线日期未来、重复或未按时间升序")
        if h < max(o, low, c) or low > min(o, c) or (volume is not None and volume < 0):
            raise ValueError("K 线 OHLC 或成交量无效")
        if instrument["asset_class"] != "futures" and min(o, h, low, c) <= 0:
            raise ValueError("指数 OHLC 必须为正")
        normalized.append({"t": int(datetime.combine(day, datetime_time(), tzinfo=tz).timestamp()),
                           "date": day.isoformat(), "o": o, "h": h, "l": low, "c": c,
                           "v": int(volume) if volume is not None else None,
                           "amount": None, "turnover_rate": None})
        previous = day
    return normalized


def parse_yahoo_daily(payload: dict, instrument: dict) -> list[dict]:
    """Do not replace absent O/H/L with the close or invent monetary turnover."""
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results or chart.get("error"):
        raise ValueError("Yahoo 无有效行情结果")
    result = results[0]
    meta = result.get("meta") or {}
    if str(meta.get("symbol", "")).upper() != instrument["symbol"].upper():
        raise ValueError("Yahoo 返回了不同的交易品种")
    if meta.get("currency") and meta["currency"] != instrument["currency"]:
        raise ValueError("Yahoo 报价币种与目录不一致")
    quotes = (result.get("indicators") or {}).get("quote") or []
    if not quotes:
        raise ValueError("Yahoo 缺少 OHLC")
    q, candles = quotes[0], []
    stamps = result.get("timestamp") or []
    for i, stamp in enumerate(stamps):
        values = {short: (q.get(long) or [])[i] if i < len(q.get(long) or []) else None
                  for short, long in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}
        # Yahoo sometimes includes an entirely empty holiday/current-session slot.
        if all(values[key] is None for key in ("o", "h", "l", "c")):
            continue
        candles.append({"t": stamp, **values})
    return validate_daily_candles(candles, instrument)


def parse_stooq_daily(text: str, instrument: dict) -> list[dict]:
    if not text.strip() or "<html" in text.lower() or "<!doctype" in text.lower():
        raise ValueError("Stooq 未返回 CSV 行情")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not set(("Date", "Open", "High", "Low", "Close")).issubset(reader.fieldnames or []):
        raise ValueError("Stooq CSV 表头异常")
    candles, tz = [], ZoneInfo(instrument["timezone"])
    for row in reader:
        try:
            day = date.fromisoformat(row["Date"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Stooq 日期无效") from None
        candles.append({"t": int(datetime.combine(day, datetime_time(), tzinfo=tz).timestamp()),
                        "o": row.get("Open"), "h": row.get("High"), "l": row.get("Low"),
                        "c": row.get("Close"), "v": row.get("Volume") or None})
    return validate_daily_candles(candles, instrument)


def _start_date(range_param: str, today: date) -> date:
    if range_param == "max":
        return date(1900, 1, 1)
    if range_param == "ytd":
        return date(today.year, 1, 1)
    if range_param in ("1d", "5d"):
        return today - timedelta(days=14)
    months = {"1mo": 1, "3mo": 3, "6mo": 6, "1y": 12, "2y": 24, "5y": 60, "10y": 120}[range_param]
    year, month = divmod(today.year * 12 + today.month - 1 - months, 12)
    month += 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


async def _fetch_provider_daily(instrument: dict, provider: dict, range_param: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), headers={"User-Agent": "Mozilla/5.0"},
                                 follow_redirects=True) as client:
        if provider["id"].startswith("yahoo"):
            host = "query2" if provider["id"] == "yahoo_query2" else "query1"
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(provider['symbol'], safe='')}"
            # Include a prior session for short-window change metrics.
            response = await client.get(url, params={"range": "1mo" if range_param in ("1d", "5d") else range_param,
                                                     "interval": "1d"})
            response.raise_for_status()
            return parse_yahoo_daily(response.json(), instrument)
        if provider["id"] == "stooq":
            today = _local_date(time.time(), instrument)
            response = await client.get("https://stooq.com/q/d/l/", params={"s": provider["symbol"], "i": "d",
                "d1": _start_date(range_param, today).strftime("%Y%m%d"), "d2": today.strftime("%Y%m%d")})
            response.raise_for_status()
            return parse_stooq_daily(response.text, instrument)
    raise ValueError("未知行情源")


def aggregate_daily(candles: list[dict], interval: str, instrument: dict) -> list[dict]:
    if interval == "1d":
        return copy.deepcopy(candles)
    if interval not in ("1wk", "1mo"):
        raise ValueError("全球行情仅支持日、周、月 K 线")
    bars, last_group = [], None
    for row in candles:
        day = _local_date(row["t"], instrument)
        group = day.isocalendar()[:2] if interval == "1wk" else (day.year, day.month)
        if group != last_group:
            bars.append(dict(row))
            last_group = group
        else:
            bar = bars[-1]
            bar.update(h=max(bar["h"], row["h"]), l=min(bar["l"], row["l"]), c=row["c"],
                       v=bar["v"] + row["v"] if bar["v"] is not None and row["v"] is not None else None)
        bars[-1]["period_end"] = day.isoformat()
    return bars


def _attach_instrument(bundle: dict, instrument: dict) -> dict:
    out = copy.deepcopy(bundle)
    out["instrument"] = copy.deepcopy(instrument)
    out.update({field: instrument[field] for field in ("timezone", "region", "currency", "unit", "asset_class", "exchange", "volume_unit")})
    out.setdefault("index_key", instrument["key"])
    return out


def _cached_daily(entry: dict, instrument: dict, *, stale: bool, failures: list[str] | None = None) -> dict:
    out = copy.deepcopy(entry["payload"])
    out.update(served_from_cache=True, cache_stale=stale, cache_saved_at=entry.get("saved_at"),
               cache_source_range=entry.get("source_range", out.get("range")),
               data_as_of=_local_date(out["candles"][-1]["t"], instrument).isoformat())
    if stale:
        out["cache_warning"] = f"在线行情暂不可用，使用本地历史缓存（截至 {out['data_as_of']}）。"
        out["fetch_error_detail"] = "；".join(failures or [])
    return out


def _fresh(entry: dict) -> bool:
    try:
        saved = datetime.fromisoformat(entry["saved_at"].replace("Z", "+00:00"))
        age = time.time() - saved.replace(tzinfo=saved.tzinfo or timezone.utc).timestamp()
        return 0 <= age < CACHE_TTL_SECONDS
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False


def _recent(candles: list[dict], instrument: dict) -> bool:
    return (_local_date(time.time(), instrument) - _local_date(candles[-1]["t"], instrument)).days <= 7


def _read_daily_cache(instrument: dict, range_param: str) -> dict | None:
    entry = kline_bundle_cache.load_bundle(f"GLOBAL_DAILY_{instrument['key']}", range_param, "1d")
    if not entry:
        return None
    try:
        entry = copy.deepcopy(entry)
        payload = entry["payload"]
        if payload.get("index_key") != instrument["key"] or payload.get("symbol") != instrument["symbol"]:
            return None
        payload["candles"] = validate_daily_candles(payload["candles"], instrument)
        entry["source_range"] = range_param
        return entry
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _covers_requested_range(entry: dict, range_param: str, instrument: dict) -> bool:
    """A different cache's label alone never proves historical coverage.

    Require an actual observation on/before the requested calendar cutoff and an
    observation in that window. Missing sessions are still left absent; this is
    a coverage check, not a claim of a complete exchange trading calendar.
    """
    rows = entry["payload"]["candles"]
    if range_param in ("1d", "5d"):
        return len(rows) >= (1 if range_param == "1d" else 5)
    if range_param == "max":
        return False  # Only the provider's exact max request establishes this.
    start = _start_date(range_param, _local_date(time.time(), instrument))
    return _local_date(rows[0]["t"], instrument) <= start <= _local_date(rows[-1]["t"], instrument)


def _best_daily_cache(instrument: dict, range_param: str) -> dict | None:
    candidates = []
    for source_range in [range_param, *sorted(SUPPORTED_RANGES - {range_param})]:
        entry = _read_daily_cache(instrument, source_range)
        if entry and (source_range == range_param or _covers_requested_range(entry, range_param, instrument)):
            candidates.append(entry)
    if not candidates:
        return None

    def rank(entry: dict) -> tuple:
        try:
            saved = datetime.fromisoformat(str(entry.get("saved_at", "")).replace("Z", "+00:00"))
            observation = saved.replace(tzinfo=saved.tzinfo or timezone.utc).timestamp()
        except (ValueError, OverflowError):
            observation = 0
        return entry["payload"]["candles"][-1]["t"], observation, entry["source_range"] == range_param

    return max(candidates, key=rank)


def _record_health(provider: str, ok: bool) -> None:
    market_source_cache.record_health(f"global_{provider}", ok=ok, detail="validated" if ok else "request_failed")


async def _refresh_daily(instrument: dict, range_param: str, cached: dict | None) -> dict:
    failures, outdated, cooldowns = [], None, []
    for provider in instrument["providers"]:
        try:
            candles = await asyncio.wait_for(_request_provider_daily(instrument, provider, range_param),
                                             timeout=PROVIDER_TIMEOUT_SECONDS)
            candles = validate_daily_candles(candles, instrument)
            if cached and candles[-1]["t"] < cached["payload"]["candles"][-1]["t"]:
                raise ValueError("来源比已缓存行情更旧")
            out = dict(index_key=instrument["key"], symbol=instrument["symbol"], range=range_param,
                       interval="1d", interval_effective="1d", candles=candles,
                       kline_source=provider["id"], source_url=provider["url"], provider_symbol=provider["symbol"],
                       fetched_at=datetime.now(timezone.utc).isoformat(), served_from_cache=False,
                       cache_stale=False, data_as_of=candles[-1]["date"])
            if not _recent(candles, instrument):
                if not outdated or candles[-1]["t"] > outdated["candles"][-1]["t"]:
                    outdated = out
                raise ValueError("来源行情长期未更新")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Avoid exposing request URLs/headers or private network diagnostics.
            if isinstance(exc, _ProviderCooldown):
                cooldowns.append(exc)
            failures.append(f"{provider['id']}: {exc.error_code if isinstance(exc, _ProviderCooldown) else type(exc).__name__}")
            _record_health(provider["id"], False)
            continue
        kline_bundle_cache.save_bundle(f"GLOBAL_DAILY_{instrument['key']}", range_param, "1d", out)
        saved = kline_bundle_cache.load_bundle(f"GLOBAL_DAILY_{instrument['key']}", range_param, "1d")
        out["cache_saved_at"] = (saved or {}).get("saved_at")
        _record_health(provider["id"], True)
        return out
    error_code = ("source_rate_limited" if any(e.error_code == "source_rate_limited" for e in cooldowns)
                  else "provider_cooldown" if cooldowns else "source_unavailable")
    retry_at = datetime.fromtimestamp(min(e.until for e in cooldowns), timezone.utc).isoformat() if cooldowns else None
    failure_meta = {"error_code": error_code, "provider_retry_at": retry_at}
    if cached:
        return {**_cached_daily(cached, instrument, stale=True, failures=failures), **failure_meta}
    if outdated:
        outdated.update(failure_meta)
        outdated.update(cache_stale=True, cache_warning="来源最新记录已超过 7 天，仅供查看历史。",
                        fetch_error_detail="；".join(failures))
        return outdated
    raise GlobalMarketUnavailable("全球行情源暂不可用；" + "；".join(failures),
                                  error_code=error_code, retry_at=retry_at)


async def _daily_bundle(instrument: dict, range_param: str, force_refresh: bool) -> dict:
    loop = asyncio.get_running_loop()
    flights = _FLIGHTS.setdefault(loop, {})
    key = (instrument["key"], range_param)
    flight = flights.get(key)
    if flight is None:
        cached = _best_daily_cache(instrument, range_param)
        if cached and not force_refresh and _fresh(cached) and _recent(cached["payload"]["candles"], instrument):
            return _cached_daily(cached, instrument, stale=False)
        flight = {"task": asyncio.create_task(_refresh_daily(instrument, range_param, cached)), "waiters": 0}
        flights[key] = flight

        def finished(task: asyncio.Task) -> None:
            if flights.get(key) is flight:
                flights.pop(key)
            if not task.cancelled():
                task.exception()

        flight["task"].add_done_callback(finished)
    flight["waiters"] += 1
    try:
        return copy.deepcopy(await asyncio.shield(flight["task"]))
    finally:
        flight["waiters"] -= 1
        if flight["waiters"] == 0 and not flight["task"].done():
            if flights.get(key) is flight:
                flights.pop(key)
            flight["task"].cancel()
            await asyncio.gather(flight["task"], return_exceptions=True)


def _cn_source_url(out: dict, instrument: dict) -> str | None:
    source = str(out.get("kline_source") or "")
    if source == "yahoo_stooq_fallback":
        stooq = "stooq" in str((out.get("metrics") or {}).get("data_source", "")).lower()
        return "https://stooq.com/q/d/l/" if stooq else _yahoo_quote_url(instrument["symbol"])
    for provider in instrument["providers"]:
        if provider["id"] in source:
            return provider["url"]
    return out.get("source_url")


def _yahoo_quote_url(symbol: str) -> str:
    return f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/"


def _period_last_date(row: dict, interval: str, instrument: dict) -> date:
    day = _local_date(row["t"], instrument)
    if row.get("period_end"):
        try:
            observed_end = date.fromisoformat(str(row["period_end"]))
            if observed_end >= day:
                return observed_end
        except ValueError:
            pass
    if interval == "1wk":
        return day + timedelta(days=6 - day.weekday())
    if interval == "1mo":
        return day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return day


def _calendar_slice(candles: list[dict], range_param: str, interval: str, instrument: dict) -> list[dict]:
    """Select bars overlapping the calendar window, retaining their full OHLC.

    A weekly/monthly candle starting before the cutoff remains visible when its
    period ends within the requested window. For native provider bars without
    period_end, the containing calendar period is used only for selection, never
    to invent a last observed trading date or change the source's data_as_of.
    """
    if range_param in ("1d", "5d"):
        return candles[-(1 if range_param == "1d" else 5):]
    today = _local_date(time.time(), instrument)
    start = _start_date(range_param, today)
    return [row for row in candles if _local_date(row["t"], instrument) <= today
            and _period_last_date(row, interval, instrument) >= start]


def _period_metrics(candles: list[dict], full_history: list[dict], instrument: dict,
                    interval: str, original: dict | None = None) -> dict:
    last = candles[-1]
    def period_key(row: dict) -> tuple:
        day = _local_date(row["t"], instrument)
        return day.isocalendar()[:2] if interval == "1wk" else (day.year, day.month) if interval == "1mo" else (day,)

    previous_rows = [row for row in full_history if period_key(row) < period_key(last)]
    previous = previous_rows[-1]["c"] if previous_rows else None
    change_pct = round((last["c"] - previous) / previous * 100, 2) if previous and previous > 0 else None
    return {**(original or {}), "symbol": instrument["symbol"], "name": instrument["label"],
            "currency": instrument["currency"], "exchange": instrument["exchange"],
            "latest_close": last["c"], "previous_close": previous, "change_pct": change_pct,
            "latest_open": last["o"], "latest_high": last["h"], "latest_low": last["l"],
            "latest_volume": last.get("v"), "latest_amount": last.get("amount"),
            "latest_turnover_rate": last.get("turnover_rate"), "interval_effective": interval}


async def fetch_market_kline(index_key: str, range_param: str = "1y", interval: str = "1d", *,
                             force_refresh: bool = False, public_only: bool = False) -> dict:
    instrument = get_market_instrument(index_key)
    if not instrument:
        raise ValueError("未知大盘指数或期货代码")
    if range_param not in SUPPORTED_RANGES or interval not in instrument["intervals"]:
        raise ValueError("不支持的行情时间范围或周期（仅日、周、月 K 线）")
    if instrument["region"] == "CN":
        out = await kline_pipeline.fetch_major_index_kline_with_fallbacks(instrument["key"], range_param, interval,
            force_refresh=force_refresh, public_only=public_only)
        out = _attach_instrument(out, instrument)
        full_history = out.get("candles") or []
        candles = _calendar_slice(full_history, range_param, interval, instrument)
        if not candles:
            raise GlobalMarketUnavailable("所选范围没有有效行情")
        out.update(candles=candles, range=range_param, interval=interval, interval_effective=interval,
                   range_boundary_semantics="include_overlapping_period",
                   metrics=_period_metrics(candles, full_history, instrument, interval, out.get("metrics")))
        source_url = _cn_source_url(out, instrument)
        if source_url:
            out["source_url"] = source_url
        return out
    daily = await _daily_bundle(instrument, range_param, force_refresh)
    daily_rows = daily["candles"]
    metrics_rows = aggregate_daily(daily_rows, interval, instrument)
    if range_param in ("1d", "5d"):
        candles = aggregate_daily(daily_rows[-(1 if range_param == "1d" else 5):], interval, instrument)
    else:
        candles = _calendar_slice(metrics_rows, range_param, interval, instrument)
    # An old source may be useful as explicitly stale historical data.
    if not candles and daily.get("cache_stale"):
        candles = metrics_rows
    if not candles:
        raise RuntimeError("所选范围没有有效行情")
    out = {**daily, "candles": candles, "range": range_param, "interval": interval, "interval_effective": interval,
           "range_boundary_semantics": "include_overlapping_period", "aggregation_source_interval": "1d",
           "metrics": _period_metrics(candles, metrics_rows, instrument, interval,
                                      {"shares_outstanding": None, "data_source": daily["kline_source"]})}
    out = _attach_instrument(out, instrument)
    # Keep the existing chart/export cache namespace for consumers that read it.
    # Stale fallback must never advance the last successful observation timestamp.
    if not out.get("cache_stale") and not out.get("served_from_cache"):
        kline_bundle_cache.save_bundle(f"INDEX_{instrument['key']}", range_param, interval, out)
    return out
