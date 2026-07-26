from __future__ import annotations

import uuid
from time import perf_counter

import orjson
import structlog
from eduagents_core.logging import get_logger
from eduagents_core.moderation import get_moderator
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


# 审核范围: 用户自由输入的两个入口 (对话 / 辅导)。子路径 (会话增删改查) 不参与。
_MODERATED_PATHS = frozenset({"/api/chat", "/api/tutor"})


class ContentModerationMiddleware(BaseHTTPMiddleware):
    """内容审核中间件 (S-A3, §8 第 4 条): 对话/辅导入参 message 的输入侧审核。

    做成中间件而非在各 router 里散落调用 —— 审核是横切关注点, 且 chat 走 SSE
    流式响应, 在中间件拦下可以在**第一个字节前**直接返回标准错误信封 (400),
    前端沿用既有 ApiError 分支, 不必给流里再造一种拒绝事件。

    未带 Authorization 的请求直接放行 (端点自身返回 401): 避免匿名者拿这个端点
    当敏感词探测器。输出侧审核见 moderation.check_output 与 decisions.md D16。
    """

    async def dispatch(self, request: Request, call_next):
        if (
            request.method != "POST"
            or request.url.path.rstrip("/") not in _MODERATED_PATHS
            or not request.headers.get("authorization")
        ):
            return await call_next(request)
        # starlette 的 BaseHTTPMiddleware 用 _CachedRequest 缓存 body, 这里读取
        # 不会把下游 endpoint 的请求体吃掉。
        raw = await request.body()
        message = ""
        if raw:
            try:
                payload = orjson.loads(raw)
            except orjson.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                message = payload["message"]
        if message:
            verdict = get_moderator().check_input(message)
            if not verdict.allowed:
                log.warning(
                    "content_rejected",
                    category=verdict.category,
                    matched=verdict.matched,
                    path=request.url.path,
                )
                return ORJSONResponse(
                    status_code=400,
                    content={
                        "code": "CONTENT_REJECTED",
                        "message": "内容不符合平台规范，请修改后再试",
                        "request_id": getattr(request.state, "request_id", ""),
                    },
                )
        return await call_next(request)


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
            "CONTENT_REJECTED": "内容不符合平台规范，请修改后再试",
            # 画像定向出题 (M4-D2): 掌握度数据不足时不静默降级成普通出题
            "NO_WEAK_POINTS": "还没有识别到薄弱点，先做一次练习积累数据",
            # URL 网页导入 (parsing/web.py 的 WebFetchError.code)
            "URL_INVALID": "网址格式不合法",
            "URL_SCHEME_NOT_ALLOWED": "仅支持 http/https 网址",
            "URL_HOST_NOT_ALLOWED": "该地址指向内网或本机，已拒绝",
            "URL_HOST_UNRESOLVABLE": "域名无法解析，请检查网址",
            "URL_FETCH_FAILED": "网页抓取失败，请稍后重试或换一个网址",
            "URL_CONTENT_UNSUPPORTED": "该网址不是网页正文（仅支持 HTML/文本）",
            "URL_CONTENT_TOO_LARGE": "网页内容过大，已拒绝",
            "URL_EXTRACT_EMPTY": "未能从该网页提取到正文内容",
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
