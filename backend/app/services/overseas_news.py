"""Always-on, bounded public news polling; terminal feeds require licensed access.

URLs are refreshed only from trusted publishers' RSS discovery pages. Network
retries may rotate an operator's configured proxies on transport failures only;
HTTP denial/rate limits always enter persistent cooldown, including manual runs.
"""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import ipaddress
import json
import logging
import math
import re
import shutil
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx

from app.config import settings
from app.schemas.news import clean_text
from app.storage import overseas_news_store as store

logger = logging.getLogger(__name__)


class _HTTPSecretFilter(logging.Filter):
    """httpx INFO logs otherwise expose Benzinga's required query-string token."""
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = re.sub(r"([?&](?:token|api_key|apikey)=)[^&\s\"]+", r"\1[REDACTED]", message, flags=re.I)
        message = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", message, flags=re.I)
        record.msg, record.args = message, ()
        return True


logging.getLogger("httpx").addFilter(_HTTPSecretFilter())
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 100
LEASE_NAME = "overseas_news_collection"
LEASE_TTL = 120
_proxy_index = 0


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    urls: tuple[str, ...]
    feed_hosts: tuple[str, ...]
    article_hosts: tuple[str, ...]
    transport: str = "rss"
    discovery_url: str = ""
    access_note: str = "公开 RSS 标题与短摘要，非付费终端；更新频率和内容由发布方决定。"


SOURCES = (
    Source("reuters", "Reuters（路透）",
           ("https://news.google.com/rss/search?q=site%3Areuters.com+when%3A7d&hl=en-US&gl=US&ceid=US%3Aen",),
           ("news.google.com",), ("reuters.com", "news.google.com"), "google_news_rss",
           access_note="Google News 索引转引的 Reuters 标题，可能延迟或缺漏；不是 Reuters 官方实时线或终端。"),
    Source("bloomberg", "Bloomberg（彭博）",
           ("https://feeds.bloomberg.com/markets/news.rss", "https://www.bloomberg.com/feeds/markets/news.rss"),
           ("feeds.bloomberg.com", "www.bloomberg.com"), ("bloomberg.com",)),
    Source("benzinga", "Benzinga", ("https://www.benzinga.com/feed",),
           ("www.benzinga.com", "benzinga.com", "api.benzinga.com"), ("benzinga.com",),
           discovery_url="https://www.benzinga.com/",
           access_note="公开 RSS；配置获授权的 Benzinga API Key 后改用 News API。API 权限和时效取决于订阅。"),
    Source("wsj", "WSJ（华尔街日报）",
           ("https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",),
           ("feeds.content.dowjones.io",), ("wsj.com", "barrons.com", "marketwatch.com"),
           discovery_url="https://www.wsj.com/news/rss-news-and-feeds"),
    Source("ft", "FT（金融时报）", ("https://www.ft.com/markets?format=rss", "https://www.ft.com/rss/home"),
           ("www.ft.com", "ft.com"), ("ft.com",), discovery_url="https://www.ft.com/markets"),
    Source("cnbc", "CNBC", ("https://www.cnbc.com/id/100003114/device/rss/rss.html",),
           ("www.cnbc.com",), ("cnbc.com",)),
)


def _enabled() -> bool:
    return bool(getattr(settings, "overseas_news_enabled", True) and _collection_config()["overseas_enabled"])


def _collection_config():
    from app.services.data_collection import get_config
    return get_config()


def _poll_interval() -> int:
    return int(_collection_config()["overseas_poll_interval_sec"])


def _resource_state():
    from app.services import collector_resources
    config = _collection_config()
    memory = collector_resources.snapshot(config["max_memory_mb"])
    reason = memory["reason"]
    if shutil.disk_usage(settings.data_dir).free < config["min_free_disk_mb"] * 1024**2:
        reason = "low_disk_space"
    return config, memory, reason


def _api_key() -> str:
    return str(getattr(settings, "overseas_news_benzinga_api_key", "") or "").strip()


def _transport(source: Source) -> str:
    return "benzinga_api" if source.id == "benzinga" and _api_key() else source.transport


