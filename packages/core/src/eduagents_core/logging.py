from __future__ import annotations

import logging
import sys

import orjson
import structlog


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
