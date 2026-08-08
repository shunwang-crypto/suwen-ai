"""Observation-only confidence calibration and explain-back integration coverage."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = requires_services


async def _register_login(client, prefix: str):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert response.status_code == 201, response.text
    user_id = uuid.UUID(response.json()["id"])
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert response.status_code == 200, response.text
    return user_id, {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_prediction_is_atomic_user_scoped_and_does_not_change_mastery(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        Attempt,
        MasteryLog,
        MasteryRecord,
        MetacognitionPrediction,
        Question,
        Quiz,
    )

    user_id, headers = await _register_login(client, "meta-predict")
    async with tenant_session(user_id) as session:
        quiz = Quiz(tenant_id=user_id, user_id=user_id, title="校准卷", status="ready")
        session.add(quiz)
        await session.flush()
        question = Question(
            tenant_id=user_id,
            quiz_id=quiz.id,
            type="single",
            stem="1+1=?",
            options_json=["1", "2"],
            answer="B",
            kc_ids=[],
        )
        session.add(question)
        await session.flush()
        quiz_id, question_id = quiz.id, question.id

    response = await client.post(
        f"/api/quiz/{quiz_id}/attempts",
        headers=headers,
        json={
            "answers": [
                {
                    "question_id": str(question_id),
                    "response": "B",
                    "predicted_confidence": 5,
                },
                {
                    "question_id": str(question_id),
                    "response": "A",
                    "predicted_confidence": 1,
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1

    async with tenant_session(user_id) as session:
        assert await session.scalar(select(func.count()).select_from(Attempt)) == 1
        prediction = await session.scalar(select(MetacognitionPrediction))
        assert prediction is not None
        assert prediction.attempt_id is not None
        assert prediction.question_id == question_id
        assert prediction.predicted_confidence == 5
        assert await session.scalar(select(func.count()).select_from(MasteryRecord)) == 0
        assert await session.scalar(select(func.count()).select_from(MasteryLog)) == 0

    response = await client.get("/api/metacognition/summary?window=week", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "window": "week",
        "sample_n": 1,
        "brier_score": 0.01,
        "overconfidence_rate": 0.0,
    }

    other_user_id, other_headers = await _register_login(client, "meta-other")
    assert other_user_id != user_id
    response = await client.get("/api/metacognition/summary?window=all", headers=other_headers)
    assert response.status_code == 200
    assert response.json()["sample_n"] == 0


class _ExplainGateway:
    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        assert req.feature == "explain_back"
        assert len(req.messages) == 2
        return ChatResult(
            text=json.dumps(
                {
                    "understanding_score": 0.75,
                    "feedback": "你说明了 $x^2$ 的变化关系。",
                    "gap": "还需说明定义域。",
                    "retry_guidance": "用一个反例复查。",
                },
                ensure_ascii=False,
            ),
            model="fake",
            tier="cheap",
            prompt_tokens=10,
            completion_tokens=10,
            cost_cny=0.0,
            latency_ms=1,
            mode="stub",
        )


class _BrokenGateway:
    async def chat(self, req):
        raise RuntimeError("fake provider failure")


class _BudgetGateway:
    async def chat(self, req):
        from eduagents_core.gateway import BudgetExceeded

        raise BudgetExceeded(retry_after_seconds=60)


async def test_explain_back_retains_degraded_text_but_never_writes_learning_evidence(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import ErrorItem, ExplainBack, MasteryLog, MasteryRecord

    user_id, headers = await _register_login(client, "meta-explain")
    app = client._transport.app
    original_gateway = app.state.gateway
    try:
        app.state.gateway = _ExplainGateway()
        response = await client.post(
            "/api/metacognition/explain-back",
            headers=headers,
            json={"student_text": "我先看定义域，再比较两边。", "prompt_span_ref": "quiz:q1"},
        )
        assert response.status_code == 200, response.text
        # The structured rubric remains shadow-only until the offline human
        # agreement gate is satisfied; the legacy field is deliberately
        # present but its numeric value is not student-visible.
        assert response.json()["understanding_score"] is None
        assert response.json()["quality_shadow"] is True
        assert response.json()["degraded"] is False

        app.state.gateway = _BrokenGateway()
        response = await client.post(
            "/api/metacognition/explain-back",
            headers=headers,
            json={"student_text": "这段内容即使失败也要保留。"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["degraded"] is True

        async with tenant_session(user_id) as session:
            rows = (await session.scalars(select(ExplainBack).order_by(ExplainBack.created_at))).all()
            assert len(rows) == 2
            assert rows[1].student_text == "这段内容即使失败也要保留。"
            assert rows[1].degraded is True
            for model in (MasteryRecord, MasteryLog, ErrorItem):
                assert await session.scalar(select(func.count()).select_from(model)) == 0

        app.state.gateway = _BudgetGateway()
        response = await client.post(
            "/api/metacognition/explain-back",
            headers=headers,
            json={"student_text": "额度异常不能被伪装成分析降级。"},
        )
        assert response.status_code == 429, response.text
        assert response.json()["code"] == "BUDGET_EXCEEDED"
        async with tenant_session(user_id) as session:
            assert await session.scalar(select(func.count()).select_from(ExplainBack)) == 2
    finally:
        app.state.gateway = original_gateway


async def test_explain_back_kc_lookup_is_tenant_isolated(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeBase, KnowledgeComponent

    owner_id, _owner_headers = await _register_login(client, "meta-owner")
    _other_id, other_headers = await _register_login(client, "meta-outsider")
    async with tenant_session(owner_id) as session:
        kb = KnowledgeBase(tenant_id=owner_id, name="私有库", subject="math")
        session.add(kb)
        await session.flush()
        kc = KnowledgeComponent(tenant_id=owner_id, kb_id=kb.id, canonical_name="私有知识点")
        session.add(kc)
        await session.flush()
        kc_id = kc.id

    response = await client.post(
        "/api/metacognition/explain-back",
        headers=other_headers,
        json={"student_text": "尝试访问其他人的知识点。", "kc_id": str(kc_id)},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "KC_NOT_FOUND"
