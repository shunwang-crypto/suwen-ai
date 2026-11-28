from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Literal

import xxhash
from eduagents_core.logging import get_logger
from eduagents_core.mastery.thresholds import VALID_MASTERY_EVIDENCE_TYPES
from eduagents_core.parsing.web import WebFetchError, fetch_and_extract
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import delete, func, select, update

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..kb_kc_graph import build_kb_kc_graph
from ..kc_hierarchy import resolve_kc_meta
from ..models import (
    Chunk,
    Document,
    KcEdge,
    KnowledgeBase,
    KnowledgeComponent,
    LearningResource,
    MasteryLog,
    MasteryRecord,
    Org,
    PendingCleanup,
    Plan,
    ResourceQuestion,
)
from ..plan_adjust import plan_progress
from ..schemas import (
    ChunkContextItem,
    ChunkContextOut,
    ChunkOut,
    DocumentOut,
    KbCreate,
    KbOut,
    KbSummary,
    RetrieveDebugIn,
    UrlImportIn,
)
from ..uploads import read_upload_capped

router = APIRouter(prefix="/api/kb", tags=["kb"])
log = get_logger("kb")

# 上传安全: 后缀白名单 + 魔数校验 (S 级约束, §8)
_ALLOWED_SUFFIXES = {".pdf", ".docx", ".pptx", ".md", ".txt"}
_MAGIC = {
    ".pdf": (b"%PDF",),
    ".docx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
}
_READY_DOCUMENT_STATUSES = frozenset({"ready", "partial_ready", "enriched"})
_ENRICH_CLAIM_STALE_SECONDS = 30 * 60


def _course_next_action(
    *, source_total: int, source_ready: int, source_processing: int, source_failed: int, plan_status: str | None
) -> str:
    if source_total == 0:
        return "upload_source"
    if source_ready == 0 and source_processing > 0:
        return "wait_for_sources"
    if source_ready == 0 and source_failed == source_total:
        return "retry_source"
    if plan_status == "active":
        return "continue_plan"
    return "create_plan"


def _suffix(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx >= 0 else ""


async def _read_upload_capped(file: UploadFile, max_mb: int) -> bytes:
    """分块限流读的本地入口 (P1-1): 语义不变, 实现收敛到 uploads.read_upload_capped。"""
    return await read_upload_capped(file, max_mb * 1024 * 1024)


def _quota_used_bytes_stmt():
    """配额占用 = 非 failed 文档的字节和 (P1-6): 摄取失败的文件不再永久吃配额。"""
    return select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
        Document.status != "failed"
    )


def _validate_upload(filename: str, head: bytes, size: int, max_mb: int) -> str:
    suffix = _suffix(filename)
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="UNSUPPORTED_FILE_TYPE")
    if size > max_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail="FILE_TOO_LARGE")
    if size == 0:
        raise HTTPException(status_code=400, detail="EMPTY_FILE")
    magics = _MAGIC.get(suffix)
    if magics and not any(head.startswith(m) for m in magics):
        # 伪装 MIME/后缀的文件在此拦截 (F2-A2)
        raise HTTPException(status_code=400, detail="FILE_SIGNATURE_MISMATCH")
    if suffix in (".md", ".txt"):
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            # 采样窗口可能把多字节字符切半: 仅当错误不在末尾截断区 (最后3字节)
            # 时才判定为伪装的二进制文件
            if exc.start < len(head) - 3:
                raise HTTPException(
                    status_code=400, detail="FILE_SIGNATURE_MISMATCH"
                ) from None
    return suffix


# ---- 机构库权限 (Wave3, D14): 成员只读, 仅 org admin 可写; service 层与 RLS 双拦截 ----


async def _resolve_kb(ctx: AuthContext, kb_id: uuid.UUID) -> KnowledgeBase:
    """scoped 读会话解析 kb (个人 owner 匹配 OR owner ∈ org_ids); 不可见 -> 404。"""
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        session.expunge(kb)
        return kb


def _require_kb_write(ctx: AuthContext, kb: KnowledgeBase) -> None:
    """机构库写操作 (上传/删除/重建索引/检索调试) 需 org admin; member -> 403。"""
    if kb.owner_type == "org" and str(kb.tenant_id) not in ctx.admin_org_ids:
        raise HTTPException(status_code=403, detail="ORG_READONLY")