def _allowed_url(value: Any, hosts: tuple[str, ...], *, subdomains: bool = False) -> str:
    """No credentials, private IPs, alternate ports, or javascript article links."""
    raw = html.unescape(str(value or "").strip())
    if len(raw) > 4096 or re.search(r"[\x00-\x20\x7f\\]", raw):
        return ""
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
            return ""
        try:
            ipaddress.ip_address(host)
            return ""
        except ValueError:
            pass
        if not any(host == allowed or (subdomains and host.endswith("." + allowed)) for allowed in hosts):
            return ""
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                 if not key.lower().startswith("utm_") and key.lower() not in {"token", "api_key", "apikey", "gclid", "fbclid"}]
        return urlunsplit(("https", host, parts.path or "/", urlencode(query), ""))
    except (ValueError, UnicodeError):
        return ""


def _feed_url(source: Source, value: str) -> str:
    return _allowed_url(value, source.feed_hosts)


def _snippet(value: Any) -> str:
    # Store only publisher-supplied short excerpts, never crawl article bodies.
    return " ".join(clean_text(value, 320).split()[:60])


def _timestamp(value: Any) -> float | None:
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
        else:
            try:
                stamp = parsedate_to_datetime(str(value)).timestamp()
            except (ValueError, TypeError, OverflowError):
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                stamp = dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        return stamp if 0 <= stamp <= time.time() + 86400 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _item(source: Source, title: Any, url: Any, summary: Any, published: Any, fetched: float) -> dict[str, Any] | None:
    title = clean_text(title, 1000)
    url = _allowed_url(url, source.article_hosts, subdomains=True)
    if not title or not url:
        return None
    identity = hashlib.sha256(f"{source.id}|{url}".encode()).hexdigest()[:24]
    return {"id": "overseas-" + identity, "title": title, "url": url,
            "summary": _snippet(summary), "source": "Reuters · Google News 索引" if source.id == "reuters" else source.label,
            "source_id": source.id, "published_ts": _timestamp(published), "fetched_at": fetched}


def _parse_rss(body: bytes, source: Source, fetched: float) -> list[dict[str, Any]]:
    if len(body) > MAX_RESPONSE_BYTES or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", body, re.I):
        raise ValueError("Unsafe or oversized RSS")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("Source did not return valid RSS/Atom XML") from exc
    if root.tag.rsplit("}", 1)[-1].lower() not in {"rss", "feed", "rdf"}:
        raise ValueError("Source returned a webpage instead of RSS/Atom")
    parsed = feedparser.parse(body)
    if not parsed.version:
        raise ValueError("Unrecognized RSS/Atom document")
    items = []
    for entry in parsed.entries[:MAX_ENTRIES]:
        # Restrict Reuters search results to its publisher attribution when present.
        publisher = entry.get("source") or {}
        if source.id == "reuters" and publisher:
            if "reuters" not in str(publisher.get("title", "")).lower():
                continue
        date = entry.get("published_parsed") or entry.get("updated_parsed")
        stamp = calendar.timegm(date) if date else entry.get("published") or entry.get("updated")
        row = _item(source, entry.get("title"), entry.get("link"),
                    entry.get("summary") or entry.get("description"), stamp, fetched)
        if row:
            items.append(row)
    if parsed.entries and not items:
        raise ValueError("RSS contained no usable publisher links")
    return list({item["id"]: item for item in items}.values())


def _parse_api(body: bytes, source: Source, fetched: float) -> list[dict[str, Any]]:
    try:
        entries = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Benzinga returned invalid JSON") from exc
    if not isinstance(entries, list):
        raise ValueError("Benzinga returned an unexpected API payload")
    items = []
    for entry in entries[:MAX_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        row = _item(source, entry.get("title"), entry.get("url"), entry.get("teaser") or entry.get("abstract"),
                    entry.get("created") or entry.get("updated"), fetched)
        if row:
            items.append(row)
    if entries and not items:
        raise ValueError("Benzinga contained no usable publisher links")
    return list({item["id"]: item for item in items}.values())


class FetchError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class FetchResult:
    body: bytes
    status: int
    headers: dict[str, str]
    url: str


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0


def _proxies() -> list[str]:
    values = str(getattr(settings, "overseas_news_proxy_urls", "") or "").split(",")
    out = []
    for value in values:
        value = value.strip()
        if not value:
            continue
        try:
            parts = urlsplit(value)
            if parts.scheme in {"http", "https"} and parts.hostname and parts.port:
                out.append(value)
        except ValueError:
            continue
    return out


def _new_client(proxy: str | None) -> httpx.AsyncClient:
    timeout = max(1, min(60, float(getattr(settings, "overseas_news_timeout_sec", 15))))
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 10)), proxy=proxy,
                             trust_env=not bool(proxy), follow_redirects=False,
                             headers={"User-Agent": "StockViewAgent/1.0 (public RSS reader)",
                                      "Accept-Encoding": "gzip, deflate",
                                      "Accept": "application/rss+xml,application/atom+xml,application/xml,application/json;q=0.9,*/*;q=0.5"})


