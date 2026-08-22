from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from sqlalchemy import delete, func, select, update

log = get_logger("ingestion")

# 摄取状态机 (F2 两阶段, Stage 1): pending→parsing→chunking→embedding→ready | failed
# Stage 2 (enriching→enriched: 上下文前缀 + KC 抽取) 在 M2 后半落地。

# P1-6 停滞回收: 摄取中的文档登记在 Redis hash
# (field="{tenant}:{doc}" -> {started_at, job_id, version}),
# reap_stale_ingestion cron 扫 >30 分钟仍停留在中间态的置 failed 并重入队 (≤3 次)。
# worker 被 SIGKILL 时 finally 不执行 → 登记残留 → 恰好被 cron 兜住。
INGEST_WATCH_KEY = "ingest:watch"
INGEST_REAP_RETRY_KEY = "ingest:reap:{doc_id}"
REAP_STALE_SECONDS = 30 * 60
REAP_MAX_RETRIES = 3
_REAP_RETRY_TTL = 48 * 3600
# 摄取中间态 (parsing/chunking/embedding): 停留超时才允许回收
_STUCK_STATUSES = ("parsing", "recognizing", "chunking", "embedding")


class _StaleIngestion(RuntimeError):
    """Raised when a reindex superseded this ingestion attempt."""


_COMPARE_DELETE_WATCH_LUA = """
if redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then
    return redis.call('HDEL', KEYS[1], ARGV[1])
end
return 0
"""


def _owns_ingest_attempt(
    doc: object,
    *,
    expected_version: int | None = None,
    expected_job_id: str | None = None,
) -> bool:
    """Return whether a document row still belongs to one ingestion attempt."""

    if expected_version is not None and int(doc.version) != expected_version:
        return False
    if expected_job_id is not None:
        meta = dict(doc.recognition_meta_json or {})
        return str(meta.get("ingest_job_id") or "") == expected_job_id
    return True


async def _delete_watch_if_current(redis, field: str, expected_value: str | None) -> bool:
    """Atomically delete a watchdog record only when it is still this attempt.

    The Lua path is used by the real Redis client.  The compatibility fallback
    is only for lightweight test doubles that do not implement ``EVAL``; it
    preserves the value comparison but cannot provide Redis' atomicity.
    """

    if expected_value is None:
        return False
    try:
        return bool(
            await redis.eval(
                _COMPARE_DELETE_WATCH_LUA,
                1,
                INGEST_WATCH_KEY,
                field,
                expected_value,
            )
        )
    except (AttributeError, NotImplementedError):
        pass
    except Exception:  # noqa: BLE001 - fall back for Redis-compatible test doubles
        log.warning("ingest_watch_compare_delete_lua_failed", field=field)

    current = await redis.hget(INGEST_WATCH_KEY, field)
    if isinstance(current, bytes):
        current = current.decode()
    if current != expected_value:
        return False
    await redis.hdel(INGEST_WATCH_KEY, field)
    return True


_PDF_ERROR_MESSAGES = {
    "PDF_FILE_TYPE_MISMATCH": "PDF 文件内容与扩展名不一致",
    "PDF_PASSWORD_PROTECTED": "PDF 已加密，请先解除密码保护",
    "PDF_PAGE_LIMIT_EXCEEDED": "PDF 页数超过安全限制",
    "PDF_VISION_PAGE_LIMIT_EXCEEDED": "需要识别的扫描页面超过安全限制，请拆分文件后重试",
    "PDF_RENDER_PIXEL_LIMIT_EXCEEDED": "PDF 页面尺寸超过安全限制",
    "PDF_RENDER_SIZE_LIMIT_EXCEEDED": "PDF 页面渲染结果超过安全限制",
    "PDF_VISION_MODEL_UNAVAILABLE": "当前视觉识别服务不可用，请稍后重试",
    "PDF_DOCUMENT_RECOGNITION_TIMEOUT": "PDF 识别超时，请拆分文件后重试",
    "PDF_DAMAGED": "PDF 已损坏或格式无效",
    "PDF_EMPTY": "PDF 没有可处理的页面",
}


