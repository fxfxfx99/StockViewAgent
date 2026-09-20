"""本地免登录仍限制请求主机与浏览器来源，保留可信主机的 CLI / Agent 调用。"""
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _origin_key(origin: str) -> tuple[str, str, int] | None:
    if not origin or "*" in origin or any(ord(char) <= 32 or ord(char) == 127 or char.isspace() for char in origin):
        return None
    try:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc.endswith(":")
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "?" in origin
            or "#" in origin
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.port == 0:
            return None
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


class TrustedOriginMiddleware(CORSMiddleware):
    """CORS 与实际请求使用同一白名单，避免简单 POST 绕过预检。

    主机只允许 loopback 或明确登记的来源主机，避免无 Origin 请求通过
    DNS rebinding 访问本地服务；不信任 X-Forwarded-Host。
    """

    def __init__(self, app: ASGIApp, allowed_origins: str = "") -> None:
        self.trusted_origins = set()
        for origin in allowed_origins.split(","):
            if not origin.strip():
                continue
            key = _origin_key(origin.strip())
            if key is None:
                raise ValueError("CORS_ALLOWED_ORIGINS 仅接受逗号分隔的 http(s) 来源，不支持通配符或路径")
            self.trusted_origins.add(key)
        self.trusted_hosts = _LOCAL_HOSTS | {key[1] for key in self.trusted_origins}
        super().__init__(app, allow_methods=["*"], allow_headers=["*"])

    def is_allowed_origin(self, origin: str) -> bool:
        key = _origin_key(origin)
        return key is not None and (key[1] in _LOCAL_HOSTS or key in self.trusted_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            hosts = headers.getlist("host")
            host = _origin_key(f"http://{hosts[0]}") if len(hosts) == 1 else None
            if host is None or host[1] not in self.trusted_hosts:
                response = JSONResponse(status_code=403, content={"detail": "不允许的请求主机"})
                await response(scope, receive, send)
                return
            origins = headers.getlist("origin")
            if origins and (len(origins) != 1 or not self.is_allowed_origin(origins[0])):
                response = JSONResponse(status_code=403, content={"detail": "不允许的浏览器来源"})
                await response(scope, receive, send)
                return
        await super().__call__(scope, receive, send)