@router.post("", response_model=KbOut, status_code=201)
async def create_kb(body: KbCreate, ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        kb = KnowledgeBase(tenant_id=ctx.tenant_id, name=body.name, subject=body.subject)
        session.add(kb)
        await session.flush()
        return KbOut(
            id=kb.id, name=kb.name, subject=kb.subject,
            kb_version=kb.kb_version, created_at=kb.created_at,
        )


@router.get("", response_model=list[KbOut])
async def list_kbs(ctx: AuthContext = Depends(api_rate_limited)):
    """个人库 + 已加入机构的机构库 (org_ids GUC 放行), 带 org 名与可写标记。"""
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        rows = (await session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at))).all()
        org_uuids = {r.tenant_id for r in rows if r.owner_type == "org"}
        org_names: dict[uuid.UUID, str] = {}
        if org_uuids:
            orgs = (await session.scalars(select(Org).where(Org.id.in_(org_uuids)))).all()
            org_names = {o.id: o.name for o in orgs}
        kb_ids = [row.id for row in rows]
        document_counts: dict[uuid.UUID, dict[str, int | datetime | None]] = {}
        question_counts: dict[uuid.UUID, dict[str, int]] = {}
        latest_plans: dict[uuid.UUID, Plan] = {}
        if kb_ids:
            for kb_id, status, count, latest_at in (
                await session.execute(
                    select(
                        Document.kb_id,
                        Document.status,
                        func.count(Document.id),
                        func.max(Document.updated_at),
                    )
                    .where(Document.kb_id.in_(kb_ids))
                    .group_by(Document.kb_id, Document.status)
                )
            ).all():
                bucket = document_counts.setdefault(
                    kb_id,
                    {"total": 0, "ready": 0, "processing": 0, "failed": 0, "latest_at": None},
                )
                bucket["total"] = int(bucket["total"] or 0) + int(count)
                if status in _READY_DOCUMENT_STATUSES:
                    bucket["ready"] = int(bucket["ready"] or 0) + int(count)
                elif status == "failed":
                    bucket["failed"] = int(bucket["failed"] or 0) + int(count)
                else:
                    bucket["processing"] = int(bucket["processing"] or 0) + int(count)
                current_latest = bucket["latest_at"]
                if latest_at is not None and (
                    current_latest is None or latest_at > current_latest
                ):
                    bucket["latest_at"] = latest_at
            for kb_id, verification_status, count in (
                await session.execute(
                    select(
                        LearningResource.linked_kb_id,
                        ResourceQuestion.verification_status,
                        func.count(ResourceQuestion.id),
                    )
                    .join(ResourceQuestion, ResourceQuestion.resource_id == LearningResource.id)
                    .where(
                        LearningResource.owner_id == ctx.user_id,
                        LearningResource.linked_kb_id.in_(kb_ids),
                    )
                    .group_by(
                        LearningResource.linked_kb_id,
                        ResourceQuestion.verification_status,
                    )
                )
            ).all():
                if kb_id is None:
                    continue
                bucket = question_counts.setdefault(kb_id, {"verified": 0, "pending": 0})
                key = "verified" if verification_status == "verified" else "pending"
                bucket[key] += int(count)
            plan_rows = (
                await session.scalars(
                    select(Plan)
                    .where(Plan.user_id == ctx.user_id, Plan.kb_id.in_(kb_ids))
                    .order_by(Plan.created_at.desc())
                )
            ).all()
            for plan in plan_rows:
                if plan.kb_id is not None:
                    latest_plans.setdefault(plan.kb_id, plan)

        def summary_for(kb_id: uuid.UUID) -> KbSummary:
            documents = document_counts.get(
                kb_id,
                {"total": 0, "ready": 0, "processing": 0, "failed": 0, "latest_at": None},
            )
            questions = question_counts.get(kb_id, {"verified": 0, "pending": 0})
            plan = latest_plans.get(kb_id)
            progress = plan_progress(plan.structure_json) if plan is not None else {"done": 0, "total": 0, "ratio": 0.0}
            latest_at = documents["latest_at"]
            if plan is not None and (latest_at is None or plan.created_at > latest_at):
                latest_at = plan.created_at
            total = int(documents["total"] or 0)
            ready = int(documents["ready"] or 0)
            processing = int(documents["processing"] or 0)
            failed = int(documents["failed"] or 0)
            return KbSummary(
                source_total=total,
                source_ready=ready,
                source_processing=processing,
                source_failed=failed,
                verified_questions=questions["verified"],
                pending_questions=questions["pending"],
                plan_id=plan.id if plan is not None else None,
                plan_status=plan.status if plan is not None else None,
                plan_done_tasks=int(progress["done"]),
                plan_total_tasks=int(progress["total"]),
                plan_progress_ratio=float(progress["ratio"]),
                last_activity_at=latest_at,
                next_action=_course_next_action(
                    source_total=total,
                    source_ready=ready,
                    source_processing=processing,
                    source_failed=failed,
                    plan_status=plan.status if plan is not None else None,
                ),
            )
        return [
            KbOut(
                id=r.id, name=r.name, subject=r.subject,
                kb_version=r.kb_version, created_at=r.created_at,
                owner_type=r.owner_type,
                org_id=r.tenant_id if r.owner_type == "org" else None,
                org_name=org_names.get(r.tenant_id) if r.owner_type == "org" else None,
                writable=r.owner_type == "user"
                or str(r.tenant_id) in ctx.admin_org_ids,
                summary=summary_for(r.id),
            )
            for r in rows
        ]


