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


class _StaleEnrichment(RuntimeError):
    """The document was reindexed while this enrichment generation was running."""


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
    claimed_job_id: str | None = None,
) -> str:
    """Stage 2 (F2 两阶段摄取): ready/partial_ready → enriching → enriched。

    内容: ① 知识库级 KC 受控词表 (首个文档时构建, strong 档) ② chunk 级 KC 抽取
    并经 KcNormalizer 归一化挂载 (cheap 档) ③ 上下文前缀生成并重嵌入 (cheap 档)。
    失败语义: semantic KC 未覆盖全部 child 时回落刷新前状态，保留已有章节 KC 与学习
    证据；已成功增强的文档采用 stale-while-revalidate，刷新失败仍保持 enriched 可用。
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
        normalize_kc_name,
    )
    from eduagents_core.kc.extraction import (
        KcExtractionError,
        section_heading_names,
        section_topic_name,
    )
    from eduagents_core.vectorstore.base import VectorPoint
    from eduagents_core.vectorstore.qdrant_store import QdrantStore

    from .vector_lock import document_vector_lock

    settings = get_settings()
    if gateway is None:
        if settings.gateway_mode == "stub":
            log.info("enrich_skipped_stub_mode", doc_id=doc_id)
            # 手动 re-enrich 可能在 stub 模式下入队；不能留下永久抢占标记。
            stub_job_id = claimed_job_id or str(ctx.get("job_id") or "") or None
            async with tenant_session(tenant_id) as session:
                doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
                if doc is not None:
                    meta = dict(doc.recognition_meta_json or {})
                    persisted_job_id = meta.get("enrich_job_id")
                    if not persisted_job_id or str(persisted_job_id) == stub_job_id:
                        fallback = str(meta.get("enrich_previous_status") or doc.status)
                        for key in (
                            "enrich_job_id",
                            "enrich_previous_status",
                            "enrich_started_at",
                            "enrich_job_state",
                            "enrich_document_version",
                        ):
                            meta.pop(key, None)
                        doc.recognition_meta_json = meta
                        doc.status = fallback
            return "skipped"

    # ARQ 的 ctx job_id 是队列记录 id，手动刷新同时显式传入同一个稳定 id。
    # 自动串联未持久化 claim，因此可直接用 ctx id；若测试/直调没有则本地生成。
    active_job_id = claimed_job_id or str(ctx.get("job_id") or "") or None
    enrich_document_version: int
    async with tenant_session(tenant_id) as session:
        doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
        if doc is None:
            return "not_ready"
        meta = dict(doc.recognition_meta_json or {})
        persisted_job_id = meta.get("enrich_job_id")
        if persisted_job_id:
            # API 已经原子抢占。只有携带同一稳定 job id 的 worker 能真正执行；
            # 重复/过期任务不得覆盖当前刷新。
            if active_job_id != str(persisted_job_id):
                return "stale_job"
            if meta.get("enrich_job_state") == "running":
                return "already_running"
            fallback_status = str(meta.get("enrich_previous_status") or "ready")
            raw_version = meta.get("enrich_document_version")
            try:
                enrich_document_version = int(raw_version or doc.version)
            except (TypeError, ValueError):
                return "stale_job"
            if enrich_document_version != int(doc.version):
                return "stale_job"
        else:
            # 自动串联路径（ingestion → enrichment）也在行锁内抢占；并发任务看到
            # enriching/running 后直接退出，避免重复模型调用和 KC 并发写。
            if doc.status not in ("ready", "partial_ready", "enriched"):
                return "not_ready"
            fallback_status = doc.status
            active_job_id = active_job_id or f"enrich:{doc_id}:{uuid.uuid4().hex}"
            meta["enrich_job_id"] = active_job_id
            meta["enrich_previous_status"] = fallback_status
            meta["enrich_started_at"] = datetime.now(UTC).isoformat()
            enrich_document_version = int(doc.version)
        if fallback_status not in ("ready", "partial_ready", "enriched"):
            fallback_status = "ready"
        meta["enrich_document_version"] = enrich_document_version
        meta["enrich_job_state"] = "running"
        doc.recognition_meta_json = meta
        # 旧成功版本刷新期间保持 enriched，让检索/Quiz 继续读旧版本。
        if fallback_status != "enriched":
            doc.status = "enriching"
        kb_id = str(doc.kb_id)
        document_name = doc.filename
        # Personal jobs can use the owner credential; org jobs without a
        # request user deliberately remain platform-only.
        effective_user_id = user_id or (str(doc.uploaded_by) if doc.uploaded_by else "")

    async def ensure_current(stage: str) -> None:
        """Check both generation identity and document version at a write boundary."""

        async with tenant_session(tenant_id) as session:
            current = await session.get(Document, uuid.UUID(doc_id))
            if current is None:
                raise _StaleEnrichment(f"document disappeared during {stage}")
            meta = dict(current.recognition_meta_json or {})
            if (
                current.version != enrich_document_version
                or meta.get("enrich_job_id") != active_job_id
                or int(meta.get("enrich_document_version") or -1)
                != enrich_document_version
            ):
                raise _StaleEnrichment(f"stale enrichment at {stage}")

    try:
        if gateway is None:
            # P0-2a: 共享带 usage_sink+budget_guard 的进程级单例, 不再裸构造。
            # 工厂失败也必须走下方统一回退，不能留下永久 running claim。
            gateway = get_worker_gateway(settings)
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
                "section_candidate": section_topic_name(c.heading_path, document_name),
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
        # Build a semantic vocabulary even when chapter placeholders exist.
        # Promote a placeholder only after its own passage verifies the section topic.
        generated_edges: list[PrereqEdge] = []
        generated_id_map: dict[str, str] = {}
        deferred_headings: list[KcEntry] = []
        heading_keys = {
            normalize_kc_name(name)
            for child in child_data for name in section_heading_names(child["heading_path"])
            if name
        }
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

        # Heading fallback rows remain stored. Only section evidence can
        # promote them, so keep them out of ordinary embedding matching.
        fallback_vocab = KcVocabulary(entries=[e for e in vocab.entries if e.kc_id in fallback_ids])
        semantic_vocab = KcVocabulary(entries=[e for e in vocab.entries if e.kc_id not in fallback_ids])
        normalizer = KcNormalizer(semantic_vocab, embedder)
        if not existing_enrichment_ids:
            for generated in generated_vocab.entries:
                if normalize_kc_name(generated.canonical_name) in heading_keys:
                    deferred_headings.append(generated)
                    continue
                target_id, _created = await normalizer.match_or_create(
                    generated.canonical_name, generated.description
                )
                generated_id_map[generated.kc_id] = target_id
                semantic_ids.add(target_id)
                target = next(
                    entry for entry in normalizer.vocab.entries if entry.kc_id == target_id
                )
                for alias in generated.aliases:
                    if alias not in target.aliases and normalize_kc_name(alias) not in heading_keys:
                        target.aliases.append(alias)
                if not target.description and generated.description:
                    target.description = generated.description
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
                confirmed_section = ""
                try:
                    names = await extract_chunk_kcs(
                        gateway,
                        child["text"],
                        vocab_names,
                        heading_path=child["heading_path"],
                        document_name=document_name,
                        tenant_id=tenant_id,
                        user_id=effective_user_id,
                    )
                    for name in names:
                        if name == child["section_candidate"]:
                            confirmed_section = name
                            continue
                        # KcNormalizer mutates its in-memory vocabulary and
                        # lazily populates embeddings; serialize that small
                        # critical section while retaining concurrent model IO.
                        async with normalizer_lock:
                            if (
                                normalize_kc_name(name) in heading_keys or fallback_vocab.by_name(name)
                            ) and semantic_vocab.by_name(name) is None:
                                continue
                            kc_id, created = await normalizer.match_or_create(name)
                            if created:
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
                    "confirmed_section": confirmed_section,
                    "prefix": prefix,
                }

        enriched = await asyncio.gather(*(process(c) for c in child_data))
        confirmed_headings = {}
        for child in enriched:
            name = child["confirmed_section"]
            if not name:
                continue
            leaf, _ = section_heading_names(child["heading_path"])
            entry = semantic_vocab.by_name(name)
            if entry is None:
                # A chapter and its details can be highly similar. Match the
                # verified chapter lexically so it keeps its own identity.
                entry = fallback_vocab.by_name(leaf) or fallback_vocab.by_name(name)
                if entry is None:
                    entry = KcEntry(str(uuid.uuid4()), name)
                elif normalize_kc_name(name) != normalize_kc_name(entry.canonical_name):
                    if name not in entry.aliases:
                        entry.aliases.append(name)
                semantic_vocab.entries.append(entry)
            semantic_ids.add(entry.kc_id)
            confirmed_headings[normalize_kc_name(name)] = entry
            confirmed_headings[normalize_kc_name(leaf)] = entry
            for key in ("kc_ids", "semantic_kc_ids"):
                if entry.kc_id not in child[key]:
                    child[key].append(entry.kc_id)

        for generated in deferred_headings:
            entry = confirmed_headings.get(normalize_kc_name(generated.canonical_name))
            if entry is not None:
                generated_id_map[generated.kc_id] = entry.kc_id
                if not entry.description:
                    entry.description = generated.description
        new_edges = [
            PrereqEdge(from_kc=generated_id_map[edge.from_kc], to_kc=generated_id_map[edge.to_kc])
            for edge in generated_edges
            if edge.from_kc in generated_id_map and edge.to_kc in generated_id_map
        ]
        # Do not turn a vocabulary row count into a false readiness signal.  A
        # successful live enrichment must produce at least one semantic KC for
        # every child; otherwise the original fallback bindings stay in place
        # and the document remains retryable at the basic-ready state.
        if not enriched or any(not child["semantic_kc_ids"] for child in enriched):
            raise _EnrichmentIncomplete("semantic KC coverage is incomplete")

        # ---- 持久化: 词表新增/别名更新 + 边 + chunk 回写 ----
        await ensure_current("before-pg-persist")
        async with tenant_session(tenant_id) as session:
            current_doc = await session.get(
                Document, uuid.UUID(doc_id), with_for_update=True
            )
            current_meta = dict(current_doc.recognition_meta_json or {}) if current_doc else {}
            if (
                current_doc is None
                or current_doc.version != enrich_document_version
                or current_meta.get("enrich_job_id") != active_job_id
                or int(current_meta.get("enrich_document_version") or -1)
                != enrich_document_version
            ):
                raise _StaleEnrichment("stale enrichment before PG commit")
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

        # ---- 前缀拼接后重嵌入全部 child (§5.1 上下文化) ----
        # A failed prefix call is represented by None in PG.  Re-embedding only
        # successful prefixes would leave the Qdrant vector carrying an older
        # prefix while the chunk row says it has none, so every child is rebuilt
        # against the same generation and payload contract.
        with_prefix = [c for c in enriched if c["prefix"]]
        if enriched:
            await ensure_current("before-qdrant-upsert")

            def embedding_text(child: dict) -> str:
                prefix_text = f"{child['prefix']}\n" if child["prefix"] else ""
                return f"{prefix_text}{child['heading_path']}\n{child['text']}"

            texts = [embedding_text(child) for child in enriched]
            vectors = await embedder.embed(texts)
            sparse_encoder = Bm25SparseEncoder()
            store = QdrantStore(settings)
            points = [
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
                        "document_version": enrich_document_version,
                        "enrich_job_id": active_job_id,
                    },
                )
                for i, c in enumerate(enriched)
            ]
            async with document_vector_lock(ctx.get("redis"), tenant_id, doc_id):
                await store.ensure_ready()
                await store.upsert(points)
                try:
                    await ensure_current("after-qdrant-upsert")
                except _StaleEnrichment:
                    # Reindex may already have written replacement points for the
                    # same document; only remove this generation's exact IDs.
                    await store.delete_points([point.id for point in points])
                    raise

        async with tenant_session(tenant_id) as session:
            doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
            if doc is None:
                return "missing"
            meta = dict(doc.recognition_meta_json or {})
            # 新一轮刷新若已取代本任务，旧任务不得提交终态或 bump 缓存版本。
            if (
                doc.version != enrich_document_version
                or meta.get("enrich_job_id") != active_job_id
                or int(meta.get("enrich_document_version") or -1)
                != enrich_document_version
            ):
                return "stale_job"
            meta.pop("enrich_job_id", None)
            meta.pop("enrich_previous_status", None)
            meta.pop("enrich_started_at", None)
            meta.pop("enrich_job_state", None)
            meta.pop("enrich_document_version", None)
            doc.recognition_meta_json = meta
            doc.status = "enriched"
            doc.error_msg = None
            # KC、依赖边、chunk 绑定、上下文前缀和检索向量均可能变化；版本递增让
            # AnswerCache 与 KC 章节映射自然失效。
            await session.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == uuid.UUID(kb_id))
                .values(kb_version=KnowledgeBase.kb_version + 1)
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
    except _StaleEnrichment:
        log.info("enrich_stale_job", doc_id=doc_id, job_id=active_job_id)
        return "stale_job"
    except BaseException as exc:  # noqa: BLE001 — 取消/超时也必须释放刷新抢占
        log.exception("enrich_failed", doc_id=doc_id)
        async with tenant_session(tenant_id) as session:
            doc = await session.get(Document, uuid.UUID(doc_id), with_for_update=True)
            if doc is not None:
                meta = dict(doc.recognition_meta_json or {})
                if meta.get("enrich_job_id") == active_job_id:
                    meta.pop("enrich_job_id", None)
                    meta.pop("enrich_previous_status", None)
                    meta.pop("enrich_started_at", None)
                    meta.pop("enrich_job_state", None)
                    meta.pop("enrich_document_version", None)
                    doc.recognition_meta_json = meta
                    doc.status = fallback_status
                    doc.error_msg = "知识增强暂时失败，旧版本仍可使用，请稍后重试"
        if not isinstance(exc, Exception):
            raise
        return "failed"
