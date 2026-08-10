"""Wave 1B/C API KC barrier and quiz-to-profile evidence loop."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"
KC_NAMES = ("章节一", "章节二", "章节三")


async def _register(client, prefix: str) -> tuple[str, dict[str, str]]:
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    async with plain_session() as session:
        user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    return str(user.id), headers


def _kc_kwargs(kb_id: uuid.UUID, tenant_id: uuid.UUID, name: str, index: int) -> dict:
    from eduagents_api.models import KnowledgeComponent

    values = {
        "tenant_id": tenant_id,
        "kb_id": kb_id,
        "canonical_name": name,
        "aliases": [],
    }
    columns = set(KnowledgeComponent.__table__.columns.keys())
    # The ORM/migration worker owns these fields.  The test remains runnable
    # against the pre-Wave-1B schema while asserting them as soon as they land.
    if "source_kind" in columns:
        values["source_kind"] = "chapter_fallback"
    if "stable_key" in columns:
        values["stable_key"] = f"chapter:{index}"
    return values


async def _seed_kb(
    uid: str,
    *,
    document_status: str = "enriched",
    with_kcs: bool = True,
    with_children: bool = True,
) -> dict[str, str]:
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KnowledgeBase, KnowledgeComponent

    tenant_id = uuid.UUID(uid)
    async with tenant_session(tenant_id) as session:
        kb = KnowledgeBase(tenant_id=tenant_id, name="Wave 1B KB", subject="math")
        session.add(kb)
        await session.flush()
        document = Document(
            tenant_id=tenant_id,
            kb_id=kb.id,
            filename="chapters.md",
            content_hash=uuid.uuid4().hex,
            size_bytes=512,
            status=document_status,
        )
        session.add(document)
        await session.flush()

        kcs = []
        if with_kcs:
            for index, name in enumerate(KC_NAMES, start=1):
                kc = KnowledgeComponent(**_kc_kwargs(kb.id, tenant_id, name, index))
                session.add(kc)
                kcs.append(kc)
            await session.flush()

        if with_children:
            parent = Chunk(
                tenant_id=tenant_id,
                doc_id=document.id,
                parent_id=None,
                text="章节父块",
                heading_path="课程",
                content_hash=uuid.uuid4().hex,
                embedding_status="parent",
            )
            session.add(parent)
            await session.flush()
            for index, kc in enumerate(kcs):
                session.add(
                    Chunk(
                        tenant_id=tenant_id,
                        doc_id=document.id,
                        parent_id=parent.id,
                        text=f"章节 {index + 1} 内容",
                        heading_path=f"章节 {index + 1}",
                        kc_ids=[str(kc.id)],
                        content_hash=uuid.uuid4().hex,
                        embedding_status="done",
                    )
                )

        return {
            "kb_id": str(kb.id),
            "document_id": str(document.id),
            **{f"kc_{index}": str(kc.id) for index, kc in enumerate(kcs, start=1)},
        }


async def _sse(client, url: str, headers: dict[str, str], body: dict) -> dict[str, list]:
    events: dict[str, list] = {}
    current = None
    async with client.stream("POST", url, headers=headers, json=body, timeout=60) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current:
                events.setdefault(current, []).append(json.loads(line.split(":", 1)[1]))
    return events


class _LoopGateway:
    async def chat(self, request):
        from eduagents_core.gateway import ChatResult

        if request.feature == "quiz_blueprint":
            payload = {
                "items": [
                    {"kc_name": name, "type": "single", "difficulty": difficulty}
                    for name, difficulty in zip(KC_NAMES, ("easy", "mid", "mid"), strict=True)
                ],
                "summary": "章节诊断",
            }
        else:
            payload = {
                "questions": [
                    {
                        "type": "single",
                        "stem": f"{name} 诊断题",
                        "options": ["A", "B", "C", "D"],
                        "answer": "A",
                        "explanation": "章节基础概念。",
                        "kc_names": [name],
                        "difficulty": difficulty,
                        "source_hint": None,
                    }
                    for name, difficulty in zip(KC_NAMES, ("easy", "mid", "mid"), strict=True)
                ]
            }
        return ChatResult(
            text=json.dumps(payload, ensure_ascii=False),
            model="wave-1b-test",
            tier=request.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=1,
            mode="stub",
        )


async def test_quiz_kc_preflight_rejects_document_without_real_kc(client):
    uid, headers = await _register(client, "quiz-kc-not-ready")
    seeded = await _seed_kb(uid, with_kcs=False, with_children=False)

    response = await client.post(
        "/api/quiz",
        headers=headers,
        json={"topic": "章节诊断", "kb_id": seeded["kb_id"], "num_questions": 3},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "QUIZ_KC_NOT_READY"


async def test_weak_point_quiz_cannot_bypass_kc_readiness_barrier(client):
    uid, headers = await _register(client, "quiz-kc-weak-barrier")
    seeded = await _seed_kb(uid, with_children=False)

    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryRecord

    tenant_id = uuid.UUID(uid)
    async with tenant_session(tenant_id) as session:
        session.add(
            MasteryRecord(
                tenant_id=tenant_id,
                user_id=tenant_id,
                kc_id=uuid.UUID(seeded["kc_1"]),
                p_mastery=0.2,
                confidence=0.5,
            )
        )

    response = await client.post(
        "/api/quiz",
        headers=headers,
        json={
            "from_weak_points": True,
            "kb_id": seeded["kb_id"],
            "num_questions": 1,
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "QUIZ_KC_NOT_READY"


async def test_explicit_kb_preparing_uses_preparing_barrier_code(client):
    uid, headers = await _register(client, "quiz-kc-preparing-explicit")
    seeded = await _seed_kb(uid, document_status="ready")
    client._transport.app.state.settings.gateway_mode = "live"

    response = await client.post(
        "/api/quiz",
        headers=headers,
        json={"topic": "章节诊断", "kb_id": seeded["kb_id"], "num_questions": 1},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "QUIZ_KC_PREPARING"


async def test_legacy_automatic_preparing_kb_uses_same_barrier_code(client):
    uid, headers = await _register(client, "quiz-kc-preparing-auto")
    await _seed_kb(uid, document_status="ready")
    client._transport.app.state.settings.gateway_mode = "live"

    response = await client.post(
        "/api/quiz",
        headers=headers,
        json={"from_weak_points": True},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "QUIZ_KC_PREPARING"


async def test_legacy_automatic_failed_enrichment_uses_not_ready_barrier_code(client):
    uid, headers = await _register(client, "quiz-kc-not-ready-auto")
    await _seed_kb(uid, document_status="failed", with_kcs=False, with_children=False)
    client._transport.app.state.settings.gateway_mode = "live"

    response = await client.post(
        "/api/quiz",
        headers=headers,
        json={"from_weak_points": True},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "QUIZ_KC_NOT_READY"


async def test_kc_resolution_is_scoped_and_never_creates_on_a_miss(client):
    uid, _headers = await _register(client, "quiz-kc-scope")
    seeded = await _seed_kb(uid)

    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeBase, KnowledgeComponent, MasteryRecord
    from eduagents_api.services import QuizKCError, _resolve_kc_ids, _resolve_persisted_kc_ids

    tenant_id = uuid.UUID(uid)
    async with tenant_session(tenant_id) as session:
        other_kb = KnowledgeBase(tenant_id=tenant_id, name="另一个库", subject="math")
        session.add(other_kb)
        await session.flush()
        other_kc = KnowledgeComponent(
            tenant_id=tenant_id,
            kb_id=other_kb.id,
            canonical_name="另一个库知识点",
            aliases=[],
        )
        session.add(other_kc)
        session.add(
            MasteryRecord(
                tenant_id=tenant_id,
                user_id=tenant_id,
                kc_id=uuid.UUID(seeded["kc_1"]),
                p_mastery=0.3,
                confidence=0.5,
            )
        )
        await session.flush()
        before = await session.scalar(
            select(func.count())
            .select_from(KnowledgeComponent)
            .where(KnowledgeComponent.kb_id == uuid.UUID(seeded["kb_id"]))
        )
        resolved = await _resolve_kc_ids(
            session,
            seeded["kb_id"],
            ["章节一", "另一个库知识点", "章节一错别字"],
            user_id=tenant_id,
            tenant_id=tenant_id,
        )
        after = await session.scalar(
            select(func.count())
            .select_from(KnowledgeComponent)
            .where(KnowledgeComponent.kb_id == uuid.UUID(seeded["kb_id"]))
        )
    assert resolved == [seeded["kc_1"]]
    assert after == before

    async with tenant_session(tenant_id) as session:
        assert await _resolve_persisted_kc_ids(
            session,
            [seeded["kc_1"]],
            kb_id=None,
            tenant_id=tenant_id,
            user_id=tenant_id,
        ) == [seeded["kc_1"]]
        with pytest.raises(QuizKCError) as exc_info:
            await _resolve_persisted_kc_ids(
                session,
                [str(other_kc.id)],
                kb_id=None,
                tenant_id=tenant_id,
                user_id=tenant_id,
            )
        assert exc_info.value.code == "QUIZ_KC_NOT_FOUND"


async def test_finalize_quiz_rejects_unresolved_question_without_polluting_quiz(client):
    uid, _headers = await _register(client, "quiz-kc-finalize")
    seeded = await _seed_kb(uid)

    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import Quiz
    from eduagents_api.services import QuizKCError, finalize_quiz

    tenant_id = uuid.UUID(uid)
    ctx = AuthContext(
        user_id=tenant_id,
        tenant_id=tenant_id,
        role="student",
        jti="test",
    )
    local_id = str(uuid.uuid4())
    with pytest.raises(QuizKCError) as exc_info:
        await finalize_quiz(
            ctx,
            local_id,
            {
                "kb_id": seeded["kb_id"],
                "topic": "章节诊断",
                "gateway_mode": "stub",
                "questions": [
                    {
                        "type": "single",
                        "stem": "未知知识点题",
                        "options": ["A", "B", "C", "D"],
                        "answer": "A",
                        "explanation": "",
                        "kc_names": ["不存在的知识点"],
                        "difficulty": "easy",
                    }
                ],
            },
        )
    assert exc_info.value.code == "QUIZ_KC_REQUIRED"

    async with tenant_session(tenant_id) as session:
        assert await session.scalar(select(Quiz).where(Quiz.id == uuid.UUID(local_id))) is None


async def test_record_attempts_rejects_empty_kc_before_writing_any_evidence(client):
    uid, _headers = await _register(client, "quiz-kc-attempt")
    seeded = await _seed_kb(uid)

    from eduagents_api.db import tenant_session
    from eduagents_api.models import Attempt, LearningEvent, Question, Quiz
    from eduagents_api.services import record_attempts

    tenant_id = uuid.UUID(uid)
    async with tenant_session(tenant_id) as session:
        quiz = Quiz(
            tenant_id=tenant_id,
            user_id=tenant_id,
            kb_id=uuid.UUID(seeded["kb_id"]),
            title="坏 KC 卷",
            status="ready",
        )
        session.add(quiz)
        await session.flush()
        question = Question(
            tenant_id=tenant_id,
            quiz_id=quiz.id,
            type="single",
            stem="没有 KC 的题",
            options_json=["A", "B", "C", "D"],
            answer="A",
            explanation="",
            kc_ids=[],
            difficulty="easy",
        )
        session.add(question)
        await session.flush()
        quiz_id, question_id = quiz.id, question.id

    ctx = __import__("eduagents_api.deps", fromlist=["AuthContext"]).AuthContext(
        user_id=tenant_id, tenant_id=tenant_id, role="student", jti="test"
    )
    with pytest.raises(HTTPException) as exc_info:
        await record_attempts(
            ctx,
            gateway=None,
            quiz_id=quiz_id,
            answers=[{"question_id": question_id, "response": "A"}],
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "QUIZ_KC_REQUIRED"

    async with tenant_session(tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(Attempt)) == 0
        assert await session.scalar(select(func.count()).select_from(LearningEvent)) == 0


async def test_stub_three_question_quiz_uses_one_kc_batch_through_profile(client):
    uid, headers = await _register(client, "quiz-kc-loop")
    seeded = await _seed_kb(uid)

    from eduagents_api.graph_runtime import GraphRuntime
    from eduagents_core.config import get_settings
    from eduagents_core.graphs.state import GraphDeps

    app = client._transport.app
    settings = get_settings()
    runtime = GraphRuntime(
        settings,
        GraphDeps(settings=settings, gateway=_LoopGateway(), retrieval=None, web_search=None),
    )
    await runtime.start()
    old_runtime = app.state.graphs
    app.state.graphs = runtime
    try:
        events = await _sse(
            client,
            "/api/quiz",
            headers,
            {
                "topic": "章节诊断",
                "kb_id": seeded["kb_id"],
                "num_questions": 3,
                "difficulty_mix": {"easy": 1, "mid": 2, "hard": 0},
            },
        )
        assert events["interrupt"][0]["kind"] == "blueprint_confirm"
        done_events = await _sse(
            client,
            events["interrupt"][0]["resume_url"],
            headers,
            {"value": {"approved": True}},
        )
        done = done_events["done"][0]
        assert done["outcome"] == "succeeded"
        quiz_id = done["result"]["quiz_id"]

        detail = await client.get(f"/api/quiz/{quiz_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        questions = detail.json()["questions"]
        assert len(questions) == 3
        assert [q["difficulty"] for q in questions] == ["easy", "mid", "mid"]
        assert all(q["kc_ids"] for q in questions)
        assert {
            name
            for q in questions
            for name in KC_NAMES
            if q["kc_ids"] == [seeded[f"kc_{KC_NAMES.index(name) + 1}"]]
        } <= set(KC_NAMES)

        submit = await client.post(
            f"/api/quiz/{quiz_id}/attempts",
            headers=headers,
            json={
                "answers": [
                    {"question_id": q["id"], "response": "A"} for q in questions
                ]
            },
        )
        assert submit.status_code == 200, submit.text
        assert submit.json()["total"] == 3
    finally:
        app.state.graphs = old_runtime
        await runtime.stop()

    from eduagents_api.db import tenant_session
    from eduagents_api.models import LearningEvent, MasteryLog, MasteryRecord

    tenant_id = uuid.UUID(uid)
    expected_kcs = {seeded[f"kc_{index}"] for index in range(1, 4)}
    async with tenant_session(tenant_id) as session:
        events = (
            await session.scalars(
                select(LearningEvent).where(LearningEvent.source_type == "quiz_attempt")
            )
        ).all()
        records = (await session.scalars(select(MasteryRecord))).all()
        logs = (
            await session.scalars(
                select(MasteryLog).where(MasteryLog.evidence_type == "quiz_attempt")
            )
        ).all()
    assert {kc for event in events for kc in event.kc_ids_json} == expected_kcs
    assert {str(record.kc_id) for record in records} == expected_kcs
    assert {str(log.kc_id) for log in logs} == expected_kcs
    assert len(events) == len(logs) == len(records) == 3

    profile = await client.get("/api/profile", headers=headers)
    assert profile.status_code == 200, profile.text
    assert expected_kcs <= {row["kc_id"] for row in profile.json()["mastery"]}


async def test_real_stub_ingestion_bootstrap_quiz_submit_and_profile_loop(client):
    """Use only the KC rows produced by real stub ingestion for the cold start loop."""

    uid, headers = await _register(client, "quiz-kc-real-bootstrap")
    content = """# 第一章 函数

