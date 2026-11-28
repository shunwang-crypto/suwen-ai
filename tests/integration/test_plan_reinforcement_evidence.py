"""Exercise low-score feedback through the quiz, proposal and confirmation APIs."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from eduagents_api import grading
from eduagents_api.db import tenant_session
from eduagents_api.models import (
    Attempt,
    KnowledgeBase,
    KnowledgeComponent,
    MasteryLog,
    MasteryRecord,
    Plan,
    PlanAdjustment,
    Question,
    Quiz,
)

from .conftest import requires_services
from .test_plan_errors import _register_login, _structure, _uid

pytestmark = requires_services


async def _seed_reinforcement(client, response_topics, history_topics, *, short_topics=()):
    email = f"reinforcement-{uuid.uuid4().hex}@test.eduagents.cn"
    headers = await _register_login(client, email)
    uid = await _uid(email)
    async with tenant_session(uid) as session:
        kb = KnowledgeBase(tenant_id=uid, name="深度学习", subject="计算机")
        session.add(kb)
        await session.flush()
        topics = {name: KnowledgeComponent(tenant_id=uid, kb_id=kb.id, canonical_name=name)
                  for name in ("链式法则", "矩阵运算")}
        session.add_all(topics.values())
        plan = Plan(tenant_id=uid, user_id=uid, kb_id=kb.id, goal="掌握反向传播",
                    plan_mode="course_bound", grounding_status="unreferenced", budget_hours=2,
                    structure_json=_structure())
        quiz = Quiz(tenant_id=uid, user_id=uid, kb_id=kb.id, title="课程随测", status="ready")
        session.add_all([plan, quiz])
        await session.flush()
        old_time = datetime.now(UTC) - timedelta(days=1)
        for name in history_topics:
            topic = topics[name]
            session.add(MasteryRecord(tenant_id=uid, user_id=uid, kc_id=topic.id,
                                      p_mastery=0.02, confidence=0.4))
            for _ in range(3):
                session.add(MasteryLog(tenant_id=uid, user_id=uid, kc_id=topic.id, old_p=0.02,
                                       new_p=0.02, evidence_type="quiz_attempt",
                                       evidence_id=uuid.uuid4(), at=old_time))
        responses = []
        for name, response in response_topics:
            q = Question(
                tenant_id=uid,
                quiz_id=quiz.id,
                type="short" if name in short_topics else "single",
                stem=f"判断 {name}",
                options_json=["A", "B"],
                answer="A",
                kc_ids=[str(topics[name].id)] if name else [],
            )
            session.add(q)
            await session.flush()
            responses.append({"question_id": str(q.id), "response": response})
        base = f"/api/plans/{plan.id}"
        submit = f"/api/quiz/{quiz.id}/attempts"
    return uid, headers, base, submit, responses


@pytest.mark.parametrize(
    ("scenario", "response_topics", "history_topics", "expected"),
    [
        ("wrong", [("链式法则", "B")], ["链式法则"], {"链式法则"}),
        ("unrelated", [("链式法则", "B")], ["链式法则", "矩阵运算"], {"链式法则"}),
        ("correct_in_same_quiz", [("链式法则", "B"), ("矩阵运算", "A")],
         ["链式法则", "矩阵运算"], {"链式法则"}),
        ("unmapped", [(None, "B")], ["矩阵运算"], set()),
        ("insufficient", [("链式法则", "B")], ["矩阵运算"], set()),
        ("regraded", [("链式法则", "B")], ["链式法则"], {"链式法则"}),
    ],
    ids=["wrong", "unrelated", "correct-in-same-quiz", "unmapped", "insufficient", "regraded"],
)
async def test_quiz_reinforcement(client, scenario, response_topics, history_topics, expected):
    uid, headers, base, submit, responses = await _seed_reinforcement(
        client, response_topics, history_topics
    )
    posted = await client.post(submit, headers={**headers, "Idempotency-Key": "quiz-feedback"},
                               json={"answers": responses})
    assert posted.status_code == 200, posted.text
    result = posted.json()
    proposal = result["plan_adjustment"]
    actual = {name for task in proposal["inserted"] for name in task["kc_names"]}
    assert actual == expected
    assert all(task["evidence"]["id"] in {
        row["attempt_id"] for row in result["results"] if row["correct"] is False
    } for task in proposal["inserted"])

    if not expected:
        assert proposal["status"] == "none" and proposal["adjustment_id"] is None
        return

    assert proposal["status"] == "proposed"
    before = (await client.get(base, headers=headers)).json()
    confirm = f"{base}/adjustments/{proposal['adjustment_id']}/confirm"
    if scenario == "unrelated":
        manual = await client.post(f"{base}/adjust", headers=headers)
        assert manual.status_code == 200, manual.text
        assert {name for task in manual.json()["inserted"] for name in task["kc_names"]} == {
            "链式法则", "矩阵运算",
        }
    if scenario == "regraded":
        # A corrected answer no longer supports this proposal even when old history is still weak.
        async with tenant_session(uid) as session:
            attempt = await session.get(Attempt, uuid.UUID(result["results"][0]["attempt_id"]))
            attempt.is_correct = True
        history = (await client.get(f"{base}/adjustments", headers=headers)).json()
        assert history[0]["status"] == "stale"
        assert (await client.post(confirm, headers=headers)).status_code == 409
        assert (await client.get(base, headers=headers)).json()["version"] == before["version"]
    else:
        replies = await asyncio.gather(*(client.post(confirm, headers=headers) for _ in range(2)))
        assert all(r.status_code == 200 and r.json()["status"] == "applied" for r in replies)
        after = (await client.get(base, headers=headers)).json()
        assert after["version"] == before["version"] + 1
        assert after["progress"]["total"] - before["progress"]["total"] == len(proposal["inserted"])


@pytest.mark.parametrize("regraded", [False, True], ids=["still-wrong", "corrected"])
async def test_reinforcement_on_grading_retry(client, monkeypatch, regraded):
    short_grade = None

    async def grade_short(*args, **kwargs):
        return short_grade

    monkeypatch.setattr(grading, "grade_short", grade_short)
    uid, headers, base, submit, responses = await _seed_reinforcement(
        client,
        [("链式法则", "B"), ("矩阵运算", "B")],
        ["链式法则", "矩阵运算"],
        short_topics=("矩阵运算",),
    )
    posted = await client.post(submit, headers=headers, json={"answers": responses})
    assert posted.status_code == 200, posted.text
    first = posted.json()
    first_attempt = first["results"][0]["attempt_id"]
    assert [row["correct"] for row in first["results"]] == [False, None]
    assert first["plan_adjustment"] is None

    if regraded:
        async with tenant_session(uid) as session:
            attempt = await session.get(Attempt, uuid.UUID(first_attempt))
            attempt.is_correct = True
    short_grade = (0.1, "解答遗漏主要步骤")
    quiz_url = submit.removesuffix("/attempts")
    retry_url = f"{quiz_url}/submissions/{first['submission_id']}/retry-grading"
    retried = await client.post(retry_url, headers=headers, json={"expected_revision": 1})
    assert retried.status_code == 200, retried.text
    result = retried.json()
    proposal = result["plan_adjustment"]
    assert proposal["status"] == "proposed"
    assert result["results"][0]["attempt_id"] == first_attempt
    expected = {"矩阵运算": result["results"][1]["attempt_id"]}
    if not regraded:
        expected["链式法则"] = first_attempt
    assert {task["kc_names"][0]: task["evidence"]["id"] for task in proposal["inserted"]} == expected
    history = await client.get(f"{base}/adjustments", headers=headers)
    assert history.status_code == 200, history.text
    assert [(row["id"], row["status"]) for row in history.json()] == [
        (proposal["adjustment_id"], "proposed")
    ]


async def test_reinforcement_verifies_submission_for_old_proposals(client):
    uid, headers, base, submit, responses = await _seed_reinforcement(
        client, [("链式法则", "B")], ["链式法则"]
    )
    submissions = []
    for key in ("first", "second"):
        response = await client.post(
            submit, headers={**headers, "Idempotency-Key": key}, json={"answers": responses}
        )
        assert response.status_code == 200, response.text
        submissions.append(response.json())
    first, second = submissions
    first_id = first["plan_adjustment"]["adjustment_id"]
    second_id = second["plan_adjustment"]["adjustment_id"]
    async with tenant_session(uid) as session:
        row = await session.get(PlanAdjustment, uuid.UUID(first_id))
        # Both attempts are wrong for the same question, but this attempt belongs to the first submission.
        row.diff_json = {**row.diff_json, "submission_id": second["submission_id"]}
        legacy_diff = {"trigger": "low_score_attempt", "inserted": [{"title": "旧版补强"}]}
        legacy = PlanAdjustment(
            tenant_id=uid, plan_id=row.plan_id, reason="历史已应用补强", diff_json=legacy_diff
        )
        session.add(legacy)
        await session.flush()
        legacy_id = str(legacy.id)

    before = (await client.get(base, headers=headers)).json()
    history = await client.get(f"{base}/adjustments", headers=headers)
    assert history.status_code == 200, history.text
    assert {row["id"]: row["status"] for row in history.json()} == {
        first_id: "stale", second_id: "proposed", legacy_id: "applied",
    }
    invalid = await client.post(f"{base}/adjustments/{first_id}/confirm", headers=headers)
    assert invalid.status_code == 409
    legacy_replay = await client.post(f"{base}/adjustments/{legacy_id}/confirm", headers=headers)
    assert legacy_replay.status_code == 200, legacy_replay.text
    assert legacy_replay.json()["status"] == "applied"
    assert legacy_replay.json()["inserted"] == legacy_diff["inserted"]
    after = (await client.get(base, headers=headers)).json()
    assert after["version"] == before["version"]
    assert after["progress"] == before["progress"]
