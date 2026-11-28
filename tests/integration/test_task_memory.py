"""Real PostgreSQL recall, provenance, user controls and concurrent reflection."""

import asyncio
import uuid
from datetime import UTC, datetime

from eduagents_api.db import tenant_session
from eduagents_api.harness_runtime import PostgresTaskStore
from eduagents_api.learner_memory import LearnerMemoryService
from eduagents_api.models import (
    HarnessSession,
    KnowledgeComponent,
    LearnerInsight,
    LearnerMemoryPolicy,
)
from eduagents_api.services import persist_learner_insights
from eduagents_core.harness.task import TaskRecord
from eduagents_worker.reflection import reflect_session
from sqlalchemy import select

from .conftest import requires_services
from .test_insights_flow import _ctx, _FakeGateway, _register_login, _seed_kc_and_chat

pytestmark = requires_services


async def test_task_recall_is_cross_session_but_scoped_to_user_and_course(client):
    user, headers = await _register_login(client, "memory-recall")
    other, _ = await _register_login(client, "memory-other")
    first_kc, first_session = await _seed_kc_and_chat(uuid.UUID(user))
    second_kc, second_session = await _seed_kc_and_chat(uuid.UUID(user))
    _, other_session = await _seed_kc_and_chat(uuid.UUID(other))
    for owner, sid in [(user, first_session), (user, second_session), (other, other_session)]:
        await reflect_session({}, owner, owner, sid, gateway=_FakeGateway())
    service = LearnerMemoryService()
    state = {"tenant_id": user, "user_id": user}
    result = await service.search(state, "导数定义", [], 4)
    assert {row["kc_id"] for row in result["memories"]} == {first_kc, second_kc}
    assert all(row["source_message_id"] and row["observed_at"] for row in result["memories"])
    async with tenant_session(user) as session:
        first = await session.get(KnowledgeComponent, uuid.UUID(first_kc))
        course = str(first.kb_id)
    scoped = await service.search({**state, "kb_ids": [course]}, "导数定义", [], 4)
    assert [row["kc_id"] for row in scoped["memories"]] == [first_kc]
    assert not (await service.search(state, "细胞分裂", [], 4))["memories"]
    toggle = await client.post("/api/preferences/personalization", headers=headers, json={"enabled": False})
    assert toggle.status_code == 200
    assert (await service.search(state, "导数定义", [], 4)) == {"memories": [], "disabled": True}


async def test_correction_revision_obsolescence_and_delete_block_reextraction(client):
    user, headers = await _register_login(client, "memory-controls")
    other, other_headers = await _register_login(client, "memory-controls-other")
    kc_id, sid = await _seed_kc_and_chat(uuid.UUID(user))
    await reflect_session({}, user, user, sid, gateway=_FakeGateway())
    item = (await client.get("/api/profile/insights", headers=headers)).json()["insights"][0]
    path = f"/api/profile/insights/{item['id']}"
    original = item["text"]
    assert (await client.patch(path, headers=other_headers, json={"text": "foreign"})).status_code == 404
    corrected = await client.patch(path, headers=headers, json={
        "text": "已经理解导数定义，之前只是没看清题", "kind": "understood", "expected_revision": 1,
    })
    assert corrected.status_code == 200
    assert corrected.json()["user_corrected"] and corrected.json()["revision"] == 2
    assert corrected.json()["source_message_id"] == item["source_message_id"]
    stale = await client.patch(path, headers=headers, json={"suppressed": True, "expected_revision": 1})
    assert stale.status_code == 409
    assert await reflect_session({}, user, user, sid, gateway=_FakeGateway()) == "no_match"
    service = LearnerMemoryService()
    state = {"tenant_id": user, "user_id": user}
    memories = (await service.search(state, "导数定义", [], 4))["memories"]
    assert len(memories) == 1 and memories[0]["kind"] == "understood"
    assert (await client.patch(path, headers=headers, json={"status": "obsolete", "expected_revision": 2})).status_code == 200
    assert not (await service.search(state, "导数定义", [], 4))["memories"]
    assert (await client.delete(path, headers=headers)).status_code == 204
    async with tenant_session(user) as session:
        policy = await session.get(LearnerMemoryPolicy, uuid.UUID(user))
        assert policy.extraction_after and len(policy.blocked_fingerprints) == 2
        assert all(len(value) == 64 for value in policy.blocked_fingerprints)
        report = {}
        await persist_learner_insights(session, _ctx(uuid.UUID(user)), [
            {"kc_id": kc_id, "kind": "confused", "text": original, "observed_at": datetime.now(UTC)},
        ], source="chat_reflection", session_id=sid, report=report)
        assert report["inserted"] == 0  # The old quote stays blocked even with a new timestamp.
    assert not (await service.search(state, "导数定义", [], 4))["memories"]
    assert (await client.get("/api/profile/insights", headers=headers)).json()["insights"] == []


