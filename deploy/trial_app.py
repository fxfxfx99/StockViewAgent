"""Public-trial ASGI entry point, copied to the isolated backend/trial_app.py.

Run with one Uvicorn worker, --host 127.0.0.1 and --no-proxy-headers.
Cloudflared connects to that loopback listener; only its CF-Connecting-IP header
is considered, never X-Forwarded-For or X-Forwarded-Host.
"""
from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
import re

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.main import app as backend
from app.middlewares.error_handler import ErrorHandlerMiddleware


# Deliberately enumerate paths and methods: new backend features are private
# until the public UI actually needs them and this list is reviewed.
API_METHODS = {
    "/api/health": {"GET"},
    "/api/auth/config": {"GET"},
    "/api/auth/login": {"POST"},
    "/api/auth/register": {"POST"},
    "/api/auth/me": {"GET"},
    "/api/watchlist": {"GET", "PUT"},
    "/api/watchlist/parse-import": {"POST"},
    "/api/watchlist/parse-import-xlsx": {"POST"},
    "/api/watchlist/profiles": {"GET"},
    "/api/watchlist/price-context": {"GET"},
    "/api/watchlist/profiles/quick-refresh": {"POST"},
    "/api/watchlist/profiles/refresh": {"POST"},
    "/api/watchlist/profiles/manual": {"PATCH"},
    "/api/watchlist/stock-detail": {"GET"},
    "/api/stocks/meta": {"GET"},
    "/api/stocks/search": {"GET"},
    "/api/stocks/refresh": {"POST"},
    "/api/stocks/universe/rebuild": {"POST"},
    "/api/settings/integrations": {"GET", "PUT"},
    "/api/settings/setup-status": {"GET"},
    "/api/settings/integrations/test-klineshare": {"POST"},
    "/api/settings/integrations/test-tushare": {"POST"},
    "/api/settings/data-center": {"GET"},
    "/api/settings/data-center/refresh": {"POST"},
    "/api/xueqiu/company": {"GET"},
    "/api/xueqiu/comments": {"GET"},
    "/api/xueqiu/comments/refresh": {"POST"},
    "/api/news/archive": {"GET"},
    "/api/news/sync-archive-feeds": {"POST"},
    "/api/news/analyze-symbol-archive": {"POST"},
}
_SYMBOL_ROUTES = (
    re.compile(r"/api/market/kline/[A-Za-z0-9_.^=-]{1,64}"),
    re.compile(r"/api/transaction-agent/views/[A-Za-z0-9_.^=-]{1,64}"),
)
_STATIC_SUFFIXES = {".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".woff", ".woff2", ".ttf"}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    ),
}


class TrialErrorMiddleware:
    """Generic failures without logging exception text, request bodies or secrets."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            if not started:
                await JSONResponse({"detail": "服务暂时不可用，请稍后重试"}, status_code=500)(scope, receive, send)


def _public_client_scope(scope: Scope) -> Scope:
    client = scope.get("client")
    if not client:
        return scope
    try:
        if not ip_address(client[0]).is_loopback:
            return scope
        values = Headers(scope=scope).getlist("cf-connecting-ip")
        if len(values) != 1:
            return scope
        # Zone IDs and lists are not a single Cloudflare client IP.
        if "%" in values[0]:
            return scope
        forwarded = str(ip_address(values[0]))
    except ValueError:
        return scope
    return {**scope, "client": (forwarded, client[1])}


def _allowed_api(scope: Scope) -> bool:
    path = scope["path"]
    methods = API_METHODS.get(path)
    if methods is None and any(pattern.fullmatch(path) for pattern in _SYMBOL_ROUTES):
        methods = {"GET"}
    if not methods:
        return False
    method = scope["method"]
    if method == "OPTIONS":
        method = Headers(scope=scope).get("access-control-request-method", "")
    return method in methods


class TrialApp:
    def __init__(self, backend_app: ASGIApp, frontend_dist: Path):
        self.backend = backend_app
        self.frontend_dist = frontend_dist.resolve()

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "lifespan":
            # Preserve backend startup/shutdown and its configured background jobs.
            await self.backend(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return

        path = scope["path"]
        is_api = path == "/api" or path.startswith("/api/")

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers[name] = value
                if is_api or path in {"/", "/setup"}:
                    headers["Cache-Control"] = "no-store"
                    headers["Pragma"] = "no-cache"
                    headers["Expires"] = "0"
            await send(message)

        if is_api:
            if _allowed_api(scope):
                await self.backend(_public_client_scope(scope), receive, secure_send)
                return
        elif scope["method"] in {"GET", "HEAD"}:
            target = None
            if path in {"/", "/setup"}:
                target = self.frontend_dist / "index.html"
            elif (
                path.startswith("/assets/")
                and not any(part.startswith(".") for part in path.split("/") if part)
                and Path(path).suffix.lower() in _STATIC_SUFFIXES
            ):
                target = self.frontend_dist / path.lstrip("/")
            if target is not None:
                try:
                    resolved = target.resolve()
                    # Resolving first rejects traversals and symlinks outside dist.
                    resolved.relative_to(self.frontend_dist)
                    if path.startswith("/assets/"):
                        resolved.relative_to(self.frontend_dist / "assets")
                    if resolved.is_file():
                        await FileResponse(resolved)(scope, receive, secure_send)
                        return
                except (OSError, ValueError):
                    pass
        await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, secure_send)


def create_trial_app(backend_app, frontend_dist: Path) -> TrialApp:
    # Only the isolated public entry point changes these handlers/middlewares.
    # Preserve TrustedOriginMiddleware and every authorization dependency.
    backend_app.user_middleware = [
        middleware for middleware in backend_app.user_middleware
        if middleware.cls is not ErrorHandlerMiddleware
    ]
    backend_app.add_middleware(TrialErrorMiddleware)

    @backend_app.exception_handler(HTTPException)
    async def safe_http_error(_request, exc: HTTPException):
        detail = "服务暂时不可用，请稍后重试" if exc.status_code >= 500 else exc.detail
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)

    @backend_app.exception_handler(RequestValidationError)
    async def safe_validation_error(_request, _exc):
        return JSONResponse({"detail": "请求参数无效"}, status_code=422)

    backend_app.openapi_url = None
    backend_app.docs_url = None
    backend_app.redoc_url = None
    backend_app.router.routes = [
        route for route in backend_app.router.routes
        if getattr(route, "path", "") not in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    ]
    return TrialApp(backend_app, frontend_dist)


app = create_trial_app(backend, Path(__file__).resolve().parent.parent / "frontend" / "dist")