async def _fetch(source: Source, url: str, *, headers: dict[str, str] | None = None,
                 params: dict[str, Any] | None = None, discovery: bool = False) -> FetchResult:
    from app.services.news_fetch_http import _bounded_body

    global _proxy_index
    allowed_hosts = source.feed_hosts
    if discovery and source.discovery_url:
        allowed_hosts += (urlsplit(source.discovery_url).hostname or "",)
    if not _allowed_url(url, allowed_hosts):
        raise FetchError("Untrusted source URL")
    proxies = _proxies()
    for attempt in range(2):
        proxy = proxies[_proxy_index % len(proxies)] if proxies else None
        try:
            # Fresh client/connections refresh DNS; IPs are never fabricated or scraped.
            async with _new_client(proxy) as client:
                current = url
                request_params = params
                for redirect in range(4):
                    async with client.stream("GET", current, headers=headers, params=request_params) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if params:  # Never forward a licensed API token via redirects.
                                raise FetchError("Benzinga API redirected; endpoint requires review")
                            target = _allowed_url(urljoin(current, response.headers.get("location", "")), allowed_hosts)
                            if not target or redirect == 3:
                                raise FetchError("Untrusted or excessive source redirect")
                            current = target
                            request_params = None
                            continue
                        if response.status_code not in {200, 304}:
                            raise FetchError(f"HTTP {response.status_code}", status=response.status_code,
                                             retry_after=_retry_after(response.headers.get("retry-after")))
                        if response.status_code == 304:
                            # A 304 can describe the cached representation's size
                            # and encoding, but has no response body to decode.
                            return FetchResult(b"", 304, dict(response.headers), current)
                        length = response.headers.get("content-length", "")
                        if length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                            raise FetchError("Response exceeded 2 MiB limit")
                        # Limit decompression itself; aiter_bytes() may allocate
                        # an entire expanded gzip chunk before a length check.
                        content = await _bounded_body(response, MAX_RESPONSE_BYTES)
                        if content is None:
                            raise FetchError("Response exceeded 2 MiB limit or used invalid compression")
                        return FetchResult(content, response.status_code, dict(response.headers), current)
        except httpx.TransportError as exc:
            if attempt == 0:
                if proxies:
                    _proxy_index = (_proxy_index + 1) % len(proxies)
                continue
            # Exception URLs may contain token or proxy credentials; never persist them.
            raise FetchError("Network transport failed: " + type(exc).__name__) from None
    raise FetchError("Source unavailable")


class _RSSLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs)
        kind = str(attrs.get("type") or "").lower()
        href = attrs.get("href") or ""
        if (tag == "link" and "alternate" in str(attrs.get("rel") or "").lower()
                and kind in {"application/rss+xml", "application/atom+xml"}):
            self.links.append(href)
        elif tag == "a" and re.search(r"(?:/rss/|\.rss(?:\?|$)|[?&]format=rss)", href, re.I):
            self.links.append(href)


async def _maintain_source(source: Source, state: dict[str, Any]) -> None:
    """Daily discovery cannot replace a working feed with an arbitrary website."""
    if time.time() - (state.get("last_maintenance_at") or 0) < 86400:
        return
    changes: dict[str, Any] = {"last_maintenance_at": time.time(), "maintenance_error": None}
    if source.discovery_url and _transport(source) != "benzinga_api":
        try:
            response = await _fetch(source, source.discovery_url, discovery=True)
            parser = _RSSLinks()
            parser.feed(response.body.decode("utf-8", errors="replace"))
            candidates = [_feed_url(source, urljoin(response.url, link)) for link in parser.links]
            changes["discovered_urls"] = list(dict.fromkeys(url for url in candidates if url))[:8]
        except (FetchError, ValueError, httpx.HTTPError) as exc:
            changes["maintenance_error"] = str(exc)[:180] if isinstance(exc, FetchError) else "Discovery page unavailable"
            if isinstance(exc, FetchError) and exc.status in {401, 403, 429}:
                current = store.sources().get(source.id, {})
                wait = max(_poll_interval() * 2, exc.retry_after, 1800 if exc.status != 429 else 0)
                changes["next_poll_at"] = max(current.get("next_poll_at") or 0, time.time() + wait)
                changes["cooldown_reason"] = exc.status
    store.update_source(source.id, **changes)


