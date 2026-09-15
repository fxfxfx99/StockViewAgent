"""标的与行情符号标准化。"""
from __future__ import annotations

import re

_A_SHARE = re.compile(r"^(\d{6})\.(SS|SH|SZ|BJ)$", re.I)


def normalize_a_share_symbol(symbol: str) -> str:
    """600519.SH → 600519.SS（与本项目 K 线路由一致）。"""
    s = (symbol or "").strip().upper()
    m = _A_SHARE.match(s)
    if not m:
        return s
    code, suf = m.group(1), m.group(2).upper()
    if suf == "SH":
        suf = "SS"
    return f"{code}.{suf}"


def is_a_share(symbol: str) -> bool:
    return bool(_A_SHARE.match((symbol or "").strip().upper()))


def market_kind(symbol: str) -> str:
    """ashare | global"""
    return "ashare" if is_a_share(symbol) else "global"
