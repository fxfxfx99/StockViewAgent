"""
Yahoo Finance 非官方公开接口（与浏览器/行情插件同源思路）：
- K 线 OHLCV：https://query1.finance.yahoo.com/v8/finance/chart/{symbol}
- 股本与名称等：https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}

说明：Yahoo 可能限流或调整字段；仅供学习，非官方 SLA。
"""
from __future__ import annotations

import asyncio
import csv
import io
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
QUOTE_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return float(v)
    return None


async def _get_chart(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    params = {"range": range_param, "interval": interval, "includePrePost": "false"}
    url = CHART_URL.format(symbol=symbol)
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": UA}) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def _get_quote_summary(symbol: str) -> dict[str, Any] | None:
    modules = "price,summaryDetail,defaultKeyStatistics,quoteType"
    async with httpx.AsyncClient(timeout=25.0, headers={"User-Agent": UA}) as client:
        r = await client.get(
            QUOTE_URL.format(symbol=symbol),
            params={"modules": modules},
        )
        if r.status_code != 200:
            return None
        return r.json()


def _parse_chart_payload(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chart = data.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        err = (chart.get("error") or {}).get("description") or "无行情数据"
        raise ValueError(err)
    res = results[0]
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    quotes = (res.get("indicators") or {}).get("quote") or []
    if not quotes:
        raise ValueError("缺少 OHLCV")
    q = quotes[0]
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    vols = q.get("volume") or []

    candles: list[dict[str, Any]] = []
    for i, t in enumerate(ts):
        o, h, low, c, v = (
            _num(opens[i] if i < len(opens) else None),
            _num(highs[i] if i < len(highs) else None),
            _num(lows[i] if i < len(lows) else None),
            _num(closes[i] if i < len(closes) else None),
            _num(vols[i] if i < len(vols) else None),
        )
        if c is None and o is None:
            continue
        c = c if c is not None else o
        o = o if o is not None else c
        h = h if h is not None else max(filter(None, [o, c]) or [0])
        low = low if low is not None else min(filter(None, [o, c]) or [0])
        v = int(v) if v is not None else 0
        amount = (c or 0) * v if c is not None else None
        candles.append(
            {
                "t": int(t),
                "o": round(o or 0, 6),
                "h": round(h or 0, 6),
                "l": round(low or 0, 6),
                "c": round(c or 0, 6),
                "v": v,
                "amount": round(amount, 2) if amount is not None else None,
            }
        )
    return candles, meta


def _parse_shares_outstanding(qs: dict[str, Any] | None) -> int | None:
    if not qs:
        return None
    results = qs.get("quoteSummary") or {}
    rlist = results.get("result") or []
    if not rlist:
        return None
    r0 = rlist[0]
    dks = r0.get("defaultKeyStatistics") or {}
    raw = dks.get("sharesOutstanding")
    if raw is None:
        raw = (r0.get("summaryDetail") or {}).get("sharesOutstanding")
    if raw is None:
        return None
    try:
        return int(float(raw.get("raw") if isinstance(raw, dict) else raw))
    except (TypeError, ValueError):
        return None


_RANGE_ROWS: dict[str, int] = {
    "1d": 3,
    "5d": 10,
    "1mo": 28,
    "3mo": 72,
    "6mo": 140,
    "1y": 280,
    "2y": 560,
    "5y": 1300,
    "10y": 2800,
    "ytd": 300,
    "max": 100_000,
}


def _stooq_ticker(sym: str) -> str:
    u = sym.strip().upper()
    if "." in u:
        a, b = u.split(".", 1)
        if b in ("SS", "SZ", "HK", "L", "DE", "TO", "TSE", "F"):
            return f"{a.lower()}.{b.lower()}"
        return f"{a.lower()}-{b.lower()}.us"
    return f"{u.lower()}.us"


def _stooq_param_i(interval: str) -> str:
    if interval == "1wk":
        return "w"
    if interval == "1mo":
        return "m"
    return "d"


def _stooq_date_window(range_param: str) -> tuple[date, date]:
    """返回 (d1, d2) 日历区间，缩小 CSV 体积，避免拉全历史阻塞。"""
    end = date.today()
    if range_param == "ytd":
        return date(end.year, 1, 1), end
    if range_param == "max":
        return date(1990, 1, 1), end
    span = {
        "1d": 14,
        "5d": 21,
        "1mo": 50,
        "3mo": 130,
        "6mo": 220,
        "1y": 450,
        "2y": 900,
        "5y": 2200,
        "10y": 4500,
    }.get(range_param, 450)
    return end - timedelta(days=span), end


async def _fetch_stooq_candles(symbol: str, range_param: str, interval: str) -> tuple[list[dict[str, Any]], str]:
    """Stooq 日/周/月线下载；无总股本，换手率留空。"""
    eff = interval if interval in ("1d", "1wk", "1mo") else "1d"
    note = ""
    if interval not in ("1d", "1wk", "1mo"):
        note = "Yahoo 限流时使用 Stooq 仅提供日线；已忽略分钟/小时周期。"
    tick = _stooq_ticker(symbol)
    d1, d2 = _stooq_date_window(range_param)
    params = {
        "s": tick,
        "i": _stooq_param_i(eff),
        "d1": d1.strftime("%Y%m%d"),
        "d2": d2.strftime("%Y%m%d"),
    }
    async with httpx.AsyncClient(timeout=35.0, headers={"User-Agent": UA}) as client:
        r = await client.get("https://stooq.com/q/d/l/", params=params)
        r.raise_for_status()
    text = r.text.strip()
    if text.startswith("<!DOCTYPE html") or "requires JavaScript to verify your browser" in text:
        raise ValueError("Stooq 触发浏览器验证，CSV 接口当前不可用")
    if not text or len(text) < 20:
        raise ValueError("Stooq 无数据或代码在 Stooq 上不可用")
    f = io.StringIO(text)
    reader = csv.reader(f)
    rows = list(reader)
    if len(rows) < 2:
        raise ValueError("Stooq CSV 为空")
    header = [h.strip().lower() for h in rows[0]]
    try:
        i_date = header.index("date")
        i_open = header.index("open")
        i_high = header.index("high")
        i_low = header.index("low")
        i_close = header.index("close")
        i_vol = header.index("volume")
    except ValueError as e:
        raise ValueError("Stooq CSV 表头异常") from e

    candles: list[dict[str, Any]] = []
    for parts in rows[1:]:
        if len(parts) <= i_vol:
            continue
        try:
            ds = parts[i_date].strip()
            t = int(datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
        o, h, low, c = (
            _num(parts[i_open]),
            _num(parts[i_high]),
            _num(parts[i_low]),
            _num(parts[i_close]),
        )
        v_raw = parts[i_vol].strip()
        try:
            v = int(float(v_raw)) if v_raw else 0
        except ValueError:
            v = 0
        if c is None:
            continue
        o = o if o is not None else c
        h = h if h is not None else c
        low = low if low is not None else c
        amt = c * v if v else None
        candles.append(
            {
                "t": t,
                "o": round(o or 0, 6),
                "h": round(h or 0, 6),
                "l": round(low or 0, 6),
                "c": round(c or 0, 6),
                "v": v,
                "amount": round(amt, 2) if amt is not None else None,
                "turnover_rate": None,
            }
        )

    candles.sort(key=lambda x: x["t"])
    keep = _RANGE_ROWS.get(range_param, 280)
    candles = candles[-keep:] if keep < len(candles) else candles
    return candles, note


def _parse_display_name(qs: dict[str, Any] | None) -> str | None:
    if not qs:
        return None
    rlist = (qs.get("quoteSummary") or {}).get("result") or []
    if not rlist:
        return None
    price = rlist[0].get("price") or {}
    return price.get("shortName") or price.get("longName")


def _build_metrics(
    sym: str,
    candles: list[dict[str, Any]],
    meta: dict[str, Any],
    shares: int | None,
    name: str | None,
    data_source: str,
    fallback_note: str | None = None,
    interval_effective: str | None = None,
) -> dict[str, Any]:
    for row in candles:
        if shares and shares > 0 and row.get("v"):
            row["turnover_rate"] = round(row["v"] / shares * 100, 4)
        elif row.get("turnover_rate") is None:
            row["turnover_rate"] = None

    last = candles[-1] if candles else {}
    prev = candles[-2]["c"] if len(candles) >= 2 else _num(meta.get("chartPreviousClose"))
    lc = last.get("c")
    chg_pct = None
    if prev and lc is not None and prev != 0:
        chg_pct = round((lc - prev) / prev * 100, 2)

    return {
        "symbol": sym,
        "name": name or sym,
        "currency": meta.get("currency"),
        "exchange": meta.get("exchangeName"),
        "latest_close": lc,
        "previous_close": prev,
        "change_pct": chg_pct,
        "latest_open": last.get("o"),
        "latest_high": last.get("h"),
        "latest_low": last.get("l"),
        "latest_volume": last.get("v"),
        "latest_amount": last.get("amount"),
        "latest_turnover_rate": last.get("turnover_rate"),
        "shares_outstanding": shares,
        "data_source": data_source,
        "fallback_note": fallback_note,
        "interval_effective": interval_effective,
    }


async def fetch_kline_bundle(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    sym = symbol.strip().upper()

    chart_res, qs_raw = await asyncio.gather(
        _get_chart(sym, range_param, interval),
        _get_quote_summary(sym),
        return_exceptions=True,
    )

    qs: dict[str, Any] | None = qs_raw if isinstance(qs_raw, dict) else None

    use_stooq = isinstance(chart_res, httpx.HTTPStatusError) and chart_res.response.status_code in (
        401,
        403,
        429,
    )

    if use_stooq:
        try:
            candles, stooq_note = await _fetch_stooq_candles(sym, range_param, interval)
        except Exception as e:
            raise ValueError(f"Yahoo 受限且 Stooq 备用失败: {e!s}") from e
        eff = interval if interval in ("1d", "1wk", "1mo") else "1d"
        note = "Yahoo 限流/拒绝，已切换 Stooq。"
        if stooq_note:
            note = f"{note} {stooq_note}"
        metrics = _build_metrics(
            sym,
            candles,
            {},
            None,
            sym,
            "Stooq CSV (fallback when Yahoo blocks)",
            fallback_note=note,
            interval_effective=eff,
        )
        return {
            "symbol": sym,
            "range": range_param,
            "interval": interval,
            "interval_effective": eff,
            "candles": candles,
            "metrics": metrics,
        }

    if isinstance(chart_res, Exception):
        if isinstance(chart_res, httpx.HTTPStatusError):
            raise ValueError(f"行情接口 HTTP {chart_res.response.status_code}") from chart_res
        if isinstance(chart_res, httpx.HTTPError):
            raise ValueError(f"网络异常: {chart_res!s}") from chart_res
        raise ValueError(f"行情异常: {chart_res!s}") from chart_res

    chart_json = chart_res
    candles, meta = _parse_chart_payload(chart_json)
    shares = _parse_shares_outstanding(qs)
    name = _parse_display_name(qs)

    metrics = _build_metrics(
        sym,
        candles,
        meta,
        shares,
        name,
        "Yahoo Finance (unofficial chart & quoteSummary APIs)",
    )
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": interval,
        "candles": candles,
        "metrics": metrics,
    }