@router.delete("/{kb_id}", status_code=204)
async def delete_kb(
    kb_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    kb = await _resolve_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)
    owner_id = kb.tenant_id  # owner_id 语义 (D14): 个人=user_id, 机构=org_id
    async with tenant_session(owner_id) as session:
        await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        # P1-5 outbox: 物理清理任务与删除同事务提交 (worker 每分钟消费),
        # 替代"先 COMMIT 后 enqueue" —— Redis 抖动不再产生永久孤儿
        session.add(
            PendingCleanup(
                kind="kb", payload={"tenant_id": str(owner_id), "kb_id": str(kb_id)}
            )
        )
    # P0-3: L2 语义缓存立即清该库点位 (L1 键含 kb_version 且查缓存前有 PG 可见性
    # 校验, 库已删 -> 404 到不了缓存, 无需清)。缓存栈缺失/异常不阻断删除。
    cache = getattr(request.app.state, "answer_cache", None)
    if cache is not None:
        try:
            await cache.delete_by_kb(str(owner_id), str(kb_id))
        except Exception:  # noqa: BLE001 — 点位仍受可见性校验保护, 只记日志
            log.exception("answer_cache_delete_by_kb_failed", kb_id=str(kb_id))


@router.post("/{kb_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile,
    request: Request,
    enrichment: Literal["semantic", "headings"] = "semantic",
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    data = await _read_upload_capped(file, settings.max_upload_mb)
    filename = file.filename or "unnamed"
    _validate_upload(filename, data[:16], len(data), settings.max_upload_mb)
    content_hash = xxhash.xxh64_hexdigest(data)

    kb = await _resolve_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)  # 机构库: 仅 org admin 可上传 (member 403)
    owner_id = kb.tenant_id
    # 存储配额上限: 个人库按全局设置, 机构库按 orgs.storage_quota_mb
    quota_mb = settings.max_storage_mb_per_user
    if kb.owner_type == "org":
        async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
            org = await session.get(Org, owner_id)
            if org is not None:
                quota_mb = org.storage_quota_mb

    # 写事务以 owner 开启: RLS 写策略 owner 匹配; 配额统计天然按 owner 聚合
    async with tenant_session(owner_id) as session:
        # 配额 (真实运营约束, §3.0)
        doc_count = await session.scalar(
            select(func.count()).select_from(Document)
        )
        if doc_count >= settings.max_docs_per_user:
            raise HTTPException(status_code=400, detail="DOC_QUOTA_EXCEEDED")
        used_bytes = (await session.scalar(_quota_used_bytes_stmt())) or 0
        if used_bytes + len(data) > quota_mb * 1024 * 1024:
            raise HTTPException(status_code=400, detail="STORAGE_QUOTA_EXCEEDED")
        # 文件级去重 (§5.1): 同库同哈希 -> 幂等返回已有文档
        existing = await session.scalar(
            select(Document).where(
                Document.kb_id == kb_id, Document.content_hash == content_hash
            )
        )
        if existing is not None:
            return DocumentOut.model_validate(existing, from_attributes=True)
        doc = Document(
            tenant_id=owner_id,
            kb_id=kb_id,
            filename=filename,
            content_hash=content_hash,
            size_bytes=len(data),
            status="pending",
            uploaded_by=ctx.user_id,  # 最小审计: 机构库演示"谁传的"
        )
        session.add(doc)
        await session.flush()
        ingest_job_id = f"ingest:{doc.id}:v{doc.version}"
        doc.recognition_meta_json = {
            "enrichment_policy": enrichment,
            "ingest_job_id": ingest_job_id,
            "ingest_document_version": doc.version,
            "ingest_job_state": "queued",
        }
        doc_out = DocumentOut.model_validate(doc, from_attributes=True)

    storage = request.app.state.storage
    await storage.save(f"{owner_id}/{kb_id}/{doc_out.id}/{filename}", data)
    try:
        job = await request.app.state.arq.enqueue_job(
            "ingest_document",
            str(owner_id),
            str(doc_out.id),
            ingest_job_id,
            _job_id=ingest_job_id,
        )
        if job is None:
            raise RuntimeError("ingestion job was not enqueued")
    except Exception as exc:
        async with tenant_session(owner_id) as session:
            failed_doc = await session.get(Document, doc_out.id, with_for_update=True)
            if failed_doc is not None:
                failed_doc.status = "failed"
                failed_doc.error_msg = "文档处理任务提交失败，请重新上传"
                failed_doc.recognition_meta_json = {"enrichment_policy": enrichment}
        raise HTTPException(status_code=503, detail="BACKGROUND_JOB_UNAVAILABLE") from exc
    log.info("document_uploaded", doc_id=str(doc_out.id), size=len(data))
    return doc_out


