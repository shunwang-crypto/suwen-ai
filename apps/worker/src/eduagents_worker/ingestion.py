from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from sqlalchemy import delete, func, select, update

log = get_logger("ingestion")

# 摄取状态机 (F2 两阶段, Stage 1): pending→parsing→chunking→embedding→ready | failed
# Stage 2 (enriching→enriched: 上下文前缀 + KC 抽取) 在 M2 后半落地。

# P1-6 停滞回收: 摄取中的文档登记在 Redis hash (field="{tenant}:{doc}" -> 开始秒),
# reap_stale_ingestion cron 扫 >30 分钟仍停留在中间态的置 failed 并重入队 (≤3 次)。
# worker 被 SIGKILL 时 finally 不执行 → 登记残留 → 恰好被 cron 兜住。
INGEST_WATCH_KEY = "ingest:watch"
INGEST_REAP_RETRY_KEY = "ingest:reap:{doc_id}"
REAP_STALE_SECONDS = 30 * 60
REAP_MAX_RETRIES = 3
_REAP_RETRY_TTL = 48 * 3600
# 摄取中间态 (parsing/chunking/embedding): 停留超时才允许回收
_STUCK_STATUSES = ("parsing", "recognizing", "chunking", "embedding")

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
    recognition_meta: dict | None = None,
    parse_backend: str | None = None,
) -> None:
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Document

    async with tenant_session(tenant_id) as session:
        values: dict = {"status": status, "error_msg": error}
        if recognition_meta is not None:
            values["recognition_meta_json"] = recognition_meta
        if parse_backend is not None:
            values["parse_backend"] = parse_backend
        await session.execute(
            update(Document).where(Document.id == uuid.UUID(doc_id)).values(**values)
        )


