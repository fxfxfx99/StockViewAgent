"""Build a bounded, dated evidence bundle from actual archived news and saved profiles."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any

from app.schemas.news import analysis_input_snapshot, clean_text, utc_iso
from app.services.news_priority import classify_priority
from app.storage import news_store


def _publication_timestamp(item: dict) -> float | None:
    try:
        value = item.get("published_ts")
        if value is None and item.get("published_at"):
            date = datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00"))
            value = date.timestamp() if date.tzinfo is not None else None
        value = float(value) if value is not None else None
        return value if value is not None and math.isfinite(value) and 0 <= value <= time.time() + 300 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def build_analysis_context(item: dict[str, Any], stocks: list[dict]) -> dict:
    """History is context, not proof of this event; never add future-dated articles."""
    now = time.time()
    published = _publication_timestamp(item)
    snapshot = analysis_input_snapshot(item)
    body = clean_text(item.get("content"))
    summary = clean_text(item.get("summary"))
    title = clean_text(item.get("title"))
    # Search RSS frequently repeats the headline as its entire description.
    # That is still title-only evidence, not additional reporting detail.
    summary_words = re.sub(r"[\W_]+", "", summary).casefold()
    title_words = re.sub(r"[\W_]+", "", title).casefold()
    if summary_words and summary_words in title_words:
        summary = ""
    kind = "full_text" if body else "summary" if summary else "title_only"
    limitations = []
    cap = 100
    if kind == "title_only":
        cap = 40
        limitations.append("仅有新闻标题，缺少正文与摘要，不能据此确认金额、业绩或事件细节。")
    elif kind == "summary":
        cap = 60 if len(summary) < 120 else 80
        limitations.append("本条仅提供摘要，尚未取得来源正文，判断需结合后续披露验证。")
    elif len(body) < 120:
        cap = 60
        limitations.append("来源提供的正文较短，可能缺少事件背景和完整条件。")
    if published is None:
        cap = min(cap, 55)
        limitations.append("发布时间无法确认，不得把该事件当作最新催化。")
    for stock in stocks:
        if not stock.get("industry") and not stock.get("main_business"):
            limitations.append(f"{stock['stock_code']} 缺少已保存的行业与主营资料，间接关联依据有限。")
    evidence = [{
        "source_id": "N0", "kind": "current_news", "article_id": item.get("id"),
        "title": snapshot["title"], "text": snapshot["content_excerpt"],
        "source": snapshot["source"], "published_at": utc_iso(published),
        "url": item.get("url") or item.get("link") or "",
    }]
    for index, stock in enumerate(stocks[:8], 1):
        # These are saved inputs, not independently verified company facts.
        profile = "\n".join(f"{key}: {stock[key]}" for key in
                            ("stock_code", "stock_name", "org_name", "industry", "main_business") if stock.get(key))
        evidence.append({
            "source_id": f"P{index}", "kind": "company_profile", "stock_code": stock["stock_code"],
            "title": f"{stock.get('stock_name') or stock['stock_code']} · 已保存公司资料",
            "text": profile[:1800], "source": "本地公司资料", "published_at": None, "url": "",
        })
    background = []
    # The cutoff is the event's publication time, so later news is never read backwards.
    cutoff = min(published, now) if published is not None else now
    try:
        history = news_store.analysis_background([s["stock_code"] for s in stocks],
                                                 exclude_id=str(item.get("id") or ""), before_ts=cutoff, limit=4)
    except Exception:
        history = []
        limitations.append("历史背景暂不可读取，本轮仅依据当前新闻和公司资料。")
    seen_urls = {item.get("url") or item.get("link") or ""}
    seen_titles = {clean_text(item.get("title")).casefold()}
    for row in history:
        url = row.get("url") or row.get("link") or ""
        title = clean_text(row.get("title"))
        if (url and url in seen_urls) or title.casefold() in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title.casefold())
        source = {
            "source_id": f"H{len(background) + 1}", "kind": "background_news", "article_id": row["id"],
            "title": title, "text": clean_text(row.get("content") or row.get("summary"), 1800),
            "source": row.get("source"), "published_at": utc_iso(row.get("published_ts")), "url": url,
            "observed_at": utc_iso(row.get("content_observed_at")),
        }
        background.append(source)
        evidence.append(source)
    return {"version": 2, "analysis_asof": utc_iso(now),
            "attention": classify_priority(item),
            "quality": {"text_kind": kind, "publication_date_known": published is not None,
                        "limitations": limitations, "confidence_cap": cap},
            "news_background": background, "evidence_sources": evidence}
