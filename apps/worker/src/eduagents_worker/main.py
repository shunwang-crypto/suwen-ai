from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings
from eduagents_api.vector_audit import run_vector_audit
from eduagents_core.cache.answer_cache import cleanup_answer_cache
from eduagents_core.config import get_settings
from eduagents_core.logging import configure_logging, get_logger
from eduagents_core.observability import flush_langfuse

from .cleanup_outbox import process_pending_cleanup
from .enrichment import enrich_document
from .error_import_cleanup import reap_stale_error_imports
from .evals import run_retrieval_eval_task
from .export import export_user_data
from .ingestion import (
    cleanup_document,
    cleanup_kb,
    ingest_document,
    reap_stale_ingestion,
)
from .purge import purge_tenant
from .reflection import reflect_session
from .reports import refresh_learning_report
from .resources import extract_learning_resource
from .retention import purge_retention
from .study_guide import build_study_guide
from .tutor_cleanup import reap_stale_tutor_drafts
from .tutor_recognition import recognize_tutor_problem

log = get_logger("worker")


async def startup(ctx: dict) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    ctx["settings"] = settings
    log.info(
        "worker_started",
        registration_mode_source=settings.registration_mode_source,
    )


async def shutdown(ctx: dict) -> None:
    # M3-D1: Langfuse OTel 后台队列冲干净 (未启用时零操作, 内部 try/except)
    flush_langfuse()
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
        refresh_learning_report,
        extract_learning_resource,
        recognize_tutor_problem,
        build_study_guide,
        export_user_data,
        purge_tenant,
        reap_stale_ingestion,
        process_pending_cleanup,
        run_vector_audit,
        purge_retention,
        reap_stale_error_imports,
        reap_stale_tutor_drafts,
    ]
    cron_jobs = [
        # 语义缓存 L2 无原生 TTL: 每日凌晨清 7 天前点位 (L1 Redis 自带 24h TTL)
        cron(cleanup_answer_cache, hour=4, minute=30, name="cleanup_answer_cache_daily"),
        # P1-5 outbox: 删除的物理清理消费 (幂等, 毒行不阻塞)
        cron(process_pending_cleanup, minute=set(range(60)), name="cleanup_outbox_minutely"),
        # A3/A4: metadata-only full vector audit; Redis lock prevents overlap.
        cron(run_vector_audit, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, name="vector_audit_5m"),
        # P1-6 摄取停滞回收: >30min 中间态置 failed, 重入队 ≤3 次
        cron(reap_stale_ingestion, minute={0, 15, 30, 45}, name="reap_stale_ingestion"),
        # P2-9 留存: usage_log 180d / mastery_log 730d / checkpoint 30d / closed feedback 90d
        cron(purge_retention, hour=4, minute=50, name="purge_retention_daily"),
        cron(reap_stale_error_imports, minute={0, 20, 40}, name="reap_stale_error_imports"),
        cron(reap_stale_tutor_drafts, minute={10, 30, 50}, name="reap_stale_tutor_drafts"),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_jobs = 10
    job_timeout = 600
    # P1-7: 成本敏感任务不自动重试 (LLM 调用后崩溃的重投会重复扣费);
    # 摄取的受控重试由 reap_stale_ingestion cron 显式管理 (≤3 次)
    max_tries = 1
