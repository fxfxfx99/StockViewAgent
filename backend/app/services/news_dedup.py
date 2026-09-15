"""Conservative, local syndication matching with retained publisher provenance.

An article URL identifies an updated article. Different URLs require close titles,
matching numbers/direction and a short publication window; broad topic overlap is
never an event identity. No network requests or model calls are made here.
"""
from __future__ import annotations

import math
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi"}
_PUBLISHERS = {
    "新浪财经", "新浪网", "新浪", "东方财富", "东方财富网", "同花顺", "同花顺财经",
    "腾讯新闻", "腾讯网", "腾讯财经", "网易新闻", "网易财经", "网易", "搜狐", "搜狐财经",
    "证券时报", "证券时报网", "证券日报", "中国证券报", "上海证券报", "财联社",
    "每日经济新闻", "第一财经", "经济观察网", "中国经济网", "新华网", "新华社",
    "人民网", "中新网", "中国新闻网", "央视新闻", "央视网", "36氪", "虎嗅", "界面新闻",
    "reuters", "bloomberg", "cnbc", "yahoo finance", "marketwatch", "financial times",
}
_RECURRING = re.compile(r"每日|每周|每月|今日|日报|周报|月报|早报|晚报|晨报|午评|收评|盘前|盘后|\bdaily\b|\bmorning briefing\b|\bmarket wrap\b", re.I)
_NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)*(?:\s*[%％]|(?:万亿|亿|万|千|百)?(?:美元|港元|日元|人民币|个百分点|基点|元|股|台|吨|年|月|日|个|项|人|套|家|倍)?)")
_DIRECTION_GROUPS = (
    r"增长|增加|上涨|上升|上调|提升|增持|加息|盈利|获批|批准|同意|恢复|复牌",
    r"下降|下滑|下跌|减少|降低|下调|减持|降息|亏损|否决|终止|取消|暂停|停牌",
    r"不|未|无|否|拒绝|\bnot\b|\bno\b|\bdenies?\b",
    r"\brises?\b|\bincreases?\b|\bgrows?\b|\bgrowth\b|\bprofit\b|\bapproves?\b",
    r"\bfalls?\b|\bdecreases?\b|\bdrops?\b|\bloss\b|\brejects?\b",
)
_TRANSITIONS = re.compile(r"由盈转亏|扭盈为亏|由亏转盈|扭亏为盈")


def canonical_url(url: Any) -> str:
    """Return a comparison URL, or empty for invalid URLs and general/search pages.

    The stored publisher link is never replaced by this value. Query parameters
    carrying article identity are retained, including generic ``id``/``source``.
    """
    try:
        parts = urlsplit(str(url or "").strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return ""
        host = parts.hostname.lower()
        port = parts.port
    except (ValueError, TypeError):
        return ""
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in _TRACKING
             and not (host == "news.google.com" and key.lower() == "oc")]
    path = parts.path or "/"
    lower_path = path.rstrip("/").lower()
    keys = {key.lower() for key, _ in query}
    # Eastmoney's /news/s is a title search, not the original article. RSS search
    # endpoints and home/category pages likewise cannot identify one event.
    if re.search(r"(?:^|/)(?:search|search\.html|search\.aspx|s)$", lower_path):
        return ""
    if keys & {"q", "query", "keyword", "keywords", "search"} and not keys & {"id", "articleid", "article_id", "docid"}:
        return ""
    if lower_path in {"", "/index.html", "/index.htm", "/news", "/finance", "/stock", "/rss", "/feed"} and not keys & {"id", "articleid", "article_id", "docid"}:
        return ""
    netloc = f"[{host}]" if ":" in host else host
    if port and not (parts.scheme.lower() == "http" and port == 80 or parts.scheme.lower() == "https" and port == 443):
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))


def _title_text(item: dict[str, Any]) -> str:
    title = unicodedata.normalize("NFKC", str(item.get("title") or "")).strip().casefold()
    suffix = re.search(r"\s+(?:-|–|—|\|)\s+(.{2,40})$", title)
    if suffix and suffix.group(1).strip() in _PUBLISHERS:
        title = title[:suffix.start()]
    return title


