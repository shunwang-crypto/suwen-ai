"""删除 outbox 消费者 (P1-5): 扫 pending_cleanup 未完成行执行物理清理。

替代"PG 先 COMMIT、ARQ 后 enqueue"的窗口: 删除事务内写行 (routers/kb.py /
routers/account.py), 本任务每分钟消费, Redis 抖动/进程崩溃只会推迟清理,
不再产生永久孤儿。

幂等: cleanup_document / cleanup_kb / purge_tenant 全部可重放; done 标记在清理
成功后才落库, 中途崩溃下轮重扫重做。attempts 在执行前自增 (独立小事务留痕),
≥5 次仍失败打 error 级告警日志 (cleanup_outbox_stuck) 供人工介入, 不自动放弃。

接线 (主线): worker/main.py cron_jobs 注册 process_pending_cleanup 每分钟一趟。
"""

from __future__ import annotations

from eduagents_core.logging import get_logger
from sqlalchemy import select, update

from .ingestion import cleanup_document, cleanup_kb
from .purge import purge_tenant

log = get_logger("cleanup_outbox")

_ALERT_ATTEMPTS = 5
_BATCH = 100


async def process_pending_cleanup(ctx: dict) -> int:
    """消费一批未完成行, 返回本趟完成数。"""
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup

    async with plain_session() as session:
        rows = (
            await session.execute(
                select(
                    PendingCleanup.id,
                    PendingCleanup.kind,
                    PendingCleanup.payload,
                    PendingCleanup.attempts,
                )
                .where(PendingCleanup.done.is_(False))
                .order_by(PendingCleanup.created_at)
                .limit(_BATCH)
            )
        ).all()

    completed = 0
    for row_id, kind, payload, prev_attempts in rows:
        payload = payload or {}
        # 尝试次数先落库: 执行中崩溃也留下痕迹, 告警口径以 DB 为准
        async with plain_session() as session:
            await session.execute(
                update(PendingCleanup)
                .where(PendingCleanup.id == row_id)
                .values(attempts=PendingCleanup.attempts + 1)
            )
        attempts = prev_attempts + 1
        try:
            if kind == "doc":
                await cleanup_document(
                    ctx,
                    payload["tenant_id"],
                    payload["kb_id"],
                    payload["doc_id"],
                    payload.get("filename", ""),
                )
            elif kind == "kb":
                await cleanup_kb(ctx, payload["tenant_id"], payload["kb_id"])
            elif kind == "tenant":
                await purge_tenant(ctx, payload["tenant_id"], payload.get("session_ids"))
            else:
                raise ValueError(f"unknown cleanup kind: {kind}")
        except Exception:  # noqa: BLE001 — 单行失败不阻塞其余行, 下轮重试
            if attempts >= _ALERT_ATTEMPTS:
                log.exception(
                    "cleanup_outbox_stuck", id=str(row_id), kind=kind, attempts=attempts
                )
            else:
                log.warning(
                    "cleanup_outbox_retry", id=str(row_id), kind=kind, attempts=attempts
                )
            continue
        async with plain_session() as session:
            await session.execute(
                update(PendingCleanup)
                .where(PendingCleanup.id == row_id)
                .values(done=True)
            )
        completed += 1

    if rows:
        log.info("cleanup_outbox_processed", scanned=len(rows), done=completed)
    return completed
