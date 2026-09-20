"""Pytdx 通达信行情 K 线兜底（与官方行情 API 约定对齐：https://pytdx-docs.readthedocs.io/zh-cn/latest/pytdx_hq/）。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from app.services.market_extra_http import _tencent_minute_bar_count, _want_bar_cap
from app.services.market_time import market_timestamp

_A_SHARE_RE = re.compile(r"^(\d{6})\.(SS|SH|SZ|BJ)$", re.I)

# 优先尝试的节点（低延迟冷启动）；之后拼接 pytdx 自带的 hq_hosts
_CURATED_HQ: tuple[tuple[str, int], ...] = (
    ("110.41.147.114", 7709),
    ("124.70.75.113", 7709),
    ("47.113.94.228", 7709),
    ("106.14.190.242", 7709),
    ("119.147.212.81", 7709),
)

# 单次拉取会话内最多尝试的前置数量，避免 100+ 节点全量扫描拖死请求
_MAX_HQ_ATTEMPTS = 18


def _iter_hq_endpoints() -> list[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for t in _CURATED_HQ:
        if t not in seen:
            seen.add(t)
            out.append(t)
    try:
        from pytdx.config.hosts import hq_hosts  # noqa: PLC0415

        for _name, ip, port in hq_hosts:
            p = int(port)
            key = (ip, p)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
            if len(out) >= _MAX_HQ_ATTEMPTS:
                break
    except Exception:
        pass
    return out[:_MAX_HQ_ATTEMPTS]


def _symbol_to_market_code(sym: str) -> tuple[int, str]:
    from pytdx.params import TDXParams  # noqa: PLC0415

    m = _A_SHARE_RE.match((sym or "").strip().upper())
    if not m:
        raise ValueError("无效 A 股代码")
    code, suf = m.group(1), m.group(2).upper()
    if suf == "BJ":
        raise ValueError("pytdx 当前未配置北交所市场号，请依赖 Baostock/HTTP 源")
    if suf in ("SS", "SH"):
        return TDXParams.MARKET_SH, code
    if suf == "SZ":
        return TDXParams.MARKET_SZ, code
    raise ValueError("无效 A 股代码")


def _bar_to_dict(bar: Any) -> dict[str, Any] | None:
    if not isinstance(bar, dict) and not hasattr(bar, "get"):
        return None
    get = bar.get if hasattr(bar, "get") else lambda k, d=None: bar[k] if k in bar else d
    dt_s = get("datetime")
    if dt_s:
        try:
            dt = datetime.strptime(str(dt_s)[:16], "%Y-%m-%d %H:%M")
            ts = market_timestamp(dt)
        except ValueError:
            return None
    else:
        try:
            dt = datetime(int(get("year")), int(get("month")), int(get("day")), int(get("hour", 15)), int(get("minute", 0)))
            ts = market_timestamp(dt)
        except (ValueError, TypeError):
            return None
    try:
        o = float(get("open"))
        h = float(get("high"))
        low = float(get("low"))
        c = float(get("close"))
        vol = float(get("vol") or 0)
    except (TypeError, ValueError):
        return None
    v = int(round(vol * 100.0))
    amt = float(get("amount") or 0)
    return {
        "t": ts,
        "o": round(o, 4),
        "h": round(h, 4),
        "l": round(low, 4),
        "c": round(c, 4),
        "v": v,
        "amount": round(amt, 2),
        "turnover_rate": None,
    }


def _fetch_bars_paged(api, category: int, market: int, code: str, need: int) -> list[dict[str, Any]]:
    from pytdx.params import TDXParams  # noqa: PLC0415

    max_chunk = min(int(TDXParams.MAX_KLINE_COUNT), 800)
    parts: list[list[dict[str, Any]]] = []
    start = 0
    remaining = max(5, min(need, 5000))
    while remaining > 0:
        n = min(max_chunk, remaining)
        raw = api.get_security_bars(category, market, code, start, n)
        if not raw:
            break
        chunk: list[dict[str, Any]] = []
        for b in raw:
            row = _bar_to_dict(b)
            if row:
                chunk.append(row)
        if not chunk:
            break
        parts.append(chunk)
        start += len(raw)
        remaining -= len(raw)
        if len(raw) < n:
            break
    if not parts:
        return []
    merged: list[dict[str, Any]] = []
    for i in range(len(parts) - 1, -1, -1):
        merged.extend(parts[i])
    merged.sort(key=lambda x: x["t"])
    return merged


def _daily_categories(interval: str) -> list[int]:
    """官方文档：日 K 示例为 get_security_bars(9,...)；9=日K 与 4=日K 均存在，优先 9 再回退 4。"""
    from pytdx.params import TDXParams  # noqa: PLC0415

    iv = (interval or "1d").strip().lower()
    if iv == "1wk":
        return [TDXParams.KLINE_TYPE_WEEKLY]
    if iv == "1mo":
        return [TDXParams.KLINE_TYPE_MONTHLY]
    return [TDXParams.KLINE_TYPE_RI_K, TDXParams.KLINE_TYPE_DAILY]


def _minute_categories(interval: str) -> list[int]:
    """文档：7=1分钟，8=1分钟K线；其余 0/1/2/3 与 TDXParams 一致。1m 类优先 8 再试 7。"""
    from pytdx.params import TDXParams  # noqa: PLC0415

    iv = (interval or "1m").strip().lower()
    if iv in ("1m", "2m"):
        return [TDXParams.KLINE_TYPE_1MIN, TDXParams.KLINE_TYPE_EXHQ_1MIN]
    if iv == "5m":
        return [TDXParams.KLINE_TYPE_5MIN]
    if iv == "15m":
        return [TDXParams.KLINE_TYPE_15MIN]
    if iv == "30m":
        return [TDXParams.KLINE_TYPE_30MIN]
    if iv in ("60m", "90m", "1h"):
        return [TDXParams.KLINE_TYPE_1HOUR]
    raise ValueError(f"pytdx 不支持周期: {interval}")


def _run_with_first_connected_host(
    fetch: Callable[[Any], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str]:
    """依次连接 hq 节点；启用 auto_retry 以利用库内断线重连（见官方文档）。"""
    from pytdx.hq import TdxHq_API  # noqa: PLC0415

    last_err: Exception | None = None
    for host, port in _iter_hq_endpoints():
        api = TdxHq_API(auto_retry=True)
        try:
            if not api.connect(host, port):
                continue
            with api:
                candles = fetch(api)
            if candles:
                return candles, f"{host}:{port}"
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    msg = "pytdx 无法连接任一行情前置或全部未返回数据"
    if last_err:
        msg = f"{msg}: {last_err!s}"
    raise RuntimeError(msg)


def _bundle(sym: str, range_param: str, interval: str, eff: str, candles: list[dict[str, Any]], label: str) -> dict[str, Any]:
    from app.services.eastmoney_market import _build_metrics  # noqa: PLC0415

    if not candles:
        raise ValueError("pytdx 未返回 K 线数据")
    cap = _want_bar_cap(range_param, interval)
    iv = (interval or "").strip().lower()
    if iv in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"):
        cap = max(cap, _tencent_minute_bar_count(range_param))
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
    metrics = _build_metrics(sym, candles, q, eff)
    metrics["data_source"] = label
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": eff,
        "candles": candles,
        "metrics": metrics,
    }


def fetch_kline_bundle_pytdx_sync(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    sym = symbol.strip().upper()
    market, code = _symbol_to_market_code(sym)
    categories = _daily_categories(interval)
    need = _want_bar_cap(range_param, interval) + 20

    def _fetch(api) -> list[dict[str, Any]]:
        for cat in categories:
            bars = _fetch_bars_paged(api, cat, market, code, need)
            if bars:
                return bars
        return []

    candles, host = _run_with_first_connected_host(_fetch)
    eff = interval.strip().lower() if interval.strip().lower() in ("1d", "5d", "1wk", "1mo") else "1d"
    return _bundle(
        sym,
        range_param,
        interval,
        eff,
        candles,
        f"Pytdx 通达信行情（{host}，HTTP/SDK 源失败兜底；日线优先 category=9 再 4）",
    )


def fetch_kline_bundle_pytdx_minute_sync(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    sym = symbol.strip().upper()
    market, code = _symbol_to_market_code(sym)
    categories = _minute_categories(interval)
    need = _tencent_minute_bar_count(range_param) + 50

    def _fetch(api) -> list[dict[str, Any]]:
        for cat in categories:
            bars = _fetch_bars_paged(api, cat, market, code, need)
            if bars:
                return bars
        return []

    candles, host = _run_with_first_connected_host(_fetch)
    eff = interval.strip().lower()
    note = f"Pytdx 分钟线（{host}；1m 优先 category=8 再 7）"
    if eff == "2m":
        note += "；请求 2m 时使用 1 分钟线数据"
    if eff in ("90m", "1h"):
        note += "；请求 90m/1h 时使用 60 分钟线槽位"
    return _bundle(sym, range_param, interval, eff, candles, note)


def is_pytdx_installed() -> bool:
    try:
        import pytdx  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False
