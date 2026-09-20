"""
行情扩展：直连公开 HTTP 接口（腾讯日/周/月 fqkline、腾讯分钟 mkline、新浪 K 线、东财数据中心与同花顺部分页面）。
新浪/腾讯 URL 与参数思路参考开源 [Ashare](https://github.com/mpquant/Ashare)。

不依赖第三方 Python 行情 SDK；与 eastmoney_market 一样使用 httpx。

说明：非官方 SLA，字段可能变更；仅供学习研究。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.services.market_time import SHANGHAI_TZ, market_timestamp
from app.storage.integrations_store import load_integrations

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REFERER_EM = "https://quote.eastmoney.com/"
REFERER_QQ = "https://finance.qq.com/"
REFERER_SINA = "https://finance.sina.com.cn/"
REFERER_BAIDU = "https://gushitong.baidu.com/"
_TIMEOUT = httpx.Timeout(45.0, connect=18.0)

_A_SHARE_RE = re.compile(r"^(\d{6})\.(SS|SH|SZ|BJ)$", re.I)

_DAILY_PLUS_INTERVALS = frozenset({"1d", "5d", "1wk", "1mo"})

# 东财分钟失败时，用腾讯 ifzq mkline 兜底（Ashare get_price_min_tx）
_MINUTE_FALLBACK_INTERVALS = frozenset({"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"})

_RANGE_LOOKBACK_DAYS: dict[str, int] = {
    "1d": 14,
    "5d": 21,
    "1mo": 45,
    "3mo": 120,
    "6mo": 200,
    "1y": 400,
    "2y": 800,
    "5y": 1600,
    "10y": 3200,
    "ytd": 400,
    "max": 4000,
}


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "on")


def integrations_proxy_tuple() -> tuple[bool, str | None, str | None]:
    """与历史 integrations 键名兼容：adata_proxy_* 实际作为全局行情 HTTP 代理使用。"""
    data = load_integrations()
    enabled = (
        _truthy(data.get("adata_proxy_enabled"))
        or settings.adata_proxy_enabled
    )
    ip = (data.get("adata_proxy_ip") or "").strip() or (settings.adata_proxy_ip or "").strip() or None
    purl = (data.get("adata_proxy_url") or "").strip() or (settings.adata_proxy_url or "").strip() or None
    use = enabled and bool(ip or purl)
    return use, ip, purl


def _httpx_proxy_url() -> str | None:
    use, ip, _purl = integrations_proxy_tuple()
    if not use:
        return None
    if ip:
        return ip if "://" in ip else f"http://{ip}"
    return None


def _sync_client() -> httpx.Client:
    proxy = _httpx_proxy_url()
    return httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": UA}, proxy=proxy, follow_redirects=True)


def _async_client() -> httpx.AsyncClient:
    proxy = _httpx_proxy_url()
    return httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": UA},
        proxy=proxy,
        follow_redirects=True,
    )


def interval_allows_secondary_kline(interval: str) -> bool:
    return (interval or "").strip().lower() in _DAILY_PLUS_INTERVALS


def interval_allows_tencent_minute_kline(interval: str) -> bool:
    return (interval or "").strip().lower() in _MINUTE_FALLBACK_INTERVALS


def _tencent_mkline_slot(interval: str) -> str:
    """腾讯 mkline 仅支持 m1/m5/m15/m30/m60；2m→m1，90m/1h→m60。"""
    iv = (interval or "").strip().lower()
    if iv in ("1m", "2m"):
        return "m1"
    if iv == "5m":
        return "m5"
    if iv == "15m":
        return "m15"
    if iv == "30m":
        return "m30"
    if iv in ("60m", "90m", "1h"):
        return "m60"
    raise ValueError(f"不支持腾讯 mkline 周期: {interval}")


def _tencent_minute_bar_count(range_param: str) -> int:
    from app.services.eastmoney_market import _RANGE_LMT

    lmt = _RANGE_LMT.get(range_param, 280)
    return min(max(lmt * 4, 120), 1200)


def _parse_tencent_mkline_time(cell: Any) -> int | None:
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        x = float(cell)
        if x > 1e12:
            return int(round(x / 1000.0))
        if x > 1e9:
            return int(round(x))
        return int(round(x))
    s = str(cell).strip()
    if s.isdigit() and len(s) >= 12:
        try:
            y, mo, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
            hh, mm = int(s[8:10]), int(s[10:12])
            dt = datetime(y, mo, d, hh, mm, 0)
            return market_timestamp(dt)
        except ValueError:
            pass
    if len(s) >= 19:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            return market_timestamp(dt)
        except ValueError:
            pass
    if len(s) >= 16:
        try:
            dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
            return market_timestamp(dt)
        except ValueError:
            pass
    return None


def _tencent_qt_last_close(qt_root: Any, tpre: str) -> float | None:
    if not isinstance(qt_root, dict):
        return None
    arr = qt_root.get(tpre)
    if not isinstance(arr, (list, tuple)) or len(arr) <= 3:
        return None
    try:
        return float(str(arr[3]).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _tencent_mkline_rows_to_candles(
    rows: list[Any],
    qt_close: float | None,
) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts = _parse_tencent_mkline_time(row[0])
            if ts is None:
                continue
            o, c, h, low = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
            vol_hand = float(row[5])
            v = int(round(vol_hand * 100.0))
        except (ValueError, TypeError, IndexError):
            continue
        candles.append(
            {
                "t": ts,
                "o": round(o, 4),
                "h": round(h, 4),
                "l": round(low, 4),
                "c": round(c, 4),
                "v": v,
                "amount": 0.0,
                "turnover_rate": None,
            }
        )
    candles.sort(key=lambda x: x["t"])
    if candles and qt_close is not None and qt_close > 0:
        candles[-1]["c"] = round(qt_close, 4)
    return candles


def symbol_six_digit(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return s.split(".", 1)[0] if "." in s else s


def _want_bar_cap(range_param: str, interval: str) -> int:
    iv = (interval or "1d").strip().lower()
    rp = (range_param or "1y").strip().lower()
    base = _RANGE_LOOKBACK_DAYS.get(rp, 280)
    if iv == "1wk":
        return max(30, min(base // 5, 520))
    if iv == "1mo":
        return max(24, min(base // 22, 240))
    return max(30, min(base + 30, 5000))


def _start_date_str(range_param: str) -> str:
    days = _RANGE_LOOKBACK_DAYS.get((range_param or "1y").strip().lower(), 400)
    d0 = datetime.now(SHANGHAI_TZ).date() - timedelta(days=days)
    return d0.strftime("%Y-%m-%d")


def _tencent_prefix(code: str) -> str:
    c = code.strip()
    if c.startswith("6"):
        return f"sh{c}"
    if c.startswith(("0", "3")):
        return f"sz{c}"
    return f"bj{c}"


def _tencent_k_type(interval: str) -> tuple[str, str]:
    iv = (interval or "1d").strip().lower()
    if iv == "1wk":
        return "week", "qfqweek"
    if iv == "1mo":
        return "month", "qfqmonth"
    return "day", "qfqday"


def _baidu_k_type(interval: str) -> int:
    iv = (interval or "1d").strip().lower()
    if iv == "1wk":
        return 2
    if iv == "1mo":
        return 3
    return 1


def _tencent_rows_to_candles(rows: list, sym_upper: str) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in rows:
        if not row or len(row) < 6:
            continue
        try:
            ds = str(row[0]).strip()
            o, c, h, low = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
            vol_hand = float(row[5])
            v = int(round(vol_hand * 100.0))
            dt = datetime.strptime(ds[:10], "%Y-%m-%d")
            ts = market_timestamp(dt)
        except (ValueError, TypeError, IndexError):
            continue
        candles.append(
            {
                "t": ts,
                "o": round(o, 4),
                "h": round(h, 4),
                "l": round(low, 4),
                "c": round(c, 4),
                "v": v,
                "amount": 0.0,
                "turnover_rate": None,
            }
        )
    candles.sort(key=lambda x: x["t"])
    return candles


def _baidu_rows_to_candles(keys: list[Any], market_data: Any) -> list[dict[str, Any]]:
    names = [str(k) for k in keys]
    candles: list[dict[str, Any]] = []
    for line in str(market_data or "").split(";"):
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < len(names):
            continue
        row = dict(zip(names, parts, strict=False))
        ds = row.get("time") or row.get("date") or row.get("trade_time")
        if not ds:
            continue
        try:
            dt = datetime.strptime(str(ds).replace("/", "-")[:10], "%Y-%m-%d")
            o = float(str(row.get("open") or "").replace(",", ""))
            c = float(str(row.get("close") or "").replace(",", ""))
            h = float(str(row.get("high") or "").replace(",", ""))
            low = float(str(row.get("low") or "").replace(",", ""))
            v = int(round(float(str(row.get("volume") or 0).replace(",", ""))))
            amount_raw = row.get("amount")
            amount = float(str(amount_raw).replace(",", "")) if amount_raw not in (None, "", "--") else 0.0
            turnover_raw = row.get("turnoverratio") or row.get("turnover_rate")
            turnover = float(str(turnover_raw).replace("%", "")) if turnover_raw not in (None, "", "--") else None
        except (TypeError, ValueError):
            continue
        candles.append(
            {
                "t": market_timestamp(dt),
                "o": round(o, 4),
                "h": round(h, 4),
                "l": round(low, 4),
                "c": round(c, 4),
                "v": v,
                "amount": amount,
                "turnover_rate": turnover,
            }
        )
    candles.sort(key=lambda x: x["t"])
    return candles


def fetch_kline_bundle_baidu_sync(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    """同步：百度股市通 K 线（adata 同源 quotation_kline_ab），作为日/周/月独立兜底。"""
    from app.services.eastmoney_market import _build_metrics

    sym = symbol.strip().upper()
    m = _A_SHARE_RE.match(sym)
    if not m:
        raise ValueError("无效 A 股代码")
    if not interval_allows_secondary_kline(interval):
        raise ValueError("百度股市通仅作为日/周/月 K 线兜底")
    code = m.group(1)
    start = _start_date_str(range_param)
    url = "https://finance.pae.baidu.com/selfselect/getstockquotation"
    params = {
        "all": "1",
        "isIndex": "false",
        "isBk": "false",
        "isBlock": "false",
        "isFutures": "false",
        "isStock": "true",
        "newFormat": "1",
        "group": "quotation_kline_ab",
        "finClientType": "pc",
        "code": code,
        "start_time": f"{start} 00:00:00",
        "ktype": str(_baidu_k_type(interval)),
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.finance-web.v1+json",
        "Origin": REFERER_BAIDU.rstrip("/"),
        "Referer": REFERER_BAIDU,
    }
    with _sync_client() as client:
        r = client.get(url, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
    if str(data.get("ResultCode")) != "0":
        raise ValueError(str(data.get("ResultMessage") or "百度股市通接口错误"))
    result = data.get("Result") or {}
    block = result.get("newMarketData") or {}
    candles = _baidu_rows_to_candles(block.get("keys") or [], block.get("marketData"))
    if not candles:
        raise ValueError("百度股市通未返回 K 线数据")
    cap = _want_bar_cap(range_param, interval)
    if len(candles) > cap:
        candles = candles[-cap:]

    last = candles[-1]
    prev_close = candles[-2]["c"] if len(candles) >= 2 else last.get("c")
    q: dict[str, Any] = {
        "name": "",
        "latest": last.get("c"),
        "open": last.get("o"),
        "high": last.get("h"),
        "low": last.get("l"),
        "prev_close": prev_close,
        "shares_total": None,
        "turnover_rate_snapshot": last.get("turnover_rate"),
        "total_market_cap_yuan": None,
        "float_market_cap_yuan": None,
        "pe_ttm": None,
    }
    eff = (interval or "1d").strip().lower()
    if eff not in _DAILY_PLUS_INTERVALS:
        eff = "1d"
    metrics = _build_metrics(sym, candles, q, eff)
    metrics["data_source"] = "百度股市通 finance.pae.baidu.com quotation_kline_ab（adata 同源，独立兜底）"
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": eff,
        "candles": candles,
        "metrics": metrics,
        "kline_source": "baidu_fallback",
    }


def fetch_kline_bundle_secondary_sync(
    symbol: str,
    range_param: str,
    interval: str,
    *,
    provider_symbol: str | None = None,
) -> dict[str, Any]:
    """同步：供 asyncio.to_thread；腾讯 fqkline 前复权日/周/月。"""
    from app.services.eastmoney_market import _build_metrics

    sym = symbol.strip().upper()
    m = _A_SHARE_RE.match(sym)
    if not m:
        raise ValueError("无效 A 股代码")
    code = m.group(1)
    tpre = (provider_symbol or _tencent_prefix(code)).strip().lower()
    kparam, arr_key = _tencent_k_type(interval)
    start = _start_date_str(range_param)
    end = datetime.now(SHANGHAI_TZ).date().strftime("%Y-%m-%d")
    n = min(_want_bar_cap(range_param, interval) + 80, 1200)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{tpre},{kparam},{start},{end},{n},qfq"}

    with _sync_client() as client:
        r = client.get(
            url,
            params=params,
            headers={"User-Agent": UA, "Referer": REFERER_QQ},
        )
        r.raise_for_status()
        data = r.json()

    block = (data.get("data") or {}).get(tpre) or {}
    rows = block.get(arr_key) or []
    candles = _tencent_rows_to_candles(rows, sym)
    if not candles:
        raise ValueError("腾讯行情未返回 K 线数据")
    cap = _want_bar_cap(range_param, interval)
    if len(candles) > cap:
        candles = candles[-cap:]
    last = candles[-1]
    prev_close = candles[-2]["c"] if len(candles) >= 2 else last.get("c")
    q: dict[str, Any] = {
        "name": "",
        "latest": last.get("c"),
        "open": last.get("o"),
        "high": last.get("h"),
        "low": last.get("l"),
        "prev_close": prev_close,
        "shares_total": None,
        "turnover_rate_snapshot": None,
        "total_market_cap_yuan": None,
        "float_market_cap_yuan": None,
        "pe_ttm": None,
    }
    eff = (interval or "1d").strip().lower()
    if eff not in _DAILY_PLUS_INTERVALS:
        eff = "1d"
    metrics = _build_metrics(sym, candles, q, eff)
    metrics["data_source"] = "腾讯财经 web.ifzq.gtimg.cn fqkline（前复权，主源失败兜底）"
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": eff,
        "candles": candles,
        "metrics": metrics,
        "kline_source": "tencent_fallback",
    }


def fetch_kline_bundle_tencent_minute_sync(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    """同步：腾讯 ifzq mkline 分钟 K（Ashare get_price_min_tx）；成交量按手×100 转股，与东财一致。"""
    from app.services.eastmoney_market import _build_metrics

    sym = symbol.strip().upper()
    m = _A_SHARE_RE.match(sym)
    if not m:
        raise ValueError("无效 A 股代码")
    code = m.group(1)
    tpre = _tencent_prefix(code)
    slot = _tencent_mkline_slot(interval)
    count = _tencent_minute_bar_count(range_param)
    url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
    params = {"param": f"{tpre},{slot},,{count}"}

    with _sync_client() as client:
        r = client.get(
            url,
            params=params,
            headers={"User-Agent": UA, "Referer": REFERER_QQ},
        )
        r.raise_for_status()
        data = r.json()
    if int(data.get("code") or 0) != 0:
        raise ValueError(str(data.get("msg") or "腾讯 mkline 接口错误"))

    block = (data.get("data") or {}).get(tpre) or {}
    rows = block.get(slot) or []
    if not isinstance(rows, list):
        raise ValueError("腾讯 mkline 数据结构异常")
    qt_close = _tencent_qt_last_close(block.get("qt"), tpre)
    candles = _tencent_mkline_rows_to_candles(rows, qt_close)
    if not candles:
        raise ValueError("腾讯 mkline 未返回 K 线数据")

    cap = min(_tencent_minute_bar_count(range_param), len(candles))
    if len(candles) > cap:
        candles = candles[-cap:]

    last = candles[-1]
    prev_close = candles[-2]["c"] if len(candles) >= 2 else last.get("c")
    q: dict[str, Any] = {
        "name": "",
        "latest": last.get("c"),
        "open": last.get("o"),
        "high": last.get("h"),
        "low": last.get("l"),
        "prev_close": prev_close,
        "shares_total": None,
        "turnover_rate_snapshot": None,
        "total_market_cap_yuan": None,
        "float_market_cap_yuan": None,
        "pe_ttm": None,
    }
    eff = (interval or "1m").strip().lower()
    metrics = _build_metrics(sym, candles, q, eff)
    note_parts = [
        "腾讯财经 ifzq.gtimg.cn kline/mkline（分钟，东财失败兜底）",
        f"腾讯周期槽 {slot}",
    ]
    if eff in ("2m",):
        note_parts.append("请求 2m 时使用 m1 数据")
    if eff in ("90m", "1h"):
        note_parts.append("请求 90m/1h 时使用 m60 数据")
    metrics["data_source"] = "；".join(note_parts)
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": eff,
        "candles": candles,
        "metrics": metrics,
        "kline_source": "tencent_mkline_fallback",
        "tencent_mkline_slot": slot,
    }


def _sina_scale_for_interval(interval: str) -> int:
    """与 Ashare 一致：日 240、周 1200、月 7200（分钟粒度编码）。"""
    iv = (interval or "1d").strip().lower()
    if iv == "1wk":
        return 1200
    if iv == "1mo":
        return 7200
    return 240


def _sina_rows_to_candles(rows: list[Any]) -> list[dict[str, Any]]:
    """解析新浪 CN_MarketData.getKLineData JSON 数组。实测 volume 已为股数级，与东财 candle.v 一致，不再 ×100。"""
    candles: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ds = str(row.get("day") or "").strip()
            if not ds:
                continue
            o = float(row["open"])
            h = float(row["high"])
            low = float(row["low"])
            c = float(row["close"])
            v = int(round(float(row.get("volume") or 0)))
            dt = datetime.strptime(ds.replace("T", " ")[:10], "%Y-%m-%d")
            ts = market_timestamp(dt)
        except (ValueError, TypeError, KeyError):
            continue
        candles.append(
            {
                "t": ts,
                "o": round(o, 4),
                "h": round(h, 4),
                "l": round(low, 4),
                "c": round(c, 4),
                "v": v,
                "amount": 0.0,
                "turnover_rate": None,
            }
        )
    candles.sort(key=lambda x: x["t"])
    return candles


def fetch_kline_bundle_sina_sync(
    symbol: str,
    range_param: str,
    interval: str,
    *,
    provider_symbol: str | None = None,
) -> dict[str, Any]:
    """同步：新浪财经 getKLineData；东财、腾讯均失败时的第三源（仅日/周/月周期，与 interval_allows_secondary_kline 一致）。"""
    from app.services.eastmoney_market import _build_metrics

    sym = symbol.strip().upper()
    m = _A_SHARE_RE.match(sym)
    if not m:
        raise ValueError("无效 A 股代码")
    code = m.group(1)
    spre = (provider_symbol or _tencent_prefix(code)).strip().lower()
    scale = _sina_scale_for_interval(interval)
    cap = _want_bar_cap(range_param, interval)
    datalen = min(cap + 120, 1023)
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": spre, "scale": str(scale), "ma": "5", "datalen": str(datalen)}

    with _sync_client() as client:
        r = client.get(
            url,
            params=params,
            headers={"User-Agent": UA, "Referer": REFERER_SINA},
        )
        r.raise_for_status()
        text = (r.text or "").strip()
        if not text or text[0] not in "[{":
            raise ValueError("新浪行情返回非 JSON")
        try:
            raw = r.json()
        except json.JSONDecodeError:
            raw = json.loads(text)

    if not isinstance(raw, list):
        raise ValueError("新浪行情数据结构异常")
    candles = _sina_rows_to_candles(raw)
    if not candles:
        raise ValueError("新浪行情未返回 K 线数据")

    start = _start_date_str(range_param)
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    start_ts = market_timestamp(start_dt)
    candles = [c for c in candles if c["t"] >= start_ts]
    candles.sort(key=lambda x: x["t"])
    if len(candles) > cap:
        candles = candles[-cap:]
    if not candles:
        raise ValueError("新浪 K 线在请求区间内无数据")

    last = candles[-1]
    prev_close = candles[-2]["c"] if len(candles) >= 2 else last.get("c")
    q: dict[str, Any] = {
        "name": "",
        "latest": last.get("c"),
        "open": last.get("o"),
        "high": last.get("h"),
        "low": last.get("l"),
        "prev_close": prev_close,
        "shares_total": None,
        "turnover_rate_snapshot": None,
        "total_market_cap_yuan": None,
        "float_market_cap_yuan": None,
        "pe_ttm": None,
    }
    eff = (interval or "1d").strip().lower()
    if eff not in _DAILY_PLUS_INTERVALS:
        eff = "1d"
    metrics = _build_metrics(sym, candles, q, eff)
    metrics["data_source"] = (
        "新浪财经 money.finance.sina.com.cn CN_MarketData.getKLineData（Ashare 同源思路，第三兜底）"
    )
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": eff,
        "candles": candles,
        "metrics": metrics,
        "kline_source": "sina_fallback",
    }


def _em_secucodes(symbol_upper: str) -> str:
    m = _A_SHARE_RE.match(symbol_upper.strip())
    if not m:
        raise ValueError("无效代码")
    code, suf = m.group(1), m.group(2).upper()
    if suf in ("SS", "SH"):
        return f"{code}.SH"
    if suf == "BJ":
        return f"{code}.BJ"
    return f"{code}.SZ"


def _em_capital_secid(code: str) -> str:
    return f"1.{code}" if code.startswith("6") else f"0.{code}"


async def fetch_north_flow_history(start_date: str | None = None) -> list[dict[str, Any]]:
    base = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get?"
        "sortColumns=TRADE_DATE&sortTypes=-1&pageSize=200&pageNumber={pn}&"
        "reportName=RPT_MUTUAL_DEAL_HISTORY&columns=TRADE_DATE,NET_DEAL_AMT,BUY_AMT,SELL_AMT&"
        "source=WEB&client=WEB&filter=(MUTUAL_TYPE%3D%22{type}%22)"
    )
    start_dt = None
    if start_date:
        start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d")

    async with _async_client() as client:
        merged: list[dict[str, Any]] = []
        for pn in range(1, 22):
            hgt_url = base.format(pn=pn, type="003")
            sgt_url = base.format(pn=pn, type="001")
            rh, rs = await client.get(hgt_url, headers={"Referer": REFERER_EM}), await client.get(
                sgt_url, headers={"Referer": REFERER_EM}
            )
            rh.raise_for_status()
            rs.raise_for_status()
            jh, js = rh.json(), rs.json()
            if not jh.get("success") or not js.get("success"):
                break
            dh = (jh.get("result") or {}).get("data") or []
            ds_gt = (js.get("result") or {}).get("data") or []
            if not dh or not ds_gt:
                break
            stop = False
            for hi, si in zip(dh, ds_gt):
                td = (hi.get("TRADE_DATE") or "")[:10]
                if start_dt and datetime.strptime(td, "%Y-%m-%d") < start_dt:
                    stop = True
                    break
                n_h = hi.get("NET_DEAL_AMT")
                n_s = si.get("NET_DEAL_AMT")
                if n_h is None or n_s is None:
                    continue
                bh, sh = hi.get("BUY_AMT"), hi.get("SELL_AMT")
                bs, ss = si.get("BUY_AMT"), si.get("SELL_AMT")
                merged.append(
                    {
                        "trade_date": td,
                        "net_hgt": int(math.ceil(float(n_h) * 1_000_000)),
                        "buy_hgt": int(math.ceil(float(bh) * 1_000_000)) if bh is not None else None,
                        "sell_hgt": int(math.ceil(float(sh) * 1_000_000)) if sh is not None else None,
                        "net_sgt": int(math.ceil(float(n_s) * 1_000_000)),
                        "buy_sgt": int(math.ceil(float(bs) * 1_000_000)) if bs is not None else None,
                        "sell_sgt": int(math.ceil(float(ss) * 1_000_000)) if ss is not None else None,
                        "net_tgt": int(math.ceil((float(n_h) + float(n_s)) * 1_000_000)),
                    }
                )
            if stop:
                break
            if len(merged) >= 200:
                break
        return merged


async def fetch_north_flow_current() -> list[dict[str, Any]]:
    url = (
        "https://push2.eastmoney.com/api/qt/kamt.rtmin/get?"
        "fields1=f1,f3&fields2=f51,f52,f54,f56&ut=b2884a393a59ad64002292a3e90d46a5"
    )
    async with _async_client() as client:
        r = await client.get(url, headers={"Referer": REFERER_EM})
        r.raise_for_status()
        j = r.json()
    data = j.get("data") or {}
    s2n = data.get("s2n") or []
    last: dict[str, Any] | None = None
    for line in reversed(s2n):
        parts = str(line).split(",")
        if len(parts) < 4:
            continue
        if parts[1] == "-" or parts[2] == "-":
            continue
        try:
            hgt = float(parts[1]) * 10_000
            sgt = float(parts[2]) * 10_000
            tgt = float(parts[3]) * 10_000
        except ValueError:
            continue
        last = {
            "trade_time": parts[0],
            "net_hgt": int(math.ceil(hgt)),
            "net_sgt": int(math.ceil(sgt)),
            "net_tgt": int(math.ceil(tgt)),
        }
        break
    if not last:
        return []
    dpart = (data.get("s2nDate") or "").strip()
    trade_day = ""
    if len(dpart) >= 8 and dpart.isdigit():
        trade_day = f"{dpart[:4]}-{dpart[4:6]}-{dpart[6:8]}"
    elif "-" in dpart:
        ps = dpart.replace("/", "-").split("-")
        if len(ps) == 2 and all(p.strip().isdigit() for p in ps):
            y = datetime.now(SHANGHAI_TZ).year
            trade_day = f"{y}-{int(ps[0]):02d}-{int(ps[1]):02d}"
    tp = last["trade_time"]
    trade_time = f"{trade_day} {tp}:00" if trade_day else tp
    return [
        {
            "trade_time": trade_time,
            "net_hgt": last["net_hgt"],
            "net_sgt": last["net_sgt"],
            "net_tgt": last["net_tgt"],
        }
    ]


async def fetch_hot_pop_east() -> list[dict[str, Any]]:
    post_url = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
    body = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "pageNo": 1,
        "pageSize": 100,
    }
    async with _async_client() as client:
        r = await client.post(
            post_url,
            json=body,
            headers={"Referer": REFERER_EM, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        raw = r.json()
    lst = raw.get("data") or []
    marks: list[str] = []
    for item in lst:
        sc = str(item.get("sc") or "")
        if "SZ" in sc:
            marks.append("0." + sc[2:])
        else:
            marks.append("1." + sc[2:])
    if not marks:
        return []
    secids = ",".join(marks)
    qurl = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "ut": "f057cbcbce2a86e2866ab8877db1d059",
        "fltt": "2",
        "invt": "2",
        "fields": "f14,f3,f12,f2",
        "secids": secids,
    }
    async with _async_client() as client:
        r2 = await client.get(qurl, params=params, headers={"Referer": REFERER_EM})
        r2.raise_for_status()
        diff = ((r2.json() or {}).get("data") or {}).get("diff")
    rows: list[Any]
    if isinstance(diff, list):
        rows = diff
    elif isinstance(diff, dict):
        rows = [diff[k] for k in sorted(diff.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)]
    else:
        rows = []
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        pr = row.get("f2")
        pct = row.get("f3")
        try:
            price = float(pr) / 100.0 if pr is not None and float(pr) > 1000 else float(pr or 0)
        except (TypeError, ValueError):
            price = None
        try:
            chg_pct = float(pct) / 100.0 if pct is not None and abs(float(pct)) > 50 else float(pct or 0)
        except (TypeError, ValueError):
            chg_pct = None
        out.append(
            {
                "rank": i + 1,
                "stock_code": str(row.get("f12") or ""),
                "short_name": str(row.get("f14") or ""),
                "price": price,
                "change": (price * chg_pct / 100.0) if price is not None and chg_pct is not None else None,
                "change_pct": chg_pct,
            }
        )
    return out


async def fetch_hot_rank_ths() -> list[dict[str, Any]]:
    url = (
        "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock?"
        "stock_type=a&type=hour&list_type=normal"
    )
    headers = {
        "User-Agent": UA,
        "Host": "dq.10jqka.com.cn",
        "Referer": "https://eq.10jqka.com.cn/",
    }
    async with _async_client() as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        j = r.json()
    stocks = ((j.get("data") or {}).get("stock_list")) or []
    out: list[dict[str, Any]] = []
    for d in stocks:
        tag = d.get("tag") or {}
        ct = tag.get("concept_tag") or []
        concept_tag = ";".join(ct) if isinstance(ct, list) else ""
        pt = tag.get("popularity_tag") or ""
        out.append(
            {
                "rank": d.get("order"),
                "stock_code": str(d.get("code") or ""),
                "short_name": str(d.get("name") or ""),
                "change_pct": d.get("rise_and_fall"),
                "hot_value": d.get("rate"),
                "concept_tag": concept_tag,
                "pop_tag": str(pt).replace("\n", "") if pt else "",
            }
        )
    return out


async def fetch_hot_concept_ths() -> list[dict[str, Any]]:
    url = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/plate?type=concept"
    headers = {
        "User-Agent": UA,
        "Host": "dq.10jqka.com.cn",
        "Referer": "https://eq.10jqka.com.cn/",
    }
    async with _async_client() as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        j = r.json()
    plates = ((j.get("data") or {}).get("plate_list")) or []
    out: list[dict[str, Any]] = []
    for p in plates:
        out.append(
            {
                "rank": p.get("order"),
                "concept_code": str(p.get("code") or ""),
                "concept_name": str(p.get("name") or ""),
                "change_pct": p.get("rise_and_fall"),
                "hot_value": p.get("rate"),
                "hot_tag": p.get("hot_tag"),
            }
        )
    return out


async def fetch_securities_margin(start_date: str | None = None) -> list[dict[str, Any]]:
    start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d") if start_date else None
    out: list[dict[str, Any]] = []
    total_pages: int | None = None
    async with _async_client() as client:
        for pn in range(1, 200):
            if total_pages is not None and pn > total_pages:
                break
            api_url = (
                "https://datacenter-web.eastmoney.com/api/data/v1/get?"
                f"reportName=RPTA_RZRQ_LSHJ&columns=ALL&source=WEB&sortColumns=dim_date&sortTypes=-1&"
                f"pageNumber={pn}&pageSize=250"
            )
            r = await client.get(api_url, headers={"Referer": "https://data.eastmoney.com/"})
            r.raise_for_status()
            j = r.json()
            if not j.get("success"):
                break
            res = j.get("result") or {}
            if pn == 1:
                total_pages = int(res.get("pages") or 1)
            batch = res.get("data") or []
            for row in batch:
                dim = row.get("DIM_DATE") or ""
                td = dim[:10] if dim else ""
                if start_dt and td:
                    try:
                        if datetime.strptime(td, "%Y-%m-%d") < start_dt:
                            continue
                    except ValueError:
                        pass
                out.append(
                    {
                        "trade_date": td,
                        "rzye": row.get("RZYE"),
                        "rqye": row.get("RQYE"),
                        "rzrqye": row.get("RZRQYE"),
                        "rzrqyecz": row.get("RZRQYECZ"),
                    }
                )
            if not start_date:
                break
            if start_dt and batch:
                dim = (batch[-1].get("DIM_DATE") or "")[:10]
                try:
                    if datetime.strptime(dim, "%Y-%m-%d") <= start_dt:
                        break
                except ValueError:
                    pass
    return out


async def fetch_stock_capital_flow(symbol: str, start_date: str | None = None) -> list[dict[str, Any]]:
    code = symbol_six_digit(symbol)
    secid = _em_capital_secid(code)
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
        "lmt=0&klt=101&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&"
        f"secid={secid}"
    )
    async with _async_client() as client:
        r = await client.get(url, headers={"Referer": REFERER_EM})
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
    lines = data.get("klines") or []
    rows: list[dict[str, Any]] = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        td = parts[0][:10]
        if start_date and td < start_date[:10]:
            continue
        try:
            rows.append(
                {
                    "stock_code": code,
                    "trade_date": td,
                    "main_net_inflow": float(parts[1]),
                    "sm_net_inflow": float(parts[2]),
                    "mid_net_inflow": float(parts[3]),
                    "lg_net_inflow": float(parts[4]),
                    "max_net_inflow": float(parts[5]),
                }
            )
        except (ValueError, IndexError):
            continue
    return rows


async def fetch_stock_concepts_east(symbol: str) -> list[dict[str, Any]]:
    sc = _em_secucodes(symbol.strip().upper())
    from urllib.parse import quote

    filt = quote(f'(SECUCODE="{sc}")(IS_PRECISE="1")', safe="")
    url = (
        "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
        "reportName=RPT_F10_CORETHEME_BOARDTYPE&"
        "columns=SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,NEW_BOARD_CODE,BOARD_NAME,"
        "SELECTED_BOARD_REASON,IS_PRECISE,BOARD_RANK,BOARD_YIELD,DERIVE_BOARD_CODE&"
        "quoteColumns=f3~05~NEW_BOARD_CODE~BOARD_YIELD&"
        f"filter={filt}&pageNumber=1&pageSize=200&sortTypes=1&sortColumns=BOARD_RANK&"
        "source=HSF10&client=PC"
    )
    async with _async_client() as client:
        r = await client.get(url, headers={"Referer": REFERER_EM})
        r.raise_for_status()
        j = r.json()
    if not j.get("success"):
        return []
    data = (j.get("result") or {}).get("data") or []
    return [
        {
            "stock_code": _.get("SECURITY_CODE"),
            "concept_code": _.get("NEW_BOARD_CODE"),
            "name": _.get("BOARD_NAME"),
            "reason": _.get("SELECTED_BOARD_REASON"),
            "source": "东方财富",
        }
        for _ in data
    ]


async def fetch_etf_current(fund_code: str) -> list[dict[str, Any]]:
    code = (fund_code or "").strip()
    url = f"https://d.10jqka.com.cn/v6/line/hs_{code}/01/today.js"
    async with _async_client() as client:
        r = await client.get(url, headers={"User-Agent": UA, "Referer": "https://stockpage.10jqka.com.cn/"})
        r.raise_for_status()
        text = r.text
    i0 = text.find("{")
    i1 = text.rfind("}")
    if i0 < 0 or i1 <= i0:
        return []
    obj = json.loads(text[i0 : i1 + 1])
    key = f"hs_{code}"
    d = obj.get(key) or {}
    if not d:
        return []
    td = str(d.get("1") or "")
    dt = str(d.get("dt") or "")
    trade_time = f"{td[:4]}-{td[4:6]}-{td[6:8]} {dt[:2]}:{dt[2:4]}:{dt[4:6]}" if len(td) >= 8 and len(dt) >= 4 else td
    return [
        {
            "fund_code": code,
            "trade_time": trade_time,
            "trade_date": f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) >= 8 else td,
            "open": d.get("7"),
            "high": d.get("8"),
            "low": d.get("9"),
            "price": d.get("11"),
            "volume": d.get("13"),
            "amount": d.get("19"),
            "name": d.get("name"),
        }
    ]
