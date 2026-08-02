"""Langfuse 可观测性接线 (M3-D1) — 模型网关级 generation + 请求级 span 汇聚。

硬约束 (与 P2-7 脱敏纪律对齐):
- env 门控: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST 三者齐全
  才启用; 未启用时本模块不 import langfuse (懒加载), 热路径只做布尔短路。
- 不阻塞主路径: langfuse v4 = OTel BatchSpanProcessor 后台线程批量导出,
  start_observation/end 只写内存缓冲, 请求路径无同步网络 IO; 所有上报入口
  整体 try/except, 任何异常只 debug 日志, 绝不影响业务。
- PII: prompt/completion 默认只上 <pii:hash8:len=N> 指纹, 仅
  LANGFUSE_LOG_CONTENT=1 (本地调试) 时上原文; user/tenant 标识一律 sha256
  不可逆哈希; 画像数据 (weak_points/掌握度) 无条件不上报, 与开关无关。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from .config import Settings
from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover — 仅类型标注, 不引入运行时依赖
    from .gateway.base import ChatRequest, ChatResult

log = get_logger("observability")

_client: Any = None
_client_failed = False


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
    )


# 阶段 trace 字段脱敏白名单: 字符串值仅这些键可原样上报 (均为枚举/常量值,
# 不承载用户内容); 其余字符串一律指纹化 (rewrite 的 from/to、router 的 reason
# 等可能回显用户原文)。
_SAFE_STAGE_STR_KEYS = frozenset({"type", "route", "action", "path", "source", "skipped"})

# P2-7 铁律: 画像数据 (薄弱点名单/掌握度) 无条件不出现在上报里,
# 即便 LANGFUSE_LOG_CONTENT=1 也只上数量。
_PROFILE_KEYS = frozenset({"weak_points", "mastery", "profile_summary"})


def langfuse_enabled(settings: Settings) -> bool:
    """三个 env 齐全才启用 (缺任一即视为未配置, 全链路零开销)。"""
    return bool(
        settings.langfuse_public_key and settings.langfuse_secret_key and settings.langfuse_host
    )


def get_langfuse_client(settings: Settings) -> Any | None:
    """进程级单例; 未启用/初始化失败返回 None (失败只试一次, 不反复重试拖慢热路径)。"""
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed or not langfuse_enabled(settings):
        return None
    try:
        from langfuse import Langfuse  # 懒加载: 未启用时永不 import langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            environment=settings.app_env,
        )
    except Exception:  # noqa: BLE001 — 可观测性初始化失败绝不影响业务
        _client_failed = True
        log.debug("langfuse_init_failed", exc_info=True)
        return None
    return _client


def reset_langfuse_client() -> None:
    """丢弃单例 (测试隔离/配置热切换用); 不 flush, 需要时调用方先 flush_langfuse()。"""
    global _client, _client_failed
    _client = None
    _client_failed = False


def flush_langfuse() -> None:
    """应用 shutdown 钩子: 把 OTel 后台批量队列冲干净。未启用时零操作。"""
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:  # noqa: BLE001
        log.debug("langfuse_flush_failed", exc_info=True)


def hash_user(user_id: str) -> str:
    """user/tenant 标识不可逆哈希 (P2-7): 可跨 trace 关联同一用户, 不可还原。"""
    if not user_id:
        return ""
    return "u_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def _fingerprint(text: str) -> str:
    """与 logging.pii_scrub_processor 同款指纹: sha256 前 8 位 + 长度。"""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"<pii:{digest}:len={len(text)}>"


def observe_generation(settings: Settings, req: ChatRequest, result: ChatResult) -> None:
    """一次网关 LLM 调用 -> 一条 Langfuse generation。

    tokens/cost/latency 全部取自网关真实计量 (ChatResult, 与 usage_log 同源),
    不做任何推算; stub/replay 调用也如实上报 (metadata.mode 区分)。
    同一 API/worker 请求内的多次调用以 request_id 为种子共享 trace。
    """
    client = get_langfuse_client(settings)
    if client is None:
        return
    try:
        from langfuse import propagate_attributes

        # Never place base64 image payloads in traces. Even when content logging is
        # explicitly enabled, record only the textual parts plus an image marker.
        prompt_text = "\n".join(_message_text(m.content) for m in req.messages)
        if settings.langfuse_log_content:
            input_: Any = []
            for message in req.messages:
                item: dict[str, Any] = {
                    "role": message.role,
                    "content": _message_text(message.content),
                }
                if isinstance(message.content, list) and any(
                    part.get("type") == "image_url" for part in message.content
                ):
                    item["has_images"] = True
                input_.append(item)
            output: Any = result.text
        else:
            input_ = _fingerprint(prompt_text)
            output = _fingerprint(result.text)
        trace_context = None
        if req.request_id:
            trace_context = {"trace_id": client.create_trace_id(seed=req.request_id)}
        user_hash = hash_user(req.user_id)
        metadata = {
            "feature": req.feature,
            "mode": result.mode,
            "requested_tier": req.tier.value,
            "used_tier": result.tier,
            "route": result.route,
            "provider": result.provider,
            "degraded": result.degraded,
            "cache_hit": result.mode in ("replay", "cache"),
            "cost_cny": result.cost_cny,
            "latency_ms": result.latency_ms,
            "prompt_chars": len(prompt_text),
            "completion_chars": len(result.text),
            "user_hash": user_hash,
            "tenant_hash": hash_user(req.tenant_id),
            "request_id": req.request_id,
        }
        with propagate_attributes(user_id=user_hash or None):
            client.start_observation(
                trace_context=trace_context,
                name=f"gateway.{req.feature or 'chat'}",
                as_type="generation",
                model=result.model,
                input=input_,
                output=output,
                metadata=metadata,
                model_parameters={
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "json_mode": req.json_mode,
                },
                usage_details={
                    "input": result.prompt_tokens,
                    "output": result.completion_tokens,
                    "total": result.prompt_tokens + result.completion_tokens,
                },
            ).end()
    except Exception:  # noqa: BLE001 — 上报失败绝不影响网关返回
        log.debug("langfuse_generation_failed", exc_info=True)


def _sanitize_stage(stage: dict, log_content: bool) -> dict:
    """qa 图阶段 trace 字段脱敏: 数值/布尔原样; 字符串按白名单否则指纹;
    列表默认只上长度 (log_content=1 才展开); 画像键无条件只上数量。"""
    out: dict[str, Any] = {}
    for key, val in stage.items():
        if key in _PROFILE_KEYS:
            if isinstance(val, (list, tuple)):
                out[f"{key}_count"] = len(val)
            continue
        if isinstance(val, (bool, int, float)):
            out[key] = val
        elif isinstance(val, str):
            if key in _SAFE_STAGE_STR_KEYS or log_content:
                out[key] = val
            else:
                out[key] = _fingerprint(val)
        elif isinstance(val, (list, tuple)):
            if log_content:
                out[key] = list(val)
            else:
                out[f"{key}_count"] = len(val)
        # dict/其他复合类型: 丢弃 (当前阶段 trace 无此形态, 防御未来字段)
    return out


def observe_stages(
    settings: Settings,
    *,
    request_id: str,
    stages: list[dict],
    user_id: str = "",
    name: str = "chat.qa",
) -> None:
    """chat 请求级 trace 汇聚: qa 图 state.trace 的阶段数据挂为 span。

    与网关 generation 共用 request_id 种子 trace_id, 阶段 span 与各次 LLM
    调用自然聚在同一条 trace 下; 阶段字段经 _sanitize_stage 脱敏。
    """
    client = get_langfuse_client(settings)
    if client is None or not request_id or not stages:
        return
    try:
        from langfuse import propagate_attributes

        trace_context = {"trace_id": client.create_trace_id(seed=request_id)}
        with propagate_attributes(user_id=hash_user(user_id) or None):
            root = client.start_observation(
                trace_context=trace_context,
                name=name,
                as_type="span",
                metadata={"stage_count": len(stages), "request_id": request_id},
            )
            for stage in stages:
                stage_type = str(stage.get("type", "stage"))
                root.start_observation(
                    name=f"{name}.{stage_type}",
                    as_type="span",
                    metadata=_sanitize_stage(stage, settings.langfuse_log_content),
                ).end()
            root.end()
    except Exception:  # noqa: BLE001 — 上报失败绝不影响问答返回
        log.debug("langfuse_stages_failed", exc_info=True)
