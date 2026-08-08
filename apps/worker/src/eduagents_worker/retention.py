"""数据留存清理 (P2-9): 监管可答的"留多久、怎么删"。

留存口径 (docs/PRODUCTION_GAP_REVIEW.md P2-9):
- usage_log        > 180 天删 (可还原个人每日用量画像, 不长留);
- mastery_log      > 730 天删 (学情审计流水, 保留 2 年);
- checkpoints 三表 > 30 天删 (LangGraph 检查点, JSONB 明文含问答原文,
  按 checkpoint->>'ts' 判龄; blobs/writes 无时间列, 随 thread 整体孤儿化后清);
- feedback         closed 且 > 90 天删 (工单闭环后不留个人内容)。

连接身份: 走 settings.migration_database_url (表 owner/超级用户, 与 alembic 同源)。
原因: usage_log/mastery_log 是 FORCE RLS 租户表 (edu_app 无跨租户 DELETE 通道,
mastery_log 连 is_admin 放行都没有), checkpoints 三表 owner 是迁移账户 —— 留存
清理是运维态维护任务, 用应用角色跑会静默删 0 行, 那是假实现。

每表独立事务: 单表失败 (如 feedback 迁移未跑) 不拖累其余表, 结果如实计 None。

主线接线 (worker/main.py, 两行):
    from .retention import purge_retention
    # WorkerSettings.functions 追加 purge_retention; cron_jobs 追加:
    cron(purge_retention, hour=4, minute=50, name="purge_retention_daily")
(时点 04:50 = 语义缓存清理 04:30 之后, 错峰凌晨低谷。)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

log = get_logger("retention")

USAGE_LOG_DAYS = 180
MASTERY_LOG_DAYS = 730
CHECKPOINT_DAYS = 30
FEEDBACK_CLOSED_DAYS = 90
METACOGNITION_DAYS = 730
LEARNING_EVENTS_DAYS = 730


def _statements(now: datetime) -> list[tuple[str, str, str, dict]]:
    """(名称, DELETE SQL, COUNT SQL, 参数) 四元组; dry_run 用 COUNT, 实删用 DELETE。"""
    cut_usage = now - timedelta(days=USAGE_LOG_DAYS)
    cut_mastery = now - timedelta(days=MASTERY_LOG_DAYS)
    cut_ckpt = now - timedelta(days=CHECKPOINT_DAYS)
    cut_feedback = now - timedelta(days=FEEDBACK_CLOSED_DAYS)
    cut_metacognition = now - timedelta(days=METACOGNITION_DAYS)
    cut_learning_events = now - timedelta(days=LEARNING_EVENTS_DAYS)
    ckpt_where = "(checkpoint->>'ts')::timestamptz < :cutoff"
    orphan_writes = (
        "checkpoint_writes cw WHERE NOT EXISTS "
        "(SELECT 1 FROM checkpoints c WHERE c.thread_id = cw.thread_id)"
    )
    orphan_blobs = (
        "checkpoint_blobs cb WHERE NOT EXISTS "
        "(SELECT 1 FROM checkpoints c WHERE c.thread_id = cb.thread_id)"
    )
    return [
        (
            "usage_log",
            "DELETE FROM usage_log WHERE at < :cutoff",
            "SELECT count(*) FROM usage_log WHERE at < :cutoff",
            {"cutoff": cut_usage},
        ),
        (
            "mastery_log",
            "DELETE FROM mastery_log WHERE at < :cutoff",
            "SELECT count(*) FROM mastery_log WHERE at < :cutoff",
            {"cutoff": cut_mastery},
        ),
        (
            "metacognition_predictions",
            "DELETE FROM metacognition_predictions WHERE created_at < :cutoff",
            "SELECT count(*) FROM metacognition_predictions WHERE created_at < :cutoff",
            {"cutoff": cut_metacognition},
        ),
        (
            "explain_backs",
            "DELETE FROM explain_backs WHERE created_at < :cutoff",
            "SELECT count(*) FROM explain_backs WHERE created_at < :cutoff",
            {"cutoff": cut_metacognition},
        ),
        (
            "fsrs_review_events",
            "DELETE FROM fsrs_review_events WHERE created_at < :cutoff",
            "SELECT count(*) FROM fsrs_review_events WHERE created_at < :cutoff",
            {"cutoff": cut_learning_events},
        ),
        (
            "learning_events",
            "DELETE FROM learning_events WHERE created_at < :cutoff",
            "SELECT count(*) FROM learning_events WHERE created_at < :cutoff",
            {"cutoff": cut_learning_events},
        ),
        (
            "checkpoints",
            f"DELETE FROM checkpoints WHERE {ckpt_where}",
            f"SELECT count(*) FROM checkpoints WHERE {ckpt_where}",
            {"cutoff": cut_ckpt},
        ),
        # 顺序敏感: 先删过期 checkpoints, 再清失去 thread 的 writes/blobs
        (
            "checkpoint_writes",
            f"DELETE FROM {orphan_writes}",
            f"SELECT count(*) FROM {orphan_writes}",
            {},
        ),
        (
            "checkpoint_blobs",
            f"DELETE FROM {orphan_blobs}",
            f"SELECT count(*) FROM {orphan_blobs}",
            {},
        ),
        (
            "feedback",
            "DELETE FROM feedback WHERE status = 'closed' AND created_at < :cutoff",
            "SELECT count(*) FROM feedback WHERE status = 'closed' AND created_at < :cutoff",
            {"cutoff": cut_feedback},
        ),
    ]


async def purge_retention(ctx: dict, dry_run: bool = False) -> dict:
    """按留存口径清理各表; 返回 {表名: 删除行数 | None(该表失败/不存在)}。

    dry_run=True 只 COUNT 不删 (上线前先看量)。删除量一律 log.info 留痕。
    时间用真实时钟: 留存合规按自然时间计, 不随 time-travel 虚拟时钟漂移。
    """
    settings = ctx.get("settings") or get_settings()
    url = settings.migration_database_url or settings.database_url
    now = datetime.now(UTC)
    engine = create_async_engine(url, pool_pre_ping=True)
    results: dict[str, int | None] = {}
    try:
        for name, delete_sql, count_sql, params in _statements(now):
            try:
                async with engine.begin() as conn:
                    if dry_run:
                        n = int(await conn.scalar(text(count_sql), params) or 0)
                    else:
                        n = (await conn.execute(text(delete_sql), params)).rowcount
                results[name] = n
            except Exception as exc:  # noqa: BLE001 — 单表失败不拖垮整个清理任务
                results[name] = None
                log.warning("retention_table_failed", table=name, error=str(exc)[:200])
    finally:
        await engine.dispose()
    log.info(
        "retention_purged",
        dry_run=dry_run,
        **{k: (v if v is not None else "failed") for k, v in results.items()},
    )
    return results
