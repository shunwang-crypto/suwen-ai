"""Executable-plan resource closure on real PostgreSQL.

These tests deliberately call the same finalize service used by the plan SSE
confirmation path, then read the result through the authenticated API.  No
LLM or external model is involved.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .conftest import requires_services

pytestmark = requires_services
PASSWORD = "pass-12345"


async def _register(client, prefix: str) -> tuple[uuid.UUID, dict[str, str]]:
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    user_id = uuid.UUID(response.json()["id"])
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return user_id, {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_plan_resource_finalize_detail_is_atomic_and_scoped(client):
    from eduagents_api import timectl
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import (
        Chunk,
        Document,
        ErrorItem,
        KnowledgeBase,
        Plan,
        PlanResource,
        Question,
        Quiz,
    )
    from eduagents_api.services import finalize_plan

    user_id, headers = await _register(client, "plan-resource-owner")
    other_id, other_headers = await _register(client, "plan-resource-other")
    await timectl.set_offset(client._transport.app.state.redis, 0)
    async with tenant_session(user_id) as session:
        kb = KnowledgeBase(tenant_id=user_id, name="资源库", subject="math")
        session.add(kb)
        await session.flush()
        document = Document(
            tenant_id=user_id,
            kb_id=kb.id,
            filename="limits.md",
            content_hash=uuid.uuid4().hex,
            size_bytes=100,
            status="ready",
        )
        session.add(document)
        await session.flush()
        chunk = Chunk(
            tenant_id=user_id,
            doc_id=document.id,
            text="极限的定义",
            content_hash=uuid.uuid4().hex,
            heading_path="第一章",
            embedding_status="ready",
        )
        session.add(chunk)
        await session.flush()
        kb_id, doc_id, chunk_id = kb.id, document.id, chunk.id

    plan_id = uuid.uuid4()
    state = {
        "goal": "掌握极限",
        "budget_hours_per_day": 1.0,
        "kb_id": str(kb_id),
        "resources": [
            {
                "type": "kb_chunk",
                "title": "极限定义快照",
                "kb_id": str(kb_id),
                "doc_id": str(doc_id),
                "chunk_id": str(chunk_id),
            },
            {"type": "web", "title": "危险地址", "url": "javascript:alert(1)"},
            {
                "type": "kb_chunk",
                "title": "伪造分块",
                "kb_id": str(kb_id),
                "doc_id": str(doc_id),
                "chunk_id": str(uuid.uuid4()),
            },
        ],
        "plan": {
            "stages": [
                {
                    "name": "基础",
                    "days": [
                        {
                            "day": 1,
                            "tasks": [
                                {
                                    "title": "阅读极限定义",
                                    "objective": "理解定义",
                                    "instructions": ["阅读", "复述"],
                                    "completion_criteria": "能解释定义",
                                    "minutes": 30,
                                    "kc_names": ["极限"],
                                    "action_type": "read",
                                    "resource_ref": 0,
                                },
                                {
                                    "title": "无效资源任务",
                                    "minutes": 20,
                                    "kc_names": [],
                                    "resource_ref": 1,
                                },
                                {
                                    "title": "过期资源任务",
                                    "minutes": 20,
                                    "kc_names": [],
                                    "resource_ref": 2,
                                },
                            ],
                        }
                    ],
                }
            ]
        },
    }
    ctx = AuthContext(user_id=user_id, tenant_id=user_id, role="student", jti="test")
    saved = await finalize_plan(ctx, str(plan_id), state)
    assert saved["kb_id"] == str(kb_id)
    assert len(saved["resources"]) == 1
    resource_id = uuid.UUID(saved["resources"][0]["id"])
    assert saved["plan"]["stages"][0]["days"][0]["tasks"][0]["resource_id"] == str(resource_id)

    async with tenant_session(user_id) as session:
        assert await session.scalar(select(Plan.kb_id).where(Plan.id == plan_id)) == kb_id
        assert await session.scalar(
            select(func.count()).select_from(PlanResource).where(PlanResource.plan_id == plan_id)
        ) == 1
        # A pending error item alone must not become plan reinforcement evidence.
        quiz = Quiz(tenant_id=user_id, user_id=user_id, title="pending", status="ready")
        session.add(quiz)
        await session.flush()
        question = Question(
            tenant_id=user_id,
            quiz_id=quiz.id,
            type="single",
            stem="pending",
            answer="A",
            options_json=["A"],
        )
        session.add(question)
        await session.flush()
        session.add(
            ErrorItem(
                tenant_id=user_id,
                user_id=user_id,
                question_id=question.id,
                verification_status="pending",
                kc_ids=[],
            )
        )

    detail = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["kb_id"] == str(kb_id)
    assert body["resources"][0]["id"] == str(resource_id)
    task = body["structure"]["stages"][0]["days"][0]["tasks"][0]
    assert task["resource_id"] == str(resource_id)
    assert task["resource"]["title"] == "极限定义快照"
    assert body["structure"]["stages"][0]["days"][0]["tasks"][1]["resource"] is None
    assert body["schedule"]["current_day"] >= 1
    assert "stage0.day0.task0" in body["schedule"]["today_tasks"]

    # A repeated save fails at the database primary key while its new resource
    # is in the same transaction; rollback must leave the original one only.
    async with tenant_session(user_id) as session:
        before = await session.scalar(
            select(func.count()).select_from(PlanResource).where(PlanResource.plan_id == plan_id)
        )
    try:
        await finalize_plan(ctx, str(plan_id), state)
    except IntegrityError:
        pass
    else:
        pytest.fail("duplicate plan id should fail in PostgreSQL")
    async with tenant_session(user_id) as session:
        after = await session.scalar(
            select(func.count()).select_from(PlanResource).where(PlanResource.plan_id == plan_id)
        )
    assert after == before == 1

    # The same plan is user-owned; another user and another tenant cannot read it
    # or the resource, even when they know both UUIDs.
    assert (await client.get(f"/api/plans/{plan_id}", headers=other_headers)).status_code == 404
    async with tenant_session(other_id) as session:
        assert await session.get(PlanResource, resource_id) is None

    # Deleting the source clears FK pointers but retains the snapshot title and
    # does not make historical plan detail fail.
    async with tenant_session(user_id) as session:
        document = await session.get(Document, doc_id)
        assert document is not None
        await session.delete(document)
    detail = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert detail.status_code == 200
    source = detail.json()["resources"][0]
    assert source["title"] == "极限定义快照"
    assert source["doc_id"] is None and source["chunk_id"] is None

    async with tenant_session(user_id) as session:
        kb = await session.get(KnowledgeBase, kb_id)
        assert kb is not None
        await session.delete(kb)
    detail = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["kb_id"] is None and body["kb_name"] is None
    assert body["resources"][0]["kb_id"] is None
    stale_resource = body["structure"]["stages"][0]["days"][0]["tasks"][0]["resource"]
    assert stale_resource["title"] == "极限定义快照"
    assert stale_resource["kb_id"] is None and stale_resource["doc_id"] is None
    # The API retains the title snapshot, while PlanTaskDrawer's resourceHref
    # returns null when all navigable source fields have been SET NULL.

async def test_legacy_plan_detail_does_not_guess_resource_ref(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Plan

    user_id, headers = await _register(client, "legacy-plan")
    async with tenant_session(user_id) as session:
        plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            goal="历史计划",
            budget_hours=1.0,
            structure_json={
                "stages": [{
                    "name": "旧阶段",
                    "days": [{"day": 1, "tasks": [{"title": "旧任务", "resource_ref": 0}]}],
                }]
            },
        )
        session.add(plan)
        await session.flush()
        plan_id = plan.id
    response = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert response.status_code == 200, response.text
    task = response.json()["structure"]["stages"][0]["days"][0]["tasks"][0]
    assert task["resource_ref"] == 0
    assert task.get("resource_id") is None
    assert task["resource"] is None