async def test_delete_during_reflection_prevents_stale_write(client):
    user, headers = await _register_login(client, "memory-race")
    _, sid = await _seed_kc_and_chat(uuid.UUID(user))
    await reflect_session({}, user, user, sid, gateway=_FakeGateway())
    item = (await client.get("/api/profile/insights", headers=headers)).json()["insights"][0]
    started, release = asyncio.Event(), asyncio.Event()
    store = PostgresTaskStore(None)
    thread = f"chat:{user}:{sid}"
    record = TaskRecord(state={"user_id": user, "_memory_context": {"revision": 0}},
                        native_archive={"sessions/history": "old-memory"})
    await store.save(thread, record)

    class SlowGateway(_FakeGateway):
        async def chat(self, req):
            started.set()
            await release.wait()
            return await super().chat(req)

    job = asyncio.create_task(reflect_session({}, user, user, sid, gateway=SlowGateway()))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        assert (await client.delete(f"/api/profile/insights/{item['id']}", headers=headers)).status_code == 204
    finally:
        release.set()
    assert await asyncio.wait_for(job, timeout=10) == "recorded:0"
    await store.save(thread, record)  # A late native checkpoint cannot undo deletion.
    async with tenant_session(user) as session:
        assert (await session.scalars(select(LearnerInsight))).all() == []
        archive = await session.get(HarnessSession, thread)
        assert archive.native_archive == {}


async def test_hybrid_recall_falls_back_and_discards_results_after_concurrent_control_change(client):
    user, headers = await _register_login(client, "hybrid-memory")
    _, sid = await _seed_kc_and_chat(uuid.UUID(user))
    await reflect_session({}, user, user, sid, gateway=_FakeGateway())
    state = {"user_id": user, "tenant_id": user}

    class Embedder:
        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    service = LearnerMemoryService(Embedder(), semantic_enabled=True)
    # A paraphrase with no lexical match is found semantically.
    result = await service.search(state, "瞬间的变化快慢", [], 4)
    assert result["retrieval_mode"] == "hybrid" and result["memories"]

    class BrokenEmbedder:
        async def embed(self, texts):
            raise ValueError("embedding unavailable")

    fallback = await LearnerMemoryService(BrokenEmbedder(), semantic_enabled=True).search(state, "导数定义", [], 4)
    assert fallback["retrieval_mode"] == "lexical" and fallback["memories"]
    started, release = asyncio.Event(), asyncio.Event()

    class SlowEmbedder(Embedder):
        async def embed(self, texts):
            started.set()
            await release.wait()
            return await super().embed(texts)

    pending = asyncio.create_task(LearnerMemoryService(SlowEmbedder(), semantic_enabled=True).search(state, "导数定义", [], 4))
    try:
        await asyncio.wait_for(started.wait(), 10)
        assert (await client.post("/api/preferences/personalization", headers=headers, json={"enabled": False})).status_code == 200
    finally:
        release.set()
    result = await asyncio.wait_for(pending, 10)
    assert result["memory_changed"] and not result["memories"]
