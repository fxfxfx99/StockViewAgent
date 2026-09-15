"""
雪球 HTTP：限频、重试、Session/Cookie（对齐 easytrader 用法，不依赖该库）。
无有效 Cookie 时接口常返回 error_code=400016，需在 integrations 或环境变量配置 xueqiu_cookies。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

_lock = threading.Lock()
_last_request_mono: float = 0.0
_warmed_cookies: dict[str, str] = {}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def parse_cookies_str(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def effective_xueqiu_cookies() -> str:
    from app.storage.integrations_store import load_integrations

    data = load_integrations()
    f = (data.get("xueqiu_cookies") or "").strip()
    if f:
        return f
    return (settings.xueqiu_cookies or "").strip()


def _throttle() -> None:
    global _last_request_mono
    gap = max(0.1, float(settings.xueqiu_min_interval_sec or 1.2))
    with _lock:
        now = time.monotonic()
        wait = gap - (now - _last_request_mono)
        if wait > 0:
            time.sleep(wait)
        _last_request_mono = time.monotonic()


def _warmup_anonymous_cookies() -> dict[str, str]:
    """无配置 Cookie 时访问首页获取 xq_a_token（匿名态，部分接口仍可能 400016）。"""
    global _warmed_cookies
    with _lock:
        if _warmed_cookies.get("xq_a_token"):
            return dict(_warmed_cookies)
        try:
            with httpx.Client(
                timeout=float(settings.xueqiu_timeout_sec or 25.0),
                headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                follow_redirects=True,
            ) as client:
                client.get("https://xueqiu.com")
                for k, v in client.cookies.items():
                    _warmed_cookies[k] = v
        except httpx.HTTPError:
            pass
        return dict(_warmed_cookies)


def effective_cookie_dict() -> dict[str, str]:
    parsed = parse_cookies_str(effective_xueqiu_cookies())
    if parsed.get("xq_a_token"):
        return parsed
    warmed = _warmup_anonymous_cookies()
    if warmed:
        return {**parsed, **warmed}
    return parsed


def build_client() -> httpx.Client:
    cookies = effective_cookie_dict()
    return httpx.Client(
        timeout=float(settings.xueqiu_timeout_sec or 25.0),
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


def _parse_xueqiu_body(data: Any) -> tuple[Any | None, str | None]:
    """从已解析的 JSON 提取业务错误或成功数据。"""
    if not isinstance(data, dict):
        return data, None
    if not data.get("error_code"):
        return data, None
    code = str(data.get("error_code"))
    desc = (data.get("error_description") or "").strip()
    if code == "400016":
        return None, (
            "雪球拒绝匿名请求(400016)。请在 backend/data/integrations.json 配置 xueqiu_cookies "
            "（从浏览器登录 xueqiu.com 后复制完整 Cookie 字符串），或设置环境变量 XUEQIU_COOKIES。"
        )
    return None, f"雪球接口错误 {code}: {desc or 'unknown'}"


def request_json(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    referer: str | None = None,
) -> tuple[Any | None, str | None]:
    """
    同步请求 JSON。返回 (data, error_message)。
    雪球常对匿名请求返回 HTTP 400，但 body 仍为 JSON（含 error_code），故不能先 raise_for_status。
    对连接类错误按配置重试；4xx/业务错误不重试。
    """
    retries = max(0, int(settings.xueqiu_max_retries or 2))
    last_err: str | None = None
    for attempt in range(retries + 1):
        _throttle()
        try:
            with build_client() as client:
                headers = dict(client.headers)
                if referer:
                    headers["Referer"] = referer
                r = client.request(method, url, params=params, headers=headers)
                text = r.text
        except httpx.HTTPError as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(0.4 * (2**attempt))
                continue
            return None, last_err

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if r.status_code >= 400:
                hint = (
                    "雪球返回 HTTP "
                    f"{r.status_code} 且正文非 JSON（多为未登录或 WAF）。"
                    "请配置 xueqiu_cookies（见 backend/data/integrations.json 或环境变量 XUEQIU_COOKIES）。"
                )
                return None, hint
            return None, "响应非 JSON（可能被 WAF 拦截，请检查 Cookie 与网络）"

        body, biz_err = _parse_xueqiu_body(data)
        if biz_err is not None:
            return None, biz_err
        if r.is_success:
            return body, None
        if r.status_code >= 400:
            return None, (
                f"雪球 HTTP {r.status_code}（无有效 Cookie 时常见）。"
                "请在 backend/data/integrations.json 配置 xueqiu_cookies，或环境变量 XUEQIU_COOKIES。"
            )
        return None, f"雪球 HTTP {r.status_code}"

    return None, last_err or "未知错误"


def stock_page_referer(xq_symbol: str) -> str:
    return f"https://xueqiu.com/S/{quote(xq_symbol, safe='')}"
