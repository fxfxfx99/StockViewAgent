"""Bound network buffers, incremental archives, and shared collection ownership."""
import asyncio
import gzip
import subprocess
import sys
import threading
import time
import tracemalloc
import zlib

import httpx
import pytest

from app.config import settings
from app.services import collector_resources, news_feed_sync as sync, news_fetch_http as fetch, rss_fetcher as rss
from app.storage import news_store


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.reads, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_pressure(monkeypatch):
    monkeypatch.setattr(collector_resources, "snapshot", lambda *args, **kwargs: {"pressure": False})


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate", "raw_deflate"])
def test_streamed_http_retains_response_interface_and_bounded_decoding(encoding):
    payload = b'{"headline":"source content"}'
    data = payload if encoding == "identity" else gzip.compress(payload) if encoding == "gzip" else zlib.compress(payload)
    if encoding == "raw_deflate":
        data = zlib.compress(payload)[2:-4]
    stream = Stream([data])

    async def run():
        def reply(request):
            assert request.headers["accept-encoding"] == "gzip, deflate"
            return httpx.Response(200, headers={"content-encoding": "deflate" if encoding == "raw_deflate" else encoding,
                                               "content-type": "application/json", "etag": "version1"}, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            response = await fetch.get_with_retry(client, "https://example.org/feed")
        assert response.json() == {"headline": "source content"}
        assert response.headers["etag"] == "version1"
        assert str(response.url) == "https://example.org/feed" and stream.closed
    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 429, 500, 503])
def test_http_rejection_does_not_retry_or_read_error_body(status):
    stream, requests = Stream([b"private-provider-error"]), []

    async def run():
        def reply(request):
            requests.append(request)
            return httpx.Response(status, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            assert await fetch.fetch_bytes_with_retry(client, "https://example.org/feed") is None
        assert len(requests) == 1 and stream.reads == 0 and stream.closed
    asyncio.run(run())


@pytest.mark.parametrize("declared", [True, False])
def test_response_size_limit_aborts_without_retries(declared):
    chunk = b"x" * 65536
    stream, requests = Stream([chunk] * 40), []

    async def run():
        def reply(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-length": str(len(chunk) * 40)} if declared else {}, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            assert await fetch.fetch_bytes_with_retry(client, "https://example.org/feed") is None
        assert len(requests) == 1 and stream.closed
        assert stream.reads == (0 if declared else 33)
    asyncio.run(run())


def test_gzip_expansion_is_bounded_before_materializing_decoded_body():
    stream = Stream([gzip.compress(b"x" * (4 * 1024 * 1024))])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream))) as client:
            tracemalloc.start()
            try:
                assert await fetch.get_with_retry(client, "https://example.org/feed", max_response_bytes=65536) is None
                assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
            finally:
                tracemalloc.stop()
        assert stream.closed
    asyncio.run(run())