def _candidate_urls(source: Source, state: dict[str, Any]) -> list[str]:
    if _transport(source) == "benzinga_api":
        return ["https://api.benzinga.com/api/v2/news"]
    urls = [state.get("active_url"), *source.urls, *(state.get("discovered_urls") or [])]
    # A prior API configuration must not persist its endpoint after key removal.
    return list(dict.fromkeys(valid for url in urls if url and "api.benzinga.com" not in url
                             and (valid := _feed_url(source, url))))[:4]


async def _fetch_benzinga(source: Source, url: str, headers: dict[str, str], params: dict[str, Any],
                          state: dict[str, Any], changed_transport: bool) -> tuple[FetchResult, list[dict[str, Any]], dict[str, Any]]:
    """Drain bounded delta pages across cycles without advancing an incomplete watermark.

    The first connection bootstraps the latest 100 headlines. Subsequent cycles
    overlap completed windows by ten minutes, preserving late updates by URL.
    """
    state = {} if changed_transport else state
    cycle_start = time.time()
    watermark = state.get("api_watermark")
    if watermark is None:
        response = await _fetch(source, url, headers=headers, params=params)
        rows = _parse_api(response.body, source, cycle_start)
        return response, rows, {"api_watermark": cycle_start, "api_pending_page": None,
                                "api_pending_since": None, "api_pending_until": None}
    since = state.get("api_pending_since")
    since = int(max(0, watermark - 600)) if since is None else since
    start_page = int(state.get("api_pending_page") or 0)
    until = state.get("api_pending_until") or cycle_start
    params.update(updatedSince=since, sort="updated:asc")
    rows = []
    for page in range(start_page, start_page + 5):
        params["page"] = page
        response = await _fetch(source, url, headers=headers, params=params)
        page_rows = _parse_api(response.body, source, time.time())
        rows.extend(page_rows)
        entries = json.loads(response.body)
        if len(entries) < MAX_ENTRIES:
            return response, list({row["id"]: row for row in rows}.values()), {
                "api_watermark": until, "api_pending_page": None, "api_pending_since": None, "api_pending_until": None}
    return response, list({row["id"]: row for row in rows}.values()), {
        "api_watermark": watermark, "api_pending_page": start_page + 5,
        "api_pending_since": since, "api_pending_until": until}


