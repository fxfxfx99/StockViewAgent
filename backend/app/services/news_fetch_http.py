"""新闻爬虫共用：UA、超时、带退避的重试请求。"""
from __future__ import annotations

import asyncio
import random
import zlib
from typing import Any

import httpx

NEWS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_NEWS_TIMEOUT = httpx.Timeout(38.0, connect=15.0)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024

_RETRYABLE = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.LocalProtocolError,
    httpx.WriteError,
    httpx.PoolTimeout,
)


async def _bounded_body(response: httpx.Response, limit: int) -> bytes | None:
    # Test/custom transports may already have materialized and decoded content.
    if response.is_stream_consumed:
        return response.content if len(response.content) <= limit else None
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate", ""}:
        return None
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS) if encoding in {"gzip", "deflate"} else None
    body, wire_bytes = bytearray(), 0
    async for chunk in response.aiter_raw(chunk_size=_READ_CHUNK_BYTES):
        wire_bytes += len(chunk)
        if wire_bytes > limit:
            return None
        try:
            # Bound decompression itself: a compressed bomb must never allocate
            # its full decoded body inside HTTPX before our length check runs.
            decoded = decoder.decompress(chunk, limit - len(body) + 1) if decoder else chunk
        except zlib.error:
            if encoding != "deflate" or wire_bytes != len(chunk):
                return None
            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            try:
                decoded = decoder.decompress(chunk, limit + 1)
            except zlib.error:
                return None
        if len(body) + len(decoded) > limit or decoder and (decoder.unconsumed_tail or decoder.unused_data):
            return None
        body.extend(decoded)
    if decoder and not decoder.eof:
        return None
    return bytes(body)


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_attempts: int = 4,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> httpx.Response | None:
    if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool) or max_response_bytes < 1:
        raise ValueError("max_response_bytes must be a positive integer")
    hdrs = dict(headers or {})
    if "User-Agent" not in hdrs:
        hdrs["User-Agent"] = NEWS_UA
    hdrs.setdefault("Accept-Encoding", "gzip, deflate")
    for attempt in range(max_attempts):
        try:
            async with client.stream("GET", url, follow_redirects=True, headers=hdrs, params=params) as response:
                if response.status_code != 200:
                    return None  # Publisher denials/rate limits are not transport retries.
                if response.status_code == 200:
                    length = response.headers.get("content-length", "")
                    if length.isdecimal() and int(length) > max_response_bytes:
                        return None  # Repeating an oversized response wastes memory and bandwidth.
                    body = await _bounded_body(response, max_response_bytes)
                    if body is None:
                        return None
                    # The bounded reader has already decompressed the body. Preserve
                    # the response API without decoding compressed content twice.
                    response_headers = httpx.Headers(response.headers)
                    response_headers.pop("content-encoding", None)
                    response_headers["content-length"] = str(len(body))
                    return httpx.Response(response.status_code, content=body,
                                          headers=response_headers, request=response.request,
                                          extensions=response.extensions)
        except _RETRYABLE:
            pass
        except httpx.HTTPError:
            return None
        if attempt + 1 < max_attempts:
            await asyncio.sleep(0.25 + random.random() * 0.45 * (attempt + 1))
    return None


async def fetch_bytes_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> bytes | None:
    r = await get_with_retry(client, url, headers=headers, max_response_bytes=max_response_bytes)
    return r.content if r is not None else None
