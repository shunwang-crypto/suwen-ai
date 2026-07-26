from __future__ import annotations

import uuid
from time import perf_counter

import structlog
from eduagents_core.logging import get_logger
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

log = get_logger("api")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """request_id 生成 + structlog 上下文绑定 + 访问日志(含耗时)."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, path=request.url.path, method=request.method
        )
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled_error")
            return ORJSONResponse(
                status_code=500,
                content={
                    "code": "INTERNAL_ERROR",
                    "message": "服务器内部错误，请稍后重试",
                    "request_id": request_id,
                },
            )
        response.headers["X-Request-ID"] = request_id
        log.info(
            "request",
            status=response.status_code,
            duration_ms=int((perf_counter() - started) * 1000),
        )
        return response


def install_error_handlers(app: FastAPI) -> None:
    """统一错误信封 {code, message, request_id} (§6 REST 约定)."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "ERROR"
        messages = {
            "UNAUTHENTICATED": "未登录或登录已过期",
            "INVALID_TOKEN": "登录凭证无效",
            "TOKEN_REVOKED": "登录已注销，请重新登录",
            "FORBIDDEN": "没有权限执行此操作",
            "RATE_LIMITED": "请求过于频繁，请稍后再试",
            "BUDGET_EXCEEDED": "今日 AI 用量已达上限，明天再来或联系管理员",
            "EMAIL_EXISTS": "该邮箱已注册",
            "BAD_CREDENTIALS": "邮箱或密码错误",
            "REFRESH_INVALID": "会话已失效，请重新登录",
            "NOT_FOUND": "资源不存在",
        }
        return ORJSONResponse(
            status_code=exc.status_code,
            content={
                "code": detail,
                "message": messages.get(detail, detail),
                "request_id": getattr(request.state, "request_id", ""),
            },
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return ORJSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "请求参数不合法: " + "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                    for e in exc.errors()[:3]
                ),
                "request_id": getattr(request.state, "request_id", ""),
            },
        )
