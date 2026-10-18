"""Evidence-based maintenance of installed adapters and public RSS definitions.

The learned state is data, never executable code. Provider health is scoped to the
account and capability; public news definitions are shared and independently
validated. Every routing/definition change retains immutable audit evidence.
"""
from __future__ import annotations

import asyncio
import calendar
from html.parser import HTMLParser
import ipaddress
import json
import math
import socket
import time
from urllib.parse import urljoin, urlparse
import uuid

import feedparser
import httpx

from app.security.user_context import current_user_id
from app.storage.research_store import ResearchStore, canonical_json, content_hash

VERSION = "source-policy-v1"
_HEALTH_KEY = "source_autopilot:health"
_NEWS_KEY = "source_autopilot:news"
_ROOTS = ("people.com.cn", "people.cn", "chinanews.com.cn", "sina.com.cn", "jrj.com.cn")
_HOMES = (
    "https://finance.people.com.cn/", "https://www.chinanews.com.cn/finance/",
    "https://finance.sina.com.cn/", "https://finance.jrj.com.cn/",
)
_PUBLIC_PROBES = ("eastmoney", "tencent", "baidu", "sina")
_KINDS = {"daily", "minute"}
_SOURCES = {"eastmoney", "tencent", "baidu", "sina", "free_stockdb", "baostock", "pytdx",
            "klineshare", "tushare", "tencent_minute", "baostock_minute", "pytdx_minute"}


def _uid(user_id=None):
    return int(user_id if user_id is not None else current_user_id.get() or 1)


def _event(db, conn, uid, payload):
    encoded = canonical_json({"version": VERSION, **payload})
    conn.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)", (
        uid, "source_change", str(uuid.uuid4()), encoded,
        content_hash(json.loads(encoded)), time.time(),
    ))


def _read(conn, uid, key, default):
    row = conn.execute("SELECT value FROM state WHERE user_id=? AND key=?", (uid, key)).fetchone()
    return json.loads(row[0]) if row else default


def record_outcome(kind, source, *, ok, reason="request_failed", user_id=None, now=None):
    """Never persist raw HTTP exceptions: they can contain credentials or URLs."""
    if kind not in _KINDS or source not in _SOURCES:
        raise ValueError("Unknown source capability")
    uid, stamp = _uid(user_id), time.time() if now is None else float(now)
    reason = reason if reason in {"request_failed", "invalid_payload", "canary_failed", "validated"} else "request_failed"
    db = ResearchStore()
    with db.connection(write=True) as conn:
        health = _read(conn, uid, _HEALTH_KEY, {})
        key = f"{kind}:{source}"
        old = health.get(key, {})
        failures = 0 if ok else min(int(old.get("failures", 0)) + 1, 100)
        # A single bad symbol does not quarantine the entire provider.
        delay = min(6 * 3600, 300 * 2 ** min(failures - 3, 6)) if failures >= 3 else 0
        value = {"id": source, "kind": kind, "ok": bool(ok), "failures": failures,
                 "checked_at": stamp, "last_success": stamp if ok else old.get("last_success"),
                 "retry_at": stamp + delay if delay else 0, "reason": "validated" if ok else reason}
        health[key] = value
        db._set_state(conn, uid, _HEALTH_KEY, health)
        if bool(old.get("retry_at")) != bool(value["retry_at"]) or (ok and not old.get("ok")):
            _event(db, conn, uid, {"at": stamp, "action": "recovered" if ok else "cooldown",
                                  "source": source, "kind": kind, "reason": value["reason"]})
    return value


