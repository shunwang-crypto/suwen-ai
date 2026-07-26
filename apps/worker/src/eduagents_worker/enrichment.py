from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from eduagents_core.config import get_settings
from eduagents_core.gateway import ChatMessage, ChatRequest, ModelGateway, Tier
from eduagents_core.logging import get_logger
from sqlalchemy import select, update

log = get_logger("enrichment")

# apps/worker/src/eduagents_worker/enrichment.py -> 仓库根 = parents[4]
_PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts"
_ENRICH_CONCURRENCY = 10  # §5.1: 并发度 ≥10, 单 chunk 失败降级不阻塞


def _load_prompt(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


async def _gen_context_prefix(
    gateway: ModelGateway,
    *,
    heading_path: str,
    section_context: str,
    chunk_text: str,
    tenant_id: str,
    user_id: str,
) -> str | None:
    template = _load_prompt("context_prefix.md")
    prompt = (
        template.replace("{{heading_path}}", heading_path)
        .replace("{{section_context}}", section_context[:2000])
        .replace("{{chunk_text}}", chunk_text[:1500])
    )
    result = await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role="user", content=prompt),),
            tier=Tier.cheap,
            feature="enrich_prefix",
            tenant_id=tenant_id,
            user_id=user_id,
            max_tokens=100,
        )
    )
    text = result.text.strip()
    # stub/异常输出防御: 前缀必须是一行短句
    if not text or result.mode == "stub" or len(text) > 120 or "\n" in text:
        return None
    return text


