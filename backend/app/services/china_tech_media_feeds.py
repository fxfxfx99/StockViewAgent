"""
36氪、虎嗅等创投/TMT 媒体 RSS（公开摘要 + 原文链接）。

用于「自动补信息」：与 A 股快讯并列，补充产业与深度报道线索。
研究报告本体多在平台站内或 PDF，见 research_sources 配置与 /research-sources 接口。
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import re
import time
from typing import Any

import feedparser
import httpx

from app.storage import tech_media_store

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 主站 RSS（label, url, referer）；Referer 降低部分站点对爬虫的拦截概率
TECH_MEDIA_FEEDS: list[tuple[str, str, str]] = [
    ("36氪 · RSS", "https://36kr.com/feed", "https://36kr.com/"),
    ("虎嗅 · RSS", "https://www.huxiu.com/rss/0.xml", "https://www.huxiu.com/"),
]

_DEEP_HINT = re.compile(
    r"(深度|研报|行业研究|研究报告|观察|专访|调研|招股书解读|财报)",
    re.I,
)


def _item_id(source: str, link: str, title: str) -> str:
    h = hashlib.sha256(f"{source}|{link}|{title}".encode("utf-8", errors="ignore")).hexdigest()[:14]
    return f"tm-{h}"


def _strip_html(s: str) -> str:
    t = re.sub(r"<[^>]+>", " ", s or "")
    return html.unescape(t).strip()


def _entry_link(e: Any) -> str:
    raw = e.get("link")
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            return str(first.get("href") or first.get("url") or "").strip()
        return str(first).strip()
    return str(raw or "").strip() or "#"


async def _fetch_one(
    client: httpx.AsyncClient, label: str, url: str, referer: str, max_entries: int
) -> list[dict[str, Any]]:
    from app.services.news_fetch_http import get_with_retry

    headers = {"Referer": referer, "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8"}
    r = await get_with_retry(client, url, headers=headers)
    if r is None:
        return []
    parsed = feedparser.parse(r.content)
    out: list[dict[str, Any]] = []
    for e in parsed.entries[:max_entries]:
        title = html.unescape((e.get("title") or "").strip())
        link = _entry_link(e) or "#"
        summary = _strip_html((e.get("summary") or e.get("description") or ""))[:1200]
        published = e.get("published") or e.get("updated") or ""
        ts = None
        if hasattr(e, "published_parsed") and e.published_parsed:
            try:
                ts = int(time.mktime(e.published_parsed))
            except (OverflowError, ValueError):
                ts = None
        deep_hint = bool(_DEEP_HINT.search(f"{title}\n{summary}"))
        out.append(
            {
                "id": _item_id(label, link, title),
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
                "published_ts": ts,
                "source": label,
                "deep_research_hint": deep_hint,
            }
        )
    return out


async def fetch_tech_media_feeds(
    limit: int = 40,
    per_feed: int = 25,
) -> dict[str, Any]:
    limit = max(1, min(limit, 120))
    per_feed = max(8, min(per_feed, 55))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(38.0, connect=15.0),
        headers={"User-Agent": UA},
    ) as client:
        groups: list[list[dict[str, Any]]] = []
        for lab, u, ref in TECH_MEDIA_FEEDS:
            groups.append(await _fetch_one(client, lab, u, ref, per_feed))
            await asyncio.sleep(0.12)

    fetch_meta = {lab: len(g) for (lab, _, _), g in zip(TECH_MEDIA_FEEDS, groups)}

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for g in groups:
        for it in g:
            if it["link"] in seen:
                continue
            seen.add(it["link"])
            merged.append(it)

    if merged:
        tech_media_store.upsert_and_prune(merged, keep=100)

    stored = tech_media_store.list_recent(100)
    stored.sort(
        key=lambda x: (
            (0 if x.get("deep_research_hint") else 1),
            -(x.get("published_ts") or 0),
        )
    )
    warnings: list[str] = []
    if not stored and not merged:
        warnings.append(
            "36氪/虎嗅 RSS 均未拉到条目：请检查网络、RSS 地址是否变更；"
            "爬虫与 A 股快讯共用同一后端端口，请确认前端 /api 代理指向正在运行的 FastAPI。"
        )
    elif fetch_meta.get("36氪 · RSS", 0) == 0 and fetch_meta.get("虎嗅 · RSS", 0) == 0:
        warnings.append("本次两个 RSS 源均未解析到条目，可能为站点限流或 RSS 结构调整。")

    return {
        "items": stored[:limit],
        "feeds": [{"label": a, "url": b} for a, b, _ in TECH_MEDIA_FEEDS],
        "fetched_at": int(time.time()),
        "stored_cap": 100,
        "stored_count": len(stored),
        "fetch_meta": fetch_meta,
        "warnings": warnings,
    }
