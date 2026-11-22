"""Real transactions: failed scoring, replay, retry and evidence stay consistent."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from eduagents_api.db import tenant_session
from eduagents_api.models import (
    AssessmentSubmission,
    Attempt,
    ErrorItem,
    FsrsReviewEvent,
    KnowledgeBase,
    KnowledgeComponent,
    LearningEvent,
    MasteryLog,
    Question,
    Quiz,
)
from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = [requires_services, pytest.mark.asyncio]


class Grader:
    def __init__(self, raw="broken"):
        self.raw = raw
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        await asyncio.sleep(0.02)
        if isinstance(self.raw, Exception):
            raise self.raw
        return SimpleNamespace(text=self.raw)


async def seed(client, *, mixed=False, review=False):
    email = f"submission-{uuid.uuid4().hex}@test.eduagents.cn"
    response = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "StrongPass123!",
            "birth_year": 1995,
        },
    )
    assert response.status_code == 201, response.text
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": "StrongPass123!"}
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = await client.get("/api/auth/me", headers=headers)
    uid = uuid.UUID(me.json()["id"])
    async with tenant_session(uid) as session:
        kb = KnowledgeBase(tenant_id=uid, name="测试课程", subject="math")
        session.add(kb)
        await session.flush()
        kc = KnowledgeComponent(tenant_id=uid, kb_id=kb.id, canonical_name="函数")
        session.add(kc)
        quiz = Quiz(tenant_id=uid, user_id=uid, kb_id=kb.id, title="评分测试", status="ready")
        session.add(quiz)
        await session.flush()
        short = Question(
            tenant_id=uid,
            quiz_id=quiz.id,
            type="short",
            stem="解释函数",
            answer="对应关系",
            explanation="参考解释",
            kc_ids=[str(kc.id)],
        )
        session.add(short)
        await session.flush()
        answers = [{"question_id": str(short.id), "response": "学生原始答案"}]
        if mixed:
            objective = Question(
                tenant_id=uid,
                quiz_id=quiz.id,
                type="single",
                stem="选择",
                answer="A",
                options_json=["A", "B"],
                kc_ids=[str(kc.id)],
            )
            session.add(objective)
            await session.flush()
            answers.append({"question_id": str(objective.id), "response": "A"})
        error_id = None
        if review:
            error = ErrorItem(tenant_id=uid, user_id=uid, question_id=short.id, kc_ids=[str(kc.id)])
            session.add(error)
            await session.flush()
            error_id = str(error.id)
    return uid, headers, str(quiz.id), answers, error_id


async def counts(uid):
    async with tenant_session(uid) as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in [
                AssessmentSubmission,
                Attempt,
                ErrorItem,
                LearningEvent,
                MasteryLog,
                FsrsReviewEvent,
            ]
        }


async def test_quiz_mixed_pending_retry_and_replay_write_once(client):
    uid, headers, quiz, answers, _ = await seed(client, mixed=True)
    grader = Grader()
    client._transport.app.state.gateway = grader
    path = f"/api/quiz/{quiz}/attempts"
    headers = {**headers, "Idempotency-Key": "same-submission"}
    first, second = await asyncio.gather(
        *[client.post(path, headers=headers, json={"answers": answers}) for _ in range(2)]
    )
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    result = first.json()
    assert result == second.json()
    assert grader.calls == 1
    assert result["pending_count"] == 1 and result["graded_total"] == 1
    assert result["correct"] == 1 and result["plan_task_completion"] is None
    pending = result["results"][0]
    assert pending["correct"] is None and pending["answer"] == pending["explanation"] == ""
    before = await counts(uid)
    assert before["attempts"] == 2 and before["learning_events"] == before["mastery_log"] == 1
    assert before["error_items"] == 0
    recovered = (await client.get(f"/api/quiz/{quiz}/pending-submission", headers=headers)).json()
    assert recovered["submission"]["request"]["answers"] == answers
    grader.raw = '{"score": 0.2, "reason": "缺少要点"}'
    retry_path = f"/api/quiz/{quiz}/submissions/{result['submission_id']}/retry-grading"
    retries = await asyncio.gather(
        *[client.post(retry_path, headers=headers, json={"expected_revision": 1}) for _ in range(2)]
    )
    assert all(response.status_code == 200 for response in retries)
    final = retries[0].json()
    assert final == retries[1].json()
    assert grader.calls == 2
    assert final["grading_status"] == "scored" and final["graded_total"] == 2
    assert final["results"][0]["attempt_id"] == pending["attempt_id"]
    after = await counts(uid)
    assert after["assessment_submissions"] == 1 and after["attempts"] == 2
    assert after["learning_events"] == after["mastery_log"] == 2 and after["error_items"] == 1
    replay = await client.post(path, headers=headers, json={"answers": answers})
    assert replay.json() == final and await counts(uid) == after
    conflict = await client.post(
        path, headers=headers, json={"answers": [{**answers[0], "response": "改答案"}]}
    )
    assert conflict.status_code == 409
    # A deliberate new submission remains a legal attempt.
    fresh = await client.post(
        path, headers={**headers, "Idempotency-Key": "new-round"}, json={"answers": answers}
    )
    assert fresh.status_code == 200 and (await counts(uid))["attempts"] == 4


@pytest.mark.parametrize(
    "raw", ["{}", '{"score": true}', '{"score": "bad"}', RuntimeError("unavailable")]
)
async def test_failed_quiz_grading_has_no_evidence_and_bounded_retries(client, raw):
    uid, headers, quiz, answers, _ = await seed(client)
    grader = Grader(raw)
    client._transport.app.state.gateway = grader
    response = await client.post(
        f"/api/quiz/{quiz}/attempts", headers=headers, json={"answers": answers}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["grading_status"] == "pending" and result["graded_total"] == 0
    path = f"/api/quiz/{quiz}/submissions/{result['submission_id']}/retry-grading"
    for revision in [1, 2]:
        response = await client.post(path, headers=headers, json={"expected_revision": revision})
        assert response.status_code == 200
    assert response.json()["retry_available"] is False
    limit = await client.post(path, headers=headers, json={"expected_revision": 3})
    assert limit.status_code == 409 and grader.calls == 3
    snapshot = await counts(uid)
    assert snapshot["attempts"] == snapshot["assessment_submissions"] == 1
    assert all(
        snapshot[name] == 0
        for name in ["error_items", "mastery_log", "learning_events", "fsrs_review_events"]
    )
    async with tenant_session(uid) as session:
        attempt = await session.scalar(select(Attempt))
        assert attempt.response == "学生原始答案" and attempt.is_correct is None


async def test_review_failed_grading_preserves_card_then_retries_once(client):
    uid, headers, _, _, error = await seed(client, review=True)
    grader = Grader()
    client._transport.app.state.gateway = grader
    headers = {**headers, "Idempotency-Key": "review-key"}
    path = f"/api/reviews/{error}/answer"
    result = (await client.post(path, headers=headers, json={"response": "保存答案"})).json()
    assert result["correct"] is None and result["grading_status"] == "pending"
    before = await counts(uid)
    assert before["fsrs_review_events"] == before["mastery_log"] == before["learning_events"] == 0
    async with tenant_session(uid) as session:
        item = await session.get(ErrorItem, uuid.UUID(error))
        assert item.state == "new" and not item.fsrs_card_json
    recovered = await client.get(f"/api/reviews/{error}/pending-submission", headers=headers)
    assert recovered.json()["submission"]["request"]["response"] == "保存答案"
    grader.raw = '{"score": 0.9}'
    retry_path = f"/api/reviews/{error}/submissions/{result['submission_id']}/retry-grading"
    response = await client.post(retry_path, headers=headers, json={"expected_revision": 1})
    assert response.status_code == 200 and response.json()["correct"] is True
    final = response.json()
    replay = (await client.post(path, headers=headers, json={"response": "保存答案"})).json()
    assert replay["idempotent"] is True and replay["correct"] == final["correct"]
    replay = (await client.post(retry_path, headers=headers, json={"expected_revision": 1})).json()
    assert replay["idempotent"] is True and replay["grading_revision"] == final["grading_revision"]
    assert grader.calls == 2
    after = await counts(uid)
    assert after["fsrs_review_events"] == after["mastery_log"] == after["learning_events"] == 1


async def test_submission_recovery_and_retry_are_owned_by_learner(client):
    _, headers, quiz, answers, _ = await seed(client)
    client._transport.app.state.gateway = Grader()
    result = (
        await client.post(f"/api/quiz/{quiz}/attempts", headers=headers, json={"answers": answers})
    ).json()
    _, other_headers, _, _, _ = await seed(client)
    response = await client.get(f"/api/quiz/{quiz}/pending-submission", headers=other_headers)
    assert response.json()["submission"] is None
    response = await client.post(
        f"/api/quiz/{quiz}/submissions/{result['submission_id']}/retry-grading",
        headers=other_headers,
        json={"expected_revision": 1},
    )
    assert response.status_code == 404


async def test_evidence_failure_rolls_back_submission_and_allows_safe_retry(client, monkeypatch):
    from eduagents_api import services

    uid, headers, quiz, answers, _ = await seed(client)
    client._transport.app.state.gateway = Grader('{"score": 0.2}')
    headers = {**headers, "Idempotency-Key": "rollback-retry"}
    original = services.write_learning_event

    async def fail_event(*args, **kwargs):
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(services, "write_learning_event", fail_event)
    response = await client.post(
        f"/api/quiz/{quiz}/attempts", headers=headers, json={"answers": answers}
    )
    assert response.status_code == 500
    assert all(value == 0 for value in (await counts(uid)).values())
    monkeypatch.setattr(services, "write_learning_event", original)
    response = await client.post(
        f"/api/quiz/{quiz}/attempts", headers=headers, json={"answers": answers}
    )
    assert response.status_code == 200
    snapshot = await counts(uid)
    assert snapshot["attempts"] == snapshot["mastery_log"] == snapshot["learning_events"] == 1


async def test_pending_grading_does_not_complete_attributed_plan(client):
    from eduagents_api.models import Plan

    uid, headers, quiz_id, answers, _ = await seed(client)
    task_id = str(uuid.uuid4())
    async with tenant_session(uid) as session:
        quiz = await session.get(Quiz, uuid.UUID(quiz_id))
        plan = Plan(
            tenant_id=uid,
            user_id=uid,
            kb_id=quiz.kb_id,
            goal="测试计划",
            plan_mode="course_bound",
            grounding_status="kb_grounded",
            budget_hours=1,
            structure_json={
                "stages": [
                    {
                        "days": [
                            {
                                "day": 1,
                                "tasks": [
                                    {
                                        "task_id": task_id,
                                        "action_type": "practice",
                                        "status": "pending",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            },
        )
        session.add(plan)
        await session.flush()
        plan_id = plan.id
        quiz.blueprint_json = {
            "_plan_mode": "course_bound",
            "_evidence_policy": "mastery_eligible",
            "_plan_task_attribution": {"plan_id": str(plan.id), "task_id": task_id},
        }
    grader = Grader()
    client._transport.app.state.gateway = grader
    response = await client.post(
        f"/api/quiz/{quiz_id}/attempts", headers=headers, json={"answers": answers}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["plan_task_completion"] is None and result["plan_adjustment"] is None
    async with tenant_session(uid) as session:
        plan = await session.get(Plan, plan_id)
        assert plan.status == "active" and plan.version == 1
    grader.raw = '{"score": 0.9}'
    response = await client.post(
        f"/api/quiz/{quiz_id}/submissions/{result['submission_id']}/retry-grading",
        headers=headers,
        json={"expected_revision": 1},
    )
    assert response.status_code == 200
    assert response.json()["plan_task_completion"]["completed"] is True
    async with tenant_session(uid) as session:
        plan = await session.get(Plan, plan_id)
        assert plan.status == "completed" and plan.version == 2


async def test_saved_pending_submission_is_exported_and_deleted_with_account(client):
    import io
    import json
    import zipfile

    from eduagents_core.config import get_settings
    from eduagents_core.storage import create_storage
    from eduagents_worker.export import export_user_data

    uid, headers, quiz, answers, _ = await seed(client)
    client._transport.app.state.gateway = Grader()
    response = await client.post(
        f"/api/quiz/{quiz}/attempts", headers=headers, json={"answers": answers}
    )
    assert response.status_code == 200
    path = await export_user_data(
        {"settings": get_settings(), "redis": client._transport.app.state.redis}, str(uid)
    )
    storage = create_storage(get_settings())
    archive = zipfile.ZipFile(io.BytesIO(await storage.load(path)))
    rows = json.loads(archive.read("tables/assessment_submissions.json"))
    assert len(rows) == 1 and rows[0]["request_json"]["answers"] == answers
    response = await client.request(
        "DELETE", "/api/account", headers=headers, json={"password": "StrongPass123!"}
    )
    assert response.status_code == 204, response.text
    assert all(value == 0 for value in (await counts(uid)).values())