def _plain_title(title: str) -> str:
    return "".join(character for character in title if character.isalnum())


def _subject_anchor(title: str) -> str | None:
    # Require a recognizable leading organization/event subject, not merely four
    # shared characters (two companies can share a city or corporate prefix).
    chinese = re.match(r"^(.{2,36}?)(?=[:：]|将|宣布|计划|拟|已经|已完成|尚未|本年度|本年|上半年|下半年|今年|下周|董事会|发布|披露|预计|拟定|签署)", title)
    if chinese:
        return _plain_title(chinese.group(1))
    english = re.match(r"^(.{3,50}?)\s+(?:announces?|reports?|plans?|will|to|has|posted|says?)\b", title, re.I)
    return _plain_title(english.group(1)) if english else None


def _timestamp(item: dict[str, Any]) -> float | None:
    try:
        value = float(item.get("published_ts"))
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _urls(item: dict[str, Any]) -> set[str]:
    urls = {canonical_url(item.get("url") or item.get("link"))}
    for source in item.get("source_articles") or []:
        if isinstance(source, dict):
            urls.add(canonical_url(source.get("url") or source.get("link")))
    return urls - {""}


def same_event(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Only merge strong article identity or a conservative near-copy headline."""
    left_title, right_title = _title_text(left), _title_text(right)
    if not left_title or not right_title:
        return False
    left_time, right_time = _timestamp(left), _timestamp(right)
    dated = left_time is not None and right_time is not None
    same_day = dated and int(left_time // 86400) == int(right_time // 86400)
    recurring = _RECURRING.search(left_title) or _RECURRING.search(right_title)
    if recurring and dated and not same_day:
        return False
    if _urls(left) & _urls(right):
        return True
    # A report and its separately published summary are distinct disclosure
    # documents. Long issuer names otherwise make the titles look like copies.
    filing_pattern = r"(?:半年度|年度|季度|中期)报告(摘要)?(?:[（(].{0,30}[）)])?$"
    left_filing = re.search(filing_pattern, left_title)
    right_filing = re.search(filing_pattern, right_title)
    if left_filing and right_filing and bool(left_filing.group(1)) != bool(right_filing.group(1)):
        return False
    plain_left, plain_right = _plain_title(left_title), _plain_title(right_title)
    # Check before removing punctuation: 1.2% and 12% otherwise have the same
    # alphanumeric title, as do several distinct decimal/date representations.
    if _NUMBER.findall(left_title) != _NUMBER.findall(right_title):
        return False
    # Preserve the existing exact-title/same-day identity. Unknown dates do not
    # supply a safe temporal window for merging different article URLs.
    if plain_left == plain_right and same_day:
        return True
    if not dated or abs(left_time - right_time) > 48 * 3600:
        return False
    if min(len(plain_left), len(plain_right)) < 12:
        return False
    if plain_left == plain_right:
        return not recurring
    if min(len(plain_left), len(plain_right)) < 16:
        return False
    if [bool(re.search(pattern, left_title, re.I)) for pattern in _DIRECTION_GROUPS] != [bool(re.search(pattern, right_title, re.I)) for pattern in _DIRECTION_GROUPS]:
        return False
    if _TRANSITIONS.findall(left_title) != _TRANSITIONS.findall(right_title):
        return False
    # Requiring the same leading subject deliberately leaves reordered or more
    # speculative paraphrases separate. Shared sector keywords are insufficient.
    subject = _subject_anchor(left_title)
    if not subject or subject != _subject_anchor(right_title):
        return False
    return SequenceMatcher(None, plain_left, plain_right, autojunk=False).ratio() >= 0.93


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(part).strip() for part in value if isinstance(part, (str, int)) and str(part).strip()))


def _source_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    provided = item.get("source_articles")
    if isinstance(provided, list) and provided:
        return [dict(row) for row in provided if isinstance(row, dict)]
    row = {"title": str(item.get("title") or ""), "url": str(item.get("url") or item.get("link") or ""),
           "source": str(item.get("source") or ""), "published_ts": item.get("published_ts"),
           "related_codes": _strings(item.get("related_codes")),
           "summary": str(item.get("summary") or ""), "content": str(item.get("content") or "")}
    if item.get("fetched_at") is not None:
        row["fetched_at"] = item["fetched_at"]
    if item.get("symbol_hint"):
        row["symbol_hint"] = str(item["symbol_hint"])
    return [row]


def _prefer_incoming(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """A newer full body at the same article URL can correct an older long body."""
    url = canonical_url(left.get("url") or left.get("link"))
    if url and url == canonical_url(right.get("url") or right.get("link")):
        if left.get("content") and right.get("content") and left["content"] != right["content"]:
            try:
                old_fetch, new_fetch = float(left.get("fetched_at")), float(right.get("fetched_at"))
                if math.isfinite(old_fetch) and math.isfinite(new_fetch) and new_fetch != old_fetch:
                    return new_fetch > old_fetch
            except (ValueError, TypeError, OverflowError):
                pass
    return len(str(right.get("content") or right.get("summary") or "")) > len(str(left.get("content") or left.get("summary") or ""))


def merge_news(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Retain left article identity and choose the fuller primary source as a unit.

    Search hints are retained only as source provenance; they never become stock
    matches. Callers re-evaluate stock evidence after merging.
    """
    merged = dict(left)
    if _prefer_incoming(left, right):
        for field in ("title", "source", "url", "link", "published_ts", "published", "published_at", "content", "summary", "symbol_hint"):
            merged[field] = right.get(field)
    try:
        fetches = [float(row.get("fetched_at")) for row in (left, right)]
        if all(math.isfinite(value) and value >= 0 for value in fetches):
            merged["fetched_at"] = max(fetches)
    except (ValueError, TypeError, OverflowError):
        pass
    sources: dict[tuple, dict[str, Any]] = {}
    for source in _source_rows(left) + _source_rows(right):
        key = tuple(str(source.get(field) or "") for field in ("title", "url", "source", "published_ts", "symbol_hint"))
        existing = sources.get(key)
        if existing:
            codes = list(dict.fromkeys(_strings(existing.get("related_codes")) + _strings(source.get("related_codes"))))
            if _prefer_incoming(existing, source):
                existing.update(source)
            existing["related_codes"] = codes
            try:
                fetches = [float(row.get("fetched_at")) for row in (existing, source)]
                if all(math.isfinite(value) and value >= 0 for value in fetches):
                    existing["fetched_at"] = max(fetches)
            except (ValueError, TypeError, OverflowError):
                pass
        else:
            sources[key] = {**source, "related_codes": _strings(source.get("related_codes"))}
    merged["source_articles"] = list(sources.values())
    merged["related_codes"] = list(dict.fromkeys(_strings(left.get("related_codes")) + _strings(right.get("related_codes"))))
    if left.get("is_major") or right.get("is_major"):
        merged["is_major"] = True
    if left.get("deep_research_hint") or right.get("deep_research_hint"):
        merged["deep_research_hint"] = True
    return merged


def deduplicate_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Index URL/title/subject candidates before the stricter pairwise check.

    Stock search feeds can contain thousands of rows before their final limit;
    avoid comparing every unrelated headline against every other headline.
    """
    output: list[dict[str, Any]] = []
    by_url: dict[str, set[int]] = {}
    by_title: dict[str, set[int]] = {}
    by_subject: dict[str, set[int]] = {}

    def index_row(index: int) -> None:
        row = output[index]
        for url in _urls(row):
            by_url.setdefault(url, set()).add(index)
        title = _title_text(row)
        by_title.setdefault(_plain_title(title), set()).add(index)
        subject = _subject_anchor(title)
        if subject:
            by_subject.setdefault(subject, set()).add(index)

    for item in items:
        title = _title_text(item)
        candidates = set(by_title.get(_plain_title(title), set()))
        candidates.update(by_subject.get(_subject_anchor(title), set()))
        for url in _urls(item):
            candidates.update(by_url.get(url, set()))
        for index in sorted(candidates):
            if same_event(output[index], item):
                output[index] = merge_news(output[index], item)
                index_row(index)
                break
        else:
            output.append(dict(item))
            index_row(len(output) - 1)
    return output
