"""
A 股行情：东方财富 push2 / push2his 公开接口（与网页行情同源思路）。

- 日/周/月 K 与分钟 K：push2his …/kline/get
- 快照（市值、PE、换手、股本等）：push2 …/stock/get

说明：非官方 SLA，字段可能调整；仅供学习研究。
"""
from __future__ import annotations

import asyncio
import math
import random
import re
import time
from datetime import datetime
from typing import Any

import httpx

from app.services.market_time import market_timestamp

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REFERER = "https://quote.eastmoney.com/"

# 东财偶发断连 / 半关闭连接，短退避重试（含 ReadError：对端半关连接时 httpx 可能抛此类而非 RemoteProtocolError）
_RETRYABLE_EXC = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.LocalProtocolError,
    httpx.WriteError,
    httpx.PoolTimeout,
)

_EM_TIMEOUT = httpx.Timeout(45.0, connect=18.0)
_EM_HEADERS = {"User-Agent": UA, "Referer": REFERER}
UT = "fa5fd1943c7b386f172d6893db7a5817"

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
SLIST_URL = "https://push2.eastmoney.com/api/qt/slist/get"

_A_SHARE_RE = re.compile(r"^(\d{6})\.(SS|SH|SZ|BJ)$", re.I)

# 行情快照扩展字段：涨停/跌停、量比、内外盘、总量额等（与网页行情同源思路）
FULL_QUOTE_FIELDS = (
    "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f57,f58,f59,f60,"
    "f84,f85,f116,f117,f162,f167,f168,f169,f170,f161,f152,f154"
)

_SECID_LABEL_CACHE: dict[str, tuple[float, str]] = {}
_SECID_LABEL_TTL_SEC = 3600.0

_RANGE_LMT: dict[str, int] = {
    "1d": 32,
    "5d": 32,
    "1mo": 35,
    "3mo": 72,
    "6mo": 140,
    "1y": 280,
    "2y": 560,
    "5y": 1300,
    "10y": 2800,
    "ytd": 300,
    "max": 5000,
}

_INTERVAL_KLT: dict[str, int] = {
    "1m": 1,
    "2m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "90m": 60,
    "1h": 60,
    "1d": 101,
    "5d": 101,
    "1wk": 102,
    "1mo": 103,
}


def yahoo_symbol_to_secid(symbol: str) -> str | None:
    m = _A_SHARE_RE.match((symbol or "").strip().upper())
    if not m:
        return None
    code, suf = m.group(1), m.group(2).upper()
    if suf in ("SS", "SH"):
        return f"1.{code}"
    if suf == "SZ":
        return f"0.{code}"
    if suf == "BJ":
        return f"0.{code}"
    return None


# 东财 push2his 常用大盘指数 secid（与网页行情一致思路）
MAJOR_INDEX_META: dict[str, tuple[str, str]] = {
    "SH_COMP": ("1.000001", "上证指数"),
    "SZ_COMP": ("0.399001", "深证成指"),
    "CYB": ("0.399006", "创业板指"),
    "HS300": ("1.000300", "沪深300"),
    "SSE50": ("1.000016", "上证50"),
}


def major_index_meta(index_key: str) -> tuple[str, str] | None:
    k = (index_key or "").strip().upper()
    return MAJOR_INDEX_META.get(k)


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    *,
    attempts: int = 5,
) -> httpx.Response:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r
        except _RETRYABLE_EXC as e:
            last = e
            if i < attempts - 1:
                await asyncio.sleep(0.55 + random.random() * 0.75)
        except httpx.HTTPStatusError as e:
            last = e
            if e.response.status_code >= 500 and i < attempts - 1:
                await asyncio.sleep(0.45 + random.random() * 0.55)
                continue
            raise
    assert last is not None
    raise last


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