async def _document_exists(doc_id: str, tenant_id: str) -> bool:
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Document

    async with tenant_session(tenant_id) as session:
        return await session.get(Document, uuid.UUID(doc_id)) is not None


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
            await _set_status(doc_id, tenant_id, "recognizing")
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

        await _set_status(
            doc_id,
            tenant_id,
            "failed",
            "PDF_ALL_PAGES_FAILED: 文件没有可提取或识别的内容",
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


async def ingest_document(ctx: dict, tenant_id: str, doc_id: str) -> str:
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

    settings = get_settings()
    qdrant = QdrantStore(settings)
    redis = ctx.get("redis")
    watch_field = f"{tenant_id}:{doc_id}"
    if redis is not None:
        try:
            # P1-6: 登记在途摄取, 供 reap_stale_ingestion cron 回收停滞任务
            await redis.hset(INGEST_WATCH_KEY, watch_field, time.time())
        except Exception:  # noqa: BLE001 — 登记失败不阻断摄取
            log.warning("ingest_watch_register_failed", doc_id=doc_id)
    try:
        async with tenant_session(tenant_id) as session:
            doc = await session.get(Document, uuid.UUID(doc_id))
            if doc is None:
                return "missing"
            kb_id, filename = str(doc.kb_id), doc.filename
            user_id = str(doc.uploaded_by or doc.tenant_id)
            document_version = doc.version

        # A retry starts from a clean vector namespace.  If parsing later
        # fails, stale vectors from an older version cannot remain searchable.
        await qdrant.ensure_ready()
        await qdrant.delete_by_doc(tenant_id, doc_id)

        # 1) 解析
        await _set_status(doc_id, tenant_id, "parsing")
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
        await _set_status(doc_id, tenant_id, "chunking")
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
            await _set_status(doc_id, tenant_id, "failed", msg)
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
                await _set_status(doc_id, tenant_id, "failed", msg)
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
            if doc is None:
                return "missing"
            if recognition_meta:
                doc.recognition_meta_json = recognition_meta
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
        await _set_status(doc_id, tenant_id, "embedding")
        if not await _document_exists(doc_id, tenant_id):
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
                    **meta,
                },
            )
            for i, (child_id, text, meta) in enumerate(child_rows)
        ]
        if not await _document_exists(doc_id, tenant_id):
            return "missing"
        await qdrant.upsert(points)

        async with tenant_session(tenant_id) as session:
            doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
            if doc is None:
                await qdrant.delete_by_doc(tenant_id, doc_id)
                return "missing"
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
            from .kc_bootstrap import bootstrap_document_kcs

            await bootstrap_document_kcs(tenant_id, doc_id)

        terminal_status = "partial_ready" if partial_warning else "ready"
        await _set_status(
            doc_id,
            tenant_id,
            terminal_status,
            partial_warning,
            recognition_meta=recognition_meta or None,
            parse_backend=parse_backend,
        )
        log.info(
            "ingest_done", doc_id=doc_id, parents=len(parents), children=len(child_rows)
        )
        # Stage 2 自动串联: live/record/replay retain the enrichment barrier;
        # stub was completed synchronously by chapter bootstrap above.
        if redis is not None and settings.gateway_mode != "stub":
            await redis.enqueue_job("enrich_document", tenant_id, doc_id, None, user_id)
        return terminal_status
    except ParseError as exc:
        await _set_status(doc_id, tenant_id, "failed", exc.message)
        log.error("ingest_parse_failed", doc_id=doc_id, error=exc.message)
        return "failed"
    except asyncio.CancelledError:
        # P1-6: arq job_timeout 抛的是 CancelledError (BaseException) — 旧代码
        # except Exception 接不住 → 状态永久卡在 parsing/embedding。先落 failed
        # 再 re-raise: 取消语义必须向上传播 (不吞), 但状态机不许留中间态。
        await _set_status(
            doc_id, tenant_id, "failed", "摄取超时被取消 (job_timeout)，请重试或拆分文档"
        )
        log.error("ingest_cancelled", doc_id=doc_id)
        raise
    except BaseException as exc:  # noqa: BLE001 — 摄取失败必须落状态, 不许吞错也不许炸队列
        await _set_status(
            doc_id,
            tenant_id,
            "failed",
            "INGESTION_FAILED: 文档处理失败，请稍后重试",
        )
        log.exception("ingest_failed", doc_id=doc_id)
        if not isinstance(exc, Exception):  # SystemExit/KeyboardInterrupt 等继续向上
            raise
        return "failed"
    finally:
        if redis is not None:
            try:
                await redis.hdel(INGEST_WATCH_KEY, watch_field)
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
        try:
            started = float(raw)
        except (TypeError, ValueError):
            started = 0.0  # 损坏的登记按最老处理, 走状态复核后清掉
        if now - started < REAP_STALE_SECONDS:
            continue
        tenant_id, _, doc_id = field.partition(":")
        if not tenant_id or not doc_id:
            await redis.hdel(INGEST_WATCH_KEY, field)
            continue
        try:
            async with tenant_session(tenant_id) as session:
                doc = await session.get(Document, uuid.UUID(doc_id))
                status = doc.status if doc is not None else None
        except Exception:  # noqa: BLE001 — 单条失败不拖垮整轮回收
            log.exception("reap_status_check_failed", doc_id=doc_id)
            continue
        if status not in _STUCK_STATUSES:
            # 已走完 (ready/failed/enriched/已删除): 只清残留登记
            await redis.hdel(INGEST_WATCH_KEY, field)
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
        await _set_status(doc_id, tenant_id, "failed", note)
        await redis.hdel(INGEST_WATCH_KEY, field)
        reaped += 1
        if retries <= REAP_MAX_RETRIES:
            await redis.enqueue_job("ingest_document", tenant_id, doc_id)
            requeued += 1
        log.warning(
            "ingest_reaped", doc_id=doc_id, retries=retries, requeued=retries <= REAP_MAX_RETRIES
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

    async def _delete_vectors() -> None:
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
