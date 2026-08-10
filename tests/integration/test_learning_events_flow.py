"""B0 review transaction, replay and idempotency coverage."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = requires_services


async def _register_login(client, email: str) -> dict:
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _user_id(email: str) -> uuid.UUID:
    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    async with plain_session() as session:
        return await session.scalar(select(User.id).where(User.email == email))


async def test_review_persists_reviewlog_learning_event_and_replays_once(client):
    from eduagents_api import timectl
    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        ErrorItem,
        FsrsReviewEvent,
        KnowledgeBase,
        KnowledgeComponent,
        LearningEvent,
        MasteryLog,
        Question,
        Quiz,
    )
    from eduagents_core.mastery.scheduler import new_card_json

    app = client._transport.app
    from eduagents_api.routers import reviews as reviews_router

    if not any(getattr(route, "path", "") == "/api/reviews/today" for route in app.router.routes):
        app.include_router(reviews_router.router)
    await timectl.set_offset(app.state.redis, 0)

    email = f"events-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    headers = await _register_login(client, email)
    user_id = await _user_id(email)
    async with tenant_session(user_id) as session:
        kb = KnowledgeBase(tenant_id=user_id, name="events", subject="math")
        session.add(kb)
        await session.flush()
        kc = KnowledgeComponent(tenant_id=user_id, kb_id=kb.id, canonical_name="代数")
        session.add(kc)
        await session.flush()
        quiz = Quiz(tenant_id=user_id, user_id=user_id, title="events", status="ready")
        session.add(quiz)
        await session.flush()
        question = Question(
            tenant_id=user_id,
            quiz_id=quiz.id,
            type="single",
            stem="1+1?",
            options_json=["2", "3"],
            answer="A",
            explanation="基础题",
            kc_ids=[str(kc.id)],
            difficulty="easy",
        )
        session.add(question)
        await session.flush()
        item_id = uuid.uuid4()
        item = ErrorItem(
            id=item_id,
            tenant_id=user_id,
            user_id=user_id,
            question_id=question.id,
            kc_ids=[str(kc.id)],
            fsrs_card_json=new_card_json(
                await timectl.now(app.state.redis), business_card_id=item_id
            ),
        )
        session.add(item)

    idem = "review-event-retry-1"
    first = await client.post(
        f"/api/reviews/{item_id}/answer",
        headers={**headers, "Idempotency-Key": idem},
        json={"response": "A", "review_duration_ms": 321},
    )
    assert first.status_code == 200, first.text
    card_after_first = first.json()["next_due"]

    async with tenant_session(user_id) as session:
        fsrs_event = await session.scalar(
            select(FsrsReviewEvent).where(FsrsReviewEvent.idempotency_key == idem)
        )
        learning_event = await session.scalar(
            select(LearningEvent).where(
                LearningEvent.source_type == "review",
                LearningEvent.source_id == item_id,
                LearningEvent.idempotency_key == idem,
            )
        )
        assert fsrs_event is not None
        mastery_log = await session.scalar(
            select(MasteryLog).where(
                MasteryLog.user_id == user_id,
                MasteryLog.evidence_type == "review",
                MasteryLog.evidence_id == fsrs_event.id,
            )
        )
        assert learning_event is not None and mastery_log is not None
        assert fsrs_event.rating == 3 and fsrs_event.correct is True
        assert fsrs_event.review_duration_ms == 321
        assert fsrs_event.review_log_json["rating"] == 3
        assert fsrs_event.card_after_json["business_card_id"] == str(item_id)
        assert learning_event.question_id == question.id
        assert learning_event.item_key == str(item_id)
        assert learning_event.kc_ids_json == [str(kc.id)]
        assert learning_event.source_id == item_id
        card_snapshot = dict((await session.get(ErrorItem, item_id)).fsrs_card_json)

    retry = await client.post(
        f"/api/reviews/{item_id}/answer",
        headers={**headers, "Idempotency-Key": idem},
        json={"response": "A", "review_duration_ms": 321},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["idempotent"] is True
    assert retry.json()["next_due"] == card_after_first

    async with tenant_session(user_id) as session:
        assert await session.scalar(
            select(func.count()).select_from(FsrsReviewEvent).where(
                FsrsReviewEvent.idempotency_key == idem
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(LearningEvent).where(
                LearningEvent.source_type == "review",
                LearningEvent.source_id == item_id,
                LearningEvent.idempotency_key == idem,
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(MasteryLog).where(
                MasteryLog.user_id == user_id,
                MasteryLog.evidence_type == "review",
                MasteryLog.evidence_id == fsrs_event.id,
            )
        ) == 1
        assert (await session.get(ErrorItem, item_id)).fsrs_card_json == card_snapshot

    conflict = await client.post(
        f"/api/reviews/{item_id}/answer",
        headers={**headers, "Idempotency-Key": idem},
        json={"response": "B", "review_duration_ms": 321},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "REVIEW_IDEMPOTENCY_CONFLICT"

    oversized_header = await client.post(
        f"/api/reviews/{uuid.uuid4()}/answer",
        headers={**headers, "Idempotency-Key": "x" * 129},
        json={"response": "A"},
    )
    assert oversized_header.status_code == 422
