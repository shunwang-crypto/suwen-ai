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
_STUCK_STATUSES = ("parsing", "chunking", "embedding")


async def _set_status(doc_id: str, tenant_id: str, status: str, error: str | None = None) -> None:
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Document

    async with tenant_session(tenant_id) as session:
        await session.execute(
            update(Document)
            .where(Document.id == uuid.UUID(doc_id))
            .values(status=status, error_msg=error)
        )


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

        # 1) 解析
        await _set_status(doc_id, tenant_id, "parsing")
        storage = create_storage(settings)
        data = await storage.load(f"{tenant_id}/{kb_id}/{doc_id}/{filename}")
        suffix = Path(filename).suffix.lower()
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
        embedder = create_embedder(settings)
        sparse_encoder = Bm25SparseEncoder()
        texts = [f"{meta['heading_path']}\n{text}" for _, text, meta in child_rows]
        dense_vectors = await embedder.embed(texts)
        store = QdrantStore(settings)
        await store.ensure_ready()
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
        await store.delete_by_doc(tenant_id, doc_id)
        await store.upsert(points)

        async with tenant_session(tenant_id) as session:
            await session.execute(
                update(Chunk)
                .where(Chunk.doc_id == uuid.UUID(doc_id), Chunk.parent_id.isnot(None))
                .values(embedding_status="done")
            )
        await _set_status(doc_id, tenant_id, "ready")
        log.info(
            "ingest_done", doc_id=doc_id, parents=len(parents), children=len(child_rows)
        )
        # Stage 2 自动串联 (stub 网关下 enrich 无意义, 跳过; 见 enrichment.py)
        if redis is not None and settings.gateway_mode != "stub":
            await redis.enqueue_job("enrich_document", tenant_id, doc_id)
        return "ready"
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
        await _set_status(doc_id, tenant_id, "failed", f"{type(exc).__name__}: {exc}")
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
    ctx: dict, tenant_id: str, kb_id: str, doc_id: str, filename: str
) -> str:
    """文档删除的物理清理: 向量 + 对象存储 (DB 行已由 API 层级联删除)。"""
    from eduagents_core.storage import create_storage
    from eduagents_core.vectorstore.qdrant_store import QdrantStore

    settings = get_settings()
    store = QdrantStore(settings)
    await store.ensure_ready()
    await store.delete_by_doc(tenant_id, doc_id)
    await create_storage(settings).delete_prefix(f"{tenant_id}/{kb_id}/{doc_id}")
    log.info("cleanup_document_done", doc_id=doc_id)
    return "ok"


async def cleanup_kb(ctx: dict, tenant_id: str, kb_id: str) -> str:
    from eduagents_core.storage import create_storage
    from eduagents_core.vectorstore.qdrant_store import QdrantStore

    settings = get_settings()
    store = QdrantStore(settings)
    await store.ensure_ready()
    await store.delete_by_kb(tenant_id, kb_id)
    await create_storage(settings).delete_prefix(f"{tenant_id}/{kb_id}")
    log.info("cleanup_kb_done", kb_id=kb_id)
    return "ok"
