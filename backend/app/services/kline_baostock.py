"""Baostock 历史 K 线兜底（日/周/月 + 5/15/30/60 分钟）。

实现遵循官方 Python API 约定：login → query_history_k_data_plus → 先判 error_code 再 next()。
文档：http://www.baostock.com/mainContent?file=pythonAPI.md
需安装：pip install baostock
"""
from __future__ import annotations

import atexit
import re
import threading
import time
from datetime import datetime
from typing import Any

from app.services.market_extra_http import _want_bar_cap, _start_date_str

_A_SHARE_RE = re.compile(r"^(\d{6})\.(SS|SH|SZ|BJ)$", re.I)

_bs_lock = threading.Lock()
_bs_logged_in = False
_bs_atexit_registered = False


def _yahoo_to_bs_code(sym: str) -> str:
    m = _A_SHARE_RE.match((sym or "").strip().upper())
    if not m:
        raise ValueError("无效 A 股代码")
    code, suf = m.group(1), m.group(2).upper()
    if suf in ("SS", "SH"):
        return f"sh.{code}"
    if suf == "SZ":
        return f"sz.{code}"
    if suf == "BJ":
        return f"bj.{code}"
    raise ValueError("无效 A 股代码")


def _ensure_baostock():
    import baostock as bs  # noqa: PLC0415

    global _bs_logged_in, _bs_atexit_registered
    with _bs_lock:
        if _bs_logged_in:
            return bs
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(lg.error_msg or "baostock 登录失败")
        _bs_logged_in = True

        def _logout_safe():
            global _bs_logged_in
            with _bs_lock:
                if not _bs_logged_in:
                    return
                try:
                    bs.logout()
                except Exception:
                    pass
                _bs_logged_in = False

        if not _bs_atexit_registered:
            atexit.register(_logout_safe)
            _bs_atexit_registered = True
        return bs


def _rows_to_daily_candles(rows: list[list[str]]) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            ds = str(row[0]).strip()
            o, h, low, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            vol = float(row[5])
            amt = float(row[6]) if len(row) > 6 else 0.0
            dt = datetime.strptime(ds[:10], "%Y-%m-%d")
            ts = int(time.mktime(dt.timetuple()))
        except (ValueError, TypeError, IndexError):
            continue
        v = int(round(vol * 100.0))
        candles.append(
            {
                "t": ts,
                "o": round(o, 4),
                "h": round(h, 4),
                "l": round(low, 4),
                "c": round(c, 4),
                "v": v,
                "amount": round(amt, 2),
                "turnover_rate": None,
            }
        )
    candles.sort(key=lambda x: x["t"])
    return candles


def _parse_minute_datetime(date_s: str, time_s: str) -> int | None:
    t = (time_s or "").strip()
    if len(t) >= 14 and t.isdigit():
        y, mo, d = int(t[0:4]), int(t[4:6]), int(t[6:8])
        hh, mm = int(t[8:10]), int(t[10:12])
        try:
            dt = datetime(y, mo, d, hh, mm, 0)
            return int(time.mktime(dt.timetuple()))
        except ValueError:
            return None
    try:
        ds = (date_s or "").strip()[:10]
        dt = datetime.strptime(ds, "%Y-%m-%d")
        return int(time.mktime(dt.timetuple()))
    except ValueError:
        return None


def _rows_to_minute_candles(rows: list[list[str]]) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            ds = str(row[0]).strip()
            time_cell = str(row[1]).strip() if len(row) > 1 else ""
            ts = _parse_minute_datetime(ds, time_cell)
            if ts is None:
                continue
            o, h, low, c = float(row[2]), float(row[3]), float(row[4]), float(row[5])
            vol = float(row[6]) if len(row) > 6 else 0.0
            amt = float(row[7]) if len(row) > 7 else 0.0
        except (ValueError, TypeError, IndexError):
            continue
        v = int(round(vol * 100.0))
        candles.append(
            {
                "t": ts,
                "o": round(o, 4),
                "h": round(h, 4),
                "l": round(low, 4),
                "c": round(c, 4),
                "v": v,
                "amount": round(amt, 2),
                "turnover_rate": None,
            }
        )
    candles.sort(key=lambda x: x["t"])
    return candles


def _query_all(rs) -> list[list[str]]:
    """与官方示例一致：创建结果集后先检查 error_code，再循环 next()。"""
    if rs.error_code != "0":
        raise RuntimeError(rs.error_msg or "baostock 查询失败")
    rows: list[list[str]] = []
    while rs.next():
        rows.append(rs.get_row_data())
    return rows


def _bundle_from_candles(
    sym: str,
    range_param: str,
    interval: str,
    eff: str,
    candles: list[dict[str, Any]],
    source_label: str,
) -> dict[str, Any]:
    from app.services.eastmoney_market import _build_metrics  # noqa: PLC0415

    if not candles:
        raise ValueError("Baostock 未返回 K 线数据")
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
    metrics = _build_metrics(sym, candles, q, eff)
    metrics["data_source"] = source_label
    return {
        "symbol": sym,
        "range": range_param,
        "interval": interval,
        "interval_effective": eff,
        "candles": candles,
        "metrics": metrics,
    }


def _bs_frequency_daily(interval: str) -> str:
    iv = (interval or "1d").strip().lower()
    if iv == "1wk":
        return "w"
    if iv == "1mo":
        return "m"
    return "d"


def _bs_frequency_minute(interval: str) -> str | None:
    iv = (interval or "").strip().lower()
    m = {"5m": "5", "15m": "15", "30m": "30", "60m": "60", "90m": "60", "1h": "60"}
    return m.get(iv)


def fetch_kline_bundle_baostock_sync(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    """日/周/月；前复权 adjustflag=3。"""
    sym = symbol.strip().upper()
    code = _yahoo_to_bs_code(sym)
    bs = _ensure_baostock()
    start = _start_date_str(range_param)
    end = datetime.now().strftime("%Y-%m-%d")
    fq = _bs_frequency_daily(interval)
    fields = "date,open,high,low,close,volume,amount,adjustflag"
    rs = bs.query_history_k_data_plus(
        code,
        fields,
        start_date=start,
        end_date=end,
        frequency=fq,
        adjustflag="3",
    )
    rows = _query_all(rs)
    candles = _rows_to_daily_candles(rows)
    eff = interval.strip().lower() if interval.strip().lower() in ("1d", "5d", "1wk", "1mo") else "1d"
    return _bundle_from_candles(
        sym,
        range_param,
        interval,
        eff,
        candles,
        "Baostock query_history_k_data_plus（前复权，HTTP 主源失败兜底）",
    )


def fetch_kline_bundle_baostock_minute_sync(symbol: str, range_param: str, interval: str) -> dict[str, Any]:
    fq = _bs_frequency_minute(interval)
    if not fq:
        raise ValueError("Baostock 不支持该分钟周期（仅 5/15/30/60）")
    sym = symbol.strip().upper()
    code = _yahoo_to_bs_code(sym)
    bs = _ensure_baostock()
    start = _start_date_str(range_param)
    end = datetime.now().strftime("%Y-%m-%d")
    fields = "date,time,open,high,low,close,volume,amount"
    rs = bs.query_history_k_data_plus(code, fields, start_date=start, end_date=end, frequency=fq)
    rows = _query_all(rs)
    candles = _rows_to_minute_candles(rows)
    eff = interval.strip().lower()
    return _bundle_from_candles(
        sym,
        range_param,
        interval,
        eff,
        candles,
        f"Baostock 分钟线 frequency={fq}（东财/腾讯分钟失败兜底）",
    )


def is_baostock_installed() -> bool:
    try:
        import baostock  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False
