"""新闻爬虫共用：UA、超时、带退避的重试请求。"""
from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

NEWS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_NEWS_TIMEOUT = httpx.Timeout(38.0, connect=15.0)

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


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_attempts: int = 4,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response | None:
    hdrs = dict(headers or {})
    if "User-Agent" not in hdrs:
        hdrs["User-Agent"] = NEWS_UA
    for attempt in range(max_attempts):
        try:
            r = await client.get(url, follow_redirects=True, headers=hdrs, params=params)
            if r.status_code == 200:
                return r
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
) -> bytes | None:
    r = await get_with_retry(client, url, headers=headers)
    return r.content if r is not None else None
