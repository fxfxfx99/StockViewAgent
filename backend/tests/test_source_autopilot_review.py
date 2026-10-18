"""Regression checks for source refresh publication and ordinary fetch paths."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import formatdate
import json
import time

import httpx
import pytest

from app.config import settings
from app.security.user_context import current_user_id
from app.services import source_autopilot as svc
from app.storage.research_store import ResearchStore


def bundle():
    return {"candles": [{"t": 1700000000, "o": 10, "h": 12, "l": 9, "c": 11, "v": 1000}]}


def feed(now, extra=""):
    date = formatdate(now - 3600, usegmt=True)
    items = "".join(f'<item><title>{s}</title><link>https://finance.sina.com.cn/{s}</link>'
                    f'<pubDate>{date}</pubDate></item>' for s in ("a", "b"))
    return f'<rss version="2.0"><channel><title>Finance</title>{items}{extra}</channel></rss>'.encode()


def test_expired_source_cooldown_allows_one_concurrent_probe():
    for _ in range(3):
        state = svc.record_outcome("daily", "eastmoney", ok=False, user_id=4, now=10000)
    retry_at = state["retry_at"]
    def route(_):
        return svc.ordered_sources("daily", ["eastmoney", "tencent"], user_id=4, now=retry_at)
    with ThreadPoolExecutor(max_workers=4) as pool:
        routes = list(pool.map(route, range(4)))
    assert sum("eastmoney" in route for route in routes) == 1
    assert all("tencent" in route for route in routes)
    assert svc.ordered_sources("minute", ["eastmoney"], user_id=4, now=retry_at) == ["eastmoney"]
    assert svc.ordered_sources("daily", ["eastmoney"], user_id=5, now=retry_at) == ["eastmoney"]
    svc.record_outcome("daily", "eastmoney", ok=True, user_id=4, now=retry_at + 1)
    assert svc.ordered_sources("daily", ["eastmoney"], user_id=4, now=retry_at + 2) == ["eastmoney"]


def test_pipeline_records_success_only_after_payload_validation(monkeypatch):
    from app.services import kline_pipeline as pipeline
    monkeypatch.setattr(pipeline.market_source_policy, "kline_daily_chain", lambda: ["eastmoney", "tencent"])
    events = []
    monkeypatch.setattr(pipeline, "_record_source_health", lambda *args: events.append(args))
    async def empty(*args):
        return {"candles": []}
    async def enrich(out, *args):
        return out
    monkeypatch.setattr(pipeline.eastmoney_market, "fetch_kline_bundle", empty)
    monkeypatch.setattr(pipeline.market_extra_http, "fetch_kline_bundle_secondary_sync", lambda *args: bundle())
    monkeypatch.setattr(pipeline, "_attach_source_and_enrich", enrich)
    asyncio.run(pipeline.fetch_a_share_kline_with_fallbacks("600519.SS", "1mo", "1d"))
    assert events == [("eastmoney_kline", False, "invalid_payload"), ("tencent_kline_fallback", True)]


def test_pipeline_never_persists_or_reraises_sensitive_provider_detail(monkeypatch):
    from app.services import kline_pipeline as pipeline
    from app.storage import market_source_cache
    monkeypatch.setattr(pipeline.market_source_policy, "kline_daily_chain", lambda: ["eastmoney"])
    async def failed(*args):
        raise RuntimeError("https://provider.test/?token=SECRET-KEY private auth failure")
    monkeypatch.setattr(pipeline.eastmoney_market, "fetch_kline_bundle", failed)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(pipeline.fetch_a_share_kline_with_fallbacks("600519.SS", "1mo", "1d"))
    assert str(exc.value) == "eastmoney: request_failed"
    assert "SECRET" not in json.dumps(market_source_cache.load_health())
    assert "SECRET" not in json.dumps(svc.summary(1))


def old_registry(stamp):
    return {"version": svc.VERSION, "last_check": stamp - 86401,
            "active": [{"id": "auto_rss_existing", "label": "Existing",
                        "url": "https://finance.sina.com.cn/old.xml",
                        "referer": "https://finance.sina.com.cn/", "failures": 0}]}


def test_interrupted_rss_refresh_keeps_previous_registry_atomically(monkeypatch):
    db, stamp = ResearchStore(), time.time()
    old = old_registry(stamp)
    db.set_state(0, svc._NEWS_KEY, old)
    monkeypatch.setattr(svc, "_HOMES", ("https://finance.sina.com.cn/",))
    async def fetch(client, url):
        if url.endswith("old.xml"):
            return feed(stamp), url
        raise RuntimeError("interrupted after old feed was refreshed")
    monkeypatch.setattr(svc, "_fetch_document", fetch)
    with pytest.raises(RuntimeError):
        asyncio.run(svc._maintain_news(stamp))
    assert db.get_state(0, svc._NEWS_KEY) == old
    assert db.get_state(0, "source_autopilot:news_lease") is None
    assert db.list_entities(0, "source_change") == []


def test_dns_has_its_own_deadline_and_timeout_preserves_existing_feed(monkeypatch):
    db, stamp = ResearchStore(), time.time()
    old = old_registry(stamp)
    db.set_state(0, svc._NEWS_KEY, old)
    monkeypatch.setattr(svc, "_HOMES", ())
    wait_for = asyncio.wait_for
    deadlines = []
    async def stuck_dns(*args, **kwargs):
        await asyncio.Event().wait()
    async def bounded(awaitable, timeout):
        deadlines.append(timeout)
        return await wait_for(awaitable, timeout=0.001)
    async def fetch(client, url):
        await svc._public_dns(url)
        pytest.fail("Timed out DNS must prevent the HTTP request")
    monkeypatch.setattr(svc.asyncio, "to_thread", stuck_dns)
    monkeypatch.setattr(svc.asyncio, "wait_for", bounded)
    monkeypatch.setattr(svc, "_fetch_document", fetch)
    assert asyncio.run(svc._maintain_news(stamp))["active_count"] == 1
    saved = db.get_state(0, svc._NEWS_KEY)["active"][0]
    assert deadlines == [8]
    assert saved["url"] == old["active"][0]["url"]
    assert saved["failures"] == 1
    assert saved["last_failure_reason"] == "dns_timeout"
    assert saved["last_failure_at"] == stamp
    assert db.get_state(0, "source_autopilot:news_lease") is None


def test_expired_rss_writer_cannot_overwrite_newer_success(monkeypatch):
    db, stamp = ResearchStore(), time.time()
    old = old_registry(stamp)
    db.set_state(0, svc._NEWS_KEY, old)
    newer = {**old, "last_check": stamp + 1000, "active": []}
    next_claim = {"token": "new-owner", "until": stamp + 1900}
    monkeypatch.setattr(svc, "_HOMES", ("https://finance.sina.com.cn/",))
    async def fetch(client, url):
        if url.endswith("old.xml"):
            return feed(stamp), url
        db.set_state(0, svc._NEWS_KEY, newer)
        db.set_state(0, "source_autopilot:news_lease", next_claim)
        return b"<html></html>", url
    monkeypatch.setattr(svc, "_fetch_document", fetch)
    assert asyncio.run(svc._maintain_news(stamp))["status"] == "lease_lost"
    assert db.get_state(0, svc._NEWS_KEY) == newer
    assert db.get_state(0, "source_autopilot:news_lease") == next_claim
    assert db.list_entities(0, "source_change") == []


def test_global_news_refresh_is_claimed_once_across_concurrent_tasks(monkeypatch):
    stamp = time.time()
    homepage = "https://finance.sina.com.cn/"
    monkeypatch.setattr(svc, "_HOMES", (homepage,))
    calls = []
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(client, url):
            calls.append(url)
            if url == homepage:
                entered.set()
                await release.wait()
                return b'<link rel="alternate" type="application/rss+xml" href="/new.xml">', url
            return feed(stamp), url
        monkeypatch.setattr(svc, "_fetch_document", fetch)
        first = asyncio.create_task(svc._maintain_news(stamp))
        await entered.wait()
        assert (await svc._maintain_news(stamp))["status"] == "already_running"
        release.set()
        assert (await first)["active_count"] == 1
        assert (await svc._maintain_news(stamp + 1))["status"] == "recently_checked"
    asyncio.run(run())
    assert calls == [homepage, homepage + "new.xml"]


def test_market_maintenance_coalesces_concurrent_and_repeated_calls(monkeypatch):
    calls = []
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def probe(source):
            calls.append((current_user_id.get(), source))
            entered.set()
            await release.wait()
            return bundle()
        async def news():
            return {"status": "checked", "active_count": 0}
        monkeypatch.setattr(svc, "_probe_market", probe)
        monkeypatch.setattr(svc, "_maintain_news", news)
        first = asyncio.create_task(svc.maintain(8))
        await entered.wait()
        assert (await svc.maintain(8))["status"] == "already_running"
        release.set()
        assert (await first)["status"] == "checked"
        assert (await svc.maintain(8))["status"] == "recently_checked"
        assert calls == [(8, "eastmoney"), (8, "tencent")]
        await svc.maintain(9)
        assert len(calls) == 4 and calls[-1][0] == 9
    token = current_user_id.set(99)
    try:
        asyncio.run(run())
        assert current_user_id.get() == 99
    finally:
        current_user_id.reset(token)


def test_invalid_canary_never_becomes_health_evidence(monkeypatch):
    async def invalid(source):
        if source == "eastmoney":
            return {"candles": []}
        raise RuntimeError("token=SECRET-KEY")
    async def news():
        return {"status": "checked", "active_count": 0}
    monkeypatch.setattr(svc, "_probe_market", invalid)
    monkeypatch.setattr(svc, "_maintain_news", news)
    summary = asyncio.run(svc.maintain(8))["summary"]
    assert summary["status"] == "degraded"
    assert all(row["last_success"] is None for row in summary["sources"])
    assert "SECRET" not in json.dumps(summary)


def test_automatic_rss_fetch_and_registry_probe_keep_redirect_boundary(monkeypatch):
    from app.services import cn_market_news as news, cn_headline_source_registry as registry, news_fetch_http
    calls = []
    async def unsafe(*args, **kwargs):
        pytest.fail("Automatic feed must not use the generic redirecting fetcher")
    async def blocked(client, url):
        calls.append(url)
        raise ValueError("redirect points outside trusted domains")
    monkeypatch.setattr(news_fetch_http, "get_with_retry", unsafe)
    monkeypatch.setattr(svc, "_fetch_document", blocked)
    async def run():
        async with httpx.AsyncClient() as client:
            assert await news._fetch_rss(client, "auto_rss_test", "Auto", "https://finance.sina.com.cn/rss") == []
            assert await registry._probe_rss(client, "https://finance.sina.com.cn/rss", "", automatic=True) == 0
    asyncio.run(run())
    assert len(calls) == 2


def test_automatic_rss_articles_use_the_same_trusted_link_filter(monkeypatch):
    from app.services import cn_market_news as news
    async def fetch(client, url):
        extra = '<item><title>Bad</title><link>javascript:alert(1)</link></item>'
        return feed(time.time(), extra), url
    monkeypatch.setattr(svc, "_fetch_document", fetch)
    async def run():
        async with httpx.AsyncClient() as client:
            return await news._fetch_rss(client, "auto_rss_test", "Auto", "https://finance.sina.com.cn/rss")
    assert [row["title"] for row in asyncio.run(run())] == ["a", "b"]


def test_corrupt_discovered_rows_do_not_break_the_registry():
    ResearchStore().set_state(0, svc._NEWS_KEY, {"active": [None, {},
        {"url": "https://finance.sina.com.cn/rss", "referer": "https://finance.sina.com.cn/"}]})
    assert svc.discovered_news_sources() == []


def test_first_queued_job_is_collection_before_optional_maintenance(monkeypatch):
    from app.services import local_autopilot as auto
    from app.services.market_time import SHANGHAI_TZ
    monkeypatch.setattr(settings, "enable_research_worker", True)
    result = auto.reconcile(91, now=datetime(2026, 10, 19, 17, tzinfo=SHANGHAI_TZ))
    assert result["queued_jobs"][0]["kind"] == "collect"
    assert ResearchStore().claim_job("test", user_id=91)["kind"] == "collect"