def test_cancel_closes_stream_without_retrying():
    async def run():
        entered = asyncio.Event()
        class WaitingStream(Stream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"unreachable"
        stream, requests = WaitingStream([]), []
        def reply(request):
            requests.append(request)
            return httpx.Response(200, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            task = asyncio.create_task(fetch.get_with_retry(client, "https://example.org/feed"))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stream.closed and len(requests) == 1
    asyncio.run(run())


def article(number):
    return {"id": f"test-{number}", "title": f"企业{number}公告", "link": f"https://example.org/article/{number}",
            "source": "Public fixture", "summary": f"资料编号{number}", "published_ts": time.time() - 60}


def mocked_rss(monkeypatch, *, stock_count=100):
    state = {"active": 0, "peak": 0, "calls": 0, "em_batches": [], "parsed": 0}

    async def body(client, url):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        state["calls"] += 1
        await asyncio.sleep(0)
        state["active"] -= 1
        return b"x" * 65536

    def parse(body, source, hint, **kwargs):
        state["parsed"] += 1
        return [article(state["parsed"])]

    async def eastmoney(symbols, **kwargs):
        state["em_batches"].append(len(symbols))
        return []

    monkeypatch.setattr(rss, "fetch_bytes_with_retry", body)
    monkeypatch.setattr(rss, "_parse_feed", parse)
    monkeypatch.setattr(rss.eastmoney_stock_news, "fetch_symbols_news_items", eastmoney)
    monkeypatch.setattr(rss.news_watchlist_matcher, "load_watchlist_match_hints", lambda: ([], {}))
    monkeypatch.setattr(settings, "news_feed_max_items", 80)
    return [f"{600000 + i}.SS" for i in range(stock_count)], state


def test_rss_small_concurrency_releases_raw_bodies_before_next_batch(monkeypatch):
    symbols, state = mocked_rss(monkeypatch)
    collected = []

    async def run():
        async def consume(rows):
            collected.extend(row["id"] for row in rows)
        tracemalloc.start()
        try:
            assert await rss.fetch_news_for_symbols(symbols, collector=consume) == []
            assert tracemalloc.get_traced_memory()[1] < 4 * 1024 * 1024
        finally:
            tracemalloc.stop()
    asyncio.run(run())
    assert state["calls"] == state["parsed"] == len(collected) == 204
    assert state["peak"] <= 2 and max(state["em_batches"]) <= 2


def empty_other_sources(monkeypatch):
    async def empty(**kwargs):
        return {"items": [], "network_items": [], "records_received": 0}
    monkeypatch.setattr(sync.cn_market_news, "fetch_cn_headlines", empty)
    monkeypatch.setattr(sync.china_tech_media_feeds, "fetch_tech_media_feeds", empty)


def test_archive_does_not_use_display_limit_and_cross_batch_duplicates_merge(monkeypatch):
    symbols, state = mocked_rss(monkeypatch, stock_count=45)
    empty_other_sources(monkeypatch)
    original = news_store.upsert_many
    sizes = []

    def write(items, **kwargs):
        sizes.append(len(items))
        return original(items, **kwargs)

    monkeypatch.setattr(news_store, "upsert_many", write)
    result = asyncio.run(sync.sync_all_sources_to_archive(symbols))
    assert result["status"] == "success" and result["merged_unique"] == 94
    assert result["records_received"] == result["records_added"] == 94
    assert news_store.stats()["stored_articles"] == 94 and max(sizes) <= 30
    # A second source/batch can refer to the same article under another id.
    async def duplicated(symbols, *, collector, collection_stats):
        row = article(1)
        await collector([row])
        await collector([{**row, "id": "another-feed-id"}])
        collection_stats["records_received"] = 2
        return []
    monkeypatch.setattr(rss, "fetch_news_for_symbols", duplicated)
    result = asyncio.run(sync.sync_all_sources_to_archive(symbols))
    assert result["merged_unique"] == 1 and news_store.stats()["stored_articles"] == 94


def test_display_mode_retains_only_requested_cap(monkeypatch):
    symbols, state = mocked_rss(monkeypatch, stock_count=45)
    rows = asyncio.run(rss.fetch_news_for_symbols(symbols))
    assert len(rows) == 80 and state["parsed"] == 94


def test_retained_feed_text_has_item_limits(monkeypatch):
    symbols, _ = mocked_rss(monkeypatch, stock_count=1)
    monkeypatch.setattr(rss, "_parse_feed", lambda *args, **kwargs: [{**article(1), "title": "长" * 5000,
                                                                   "summary": "文" * 10000}])
    rows = asyncio.run(rss.fetch_news_for_symbols(symbols))
    assert len(rows[0]["title"]) == 1000 and len(rows[0]["summary"]) == 2000


@pytest.mark.parametrize("cancel_count", [1, 2])
def test_sync_cancellation_keeps_process_lock_until_writer_finishes(monkeypatch, cancel_count):
    empty_other_sources(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = news_store.upsert_many
    calls = []

    def write(items, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(items, **kwargs)

    async def source(symbols, *, collector, collection_stats):
        calls.append(1)
        await collector([article(1)])
        collection_stats["records_received"] = 1
        return []

    monkeypatch.setattr(news_store, "upsert_many", write)
    monkeypatch.setattr(rss, "fetch_news_for_symbols", source)

    async def run():
        task = asyncio.create_task(sync.sync_all_sources_to_archive([]))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
            second = await sync.sync_all_sources_to_archive([])
            assert second["status"] == "skipped" and second["skipped"] == "already_running"
            assert not task.done() and len(calls) == 1
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        with sync._collection_lock() as held:
            assert held
        assert news_store.stats()["stored_articles"] == 1
    asyncio.run(run())


def test_collection_lock_excludes_other_process(tmp_path):
    with sync._collection_lock() as held:
        assert held
        script = """import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDWR)
try:
 fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:
 sys.exit(0)
sys.exit(1)
"""
        process = subprocess.run([sys.executable, "-c", script, str(tmp_path / "news-feed-sync.lock")],
                                 capture_output=True, text=True, timeout=3)
        assert process.returncode == 0


def test_memory_pressure_skips_before_any_source_and_probe_runs_off_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    def pressure(*args):
        assert threading.get_ident() != main_thread
        return {"pressure": True, "rss_mb": 900, "limit_mb": 768}
    async def forbidden(*args, **kwargs):
        pytest.fail("Memory pressure must not start a source")
    monkeypatch.setattr(collector_resources, "snapshot", pressure)
    monkeypatch.setattr(rss, "fetch_news_for_symbols", forbidden)
    monkeypatch.setattr(sync.cn_market_news, "fetch_cn_headlines", forbidden)
    monkeypatch.setattr(sync.china_tech_media_feeds, "fetch_tech_media_feeds", forbidden)
    result = asyncio.run(sync.sync_all_sources_to_archive([]))
    assert result["status"] == "skipped" and result["skipped"] == "high_memory"
    assert result["records_received"] == 0
    with sync._collection_lock() as held:
        assert held


def test_startup_news_archive_uses_shared_memory_gate(monkeypatch):
    from app.services import startup_data_check

    async def forbidden(*args, **kwargs):
        pytest.fail("Startup must not start news sources under memory pressure")

    monkeypatch.setattr(collector_resources, "snapshot", lambda *args: {"pressure": True})
    monkeypatch.setattr(rss, "fetch_news_for_symbols", forbidden)
    monkeypatch.setattr(sync.cn_market_news, "fetch_cn_headlines", forbidden)
    monkeypatch.setattr(sync.china_tech_media_feeds, "fetch_tech_media_feeds", forbidden)
    result = asyncio.run(startup_data_check._sync_news_archive())
    assert result["status"] == "skipped" and result["reason"] == "high_memory"
    assert result["upserted"] == result["merged_unique"] == 0