# ---- URL 网页导入 (对标 ima/元宝网页剪藏; 抓取→对象存储→复用两阶段摄取流水线) ----

_FILENAME_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_md_filename(title: str) -> str:
    """网页标题 -> 安全的 .md 文件名 (路径分隔符/控制字符剔除, 截断防超长)。"""
    cleaned = _FILENAME_UNSAFE_RE.sub(" ", title or "")
    cleaned = " ".join(cleaned.split()).strip(". ")
    if not cleaned:
        cleaned = "网页剪藏"
    return f"{cleaned[:120]}.md"


@router.post("/{kb_id}/documents/from-url", response_model=DocumentOut, status_code=201)
async def import_document_from_url(
    kb_id: uuid.UUID,
    body: UrlImportIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    """粘贴网址导入: SSRF 校验后抓正文转 markdown, 建 Document 行入 ARQ 摄取队列。

    权限/配额与文件上传路径完全一致 (机构库仅 org admin 可导入)。
    """
    kb = await _resolve_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)
    owner_id = kb.tenant_id

    try:
        title, markdown = await fetch_and_extract(
            body.url, allow_private=settings.web_import_allow_private_hosts
        )
    except WebFetchError as exc:
        log.warning("web_import_rejected", code=exc.code, url=body.url[:200])
        raise HTTPException(status_code=400, detail=exc.code) from None

    data = markdown.encode("utf-8")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail="FILE_TOO_LARGE")
    filename = _safe_md_filename(title)
    content_hash = xxhash.xxh64_hexdigest(data)

    quota_mb = settings.max_storage_mb_per_user
    if kb.owner_type == "org":
        async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
            org = await session.get(Org, owner_id)
            if org is not None:
                quota_mb = org.storage_quota_mb

    async with tenant_session(owner_id) as session:
        doc_count = await session.scalar(select(func.count()).select_from(Document))
        if doc_count >= settings.max_docs_per_user:
            raise HTTPException(status_code=400, detail="DOC_QUOTA_EXCEEDED")
        used_bytes = (await session.scalar(_quota_used_bytes_stmt())) or 0
        if used_bytes + len(data) > quota_mb * 1024 * 1024:
            raise HTTPException(status_code=400, detail="STORAGE_QUOTA_EXCEEDED")
        # 同库同哈希幂等 (重复粘同一网址且正文未变 -> 返回已有文档)
        existing = await session.scalar(
            select(Document).where(
                Document.kb_id == kb_id, Document.content_hash == content_hash
            )
        )
        if existing is not None:
            return DocumentOut.model_validate(existing, from_attributes=True)
        doc = Document(
            tenant_id=owner_id,
            kb_id=kb_id,
            filename=filename,
            content_hash=content_hash,
            size_bytes=len(data),
            status="pending",
            parse_backend="web",  # 来源标记: 与文件上传路径区分
            uploaded_by=ctx.user_id,
        )
        session.add(doc)
        await session.flush()
        ingest_job_id = f"ingest:{doc.id}:v{doc.version}"
        doc.recognition_meta_json = {
            "ingest_job_id": ingest_job_id,
            "ingest_document_version": doc.version,
            "ingest_job_state": "queued",
        }
        doc_out = DocumentOut.model_validate(doc, from_attributes=True)

    storage = request.app.state.storage
    await storage.save(f"{owner_id}/{kb_id}/{doc_out.id}/{filename}", data)
    try:
        job = await request.app.state.arq.enqueue_job(
            "ingest_document",
            str(owner_id),
            str(doc_out.id),
            ingest_job_id,
            _job_id=ingest_job_id,
        )
        if job is None:
            raise RuntimeError("ingestion job was not enqueued")
    except Exception as exc:
        async with tenant_session(owner_id) as session:
            failed_doc = await session.get(Document, doc_out.id, with_for_update=True)
            if failed_doc is not None:
                failed_doc.status = "failed"
                failed_doc.error_msg = "文档处理任务提交失败，请重新导入"
                failed_doc.recognition_meta_json = {}
        raise HTTPException(status_code=503, detail="BACKGROUND_JOB_UNAVAILABLE") from exc
    log.info("document_imported_from_url", doc_id=str(doc_out.id), size=len(data))
    return doc_out


