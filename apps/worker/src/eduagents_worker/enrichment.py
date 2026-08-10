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


class _EnrichmentIncomplete(RuntimeError):
    """Semantic enrichment did not produce a safe, non-empty KC result."""


def _kc_source_kind(row: object) -> str:
    """Read the 0028 source discriminator without trusting names/counts."""

    return str(getattr(row, "source_kind", "enrichment") or "enrichment")


def _kc_constructor_kwargs(model: type, **values: object) -> dict:
    """Keep the worker importable during a rolling 0028 schema upgrade."""

    return {
        key: value
        for key, value in values.items()
        if key in {"id", "tenant_id", "kb_id", "canonical_name", "aliases", "description"}
        or getattr(model, key, None) is not None
    }

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
    失败语义: semantic KC 未覆盖全部 child 时回落 ready，保留已有章节 KC 与学习证据；
    只有真实 ``source_kind=enrichment`` KC 覆盖完成后才进入 enriched。
    stub 网关下跳过（章节 fallback 由 ingestion 在 ready 前同步建立）。
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
                        Chunk.tenant_id == uuid.UUID(tenant_id),
                        Chunk.doc_id == uuid.UUID(doc_id),
                        Chunk.parent_id.isnot(None),
                    )
                )
            ).all()
            parents = {
                str(c.id): c
                for c in (
                    await session.scalars(
                        select(Chunk).where(
                            Chunk.tenant_id == uuid.UUID(tenant_id),
                            Chunk.doc_id == uuid.UUID(doc_id), Chunk.parent_id.is_(None)
                        )
                    )
                ).all()
            }
            kc_rows = (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.tenant_id == uuid.UUID(tenant_id),
                        KnowledgeComponent.kb_id == uuid.UUID(kb_id),
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
            existing_enrichment_ids = {
                str(r.id) for r in kc_rows if _kc_source_kind(r) == "enrichment"
            }
            fallback_ids = {
                str(r.id) for r in kc_rows if _kc_source_kind(r) == "chapter_fallback"
            }
        child_data = [
            {
                "id": str(c.id),
                "text": c.text,
                "heading_path": c.heading_path,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "parent_id": str(c.parent_id),
                "existing_kc_ids": [str(value) for value in (c.kc_ids or []) if value],
            }
            for c in children
        ]
        if not child_data:
            raise _EnrichmentIncomplete("no child chunks")

        # ---- 首个真实 enrichment: 构建知识库级受控词表 + 先修 DAG ----
        # A non-empty vocabulary made only of chapter_fallback rows is not
        # evidence that semantic enrichment completed.  Build the semantic
        # vocabulary in that case, while retaining fallback rows in the
        # normalizer so a high-confidence match can promote them in place.
        new_edges: list[PrereqEdge] = []
        generated_id_map: dict[str, str] = {}
        embedder = create_embedder(settings)
        semantic_ids = set(existing_enrichment_ids)
        if not existing_enrichment_ids:
            headings = list(dict.fromkeys(c["heading_path"] for c in child_data))
            sample = "\n".join(
                p.text[:400] for p in list(parents.values())[:8]
            )
            summary = "章节结构:\n" + "\n".join(headings[:60]) + "\n\n内容节选:\n" + sample
            try:
                # P0-2a: tenant/user 透传, 词表构建 (strong 档) 计量入 usage_log
                generated_vocab, generated_edges = await build_vocabulary(
                    gateway, summary, tenant_id=tenant_id, user_id=effective_user_id
                )
            except KcExtractionError as exc:
                # KcExtractionError contains only a bounded schema error, not
                # model output.  Keep the existing fallback rows untouched.
                log.warning("kc_vocab_build_failed", doc_id=doc_id, error=str(exc))
                raise _EnrichmentIncomplete("semantic vocabulary unavailable") from exc
            if not generated_vocab.entries:
                raise _EnrichmentIncomplete("semantic vocabulary is empty")

        normalizer = KcNormalizer(vocab, embedder)
        if not existing_enrichment_ids:
            # Merge model vocabulary through the normalizer rather than
            # replacing fallback rows.  Exact/embedding matches therefore
            # preserve the fallback UUID and can be promoted below.
            for generated in generated_vocab.entries:
                target_id, _created = await normalizer.match_or_create(
                    generated.canonical_name, generated.description
                )
                generated_id_map[generated.kc_id] = target_id
                semantic_ids.add(target_id)
                target = next(
                    entry for entry in normalizer.vocab.entries if entry.kc_id == target_id
                )
                for alias in generated.aliases:
                    if alias not in target.aliases:
                        target.aliases.append(alias)
                if not target.description and generated.description:
                    target.description = generated.description
            new_edges = [
                PrereqEdge(
                    from_kc=generated_id_map.get(edge.from_kc, edge.from_kc),
                    to_kc=generated_id_map.get(edge.to_kc, edge.to_kc),
                )
                for edge in generated_edges
            ]
        # The first live pass may have started with only chapter fallback rows.
        # Feed the generated semantic vocabulary to chunk extraction as well;
        # using the pre-bootstrap snapshot here would make the model blind to
        # the newly built enrichment KC names and could falsely fail coverage.
        vocab_names = [e.canonical_name for e in normalizer.vocab.entries]

        # ---- chunk 级: KC 抽取归一 + 上下文前缀 ----
        sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)
        normalizer_lock = asyncio.Lock()

        async def process(child: dict) -> dict:
            async with sem:
                kc_ids: list[str] = []
                semantic_kc_ids: list[str] = []
                try:
                    names = await extract_chunk_kcs(
                        gateway,
                        child["text"],
                        vocab_names,
                        tenant_id=tenant_id,
                        user_id=effective_user_id,
                    )
                    for name in names[:3]:
                        # KcNormalizer mutates its in-memory vocabulary and
                        # lazily populates embeddings; serialize that small
                        # critical section while retaining concurrent model IO.
                        async with normalizer_lock:
                            kc_id, created = await normalizer.match_or_create(name)
                            if created or kc_id in fallback_ids:
                                semantic_ids.add(kc_id)
                        kc_ids.append(kc_id)
                        if kc_id in semantic_ids:
                            semantic_kc_ids.append(kc_id)
                except KcExtractionError:
                    log.warning("kc_chunk_extract_failed", chunk_id=child["id"])
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
                merged_kc_ids = list(
                    dict.fromkeys([*child["existing_kc_ids"], *kc_ids])
                )
                return {
                    **child,
                    "kc_ids": merged_kc_ids,
                    "semantic_kc_ids": list(dict.fromkeys(semantic_kc_ids)),
                    "prefix": prefix,
                }

        enriched = await asyncio.gather(*(process(c) for c in child_data))
        # Do not turn a vocabulary row count into a false readiness signal.  A
        # successful live enrichment must produce at least one semantic KC for
        # every child; otherwise the original fallback bindings stay in place
        # and the document remains retryable at the basic-ready state.
        if not enriched or any(not child["semantic_kc_ids"] for child in enriched):
            raise _EnrichmentIncomplete("semantic KC coverage is incomplete")

        # ---- 持久化: 词表新增/别名更新 + 边 + chunk 回写 ----
        async with tenant_session(tenant_id) as session:
            existing = {
                str(r.id): r
                for r in (
                    await session.scalars(
                        select(KnowledgeComponent).where(
                            KnowledgeComponent.tenant_id == uuid.UUID(tenant_id),
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
                            **_kc_constructor_kwargs(
                                KnowledgeComponent,
                                id=uuid.UUID(entry.kc_id),
                                tenant_id=uuid.UUID(tenant_id),
                                kb_id=uuid.UUID(kb_id),
                                canonical_name=entry.canonical_name,
                                aliases=entry.aliases,
                                description=entry.description,
                                source_kind="enrichment",
                                stable_key=None,
                            )
                        )
                    )
                else:
                    if list(row.aliases) != entry.aliases:
                        row.aliases = entry.aliases
                    # A fallback KC is promoted in place only when it was
                    # reached by a semantic vocabulary entry or a new semantic
                    # extraction.  Unmatched historical fallback rows remain
                    # untouched, preserving all question/mastery evidence.
                    if entry.kc_id in semantic_ids and _kc_source_kind(row) == "chapter_fallback":
                        if getattr(KnowledgeComponent, "source_kind", None) is not None:
                            row.source_kind = "enrichment"
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
                    .where(
                        Chunk.id == uuid.UUID(child["id"]),
                        Chunk.tenant_id == uuid.UUID(tenant_id),
                        Chunk.doc_id == uuid.UUID(doc_id),
                        Chunk.parent_id.isnot(None),
                    )
                    .values(kc_ids=child["kc_ids"], context_prefix=child["prefix"])
                )
            persisted_semantic = (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.tenant_id == uuid.UUID(tenant_id),
                        KnowledgeComponent.kb_id == uuid.UUID(kb_id),
                        KnowledgeComponent.id.in_(tuple(uuid.UUID(k) for k in semantic_ids)),
                    )
                )
            ).all()
            if {
                str(row.id) for row in persisted_semantic
            } != semantic_ids or any(
                _kc_source_kind(row) != "enrichment" for row in persisted_semantic
            ):
                raise _EnrichmentIncomplete("semantic KC persistence invariant failed")

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
    except Exception:  # noqa: BLE001 — enriching 失败仅降级为 ready, 不回退 Stage1
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