def ordered_sources(kind, chain, *, user_id=None, now=None):
    """Prefer recently proven adapters; expired quarantines get one retry first.

    Only reorders the configured allowlist. It cannot enable an unknown provider,
    install a package, or mutate a user's credentials/configuration.
    """
    if kind not in _KINDS:
        raise ValueError("Unknown source capability")
    uid, stamp = _uid(user_id), time.time() if now is None else float(now)
    db = ResearchStore()
    key = f"source_autopilot:route:{kind}"
    with db.connection(write=True) as conn:
        health = _read(conn, uid, _HEALTH_KEY, {})
        available, claimed = [], False
        for idx, source in enumerate(dict.fromkeys(chain)):
            if source not in _SOURCES:
                continue
            state = health.get(f"{kind}:{source}", {})
            retry = state.get("retry_at", 0)
            if retry > stamp or state.get("probe_until", 0) > stamp:
                continue
            if retry:
                # Atomically allow only one half-open request after cooldown.
                state["probe_until"] = stamp + 60
                health[f"{kind}:{source}"] = state
                claimed = True
                rank = 0
            else:
                rank = 1 if state.get("ok") and stamp - state.get("last_success", 0) <= 86400 else 2
            available.append((rank, idx, source))
        out = [source for _, _, source in sorted(available)]
        if claimed:
            db._set_state(conn, uid, _HEALTH_KEY, health)
        previous = _read(conn, uid, key, None)
        if previous != out:
            db._set_state(conn, uid, key, out)
            _event(db, conn, uid, {"at": stamp, "action": "route_updated", "source": kind,
                                  "reason": "health_evidence", "before": previous, "after": out})
    return out


def validate_kline_bundle(bundle):
    """A successful HTTP response is insufficient evidence of adapter health."""
    rows = bundle.get("candles") if isinstance(bundle, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("行情返回空日线")
    previous = -math.inf
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("行情行格式无效")
        try:
            stamp, o, h, l, c, v = (float(row[k]) for k in ("t", "o", "h", "l", "c", "v"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("行情字段缺失") from exc
        if not all(math.isfinite(x) for x in (stamp, o, h, l, c, v)) or stamp <= previous:
            raise ValueError("行情时间重复、倒序或数值无效")
        if min(o, h, l, c) <= 0 or v < 0 or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError("行情价格或成交量无效")
        previous = stamp
    return len(rows)


def _trusted_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.port not in {None, 80, 443}
            or not any(host == root or host.endswith("." + root) for root in _ROOTS)):
        raise ValueError("自动来源必须属于内置信任站点")
    return url


async def _public_dns(url):
    parsed = urlparse(_trusted_url(url))
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, parsed.hostname,
                              parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM),
            timeout=8,
        )
    except TimeoutError:
        raise TimeoutError("source_dns_timeout") from None
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("自动来源不能访问本地网络")


async def _fetch_document(client, url):
    # Validate every redirect before requesting it; no cookies/API keys/proxies.
    for _ in range(4):
        await _public_dns(url)
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                url = _trusted_url(urljoin(url, response.headers.get("location", "")))
                continue
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1_000_000:
                    raise ValueError("来源文档超过大小限制")
            return bytes(content), url
    raise ValueError("来源重定向过多")