@router.get("/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)
):
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        rows = (
            await session.scalars(
                select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at)
            )
        ).all()
        return [DocumentOut.model_validate(r, from_attributes=True) for r in rows]


@router.delete("/{kb_id}/documents/{doc_id}", status_code=204)
async def delete_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    kb = await _resolve_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)
    owner_id = kb.tenant_id
    async with tenant_session(owner_id) as session:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        filename = doc.filename
        await session.execute(delete(Document).where(Document.id == doc_id))
        # P0-3: kb_version 自增 -> 答案缓存 L1 键与 L2 filter 同时落空,
        # 被删文档的原文摘录不可能再从缓存吐出
        await session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id)
            .values(kb_version=KnowledgeBase.kb_version + 1)
        )
        # P1-5 outbox: 向量/对象存储清理与删除同事务提交 (F2-A3 语义不变:
        # 行删除即立即不可见, 物理清理由 worker 每分钟消费)
        session.add(
            PendingCleanup(
                kind="doc",
                payload={
                    "tenant_id": str(owner_id),
                    "kb_id": str(kb_id),
                    "doc_id": str(doc_id),
                    "filename": filename,
                },
            )
        )


@router.post("/{kb_id}/documents/{doc_id}/reindex", status_code=202)
async def reindex_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    kb = await _resolve_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)
    owner_id = kb.tenant_id
    reindex_version: int | None = None
    ingest_job_id: str | None = None
    async with tenant_session(owner_id) as session:
        doc = await session.get(Document, doc_id, with_for_update=True)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        # Reindex advances the document generation. Any in-flight ingestion or
        # enrichment carrying the previous version must lose all write rights.
        meta = dict(doc.recognition_meta_json or {})
        for key in (
            "enrich_job_id",
            "enrich_previous_status",
            "enrich_started_at",
            "enrich_job_state",
            "enrich_document_version",
        ):
            meta.pop(key, None)
        doc.status = "pending"
        doc.error_msg = None
        doc.version += 1
        ingest_job_id = f"ingest:{doc.id}:v{doc.version}"
        reindex_version = int(doc.version)
        meta["ingest_job_id"] = ingest_job_id
        meta["ingest_document_version"] = doc.version
        meta["ingest_job_state"] = "queued"
        doc.recognition_meta_json = meta
        if doc.parse_backend.startswith("pdf-"):
            doc.parse_backend = ""
        await session.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        # P0-3: 重建索引会改变分块/向量, 旧答案缓存必须随之失效
        await session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id)
            .values(kb_version=KnowledgeBase.kb_version + 1)
        )
    try:
        job = await request.app.state.arq.enqueue_job(
            "ingest_document",
            str(owner_id),
            str(doc_id),
            ingest_job_id,
            _job_id=ingest_job_id,
        )
        if job is None:
            raise RuntimeError("ingestion job was not enqueued")
    except Exception as exc:
        async with tenant_session(owner_id) as session:
            failed_doc = await session.get(Document, doc_id, with_for_update=True)
            failed_meta = dict(failed_doc.recognition_meta_json or {}) if failed_doc else {}
            if (
                failed_doc is not None
                and reindex_version is not None
                and failed_doc.version == reindex_version
                and failed_meta.get("ingest_job_id") == ingest_job_id
            ):
                failed_doc.status = "failed"
                failed_doc.error_msg = "文档处理任务提交失败，请稍后重试"
                failed_doc.recognition_meta_json = {
                    key: value for key, value in failed_meta.items() if not key.startswith("ingest_")
                }
        raise HTTPException(status_code=503, detail="BACKGROUND_JOB_UNAVAILABLE") from exc
    return {"status": "queued"}


