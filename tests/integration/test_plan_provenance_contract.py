"""Contracts for plan provenance, course scope, and practice read models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from .conftest import requires_services
from .test_quiz_kc_loop import _register

pytestmark = requires_services


def _task(*, status: str = "pending") -> dict:
    return {
        "task_id": str(uuid.uuid4()),
        "title": "完成一组校准练习",
        "action_type": "practice",
        "kc_names": ["正式课程知识点"],
        "practice_count": 1,
        "status": status,
    }


def _structure(*tasks: dict) -> dict:
    return {"stages": [{"name": "基础", "days": [{"day": 1, "tasks": list(tasks)}]}]}


async def test_goal_only_attempt_cannot_update_formal_mastery_even_with_kc_input(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import (
        KnowledgeBase,
        KnowledgeComponent,
        LearningEvent,
        MasteryLog,
        MasteryRecord,
        Plan,
        Question,
    )
    from eduagents_api.plan_adjust import validate_plan_task_attribution
    from eduagents_api.services import finalize_quiz

    uid, headers = await _register(client, "goal-only-firewall")
    user_id = uuid.UUID(uid)
    task = _task()
    async with tenant_session(user_id) as session:
        kb = KnowledgeBase(tenant_id=user_id, name="正式课程", subject="math")
        session.add(kb)
        await session.flush()
        kc = KnowledgeComponent(
            tenant_id=user_id,
            kb_id=kb.id,
            canonical_name="正式课程知识点",
            aliases=[],
        )
        session.add(kc)
        await session.flush()
        record = MasteryRecord(
            tenant_id=user_id,
            user_id=user_id,
            kc_id=kc.id,
            p_mastery=0.77,
            confidence=0.8,
        )
        plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=None,
            goal="无资料起步",
            plan_mode="goal_only",
            grounding_status="unreferenced",
            structure_json=_structure(task),
        )
        session.add_all([record, plan])
        await session.flush()
        original_log = MasteryLog(
            tenant_id=user_id,
            user_id=user_id,
            kc_id=kc.id,
            old_p=0.77,
            new_p=0.77,
            evidence_type="quiz_attempt",
            evidence_id=uuid.uuid4(),
            weight=1.0,
        )
        session.add(original_log)
        await session.flush()
        attribution = await validate_plan_task_attribution(
            session,
            tenant_id=user_id,
            user_id=user_id,
            plan_id=plan.id,
            task_id=task["task_id"],
            source_type="quiz_attempt",
        )
        plan_id = plan.id
        kc_id = kc.id

    assert attribution["plan_mode"] == "goal_only"
    assert attribution["evidence_policy"] == "calibration_only"
    assert attribution["kb_id"] is None

    ctx = AuthContext(user_id=user_id, tenant_id=user_id, role="student", jti="test")
    quiz_id = uuid.uuid4()
    result = await finalize_quiz(
        ctx,
        str(quiz_id),
        {
            "topic": "目标起步校准",
            "kb_id": str(kb.id),
            "plan_id": str(plan_id),
            "task_id": task["task_id"],
            "plan_mode": attribution["plan_mode"],
            "evidence_policy": attribution["evidence_policy"],
            "blueprint": {},
            "questions": [
                {
                    "type": "single",
                    "stem": "校准题",
                    "options": ["A", "B", "C", "D"],
                    "answer": "A",
                    "explanation": "仅用于校准。",
                    "kc_names": ["正式课程知识点"],
                    "difficulty": "mid",
                }
            ],
        },
    )
    question = result["questions"][0]
    assert question["kc_ids"] == []

    submitted = await client.post(
        f"/api/quiz/{quiz_id}/attempts",
        headers=headers,
        json={"answers": [{"question_id": question["id"], "response": "A"}]},
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["evidence_policy"] == "calibration_only"
    assert body["results"][0]["mastery_changes"] == []
    assert body["plan_task_completion"]["completed"] is True
    detail = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert body["plan_task_completion"]["version"] == detail.json()["version"]

    async with tenant_session(user_id) as session:
        persisted_record = await session.scalar(
            select(MasteryRecord).where(
                MasteryRecord.user_id == user_id,
                MasteryRecord.kc_id == kc_id,
            )
        )
        log_count = await session.scalar(
            select(func.count()).select_from(MasteryLog).where(
                MasteryLog.user_id == user_id,
                MasteryLog.kc_id == kc_id,
            )
        )
        learning_event = await session.scalar(
            select(LearningEvent).where(
                LearningEvent.user_id == user_id,
                LearningEvent.source_type == "quiz_attempt",
            )
        )
        persisted_question = await session.get(Question, uuid.UUID(question["id"]))

    assert persisted_record is not None and persisted_record.p_mastery == 0.77
    assert log_count == 1
    assert learning_event is not None and learning_event.kc_ids_json == []
    assert persisted_question is not None and persisted_question.kc_ids == []


async def test_unattributed_no_kb_quiz_is_standalone_and_mastery_eligible(client):
    """Missing KB scope does not imply a goal-only calibration quiz."""
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import KnowledgeBase, KnowledgeComponent, MasteryRecord
    from eduagents_api.services import finalize_quiz, record_attempts

    uid, _headers = await _register(client, "standalone-no-kb")
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        kb = KnowledgeBase(tenant_id=user_id, name="已有知识点", subject="math")
        session.add(kb)
        await session.flush()
        kc = KnowledgeComponent(
            tenant_id=user_id,
            kb_id=kb.id,
            canonical_name="可独立练习的知识点",
            aliases=[],
        )
        session.add(kc)
        await session.flush()
        session.add(
            MasteryRecord(
                tenant_id=user_id,
                user_id=user_id,
                kc_id=kc.id,
                p_mastery=0.3,
                confidence=0.2,
            )
        )
        kc_name = kc.canonical_name
        original_p = 0.3

    ctx = AuthContext(user_id=user_id, tenant_id=user_id, role="student", jti="test")
    quiz_id = uuid.uuid4()
    result = await finalize_quiz(
        ctx,
        str(quiz_id),
        {
            "topic": "普通无资料练习",
            "kb_id": None,
            "plan_mode": "standalone",
            "evidence_policy": "mastery_eligible",
            "blueprint": {},
            "questions": [
                {
                    "type": "single",
                    "stem": "普通练习题",
                    "options": ["A", "B"],
                    "answer": "A",
                    "explanation": "解释",
                    "kc_names": [kc_name],
                    "difficulty": "mid",
                }
            ],
        },
    )
    assert result["questions"][0]["kc_ids"]

    attempt_result = await record_attempts(
        ctx,
        gateway=None,
        quiz_id=quiz_id,
        answers=[
            {
                "question_id": result["questions"][0]["id"],
                "response": "A",
            }
        ],
    )
    assert attempt_result["evidence_policy"] == "mastery_eligible"
    assert attempt_result["results"][0]["mastery_changes"]

    async with tenant_session(user_id) as session:
        mastery = await session.scalar(
            select(MasteryRecord).where(
                MasteryRecord.user_id == user_id,
                MasteryRecord.kc_id == kc.id,
            )
        )
    assert mastery is not None and mastery.p_mastery > original_p


async def test_calibration_policy_requires_goal_only_plan_attribution(client):
    from eduagents_api.deps import AuthContext
    from eduagents_api.services import QuizKCError, finalize_quiz

    uid, _headers = await _register(client, "calibration-attribution-guard")
    user_id = uuid.UUID(uid)
    ctx = AuthContext(user_id=user_id, tenant_id=user_id, role="student", jti="test")
    with pytest.raises(QuizKCError) as exc_info:
        await finalize_quiz(
            ctx,
            str(uuid.uuid4()),
            {
                "topic": "伪造校准练习",
                "plan_mode": "goal_only",
                "evidence_policy": "calibration_only",
                "questions": [
                    {
                        "type": "single",
                        "stem": "题目",
                        "options": ["A", "B", "C", "D"],
                        "answer": "A",
                        "explanation": "解释",
                        "kc_names": ["临时概念"],
                        "difficulty": "mid",
                    }
                ],
            },
        )
    assert exc_info.value.code == "INVALID_QUIZ_PROVENANCE"


async def test_plan_personalization_is_isolated_to_the_selected_kb(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import (
        KcEdge,
        KnowledgeBase,
        KnowledgeComponent,
        MasteryLog,
        MasteryRecord,
    )
    from eduagents_api.routers.plans import _load_personalization

    uid, _headers = await _register(client, "plan-kb-isolation")
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        selected = KnowledgeBase(tenant_id=user_id, name="所选课程", subject="math")
        other = KnowledgeBase(tenant_id=user_id, name="其他课程", subject="math")
        session.add_all([selected, other])
        await session.flush()
        prerequisite = KnowledgeComponent(
            tenant_id=user_id,
            kb_id=selected.id,
            canonical_name="所选前置",
            aliases=[],
        )
        target = KnowledgeComponent(
            tenant_id=user_id,
            kb_id=selected.id,
            canonical_name="所选目标",
            aliases=[],
        )
        foreign = KnowledgeComponent(
            tenant_id=user_id,
            kb_id=other.id,
            canonical_name="跨库强项",
            aliases=[],
        )
        session.add_all([prerequisite, target, foreign])
        await session.flush()
        session.add(
            KcEdge(
                tenant_id=user_id,
                from_kc=prerequisite.id,
                to_kc=target.id,
                type="prerequisite",
            )
        )
        for kc, score in ((prerequisite, 0.25), (target, 0.45), (foreign, 0.99)):
            session.add(
                MasteryRecord(
                    tenant_id=user_id,
                    user_id=user_id,
                    kc_id=kc.id,
                    p_mastery=score,
                    confidence=0.8,
                )
            )
            for _ in range(3):
                session.add(
                    MasteryLog(
                        tenant_id=user_id,
                        user_id=user_id,
                        kc_id=kc.id,
                        old_p=score,
                        new_p=score,
                        evidence_type="quiz_attempt",
                        evidence_id=uuid.uuid4(),
                        weight=1.0,
                    )
                )
        selected_id = selected.id

    ctx = AuthContext(user_id=user_id, tenant_id=user_id, role="student", jti="test")
    diagnosis, kc_order = await _load_personalization(ctx, str(selected_id))

    assert set(diagnosis) == {"所选前置", "所选目标"}
    assert diagnosis["所选前置"] == {
        "evidence_state": "observed",
        "mastery_band": "较弱",
    }
    assert "跨库强项" not in diagnosis
    assert kc_order == ["所选前置", "所选目标"]


async def test_course_summary_api_reports_sources_questions_plan_and_next_action(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        Document,
        KnowledgeBase,
        LearningResource,
        Plan,
        Question,
        ResourceQuestion,
    )

    uid, headers = await _register(client, "course-summary")
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        course = KnowledgeBase(tenant_id=user_id, name="进行中的课程", subject="math")
        empty_course = KnowledgeBase(tenant_id=user_id, name="空课程", subject="math")
        session.add_all([course, empty_course])
        await session.flush()
        for index, status in enumerate(("enriched", "parsing", "failed"), start=1):
            session.add(
                Document(
                    tenant_id=user_id,
                    kb_id=course.id,
                    filename=f"source-{index}.md",
                    content_hash=uuid.uuid4().hex,
                    size_bytes=100 * index,
                    status=status,
                )
            )
        resource = LearningResource(
            tenant_id=user_id,
            owner_type="personal",
            owner_id=user_id,
            created_by=user_id,
            resource_type="worksheet",
            title="课程题目资料",
            linked_kb_id=course.id,
        )
        session.add(resource)
        await session.flush()
        verified = Question(
            tenant_id=user_id,
            type="single",
            stem="已核验题",
            options_json=["A", "B", "C", "D"],
            answer="A",
            explanation="解释",
            source_type="document",
        )
        pending = Question(
            tenant_id=user_id,
            type="single",
            stem="待核验题",
            options_json=["A", "B", "C", "D"],
            answer="A",
            explanation="解释",
            source_type="document",
        )
        session.add_all([verified, pending])
        await session.flush()
        session.add_all(
            [
                ResourceQuestion(
                    tenant_id=user_id,
                    resource_id=resource.id,
                    question_id=verified.id,
                    ordinal=1,
                    verification_status="verified",
                ),
                ResourceQuestion(
                    tenant_id=user_id,
                    resource_id=resource.id,
                    question_id=pending.id,
                    ordinal=2,
                    verification_status="pending",
                ),
            ]
        )
        plan = Plan(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=course.id,
            goal="完成课程",
            plan_mode="course_bound",
            grounding_status="kb_grounded",
            structure_json=_structure(_task(status="done"), _task(status="pending")),
        )
        session.add(plan)
        await session.flush()
        course_id = str(course.id)
        empty_course_id = str(empty_course.id)
        plan_id = str(plan.id)

    response = await client.get("/api/kb", headers=headers)
    assert response.status_code == 200, response.text
    by_id = {item["id"]: item for item in response.json()}

    summary = by_id[course_id]["summary"]
    assert {key: summary[key] for key in (
        "source_total",
        "source_ready",
        "source_processing",
        "source_failed",
        "verified_questions",
        "pending_questions",
        "plan_id",
        "plan_status",
        "plan_done_tasks",
        "plan_total_tasks",
        "plan_progress_ratio",
        "next_action",
    )} == {
        "source_total": 3,
        "source_ready": 1,
        "source_processing": 1,
        "source_failed": 1,
        "verified_questions": 1,
        "pending_questions": 1,
        "plan_id": plan_id,
        "plan_status": "active",
        "plan_done_tasks": 1,
        "plan_total_tasks": 2,
        "plan_progress_ratio": 0.5,
        "next_action": "continue_plan",
    }
    assert summary["last_activity_at"] is not None

    empty_summary = by_id[empty_course_id]["summary"]
    assert empty_summary["source_total"] == 0
    assert empty_summary["plan_id"] is None
    assert empty_summary["next_action"] == "upload_source"


async def test_quiz_history_exposes_scan_friendly_attempt_and_provenance_metadata(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        Attempt,
        ErrorItem,
        KnowledgeBase,
        KnowledgeComponent,
        Question,
        Quiz,
    )

    uid, headers = await _register(client, "quiz-history-metadata")
    user_id = uuid.UUID(uid)
    async with tenant_session(user_id) as session:
        kb = KnowledgeBase(tenant_id=user_id, name="历史元数据课程", subject="math")
        session.add(kb)
        await session.flush()
        first_kc = KnowledgeComponent(
            tenant_id=user_id,
            kb_id=kb.id,
            canonical_name="知识点一",
            aliases=[],
        )
        second_kc = KnowledgeComponent(
            tenant_id=user_id,
            kb_id=kb.id,
            canonical_name="知识点二",
            aliases=[],
        )
        session.add_all([first_kc, second_kc])
        await session.flush()
        quiz = Quiz(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=kb.id,
            title="课程巩固卷",
            status="ready",
            blueprint_json={
                "_evidence_policy": "mastery_eligible",
                "_plan_mode": "course_bound",
            },
        )
        calibration_quiz = Quiz(
            tenant_id=user_id,
            user_id=user_id,
            kb_id=None,
            title="目标起步校准卷",
            status="ready",
            blueprint_json={
                "_evidence_policy": "calibration_only",
                "_plan_mode": "goal_only",
            },
        )
        session.add_all([quiz, calibration_quiz])
        await session.flush()
        questions = [
            Question(
                tenant_id=user_id,
                quiz_id=quiz.id,
                type="single",
                stem=f"第 {index} 题",
                options_json=["A", "B", "C", "D"],
                answer="A",
                explanation="解释",
                kc_ids=kc_ids,
                difficulty="mid",
            )
            for index, kc_ids in enumerate(
                ([str(first_kc.id)], [str(second_kc.id)], [str(first_kc.id)]),
                start=1,
            )
        ]
        session.add_all(questions)
        await session.flush()
        correct_attempt = Attempt(
            tenant_id=user_id,
            user_id=user_id,
            question_id=questions[0].id,
            response="A",
            is_correct=True,
            at=datetime.now(UTC),
        )
        wrong_attempt = Attempt(
            tenant_id=user_id,
            user_id=user_id,
            question_id=questions[1].id,
            response="B",
            is_correct=False,
            at=datetime.now(UTC),
        )
        session.add_all([correct_attempt, wrong_attempt])
        await session.flush()
        error = ErrorItem(
            tenant_id=user_id,
            user_id=user_id,
            question_id=questions[1].id,
            kc_ids=[str(second_kc.id)],
            source_attempt_id=wrong_attempt.id,
            verification_status="verified",
        )
        session.add(error)
        calibration_question = Question(
            tenant_id=user_id,
            quiz_id=calibration_quiz.id,
            type="single",
            stem="校准题",
            options_json=["A", "B", "C", "D"],
            answer="A",
            explanation="解释",
            kc_ids=[],
            difficulty="mid",
        )
        session.add(calibration_question)
        await session.flush()
        quiz_id = str(quiz.id)
        calibration_quiz_id = str(calibration_quiz.id)
        error_question_id = str(questions[1].id)

    response = await client.get("/api/quiz", headers=headers)
    assert response.status_code == 200, response.text
    history = {item["id"]: item for item in response.json()}
    item = history[quiz_id]

    assert item["question_count"] == 3
    assert item["attempt_count"] == 2
    assert item["score"] == 0.5
    assert item["error_count"] == 1
    assert item["error_question_ids"] == [error_question_id]
    assert item["kc_count"] == 2
    assert item["evidence_policy"] == "mastery_eligible"
    assert item["plan_mode"] == "course_bound"

    calibration = history[calibration_quiz_id]
    assert calibration["question_count"] == 1
    assert calibration["attempt_count"] == 0
    assert calibration["score"] is None
    assert calibration["error_count"] == 0
    assert calibration["kc_count"] == 0
    assert calibration["evidence_policy"] == "calibration_only"
    assert calibration["plan_mode"] == "goal_only"