## 函数定义

函数定义与自变量、因变量的对应关系。

# 第二章 图像

## 图像性质

图像的顶点、对称轴和开口方向。

# 第三章 方程

## 方程应用

利用函数图像分析方程根的数量与位置。
"""

    kb_response = await client.post(
        "/api/kb", headers=headers, json={"name": "真实 stub 冷启动库", "subject": "math"}
    )
    assert kb_response.status_code == 201, kb_response.text
    kb_id = kb_response.json()["id"]
    document_response = await client.post(
        f"/api/kb/{kb_id}/documents",
        headers=headers,
        files={"file": ("real-stub-chapters.md", content.encode(), "text/markdown")},
    )
    assert document_response.status_code == 201, document_response.text
    document_id = document_response.json()["id"]

    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KnowledgeComponent
    from eduagents_worker.ingestion import ingest_document
    from eduagents_worker.kc_bootstrap import bootstrap_document_kcs

    tenant_id = uuid.UUID(uid)
    async with tenant_session(tenant_id) as session:
        before_kc_count = await session.scalar(
            select(func.count())
            .select_from(KnowledgeComponent)
            .where(KnowledgeComponent.kb_id == uuid.UUID(kb_id))
        )
    assert before_kc_count == 0

    # ingest_document(stub) performs the production ordering: child chunks,
    # chapter bootstrap, then the terminal ready status. The explicit replay
    # below verifies the public bootstrap operation is idempotent as well.
    assert await ingest_document({}, uid, document_id) == "ready"
    first_bootstrap = await bootstrap_document_kcs(uid, document_id)
    second_bootstrap = await bootstrap_document_kcs(uid, document_id)
    assert first_bootstrap.kc_ids == second_bootstrap.kc_ids
    assert first_bootstrap.chapter_count >= 3

    async with tenant_session(tenant_id) as session:
        document = await session.get(Document, uuid.UUID(document_id))
        kcs = (
            await session.scalars(
                select(KnowledgeComponent)
                .where(KnowledgeComponent.kb_id == uuid.UUID(kb_id))
                .order_by(KnowledgeComponent.stable_key)
            )
        ).all()
        children = (
            await session.scalars(
                select(Chunk).where(
                    Chunk.doc_id == uuid.UUID(document_id), Chunk.parent_id.isnot(None)
                )
            )
        ).all()
    assert document is not None and document.status == "ready"
    assert kcs and all(row.source_kind == "chapter_fallback" for row in kcs)
    generated_kc_ids = {str(row.id) for row in kcs}
    assert generated_kc_ids == set(first_bootstrap.kc_ids)
    assert children and all(child.kc_ids for child in children)
    assert all(set(child.kc_ids) <= generated_kc_ids for child in children)

    # Drive the quiz gateway from the names actually generated by bootstrap;
    # no KnowledgeComponent is inserted or hand-authored by this test.
    selected_kcs = kcs[:3]
    selected_names = tuple(row.canonical_name for row in selected_kcs)
    assert len(set(selected_names)) == 3
    selected_ids = {str(row.id) for row in selected_kcs}
    difficulties = ("easy", "mid", "mid")

    class GeneratedKcGateway:
        async def chat(self, request):
            from eduagents_core.gateway import ChatResult

            if request.feature == "quiz_blueprint":
                payload = {
                    "items": [
                        {"kc_name": name, "type": "single", "difficulty": difficulty}
                        for name, difficulty in zip(selected_names, difficulties, strict=True)
                    ],
                    "summary": "章节冷启动诊断",
                }
            elif request.feature == "quiz_verify":
                payload = {"issues": []}
            else:
                payload = {
                    "questions": [
                        {
                            "type": "single",
                            "stem": f"{name} 诊断题",
                            "options": ["A", "B", "C", "D"],
                            "answer": "A",
                            "explanation": "根据当前章节资料作答。",
                            "kc_names": [name],
                            "difficulty": difficulty,
                            "source_hint": None,
                        }
                        for name, difficulty in zip(selected_names, difficulties, strict=True)
                    ]
                }
            return ChatResult(
                text=json.dumps(payload, ensure_ascii=False),
                model="real-stub-kc-test",
                tier=request.tier.value,
                prompt_tokens=1,
                completion_tokens=1,
                cost_cny=0.0,
                latency_ms=1,
                mode="stub",
            )

    from eduagents_api.graph_runtime import GraphRuntime
    from eduagents_core.config import get_settings
    from eduagents_core.graphs.state import GraphDeps

    app = client._transport.app
    settings = get_settings()
    runtime = GraphRuntime(
        settings,
        GraphDeps(settings=settings, gateway=GeneratedKcGateway(), retrieval=None, web_search=None),
    )
    await runtime.start()
    old_runtime = app.state.graphs
    app.state.graphs = runtime
    try:
        events = await _sse(
            client,
            "/api/quiz",
            headers,
            {
                "topic": "真实章节冷启动",
                "kb_id": kb_id,
                "num_questions": 3,
                "difficulty_mix": {"easy": 1, "mid": 2, "hard": 0},
            },
        )
        interrupt = events["interrupt"][0]
        completed = await _sse(
            client,
            interrupt["resume_url"],
            headers,
            {"value": {"approved": True}},
        )
        done = completed["done"][0]
        assert done["outcome"] == "succeeded"
        questions = done["result"]["questions"]
        assert len(questions) == 3
        assert [question["difficulty"] for question in questions] == list(difficulties)
        assert all(question["kc_ids"] for question in questions)
        assert {kc_id for question in questions for kc_id in question["kc_ids"]} <= selected_ids
        assert {
            kc_id for question in questions for kc_id in question["kc_ids"]
        } == selected_ids

        quiz_id = done["result"]["quiz_id"]
        submit = await client.post(
            f"/api/quiz/{quiz_id}/attempts",
            headers=headers,
            json={
                "answers": [
                    {"question_id": question["id"], "response": "A"}
                    for question in questions
                ]
            },
        )
        assert submit.status_code == 200, submit.text
        assert submit.json()["total"] == 3
    finally:
        app.state.graphs = old_runtime
        await runtime.stop()

    from eduagents_api.models import LearningEvent, MasteryLog, MasteryRecord

    async with tenant_session(tenant_id) as session:
        learning_events = (
            await session.scalars(
                select(LearningEvent).where(LearningEvent.source_type == "quiz_attempt")
            )
        ).all()
        mastery_records = (await session.scalars(select(MasteryRecord))).all()
        mastery_logs = (
            await session.scalars(
                select(MasteryLog).where(MasteryLog.evidence_type == "quiz_attempt")
            )
        ).all()
    assert {kc_id for event in learning_events for kc_id in event.kc_ids_json} == selected_ids
    assert {str(record.kc_id) for record in mastery_records} == selected_ids
    assert {str(log.kc_id) for log in mastery_logs} == selected_ids
    assert len(learning_events) == len(mastery_records) == len(mastery_logs) == 3

    profile = await client.get("/api/profile", headers=headers)
    assert profile.status_code == 200, profile.text
    assert selected_ids <= {row["kc_id"] for row in profile.json()["mastery"]}