@router.post("/{kb_id}/documents/{doc_id}/re-enrich", status_code=202)
async def re_enrich_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """重新触发知识增强 (T0-1), 采用原子抢占 + stale-while-revalidate。

    已经成功增强的文档在刷新期间仍保留旧 KC/分块可用；只有新版本成功写完后才
    进入新的 ``enriched`` 状态。重复点击不会重复入队，队列失败会恢复到刷新前的
    可用状态。``job_id`` 写入文档元数据，保证同一轮刷新可重试/重放时使用同一 ID。
    """
    kb = await _resolve_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)
    owner_id = kb.tenant_id
    job_id: str | None = None
    fallback_status = "ready"
    async with tenant_session(owner_id, org_ids=ctx.org_ids) as session:
        doc = await session.get(Document, doc_id, with_for_update=True)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        meta = dict(doc.recognition_meta_json or {})
        current_job_id = meta.get("enrich_job_id")
        if current_job_id or doc.status == "enriching":
            # Worker 崩溃后 claim 可能永久残留。超过回收窗口才允许换代；
            # 活跃 claim 仍保持幂等，不创建第二个模型任务。
            stale = not current_job_id or not meta.get("enrich_started_at")
            raw_started = meta.get("enrich_started_at")
            if current_job_id and raw_started:
                try:
                    started_at = datetime.fromisoformat(str(raw_started))
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=UTC)
                    stale = (
                        datetime.now(UTC) - started_at
                    ).total_seconds() > _ENRICH_CLAIM_STALE_SECONDS
                except (TypeError, ValueError):
                    stale = True
            if not stale:
                return {
                    "status": "already_running",
                    "job_id": str(current_job_id) if current_job_id else None,
                }
            for key in (
                "enrich_job_id",
                "enrich_previous_status",
                "enrich_started_at",
                "enrich_job_state",
                "enrich_document_version",
            ):
                meta.pop(key, None)
            doc.recognition_meta_json = meta
            if doc.status == "enriching":
                doc.status = "ready"
        if doc.status not in ("ready", "partial_ready", "enriched"):
            # 摄取尚未完成 (pending/parsing/…) 或摄取失败 (failed): 还不能增强
            raise HTTPException(status_code=409, detail="DOC_NOT_READY")
        fallback_status = doc.status
        job_id = f"enrich:{doc.id}:{uuid.uuid4().hex}"
        meta = dict(doc.recognition_meta_json or {})
        meta["enrich_job_id"] = job_id
        meta["enrich_previous_status"] = fallback_status
        meta["enrich_started_at"] = datetime.now(UTC).isoformat()
        meta["enrich_document_version"] = int(doc.version)
        meta["enrich_job_state"] = "queued"
        # 先在同一事务内抢占，两个并发请求最多只有一个能进入这里。旧成功版本刷新
        # 时保持 enriched，对 Quiz/检索继续可用；首次增强才显示 enriching。
        if fallback_status != "enriched":
            doc.status = "enriching"
        doc.error_msg = None
        doc.recognition_meta_json = meta
    try:
        job = await request.app.state.arq.enqueue_job(
            "enrich_document",
            str(owner_id),
            str(doc_id),
            None,
            str(ctx.user_id),
            job_id,
            _job_id=job_id,
        )
        if job is None:
            raise RuntimeError("enrichment job already exists or was not enqueued")
    except Exception as exc:
        # 入队失败不能把文档永久卡在 enriching；恢复旧成功版本仍可检索/出题。
        async with tenant_session(owner_id, org_ids=ctx.org_ids) as session:
            doc = await session.get(Document, doc_id, with_for_update=True)
            if doc is not None:
                meta = dict(doc.recognition_meta_json or {})
                if meta.get("enrich_job_id") == job_id:
                    meta.pop("enrich_job_id", None)
                    meta.pop("enrich_previous_status", None)
                    meta.pop("enrich_started_at", None)
                    meta.pop("enrich_document_version", None)
                    meta.pop("enrich_job_state", None)
                    doc.recognition_meta_json = meta
                    doc.status = fallback_status
                    doc.error_msg = "知识增强任务提交失败，请稍后重试"
        log.exception("enrich_enqueue_failed", doc_id=str(doc_id), job_id=job_id)
        raise HTTPException(status_code=503, detail="BACKGROUND_JOB_UNAVAILABLE") from exc
    return {"status": "queued", "job_id": job_id}


@router.post("/{kb_id}/debug/retrieve")
async def debug_retrieve(
    kb_id: uuid.UUID,
    body: RetrieveDebugIn,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """检索调试面板 (F2): dense/sparse 召回、融合重排后的结果与分数全透明。"""
    retrieval = getattr(request.app.state, "retrieval", None)
    if retrieval is None:
        raise HTTPException(status_code=503, detail="RETRIEVAL_UNAVAILABLE")
    kb = await _resolve_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)  # 调试面板与写操作同权限 (机构库仅 admin)
    # tenant 过滤按 owner_id: 机构库的 Qdrant 点位 tenant_id=org_id (D14)
    result = await retrieval.retrieve(str(kb.tenant_id), str(kb_id), body.query, debug=True)
    stages = result.stages or {}
    return {
        "query": body.query,
        "dense_hits": result.dense_hits,
        "sparse_hits": result.sparse_hits,
        "used_sparse": result.used_sparse,
        "used_rerank": result.used_rerank,
        "confident": result.confident,
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "score": c.score,
                "rerank_score": c.rerank_score,
                "source": c.source,
                "heading_path": c.heading_path,
                "page_start": c.page_start,
                "text": c.text[:300],
            }
            for c in result.chunks
        ],
        # 四阶段明细 (命中测试页): dense/sparse 召回 → RRF 融合 → rerank 终排
        "stages": {
            "dense": stages.get("dense", []),
            "sparse": stages.get("sparse", []),
            "fused": stages.get("fused", []),
            "reranked": stages.get("reranked", []),
            "chunk_meta": {
                cid: {**meta, "text": (meta.get("text") or "")[:160]}
                for cid, meta in (stages.get("chunk_meta") or {}).items()
            },
        },
    }