async def _set_status(
    doc_id: str,
    tenant_id: str,
    status: str,
    error: str | None = None,
    *,
    expected_version: int | None = None,
    expected_job_id: str | None = None,
    recognition_meta: dict | None = None,
    parse_backend: str | None = None,
    clear_ingest_claim: bool = False,
    next_ingest_job_id: str | None = None,
) -> bool:
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Document

    async with tenant_session(tenant_id) as session:
        doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
        if doc is None or not _owns_ingest_attempt(
            doc,
            expected_version=expected_version,
            expected_job_id=expected_job_id,
        ):
            return False
        if recognition_meta is not None:
            merged_meta = dict(doc.recognition_meta_json or {})
            merged_meta.update(recognition_meta)
            doc.recognition_meta_json = merged_meta
        if clear_ingest_claim or next_ingest_job_id is not None:
            merged_meta = dict(doc.recognition_meta_json or {})
            for key in (
                "ingest_job_id",
                "ingest_document_version",
                "ingest_started_at",
                "ingest_job_state",
            ):
                merged_meta.pop(key, None)
            if next_ingest_job_id is not None:
                merged_meta.update(
                    {
                        "ingest_job_id": next_ingest_job_id,
                        "ingest_document_version": int(doc.version),
                        "ingest_started_at": time.time(),
                        "ingest_job_state": "queued",
                    }
                )
            doc.recognition_meta_json = merged_meta
        doc.status = status
        doc.error_msg = error
        if parse_backend is not None:
            doc.parse_backend = parse_backend
        return True


async def _set_status_checked(
    doc_id: str,
    tenant_id: str,
    status: str,
    error: str | None = None,
    *,
    expected_version: int,
    expected_job_id: str | None = None,
    recognition_meta: dict | None = None,
    parse_backend: str | None = None,
    clear_ingest_claim: bool = False,
    next_ingest_job_id: str | None = None,
) -> None:
    if not await _set_status(
        doc_id,
        tenant_id,
        status,
        error,
        expected_version=expected_version,
        expected_job_id=expected_job_id,
        recognition_meta=recognition_meta,
        parse_backend=parse_backend,
        clear_ingest_claim=clear_ingest_claim,
        next_ingest_job_id=next_ingest_job_id,
    ):
        raise _StaleIngestion(
            f"document {doc_id} ingestion attempt for version {expected_version} is stale"
        )


async def _document_exists(
    doc_id: str,
    tenant_id: str,
    *,
    expected_version: int | None = None,
    expected_job_id: str | None = None,
) -> bool:
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Document

    async with tenant_session(tenant_id) as session:
        doc = await session.get(Document, uuid.UUID(doc_id))
        return doc is not None and _owns_ingest_attempt(
            doc,
            expected_version=expected_version,
            expected_job_id=expected_job_id,
        )


async def _clear_ingest_claim(
    doc_id: str, tenant_id: str, job_id: str | None, expected_version: int
) -> None:
    """Release a claim only if this worker still owns the document generation."""

    if not job_id:
        return
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Document

    async with tenant_session(tenant_id) as session:
        doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
        if doc is None or not _owns_ingest_attempt(
            doc,
            expected_version=expected_version,
            expected_job_id=job_id,
        ):
            return
        meta = dict(doc.recognition_meta_json or {})
        for key in (
            "ingest_job_id",
            "ingest_document_version",
            "ingest_started_at",
            "ingest_job_state",
        ):
            meta.pop(key, None)
        doc.recognition_meta_json = meta


def _pdf_parse_error(exc: ValueError):
    from eduagents_core.parsing.base import ParseError

    code = str(exc) if str(exc).startswith("PDF_") else "PDF_RECOGNITION_FAILED"
    message = _PDF_ERROR_MESSAGES.get(code, "PDF 解析或识别失败，请稍后重试")
    return ParseError(f"{code}: {message}")


