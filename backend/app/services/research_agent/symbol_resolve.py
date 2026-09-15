"""从用户输入解析主标的（代码 / 简称 / 部分自然语言）。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.services import a_share_stocks
from app.services.llm_client import chat_completion_sync

from app.services.research_agent.normalizer import is_a_share, normalize_a_share_symbol

# 常见海外标的：自然语言 → Yahoo 符号（可扩展）
_KNOWN_GLOBAL: dict[str, str] = {
    "英伟达": "NVDA",
    "苹果": "AAPL",
    "微软": "MSFT",
    "谷歌": "GOOGL",
    "亚马逊": "AMZN",
    "特斯拉": "TSLA",
    "腾讯": "0700.HK",
    "阿里巴巴": "BABA",
    "美团": "3690.HK",
    "比亚迪": "1211.HK",
    "茅台": "600519.SS",
    "贵州茅台": "600519.SS",
    "五粮液": "000858.SZ",
}

_CODE_A = re.compile(r"\b(\d{6})\s*[\.\s]\s*(SS|SH|SZ|BJ)\b", re.I)
_CODE_A_COMPACT = re.compile(r"\b(\d{6})(SS|SH|SZ|BJ)\b", re.I)
_TICKER_US = re.compile(r"\b([A-Z]{1,5})\b")
_HK = re.compile(r"\b(\d{4})\s*\.\s*HK\b", re.I)
_COMPARE_SPLIT = re.compile(r"[与和、，,]|(?:\bvs\.?\b)|(?:\b对比\b)|(?:\b比较\b)", re.I)


@dataclass
class ResolvedSymbol:
    symbol: str
    display_name: str
    market: str  # ashare | global
    resolution_note: str


def _first_stock_hit(query: str) -> ResolvedSymbol | None:
    q = (query or "").strip()
    if len(q) < 1:
        return None
    hits = a_share_stocks.search_stocks(q[:32], limit=5)
    if not hits:
        return None
    top = hits[0]
    code = str(top.get("code") or "").strip()
    name = str(top.get("name") or code).strip()
    if not code:
        return None
    suf = "SS" if code.startswith(("5", "6", "9")) else "SZ"
    if code.startswith(("8", "4")):
        suf = "BJ"
    sym = normalize_a_share_symbol(f"{code}.{suf}")
    return ResolvedSymbol(symbol=sym, display_name=name, market="ashare", resolution_note="A 股简称/代码搜索")


def _parse_embedded_codes(text: str) -> list[str]:
    out: list[str] = []
    for m in _CODE_A.finditer(text):
        suf = m.group(2).upper()
        if suf == "SH":
            suf = "SS"
        out.append(normalize_a_share_symbol(f"{m.group(1)}.{suf}"))
    for m in _CODE_A_COMPACT.finditer(text):
        suf = m.group(2).upper()
        if suf == "SH":
            suf = "SS"
        out.append(normalize_a_share_symbol(f"{m.group(1)}.{suf}"))
    m = _HK.search(text)
    if m:
        out.append(f"{m.group(1).zfill(4)}.HK")
    return out


def heuristic_resolve(user_query: str) -> ResolvedSymbol | None:
    raw = (user_query or "").strip()
    if not raw:
        return None

    embedded = _parse_embedded_codes(raw)
    if embedded:
        sym = embedded[0]
        mk = "ashare" if is_a_share(sym) else "global"
        return ResolvedSymbol(symbol=sym, display_name=sym, market=mk, resolution_note="从问题中提取代码")

    for zh, sym in _KNOWN_GLOBAL.items():
        if zh in raw:
            mk = "ashare" if is_a_share(sym) else "global"
            dn = zh if zh else sym
            return ResolvedSymbol(symbol=sym, display_name=dn, market=mk, resolution_note="内置名称映射")

    # 纯 6 位 A 股代码
    m6 = re.search(r"\b(\d{6})\b", raw)
    if m6:
        code = m6.group(1)
        suf = "SS" if code.startswith(("5", "6", "9")) else "SZ"
        if code.startswith(("8", "4")):
            suf = "BJ"
        sym = normalize_a_share_symbol(f"{code}.{suf}")
        hit = _first_stock_hit(code)
        name = hit.display_name if hit else sym
        return ResolvedSymbol(symbol=sym, display_name=name, market="ashare", resolution_note="6 位代码推断交易所")

    # 美股 ticker（独立词，大写）
    for m in _TICKER_US.finditer(raw.upper()):
        t = m.group(1)
        if t in {"AND", "THE", "FOR", "IPO", "ETF", "CEO", "EPS", "PE", "AI"}:
            continue
        if len(t) >= 2:
            return ResolvedSymbol(symbol=t, display_name=t, market="global", resolution_note="英文 ticker 推断")

    # 中文：本地股票库搜索取首条
    if re.search(r"[\u4e00-\u9fff]", raw):
        hit = _first_stock_hit(raw[:20])
        if hit:
            return hit

    return None


def llm_resolve_symbol(user_query: str) -> tuple[ResolvedSymbol | None, str | None]:
    """无法启发式解析时，让模型只输出 JSON（可能幻觉代码，需后续行情校验）。"""
    system = (
        "你是证券代码解析助手。只输出一个 JSON 对象，不要其它文字。"
        '格式：{"symbol":"600519.SS或NVDA或0700.HK","display_name":"公司简称","market":"ashare或global"}。'
        "market：中国沪深北交所为 ashare，其余为 global。若完全无法判断，symbol 用空字符串。"
    )
    user = f"用户问题：\n{user_query[:2000]}"
    raw, err = chat_completion_sync(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.1,
        timeout=45.0,
    )
    if err or not raw:
        return None, err
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "LLM 解析 JSON 失败"
    sym = str(data.get("symbol") or "").strip().upper()
    if not sym:
        return None, "LLM 未给出代码"
    sym = normalize_a_share_symbol(sym) if "." in sym and sym.split(".")[-1] in ("SS", "SH", "SZ", "BJ") else sym
    mk = str(data.get("market") or "").strip().lower()
    if mk not in ("ashare", "global"):
        mk = "ashare" if is_a_share(sym) else "global"
    dn = str(data.get("display_name") or sym).strip()
    return ResolvedSymbol(symbol=sym, display_name=dn, market=mk, resolution_note="大模型辅助解析"), None


def resolve_primary(user_query: str) -> tuple[ResolvedSymbol | None, str | None]:
    h = heuristic_resolve(user_query)
    if h:
        return h, None
    return llm_resolve_symbol(user_query)


def extract_comparison_fragment(user_query: str) -> str | None:
    """若包含比较义，返回可能含第二标的的片段（供 planner / 二次解析）。"""
    raw = (user_query or "").strip()
    if not raw:
        return None
    if not _COMPARE_SPLIT.search(raw):
        return None
    parts = [p.strip() for p in _COMPARE_SPLIT.split(raw) if p.strip()]
    if len(parts) >= 2:
        return parts[1][:200]
    return None