async def _poll_source(source: Source, *, force: bool = False) -> dict[str, Any]:
    state = store.sources().get(source.id, {})
    now = time.time()
    changed_transport = state.get("transport") not in {None, _transport(source)}
    cooling_down = state.get("next_poll_at", 0) > now
    # A refresh button cannot bypass publisher denial or rate-limit cooldown.
    if cooling_down and (state.get("status") in {"rate_limited", "access_denied"} or state.get("cooldown_reason") in {401, 403, 429}
                         or (not changed_transport and (not force or state.get("status") not in {"ok", "stale"}))):
        return {"source_id": source.id, "skipped": True, "items_received": 0}
    start = time.monotonic()
    store.update_source(source.id, last_attempt_at=now, transport=_transport(source))
    validators = state.get("validators") or {}
    errors: list[FetchError] = []
    stale_success: tuple[dict[str, Any], dict[str, Any]] | None = None
    for url in _candidate_urls(source, state):
        cache = validators.get(url) or {}
        headers = {}
        if cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]
        params = None
        if _transport(source) == "benzinga_api":
            headers = {"Accept": "application/json"}
            params = {"token": _api_key(), "displayOutput": "abstract", "pageSize": MAX_ENTRIES,
                      "sort": "updated:desc"}
        try:
            cursor_changes: dict[str, Any] = {}
            if params:
                response, rows, cursor_changes = await _fetch_benzinga(source, url, headers, params, state, changed_transport)
            else:
                response = await _fetch(source, url, headers=headers)
                rows = [] if response.status == 304 else _parse_rss(response.body, source, time.time())
            fetched = time.time()
            newest = max((row["published_ts"] for row in rows if row.get("published_ts") is not None), default=None)
            if response.status == 304 or not rows:
                newest = cache.get("newest_published_at") or state.get("newest_published_at")
            elif params:
                newest = max(newest or 0, state.get("newest_published_at") or 0) or None
            stale = bool(newest is not None and fetched - newest > 72 * 3600)
            store.upsert_many(rows)
            validators[url] = {"etag": response.headers.get("etag") or (cache.get("etag") if response.status == 304 else None),
                               "last_modified": response.headers.get("last-modified") or (cache.get("last_modified") if response.status == 304 else None),
                               "newest_published_at": newest}
            if response.url != url:
                validators[response.url] = validators[url]
            success_changes = dict(status="stale" if stale else "ok", active_url=response.url, last_success_at=fetched,
                                   next_poll_at=fetched + (30 if cursor_changes.get("api_pending_page") else _poll_interval()),
                                   error="Feed returned headlines older than 72 hours" if stale else None, failure_count=0,
                                   newest_published_at=newest, content_stale=stale, cooldown_reason=None,
                                   latency_sec=round(time.monotonic() - start, 3), items_received=len(rows),
                                   validators=validators, transport=_transport(source), **cursor_changes)
            result = {"source_id": source.id, "items_received": len(rows), "not_modified": response.status == 304}
            if stale:
                if stale_success is None or newest > (stale_success[0].get("newest_published_at") or 0):
                    stale_success = (success_changes, result)
                continue  # A frozen but valid feed cannot monopolize the source.
            store.update_source(source.id, **success_changes)
            await _maintain_source(source, state)
            return result
        except (FetchError, ValueError, httpx.HTTPError) as exc:
            error = exc if isinstance(exc, FetchError) else FetchError(str(exc)[:180] if isinstance(exc, ValueError) else "HTTP client failed")
            errors.append(error)
            if error.status in {401, 403, 429}:
                break
    error = errors[-1] if errors else FetchError("No valid source endpoint")
    if stale_success:
        store.update_source(source.id, **stale_success[0])
        if error.status not in {401, 403, 429}:
            await _maintain_source(source, state)
            return stale_success[1]
    failures = int(state.get("failure_count") or 0) + 1
    cooldown = min(21600, _poll_interval() * 2 ** min(failures, 8))
    if error.status in {401, 403}:
        cooldown = max(cooldown, 1800)
    cooldown = max(cooldown, error.retry_after)
    status_name = "rate_limited" if error.status == 429 else "access_denied" if error.status in {401, 403} else "error"
    store.update_source(source.id, status=status_name, error=str(error)[:180], failure_count=failures, cooldown_reason=error.status,
                        next_poll_at=time.time() + cooldown, latency_sec=round(time.monotonic() - start, 3), items_received=0)
    # Discovery shares the same publisher boundary; do not issue another request
    # after a rate limit or denial merely to search for an alternative URL.
    if error.status not in {401, 403, 429}:
        await _maintain_source(source, state)
    return {"source_id": source.id, "items_received": 0, "error": str(error)}


async def _flush_archive() -> None:
    """Retry durable mirror writes even when publishers answer 304 or are offline."""
    rows = store.pending_archive()
    if not rows:
        return

    def flush() -> None:
        from app.storage import news_store
        news_store.upsert_many([dict(row) for row in rows])
        store.acknowledge_archive(rows)
        store.set_metadata("archive_sync_error", None)

    from app.services.data_collection import durable_call
    try:
        await durable_call(flush)
    except Exception:
        store.set_metadata("archive_sync_error", "Shared news archive unavailable; pending headlines will retry")
        logger.warning("Overseas headlines saved; shared news archive sync will retry")


async def _heartbeat(owner: str, task: asyncio.Task) -> None:
    while True:
        await asyncio.sleep(30)
        if not store.renew_lease(LEASE_NAME, owner, LEASE_TTL):
            # Stop network work if ownership was lost, rather than duplicate it.
            task.cancel()
            return