async def _parse_pdf(
    data: bytes,
    *,
    settings,
    gateway,
    tenant_id: str,
    user_id: str,
    doc_id: str,
    document_version: int,
    job_id: str,
):
    from eduagents_core.document_recognition import (
        PdfRecognitionPolicy,
        inspect_pdf,
        recognize_pdf,
    )
    from eduagents_core.parsing.base import ParsedDocument
    from eduagents_core.parsing.md_structure import parse_markdown_elements

    policy = PdfRecognitionPolicy.from_settings(settings)
    try:
        inspection = await asyncio.to_thread(inspect_pdf, data, policy)
        methods = {page.extraction_method for page in inspection.pages}
        needs_vision = bool(methods & {"scanned", "mixed"})
        if needs_vision:
            await _set_status_checked(
                doc_id,
                tenant_id,
                "recognizing",
                expected_version=document_version,
                expected_job_id=job_id,
            )
        recognized = await recognize_pdf(
            data,
            policy=policy,
            gateway=gateway,
            vision_model=(
                settings.vision_model
                if not settings.model_routing_enabled
                else settings.trenddao_vision_model
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            inspection=inspection,
            request_namespace=f"kb:{doc_id}:version:{document_version}",
        )
    except ValueError as exc:
        raise _pdf_parse_error(exc) from exc

    elements = []
    for page in recognized.successful_pages:
        elements.extend(parse_markdown_elements(page.markdown, page=page.page_number))
    if not elements:
        from eduagents_core.parsing.base import ParseError

        await _set_status_checked(
            doc_id,
            tenant_id,
            "failed",
            "PDF_ALL_PAGES_FAILED: 文件没有可提取或识别的内容",
            expected_version=document_version,
            expected_job_id=job_id,
            recognition_meta=recognized.bounded_metadata(),
            parse_backend="pdf-vision" if methods <= {"scanned", "failed"} else "pdf-mixed",
        )
        raise ParseError("PDF_ALL_PAGES_FAILED: 文件没有可提取或识别的内容")

    failed_pages = [page.page_number for page in recognized.failed_pages]
    warning = None
    if failed_pages:
        page_list = "、".join(str(page) for page in failed_pages[:20])
        warning = f"部分页面未能识别：第 {page_list} 页"
    if methods == {"native_text"}:
        backend = "pdf-native"
    elif methods <= {"scanned", "failed"}:
        backend = "pdf-vision"
    else:
        backend = "pdf-mixed"
    parsed = ParsedDocument(
        elements=elements,
        page_count=recognized.page_count,
        meta={"source": "pdf", "recognition": recognized.bounded_metadata()},
    )
    return parsed, recognized.bounded_metadata(), backend, warning


async def ingest_document(
    ctx: dict,
    tenant_id: str,
    doc_id: str,
    claimed_job_id: str | None = None,
) -> str:
    """Stage 1 全流程。任何一步失败 -> status=failed + 可读 error_msg (失败有尊严)。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document
    from eduagents_core.chunking.base import chunk_document
    from eduagents_core.embedding.providers import create_embedder
    from eduagents_core.embedding.sparse import Bm25SparseEncoder
    from eduagents_core.parsing.base import ParseError
    from eduagents_core.parsing.registry import get_parser
    from eduagents_core.storage import create_storage
    from eduagents_core.vectorstore.base import VectorPoint
    from eduagents_core.vectorstore.qdrant_store import QdrantStore

    from .vector_lock import document_vector_lock

    settings = get_settings()
    qdrant = QdrantStore(settings)
    redis = ctx.get("redis")
    watch_field = f"{tenant_id}:{doc_id}"
    active_job_id = claimed_job_id or str(ctx.get("job_id") or "") or None
    document_version: int | None = None
    watch_value: str | None = None
    try:
        async with tenant_session(tenant_id) as session:
            doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
            if doc is None:
                return "missing"
            meta = dict(doc.recognition_meta_json or {})
            persisted_job_id = meta.get("ingest_job_id")
            if persisted_job_id:
                # Local/integration callers may execute the queued row directly
                # without an ARQ context.  They can safely adopt its persisted
                # claim because this row lock still owns queued -> running.
                if active_job_id is None and redis is None:
                    active_job_id = str(persisted_job_id)
                if active_job_id != str(persisted_job_id):
                    return "stale_job"
                if meta.get("ingest_job_state") == "running":
                    return "already_running"
                raw_version = meta.get("ingest_document_version")
                try:
                    claimed_version = int(raw_version)
                except (TypeError, ValueError):
                    return "stale_job"
                if claimed_version != int(doc.version):
                    return "stale_job"
            else:
                if doc.status not in ("pending", "failed"):
                    return "already_running"
                expected_job_prefix = f"ingest:{doc_id}:v{doc.version}"
                if active_job_id and not str(active_job_id).startswith(expected_job_prefix):
                    return "stale_job"
                active_job_id = active_job_id or f"ingest:{doc_id}:v{doc.version}"
                meta["ingest_job_id"] = active_job_id
                meta["ingest_document_version"] = int(doc.version)
                meta["ingest_started_at"] = time.time()
            document_version = int(doc.version)
            meta["ingest_job_state"] = "running"
            doc.recognition_meta_json = meta
            kb_id, filename = str(doc.kb_id), doc.filename
            user_id = str(doc.uploaded_by or doc.tenant_id)

        if active_job_id is None:
            raise _StaleIngestion(f"document {doc_id} has no ingestion claim")

        watch_value = json.dumps(
            {
                "started_at": time.time(),
                "job_id": active_job_id,
                "version": document_version,
            },
            separators=(",", ":"),
        )
        if redis is not None:
            try:
                # Value carries ownership, so an older attempt cannot delete a
                # newer attempt's watchdog registration in its finally block.
                await redis.hset(INGEST_WATCH_KEY, watch_field, watch_value)
            except Exception:  # noqa: BLE001 — 登记失败不阻断摄取
                log.warning("ingest_watch_register_failed", doc_id=doc_id)

        # 1) 解析
        await _set_status_checked(
            doc_id,
            tenant_id,
            "parsing",
            expected_version=document_version,
            expected_job_id=active_job_id,
        )
        storage = create_storage(settings)
        data = await storage.load(f"{tenant_id}/{kb_id}/{doc_id}/{filename}")
        suffix = Path(filename).suffix.lower()
        recognition_meta: dict = {}
        parse_backend: str | None = None
        partial_warning: str | None = None
        if suffix == ".pdf":
            from .enrichment import get_worker_gateway

            parsed, recognition_meta, parse_backend, partial_warning = await _parse_pdf(
                data,
                settings=settings,
                gateway=ctx.get("gateway") or get_worker_gateway(settings),
                tenant_id=tenant_id,
                user_id=user_id,
                doc_id=doc_id,
                document_version=document_version,
                job_id=active_job_id,
            )
        else:
            parser = get_parser(suffix)
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tmp_path = Path(tmp.name)
            try:
                # 解析超时上限 120s (压缩炸弹/病态文件防护, §8)
                parsed = await asyncio.wait_for(
                    asyncio.to_thread(parser.parse, tmp_path), timeout=120
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        if not parsed.elements:
            raise ParseError("解析结果为空: 文件无可提取文本")

        # 2) 分块
        await _set_status_checked(
            doc_id,
            tenant_id,
            "chunking",
            expected_version=document_version,
            expected_job_id=active_job_id,
        )
        parents = chunk_document(
            parsed,
            child_chars=settings.chunk_child_chars,
            parent_chars=settings.chunk_parent_chars,
            overlap_chars=settings.chunk_overlap_chars,
        )
        if not parents:
            raise ParseError("分块结果为空")

        # 2.5) chunk 配额 (P0-2c): 成本按 chunk 走, 配额必须与成本同单位。
        # 超限即置 failed (不静默截断); 用户级按 chunks 表现有行数预检
        # (排除本文档旧行: 重摄取会先删后建, 不该重复计)。
        doc_rows = len(parents) + sum(len(p.children) for p in parents)
        if settings.max_chunks_per_doc > 0 and doc_rows > settings.max_chunks_per_doc:
            msg = (
                f"CHUNK_QUOTA_EXCEEDED: 该文档分块数 {doc_rows} 超过单文档配额 "
                f"{settings.max_chunks_per_doc}，请拆分为更小的文档后分批上传"
            )
            await _set_status_checked(
                doc_id,
                tenant_id,
                "failed",
                msg,
                expected_version=document_version,
                expected_job_id=active_job_id,
                clear_ingest_claim=True,
            )
            log.warning("ingest_doc_chunk_quota_exceeded", doc_id=doc_id, chunks=doc_rows)
            return "failed"
        if settings.max_chunks_per_user > 0:
            async with tenant_session(tenant_id) as session:
                existing_rows = (
                    await session.scalar(
                        select(func.count())
                        .select_from(Chunk)
                        .where(
                            Chunk.tenant_id == uuid.UUID(tenant_id),
                            Chunk.doc_id != uuid.UUID(doc_id),
                        )
                    )
                    or 0
                )
            if existing_rows + doc_rows > settings.max_chunks_per_user:
                msg = (
                    f"CHUNK_QUOTA_EXCEEDED: 账户分块总数将达 {existing_rows + doc_rows}，"
                    f"超过账户配额 {settings.max_chunks_per_user}，"
                    "请删除部分旧文档后重试"
                )
                await _set_status_checked(
                    doc_id,
                    tenant_id,
                    "failed",
                    msg,
                    expected_version=document_version,
                    expected_job_id=active_job_id,
                    clear_ingest_claim=True,
                )
                log.warning(
                    "ingest_user_chunk_quota_exceeded",
                    doc_id=doc_id,
                    existing=existing_rows,
                    new=doc_rows,
                )
                return "failed"

        # 3) 入库 (chunks 表: 父块行 + 子块行, 子块 parent_id 指向父块行)
        child_rows: list[tuple[uuid.UUID, str, dict]] = []  # (id, text, meta)
        async with tenant_session(tenant_id) as session:
            doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
            if doc is None or not _owns_ingest_attempt(
                doc,
                expected_version=document_version,
                expected_job_id=active_job_id,
            ):
                raise _StaleIngestion(f"document {doc_id} generation changed")
            if recognition_meta:
                merged_meta = dict(doc.recognition_meta_json or {})
                merged_meta.update(recognition_meta)
                doc.recognition_meta_json = merged_meta
            if parse_backend:
                doc.parse_backend = parse_backend
            await session.execute(delete(Chunk).where(Chunk.doc_id == uuid.UUID(doc_id)))
            for parent in parents:
                parent_id = uuid.uuid4()
                session.add(
                    Chunk(
                        id=parent_id,
                        tenant_id=uuid.UUID(tenant_id),
                        doc_id=uuid.UUID(doc_id),
                        parent_id=None,
                        text=parent.text,
                        heading_path=parent.heading_path,
                        page_start=parent.page_start,
                        page_end=parent.page_end,
                        content_hash="",
                        embedding_status="parent",
                    )
                )
                for child in parent.children:
                    child_id = uuid.uuid4()
                    import xxhash

                    session.add(
                        Chunk(
                            id=child_id,
                            tenant_id=uuid.UUID(tenant_id),
                            doc_id=uuid.UUID(doc_id),
                            parent_id=parent_id,
                            text=child.text,
                            heading_path=child.heading_path,
                            page_start=child.page_start,
                            page_end=child.page_end,
                            content_hash=xxhash.xxh64_hexdigest(child.text),
                            embedding_status="pending",
                        )
                    )
                    child_rows.append(
                        (
                            child_id,
                            child.text,
                            {
                                "parent_id": str(parent_id),
                                "heading_path": child.heading_path,
                                "page_start": child.page_start,
                                "page_end": child.page_end,
                                "is_table": child.is_table,
                            },
                        )
                    )

        # 4) 向量化 (子块): heading_path 面包屑拼进嵌入文本增强定位
        await _set_status_checked(
            doc_id,
            tenant_id,
            "embedding",
            expected_version=document_version,
            expected_job_id=active_job_id,
        )
        if not await _document_exists(
            doc_id,
            tenant_id,
            expected_version=document_version,
            expected_job_id=active_job_id,
        ):
            return "missing"
        embedder = create_embedder(settings)
        sparse_encoder = Bm25SparseEncoder()
        texts = [f"{meta['heading_path']}\n{text}" for _, text, meta in child_rows]
        dense_vectors = await embedder.embed(texts)
        points = [
            VectorPoint(
                id=str(child_id),
                dense=dense_vectors[i],
                sparse=sparse_encoder.encode_document(texts[i]),
                payload={
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "text": text,
                    "document_version": document_version,
                    "ingest_job_id": active_job_id,
                    **meta,
                },
            )
            for i, (child_id, text, meta) in enumerate(child_rows)
        ]
        if not await _document_exists(
            doc_id,
            tenant_id,
            expected_version=document_version,
            expected_job_id=active_job_id,
        ):
            return "missing"
        async with document_vector_lock(redis, tenant_id, doc_id):
            if not await _document_exists(
                doc_id,
                tenant_id,
                expected_version=document_version,
                expected_job_id=active_job_id,
            ):
                raise _StaleIngestion(f"document {doc_id} generation changed")
            await qdrant.ensure_ready()
            # Delete and upsert are one serialized replacement boundary.  An
            # older job must pass the version fence while holding this lock.
            await qdrant.delete_by_doc(tenant_id, doc_id)
            await qdrant.upsert(points)
            async with tenant_session(tenant_id) as session:
                doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
                meta = dict(doc.recognition_meta_json or {}) if doc is not None else {}
                if (
                    doc is None
                    or doc.version != document_version
                    or meta.get("ingest_job_id") != active_job_id
                ):
                    await qdrant.delete_points(
                        [str(child_id) for child_id, _, _ in child_rows]
                    )
                    raise _StaleIngestion(f"document {doc_id} generation changed")
                await session.execute(
                    update(Chunk)
                    .where(Chunk.doc_id == uuid.UUID(doc_id), Chunk.parent_id.isnot(None))
                    .values(embedding_status="done")
                )
        # Stub has no semantic enrichment call to wait for, but it must not
        # expose a ready document with empty child KC bindings.  Bootstrap is
        # intentionally before the terminal status write: a missing/invalid
        # child set follows the normal failed-ingestion path instead of being
        # presented as a usable cold-start source.
        if settings.gateway_mode == "stub":
            from .kc_bootstrap import KcBootstrapStale, bootstrap_document_kcs

            try:
                await bootstrap_document_kcs(
                    tenant_id,
                    doc_id,
                    expected_version=document_version,
                    expected_job_id=active_job_id,
                )
            except KcBootstrapStale as exc:
                raise _StaleIngestion(str(exc)) from exc

        terminal_status = "partial_ready" if partial_warning else "ready"
        await _set_status_checked(
            doc_id,
            tenant_id,
            terminal_status,
            partial_warning,
            expected_version=document_version,
            expected_job_id=active_job_id,
            recognition_meta=recognition_meta or None,
            parse_backend=parse_backend,
        )
        async with tenant_session(tenant_id) as session:
            doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
            if doc is None:
                raise _StaleIngestion(f"document {doc_id} disappeared")
            meta = dict(doc.recognition_meta_json or {})
            if (
                doc.version != document_version
                or meta.get("ingest_job_id") != active_job_id
            ):
                raise _StaleIngestion(f"document {doc_id} generation changed")
            for key in (
                "ingest_job_id",
                "ingest_document_version",
                "ingest_started_at",
                "ingest_job_state",
            ):
                meta.pop(key, None)
            doc.recognition_meta_json = meta
        log.info(
            "ingest_done", doc_id=doc_id, parents=len(parents), children=len(child_rows)
        )
        # Stage 2 自动串联: live/record/replay retain the enrichment barrier;
        # stub was completed synchronously by chapter bootstrap above.
        if redis is not None and settings.gateway_mode != "stub":
            enrich_job_id = f"enrich:{doc_id}:v{document_version}"
            try:
                job = await redis.enqueue_job(
                    "enrich_document",
                    tenant_id,
                    doc_id,
                    None,
                    user_id,
                    enrich_job_id,
                    _job_id=enrich_job_id,
                )
                if job is None:
                    raise RuntimeError("automatic enrichment job was not enqueued")
            except Exception:  # noqa: BLE001 — ready 文档仍可用，允许稍后手动增强
                log.exception("automatic_enrichment_enqueue_failed", doc_id=doc_id)
                await _set_status(
                    doc_id,
                    tenant_id,
                    terminal_status,
                    "知识增强任务提交失败，资料仍可检索，请稍后重试",
                    expected_version=document_version,
                )
        return terminal_status
    except _StaleIngestion:
        log.info("ingest_stale_job", doc_id=doc_id, job_id=active_job_id)
        return "stale_job"
    except ParseError as exc:
        if document_version is not None:
            updated = await _set_status(
                doc_id,
                tenant_id,
                "failed",
                exc.message,
                expected_version=document_version,
                expected_job_id=active_job_id,
                clear_ingest_claim=True,
            )
            if not updated:
                log.info("ingest_stale_job", doc_id=doc_id, job_id=active_job_id)
                return "stale_job"
        log.error("ingest_parse_failed", doc_id=doc_id, error=exc.message)
        return "failed"
    except asyncio.CancelledError:
        # P1-6: arq job_timeout 抛的是 CancelledError (BaseException) — 旧代码
        # except Exception 接不住 → 状态永久卡在 parsing/embedding。先落 failed
        # 再 re-raise: 取消语义必须向上传播 (不吞), 但状态机不许留中间态。
        if document_version is not None:
            await _set_status(
                doc_id,
                tenant_id,
                "failed",
                "摄取超时被取消 (job_timeout)，请重试或拆分文档",
                expected_version=document_version,
                expected_job_id=active_job_id,
                clear_ingest_claim=True,
            )
        log.error("ingest_cancelled", doc_id=doc_id)
        raise
    except BaseException as exc:  # noqa: BLE001 — 摄取失败必须落状态, 不许吞错也不许炸队列
        if document_version is not None:
            updated = await _set_status(
                doc_id,
                tenant_id,
                "failed",
                "INGESTION_FAILED: 文档处理失败，请稍后重试",
                expected_version=document_version,
                expected_job_id=active_job_id,
                clear_ingest_claim=True,
            )
            if not updated and isinstance(exc, Exception):
                log.info("ingest_stale_job", doc_id=doc_id, job_id=active_job_id)
                return "stale_job"
        log.exception("ingest_failed", doc_id=doc_id)
        if not isinstance(exc, Exception):  # SystemExit/KeyboardInterrupt 等继续向上
            raise
        return "failed"
    finally:
        if redis is not None:
            try:
                await _delete_watch_if_current(redis, watch_field, watch_value)
            except Exception:  # noqa: BLE001 — 清理失败留给 reap cron 兜底
                log.warning("ingest_watch_cleanup_failed", doc_id=doc_id)


async def reap_stale_ingestion(ctx: dict) -> dict:
    """P1-6 停滞回收 cron: >30 分钟仍停留在 parsing/chunking/embedding 的摄取置
    failed 并重入队 (Redis 计数 ≤3 次, 防坏文件无限重试烧资源)。

    数据源是 ingest_document 登记的 Redis hash (worker 崩溃/SIGKILL 时 finally
    不执行, 登记残留 → 本 cron 兜住), 因此无需跨租户扫 documents 表 (RLS 下不可行)。
    注册方式 (主线接线): WorkerSettings.cron_jobs 加
    cron(reap_stale_ingestion, minute={0, 15, 30, 45}, name="reap_stale_ingestion")。
    """
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Document

    redis = ctx.get("redis")
    if redis is None:
        return {"reaped": 0, "requeued": 0}
    entries = await redis.hgetall(INGEST_WATCH_KEY)
    now = time.time()
    reaped = requeued = 0
    for field, raw in entries.items():
        field = field.decode() if isinstance(field, bytes) else str(field)
        raw = raw.decode() if isinstance(raw, bytes) else raw
        legacy_watch = False
        try:
            watch = json.loads(raw)
            if not isinstance(watch, dict):
                raise TypeError("watch value is not an object")
            started = float(watch["started_at"])
            watch_job_id = str(watch["job_id"])
            watch_version = int(watch["version"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            # Pre-fence workers stored only a timestamp.  Keep those records
            # reapable during rollout, but never treat an actually malformed
            # value as an owned attempt.
            try:
                started = float(raw)
            except (TypeError, ValueError):
                started = 0.0
                watch_job_id = None
                watch_version = None
            else:
                legacy_watch = True
                watch_job_id = None
                watch_version = None
        if now - started < REAP_STALE_SECONDS:
            continue
        tenant_id, _, doc_id = field.partition(":")
        if not tenant_id or not doc_id:
            await _delete_watch_if_current(redis, field, raw)
            continue
        try:
            async with tenant_session(tenant_id) as session:
                doc = await session.get(Document, uuid.UUID(doc_id))
                status = doc.status if doc is not None else None
                meta = dict(doc.recognition_meta_json or {}) if doc is not None else {}
                if legacy_watch:
                    # Legacy records cannot prove which attempt wrote them.
                    # Infer the current generation only when it is still in a
                    # stuck state; structured records below retain the strict
                    # job/version fence.
                    watch_version = int(doc.version) if doc is not None else None
                    persisted_version = meta.get("ingest_document_version")
                    try:
                        persisted_version = (
                            int(persisted_version) if persisted_version is not None else None
                        )
                    except (TypeError, ValueError):
                        persisted_version = None
                    watch_job_id = str(meta.get("ingest_job_id") or "") or None
                    still_owned = bool(
                        doc is not None
                        and (
                            persisted_version is None
                            or persisted_version == int(doc.version)
                        )
                    )
                else:
                    still_owned = bool(
                        doc is not None
                        and doc.version == watch_version
                        and meta.get("ingest_job_id") == watch_job_id
                    )
        except Exception:  # noqa: BLE001 — 单条失败不拖垮整轮回收
            log.exception("reap_status_check_failed", doc_id=doc_id)
            continue
        if status not in _STUCK_STATUSES or not still_owned or watch_version is None:
            # 已走完 (ready/failed/enriched/已删除): 只清残留登记
            await _delete_watch_if_current(redis, field, raw)
            continue
        retry_key = INGEST_REAP_RETRY_KEY.format(doc_id=doc_id)
        retries = int(await redis.incr(retry_key))
        await redis.expire(retry_key, _REAP_RETRY_TTL)
        stale_min = REAP_STALE_SECONDS // 60
        if retries <= REAP_MAX_RETRIES:
            note = f"摄取停滞超 {stale_min} 分钟已回收 (第 {retries} 次)，系统将自动重试"
        else:
            note = (
                f"摄取停滞超 {stale_min} 分钟已回收，"
                f"重试 {REAP_MAX_RETRIES} 次仍失败，请删除后重新上传"
            )
        retry_job_id = (
            f"ingest:{doc_id}:v{watch_version}:retry:{retries}"
            if retries <= REAP_MAX_RETRIES
            else None
        )
        status_updated = await _set_status(
            doc_id,
            tenant_id,
            "failed",
            note,
            expected_version=watch_version,
            expected_job_id=watch_job_id,
            clear_ingest_claim=retry_job_id is None,
            next_ingest_job_id=retry_job_id,
        )
        if not status_updated:
            # A newer attempt won the document fence while this row was being
            # inspected.  Never count or enqueue work for the old watch.
            await _delete_watch_if_current(redis, field, raw)
            continue
        await _delete_watch_if_current(redis, field, raw)
        reaped += 1
        requeued_this_attempt = False
        if retry_job_id is not None:
            try:
                job = await redis.enqueue_job(
                    "ingest_document",
                    tenant_id,
                    doc_id,
                    retry_job_id,
                    _job_id=retry_job_id,
                )
                if job is None:
                    raise RuntimeError("reaped ingestion job was not enqueued")
                requeued += 1
                requeued_this_attempt = True
            except Exception:  # noqa: BLE001 — 释放本轮 claim，允许人工重新触发
                log.exception(
                    "reap_requeue_failed", doc_id=doc_id, job_id=retry_job_id
                )
                await _clear_ingest_claim(
                    doc_id,
                    tenant_id,
                    retry_job_id,
                    watch_version,
                )
        log.warning(
            "ingest_reaped",
            doc_id=doc_id,
            retries=retries,
            requeued=requeued_this_attempt,
        )
    if reaped:
        log.info("reap_stale_ingestion_done", reaped=reaped, requeued=requeued)
    return {"reaped": reaped, "requeued": requeued}


async def cleanup_document(
    ctx: dict,
    tenant_id: str,
    kb_id: str,
    doc_id: str,
    filename: str,
    *,
    progress=None,
) -> str:
    """文档删除的物理清理: 向量 + 对象存储 (DB 行已由 API 层级联删除)。"""
    from eduagents_core.storage import create_storage
    from eduagents_core.vectorstore.qdrant_store import QdrantStore

    from .cleanup_progress import run_cleanup_step

    settings = get_settings()
    store = QdrantStore(settings)
    from .vector_lock import document_vector_lock

    async def _delete_vectors() -> None:
        async with document_vector_lock(ctx.get("redis"), tenant_id, doc_id):
            await store.ensure_ready()
            await store.delete_by_doc(tenant_id, doc_id)

    await run_cleanup_step(progress, "qdrant", _delete_vectors)
    await run_cleanup_step(
        progress,
        "storage",
        lambda: create_storage(settings).delete_prefix(f"{tenant_id}/{kb_id}/{doc_id}"),
    )
    log.info("cleanup_document_done", doc_id=doc_id)
    return "ok"


async def cleanup_kb(ctx: dict, tenant_id: str, kb_id: str, *, progress=None) -> str:
    from eduagents_core.storage import create_storage
    from eduagents_core.vectorstore.qdrant_store import QdrantStore

    from .cleanup_progress import run_cleanup_step

    settings = get_settings()
    store = QdrantStore(settings)

    async def _delete_vectors() -> None:
        await store.ensure_ready()
        await store.delete_by_kb(tenant_id, kb_id)

    await run_cleanup_step(progress, "qdrant", _delete_vectors)
    await run_cleanup_step(
        progress,
        "storage",
        lambda: create_storage(settings).delete_prefix(f"{tenant_id}/{kb_id}"),
    )
    log.info("cleanup_kb_done", kb_id=kb_id)
    return "ok"
