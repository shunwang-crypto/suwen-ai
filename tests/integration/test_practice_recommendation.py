"""Wave 2 recommendation API: server-owned facts and stale-decision barrier."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eduagents_api.db import tenant_session
from eduagents_api.models import Plan, Question, Quiz
from eduagents_api.practice_recommendation import recommend_practice

from .conftest import requires_services
from .test_quiz_kc_loop import _LoopGateway, _register, _seed_kb, _sse

pytestmark = requires_services


async def test_next_practice_returns_cold_start_for_a_ready_kb(client):
    uid, headers = await _register(client, "practice-next-cold")
    seeded = await _seed_kb(uid)
    client._transport.app.state.settings.gateway_mode = "stub"

    response = await client.get("/api/practice/next", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["action"] == "quiz"
    assert body["source"] == "cold_start"
    assert body["question_count"] == 3
    assert body["difficulty_mix"] == {"easy": 1, "mid": 2, "hard": 0}
    assert body["kb"]["id"] == seeded["kb_id"]


async def test_multiple_ready_kbs_return_scope_and_recommended_post_is_barriered(client):
    uid, headers = await _register(client, "practice-next-scope")
    await _seed_kb(uid)
    await _seed_kb(uid)
    client._transport.app.state.settings.gateway_mode = "stub"

    response = await client.get("/api/practice/next", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_scope"
    assert response.json()["action"] == "choose_scope"
    assert len(response.json()["scope_options"]) == 2

    stale = await client.post("/api/quiz/recommended", headers=headers, json={})
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "PRACTICE_DECISION_CHANGED"
    assert stale.json()["recommendation"]["status"] == "needs_scope"


async def test_recommended_request_rejects_freeform_topic_and_difficulty(client):
    uid, headers = await _register(client, "practice-next-safe-input")
    await _seed_kb(uid)

    response = await client.post(
        "/api/quiz/recommended",
        headers=headers,
        json={"topic": "越权主题", "difficulty_mix": {"easy": 20}},
    )

    assert response.status_code == 422, response.text


async def test_recommended_cold_start_uses_auto_confirmation_and_real_kcs(client):
    uid, headers = await _register(client, "practice-next-auto")
    await _seed_kb(uid)
    client._transport.app.state.settings.gateway_mode = "stub"

    from eduagents_api.graph_runtime import GraphRuntime
    from eduagents_core.config import get_settings
    from eduagents_core.graphs.state import GraphDeps

    app = client._transport.app
    runtime = GraphRuntime(
        get_settings(),
        GraphDeps(settings=get_settings(), gateway=_LoopGateway(), retrieval=None, web_search=None),
    )
    await runtime.start()
    previous = app.state.graphs
    app.state.graphs = runtime
    try:
        events = await _sse(client, "/api/quiz/recommended", headers, {})
    finally:
        app.state.graphs = previous
        await runtime.stop()

    assert "interrupt" not in events
    assert events["done"][0]["outcome"] == "succeeded"
    questions = events["done"][0]["result"]["questions"]
    assert len(questions) == 3
    assert all(question["kc_ids"] for question in questions)


async def test_plan_task_switch_kc_overrides_plan_default_and_changes_target(client):
    uid, headers = await _register(client, "practice-adjust-plan-kc")
    seeded = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=__import__("uuid").UUID(seeded["kb_id"]),
            goal="计划练习",
            structure_json={
                "stages": [{"name": "基础", "days": [{"day": 1, "tasks": [{
                    "title": "计划 KC 一",
                    "action_type": "practice",
                    "kc_names": ["章节一"],
                    "practice_count": 3,
                }]}]}],
            },
        )
        quiz = Quiz(tenant_id=user_id, user_id=user_id, kb_id=__import__("uuid").UUID(seeded["kb_id"]), title="上一份练习", status="ready")
        session.add_all([plan, quiz])
        await session.flush()
        session.add(Question(
            tenant_id=user_id,
            quiz_id=quiz.id,
            type="single",
            stem="上一份题",
            options_json=["A", "B"],
            answer="A",
            explanation="解释",
            kc_ids=[seeded["kc_1"]],
        ))
        await session.flush()
        plan_id, quiz_id = plan.id, quiz.id

        recommendation = await recommend_practice(
            session,
            tenant_id=user_id,
            user_id=user_id,
            gateway_mode="stub",
            now_dt=datetime.now(UTC),
            plan_id=plan_id,
            task_key="stage0.day0.task0",
            previous_quiz_id=quiz_id,
            adjustment="switch_kc",
        )

    assert recommendation.action == "quiz"
    assert recommendation.plan_id is None
    assert recommendation.target_kc_ids
    assert seeded["kc_1"] not in recommendation.target_kc_ids


async def test_plan_task_switch_kb_changes_ready_kb(client):
    uid, headers = await _register(client, "practice-adjust-plan-kb")
    first = await _seed_kb(uid)
    second = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=__import__("uuid").UUID(first["kb_id"]),
            goal="计划练习",
            structure_json={
                "stages": [{"name": "基础", "days": [{"day": 1, "tasks": [{
                    "title": "计划科目一",
                    "action_type": "practice",
                    "kc_names": ["章节一"],
                    "practice_count": 3,
                }]}]}],
            },
        )
        quiz = Quiz(tenant_id=user_id, user_id=user_id, kb_id=__import__("uuid").UUID(first["kb_id"]), title="上一份练习", status="ready")
        session.add_all([plan, quiz])
        await session.flush()
        recommendation = await recommend_practice(
            session,
            tenant_id=user_id,
            user_id=user_id,
            gateway_mode="stub",
            now_dt=datetime.now(UTC),
            plan_id=plan.id,
            task_key="stage0.day0.task0",
            previous_quiz_id=quiz.id,
            adjustment="switch_kb",
        )

    assert recommendation.action == "quiz"
    assert recommendation.plan_id is None
    assert recommendation.kb is not None
    assert recommendation.kb["id"] == second["kb_id"]


async def test_previous_quiz_must_belong_to_requesting_user(client):
    owner_uid, owner_headers = await _register(client, "practice-adjust-owner")
    other_uid, other_headers = await _register(client, "practice-adjust-other")
    seeded = await _seed_kb(owner_uid)
    owner_id = uuid.UUID(owner_uid)
    async with tenant_session(owner_id) as session:
        quiz = Quiz(tenant_id=owner_id, user_id=owner_id, kb_id=uuid.UUID(seeded["kb_id"]), title="仅本人", status="ready")
        session.add(quiz)
        await session.flush()
        quiz_id = str(quiz.id)

    response = await client.post(
        "/api/quiz/recommended",
        headers=other_headers,
        json={"previous_quiz_id": quiz_id, "adjustment": "switch_kc"},
    )
    assert response.status_code == 404, response.text


async def test_switch_kc_with_no_alternative_returns_409_without_reoffering_same_action(client):
    uid, headers = await _register(client, "practice-adjust-no-kc")
    seeded = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        quiz = Quiz(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=uuid.UUID(seeded["kb_id"]),
            title="已覆盖全部知识点",
            status="ready",
        )
        session.add(quiz)
        await session.flush()
        for index in range(1, 4):
            session.add(
                Question(
                    tenant_id=user_id,
                    quiz_id=quiz.id,
                    type="single",
                    stem=f"上一份第 {index} 题",
                    options_json=["A", "B"],
                    answer="A",
                    explanation="解释",
                    kc_ids=[seeded[f"kc_{index}"]],
                )
            )
        await session.flush()
        previous_quiz_id = str(quiz.id)

    response = await client.post(
        "/api/quiz/recommended",
        headers=headers,
        json={"previous_quiz_id": previous_quiz_id, "adjustment": "switch_kc"},
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "PRACTICE_DECISION_CHANGED"
    assert "switch_kc" not in body["recommendation"]["adjustments"]


async def test_switch_kb_with_one_ready_kb_returns_409_without_same_subject(client):
    uid, headers = await _register(client, "practice-adjust-no-kb")
    seeded = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        quiz = Quiz(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=uuid.UUID(seeded["kb_id"]),
            title="唯一科目",
            status="ready",
        )
        session.add(quiz)
        await session.flush()
        previous_quiz_id = str(quiz.id)

    response = await client.post(
        "/api/quiz/recommended",
        headers=headers,
        json={"previous_quiz_id": previous_quiz_id, "adjustment": "switch_kb"},
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "PRACTICE_DECISION_CHANGED"
    assert "switch_kb" not in body["recommendation"]["adjustments"]


async def test_adjustment_after_due_review_returns_latest_review_recommendation(client):
    uid, headers = await _register(client, "practice-adjust-due")
    seeded = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        quiz = Quiz(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=uuid.UUID(seeded["kb_id"]),
            title="上一份练习",
            status="ready",
        )
        session.add(quiz)
        await session.flush()
        question = Question(
            tenant_id=user_id,
            quiz_id=quiz.id,
            type="single",
            stem="到期错题",
            options_json=["A", "B"],
            answer="A",
            explanation="解释",
            kc_ids=[seeded["kc_1"]],
        )
        session.add(question)
        await session.flush()
        from eduagents_api.models import ErrorItem

        session.add(
            ErrorItem(
                tenant_id=user_id,
                user_id=user_id,
                question_id=question.id,
                kc_ids=[seeded["kc_1"]],
                verification_status="verified",
                fsrs_card_json={},
            )
        )
        await session.flush()
        previous_quiz_id = str(quiz.id)

    response = await client.post(
        "/api/quiz/recommended",
        headers=headers,
        json={"previous_quiz_id": previous_quiz_id, "adjustment": "easier"},
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "PRACTICE_DECISION_CHANGED"
    assert body["recommendation"]["action"] == "review"
    assert body["recommendation"]["source"] == "due_review"
