from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings
from eduagents_core.cache.answer_cache import cleanup_answer_cache
from eduagents_core.config import get_settings
from eduagents_core.logging import configure_logging, get_logger

from .enrichment import enrich_document
from .evals import run_retrieval_eval_task
from .ingestion import cleanup_document, cleanup_kb, ingest_document
from .reflection import reflect_session
from .study_guide import build_study_guide

log = get_logger("worker")


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    ctx["settings"] = settings
    log.info("worker_started")


async def shutdown(ctx: dict) -> None:
    log.info("worker_stopped")


async def ping(ctx: dict) -> str:
    """Liveness task."""
    return "pong"


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    functions = [
        ping,
        ingest_document,
        enrich_document,
        cleanup_document,
        cleanup_kb,
        cleanup_answer_cache,
        run_retrieval_eval_task,
        reflect_session,
        build_study_guide,
    ]
    # 语义缓存 L2 无原生 TTL: 每日凌晨清 7 天前点位 (L1 Redis 自带 24h TTL)
    cron_jobs = [
        cron(cleanup_answer_cache, hour=4, minute=30, name="cleanup_answer_cache_daily")
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_jobs = 10
    job_timeout = 600
