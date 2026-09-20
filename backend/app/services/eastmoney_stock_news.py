"""东财 F10：个股公告与公司资讯（供新闻入库与 Agent 解读）。"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

import httpx

from app.services.company_profile_em import yahoo_symbol_to_em_code
from app.services.market_time import market_timestamp
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
    # 部分 F10 时间以冒号分隔毫秒；ISO 解析保留秒数及显式时区。
    normalized = re.sub(r"(?<=\d{2}:\d{2}:\d{2}):(\d{1,3})$", r".\1", s)
    try:
        return s, market_timestamp(datetime.fromisoformat(normalized.replace("Z", "+00:00")))
    except ValueError:
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
    strict: bool = False,
    timeout_sec: float | None = None,
    max_retries: int | None = None,
) -> list[dict[str, Any]]:
    """拉取单标的公告及公司资讯；strict 模式区分源故障与正常空结果。

    默认沿用新闻采集的超时和 3 次重试。公司面板可传短超时、0 次重试，
    并启用 strict，让数据管线展示可恢复的公共源错误。
    """
    sym = (symbol or "").strip().upper()
    em_code = yahoo_symbol_to_em_code(sym)
    if not em_code:
        if strict:
            raise ValueError("东方财富公告仅支持 A 股代码")
        return []
    code6 = em_code[2:] if len(em_code) >= 8 else sym.split(".", 1)[0]
    limit = max(1, min(limit, 40))

    timeout = DEFAULT_NEWS_TIMEOUT if timeout_sec is None else httpx.Timeout(timeout_sec)
    attempts = 4 if max_retries is None else max(0, max_retries) + 1
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": NEWS_UA}) as client:
        r = await get_with_retry(
            client,
            _PAGE_AJAX,
            params={"code": em_code},
            max_attempts=attempts,
            headers={"Referer": _REFERER},
        )
        if r is None:
            if strict:
                raise RuntimeError("东方财富公告请求失败，请稍后重试")
            return []
        try:
            data = r.json()
        except ValueError as exc:
            if strict:
                raise ValueError("东方财富公告响应不是有效 JSON") from exc
            return []

    if not isinstance(data, dict):
        if strict:
            raise ValueError("东方财富公告响应格式异常")
        return []
    gsgg = data.get("gsgg")
    gszx = data.get("gszx")
    gszx_data = gszx.get("data") if isinstance(gszx, dict) else None
    gszx_items = gszx_data.get("items") if isinstance(gszx_data, dict) else None
    # 合法空列表意味着暂无公告/资讯，不能当成源故障。
    if not (isinstance(gsgg, list) or isinstance(gszx_items, list) or gszx == []):
        if strict:
            raise ValueError("东方财富公告响应缺少公告和资讯列表")
        return []

    out: list[dict[str, Any]] = []
    if isinstance(gsgg, list):
        for row in gsgg[:limit]:
            if not isinstance(row, dict):
                continue
            it = _normalize_gsgg_row(row, sym, code6)
            if it:
                out.append(it)

    for row in (gszx_items if isinstance(gszx_items, list) else [])[:30]:
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