@router.get("/{kb_id}/documents/{doc_id}/chunks", response_model=list[ChunkOut])
async def preview_chunks(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    offset: int = 0,
    limit: int = 50,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """分块预览器 (F2): 每个 chunk 的文本/页码/heading_path/向量化状态。"""
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        rows = (
            await session.scalars(
                select(Chunk)
                .where(Chunk.doc_id == doc_id)
                .order_by(Chunk.created_at, Chunk.id)
                .offset(offset)
                .limit(min(limit, 200))
            )
        ).all()
        return [ChunkOut.model_validate(r, from_attributes=True) for r in rows]


# ---- 库级知识图 (Learning Map full 模式) ----
# 计划级依赖图在 routers/plans（"下一步学什么"）；这里是"我在整门课的什么位置"。
# 只读派生，不落新表；掌握度数值不出境，只给 evidence_state 与定性档。


@router.get("/{kb_id}/kc-graph")
async def get_kb_kc_graph(
    kb_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """该库全部 KC + 全部先修边 + 章节聚合 + 本人的定性掌握档。

    章节由 kc_hierarchy 确定性推导（带 5 分钟缓存）；掌握档只在证据充分
    （evidence_state=observed）时给出，未观测/估计中不叙述成薄弱或已掌握。

    节点序 = ``(created_at, id)``：enrich 批量写入时同批 KC 的 created_at 相同，
    因此同批内实际由 id 兜底 —— 它是**确定性可重复**的，但不等于插入顺序；
    展示语义由章节分组承担，不依赖这一层顺序。
    """
    kb = await _resolve_kb(ctx, kb_id)
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        kc_rows = (
            await session.execute(
                select(KnowledgeComponent.id, KnowledgeComponent.canonical_name)
                .where(
                    KnowledgeComponent.kb_id == kb_id,
                    KnowledgeComponent.superseded_by_id.is_(None),
                )
                .order_by(KnowledgeComponent.created_at.asc(), KnowledgeComponent.id.asc())
            )
        ).all()
        kc_ids = [str(kc_id) for kc_id, _name in kc_rows]
        edge_rows: list[tuple[object, object]] = []
        mastery_rows: list[tuple[object, float, int]] = []
        if kc_ids:
            edge_rows = list(
                (
                    await session.execute(
                        select(KcEdge.from_kc, KcEdge.to_kc).where(
                            KcEdge.type == "prerequisite",
                            KcEdge.from_kc.in_([uuid.UUID(x) for x in kc_ids]),
                            KcEdge.to_kc.in_([uuid.UUID(x) for x in kc_ids]),
                        )
                    )
                ).all()
            )
            mastery_rows = list(
                (
                    await session.execute(
                        select(
                            MasteryRecord.kc_id,
                            MasteryRecord.p_mastery,
                            func.count(func.distinct(MasteryLog.evidence_id)),
                        )
                        .outerjoin(
                            MasteryLog,
                            (MasteryLog.user_id == MasteryRecord.user_id)
                            & (MasteryLog.kc_id == MasteryRecord.kc_id)
                            & MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES),
                        )
                        .where(
                            MasteryRecord.user_id == ctx.user_id,
                            MasteryRecord.kc_id.in_([uuid.UUID(x) for x in kc_ids]),
                        )
                        .group_by(MasteryRecord.kc_id, MasteryRecord.p_mastery)
                    )
                ).all()
            )
    meta = await resolve_kc_meta(
        ctx.tenant_id,
        kc_ids,
        redis=request.app.state.redis,
        org_ids=ctx.org_ids,
    )
    chapter_by_kc = {kc_id: (info or {}).get("chapter") or "" for kc_id, info in meta.items()}
    graph = build_kb_kc_graph(list(kc_rows), edge_rows, chapter_by_kc, mastery_rows)
    return {"kb_id": str(kb_id), "kb_name": kb.name, **graph}


# ---- 学习指南 (对标 NotebookLM Source guide / Studio: 摄取完成后"下一步干嘛") ----
# 存储决策 (D16): 不加新表, 聚合到 kb 级 —— knowledge_bases.config_json["study_guide"]。


@router.get("/{kb_id}/study-guide")
async def get_study_guide(kb_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)):
    """读取该库的学习指南; 未生成时 guide=None (前端据此显示"生成学习指南")。"""
    kb = await _resolve_kb(ctx, kb_id)
    config = kb.config_json or {}
    guide = config.get("study_guide")
    generation = config.get("study_guide_generation")
    if not isinstance(generation, dict):
        generation = {"status": "completed" if guide else "idle"}
    writable = kb.owner_type == "user" or str(kb.tenant_id) in ctx.admin_org_ids
    return {
        "kb_id": str(kb_id),
        "guide": guide,
        "generation": generation,
        "writable": writable,
    }