async def fetch_discovered_news_document(url):
    """Use the same trust, redirect and size limits on every later RSS fetch."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), trust_env=False,
                                 headers={"User-Agent": "StockViewAgent-SourceCheck/1.0"}) as client:
        document, _ = await _fetch_document(client, url)
    validate_feed(document)
    return document


class _Feeds(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if (tag == "link" and "alternate" in attrs.get("rel", "").lower().split()
                and attrs.get("type", "").lower() in {"application/rss+xml", "application/atom+xml"}):
            self.links.append(attrs.get("href", ""))


def discover_feed_links(document, homepage):
    parser = _Feeds()
    parser.feed(document.decode("utf-8", errors="replace"))
    links = []
    for href in parser.links[:20]:
        try:
            candidate = _trusted_url(urljoin(homepage, href))
        except ValueError:
            continue
        if candidate not in links:
            links.append(candidate)
    return links[:3]


def validate_feed(document, *, now=None):
    stamp = time.time() if now is None else float(now)
    parsed = feedparser.parse(document)
    entries = parsed.get("entries", [])
    valid, fresh = 0, False
    for entry in entries[:100]:
        if not str(entry.get("title", "")).strip():
            continue
        try:
            _trusted_url(str(entry.get("link", "")))
        except ValueError:
            continue
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            age = stamp - calendar.timegm(published)
            fresh = fresh or -86400 <= age <= 30 * 86400
        valid += 1
    if valid < 2 or not fresh:
        raise ValueError("RSS缺少足够条目或近期发布时间")
    return valid


def discovered_news_sources():
    rows = ResearchStore().get_state(0, _NEWS_KEY, {}).get("active", [])
    # Revalidate stored definitions too: state is not executable authority.
    out = []
    for row in rows[:12]:
        try:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k].strip()
                    for k in ("id", "label", "url", "referer")):
                continue
            _trusted_url(row["url"])
            _trusted_url(row["referer"])
        except (ValueError, KeyError, TypeError):
            continue
        out.append({"id": row["id"], "kind": "rss", "label": row["label"],
                    "url": row["url"], "referer": row["referer"], "priority": 30})
    return out


async def _maintain_news(now=None):
    db, stamp = ResearchStore(), time.time() if now is None else float(now)
    # Shared news across users: prevent concurrent workers/tasks probing twice.
    claim_key = "source_autopilot:news_lease"
    claim = {"token": str(uuid.uuid4()), "until": stamp + 900}
    with db.connection(write=True) as conn:
        old = _read(conn, 0, _NEWS_KEY, {})
        if stamp - old.get("last_check", 0) < 86400:
            return {"status": "recently_checked", "active_count": len(old.get("active", []))}
        lease = _read(conn, 0, claim_key, None)
        if lease and lease.get("until", 0) > stamp:
            return {"status": "already_running"}
        db._set_state(conn, 0, claim_key, claim)
    try:
        active, changes = [], []
        async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), trust_env=False,
                                     headers={"User-Agent": "StockViewAgent-SourceCheck/1.0"}) as client:
            # Previously verified feeds remain until three daily validation failures.
            for row in old.get("active", [])[:12]:
                updated = dict(row)
                try:
                    document, url = await _fetch_document(client, row["url"])
                    count = validate_feed(document, now=stamp)
                    updated.update(url=url, failures=0, last_success=stamp, entries=count)
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    updated["failures"] = int(row.get("failures", 0)) + 1
                    updated["last_failure_reason"] = "dns_timeout" if isinstance(exc, TimeoutError) else "validation_failed"
                    updated["last_failure_at"] = stamp
                if updated.get("failures", 0) < 3:
                    active.append(updated)
                else:
                    changes.append({"action": "news_quarantined", "source": row["id"], "reason": "three_failed_checks"})
            known = {r["url"] for r in active}
            for homepage in _HOMES:
                if len(active) >= 12:
                    break
                try:
                    document, actual_home = await _fetch_document(client, homepage)
                    links = discover_feed_links(document, actual_home)
                except (httpx.HTTPError, ValueError, OSError):
                    continue
                for link in links:
                    if link in known or len(active) >= 12:
                        continue
                    try:
                        document, url = await _fetch_document(client, link)
                        count = validate_feed(document, now=stamp)
                    except (httpx.HTTPError, ValueError, OSError):
                        continue
                    if url in known:
                        continue
                    sid = "auto_rss_" + content_hash(url)[:12]
                    definition = {"id": sid, "url": url, "referer": actual_home,
                                  "label": f"自动发现 · {urlparse(url).hostname}", "failures": 0,
                                  "last_success": stamp, "entries": count}
                    db.put_entity(0, "source_definition", {**definition, "origin": "official_feed_link",
                                   "evidence_hash": content_hash(document.decode("utf-8", errors="replace")),
                                   "version": VERSION}, content_hash(definition))
                    active.append(definition)
                    known.add(url)
                    changes.append({"action": "news_added", "source": sid, "reason": "official_link_and_validated_feed", "url": url})
        with db.connection(write=True) as conn:
            # A slow old refresh may outlive its lease while a newer one succeeds.
            # It must not replace that newer registry or append misleading events.
            if _read(conn, 0, claim_key, None) != claim:
                return {"status": "lease_lost"}
            db._set_state(conn, 0, _NEWS_KEY, {"active": active, "last_check": stamp, "version": VERSION})
            for change in changes:
                _event(db, conn, 0, {"at": stamp, **change})
        return {"status": "checked", "active_count": len(active), "changes": len(changes)}
    finally:
        db.compare_and_set_state(0, claim_key, claim, None)


async def _probe_market(source):
    from app.services import eastmoney_market, market_extra_http
    if source == "eastmoney":
        return await eastmoney_market.fetch_kline_bundle("600519.SS", "1mo", "1d")
    fn = {"tencent": market_extra_http.fetch_kline_bundle_secondary_sync,
          "baidu": market_extra_http.fetch_kline_bundle_baidu_sync,
          "sina": market_extra_http.fetch_kline_bundle_sina_sync}[source]
    return await asyncio.to_thread(fn, "600519.SS", "1mo", "1d")


async def maintain(user_id):
    """Two small provider canaries plus daily trusted news-link discovery."""
    uid, stamp = _uid(user_id), time.time()
    db = ResearchStore()
    claim_key, done_key = "source_autopilot:maintenance_lease", "source_autopilot:maintenance_at"
    claim = {"token": str(uuid.uuid4()), "until": stamp + 1200}
    skip = None
    with db.connection(write=True) as conn:
        if stamp - _read(conn, uid, done_key, 0) < 6 * 3600:
            skip = "recently_checked"
        else:
            lease = _read(conn, uid, claim_key, None)
            if lease and lease.get("until", 0) > stamp:
                skip = "already_running"
            else:
                db._set_state(conn, uid, claim_key, claim)
    if skip:
        return {"status": skip, "market_probes": [], "summary": summary(uid)}
    token = current_user_id.set(uid)
    try:
        result = await _maintain_claimed(uid, stamp)
        with db.connection(write=True) as conn:
            if _read(conn, uid, claim_key, None) == claim:
                db._set_state(conn, uid, done_key, stamp)
        return result
    finally:
        current_user_id.reset(token)
        db.compare_and_set_state(uid, claim_key, claim, None)


async def _maintain_claimed(uid, stamp):
    health = ResearchStore().get_state(uid, _HEALTH_KEY, {})
    # Choose least recently checked, so daily probes eventually cover all adapters.
    candidates = sorted(_PUBLIC_PROBES, key=lambda s: health.get(f"daily:{s}", {}).get("checked_at", 0))
    checked = []
    for source in candidates:
        state = health.get(f"daily:{source}", {})
        if stamp - state.get("checked_at", 0) < 6 * 3600:
            continue
        try:
            bundle = await asyncio.wait_for(_probe_market(source), timeout=25)
            validate_kline_bundle(bundle)
            record_outcome("daily", source, ok=True, user_id=uid)
        except Exception:
            # CancelledError is a BaseException and must propagate for shutdown.
            record_outcome("daily", source, ok=False, user_id=uid, reason="canary_failed")
        checked.append(source)
        if len(checked) >= 2:
            break
    news = await _maintain_news()
    return {"status": "checked", "market_probes": checked, "news": news, "summary": summary(uid)}


def summary(user_id):
    db, uid, stamp = ResearchStore(), _uid(user_id), time.time()
    health = db.get_state(uid, _HEALTH_KEY, {})
    sources = [{**value, "status": "cooldown" if value.get("retry_at", 0) > stamp else
                "healthy" if value.get("ok") else "retrying"} for value in health.values()]
    changes = db.list_entities(uid, "source_change", 10) + db.list_entities(0, "source_change", 5)
    changes.sort(key=lambda row: row["created_at"], reverse=True)
    news = db.get_state(0, _NEWS_KEY, {})
    return {"version": VERSION, "status": "learning" if not sources else
            "healthy" if any(s["status"] == "healthy" for s in sources) else "degraded",
            "daily_chain": db.get_state(uid, "source_autopilot:route:daily", []),
            "sources": sources, "recent_changes": [row["payload"] for row in changes[:10]],
            "news": {"active_count": len(news.get("active", [])), "last_check": news.get("last_check")}}
