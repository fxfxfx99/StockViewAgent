"""估值：PE/PB/EV 等（能取则取），并强调非投资建议。"""
from __future__ import annotations

from typing import Any


def analyze_valuation(bundle: dict[str, Any]) -> dict[str, Any]:
    yf = bundle.get("yahoo_fundamentals") or {}
    m = bundle.get("kline_metrics") or {}
    lines: list[str] = []

    pe = yf.get("trailing_pe") or m.get("pe_ttm")
    pb = yf.get("price_to_book")
    fwd = yf.get("forward_pe")
    ev_e = yf.get("enterprise_to_ebitda")
    peg = yf.get("peg")

    if pe is not None:
        lines.append(f"市盈率 TTM（数据源混合）: 约 {pe}。")
    else:
        lines.append("市盈率 TTM: 未获取。")
    if pb is not None:
        lines.append(f"市净率 PB: 约 {pb}。")
    if fwd is not None:
        lines.append(f"前瞻 PE（若披露）: 约 {fwd}。")
    if ev_e is not None:
        lines.append(f"EV/EBITDA（若披露）: 约 {ev_e}。")
    if peg is not None:
        lines.append(f"PEG（若披露）: 约 {peg}。")

    lines.append(
        "估值结论仅作相对与历史区间讨论的辅助，受会计准则、一次性损益、分部估值影响；"
        "不构成投资建议，须结合质量、成长可持续性与风险溢价。"
    )

    return {
        "pe_ttm": pe,
        "pb": pb,
        "forward_pe": fwd,
        "ev_ebitda": ev_e,
        "peg": peg,
        "narrative": "\n".join(lines),
    }
