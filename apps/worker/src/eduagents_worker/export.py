"""用户数据导出 (P1-8, 个保法第 45 条可携带权)。

export_user_data(ctx, tenant_id): RLS 租户会话逐表 dump (业务表 JSON,
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


def _safe_zip_filename(value: str | None) -> str:
    """只保留一个 ZIP 文件名组件，防止旧数据中的目录穿越。"""
    name = str(value or "source").replace("\\", "/").rsplit("/", 1)[-1]
    name = name.replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    if name in {"", ".", ".."}:
        return "source"
    return name[:255]


def _error_import_zip_path(import_id: str, filename: str | None) -> str:
    try:
        safe_import_id = str(uuid.UUID(str(import_id)))
    except (ValueError, TypeError, AttributeError):
        safe_import_id = "unknown"
    return f"uploads/error_imports/{safe_import_id}/{_safe_zip_filename(filename)}"


def _resource_zip_path(resource_id: str, filename: str | None) -> str:
    try:
        safe_resource_id = str(uuid.UUID(str(resource_id)))
    except (ValueError, TypeError, AttributeError):
        safe_resource_id = "unknown"
    return f"uploads/resources/{safe_resource_id}/{_safe_zip_filename(filename)}"


async def export_user_data(ctx: dict, tenant_id: str) -> str:
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import (
        AnswerRating,
        AssessmentAnswer,
        AssessmentAttempt,
        AssessmentSubmission,
        Attempt,
        ChatAttachment,
        ChatMessage,
        ChatSession,
        Chunk,
        Document,
        ErrorImport,
        ErrorItem,
        ExplainBack,
        FsrsParams,
        FsrsReviewEvent,
        HarnessSession,
        KcExplanation,
        KnowledgeBase,
        LearnerInsight,
        LearnerMemoryPolicy,
        LearningEvent,
        LearningReportSnapshot,
        LearningResource,
        MasteryLog,
        MasteryRecord,
        MetacognitionPrediction,
        Plan,
        PlanAdjustment,
        Question,
        Quiz,
        ResourceQuestion,
        TutorProblemDraft,
        User,
        UserModelCredential,
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
        "assessment_submissions": AssessmentSubmission,
        "harness_sessions": HarnessSession,
        "error_items": ErrorItem,
        "error_imports": ErrorImport,
        "learning_resources": LearningResource,
        "resource_questions": ResourceQuestion,
        "tutor_problem_drafts": TutorProblemDraft,
        "assessment_attempts": AssessmentAttempt,
        "assessment_answers": AssessmentAnswer,
        "plans": Plan,
        "plan_adjustments": PlanAdjustment,
        "mastery_records": MasteryRecord,
        "mastery_log": MasteryLog,
        "fsrs_review_events": FsrsReviewEvent,
        "fsrs_params": FsrsParams,
        "learning_events": LearningEvent,
        "metacognition_predictions": MetacognitionPrediction,
        "explain_backs": ExplainBack,
        # 0005 (P2-1): 长期记忆与偏好 — 个性化数据同属个保法可携带权范围
        "learner_insights": LearnerInsight,
        "learner_memory_policies": LearnerMemoryPolicy,
        "user_preferences": UserPreference,
        # 0031: 回答评分遥测 (含画像指纹) 同属可携带范围
        "answer_ratings": AnswerRating,
        "learning_report_snapshots": LearningReportSnapshot,
        # 0033: 按知识点的结构化讲解 (由本人资料生成并按掌握档适配, 同属可携带范围)
        "kc_explanations": KcExplanation,
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
        credential = await session.scalar(
            select(UserModelCredential).where(
                UserModelCredential.user_id == uuid.UUID(tenant_id),
                UserModelCredential.provider == "deepseek",
            )
        )
        dumped["model_credentials"] = (
            [
                {
                    "provider": credential.provider,
                    "enabled": credential.enabled,
                    "fallback_to_platform": credential.fallback_to_platform,
                    "key_last_four": credential.key_last_four,
                    "normal_model": credential.normal_model,
                    "reasoning_model": credential.reasoning_model,
                    "last_validated_at": _json_safe(credential.last_validated_at),
                    "last_validation_status": credential.last_validation_status,
                }
            ]
            if credential is not None
            else []
        )

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
    storage = create_storage(settings)
    source_files: list[tuple[str, bytes]] = []
    for resource in dumped["learning_resources"]:
        zip_path = _resource_zip_path(resource["id"], resource.get("source_filename"))
        entry = {
            "resource_id": resource["id"],
            "filename": resource.get("source_filename"),
            "size_bytes": resource.get("size_bytes", 0),
            "kind": resource.get("resource_type"),
            "storage_key": resource.get("storage_key"),
            "zip_path": zip_path,
            "available": False,
        }
        if resource.get("storage_key"):
            try:
                source_data = await storage.load(resource["storage_key"])
            except Exception:  # noqa: BLE001 - one missing object must not abort export
                entry["error"] = "SOURCE_NOT_FOUND"
                log.warning("export_resource_source_missing", resource_id=resource["id"])
            else:
                entry["available"] = True
                source_files.append((zip_path, source_data))
        else:
            entry["error"] = "SOURCE_NOT_FOUND"
        uploads.append(entry)

    for imported in dumped["error_imports"]:
        zip_path = _error_import_zip_path(imported["id"], imported.get("filename"))
        entry = {
            "error_import_id": imported["id"],
            "filename": imported["filename"],
            "size_bytes": imported["size_bytes"],
            "kind": imported["source_type"],
            "storage_key": imported["storage_key"],
            "zip_path": zip_path,
            "available": False,
        }
        if imported["storage_key"]:
            try:
                source_data = await storage.load(imported["storage_key"])
            except Exception:  # noqa: BLE001 - 一个对象缺失不应中断整份导出
                entry["error"] = "SOURCE_NOT_FOUND"
                log.warning("export_source_missing", error_import_id=imported["id"])
            else:
                entry["available"] = True
                source_files.append((zip_path, source_data))
        else:
            entry["error"] = "SOURCE_NOT_FOUND"
        uploads.append(entry)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("profile.json", _dumps(profile))
        for name, rows in dumped.items():
            zf.writestr(f"tables/{name}.json", _dumps(rows))
        zf.writestr("chunks.json", _dumps(chunk_rows))
        zf.writestr("uploads_manifest.json", _dumps(uploads))
        for zip_path, source_data in source_files:
            zf.writestr(zip_path, source_data)
    data = buf.getvalue()

    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    path = f"exports/{tenant_id}/{ts}.zip"
    await storage.save(path, data)

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
