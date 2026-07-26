from __future__ import annotations

from arq.connections import RedisSettings
from eduagents_core.config import get_settings
from eduagents_core.logging import configure_logging, get_logger

log = get_logger("worker")


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    ctx["settings"] = settings
    log.info("worker_started")


async def shutdown(ctx: dict) -> None:
    log.info("worker_stopped")


async def ping(ctx: dict) -> str:
    """Liveness task; M2 ingestion pipeline tasks land beside it."""
    return "pong"


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    functions = [ping]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_jobs = 10
    job_timeout = 600
