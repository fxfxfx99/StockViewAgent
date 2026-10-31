"""Overseas responses remain bounded before compressed bodies are expanded."""
import asyncio
import gzip
import tracemalloc
import zlib

import httpx
import pytest

from app.services import overseas_news as news


class Body(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        self.reads += 1
        yield self.content

    async def aclose(self):
        self.closed = True


def install_response(monkeypatch, stream, *, status=200, headers=None):
    source = next(row for row in news.SOURCES if row.id == "ft")
    requests = []
    monkeypatch.setattr(news, "_proxies", lambda: [])

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers=headers or {}, stream=stream)

    monkeypatch.setattr(news, "_new_client", lambda proxy: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return source, requests


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_expansion_stops_at_limit_without_full_body_allocation_or_retry(monkeypatch, encoding):
    payload = b"x" * (16 * 1024 * 1024)
    compressed = gzip.compress(payload) if encoding == "gzip" else zlib.compress(payload)
    del payload
    stream = Body(compressed)
    source, requests = install_response(monkeypatch, stream, headers={"content-encoding": encoding})

    async def run():
        tracemalloc.start()
        try:
            with pytest.raises(news.FetchError, match="2 MiB"):
                await news._fetch(source, source.urls[0])
            # The compressed fixture expands to 16 MiB. HTTPX aiter_bytes()
            # used to materialize it before checking, exceeding this bound.
            assert tracemalloc.get_traced_memory()[1] < 6 * 1024 * 1024
        finally:
            tracemalloc.stop()

    asyncio.run(run())
    assert len(requests) == 1 and stream.closed


def test_valid_compressed_feed_retains_metadata(monkeypatch):
    payload = b'<rss version="2.0"><channel><title>Markets</title></channel></rss>'
    stream = Body(gzip.compress(payload))
    source, requests = install_response(monkeypatch, stream, headers={"content-encoding": "gzip", "etag": '"revision1"'})
    result = asyncio.run(news._fetch(source, source.urls[0], headers={"If-None-Match": '"old"'}))
    assert result.body == payload and result.status == 200
    assert result.url == source.urls[0] and result.headers["etag"] == '"revision1"'
    assert requests[0].headers["if-none-match"] == '"old"'
    assert len(requests) == 1 and stream.closed


def test_declared_oversized_body_is_closed_without_reading(monkeypatch):
    stream = Body(b"not read")
    source, requests = install_response(monkeypatch, stream, headers={"content-length": str(news.MAX_RESPONSE_BYTES + 1)})
    with pytest.raises(news.FetchError, match="2 MiB"):
        asyncio.run(news._fetch(source, source.urls[0]))
    assert len(requests) == 1 and stream.reads == 0 and stream.closed


def test_not_modified_ignores_representation_length_and_encoding(monkeypatch):
    stream = Body(b"")
    source, requests = install_response(monkeypatch, stream, status=304, headers={
        "content-length": str(news.MAX_RESPONSE_BYTES + 1), "content-encoding": "gzip", "etag": '"revision1"'})
    result = asyncio.run(news._fetch(source, source.urls[0]))
    assert result.status == 304 and result.body == b"" and result.headers["etag"] == '"revision1"'
    assert len(requests) == 1 and stream.reads == 0 and stream.closed


@pytest.mark.parametrize("status", [401, 403, 429])
def test_protected_response_keeps_cooldown_without_reading_or_retry(monkeypatch, status):
    stream = Body(gzip.compress(b"provider refusal"))
    source, requests = install_response(monkeypatch, stream, status=status, headers={
        "content-encoding": "gzip", "retry-after": "900"})
    with pytest.raises(news.FetchError) as caught:
        asyncio.run(news._fetch(source, source.urls[0]))
    assert caught.value.status == status and caught.value.retry_after == 900
    assert len(requests) == 1 and stream.reads == 0 and stream.closed


def test_client_advertises_only_bounded_supported_encodings():
    async def run():
        async with news._new_client(None) as client:
            assert client.headers["accept-encoding"] == "gzip, deflate"
            assert client.follow_redirects is False
    asyncio.run(run())
