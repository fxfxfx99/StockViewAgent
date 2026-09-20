"""雪球 HTTP：限频、连接重试和有期限的匿名会话缓存。

登录态由用户在配置台提供；匿名会话被拒绝时进入冷却，不自动绕过登录要求。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

_lock = threading.Lock()
_cookie_lock = threading.Lock()
_last_request_mono: float = 0.0
_warmed_cookies: dict[str, str] = {}
_anonymous_expires_at: float = 0.0
_anonymous_retry_after: float = 0.0
_rejected_cookie_key: str | None = None
_rejected_until: float = 0.0
_ANONYMOUS_TTL_SEC = 15 * 60
_SESSION_RETRY_SEC = 5 * 60

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class XueqiuSessionError(str):
    """字符串错误附带会话状态，供调用方区分缺失和失效会话。"""

    auth_status: str

    def __new__(cls, configured: bool) -> XueqiuSessionError:
        message = (
            "雪球登录会话不可用或已过期，请在配置台更新雪球 Cookie。"
            if configured
            else "雪球当前接口需要登录，请在配置台配置雪球 Cookie。"
        )
        value = super().__new__(cls, message)
        value.auth_status = "expired" if configured else "missing"
        return value


def auth_error_status(error: str | None) -> str | None:
    """供数据管线识别会话错误，不依赖提示文案。"""
    return error.auth_status if isinstance(error, XueqiuSessionError) else None


def parse_cookies_str(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def effective_xueqiu_cookies() -> str:
    from app.storage.integrations_store import load_integrations

    data = load_integrations()
    configured = (data.get("xueqiu_cookies") or "").strip()
    return configured or (settings.xueqiu_cookies or "").strip()


def _throttle() -> None:
    global _last_request_mono
    gap = max(0.1, float(settings.xueqiu_min_interval_sec or 1.2))
    with _lock:
        now = time.monotonic()
        wait = gap - (now - _last_request_mono)
        if wait > 0:
            time.sleep(wait)
        _last_request_mono = time.monotonic()


def _warmup_anonymous_cookies(*, timeout_sec: float | None = None) -> dict[str, str]:
    """首页匿名 Cookie 缓存 15 分钟；获取失败或被接口拒绝后冷却 5 分钟。"""
    global _warmed_cookies, _anonymous_expires_at, _anonymous_retry_after
    with _cookie_lock:
        now = time.monotonic()
        if _warmed_cookies.get("xq_a_token") and now < _anonymous_expires_at:
            return dict(_warmed_cookies)
        _warmed_cookies = {}
        _anonymous_expires_at = 0.0
        if now < _anonymous_retry_after:
            return {}
        # 先设置冷却，网络异常也不会导致每次自动更新都访问首页。
        _anonymous_retry_after = now + _SESSION_RETRY_SEC
        try:
            with httpx.Client(
                timeout=float(settings.xueqiu_timeout_sec or 25.0) if timeout_sec is None else timeout_sec,
                headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                follow_redirects=True,
            ) as client:
                response = client.get("https://xueqiu.com")
                if response.is_success:
                    cookies = dict(client.cookies.items())
                    if cookies.get("xq_a_token"):
                        _warmed_cookies = cookies
                        _anonymous_expires_at = time.monotonic() + _ANONYMOUS_TTL_SEC
        except httpx.HTTPError:
            pass
        return dict(_warmed_cookies)


def _resolve_cookies(raw: str, *, timeout_sec: float | None = None) -> dict[str, str]:
    parsed = parse_cookies_str(raw)
    if raw:
        return parsed
    return _warmup_anonymous_cookies(timeout_sec=timeout_sec)


def effective_cookie_dict() -> dict[str, str]:
    return _resolve_cookies(effective_xueqiu_cookies())


def build_client(
    cookies: dict[str, str] | None = None, *, timeout_sec: float | None = None
) -> httpx.Client:
    if cookies is None:
        cookies = _resolve_cookies(effective_xueqiu_cookies(), timeout_sec=timeout_sec)
    return httpx.Client(
        timeout=float(settings.xueqiu_timeout_sec or 25.0) if timeout_sec is None else timeout_sec,
        headers={
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
        },
        cookies=cookies or None,
        follow_redirects=True,
    )


def _parse_xueqiu_body(data: Any, *, configured: bool = False) -> tuple[Any | None, str | None]:
    """从已解析的 JSON 提取业务错误或成功数据。"""
    if not isinstance(data, dict) or str(data.get("error_code") or "0") == "0":
        return data, None
    code = str(data.get("error_code"))
    desc = str(data.get("error_description") or "").strip()
    if code == "400016":
        return None, XueqiuSessionError(configured)
    return None, f"雪球接口错误 {code}: {desc or 'unknown'}"


def _reject_session(cookie_key: str, configured: bool) -> XueqiuSessionError:
    global _rejected_cookie_key, _rejected_until
    global _warmed_cookies, _anonymous_expires_at, _anonymous_retry_after
    with _cookie_lock:
        _rejected_cookie_key = cookie_key
        _rejected_until = time.monotonic() + _SESSION_RETRY_SEC
        if not configured:
            _warmed_cookies = {}
            _anonymous_expires_at = 0.0
            _anonymous_retry_after = _rejected_until
    return XueqiuSessionError(configured)


def _request_error_message(error: httpx.HTTPError) -> str:
    """协议异常可能包含完整请求头，不能将异常原文返回接口或写入缓存。"""
    if isinstance(error, httpx.TimeoutException):
        return "雪球请求超时，请稍后重试。"
    if isinstance(error, httpx.LocalProtocolError):
        return "雪球请求格式无效，请检查配置台的 Cookie 格式。"
    if isinstance(error, httpx.RemoteProtocolError):
        return "雪球响应协议异常，请稍后重试。"
    return "雪球网络连接失败，请稍后重试。"


def request_json(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    referer: str | None = None,
    timeout_sec: float | None = None,
    max_retries: int | None = None,
) -> tuple[Any | None, str | None]:
    """同步请求并返回 (data, error_message)。

    连接错误按配置重试，4xx/业务错误不重试。会话拒绝后冷却 5 分钟，
    保存不同的 Cookie 后立即允许请求；只有会话指纹用于比较，绝不输出 Cookie。
    可为公司面板单独缩短超时（也适用于匿名预热）与重试，默认保留全局配置。
    """
    raw_cookies = effective_xueqiu_cookies()
    configured = bool(raw_cookies)
    cookie_key = hashlib.sha256(raw_cookies.encode()).hexdigest()
    with _cookie_lock:
        if cookie_key == _rejected_cookie_key and time.monotonic() < _rejected_until:
            return None, XueqiuSessionError(configured)
    cookies = _resolve_cookies(raw_cookies, timeout_sec=timeout_sec)
    retries = max(0, int(settings.xueqiu_max_retries if max_retries is None else max_retries))
    for attempt in range(retries + 1):
        _throttle()
        try:
            with build_client(cookies, timeout_sec=timeout_sec) as client:
                headers = dict(client.headers)
                if referer:
                    headers["Referer"] = referer
                response = client.request(method, url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            if attempt < retries:
                time.sleep(0.4 * (2**attempt))
                continue
            return None, _request_error_message(exc)

        if response.status_code == 401:
            return None, _reject_session(cookie_key, configured)
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            return None, f"雪球返回非 JSON 响应（HTTP {response.status_code}），请稍后重试或检查网络。"

        body, error = _parse_xueqiu_body(data, configured=configured)
        if auth_error_status(error):
            return None, _reject_session(cookie_key, configured)
        if error is not None:
            return None, error
        if response.is_success:
            return body, None
        return None, f"雪球 HTTP {response.status_code}，请稍后重试。"

    return None, "雪球请求失败"


def stock_page_referer(xq_symbol: str) -> str:
    return f"https://xueqiu.com/S/{quote(xq_symbol, safe='')}"