def _parse_kline_lines(klines: list[str], shares: int | None) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            ds = parts[0].strip()
            o = float(parts[1])
            c = float(parts[2])
            h = float(parts[3])
            low = float(parts[4])
            vol_lots = float(parts[5])
            amt = float(parts[6]) if len(parts) > 6 else 0.0
            turnover_pct = float(parts[10]) if len(parts) > 10 else None
        except (ValueError, IndexError):
            continue
        try:
            if " " in ds:
                dt = datetime.strptime(ds[:16], "%Y-%m-%d %H:%M")
            else:
                dt = datetime.strptime(ds[:10], "%Y-%m-%d")
            ts = market_timestamp(dt)
        except ValueError:
            continue
        v_shares = int(round(vol_lots * 100.0))
        row: dict[str, Any] = {
            "t": ts,
            "o": round(o, 4),
            "h": round(h, 4),
            "l": round(low, 4),
            "c": round(c, 4),
            "v": v_shares,
            "amount": round(amt, 2),
            "turnover_rate": round(turnover_pct, 4) if turnover_pct is not None else None,
        }
        if shares and shares > 0 and v_shares > 0 and row["turnover_rate"] is None:
            row["turnover_rate"] = round(v_shares / shares * 100.0, 4)
        candles.append(row)
    candles.sort(key=lambda x: x["t"])
    return candles


def _price100(v: Any) -> float | None:
    """东财行情价多为「整数×100」；若已是小数元价则不再除。"""
    x = _num(v)
    if x is None:
        return None
    if abs(x - round(x)) < 1e-3 and abs(x) >= 1000:
        return round(x / 100.0, 4)
    return round(float(x), 4)


