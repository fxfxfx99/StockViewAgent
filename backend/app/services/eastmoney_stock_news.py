"""东财 F10：个股公告与公司资讯（供新闻入库与 Agent 解读）。"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime
from typing import Any

import httpx

from app.services.company_profile_em import yahoo_symbol_to_em_code
from app.services.news_fetch_http import DEFAULT_NEWS_TIMEOUT, NEWS_UA, get_with_retry

_PAGE_AJAX = "https://emweb.securities.eastmoney.com/PC_HSF10/NewsBulletin/PageAjax"
_REFERER = "https://emweb.securities.eastmoney.com/"


def _item_id(art_code: str, title: str) -> str:
    h = hashlib.sha256(f"{art_code}|{title}".encode("utf-8", errors="ignore")).hexdigest()[:14]
    return f"em-ann-{h}"


def _parse_display_time(raw: str | None) -> tuple[str, int | None]:
    if not raw:
        return "", None
    s = str(raw).strip()
    s = re.sub(r":\d{1,3}$", "", s) if s.count(":") >= 2 else s
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len(fmt)], fmt)
            return s, int(dt.timestamp())
        except ValueError:
            continue
    return s, None


def _notice_link(code6: str, art_code: str) -> str:
    return f"https://data.eastmoney.com/notices/detail/{code6}/{art_code}.html"


def _normalize_gsgg_row(row: dict[str, Any], symbol: str, code6: str) -> dict[str, Any] | None:
    title = str(row.get("title") or "").strip()
    art = str(row.get("art_code") or "").strip()
    if not title or not art:
        return None
    published, ts = _parse_display_time(row.get("display_time") or row.get("notice_date"))
    return {
        "id": _item_id(art, title),
        "title": title,
        "link": _notice_link(code6, art),
        "summary": title[:800],
        "published": published,
        "published_ts": ts,
        "source": f"东方财富 · 公司公告 · {symbol}",
        "symbol_hint": symbol,
        "related_codes": [],
        "related_entities": [],
    }


def _normalize_gszx_row(row: dict[str, Any], symbol: str, stock_name: str | None) -> dict[str, Any] | None:
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    code6 = symbol.split(".", 1)[0] if "." in symbol else symbol[:6]
    if stock_name and stock_name not in title and code6 not in title:
        return None
    link = str(row.get("uniqueUrl") or row.get("url") or "").strip() or "#"
    summary = str(row.get("summary") or "").strip()[:800]
    ts = row.get("showDateTime") or row.get("publishDate") or 0
    published_ts = None
    try:
        if ts:
            published_ts = int(ts) // 1000 if int(ts) > 1e12 else int(ts)
    except (TypeError, ValueError):
        published_ts = None
    published = str(published_ts or "")
    return {
        "id": _item_id(link, title),
        "title": title,
        "link": link,
        "summary": summary,
        "published": published,
        "published_ts": published_ts,
        "source": f"东方财富 · 资讯 · {symbol}",
        "symbol_hint": symbol,
        "related_codes": [],
        "related_entities": [],
    }


async def fetch_symbol_news_items(
    symbol: str,
    *,
    limit: int = 20,
    stock_name: str | None = None,
) -> list[dict[str, Any]]:
    """拉取单标的公告（gsgg）及标题含简称的资讯（gszx 过滤）。"""
    sym = (symbol or "").strip().upper()
    em_code = yahoo_symbol_to_em_code(sym)
    if not em_code:
        return []
    code6 = em_code[2:] if len(em_code) >= 8 else sym.split(".", 1)[0]
    limit = max(1, min(limit, 40))

    async with httpx.AsyncClient(timeout=DEFAULT_NEWS_TIMEOUT, headers={"User-Agent": NEWS_UA}) as client:
        r = await get_with_retry(
            client,
            _PAGE_AJAX,
            params={"code": em_code},
            headers={"Referer": _REFERER},
        )
        if r is None:
            return []
        try:
            data = r.json()
        except ValueError:
            return []

    out: list[dict[str, Any]] = []
    gsgg = data.get("gsgg")
    if isinstance(gsgg, list):
        for row in gsgg[:limit]:
            if not isinstance(row, dict):
                continue
            it = _normalize_gsgg_row(row, sym, code6)
            if it:
                out.append(it)

    gszx = data.get("gszx")
    gszx_items: list[Any] = []
    if isinstance(gszx, dict):
        gszx_items = (gszx.get("data") or {}).get("items") or []
    for row in gszx_items[:30]:
        if not isinstance(row, dict):
            continue
        it = _normalize_gszx_row(row, sym, stock_name)
        if it:
            out.append(it)

    out.sort(key=lambda x: x.get("published_ts") or 0, reverse=True)
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in out:
        k = it["id"]
        if k in seen:
            continue
        seen.add(k)
        deduped.append(it)
    return deduped[:limit]


async def fetch_symbols_news_items(
    symbols: list[str],
    *,
    limit_per_symbol: int = 12,
    name_hints: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    import asyncio

    hints = name_hints or {}
    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return []

    async def one(sym: str) -> list[dict[str, Any]]:
        return await fetch_symbol_news_items(
            sym,
            limit=limit_per_symbol,
            stock_name=hints.get(sym) or hints.get(sym.split(".", 1)[0]),
        )

    groups = await asyncio.gather(*[one(s) for s in syms], return_exceptions=True)
    merged: list[dict[str, Any]] = []
    for g in groups:
        if isinstance(g, list):
            merged.extend(g)
    return merged
