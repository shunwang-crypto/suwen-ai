"""Wave 1B-B database contract tests for chapter KC bootstrap."""

from __future__ import annotations

import asyncio
import json
import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

_DOC = """# 第一章

## 定义

极限定义内容。

## 例题

极限例题内容。

# 第二章

## 定义

导数定义内容。

## 例题

导数例题内容。
"""


async def _register_login(client, suffix: str) -> tuple[str, str]:
    email = f"kc-bootstrap-{suffix}-{uuid.uuid4().hex[:8]}@test.eduagents.cn"
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    token = response.json()["access_token"]
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    return token, me.json()["id"]


async def _create_document(
    client, headers: dict[str, str], name: str, content: str = _DOC,
) -> tuple[str, str]:
    kb = await client.post("/api/kb", json={"name": name, "subject": "math"}, headers=headers)
    kb_id = kb.json()["id"]
    document = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("chapters.md", content.encode(), "text/markdown")},
        headers=headers,
    )
    return kb_id, document.json()["id"]


async def test_stub_bootstrap_is_idempotent_concurrent_and_covers_children(client):
    token, tenant_id = await _register_login(client, "idempotent")
    headers = {"Authorization": f"Bearer {token}"}
    kb_id, doc_id = await _create_document(client, headers, "章节 KC 幂等库")

    from eduagents_worker.ingestion import ingest_document
    from eduagents_worker.kc_bootstrap import bootstrap_document_kcs

    assert await ingest_document({}, tenant_id, doc_id) == "ready"

    # Replays and duplicate ARQ deliveries must converge through the database
    # unique key rather than a select-then-insert race.
    await asyncio.gather(
        *(bootstrap_document_kcs(tenant_id, doc_id) for _ in range(3))
    )

    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, KnowledgeComponent

    async with tenant_session(tenant_id) as session:
        kcs = (
            await session.scalars(
                select(KnowledgeComponent).where(
                    KnowledgeComponent.kb_id == uuid.UUID(kb_id),
                )
            )
        ).all()
        children = (
            await session.scalars(
                select(Chunk).where(
                    Chunk.doc_id == uuid.UUID(doc_id),
                    Chunk.parent_id.isnot(None),
                )
            )
        ).all()

    assert kcs
    assert len({str(row.id) for row in kcs}) == len(kcs)
    assert all(row.source_kind == "chapter_fallback" for row in kcs)
    assert all(row.stable_key for row in kcs)
    assert all(child.kc_ids for child in children)
    valid_ids = {str(row.id) for row in kcs}
    assert all(set(child.kc_ids) <= valid_ids for child in children)

    before = {row.stable_key: str(row.id) for row in kcs}
    await bootstrap_document_kcs(tenant_id, doc_id)
    async with tenant_session(tenant_id) as session:
        after_rows = (
            await session.scalars(
                select(KnowledgeComponent).where(
                    KnowledgeComponent.kb_id == uuid.UUID(kb_id),
                )
            )
        ).all()
    assert {row.stable_key: str(row.id) for row in after_rows} == before


async def test_same_heading_paths_share_kc_but_different_kbs_do_not(client):
    token, tenant_id = await _register_login(client, "isolation")
    headers = {"Authorization": f"Bearer {token}"}
    first_kb, first_doc = await _create_document(client, headers, "第一知识库")
    second_kb, second_doc = await _create_document(client, headers, "第二知识库")

    from eduagents_worker.ingestion import ingest_document

    assert await ingest_document({}, tenant_id, first_doc) == "ready"
    assert await ingest_document({}, tenant_id, second_doc) == "ready"

    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeComponent

    async with tenant_session(tenant_id) as session:
        first_rows = (
            await session.scalars(
                select(KnowledgeComponent).where(
                    KnowledgeComponent.kb_id == uuid.UUID(first_kb),
                )
            )
        ).all()
        second_rows = (
            await session.scalars(
                select(KnowledgeComponent).where(
                    KnowledgeComponent.kb_id == uuid.UUID(second_kb),
                )
            )
        ).all()

    first_by_key = {row.stable_key: str(row.id) for row in first_rows}
    second_by_key = {row.stable_key: str(row.id) for row in second_rows}
    assert first_by_key.keys() == second_by_key.keys()
    assert all(first_by_key[key] != second_by_key[key] for key in first_by_key)