def _align_snapshot_ohlc_to_candle(
    q: dict[str, Any],
    last: dict[str, Any],
    prev: float | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """
    push2 快照经 _price100 后，偶发与 push2his K 线末根尺度不一致（例如 f43 返回 1420
    被当成「×100」而得到 14.2，而昨收 f60、K 线仍为 ~1400 元）。此时以 K 线末根 OHLC 为准。
    """
    ql = q.get("latest")
    qo = q.get("open")
    qh = q.get("high")
    qlow = q.get("low")
    cl = last.get("c")
    co = last.get("o")
    ch = last.get("h")
    clow = last.get("l")
    if cl is None:
        return ql, qo, qh, qlow
    try:
        clf = float(cl)
    except (TypeError, ValueError):
        return ql, qo, qh, qlow
    if ql is None:
        return ql, qo, qh, qlow
    try:
        qlf = float(ql)
    except (TypeError, ValueError):
        return ql, qo, qh, qlow
    if abs(qlf - clf) / max(abs(clf), 1e-9) <= 0.02:
        return ql, qo, qh, qlow
    pv = None
    if prev is not None:
        try:
            pv = float(prev)
        except (TypeError, ValueError):
            pv = None
    use_candle = False
    if pv is not None and pv > 1.0:
        if abs(clf - pv) / pv <= 0.12 and abs(qlf - pv) / pv > 0.25:
            use_candle = True
    if not use_candle and clf > 50 and qlf < clf * 0.12:
        use_candle = True
    if not use_candle and clf > 50 and qlf > clf * 8:
        use_candle = True
    if not use_candle:
        return ql, qo, qh, qlow
    return (
        cl,
        co if co is not None else qo,
        ch if ch is not None else qh,
        clow if clow is not None else qlow,
    )


def _parse_quote_data(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    d = data
    pe = _num(d.get("f162"))
    if pe is not None and pe > 200 and abs(pe - round(pe)) < 1e-3:
        pe = round(pe / 100.0, 2)
    tr = _num(d.get("f168"))
    if tr is not None and tr > 5 and abs(tr - round(tr)) < 1e-3:
        tr = round(tr / 100.0, 4)
    mcap = _num(d.get("f116"))
    fmcap = _num(d.get("f117"))
    shares = _num(d.get("f84"))
    sh_int = int(shares) if shares is not None and shares > 0 else None
    name = (d.get("f58") or d.get("f57") or "") if isinstance(d.get("f58") or d.get("f57"), str) else ""
    if not name and isinstance(d.get("f58"), (int, float)):
        name = str(d.get("f58"))
    return {
        "name": str(name).strip() if name else "",
        "latest": _price100(d.get("f43")),
        "open": _price100(d.get("f46")),
        "high": _price100(d.get("f44")),
        "low": _price100(d.get("f45")),
        "prev_close": _price100(d.get("f60")),
        "total_market_cap_yuan": mcap,
        "float_market_cap_yuan": fmcap,
        "pe_ttm": pe,
        "turnover_rate_snapshot": tr,
        "shares_total": sh_int,
    }


def _build_metrics(
    sym: str,
    candles: list[dict[str, Any]],
    q: dict[str, Any],
    interval_effective: str,
    *,
    extended: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for row in candles:
        st = q.get("shares_total")
        if st and st > 0 and row.get("v"):
            if row.get("turnover_rate") is None:
                row["turnover_rate"] = round(row["v"] / st * 100.0, 4)

    last = candles[-1] if candles else {}
    prev = q.get("prev_close")
    if prev is None and len(candles) >= 2:
        prev = candles[-2].get("c")
    lc, lo, hi, lw = _align_snapshot_ohlc_to_candle(q, last, prev)
    if lc is None:
        lc = last.get("c")
    chg_pct = None
    if prev and lc is not None and prev != 0:
        chg_pct = round((lc - prev) / prev * 100.0, 2)

    tr_last = last.get("turnover_rate")
    if tr_last is None:
        tr_last = q.get("turnover_rate_snapshot")
    ext = extended or {}
    if tr_last is None and ext.get("turnover_rate_pct") is not None:
        tr_last = ext.get("turnover_rate_pct")

    out: dict[str, Any] = {
        "symbol": sym,
        "name": q.get("name") or sym,
        "currency": "CNY",
        "exchange": "沪深京A股",
        "latest_close": lc,
        "previous_close": prev,
        "change_pct": chg_pct,
        "latest_open": lo if lo is not None else last.get("o"),
        "latest_high": hi if hi is not None else last.get("h"),
        "latest_low": lw if lw is not None else last.get("l"),
        "latest_volume": last.get("v"),
        "latest_amount": last.get("amount"),
        "latest_turnover_rate": tr_last,
        "shares_outstanding": q.get("shares_total"),
        "total_market_cap_yuan": q.get("total_market_cap_yuan"),
        "float_market_cap_yuan": q.get("float_market_cap_yuan"),
        "pe_ttm": q.get("pe_ttm"),
        "data_source": "东方财富 push2 / push2his（A 股）",
        "fallback_note": None,
        "interval_effective": interval_effective,
    }
    # push2 扩展快照（与 watchlist stock-detail / 网页行情同源字段），K 线末根缺失时可作换手等备份
    if ext.get("limit_up") is not None:
        out["limit_up"] = ext["limit_up"]
    if ext.get("limit_down") is not None:
        out["limit_down"] = ext["limit_down"]
    if ext.get("volume_ratio") is not None:
        out["volume_ratio"] = ext["volume_ratio"]
    if ext.get("inner_lots") is not None:
        out["inner_lots"] = int(ext["inner_lots"])
    if ext.get("outer_lots") is not None:
        out["outer_lots"] = int(ext["outer_lots"])
    if ext.get("volume_lots") is not None:
        out["snapshot_volume_lots"] = int(ext["volume_lots"])
    if ext.get("amount_yuan") is not None:
        out["snapshot_amount_yuan"] = round(float(ext["amount_yuan"]), 2)
    return out


async def _fetch_kline(secid: str, klt: int, lmt: int, fqt: int = 1) -> list[str]:
    params = {
        "secid": secid,
        "ut": UT,
        "fields1": "f1,f2",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": str(klt),
        "fqt": str(fqt),
        "beg": "0",
        "end": "20500101",
        "lmt": str(max(5, min(lmt, 12000))),
    }
    async with httpx.AsyncClient(timeout=_EM_TIMEOUT, headers=_EM_HEADERS, follow_redirects=True) as client:
        r = await _get_with_retry(client, KLINE_URL, params)
        data = r.json()
    if (data or {}).get("rc") != 0:
        return []
    kl = ((data or {}).get("data") or {}).get("klines") or []
    return list(kl) if isinstance(kl, list) else []


async def resolve_secid_display_labels(
    secids: list[str],
    *,
    max_concurrent: int = 12,
) -> dict[str, str]:
    """
    将东财 secid（如 90.BK0579、0.600519、116.09992）解析为行情接口 f58 展示名。
    带内存缓存，减轻快讯轮询压力。
    """
    now = time.time()
    uniq: list[str] = []
    seen: set[str] = set()
    for x in secids:
        s = (x or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        uniq.append(s)

    out: dict[str, str] = {}
    need_fetch: list[str] = []
    for s in uniq:
        ent = _SECID_LABEL_CACHE.get(s)
        if ent and now - ent[0] < _SECID_LABEL_TTL_SEC:
            out[s] = ent[1]
        else:
            need_fetch.append(s)

    if not need_fetch:
        return out

    sem = asyncio.Semaphore(max_concurrent)

    async def one(sid: str) -> tuple[str, str | None]:
        async with sem:
            try:
                raw = await _fetch_quote(sid, "f57,f58")
                name = (raw.get("f58") or "").strip()
                return sid, name or None
            except Exception:
                return sid, None

    pairs = await asyncio.gather(*[one(s) for s in need_fetch])
    t0 = time.time()
    for sid, name in pairs:
        if name:
            _SECID_LABEL_CACHE[sid] = (t0, name)
            out[sid] = name
    return out


async def _fetch_quote(secid: str, fields: str | None = None) -> dict[str, Any]:
    fld = fields or (
        "f43,f44,f45,f46,f47,f48,f49,f57,f58,f59,f60,"
        "f84,f85,f116,f117,f162,f167,f168,f169,f170"
    )
    params = {"secid": secid, "fields": fld, "invt": "2", "fltt": "2"}
    async with httpx.AsyncClient(timeout=_EM_TIMEOUT, headers=_EM_HEADERS, follow_redirects=True) as client:
        r = await _get_with_retry(client, QUOTE_URL, params)
        data = r.json()
    if (data or {}).get("rc") != 0:
        return {}
    return (data or {}).get("data") or {}


def parse_extended_quote(raw: dict[str, Any] | None) -> dict[str, Any]:
    """解析扩展快照：昨收、开高低、涨停跌停、换手、量比、内外盘、总量额（价用 _price100）。"""
    if not raw:
        return {}
    d = raw
    tr = _num(d.get("f168"))
    if tr is not None and tr > 5 and abs(tr - round(tr)) < 1e-3:
        tr = round(tr / 100.0, 4)
    elif tr is not None:
        tr = round(float(tr), 4)
    pd = _parse_quote_data(d)
    return {
        "name": str(d.get("f58") or "").strip() or None,
        "code_em": str(d.get("f57") or "").strip() or None,
        "latest": _price100(d.get("f43")),
        "open": _price100(d.get("f46")),
        "high": _price100(d.get("f44")),
        "low": _price100(d.get("f45")),
        "prev_close": _price100(d.get("f60")),
        "limit_up": _price100(d.get("f51")),
        "limit_down": _price100(d.get("f52")),
        "volume_lots": int(_num(d.get("f47")) or 0) if d.get("f47") is not None else None,
        "amount_yuan": round(float(_num(d.get("f48")) or 0), 2) if d.get("f48") is not None else None,
        "outer_lots": int(_num(d.get("f49")) or 0) if d.get("f49") is not None else None,
        "inner_lots": int(_num(d.get("f161")) or 0) if d.get("f161") is not None else None,
        "volume_ratio": round(float(_num(d.get("f50")) or 0), 2) if d.get("f50") is not None else None,
        "turnover_rate_pct": tr,
        "pe_ttm": pd.get("pe_ttm"),
        "total_market_cap_yuan": pd.get("total_market_cap_yuan"),
        "float_market_cap_yuan": pd.get("float_market_cap_yuan"),
        "shares_total": pd.get("shares_total"),
    }


def _blocks_diff_to_list(diff: Any) -> list[dict[str, Any]]:
    """slist/get diff → [{ code, name, change_pct, constituents, type_code }]."""
    if not diff:
        return []
    rows: list[dict[str, Any]] = []
    if isinstance(diff, dict):
        keys = sorted(diff.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
        for k in keys:
            b = diff[k]
            if not isinstance(b, dict):
                continue
            chg = _num(b.get("f3"))
            cons = b.get("f104")
            tc = b.get("f105")
            cons_i = None
            if cons is not None and str(cons).strip():
                try:
                    cons_i = int(float(cons))
                except (TypeError, ValueError):
                    cons_i = None
            tc_i = None
            if tc is not None and str(tc).strip():
                try:
                    tc_i = int(float(tc))
                except (TypeError, ValueError):
                    tc_i = None
            rows.append(
                {
                    "code": str(b.get("f12") or "").strip(),
                    "name": str(b.get("f14") or "").strip(),
                    "change_pct": round(float(chg), 2) if chg is not None else None,
                    "constituents": cons_i,
                    "type_code": tc_i,
                }
            )
    return rows


# 东财板块 f105 类型（不完全枚举，未知则回退为「板块」）
_BK_TYPE_LABEL: dict[int, str] = {
    0: "其他",
    1: "地域",
    2: "行业",
    3: "概念",
    4: "风格",
    5: "指数",
    6: "主题",
    7: "概念",
    8: "行业",
    9: "行业",
    61: "指数",
}


def label_block_type(type_code: int | None) -> str:
    if type_code is None:
        return "板块"
    return _BK_TYPE_LABEL.get(type_code, "板块")


async def fetch_stock_blocks(secid: str, page_size: int = 120) -> list[dict[str, Any]]:
    """个股所属板块/概念列表（slist spt=3）。"""
    params = {
        "secid": secid,
        "ut": UT,
        "invt": "2",
        "fltt": "2",
        "spt": "3",
        "fields": "f12,f14,f3,f13,f104,f105",
        "pn": "1",
        "pz": str(max(10, min(page_size, 200))),
    }
    async with httpx.AsyncClient(timeout=_EM_TIMEOUT, headers=_EM_HEADERS, follow_redirects=True) as client:
        r = await _get_with_retry(client, SLIST_URL, params)
        data = r.json()
    if (data or {}).get("rc") != 0:
        return []
    diff = ((data or {}).get("data") or {}).get("diff")
    raw_list = _blocks_diff_to_list(diff)
    for row in raw_list:
        row["type_label"] = label_block_type(row.get("type_code"))
    return raw_list


async def fetch_extended_quote_snapshot(symbol: str) -> dict[str, Any]:
    """拉取扩展快照并解析为结构化行情。"""
    sym = symbol.strip().upper()
    secid = yahoo_symbol_to_secid(sym)
    if not secid:
        raise ValueError("仅支持 A 股：6 位代码 + .SS/.SH/.SZ/.BJ（如 600519.SS）")
    raw = await _fetch_quote(secid, FULL_QUOTE_FIELDS)
    return parse_extended_quote(raw if isinstance(raw, dict) else None)


async def _fetch_kline_bundle_for_secid(
    secid: str,
    sym: str,
    range_param: str,
    interval: str,
    *,
    preloaded_shares_total: int | None = None,
) -> dict[str, Any]:
    klt = _INTERVAL_KLT.get(interval, 101)
    lmt = _RANGE_LMT.get(range_param, 280)
    if klt < 100:
        lmt = min(max(lmt * 4, 120), 1200)

    # 串行略间隔，降低东财同时多连接被掐断的概率
    klines_raw = await _fetch_kline(secid, klt, lmt)
    await asyncio.sleep(0.22)
    ext_quote: dict[str, Any] = {}
    if preloaded_shares_total is not None and preloaded_shares_total > 0:
        q: dict[str, Any] = {"shares_total": preloaded_shares_total}
    else:
        q_raw = await _fetch_quote(secid, FULL_QUOTE_FIELDS)
        q = _parse_quote_data(q_raw if isinstance(q_raw, dict) else None)
        ext_quote = parse_extended_quote(q_raw if isinstance(q_raw, dict) else None)
    shares = q.get("shares_total")
    candles = _parse_kline_lines(klines_raw, shares)
    if not candles:
        raise ValueError("东方财富未返回 K 线数据（可能代码无效或接口限流）")

    want = _RANGE_LMT.get(range_param, 280)
    if klt >= 101:
        want = min(want, len(candles))
        candles = candles[-want:] if len(candles) > want else candles
    else:
        cap = min(max(lmt, 120), len(candles))
        candles = candles[-cap:] if len(candles) > cap else candles

    eff = interval if interval in _INTERVAL_KLT else "1d"
    metrics = _build_metrics(sym, candles, q, eff, extended=ext_quote or None)

    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": eff,
        "candles": candles,
        "metrics": metrics,
    }


async def fetch_kline_bundle(
    symbol: str,
    range_param: str,
    interval: str,
    *,
    preloaded_shares_total: int | None = None,
) -> dict[str, Any]:
    sym = symbol.strip().upper()
    secid = yahoo_symbol_to_secid(sym)
    if not secid:
        raise ValueError("仅支持 A 股：6 位代码 + .SS/.SH/.SZ/.BJ（如 600519.SS）")
    return await _fetch_kline_bundle_for_secid(
        secid, sym, range_param, interval, preloaded_shares_total=preloaded_shares_total
    )


async def fetch_major_index_kline_bundle(index_key: str, range_param: str, interval: str) -> dict[str, Any]:
    meta = major_index_meta(index_key)
    if not meta:
        allowed = ", ".join(sorted(MAJOR_INDEX_META))
        raise ValueError(f"未知指数代码，支持：{allowed}")
    secid, label = meta
    out = await _fetch_kline_bundle_for_secid(secid, label, range_param, interval, preloaded_shares_total=None)
    out["index_key"] = (index_key or "").strip().upper()
    return out
