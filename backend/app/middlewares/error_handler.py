"""全局异常与校验错误：统一 JSON，便于前端与日志（借鉴 daily_stock_analysis api/middlewares）。"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Any:
        try:
            return await call_next(request)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "未处理异常 path=%s method=%s err=%s\n%s",
                request.url.path,
                request.method,
                e,
                traceback.format_exc(),
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "message": "服务器内部错误，请稍后重试",
                    "detail": str(e),
                },
            )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exc_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            body = {"error": "http_error", "message": detail.get("message", "请求错误"), **detail}
            if "detail" not in body:
                body["detail"] = body.get("message", "请求错误")
        else:
            msg = str(detail)
            # 同时提供 detail，与 FastAPI 默认及前端 axios 习惯一致，避免只显示「status code 502」
            body = {"error": "http_error", "message": msg, "detail": msg}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "请求参数校验失败",
                "detail": exc.errors(),
            },
        )
