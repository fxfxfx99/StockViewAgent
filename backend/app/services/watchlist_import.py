"""从粘贴文本或 CSV 行解析 A 股 Yahoo 格式代码（与股票列表存储格式一致）。"""
from __future__ import annotations

import csv
import io
import re
from typing import Any

from app.services import a_share_stocks

_FULL = re.compile(
    r"\b(\d{6})\.(SS|SZ|BJ)\b",
    re.I,
)
_CODE6 = re.compile(r"\b(\d{6})\b")


def parse_text_to_symbols(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    """
    返回 (去重后的 symbol 列表, 每条解析记录)。
    """
    raw = (text or "").strip()
    if not raw:
        return [], []

    found: list[tuple[str, str, str]] = []

    for m in _FULL.finditer(raw):
        code = m.group(1).zfill(6)
        suf = m.group(2).upper()
        found.append((m.group(0), f"{code}.{suf}", "explicit_suffix"))

    use_csv = "," in raw or "\t" in raw
    if use_csv:
        try:
            dialect = csv.Sniffer().sniff(raw[:512], delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(raw), dialect)
        for row in reader:
            for cell in row:
                s = (cell or "").strip()
                if not s:
                    continue
                for m in _FULL.finditer(s):
                    code = m.group(1).zfill(6)
                    suf = m.group(2).upper()
                    found.append((m.group(0), f"{code}.{suf}", "csv_cell"))
                for m2 in _CODE6.finditer(s):
                    code = m2.group(1).zfill(6)
                    y = a_share_stocks.symbol_from_code6(code)
                    if y:
                        found.append((m2.group(0), y, "csv_code6"))

    covered = {f[1].upper() for f in found}
    for m in _CODE6.finditer(raw):
        code = m.group(1).zfill(6)
        y = a_share_stocks.symbol_from_code6(code)
        if y and y.upper() not in covered:
            found.append((m.group(0), y, "code6"))
            covered.add(y.upper())

    ordered: list[str] = []
    seen: set[str] = set()
    details: list[dict[str, Any]] = []
    for fragment, sym, note in found:
        u = sym.upper()
        if u not in seen:
            seen.add(u)
            ordered.append(u)
            details.append({"raw": fragment, "symbol": u, "note": note})

    return ordered, details
