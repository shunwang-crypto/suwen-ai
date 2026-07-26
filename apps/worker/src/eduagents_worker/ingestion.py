from __future__ import annotations

import uuid
from pathlib import Path

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from sqlalchemy import delete, update

log = get_logger("ingestion")

# 摄取状态机 (F2 两阶段, Stage 1): pending→parsing→chunking→embedding→ready | failed
# Stage 2 (enriching→enriched: 上下文前缀 + KC 抽取) 在 M2 后半落地。


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
        import asyncio
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
        redis = ctx.get("redis")
        if redis is not None and settings.gateway_mode != "stub":
            await redis.enqueue_job("enrich_document", tenant_id, doc_id)
        return "ready"
    except ParseError as exc:
        await _set_status(doc_id, tenant_id, "failed", exc.message)
        log.error("ingest_parse_failed", doc_id=doc_id, error=exc.message)
        return "failed"
    except Exception as exc:  # noqa: BLE001 — 摄取失败必须落状态, 不许吞错也不许炸队列
        await _set_status(doc_id, tenant_id, "failed", f"{type(exc).__name__}: {exc}")
        log.exception("ingest_failed", doc_id=doc_id)
        return "failed"


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
