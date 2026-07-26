from __future__ import annotations

import hashlib
import logging
import os
import sys

import orjson
import structlog

# P2-8 日志脱敏: 这些键名的字符串值可能承载用户原文 (查询/答案/消息/题干等),
# 「我妈得了乳腺癌…」这类内容一旦进集中日志就是全员可搜的隐私事故。
# 统一替换为 sha256 前 8 位 + 长度指纹: 同一原文指纹一致, 仍可跨行关联排障,
# 但不可逆、不落明文。本地调试需要看原文时设 LOG_PII_PLAINTEXT=1 (仅限本地)。
SENSITIVE_KEYS = frozenset(
    {"query", "answer", "content", "message", "text", "stem", "response"}
)
_PLAINTEXT_ENV = "LOG_PII_PLAINTEXT"


def _fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"<pii:{digest}:len={len(value)}>"


def _scrub(obj: object) -> object:
    """递归处理嵌套容器: dict 按键名单替换, list/tuple 逐元素下钻。"""
    if isinstance(obj, dict):
        return {
            key: (
                _fingerprint(val)
                if isinstance(key, str)
                and key.lower() in SENSITIVE_KEYS
                and isinstance(val, str)
                else _scrub(val)
            )
            for key, val in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def pii_scrub_processor(
    logger: object, method_name: str, event_dict: dict
) -> dict:
    """structlog processor (P2-8): 敏感键的字符串值哈希化, 嵌套 dict/list 同样生效。

    - "event" 键是事件名 (如 "request"), 按约定不携带用户原文, 不参与替换;
    - 环境变量 LOG_PII_PLAINTEXT=1 时整体旁路 (逐次读取, 便于测试与紧急排障切换)。
    """
    if os.environ.get(_PLAINTEXT_ENV) == "1":
        return event_dict
    for key, val in list(event_dict.items()):
        if key == "event":
            continue
        if (
            isinstance(key, str)
            and key.lower() in SENSITIVE_KEYS
            and isinstance(val, str)
        ):
            event_dict[key] = _fingerprint(val)
        elif isinstance(val, (dict, list, tuple)):
            event_dict[key] = _scrub(val)
    return event_dict


def _orjson_dumps(obj: object, **kwargs: object) -> str:
    return orjson.dumps(obj, default=str).decode()


def configure_logging(level: str = "INFO") -> None:
    """JSON logs to stdout. Every request handler binds request_id/tenant_id/
    user_id via structlog.contextvars so all lines within a request carry them."""
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            pii_scrub_processor,  # P2-8: 渲染前最后一道脱敏 (在时间戳/级别之后)
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(serializer=_orjson_dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "eduagents") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
