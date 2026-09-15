"""Excel 导入股票列表（借鉴 daily_stock_analysis import_parser 列别名与体积限制）。"""
from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd

from app.services import a_share_stocks
from app.services.stock_code_utils import to_yahoo_from_mixed

MAX_XLSX_BYTES = 2 * 1024 * 1024
_CODE_ALIASES = frozenset({"code", "股票代码", "代码", "stock_code", "symbol"})


def _norm_col(c: Any) -> str:
    return str(c).strip().lower().replace(" ", "")


def _code_col_index(columns: list[Any]) -> int | None:
    lowered = [_norm_col(c) for c in columns]
    for i, c in enumerate(lowered):
        if c in _CODE_ALIASES:
            return i
        for a in _CODE_ALIASES:
            if a in c or c in a:
                return i
    return None


def parse_excel_bytes(data: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    if len(data) > MAX_XLSX_BYTES:
        raise ValueError(f"Excel 超过 {MAX_XLSX_BYTES // 1024}KB")
    df = pd.read_excel(io.BytesIO(data), header=0)
    if df.empty:
        return [], []
    cols = list(df.columns)
    code_idx = _code_col_index(cols)
    found: list[tuple[str, str, str]] = []

    def add_raw(raw: str, note: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            return
        y = to_yahoo_from_mixed(raw)
        if y:
            found.append((raw, y, note))
            return
        # 纯数字 6 位
        m = re.search(r"\b(\d{6})\b", raw)
        if m:
            y2 = a_share_stocks.symbol_from_code6(m.group(1))
            if y2:
                found.append((raw, y2.upper(), note))

    if code_idx is not None:
        for _, row in df.iterrows():
            if code_idx < len(row):
                v = row.iloc[code_idx]
                if pd.notna(v):
                    add_raw(str(v).strip(), "excel_code_col")
    else:
        for _, row in df.iterrows():
            for v in row:
                if pd.notna(v) and str(v).strip():
                    add_raw(str(v).strip(), "excel_scan")

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
