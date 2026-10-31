import asyncio
import json
import time
from dataclasses import replace
from email.utils import formatdate

import httpx
import pytest

from app.config import settings
from app.services import overseas_news as news
from app.storage import overseas_news_store as store


def rss(title="Market update", link="https://www.ft.com/content/article-1", stamp=None, summary="A short summary"):
    stamp = time.time() if stamp is None else stamp
    return (f'<rss version="2.0"><channel><title>Markets</title><item><title>{title}</title>'
            f'<link>{link}</link><description><![CDATA[{summary}]]></description>'
            f'<pubDate>{formatdate(stamp, usegmt=True)}</pubDate></item></channel></rss>').encode()


@pytest.fixture
def one_source(monkeypatch):
    source = replace(next(source for source in news.SOURCES if source.id == "ft"), discovery_url="")
    monkeypatch.setattr(news, "SOURCES", (source,))
    monkeypatch.setattr(settings, "overseas_news_enabled", True)
    monkeypatch.setattr(settings, "overseas_news_proxy_urls", "")
    monkeypatch.setattr(settings, "overseas_news_benzinga_api_key", "")
    monkeypatch.setattr("app.storage.news_store.upsert_many", lambda rows: len(rows))
    return source


def client_for(monkeypatch, handler):
    monkeypatch.setattr(news, "_new_client", lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_store_restarts_updates_and_filters(one_source):
    item = news._parse_rss(rss(summary="100% market growth"), one_source, time.time())[0]
    store.upsert_many([item, item])
    original_id = item["id"]
    item.update(title="Corrected headline", fetched_at=time.time() + 1)
    store.upsert_many([item])
    store.init_db()  # Every API opens a new connection, as a restarted worker does.
    result = store.list_feed(source="ft", query="%")
    assert result["total"] == 1
    assert result["items"][0]["id"] == original_id
    assert result["items"][0]["title"] == "Corrected headline"
    assert store.list_feed(source="reuters")["total"] == 0
    assert store.list_feed(query="missing")["total"] == 0
    assert store.list_feed(limit=1, offset=1)["items"] == []


def test_lease_exclusion_expiry_and_owner_safe_release():
    assert store.acquire_lease("poll", "worker-a", 100, now=100)
    assert not store.acquire_lease("poll", "worker-b", 100, now=101)
    store.release_lease("poll", "worker-b")
    assert not store.acquire_lease("poll", "worker-b", 100, now=102)
    assert store.acquire_lease("poll", "worker-b", 100, now=201)
    store.release_lease("poll", "worker-a")
    assert not store.acquire_lease("poll", "worker-c", 100, now=202)


def test_collect_conditional_headers_dedup_and_disabled(one_source, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, content=rss(), headers={"etag": '"version1"', "last-modified": "Fri, 30 Oct 2026 12:00:00 GMT"})
        assert request.headers["if-none-match"] == '"version1"'
        assert request.headers["if-modified-since"] == "Fri, 30 Oct 2026 12:00:00 GMT"
        return httpx.Response(304)

    client_for(monkeypatch, handler)
    first = asyncio.run(news.collect_once())
    assert first["items_received"] == 1
    assert news.list_feed()["total"] == 1
    assert news.status()["sources"][0]["status"] == "ok"
    asyncio.run(news.collect_once())
    assert len(calls) == 1  # Persisted schedule survives new loops/workers.
    second = asyncio.run(news.collect_once(force=True))
    assert second["sources"][0]["not_modified"]
    assert news.list_feed()["total"] == 1
    monkeypatch.setattr(settings, "overseas_news_enabled", False)
    assert asyncio.run(news.collect_once(force=True))["skipped"] == "disabled"
    assert news.status()["sources"][0]["status"] == "disabled"
    assert len(calls) == 2


@pytest.mark.parametrize("code,expected", [(429, "rate_limited"), (403, "access_denied"), (401, "access_denied")])
def test_denial_does_not_rotate_or_override_cooldown(one_source, monkeypatch, code, expected):
    proxies, requests = [], []
    monkeypatch.setattr(settings, "overseas_news_proxy_urls", "http://proxy-a.local:8080,http://proxy-b.local:8080")
    monkeypatch.setattr(news, "_proxy_index", 0)

    def handler(request):
        requests.append(request)
        return httpx.Response(code, headers={"retry-after": "7200"})

    def factory(proxy):
        proxies.append(proxy)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(news, "_new_client", factory)
    asyncio.run(news.collect_once(force=True))
    state = news.status()["sources"][0]
    assert state["status"] == expected
    assert state["next_poll_at"] > time.time() + 7100
    assert len(requests) == 1
    asyncio.run(news.collect_once(force=True))
    assert len(requests) == 1
    assert proxies == ["http://proxy-a.local:8080"]


def test_transport_failure_recreates_client_and_rotates_authorized_proxy(one_source, monkeypatch):
    proxies = []
    monkeypatch.setattr(settings, "overseas_news_proxy_urls", "http://proxy-a.local:8080,http://proxy-b.local:8080")
    monkeypatch.setattr(news, "_proxy_index", 0)

    def factory(proxy):
        proxies.append(proxy)

        def handler(request):
            if proxy.endswith("a.local:8080"):
                raise httpx.ConnectError("sensitive connection details", request=request)
            return httpx.Response(200, content=rss())

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(news, "_new_client", factory)
    assert asyncio.run(news.collect_once())["items_received"] == 1
    assert proxies == ["http://proxy-a.local:8080", "http://proxy-b.local:8080"]


def test_html_and_failures_preserve_archive_and_choose_alternative(one_source, monkeypatch):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.path == "/markets":
            return httpx.Response(200, text="<html><body>Sign in</body></html>")
        return httpx.Response(200, content=rss())

    client_for(monkeypatch, handler)
    assert asyncio.run(news.collect_once())["items_received"] == 1
    assert len(requests) == 2
    assert news.status()["sources"][0]["active_url"] == "https://www.ft.com/rss/home"
    client_for(monkeypatch, lambda request: httpx.Response(503))
    asyncio.run(news.collect_once(force=True))
    assert news.list_feed()["total"] == 1
    assert news.status()["sources"][0]["status"] == "error"
    assert news.status()["sources"][0]["last_success_at"]


def test_stale_content_is_visible_and_not_reported_as_current(one_source, monkeypatch):
    client_for(monkeypatch, lambda request: httpx.Response(200, content=rss(stamp=time.time() - 5 * 86400)))
    asyncio.run(news.collect_once())
    state = news.status()["sources"][0]
    assert state["content_stale"]
    assert state["status"] == "stale"
    assert state["newest_published_at"] < time.time() - 72 * 3600
    assert news.list_feed()["total"] == 1


@pytest.mark.parametrize("link", ["javascript:alert(1)", "http://www.ft.com/story", "https://127.0.0.1/private",
                                  "https://www.ft.com.evil.test/story", "https://user:pass@www.ft.com/story",
                                  "https://www.ft.com:8443/story"])
def test_rejects_unsafe_article_links(one_source, link):
    with pytest.raises(ValueError, match="usable publisher"):
        news._parse_rss(rss(link=link), one_source, time.time())


def test_rss_snippets_bounded_utc_and_sanitized(one_source):
    raw = rss(link="https://www.ft.com/content/a?utm_source=tracker#fragment", summary="<p>" + "word " * 500 + "</p>")
    item = news._parse_rss(raw, one_source, time.time())[0]
    assert item["url"] == "https://www.ft.com/content/a"
    assert len(item["summary"]) <= 320
    assert len(item["summary"].split()) <= 60
    assert abs(item["published_ts"] - time.time()) < 2
    assert "<" not in item["summary"]


def test_response_limit_redirect_guard_and_xml_rejection(one_source, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data"})

    client_for(monkeypatch, handler)
    with pytest.raises(news.FetchError, match="Untrusted"):
        asyncio.run(news._fetch(one_source, one_source.urls[0]))
    assert len(requests) == 1
    client_for(monkeypatch, lambda request: httpx.Response(200, content=b"x" * (news.MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(news.FetchError, match="2 MiB"):
        asyncio.run(news._fetch(one_source, one_source.urls[0]))
    with pytest.raises(ValueError, match="Unsafe"):
        news._parse_rss(b'<!DOCTYPE rss [<!ENTITY a "boom">]><rss/>', one_source, time.time())


def test_daily_discovery_only_adds_trusted_feed_urls(one_source, monkeypatch):
    source = replace(one_source, discovery_url="https://www.ft.com/markets")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text='''<html><head>
          <link rel="alternate" type="application/rss+xml" href="https://evil.test/feed"/>
          <link rel="alternate" type="application/rss+xml" href="/new-feed?format=rss"/>
          </head></html>''')

    client_for(monkeypatch, handler)
    asyncio.run(news._maintain_source(source, {}))
    state = store.sources()["ft"]
    assert state["discovered_urls"] == ["https://www.ft.com/new-feed?format=rss"]
    asyncio.run(news._maintain_source(source, state))
    assert len(calls) == 1


def test_concurrent_cycles_share_persistent_lease(one_source, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def fake_poll(source, force=False):
            entered.set()
            await release.wait()
            return {"source_id": source.id, "items_received": 0}

        monkeypatch.setattr(news, "_poll_source", fake_poll)
        first = asyncio.create_task(news.collect_once())
        await entered.wait()
        assert (await news.collect_once(force=True))["skipped"] == "another_worker"
        assert news.status()["collecting"]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not store.lease_active(news.LEASE_NAME)

    asyncio.run(scenario())


def test_scheduler_independent_of_watchlist_scheduler_and_llm(one_source, monkeypatch):
    monkeypatch.setattr(settings, "enable_scheduler", False)
    monkeypatch.setattr(settings, "openai_api_key", "")

    async def scenario():
        called = asyncio.Event()

        async def collect(force=False):
            called.set()
            return {}

        monkeypatch.setattr(news, "collect_once", collect)
        task = asyncio.create_task(news.scheduler_loop())
        await asyncio.wait_for(called.wait(), 1)
        assert news.status()["running"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_benzinga_drains_delta_pages_before_advancing_watermark(one_source, monkeypatch):
    source = news.Source("benzinga", "Benzinga", ("https://www.benzinga.com/feed",),
                         ("www.benzinga.com", "api.benzinga.com"), ("benzinga.com",))
    monkeypatch.setattr(news, "SOURCES", (source,))
    monkeypatch.setattr(settings, "overseas_news_benzinga_api_key", "secret-key-do-not-log")
    watermark = time.time() - 3600
    store.update_source("benzinga", api_watermark=watermark, transport="benzinga_api")
    requested = []

    def handler(request):
        params = request.url.params
        assert params["token"] == "secret-key-do-not-log"
        assert params["displayOutput"] == "abstract"
        assert params["sort"] == "updated:asc"
        assert int(params["updatedSince"]) == int(watermark - 600)
        page = int(params["page"])
        requested.append(page)
        entries = [{"title": f"News {page}-{idx}", "url": f"https://www.benzinga.com/news/{page}-{idx}",
                    "teaser": "Publisher summary", "created": formatdate(time.time(), usegmt=True)}
                   for idx in range(100 if page < 5 else 1)]
        return httpx.Response(200, json=entries)

    client_for(monkeypatch, handler)
    first = asyncio.run(news.collect_once())
    assert first["items_received"] == 500
    state = store.sources()["benzinga"]
    assert state["api_watermark"] == watermark
    assert state["api_pending_page"] == 5
    asyncio.run(news.collect_once(force=True))
    state = store.sources()["benzinga"]
    assert state["api_watermark"] > watermark
    assert state["api_pending_page"] is None
    assert requested == [0, 1, 2, 3, 4, 5]
    assert news.list_feed()["total"] == 501
    assert "secret-key" not in json.dumps(news.status())


def test_stale_endpoint_falls_back_to_current_feed(one_source, monkeypatch):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        stamp = time.time() - 5 * 86400 if request.url.path == "/markets" else time.time()
        return httpx.Response(200, content=rss(stamp=stamp))

    client_for(monkeypatch, handler)
    asyncio.run(news.collect_once())
    state = news.status()["sources"][0]
    assert requested == ["/markets", "/rss/home"]
    assert state["status"] == "ok"
    assert state["active_url"] == "https://www.ft.com/rss/home"
    assert not state["content_stale"]


def test_archive_outbox_retries_after_source_304(one_source, monkeypatch):
    archived = []
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(304) if len(requests) > 1 else httpx.Response(200, content=rss(), headers={"etag": '"v1"'})

    def broken(rows):
        raise RuntimeError("archive temporarily unavailable")

    monkeypatch.setattr("app.storage.news_store.upsert_many", broken)
    client_for(monkeypatch, handler)
    asyncio.run(news.collect_once())
    assert news.list_feed()["total"] == 1
    assert store.archive_pending_count() == 1
    assert news.status()["archive_sync_error"]
    monkeypatch.setattr("app.storage.news_store.upsert_many", lambda rows: archived.extend(rows))
    asyncio.run(news.collect_once(force=True))
    assert len(archived) == 1
    assert store.archive_pending_count() == 0
    assert news.status()["archive_sync_error"] is None


def test_cancelled_mirror_drains_before_releasing_lease(one_source, monkeypatch):
    import threading

    started, release = threading.Event(), threading.Event()

    def slow_archive(rows):
        started.set()
        assert release.wait(3)

    monkeypatch.setattr("app.storage.news_store.upsert_many", slow_archive)
    client_for(monkeypatch, lambda request: httpx.Response(200, content=rss()))

    async def scenario():
        task = asyncio.create_task(news.collect_once())
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        assert store.lease_active(news.LEASE_NAME)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert store.lease_active(news.LEASE_NAME)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not store.lease_active(news.LEASE_NAME)
        assert not store.archive_pending_count()

    asyncio.run(scenario())


def test_discovery_denial_manual_force_cannot_bypass(one_source, monkeypatch):
    source = replace(one_source, discovery_url="https://www.ft.com/discover")
    monkeypatch.setattr(news, "SOURCES", (source,))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "900"}) if request.url.path == "/discover" else httpx.Response(200, content=rss())

    client_for(monkeypatch, handler)
    asyncio.run(news.collect_once())
    assert len(requests) == 2
    assert store.sources()["ft"]["cooldown_reason"] == 429
    asyncio.run(news.collect_once(force=True))
    assert len(requests) == 2


def test_benzinga_query_token_redacted_from_httpx_logs(one_source, monkeypatch, caplog):
    import logging

    source = news.Source("benzinga", "Benzinga", (), ("api.benzinga.com",), ("benzinga.com",))
    client_for(monkeypatch, lambda request: httpx.Response(200, json=[]))
    with caplog.at_level(logging.INFO, logger="httpx"):
        asyncio.run(news._fetch(source, "https://api.benzinga.com/api/v2/news", params={"token": "fake-secret-sentinel"}))
    assert "fake-secret-sentinel" not in caplog.text
    assert "REDACTED" in caplog.text


def test_saved_pause_resumes_without_restart_and_environment_stays_authoritative(one_source, monkeypatch):
    from app.services import data_collection
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=rss())
    client_for(monkeypatch, handler)
    data_collection.update_config({'overseas_enabled': False})
    assert asyncio.run(news.collect_once())['skipped'] == 'disabled'
    assert news.status()['reason'] == 'automatic_disabled'
    assert not calls
    data_collection.update_config({'overseas_enabled': True, 'overseas_poll_interval_sec': 300})
    assert asyncio.run(news.collect_once())['items_received'] == 1
    assert news.status()['poll_interval_sec'] == 300
    monkeypatch.setattr(settings, 'overseas_news_enabled', False)
    assert asyncio.run(news.collect_once(force=True))['skipped'] == 'disabled'
    assert news.status()['reason'] == 'service_disabled'
    assert len(calls) == 1


def test_memory_pressure_defers_without_network_or_claiming_lease(one_source, monkeypatch):
    from app.services import collector_resources
    def forbidden(*args, **kwargs):
        raise AssertionError('resource pressure must defer before network and lease')
    monkeypatch.setattr(collector_resources, 'snapshot', lambda limit: {
        'rss_mb': 900, 'limit_mb': limit, 'pressure': True, 'reason': 'high_memory',
        'supported': True, 'scope': 'current_process'})
    monkeypatch.setattr(news, '_new_client', forbidden)
    monkeypatch.setattr(store, 'acquire_lease', forbidden)
    result = asyncio.run(news.collect_once(force=True))
    assert result['skipped'] == 'high_memory'
    assert result['items_received'] == 0
    assert store.sources() == {}
    assert news.status()['reason'] == 'high_memory'


def test_resource_probe_failure_drains_sibling_before_releasing_lease(one_source, monkeypatch):
    import threading
    from app.services import data_collection
    data_collection.update_config({'max_concurrent_jobs': 2})
    monkeypatch.setattr(news, 'SOURCES', (one_source, replace(one_source, id='second')))
    original = news._resource_state
    entered = threading.Event()
    calls = 0
    lock = threading.Lock()
    ownership_at_cancel = []
    def resources():
        nonlocal calls
        with lock:
            calls += 1
            current = calls
        if current == 3:
            assert entered.wait(2)
            raise OSError('resource probe failed')
        return original()
    async def poll(source, *, force):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            ownership_at_cancel.append(store.lease_active(news.LEASE_NAME))
    monkeypatch.setattr(news, '_resource_state', resources)
    monkeypatch.setattr(news, '_poll_source', poll)
    async def scenario():
        with pytest.raises(OSError, match='resource probe'):
            await news.collect_once()
        assert ownership_at_cancel == [True]
        assert not store.lease_active(news.LEASE_NAME)
    asyncio.run(scenario())
