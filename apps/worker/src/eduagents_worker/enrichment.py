from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from eduagents_core.config import Settings, get_settings
from eduagents_core.gateway import ChatMessage, ChatRequest, ModelGateway, Tier
from eduagents_core.logging import get_logger
from sqlalchemy import func, select, update

log = get_logger("enrichment")

# apps/worker/src/eduagents_worker/enrichment.py -> 仓库根 = parents[4]
_PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts"
_ENRICH_CONCURRENCY = 10  # §5.1: 并发度 ≥10, 单 chunk 失败降级不阻塞

# 进程级共享网关 (P0-2a/P1-15): worker 三个 LLM 任务 (enrichment/reflection/
# study_guide) 一律经此单例, 禁止裸构造 OpenAIGateway ——
# - usage_sink=persist_usage: 后台支出进 usage_log, 成本看板不再系统性少报;
# - budget_guard: 后台任务同样受 钱包余额(0006)+用户日预算+全局熔断 约束;
# - 单例复用: 断路器/客户端连接池跨任务共享, 熔断真正生效 (P1-15)。
_worker_gateway = None


def get_worker_gateway(settings: Settings | None = None):
    """worker 进程级 OpenAIGateway 单例工厂 (线程内 arq 单事件循环, 无锁足够)。"""
    global _worker_gateway
    if _worker_gateway is None:
        from eduagents_api.model_credentials import resolve_user_credential
        from eduagents_api.usage import persist_usage
        from eduagents_api.wallet import create_billing_guard
        from eduagents_core.gateway import OpenAIGateway
        from redis.asyncio import Redis

        settings = settings or get_settings()
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        _worker_gateway = OpenAIGateway(
            settings,
            usage_sink=persist_usage,
            budget_guard=create_billing_guard(redis, settings),
            credential_resolver=lambda req: resolve_user_credential(req, settings),
        )
    return _worker_gateway


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
    ctx: dict,
    tenant_id: str,
    doc_id: str,
    gateway: ModelGateway | None = None,
    user_id: str | None = None,
) -> str:
    """Stage 2 (F2 两阶段摄取): ready/partial_ready → enriching → enriched。

    内容: ① 知识库级 KC 受控词表 (首个文档时构建, strong 档) ② chunk 级 KC 抽取
    并经 KcNormalizer 归一化挂载 (cheap 档) ③ 上下文前缀生成并重嵌入 (cheap 档)。
    失败语义: 单 chunk LLM 失败仅降级 (无前缀/空 kc); 整体失败回落 ready 不回退。
    stub 网关下跳过 (D2: stub 不产生可入库内容), 留待 live/record/replay 真实运行。
    """
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KcEdge, KnowledgeBase, KnowledgeComponent
    from eduagents_core.embedding.providers import create_embedder
    from eduagents_core.embedding.sparse import Bm25SparseEncoder
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
        # P0-2a: 共享带 usage_sink+budget_guard 的进程级单例, 不再裸构造
        gateway = get_worker_gateway(settings)

    async with tenant_session(tenant_id) as session:
        doc = await session.get(Document, uuid.UUID(doc_id))
        if doc is None or doc.status not in ("ready", "partial_ready", "enriched"):
            return "not_ready"
        fallback_status = "partial_ready" if doc.status == "partial_ready" else "ready"
        kb_id = str(doc.kb_id)
        # Personal jobs can use the owner credential; org jobs without a
        # request user deliberately remain platform-only.
        effective_user_id = user_id or (str(doc.uploaded_by) if doc.uploaded_by else "")
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
                # P0-2a: tenant/user 透传, 词表构建 (strong 档) 计量入 usage_log
                vocab, new_edges = await build_vocabulary(
                    gateway, summary, tenant_id=tenant_id, user_id=effective_user_id
                )
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
                        gateway,
                        child["text"],
                        vocab_names,
                        tenant_id=tenant_id,
                        user_id=effective_user_id,
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
                        user_id=effective_user_id,
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
        # 学习指南自动生成 (对标 NotebookLM Source guide): 该库**首个**文档 enriched
        # 时触发一次, 后续文档不重复烧钱 —— 需要刷新走手动 rebuild 端点。
        redis = ctx.get("redis")
        if redis is not None:
            guide_job_id: str | None = None
            async with tenant_session(tenant_id) as session:
                enriched_count = await session.scalar(
                    select(func.count())
                    .select_from(Document)
                    .where(
                        Document.kb_id == uuid.UUID(kb_id),
                        Document.status == "enriched",
                    )
                )
                if enriched_count == 1:
                    kb = await session.scalar(
                        select(KnowledgeBase)
                        .where(KnowledgeBase.id == uuid.UUID(kb_id))
                        .with_for_update()
                    )
                    if kb is not None:
                        config = dict(kb.config_json or {})
                        generation = config.get("study_guide_generation")
                        active = isinstance(generation, dict) and generation.get("status") in {
                            "queued",
                            "running",
                        }
                        if not active and not config.get("study_guide"):
                            guide_job_id = f"study-guide:{kb_id}:{uuid.uuid4().hex}"
                            config["study_guide_generation"] = {
                                "status": "queued",
                                "job_id": guide_job_id,
                                "requested_at": datetime.now(UTC).isoformat(),
                            }
                            kb.config_json = config
            if guide_job_id:
                try:
                    job = await redis.enqueue_job(
                        "build_study_guide",
                        tenant_id,
                        kb_id,
                        _job_id=guide_job_id,
                    )
                    if job is None:
                        raise RuntimeError("study guide job was not enqueued")
                    log.info("study_guide_enqueued", kb_id=kb_id, job_id=guide_job_id)
                except Exception:  # noqa: BLE001 — 自动指南失败不能回退已完成的文档增强
                    log.exception("study_guide_enqueue_failed", kb_id=kb_id)
                    async with tenant_session(tenant_id) as session:
                        kb = await session.get(KnowledgeBase, uuid.UUID(kb_id))
                        if kb is not None:
                            config = dict(kb.config_json or {})
                            generation = config.get("study_guide_generation")
                            if (
                                isinstance(generation, dict)
                                and generation.get("job_id") == guide_job_id
                            ):
                                config["study_guide_generation"] = {
                                    **generation,
                                    "status": "failed",
                                    "finished_at": datetime.now(UTC).isoformat(),
                                    "message": "后台任务提交失败，请手动重试",
                                }
                                kb.config_json = config
        return "enriched"
    except Exception as exc:  # noqa: BLE001 — enriching 失败仅降级为 ready, 不回退 Stage1
        log.exception("enrich_failed", doc_id=doc_id)
        async with tenant_session(tenant_id) as session:
            await session.execute(
                update(Document)
                .where(Document.id == uuid.UUID(doc_id))
                .values(
                    status=fallback_status,
                    error_msg="知识增强暂时失败，文档仍可检索，请稍后重试",
                )
            )
        return "failed"
