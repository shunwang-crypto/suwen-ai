"""Wave 2 recommendation API: server-owned facts and stale-decision barrier."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from eduagents_api.db import tenant_session
from eduagents_api.models import ErrorItem, MasteryLog, MasteryRecord, Plan, Question, Quiz
from eduagents_api.practice_recommendation import recommend_practice
from sqlalchemy import select

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


async def test_legacy_weak_point_plan_task_keeps_server_owned_attribution(client):
    """The compatibility entry point must persist the selected plan task."""
    uid, headers = await _register(client, "practice-legacy-plan")
    seeded = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        task_id = str(uuid.uuid4())
        plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=uuid.UUID(seeded["kb_id"]),
            goal="按章节完成计划",
            plan_mode="course_bound",
            grounding_status="kb_grounded",
            structure_json={
                "stages": [{
                    "name": "基础",
                    "days": [{
                        "day": 1,
                        "tasks": [{
                            "task_id": task_id, "title": "章节一练习",
                            "action_type": "practice",
                            "kc_names": ["章节一"],
                            "practice_count": 3,
                        }],
                    }],
                }],
            },
        )
        session.add(plan)
        await session.flush()
        plan_id = str(plan.id)

    from eduagents_api.graph_runtime import GraphRuntime
    from eduagents_core.config import get_settings
    from eduagents_core.gateway import ChatResult
    from eduagents_core.graphs.state import GraphDeps

    class PlanGateway:
        async def chat(self, request):
            if request.feature == "quiz_blueprint":
                payload = {
                    "items": [
                        {"kc_name": "章节一", "type": "single", "difficulty": "mid"}
                        for _ in range(3)
                    ],
                    "summary": "章节一计划练习",
                }
            elif request.feature == "quiz_verify":
                payload = {"issues": [], "summary": "题目质量合格"}
            else:
                payload = {
                    "questions": [
                        {
                            "type": "single",
                            "stem": f"章节一第 {index} 题",
                            "options": ["A", "B", "C", "D"],
                            "answer": "A",
                            "explanation": "章节一基础概念。",
                            "kc_names": ["章节一"],
                            "difficulty": "mid",
                            "source_hint": None,
                        }
                        for index in range(3)
                    ]
                }
            return ChatResult(
                text=__import__("json").dumps(payload, ensure_ascii=False),
                model="legacy-plan-test",
                tier=request.tier.value,
                prompt_tokens=1,
                completion_tokens=1,
                cost_cny=0.0,
                latency_ms=1,
                mode="stub",
            )

    app = client._transport.app
    app.state.settings.gateway_mode = "stub"
    settings = get_settings()
    runtime = GraphRuntime(
        settings,
        GraphDeps(settings=settings, gateway=PlanGateway(), retrieval=None, web_search=None),
    )
    await runtime.start()
    previous = app.state.graphs
    app.state.graphs = runtime
    try:
        events = await _sse(
            client,
            "/api/quiz",
            headers,
            {"from_weak_points": True, "num_questions": 3},
        )
        if "interrupt" in events:
            interrupt = events["interrupt"][0]
            events = await _sse(
                client,
                interrupt["resume_url"],
                headers,
                {"value": {"approved": True}},
            )
    finally:
        app.state.graphs = previous
        await runtime.stop()

    assert "done" in events, events
    done = events["done"][0]
    assert done["outcome"] == "succeeded", events
    quiz_id = done["result"]["quiz_id"]
    async with tenant_session(user_id) as session:
        quiz = await session.get(Quiz, uuid.UUID(quiz_id))
        assert quiz is not None
        assert quiz.blueprint_json["_plan_task_attribution"] == {
            "plan_id": plan_id,
            "task_id": task_id,
        }
        questions = (await session.scalars(
            select(Question).where(Question.quiz_id == quiz.id)
        )).all()
        assert len(questions) == 3
        question_ids = [str(question.id) for question in questions]

    submitted = await client.post(
        f"/api/quiz/{quiz_id}/attempts",
        headers=headers,
        json={
            "answers": [
                {"question_id": question_id, "response": "A"}
                for question_id in question_ids
            ]
        },
    )
    assert submitted.status_code == 200, submitted.text
    completion = submitted.json()["plan_task_completion"]
    assert completion["completed"] is True
    assert completion["task_id"] == task_id


async def test_legacy_plan_context_cannot_silently_switch_after_recommendation_changes(
    client, monkeypatch
):
    uid, headers = await _register(client, "practice-legacy-stale-plan")
    seeded = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    now = datetime.now(UTC)
    async with tenant_session(user_id) as session:
        task_id = str(uuid.uuid4())
        old_plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=uuid.UUID(seeded["kb_id"]),
            goal="旧计划",
            plan_mode="course_bound",
            grounding_status="kb_grounded",
            status="archived",
            created_at=now,
            structure_json={
                "stages": [{"name": "旧", "days": [{"day": 1, "tasks": [{
                    "task_id": task_id, "title": "旧任务", "action_type": "practice", "kc_names": ["章节一"]
                }]}]}],
            },
        )
        new_plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=uuid.UUID(seeded["kb_id"]),
            goal="新计划",
            plan_mode="course_bound",
            grounding_status="kb_grounded",
            status="active",
            created_at=now + timedelta(seconds=1),
            structure_json={
                "stages": [{"name": "新", "days": [{"day": 1, "tasks": [{
                    "task_id": str(uuid.uuid4()), "title": "新任务",
                    "action_type": "practice", "kc_names": ["章节二"]
                }]}]}],
            },
        )
        session.add_all([old_plan, new_plan])
        await session.flush()
        old_plan_id = str(old_plan.id)

    app = client._transport.app
    app.state.settings.gateway_mode = "stub"

    async def unexpected_graph(*_args, **_kwargs):
        pytest.fail("stale legacy plan must be rejected before entering the Quiz graph")

    monkeypatch.setattr("eduagents_api.routers.quiz.stream_graph_run", unexpected_graph)
    response = await client.post(
        "/api/quiz",
        headers=headers,
        json={
            "from_weak_points": True,
            "plan_id": old_plan_id,
            "task_id": task_id,
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "PRACTICE_DECISION_CHANGED"


async def test_legacy_non_plan_recommendation_does_not_add_plan_payload(client, monkeypatch):
    uid, headers = await _register(client, "practice-legacy-no-plan")
    await _seed_kb(uid)
    app = client._transport.app
    app.state.settings.gateway_mode = "stub"
    captured: dict = {}

    async def capture_payload(_runtime, _kind, _thread_id, _ctx, payload, **_kwargs):
        captured.update(payload)
        return {"captured": True}

    monkeypatch.setattr("eduagents_api.routers.quiz.stream_graph_run", capture_payload)
    response = await client.post(
        "/api/quiz",
        headers=headers,
        json={"from_weak_points": True, "num_questions": 3},
    )
    assert response.status_code == 200, response.text
    assert "plan_id" not in captured
    assert "task_id" not in captured


async def test_weak_point_preview_recommendation_and_quiz_trace_share_recent_error_order(client):
    uid, headers = await _register(client, "practice-weak-order")
    seeded = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        session.add_all([
            MasteryRecord(
                tenant_id=user_id,
                user_id=user_id,
                kc_id=uuid.UUID(seeded["kc_1"]),
                p_mastery=0.18,
                confidence=0.8,
            ),
            MasteryRecord(
                tenant_id=user_id,
                user_id=user_id,
                kc_id=uuid.UUID(seeded["kc_2"]),
                p_mastery=0.32,
                confidence=0.8,
            ),
        ])
        for kc_key, p in (("kc_1", 0.18), ("kc_2", 0.32)):
            for _ in range(3):
                session.add(
                    MasteryLog(
                        tenant_id=user_id,
                        user_id=user_id,
                        kc_id=uuid.UUID(seeded[kc_key]),
                        old_p=p,
                        new_p=p,
                        evidence_type="quiz_attempt",
                        evidence_id=uuid.uuid4(),
                        weight=1.0,
                    )
                )
        quiz = Quiz(tenant_id=user_id, user_id=user_id, title="近期错题", status="ready")
        session.add(quiz)
        await session.flush()
        question = Question(
            tenant_id=user_id,
            quiz_id=quiz.id,
            type="single",
            stem="章节二题",
            options_json=["A", "B"],
            answer="A",
            explanation="",
            kc_ids=[seeded["kc_2"]],
        )
        session.add(question)
        await session.flush()
        session.add(ErrorItem(
            tenant_id=user_id,
            user_id=user_id,
            question_id=question.id,
            kc_ids=[seeded["kc_2"]],
            state="archived",
            verification_status="verified",
        ))

    app = client._transport.app
    app.state.settings.gateway_mode = "stub"
    preview = await client.get("/api/profile/weak-points", headers=headers)
    assert preview.status_code == 200, preview.text
    preview_names = [item["name"] for item in preview.json()["weak_points"]]
    assert preview_names[:2] == ["章节二", "章节一"]

    async with tenant_session(user_id) as session:
        recommendation = await recommend_practice(
            session,
            tenant_id=user_id,
            user_id=user_id,
            gateway_mode="stub",
            now_dt=datetime.now(UTC),
        )
    assert list(recommendation.target_kc_names[:2]) == preview_names[:2]
    assert all(key not in str(recommendation.to_dict()) for key in ("p_mastery", "confidence", "mastery_band"))

    from eduagents_api.graph_runtime import GraphRuntime
    from eduagents_core.config import get_settings
    from eduagents_core.graphs.state import GraphDeps

    settings = get_settings()
    runtime = GraphRuntime(
        settings,
        GraphDeps(settings=settings, gateway=_LoopGateway(), retrieval=None, web_search=None),
    )
    await runtime.start()
    previous = app.state.graphs
    app.state.graphs = runtime
    try:
        events = await _sse(
            client,
            "/api/quiz",
            headers,
            {"from_weak_points": True, "num_questions": 2},
        )
    finally:
        app.state.graphs = previous
        await runtime.stop()

    selected = [event for event in events["trace_event"] if event.get("event") == "weak_points_selected"]
    assert selected[0]["kc_names"] == preview_names


async def test_plan_task_switch_kc_overrides_plan_default_and_changes_target(client):
    uid, headers = await _register(client, "practice-adjust-plan-kc")
    seeded = await _seed_kb(uid)
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        task_id = str(uuid.uuid4())
        plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=__import__("uuid").UUID(seeded["kb_id"]),
            goal="计划练习",
            plan_mode="course_bound",
            grounding_status="kb_grounded",
            structure_json={
                "stages": [{"name": "基础", "days": [{"day": 1, "tasks": [{
                    "task_id": task_id, "title": "计划 KC 一",
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
            task_id=task_id,
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
        task_id = str(uuid.uuid4())
        plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=__import__("uuid").UUID(first["kb_id"]),
            goal="计划练习",
            plan_mode="course_bound",
            grounding_status="kb_grounded",
            structure_json={
                "stages": [{"name": "基础", "days": [{"day": 1, "tasks": [{
                    "task_id": task_id, "title": "计划科目一",
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
            task_id=task_id,
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
