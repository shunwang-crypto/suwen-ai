from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from time import perf_counter
from zoneinfo import ZoneInfo

import orjson
import structlog
from eduagents_core.gateway import BudgetExceeded
from eduagents_core.logging import get_logger
from eduagents_core.moderation import SAFE_MESSAGE_CRISIS, get_moderator
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from .wallet import InsufficientCredits

log = get_logger("api")

# 预算 Retry-After 与 budget.py 同口径: 沪时日界, 真实系统时钟
CN_TZ = ZoneInfo("Asia/Shanghai")


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


# 审核范围 (P2-4): 用户自由输入入口 → 需过审的顶层字符串字段, 配置驱动。
# 按 rstrip("/") 后的完整路径精确匹配, 子路径 (会话增删改查/{id}/attempts 等) 不参与。
_MODERATED_FIELDS: dict[str, tuple[str, ...]] = {
    "/api/chat": ("message",),
    "/api/tutor": ("problem", "message"),
    "/api/quiz": ("topic",),
    "/api/plans": ("goal",),
}


class ContentModerationMiddleware(BaseHTTPMiddleware):
    """内容审核中间件 (S-A3, §8 第 4 条): 自由文本入参的输入侧审核 (路径→字段表驱动)。

    做成中间件而非在各 router 里散落调用 —— 审核是横切关注点, 且 chat 走 SSE
    流式响应, 在中间件拦下可以在**第一个字节前**直接返回标准错误信封 (400),
    前端沿用既有 ApiError 分支, 不必给流里再造一种拒绝事件。

    未带 Authorization 的请求直接放行 (端点自身返回 401): 避免匿名者拿这个端点
    当敏感词探测器。body 解析失败也放行 (交给 pydantic 返 422)。

    危机语义 (P0-5): check_input 命中危机类**不拦截**, 在 request.state 上置
    ``moderation_crisis=True``, 下游端点 (chat.py) 据此注入关怀提示; 若同时命中
    其他分类被拦截, 拒绝文案换成含心理援助热线的危机干预文案。
    输出侧审核见 moderation.StreamModerationBuffer (SSE 分句缓冲)。
    """

    async def dispatch(self, request: Request, call_next):
        fields = _MODERATED_FIELDS.get(request.url.path.rstrip("/"))
        if request.method != "POST" or fields is None or not request.headers.get("authorization"):
            return await call_next(request)
        # starlette 的 BaseHTTPMiddleware 用 _CachedRequest 缓存 body, 这里读取
        # 不会把下游 endpoint 的请求体吃掉。
        payload = None
        raw = await request.body()
        if raw:
            try:
                payload = orjson.loads(raw)
            except orjson.JSONDecodeError:
                payload = None  # 解析失败放行: 端点侧 pydantic 会返 422
        if isinstance(payload, dict):
            moderator = get_moderator()
            for field in fields:
                value = payload.get(field)
                if not isinstance(value, str) or not value:
                    continue
                verdict = moderator.check_input(value)
                if verdict.crisis:
                    # 危机不拦截只标记 (P0-5): 下游端点注入关怀提示/热线
                    request.state.moderation_crisis = True
                if not verdict.allowed:
                    log.warning(
                        "content_rejected",
                        category=verdict.category,
                        matched=verdict.matched,
                        path=request.url.path,
                        field=field,
                        crisis=verdict.crisis,
                    )
                    return ORJSONResponse(
                        status_code=400,
                        content={
                            "code": "CONTENT_REJECTED",
                            "message": (
                                SAFE_MESSAGE_CRISIS
                                if verdict.crisis
                                else "内容不符合平台规范，请修改后再试"
                            ),
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
            # 图形验证码 + 密码找回 (routers/auth.py)
            "CAPTCHA_REQUIRED": "请先完成图形验证码",
            "CAPTCHA_INVALID": "验证码错误或已过期，请刷新后重试",
            "RESET_TOKEN_INVALID": "重置链接无效或已过期，请重新申请",
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
            # P2-15: 补齐信封映射, 前端不再显示英文裸码
            "KB_FORBIDDEN": "没有权限访问该知识库",
            "RETRIEVAL_UNAVAILABLE": "检索服务暂时不可用，请稍后重试",
            "WEB_SEARCH_QUERY_TOO_SHORT": "搜索内容过短，请补充完整问题后再试",
            "WEB_SEARCH_UNAVAILABLE": "网页搜索暂时不可用，请稍后重试",
            "ORG_READONLY": "机构知识库为只读，仅管理员可修改",
            # 当前对话附件（知识库上传仍沿用既有文件错误码）
            "CHAT_ATTACHMENT_TOO_LARGE": "附件超过当前对话允许的大小",
            "CHAT_ATTACHMENTS_TOTAL_TOO_LARGE": "本次添加的附件总大小超过限制",
            "TOO_MANY_CHAT_ATTACHMENTS": "每条消息最多添加 4 个附件",
            "TOO_MANY_PENDING_ATTACHMENTS": "待发送附件过多，请先移除不用的文件",
            "CHAT_ATTACHMENT_NOT_FOUND": "附件不存在、已使用或无权访问",
            "UNSUPPORTED_CHAT_ATTACHMENT_TYPE": "当前对话不支持这种文件类型",
            "VISION_MODEL_UNAVAILABLE": "图片理解服务暂未配置",
            "FILE_SIGNATURE_MISMATCH": "文件内容与扩展名不一致",
            "EMPTY_FILE": "不能上传空文件",
            "CHAT_ATTACHMENT_PARSE_TIMEOUT": "文件解析超时，请缩小文件后重试",
            "CHAT_ATTACHMENT_PARSE_FAILED": "未能解析该文件，请检查文件是否损坏",
            "CHAT_ATTACHMENT_EMPTY_TEXT": "文件中没有可读取的文字",
            "CHAT_ATTACHMENT_TEXT_TOO_LARGE": "文件正文超过当前对话容量，请拆分后上传",
            "ANSWER_NOT_VERIFIED": "请先核验正确答案，再进入复习或生成变式题",
            "ERROR_ITEM_NOT_EDITABLE": "只有手动录入或上传识别的错题可以修改题目内容",
            "IMPORT_ALREADY_CONFIRMED": "该导入已经写入错题本",
            "KB_NOT_FOUND": "选择的知识库不存在或无权访问",
            # 0006 预付额度 (routers/billing.py)
            "AMOUNT_NOT_ALLOWED": "充值金额不在可选档位内",
            "ORDER_NOT_PENDING": "订单不是待支付状态，请勿重复支付",
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

    @app.exception_handler(InsufficientCredits)
    async def insufficient_credits_error(request: Request, exc: InsufficientCredits):
        # 0006 预付额度: 余额 <=0 在网关入口被拦 (先钱包后预算, wallet.py) -> 402 +
        # balance 字段 (契约冻结), 前端据此弹充值引导。InsufficientCredits 是
        # BudgetExceeded 子类, starlette 按 __mro__ 匹配 — 本 handler 优先于 429。
        return ORJSONResponse(
            status_code=402,
            content={
                "code": "INSUFFICIENT_CREDITS",
                "message": exc.message or "钱包额度已用完，请充值后继续使用 AI 功能",
                "request_id": getattr(request.state, "request_id", ""),
                "balance": format(exc.balance, "f"),
            },
        )

    @app.exception_handler(BudgetExceeded)
    async def budget_exceeded_error(request: Request, exc: BudgetExceeded):
        # P1-9: BudgetExceeded 是 GatewayError(Exception) 非 HTTPException, 旧代码
        # 落 except Exception 变 500。这里统一转 429 + Retry-After (距沪时次日零点),
        # 非流式入口 (quiz/plans/reports 等) 全部适用; SSE 入口在 chat.py 内自行发
        # event:error BUDGET_EXCEEDED (首字节前抛出的仍走本 handler)。
        retry_after = exc.retry_after_seconds
        if not retry_after:
            cn_now = datetime.now(UTC).astimezone(CN_TZ)
            midnight = (cn_now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            retry_after = max(1, int((midnight - cn_now).total_seconds()))
        return ORJSONResponse(
            status_code=429,
            content={
                "code": "BUDGET_EXCEEDED",
                "message": exc.message or "今日 AI 用量已达上限，明天再来或联系管理员",
                "request_id": getattr(request.state, "request_id", ""),
            },
            headers={"Retry-After": str(retry_after)},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return ORJSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "请求参数不合法: "
                + "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:3]
                ),
                "request_id": getattr(request.state, "request_id", ""),
            },
        )