@router.post("/{kb_id}/study-guide/rebuild", status_code=202)
async def rebuild_study_guide(
    kb_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    """手动重建 (自动触发在 enrich 完成后, 见 worker/enrichment.py)。"""
    kb = await _resolve_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)
    owner_id = kb.tenant_id
    job_id = f"study-guide:{kb_id}:{uuid.uuid4().hex}"
    now = datetime.now(UTC).isoformat()
    async with tenant_session(owner_id) as session:
        row = await session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id).with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        config = dict(row.config_json or {})
        current = config.get("study_guide_generation")
        if isinstance(current, dict) and current.get("status") in {"queued", "running"}:
            return {
                "status": current["status"],
                "job_id": current.get("job_id"),
            }
        config["study_guide_generation"] = {
            "status": "queued",
            "job_id": job_id,
            "requested_at": now,
        }
        row.config_json = config
    try:
        job = await request.app.state.arq.enqueue_job(
            "build_study_guide", str(owner_id), str(kb_id), _job_id=job_id
        )
        if job is None:
            raise RuntimeError("study guide job was not enqueued")
    except Exception as exc:
        async with tenant_session(owner_id) as session:
            row = await session.get(KnowledgeBase, kb_id)
            if row is not None:
                config = dict(row.config_json or {})
                current = config.get("study_guide_generation")
                if isinstance(current, dict) and current.get("job_id") == job_id:
                    config["study_guide_generation"] = {
                        **current,
                        "status": "failed",
                        "finished_at": datetime.now(UTC).isoformat(),
                        "message": "后台任务提交失败，请稍后重试",
                    }
                    row.config_json = config
        log.exception("study_guide_enqueue_failed", kb_id=str(kb_id))
        raise HTTPException(status_code=503, detail="BACKGROUND_JOB_UNAVAILABLE") from exc
    log.info("study_guide_rebuild_queued", kb_id=str(kb_id), job_id=job_id)
    return {"status": "queued", "job_id": job_id}


def _context_item(c: Chunk) -> ChunkContextItem:
    return ChunkContextItem(
        id=c.id,
        text=c.text,
        heading_path=c.heading_path,
        page_start=c.page_start,
        page_end=c.page_end,
    )


@router.get("/{kb_id}/chunks/{chunk_id}/context", response_model=ChunkContextOut)
async def chunk_context(
    kb_id: uuid.UUID,
    chunk_id: uuid.UUID,
    window: int = 1,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """引用回原文定位 (包A): 命中 chunk 全文 + 同 parent 前后各 window 个相邻子块
    + 父块全文。跨租户 chunk 在 RLS 下不可见 -> 404 (不泄漏存在性); 再校验属于该 kb。
    """
    window = max(0, min(window, 5))
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await session.get(Chunk, chunk_id)
        if row is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        doc = await session.get(Document, row.doc_id)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        parent = (
            await session.get(Chunk, row.parent_id)
            if row.parent_id is not None
            else None
        )
        prev_rows: list[Chunk] = []
        next_rows: list[Chunk] = []
        if parent is not None:
            siblings = (
                await session.scalars(
                    select(Chunk).where(Chunk.parent_id == parent.id)
                )
            ).all()
            parent_text = parent.text

            # 子块无独立序号列, 且同事务入库的 created_at 全部相同; 子块按父块
            # 文本顺序切出 (chunking/structural.py), 以其在父块全文中的起始偏移
            # 复原文档序, 找不到的排尾, id 作稳定 tiebreaker。
            def _pos(c: Chunk) -> tuple[int, str]:
                at = parent_text.find(c.text)
                return (at if at >= 0 else len(parent_text) + 1, str(c.id))

            ordered = sorted(siblings, key=_pos)
            idx = next(i for i, c in enumerate(ordered) if c.id == row.id)
            prev_rows = ordered[max(0, idx - window) : idx]
            next_rows = ordered[idx + 1 : idx + 1 + window]
        return ChunkContextOut(
            chunk=_context_item(row),
            doc_id=doc.id,
            doc_name=doc.filename,
            prev=[_context_item(c) for c in prev_rows],
            next=[_context_item(c) for c in next_rows],
            parent_text=parent.text if parent is not None else None,
            parent_heading_path=parent.heading_path if parent is not None else None,
        )
