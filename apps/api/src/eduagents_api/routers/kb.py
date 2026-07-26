from __future__ import annotations

import uuid

import xxhash
from eduagents_core.logging import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy import delete, func, select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_app_settings
from ..models import Chunk, Document, KnowledgeBase, Org
from ..schemas import (
    ChunkContextItem,
    ChunkContextOut,
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
        return [
            KbOut(
                id=r.id, name=r.name, subject=r.subject,
                kb_version=r.kb_version, created_at=r.created_at,
                owner_type=r.owner_type,
                org_id=r.tenant_id if r.owner_type == "org" else None,
                org_name=org_names.get(r.tenant_id) if r.owner_type == "org" else None,
                writable=r.owner_type == "user"
                or str(r.tenant_id) in ctx.admin_org_ids,
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
    # 向量与对象清理走后台 (删除语义: 立即不可见, 物理清理异步)
    await request.app.state.arq.enqueue_job(
        "cleanup_kb", str(owner_id), str(kb_id)
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
        used_bytes = (
            await session.scalar(select(func.coalesce(func.sum(Document.size_bytes), 0)))
        ) or 0
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
        doc_out = DocumentOut.model_validate(doc, from_attributes=True)

    storage = request.app.state.storage
    await storage.save(f"{owner_id}/{kb_id}/{doc_out.id}/{filename}", data)
    await request.app.state.arq.enqueue_job(
        "ingest_document", str(owner_id), str(doc_out.id)
    )
    log.info("document_uploaded", doc_id=str(doc_out.id), size=len(data))
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
    # F2-A3: 删除后立即从向量库清理, 相关引用不再出现
    await request.app.state.arq.enqueue_job(
        "cleanup_document", str(owner_id), str(kb_id), str(doc_id), filename
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
    async with tenant_session(owner_id) as session:
        doc = await session.get(Document, doc_id)
        if doc is None or doc.kb_id != kb_id:
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        doc.status = "pending"
        doc.error_msg = None
        await session.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
    await request.app.state.arq.enqueue_job(
        "ingest_document", str(owner_id), str(doc_id)
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
                .order_by(Chunk.created_at)
                .offset(offset)
                .limit(min(limit, 200))
            )
        ).all()
        return [ChunkOut.model_validate(r, from_attributes=True) for r in rows]


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
