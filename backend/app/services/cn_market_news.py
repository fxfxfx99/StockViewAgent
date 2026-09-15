"""
中国 A 股相关财经快讯聚合（合规：优先官方/公开接口与 RSS，非登录抓取）。

多备用源由 cn_headline_source_registry 配置：东财多栏目 + 人民网/中新网等 RSS；
每日（上海时区）首次请求自动探测各源可用性并写入 data/cn_headline_sources_state.json；
可在 data/cn_headline_sources_override.json 扩展 RSS（站点变更时人工更新 URL）。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import feedparser
import httpx

from app.services import eastmoney_market
from app.services.news_dedup import deduplicate_news
from app.services.cn_headline_source_registry import (
    run_daily_maintenance_if_due,
    sources_for_fetch,
    write_override_template,
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_MAJOR_PAT = re.compile(
    r"(央行|证监会|金融监管|国务院|财政部|发改委|商务部|国资委|上交所|深交所|北交所|港交所|"
    r"降准|降息|加息|LPR|印花税|科创板|创业板|注册制|退市|停牌|复牌|风险警示|ST\*?|"
    r"立案调查|行政处罚|罚单|增持|减持|要约收购|重组|并购|借壳|业绩快报|业绩预告|"
    r"年报|半年报|季报|分红|配股|定增|破发|熔断|系统性风险|黑天鹅|"
    r"A股|沪深|沪指|深成指|创业板指|北向资金|南向资金|融资融券)",
    re.I,
)

_EM_LIST_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"


def _item_id(prefix: str, *parts: str) -> str:
    h = hashlib.sha256("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()[:14]
    return f"{prefix}-{h}"


def _parse_show_time(s: str | None) -> tuple[str, int | None]:
    if not s:
        return "", None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            ts = int(dt.timestamp())
            return s, ts
        except ValueError:
            continue
    return s, None


def _em_detail_search_url(title: str) -> str:
    return f"https://so.eastmoney.com/news/s?keyword={quote(title)}"


def _stocklist_entry_to_secid(entry: Any) -> str | None:
    if isinstance(entry, str):
        t = entry.strip()
        return t if t else None
    if isinstance(entry, dict):
        c = entry.get("code") or entry.get("stockCode")
        if c is not None:
            s = str(c).strip()
            return s if s else None
    return None


def _is_major(title: str, summary: str) -> bool:
    t = f"{title}\n{summary}"
    return bool(_MAJOR_PAT.search(t))


async def _fetch_rss(
    client: httpx.AsyncClient,
    id_prefix: str,
    label: str,
    url: str,
    referer: str = "",
) -> list[dict[str, Any]]:
    from app.services.news_fetch_http import get_with_retry

    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    r = await get_with_retry(client, url, headers=headers or None)
    if r is None:
        return []
    parsed = feedparser.parse(r.content)
    out: list[dict[str, Any]] = []
    for e in parsed.entries[:45]:
        title = html.unescape((e.get("title") or "").strip())
        link = (e.get("link") or "").strip() or "#"
        summary = (e.get("summary") or e.get("description") or "").strip()
        summary = re.sub(r"<[^>]+>", "", summary)
        summary = html.unescape(summary)[:800]
        published = e.get("published") or e.get("updated") or ""
        ts = None
        if hasattr(e, "published_parsed") and e.published_parsed:
            try:
                ts = int(time.mktime(e.published_parsed))
            except (OverflowError, ValueError):
                ts = None
        major = _is_major(title, summary)
        out.append(
            {
                "id": _item_id(id_prefix, link, title),
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
                "published_ts": ts,
                "source": label,
                "is_major": major,
                "related_codes": [],
                "related_entities": [],
            }
        )
    return out


async def _fetch_eastmoney_724(
    client: httpx.AsyncClient,
    page_size: int,
    fast_column: str,
    source_label: str,
) -> list[dict[str, Any]]:
    params = {
        "client": "web",
        "biz": "web_724",
        "fastColumn": fast_column,
        "sortEnd": "0",
        "pageSize": str(page_size),
        "req_trace": "1",
    }
    data: dict[str, Any] | None = None
    for _ in range(4):
        try:
            r = await client.get(_EM_LIST_URL, params=params)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                break
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            await asyncio.sleep(0.35 + random.random() * 0.45)
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    if not isinstance(inner, dict):
        inner = {}
    lst = inner.get("fastNewsList") or []
    out: list[dict[str, Any]] = []
    for row in lst:
        title = html.unescape((row.get("title") or "").strip())
        if not title:
            continue
        summary = (row.get("summary") or "").strip()
        summary = re.sub(r"<[^>]+>", "", summary)
        summary = html.unescape(summary)[:800]
        code = str(row.get("code") or "")
        show_time, ts = _parse_show_time(row.get("showTime"))
        stocks = row.get("stockList") or []
        codes: list[str] = []
        if isinstance(stocks, list):
            for s in stocks:
                sid = _stocklist_entry_to_secid(s)
                if sid:
                    codes.append(sid)
        major = _is_major(title, summary) or bool(codes)
        out.append(
            {
                "id": _item_id(f"em{fast_column}", code, title),
                "title": title,
                "link": _em_detail_search_url(title),
                "summary": summary,
                "published": show_time,
                "published_ts": ts,
                "source": source_label,
                "is_major": major,
                "related_codes": codes[:12],
                "related_entities": [],
            }
        )
    return out


async def _fetch_one_row(
    client: httpx.AsyncClient,
    row: dict[str, Any],
    em_page_size: int,
) -> tuple[str, list[dict[str, Any]]]:
    kind = row["kind"]
    label = row["label"]
    if kind == "eastmoney_json":
        g = await _fetch_eastmoney_724(client, em_page_size, row["fast_column"], label)
    elif kind == "rss":
        g = await _fetch_rss(
            client,
            row["id"],
            label,
            row["url"],
            row.get("referer") or "",
        )
    else:
        g = []
    return label, g


async def _gather_from_rows(
    client: httpx.AsyncClient,
    rows: list[dict[str, Any]],
    em_page_size: int,
) -> tuple[list[list[dict[str, Any]]], dict[str, int]]:
    pairs = await asyncio.gather(
        *[_fetch_one_row(client, row, em_page_size) for row in rows],
        return_exceptions=True,
    )
    meta: dict[str, int] = {}
    groups: list[list[dict[str, Any]]] = []
    for p in pairs:
        if isinstance(p, BaseException):
            continue
        label, g = p
        meta[label] = len(g)
        groups.append(g)
    return groups, meta


def _merge_groups(groups: list[list[dict[str, Any]]], major_first: bool) -> list[dict[str, Any]]:
    merged = deduplicate_news([item for group in groups for item in group if item.get("title")])
    if major_first:
        merged.sort(
            key=lambda x: (
                (0 if x.get("is_major") else 1),
                -(x.get("published_ts") or 0),
            )
        )
    else:
        merged.sort(key=lambda x: -(x.get("published_ts") or 0))
    return merged


async def _attach_related_entity_labels(items: list[dict[str, Any]]) -> None:
    """
    为每条快讯补充 related_entities：{ code, label }。
    label 优先为东财 f58 中文名；仅对每条前 4 个 secid 请求接口（与前端展示一致），其余 label 回退为 code。
    related_codes 仍保留完整列表供入库与匹配。
    """
    wanted: list[str] = []
    for it in items:
        rc = it.get("related_codes")
        if not isinstance(rc, list):
            continue
        for c in rc[:4]:
            s = str(c).strip() if c is not None else ""
            if s:
                wanted.append(s)
    resolved = await eastmoney_market.resolve_secid_display_labels(wanted)

    for it in items:
        rc = it.get("related_codes")
        if not isinstance(rc, list):
            it["related_entities"] = []
            continue
        ents: list[dict[str, str]] = []
        for c in rc[:12]:
            code = str(c).strip() if c is not None else ""
            if not code:
                continue
            label = resolved.get(code) or code
            ents.append({"code": code, "label": label})
        it["related_entities"] = ents


async def fetch_cn_headlines(
    limit: int = 50,
    major_first: bool = True,
) -> dict[str, Any]:
    limit = max(10, min(limit, 120))
    write_override_template()

    maint = await run_daily_maintenance_if_due()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(45.0, connect=15.0),
        headers={"User-Agent": UA, "Referer": "https://kuaixun.eastmoney.com/"},
    ) as client:
        rows = sources_for_fetch(include_disabled_rss=False)
        groups, fetch_meta = await _gather_from_rows(client, rows, em_page_size=55)
        merged = _merge_groups(groups, major_first)

        if not merged:
            rows_all = sources_for_fetch(include_disabled_rss=True)
            groups2, meta2 = await _gather_from_rows(client, rows_all, em_page_size=60)
            merged = _merge_groups(groups2, major_first)
            fetch_meta = meta2

    warnings: list[str] = []
    if not merged:
        warnings.append(
            "未合并到任何快讯：请检查网络、东财接口与 RSS；可查看 GET /api/news/cn-headlines/sources 与 data/cn_headline_sources_state.json。"
        )
    em_counts = [fetch_meta.get(lab, 0) for lab in fetch_meta if "东方财富" in lab]
    if merged and sum(em_counts) == 0:
        warnings.append("东方财富各栏目本次均无条目，当前结果主要来自 RSS 备用源。")

    if maint.get("notes"):
        warnings.extend(str(x) for x in maint["notes"] if x)

    await _attach_related_entity_labels(merged)

    catalog = [
        {"name": "东方财富 7×24", "type": "JSON", "url": _EM_LIST_URL, "note": "多 fastColumn 互为备份"},
        {"name": "人民网/中新网等", "type": "RSS", "note": "见 builtin_source_rows 与 override 配置"},
    ]

    return {
        "items": merged[:limit],
        "sources": catalog,
        "fetched_at": int(time.time()),
        "fetch_meta": fetch_meta,
        "warnings": warnings,
        "maintenance": {
            "ran": not maint.get("skipped"),
            "skipped": maint.get("skipped"),
            "calendar_date": maint.get("calendar_date"),
            "healthy_count": maint.get("healthy_count"),
        },
    }
