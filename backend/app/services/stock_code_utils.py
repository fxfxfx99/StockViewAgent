"""
股票代码规范化（借鉴 daily_stock_analysis data_provider.normalize_stock_code 思路，适配本项目 Yahoo 后缀）。
"""
from __future__ import annotations

import re

_RE_YAHOO = re.compile(r"^(\d{6})\.(SS|SH|SZ|BJ)$", re.I)
_RE_XQ = re.compile(r"^(SH|SZ|BJ)(\d{6})$", re.I)


def normalize_plain_code(code: str) -> str | None:
    """提取 6 位数字代码；非法返回 None。"""
    s = (code or "").strip()
    s = re.sub(r"\D", "", s)
    if len(s) > 6:
        s = s[-6:]
    if len(s) == 6 and s.isdigit():
        return s
    return None


def yahoo_symbol_from_parts(code6: str, market: str) -> str | None:
    m = (market or "").strip().upper()
    c = normalize_plain_code(code6)
    if not c:
        return None
    if m in ("SH", "SS", "SSE"):
        return f"{c}.SS"
    if m in ("SZ", "SZSE"):
        return f"{c}.SZ"
    if m in ("BJ", "BSE"):
        return f"{c}.BJ"
    return None


def to_yahoo_from_mixed(raw: str) -> str | None:
    """
    接受 600519.SS / SH600519 / 600519 等形式，尽量得到 Yahoo 格式。
    """
    t = (raw or "").strip().upper()
    if not t:
        return None
    m = _RE_YAHOO.match(t)
    if m:
        return f"{m.group(1).zfill(6)}.{m.group(2).upper() if m.group(2).upper() != 'SH' else 'SS'}"
    m2 = _RE_XQ.match(t.replace(".", ""))
    if m2:
        ex, c = m2.group(1).upper(), m2.group(2).zfill(6)
        suf = "SS" if ex == "SH" else ex
        return f"{c}.{suf}"
    c = normalize_plain_code(t)
    if c:
        if c.startswith("6") or c.startswith("9"):
            return f"{c}.SS"
        if c.startswith(("0", "3")):
            return f"{c}.SZ"
        if c.startswith(("4", "8")):
            return f"{c}.BJ"
    return None


def normalize_symbol(raw: str) -> str | None:
    """A 股、港股 Yahoo 后缀及美股 ticker；不接受 URL 或指数代码。"""
    text = str(raw or "").strip().upper()
    cn = to_yahoo_from_mixed(text)
    if cn:
        return cn
    hk = re.fullmatch(r"(\d{1,5})\.HK", text)
    if hk and int(hk.group(1)) > 0:
        return f"{int(hk.group(1)):04d}.HK"
    if re.fullmatch(r"[A-Z]{1,6}(?:[.-][A-Z])?", text):
        return text.replace(".", "-")
    return None
