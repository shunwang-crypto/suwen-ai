"""用户数据导出 (P1-8, 个保法第 45 条可携带权)。

export_user_data(ctx, tenant_id): RLS 租户会话逐表 dump (17 张业务表 JSON,
含个性化记忆/偏好、学情报告快照与聊天附件 — 均属可携带权范围) +
chunk 文本 + 上传文件清单, 打 ZIP 存对象存储 exports/{uid}/{ts}.zip,
完成后写 Redis export:{uid}=path (24h TTL)。下载经 API 流式转发
(routers/account.py, LocalStorage 无预签名概念)。

接线 (主线): worker/main.py functions 注册 export_user_data。
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

log = get_logger("export")

EXPORT_TTL_SECONDS = 24 * 3600


def _json_safe(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_dict(obj) -> dict:
    return {
        attr.key: _json_safe(getattr(obj, attr.key))
        for attr in sa_inspect(obj).mapper.column_attrs
    }


def _dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


async def export_user_data(ctx: dict, tenant_id: str) -> str:
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import (
        Attempt,
        ChatAttachment,
        ChatMessage,
        ChatSession,
        Chunk,
        Document,
        ErrorImport,
        ErrorItem,
        KnowledgeBase,
        LearnerInsight,
        LearningReportSnapshot,
        MasteryLog,
        MasteryRecord,
        Plan,
        PlanAdjustment,
        Question,
        Quiz,
        User,
        UserPreference,
    )
    from eduagents_core.storage import create_storage

    settings = ctx.get("settings") or get_settings()
    tenant_id = str(tenant_id)

    # RLS 租户会话天然只见本人行，不越权。
    tables = {
        "knowledge_bases": KnowledgeBase,
        "documents": Document,
        "chat_sessions": ChatSession,
        "chat_messages": ChatMessage,
        "chat_attachments": ChatAttachment,
        "quizzes": Quiz,
        "questions": Question,
        "attempts": Attempt,
        "error_items": ErrorItem,
        "error_imports": ErrorImport,
        "plans": Plan,
        "plan_adjustments": PlanAdjustment,
        "mastery_records": MasteryRecord,
        "mastery_log": MasteryLog,
        # 0005 (P2-1): 长期记忆与偏好 — 个性化数据同属个保法可携带权范围
        "learner_insights": LearnerInsight,
        "user_preferences": UserPreference,
        "learning_report_snapshots": LearningReportSnapshot,
    }
    dumped: dict[str, list[dict]] = {}
    async with tenant_session(tenant_id) as session:
        for name, model in tables.items():
            rows = (await session.scalars(select(model))).all()
            dumped[name] = [_row_dict(r) for r in rows]
        chunk_rows = [
            {
                "doc_id": str(c.doc_id),
                "heading_path": c.heading_path,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "text": c.text,
            }
            for c in (await session.scalars(select(Chunk))).all()
        ]

    profile: dict = {}
    async with plain_session() as session:
        user = await session.get(User, uuid.UUID(tenant_id))
        if user is not None:
            profile = {
                "id": str(user.id),
                "email": user.email,
                "role": user.role,
                "plan": user.plan,
                "created_at": user.created_at.isoformat(),
            }

    # 上传文件清单 (原始文件本体留在对象存储, 清单给出 key 便于按需取回)
    uploads = [
        {
            "doc_id": d["id"],
            "filename": d["filename"],
            "size_bytes": d["size_bytes"],
            "status": d["status"],
            "storage_key": f"{tenant_id}/{d['kb_id']}/{d['id']}/{d['filename']}",
        }
        for d in dumped["documents"]
    ]
    uploads.extend(
        {
            "attachment_id": attachment["id"],
            "filename": attachment["filename"],
            "size_bytes": attachment["size_bytes"],
            "kind": attachment["kind"],
            "storage_key": attachment["storage_key"],
        }
        for attachment in dumped["chat_attachments"]
    )
    uploads.extend(
        {
            "error_import_id": imported["id"],
            "filename": imported["filename"],
            "size_bytes": imported["size_bytes"],
            "kind": imported["source_type"],
            "storage_key": imported["storage_key"],
        }
        for imported in dumped["error_imports"]
        if imported["storage_key"]
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("profile.json", _dumps(profile))
        for name, rows in dumped.items():
            zf.writestr(f"tables/{name}.json", _dumps(rows))
        zf.writestr("chunks.json", _dumps(chunk_rows))
        zf.writestr("uploads_manifest.json", _dumps(uploads))
    data = buf.getvalue()

    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    path = f"exports/{tenant_id}/{ts}.zip"
    await create_storage(settings).save(path, data)

    redis = ctx.get("redis")
    owns_redis = False
    if redis is None:
        from redis.asyncio import Redis

        redis = Redis.from_url(settings.redis_url)
        owns_redis = True
    try:
        await redis.setex(f"export:{tenant_id}", EXPORT_TTL_SECONDS, path)
        await redis.delete(f"export:pending:{tenant_id}")
    finally:
        if owns_redis:
            await redis.aclose()

    log.info(
        "export_done",
        tenant_id=tenant_id,
        path=path,
        zip_bytes=len(data),
        tables={k: len(v) for k, v in dumped.items()},
    )
    return path