async def enrich_document(
    ctx: dict, tenant_id: str, doc_id: str, gateway: ModelGateway | None = None
) -> str:
    """Stage 2 (F2 两阶段摄取): ready → enriching → enriched。

    内容: ① 知识库级 KC 受控词表 (首个文档时构建, strong 档) ② chunk 级 KC 抽取
    并经 KcNormalizer 归一化挂载 (cheap 档) ③ 上下文前缀生成并重嵌入 (cheap 档)。
    失败语义: 单 chunk LLM 失败仅降级 (无前缀/空 kc); 整体失败回落 ready 不回退。
    stub 网关下跳过 (D2: stub 不产生可入库内容), 留待 live/record/replay 真实运行。
    """
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KcEdge, KnowledgeComponent
    from eduagents_core.embedding.providers import create_embedder
    from eduagents_core.embedding.sparse import Bm25SparseEncoder
    from eduagents_core.gateway import OpenAIGateway
    from eduagents_core.kc import (
        KcEntry,
        KcNormalizer,
        KcVocabulary,
        PrereqEdge,
        add_edge_safe,
        build_vocabulary,
        extract_chunk_kcs,
    )
    from eduagents_core.kc.extraction import KcExtractionError
    from eduagents_core.vectorstore.base import VectorPoint
    from eduagents_core.vectorstore.qdrant_store import QdrantStore

    settings = get_settings()
    if gateway is None:
        if settings.gateway_mode == "stub":
            log.info("enrich_skipped_stub_mode", doc_id=doc_id)
            return "skipped"
        gateway = OpenAIGateway(settings)

    async with tenant_session(tenant_id) as session:
        doc = await session.get(Document, uuid.UUID(doc_id))
        if doc is None or doc.status not in ("ready", "enriched"):
            return "not_ready"
        kb_id = str(doc.kb_id)
        await session.execute(
            update(Document).where(Document.id == doc.id).values(status="enriching")
        )

    try:
        # ---- 载入 chunk 与既有词表 ----
        async with tenant_session(tenant_id) as session:
            children = (
                await session.scalars(
                    select(Chunk).where(
                        Chunk.doc_id == uuid.UUID(doc_id), Chunk.parent_id.isnot(None)
                    )
                )
            ).all()
            parents = {
                str(c.id): c
                for c in (
                    await session.scalars(
                        select(Chunk).where(
                            Chunk.doc_id == uuid.UUID(doc_id), Chunk.parent_id.is_(None)
                        )
                    )
                ).all()
            }
            kc_rows = (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.kb_id == uuid.UUID(kb_id)
                    )
                )
            ).all()
            vocab = KcVocabulary(
                entries=[
                    KcEntry(
                        kc_id=str(r.id),
                        canonical_name=r.canonical_name,
                        aliases=list(r.aliases),
                        description=r.description,
                        embedding=None,
                    )
                    for r in kc_rows
                ]
            )
        child_data = [
            {
                "id": str(c.id),
                "text": c.text,
                "heading_path": c.heading_path,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "parent_id": str(c.parent_id),
            }
            for c in children
        ]

        # ---- 首个文档: 构建知识库级受控词表 + 先修 DAG (§5.8 第一遍) ----
        new_edges: list[PrereqEdge] = []
        if not vocab.entries:
            headings = list(dict.fromkeys(c["heading_path"] for c in child_data))
            sample = "\n".join(
                p.text[:400] for p in list(parents.values())[:8]
            )
            summary = "章节结构:\n" + "\n".join(headings[:60]) + "\n\n内容节选:\n" + sample
            try:
                vocab, new_edges = await build_vocabulary(gateway, summary)
            except KcExtractionError as exc:
                log.warning("kc_vocab_build_failed", doc_id=doc_id, error=str(exc))
                vocab = KcVocabulary(entries=[])

        embedder = create_embedder(settings)
        normalizer = KcNormalizer(vocab, embedder)
        vocab_names = [e.canonical_name for e in vocab.entries]

        # ---- chunk 级: KC 抽取归一 + 上下文前缀 (并发, 单点失败降级) ----
        sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)

        async def process(child: dict) -> dict:
            async with sem:
                kc_ids: list[str] = []
                try:
                    names = await extract_chunk_kcs(
                        gateway, child["text"], vocab_names
                    )
                    for name in names[:3]:
                        kc_id, _created = await normalizer.match_or_create(name)
                        kc_ids.append(kc_id)
                except KcExtractionError:
                    pass  # 降级: 该 chunk 不挂 KC
                parent = parents.get(child["parent_id"])
                prefix = None
                try:
                    prefix = await _gen_context_prefix(
                        gateway,
                        heading_path=child["heading_path"],
                        section_context=parent.text if parent else "",
                        chunk_text=child["text"],
                        tenant_id=tenant_id,
                        user_id=tenant_id,
                    )
                except Exception:  # noqa: BLE001 — 前缀失败降级为无前缀
                    log.warning("prefix_gen_failed", chunk_id=child["id"])
                return {**child, "kc_ids": kc_ids, "prefix": prefix}

        enriched = await asyncio.gather(*(process(c) for c in child_data))

        # ---- 持久化: 词表新增/别名更新 + 边 + chunk 回写 ----
        async with tenant_session(tenant_id) as session:
            existing = {
                str(r.id): r
                for r in (
                    await session.scalars(
                        select(KnowledgeComponent).where(
                            KnowledgeComponent.kb_id == uuid.UUID(kb_id)
                        )
                    )
                ).all()
            }
            for entry in normalizer.vocab.entries:
                row = existing.get(entry.kc_id)
                if row is None:
                    session.add(
                        KnowledgeComponent(
                            id=uuid.UUID(entry.kc_id),
                            tenant_id=uuid.UUID(tenant_id),
                            kb_id=uuid.UUID(kb_id),
                            canonical_name=entry.canonical_name,
                            aliases=entry.aliases,
                            description=entry.description,
                        )
                    )
                elif list(row.aliases) != entry.aliases:
                    row.aliases = entry.aliases
            # KC 行必须先 flush 再插边: UoW 的跨表排序靠 relationship 而非
            # 数据库外键, 否则 kc_edges 可能先于 knowledge_components 落库
            await session.flush()
            valid_ids = {e.kc_id for e in normalizer.vocab.entries}
            safe_edges: list[PrereqEdge] = []
            for edge in new_edges:
                if edge.from_kc in valid_ids and edge.to_kc in valid_ids:
                    if add_edge_safe(safe_edges, edge):
                        session.add(
                            KcEdge(
                                tenant_id=uuid.UUID(tenant_id),
                                from_kc=uuid.UUID(edge.from_kc),
                                to_kc=uuid.UUID(edge.to_kc),
                            )
                        )
            for child in enriched:
                await session.execute(
                    update(Chunk)
                    .where(Chunk.id == uuid.UUID(child["id"]))
                    .values(kc_ids=child["kc_ids"], context_prefix=child["prefix"])
                )

        # ---- 前缀拼接后增量重嵌入 (§5.1 上下文化) ----
        with_prefix = [c for c in enriched if c["prefix"]]
        if with_prefix:
            texts = [
                f"{c['prefix']}\n{c['heading_path']}\n{c['text']}" for c in with_prefix
            ]
            vectors = await embedder.embed(texts)
            sparse_encoder = Bm25SparseEncoder()
            store = QdrantStore(settings)
            await store.ensure_ready()
            await store.upsert(
                [
                    VectorPoint(
                        id=c["id"],
                        dense=vectors[i],
                        sparse=sparse_encoder.encode_document(texts[i]),
                        payload={
                            "tenant_id": tenant_id,
                            "kb_id": kb_id,
                            "doc_id": doc_id,
                            "text": c["text"],
                            "parent_id": c["parent_id"],
                            "heading_path": c["heading_path"],
                            "page_start": c["page_start"],
                            "page_end": c["page_end"],
                            "is_table": False,
                            "context_prefix": c["prefix"],
                        },
                    )
                    for i, c in enumerate(with_prefix)
                ]
            )

        async with tenant_session(tenant_id) as session:
            await session.execute(
                update(Document)
                .where(Document.id == uuid.UUID(doc_id))
                .values(status="enriched")
            )
        kc_count = sum(1 for c in enriched if c["kc_ids"])
        log.info(
            "enrich_done",
            doc_id=doc_id,
            chunks=len(enriched),
            with_kc=kc_count,
            with_prefix=len(with_prefix),
            vocab_size=len(normalizer.vocab.entries),
        )
        return "enriched"
    except Exception as exc:  # noqa: BLE001 — enriching 失败仅降级为 ready, 不回退 Stage1
        log.exception("enrich_failed", doc_id=doc_id)
        async with tenant_session(tenant_id) as session:
            await session.execute(
                update(Document)
                .where(Document.id == uuid.UUID(doc_id))
                .values(status="ready", error_msg=f"enrich: {type(exc).__name__}")
            )
        return "failed"
