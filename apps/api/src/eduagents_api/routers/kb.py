from __future__ import annotations

import uuid

import xxhash
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import delete, func, select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..models import Chunk, Document, KnowledgeBase
from ..schemas import (
    ChunkOut,
    DocumentOut,
    KbCreate,
    KbOut,
    RetrieveDebugIn,
)

router = APIRouter(prefix="/api/kb", tags=["kb"])
log = get_logger("kb")

# 上传安全: 后缀白名单 + 魔数校验 (S 级约束, §8)
_ALLOWED_SUFFIXES = {".pdf", ".docx", ".pptx", ".md", ".txt"}
_MAGIC = {
    ".pdf": (b"%PDF",),
    ".docx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
}


def _suffix(filename: str) -> str:
    idx = filename.rfind(".")
    return filename[idx:].lower() if idx >= 0 else ""


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
    async with tenant_session(ctx.tenant_id) as session:
        rows = (await session.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at))).all()
        return [
            KbOut(id=r.id, name=r.name, subject=r.subject,
                  kb_version=r.kb_version, created_at=r.created_at)
            for r in rows
        ]


@router.delete("/{kb_id}", status_code=204)
async def delete_kb(
    kb_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    async with tenant_session(ctx.tenant_id) as session:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    # 向量与对象清理走后台 (删除语义: 立即不可见, 物理清理异步)
    await request.app.state.arq.enqueue_job(
        "cleanup_kb", str(ctx.tenant_id), str(kb_id)
    )


@router.post("/{kb_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    kb_id: uuid.UUID,
    file: UploadFile,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
    settings=Depends(get_app_settings),
):
    data = await file.read()
    filename = file.filename or "unnamed"
    _validate_upload(filename, data[:16], len(data), settings.max_upload_mb)
    content_hash = xxhash.xxh64_hexdigest(data)

    async with tenant_session(ctx.tenant_id) as session:
        kb = await session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        # 配额 (真实运营约束, §3.0)
        doc_count = await session.scalar(
            select(func.count()).select_from(Document)
        )
        if doc_count >= settings.max_docs_per_user:
            raise HTTPException(status_code=400, detail="DOC_QUOTA_EXCEEDED")
        used_bytes = (
            await session.scalar(select(func.coalesce(func.sum(Document.size_bytes), 0)))
        ) or 0
        if used_bytes + len(data) > settings.max_storage_mb_per_user * 1024 * 1024:
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
            tenant_id=ctx.tenant_id,
            kb_id=kb_id,
            filename=filename,
            content_hash=content_hash,
            size_bytes=len(data),
            status="pending",
        )
        session.add(doc)
        await session.flush()
        doc_out = DocumentOut.model_validate(doc, from_attributes=True)

    storage = request.app.state.storage
    await storage.save(f"{ctx.tenant_id}/{kb_id}/{doc_out.id}/{filename}", data)
    await request.app.state.arq.enqueue_job(
        "ingest_document", str(ctx.tenant_id), str(doc_out.id)
    )
    log.info("document_uploaded", doc_id=str(doc_out.id), size=len(data))
    return doc_out


@router.get("/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)
):
    async with tenant_session(ctx.tenant_id) as session:
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
    async with tenant_session(ctx.tenant_id) as session:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        filename = doc.filename
        await session.execute(delete(Document).where(Document.id == doc_id))
    # F2-A3: 删除后立即从向量库清理, 相关引用不再出现
    await request.app.state.arq.enqueue_job(
        "cleanup_document", str(ctx.tenant_id), str(kb_id), str(doc_id), filename
    )


@router.post("/{kb_id}/documents/{doc_id}/reindex", status_code=202)
async def reindex_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(api_rate_limited),
):
    async with tenant_session(ctx.tenant_id) as session:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        doc.status = "pending"
        doc.error_msg = None
        await session.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
    await request.app.state.arq.enqueue_job(
        "ingest_document", str(ctx.tenant_id), str(doc_id)
    )
    return {"status": "queued"}


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
    async with tenant_session(ctx.tenant_id) as session:
        if await session.get(KnowledgeBase, kb_id) is None:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
    result = await retrieval.retrieve(str(ctx.tenant_id), str(kb_id), body.query)
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
    async with tenant_session(ctx.tenant_id) as session:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        rows = (
            await session.scalars(
                select(Chunk)
                .where(Chunk.doc_id == doc_id)
                .order_by(Chunk.created_at)
                .offset(offset)
                .limit(min(limit, 200))
            )
        ).all()
        return [ChunkOut.model_validate(r, from_attributes=True) for r in rows]
