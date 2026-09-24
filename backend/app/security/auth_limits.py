"""Bounded, process-local auth protection for the single-worker trial server.

Client addresses must come from Request.client, never directly from forwarded
headers. The deployment must only trust proxy headers from its loopback gateway.
Multiple workers/instances need a shared limiter before enabling public signup.
"""
from __future__ import annotations

import math
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from typing import Callable, Iterator

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute


class AuthRoute(APIRoute):
    """Do not include submitted passwords/extra fields in validation responses."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise HTTPException(status_code=422, detail="请求参数无效") from exc

        return safe_handler


@dataclass
class _Bucket:
    window: float
    hits: deque[float] = field(default_factory=deque)


class AuthAttemptLimiter:
    # (per-client attempts, global attempts, rolling window in seconds).
    RULES = {"login": (10, 120, 60.0), "register": (5, 30, 3600.0)}
    MAX_BUCKETS = 4096

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = Lock()
        self._buckets: dict[tuple[str, str, str], _Bucket] = {}

    def check(self, action: str, client: str) -> None:
        client_limit, global_limit, window = self.RULES[action]
        keys = [(action, "global", ""), (action, "client", client)]
        limits = [global_limit, client_limit]
        with self._lock:
            now = self._clock()
            # Never evict an active client bucket: IP churn must not reset limits.
            if len(self._buckets) + 2 > self.MAX_BUCKETS:
                self._buckets = {
                    key: bucket for key, bucket in self._buckets.items()
                    if bucket.hits and bucket.hits[-1] > now - bucket.window
                }
            if len(self._buckets) + sum(key not in self._buckets for key in keys) > self.MAX_BUCKETS:
                self._reject(60)

            buckets = []
            for key, limit in zip(keys, limits):
                bucket = self._buckets.setdefault(key, _Bucket(window))
                while bucket.hits and bucket.hits[0] <= now - window:
                    bucket.hits.popleft()
                if len(bucket.hits) >= limit:
                    self._reject(math.ceil(bucket.hits[0] + window - now))
                buckets.append(bucket)
            for bucket in buckets:
                bucket.hits.append(now)

    @staticmethod
    def _reject(retry_after: int) -> None:
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后重试",
            headers={"Retry-After": str(max(1, retry_after))},
        )


auth_attempt_limiter = AuthAttemptLimiter()
registration_lock = Lock()
_password_slots = BoundedSemaphore(2)


def check_auth_attempt(request: Request, action: str) -> None:
    client = request.client.host if request.client else "unknown"
    auth_attempt_limiter.check(action, client)


@contextmanager
def password_work() -> Iterator[None]:
    """Reject excess work instead of queuing unbounded expensive password hashes."""
    if not _password_slots.acquire(blocking=False):
        AuthAttemptLimiter._reject(1)
    try:
        yield
    finally:
        _password_slots.release()