async def test_failed_live_enrichment_keeps_fallback_and_does_not_claim_enriched(client):
    token, tenant_id = await _register_login(client, "failure")
    headers = {"Authorization": f"Bearer {token}"}
    kb_id, doc_id = await _create_document(client, headers, "失败保留库")

    from eduagents_worker.enrichment import enrich_document
    from eduagents_worker.ingestion import ingest_document

    assert await ingest_document({}, tenant_id, doc_id) == "ready"

    class FailingGateway:
        async def chat(self, request):
            raise RuntimeError("simulated gateway failure")

    outcome = await enrich_document({}, tenant_id, doc_id, gateway=FailingGateway())
    assert outcome == "failed"
    documents = await client.get(f"/api/kb/{kb_id}/documents", headers=headers)
    assert documents.json()[0]["status"] == "ready"

    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, KnowledgeComponent

    async with tenant_session(tenant_id) as session:
        fallback_rows = (
            await session.scalars(
                select(KnowledgeComponent).where(
                    KnowledgeComponent.kb_id == uuid.UUID(kb_id),
                )
            )
        ).all()
        children = (
            await session.scalars(
                select(Chunk).where(
                    Chunk.doc_id == uuid.UUID(doc_id), Chunk.parent_id.isnot(None)
                )
            )
        ).all()

    assert fallback_rows and all(row.source_kind == "chapter_fallback" for row in fallback_rows)
    assert all(child.kc_ids for child in children)


async def test_fallback_is_promoted_in_place_when_body_verifies_a_section_topic(client):
    token, tenant_id = await _register_login(client, "promotion")
    headers = {"Authorization": f"Bearer {token}"}
    title = "气体的等温变化"
    quote = "当温度保持不变时，一定量的气体压强随体积增大而减小。"
    kb_id, doc_id = await _create_document(
        client, headers, "原位提升库", f"# 基础实验\n\n## {title}\n\n{quote}\n",
    )

    from eduagents_worker.enrichment import enrich_document
    from eduagents_worker.ingestion import ingest_document

    assert await ingest_document({}, tenant_id, doc_id) == "ready"

    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeComponent

    async with tenant_session(tenant_id) as session:
        before = (
            await session.scalars(
                select(KnowledgeComponent).where(
                    KnowledgeComponent.kb_id == uuid.UUID(kb_id),
                    KnowledgeComponent.canonical_name == title,
                )
            )
        ).first()
    assert before is not None
    before_id = str(before.id)

    class MatchingGateway:
        async def chat(self, request):
            from eduagents_core.gateway import ChatResult

            if request.json_mode and "章节结构" in request.messages[-1].content:
                text = json.dumps(
                    {"kcs": [{"canonical_name": title, "description": "恒温下气体状态的变化"}]},
                    ensure_ascii=False,
                )
            elif request.json_mode:
                text = json.dumps({"kcs": ["气体压强"], "section_quote": quote}, ensure_ascii=False)
            else:
                text = "章节上下文摘要"
            return ChatResult(
                text=text,
                model="fake",
                tier=request.tier.value,
                prompt_tokens=1,
                completion_tokens=1,
                cost_cny=0.0,
                latency_ms=0,
                mode="live",
            )

    assert await enrich_document({}, tenant_id, doc_id, gateway=MatchingGateway()) == "enriched"
    async with tenant_session(tenant_id) as session:
        promoted = await session.get(KnowledgeComponent, uuid.UUID(before_id))
    assert promoted is not None
    assert promoted.source_kind == "enrichment"
    assert str(promoted.id) == before_id