async def collect_once(force: bool = False) -> dict[str, Any]:
    if not _enabled():
        return {"enabled": False, "items_received": 0, "skipped": "disabled", "sources": []}
    config, memory, reason = await asyncio.to_thread(_resource_state)
    if reason:
        store.set_metadata("pause_reason", reason)
        return {"enabled": True, "items_received": 0, "skipped": reason, "resources": memory, "sources": []}
    store.set_metadata("pause_reason", None)
    owner = uuid.uuid4().hex
    if not store.acquire_lease(LEASE_NAME, owner, LEASE_TTL):
        return {"enabled": True, "items_received": 0, "skipped": "another_worker", "sources": []}
    heartbeat = asyncio.create_task(_heartbeat(owner, asyncio.current_task()))
    tasks = []
    try:
        semaphore = asyncio.Semaphore(config["max_concurrent_jobs"])

        async def run(source: Source) -> dict[str, Any]:
            async with semaphore:
                if not _enabled():
                    return {"source_id": source.id, "items_received": 0, "skipped": "disabled"}
                _, _, pressure_reason = await asyncio.to_thread(_resource_state)
                if pressure_reason:
                    store.set_metadata("pause_reason", pressure_reason)
                    return {"source_id": source.id, "items_received": 0, "skipped": pressure_reason}
                try:
                    return await _poll_source(source, force=force)
                except Exception:
                    logger.exception("Overseas collector failed for source %s", source.id)
                    store.update_source(source.id, status="error", error="Collector failed; see server logs",
                                        next_poll_at=time.time() + _poll_interval() * 2)
                    return {"source_id": source.id, "items_received": 0, "error": "Collector failed"}

        tasks = [asyncio.create_task(run(source)) for source in SOURCES]
        rows = await asyncio.gather(*tasks)
        await _flush_archive()
        store.set_metadata("last_cycle_at", time.time())
        return {"enabled": True, "items_received": sum(row.get("items_received", 0) for row in rows), "sources": rows}
    finally:
        # A sibling failure or repeated shutdown cancellation must not release
        # ownership while any source or archive writer is still running.
        for task in tasks:
            if not task.done():
                task.cancel()
        drain = asyncio.gather(*tasks, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                continue
        try:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        finally:
            store.release_lease(LEASE_NAME, owner)


async def scheduler_loop() -> None:
    """Start directly from API lifespan, regardless of the unrelated scheduler flag."""
    # Runtime pause does not terminate the scheduler; saving enabled=true resumes
    # it without reloading the API. The environment flag still controls startup.
    while getattr(settings, "overseas_news_enabled", True):
        store.set_metadata("scheduler_last_seen_at", time.time())
        try:
            await collect_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Overseas news collection cycle failed")
        await asyncio.sleep(min(30, _poll_interval()))


def status() -> dict[str, Any]:
    states = store.sources()
    enabled = _enabled()
    rows = []
    fields = ("active_url", "last_success_at", "last_attempt_at", "next_poll_at", "error", "latency_sec", "items_received")
    for source in SOURCES:
        state = states.get(source.id, {})
        row = {key: state.get(key) for key in fields}
        stale = bool(state.get("newest_published_at") and time.time() - state["newest_published_at"] > 72 * 3600)
        display_status = "stale" if stale and state.get("status") == "ok" else state.get("status", "pending")
        row.update(id=source.id, label=source.label, status=display_status if enabled else "disabled",
                   newest_published_at=state.get("newest_published_at"), content_stale=stale,
                   transport=_transport(source), access_note=source.access_note,
                   last_maintenance_at=state.get("last_maintenance_at"), maintenance_error=state.get("maintenance_error"))
        if _transport(source) == "benzinga_api":
            row["access_note"] = "使用已配置的 Benzinga News API；可用范围与更新时效取决于授权订阅，不代表完整终端权限。"
        rows.append(row)
    collecting = store.lease_active(LEASE_NAME)
    scheduler_seen = store.metadata("scheduler_last_seen_at")
    config, memory, reason = _resource_state()
    reason = "service_disabled" if not settings.overseas_news_enabled else "automatic_disabled" if not config["overseas_enabled"] else reason
    return {"enabled": enabled, "service_enabled": bool(settings.overseas_news_enabled), "poll_interval_sec": _poll_interval(),
            "automatic_enabled": bool(config["overseas_enabled"]), "resources": memory, "reason": reason,
            "max_concurrent_sources": config["max_concurrent_jobs"],
            "running": enabled and (collecting or bool(scheduler_seen and time.time() - scheduler_seen < 90)),
            "collecting": collecting, "scheduler_last_seen_at": scheduler_seen,
            "last_cycle_at": store.metadata("last_cycle_at"), "sources": rows,
            "archive_pending": store.archive_pending_count(), "archive_sync_error": store.metadata("archive_sync_error"),
            "network_policy": "仅在网络连接失败时重建连接并轮换已配置代理；401/403/429 遵守冷却时间。"}


def list_feed(source: str | None = None, query: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return store.list_feed(source=source, query=query, limit=limit, offset=offset)
