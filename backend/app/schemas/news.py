"""Normalize collector output at the archive boundary; timestamps are UTC."""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def clean_text(value: Any, limit: int = 16000) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:limit].rstrip()


def rss_entry_content(entry: dict[str, Any]) -> str:
    """Keep inline feed body text; feedparser maps RSS content:encoded here too.

    Atom can carry alternative text representations. Keep the fullest one instead
    of repeating them, and never promote a summary or fetch an external content src.
    """
    content = entry.get("content") or []
    if isinstance(content, (str, dict)):
        content = [content]
    if not isinstance(content, (list, tuple)):
        return ""
    candidates = []
    for part in content:
        if isinstance(part, str):
            value = part
        elif isinstance(part, dict):
            media_type = str(part.get("type") or "text/plain").lower().split(";", 1)[0].strip()
            if media_type not in {"text", "html", "xhtml", "text/plain", "text/html", "application/xhtml+xml"}:
                continue
            value = part.get("value")
        else:
            continue
        if isinstance(value, str):
            candidates.append(clean_text(value, 16000))
    return max(candidates, key=len, default="")


def utc_iso(timestamp: float | int | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def analysis_input_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    """Exact bounded news text sent to the model, retained for later review."""
    return {
        "title": str(item.get("title") or "")[:2000],
        "content_excerpt": str(item.get("content") or item.get("summary") or "")[:10000],
        "source": item.get("source"),
        "published_at": item.get("published_at") or utc_iso(item.get("published_ts")) or item.get("published"),
    }


def analysis_content_hash(snapshot: dict[str, Any]) -> str:
    text = json.dumps({key: snapshot.get(key) or "" for key in ("title", "content_excerpt")},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(part).strip() for part in value
                             if isinstance(part, (str, int)) and str(part).strip()))


def _news_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _source_articles(value: Any, primary: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = None
    provided = [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []
    # A merged item already contains the original source rows. Recreating a row
    # from its combined related_codes would falsely attribute another publisher's
    # stock mapping to the primary publisher on every subsequent normalization.
    sources: dict[tuple, dict[str, Any]] = {}
    for entry in provided or [primary]:
        title = clean_text(entry.get("title"), 2000)
        if not title:
            continue
        timestamp = entry.get("published_ts")
        try:
            timestamp = int(float(timestamp)) if timestamp is not None else None
        except (TypeError, ValueError, OverflowError):
            timestamp = None
        if timestamp is not None and (timestamp < 0 or timestamp > time.time() + 86400):
            timestamp = None
        source = {"title": title, "url": _news_url(entry.get("url") or entry.get("link")),
                  "source": clean_text(entry.get("source"), 500), "published_ts": timestamp,
                  "related_codes": _string_list(entry.get("related_codes")),
                  "summary": clean_text(entry.get("summary"), 8000), "content": clean_text(entry.get("content"), 16000)}
        same_primary = all(source.get(field) == primary.get(field) for field in ("title", "url", "source", "published_ts"))
        if same_primary:
            for field, bound in (("summary", 8000), ("content", 16000)):
                if not source[field]:
                    source[field] = clean_text(primary.get(field), bound)
        try:
            source_fetch = float(entry.get("fetched_at") if entry.get("fetched_at") is not None
                                 else primary.get("fetched_at") if same_primary else None)
            if math.isfinite(source_fetch) and source_fetch >= 0:
                source["fetched_at"] = source_fetch
        except (ValueError, TypeError, OverflowError):
            pass
        hint = str(entry.get("symbol_hint") or "").strip().upper()
        if hint:
            source["symbol_hint"] = hint
        key = tuple(source.get(field) for field in ("title", "url", "source", "published_ts", "symbol_hint"))
        if key in sources:
            sources[key]["related_codes"] = list(dict.fromkeys(sources[key]["related_codes"] + source["related_codes"]))
            for field in ("summary", "content"):
                if len(source[field]) > len(sources[key][field]):
                    sources[key][field] = source[field]
        else:
            sources[key] = source
    return list(sources.values())


def normalize_news(item: dict[str, Any]) -> dict[str, Any] | None:
    title = clean_text(item.get("title"), 2000)
    if not title:
        return None
    url = _news_url(item.get("url") or item.get("link"))
    timestamp = item.get("published_ts")
    if timestamp is None and item.get("published_at"):
        try:
            dt = datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00"))
            timestamp = dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        except (TypeError, ValueError):
            pass
    try:
        timestamp = int(float(timestamp)) if timestamp is not None else None
    except (TypeError, ValueError, OverflowError):
        timestamp = None
    if timestamp is not None and (timestamp < 0 or timestamp > time.time() + 86400):
        timestamp = None
    # Syndication within a publication day is one event; recurring daily titles remain distinct.
    identity = re.sub(r"\s+", "", unicodedata.normalize("NFKC", title)).casefold()
    identity += "|" + (utc_iso(timestamp) or "unknown-date")[:10]
    previous_id = str(item.get("id") or "")
    article_id = previous_id if re.fullmatch(r"news-[0-9a-f]{24}", previous_id) else "news-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    fetched = item.get("fetched_at")
    try:
        fetched = float(fetched) if fetched is not None else time.time()
    except (TypeError, ValueError):
        fetched = time.time()
    if not math.isfinite(fetched) or fetched < 0:
        fetched = time.time()
    normalized = {
        "id": article_id,
        "title": title,
        "content": clean_text(item.get("content")),
        "summary": clean_text(item.get("summary"), 8000),
        "source": clean_text(item.get("source"), 500),
        "url": url,
        "link": url,
        "published_at": utc_iso(timestamp),
        "published": str(item.get("published") or utc_iso(timestamp) or ""),
        "published_ts": timestamp,
        "fetched_at": fetched,
        "symbol_hint": str(item.get("symbol_hint") or "").strip().upper() or None,
        "matched_symbols": _string_list(item.get("matched_symbols")),
        "related_codes": _string_list(item.get("related_codes")),
        "match_evidence": item.get("match_evidence") if isinstance(item.get("match_evidence"), dict) else {},
    }
    normalized["source_articles"] = _source_articles(item.get("source_articles"), normalized)
    normalized["related_codes"] = list(dict.fromkeys(normalized["related_codes"] + [
        code for source in normalized["source_articles"] for code in source["related_codes"]
    ]))
    return normalized
