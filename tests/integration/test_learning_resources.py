"""Structured resource, assessment and source-isolation integration coverage."""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from .conftest import requires_services

pytestmark = requires_services
PASSWORD = "pass-12345"


async def _register(client, prefix: str):
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@test.eduagents.cn"
    response = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _register_with_id(client, prefix: str):
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@test.eduagents.cn"
    response = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return user_id, {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_image_extraction_bypasses_document_parser_and_stays_conservative(monkeypatch):
    from eduagents_core.config import get_settings
    from eduagents_worker import resources as resource_worker

    def fail_parser(_suffix):
        raise AssertionError("image input must not enter a document parser")

    monkeypatch.setattr(resource_worker, "get_parser", fail_parser)
    text, pages, page_count = await resource_worker._parse_source("scan.png", b"not-a-full-png")
    assert text == "" and pages == {1: ""} and page_count == 1
    draft = await resource_worker._recognize(
        get_settings(),
        None,
        "scan.png",
        b"image bytes",
        text,
        pages,
        10,
        "tenant-id",
        "owner-id",
    )
    assert len(draft) == 1
    assert draft[0]["detected_reference_answer"] == ""
    assert draft[0]["detected_student_answer"] == ""


async def test_resource_worker_handles_unlimited_blocks_and_jpeg_mime(monkeypatch):
    from eduagents_core.config import get_settings
    from eduagents_worker import resources as resource_worker

    text = "1. 第一题\n第一题内容\n2. 第二题\n第二题内容\n3. 第三题\n第三题内容"
    blocks = resource_worker._numbered_blocks(text, 0)
    assert len(blocks) == 3
    limited = resource_worker._numbered_blocks(text, 2)
    assert len(limited) == 2
    assert "第二题内容" in limited[1]["stem"]

    calls = []

    class CaptureGateway:
        async def chat(self, request):
            calls.append(request)
            return SimpleNamespace(text="[]")

    settings = get_settings().model_copy(
        update={"gateway_mode": "live", "vision_model": "test-vision-model"}
    )
    await resource_worker._recognize(
        settings,
        CaptureGateway(),
        "scan.jpg",
        b"jpeg bytes",
        "",
        {1: ""},
        10,
        "tenant-id",
        "owner-id",
    )
    image_url = calls[0].messages[0].content[1]["image_url"]["url"]
    assert image_url.startswith("data:image/jpeg;base64,")


async def test_learning_resource_quotas_are_enforced(client):
    from eduagents_api.deps import get_app_settings
    from eduagents_core.config import get_settings

    constrained = get_settings().model_copy(
        update={
            "max_learning_resources_per_user": 10,
            "max_pending_learning_resources": 1,
            "max_learning_resource_questions": 1,
            "max_learning_resource_mb": 5,
            "max_storage_mb_per_user": 1,
        }
    )
    app = client._transport.app
    app.dependency_overrides[get_app_settings] = lambda: constrained
    headers = await _register(client, "resource-quota")

    first = await client.post(
        "/api/resources", headers=headers, json={"title": "限额资料", "resource_type": "worksheet"}
    )
    assert first.status_code == 201, first.text
    resource_id = first.json()["id"]

    constrained.max_learning_resources_per_user = 1
    too_many_resources = await client.post(
        "/api/resources", headers=headers, json={"title": "第二份", "resource_type": "worksheet"}
    )
    assert too_many_resources.status_code == 400
    assert too_many_resources.json()["code"] == "RESOURCE_QUOTA_EXCEEDED"

    constrained.max_learning_resources_per_user = 10
    too_many_pending = await client.post(
        "/api/resources", headers=headers, json={"title": "仍在处理", "resource_type": "worksheet"}
    )
    assert too_many_pending.status_code == 429
    assert too_many_pending.json()["code"] == "TOO_MANY_PENDING_RESOURCES"

    question = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "限额题"},
    )
    assert question.status_code == 201, question.text
    too_many_questions = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "超出限额"},
    )
    assert too_many_questions.status_code == 400
    assert too_many_questions.json()["code"] == "RESOURCE_QUESTION_QUOTA_EXCEEDED"

    constrained.max_pending_learning_resources = 10
    too_large_for_storage = await client.post(
        "/api/resources",
        headers=headers,
        files={"file": ("over-quota.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
    )
    assert too_large_for_storage.status_code == 400
    assert too_large_for_storage.json()["code"] == "STORAGE_QUOTA_EXCEEDED"


async def test_worker_uses_resource_owner_for_kc_resolution(client, monkeypatch):
    owner_headers = await _register(client, "resource-owner-kc")
    await _register(client, "resource-owner-kc-other")
    create = await client.post(
        "/api/resources",
        headers=owner_headers,
        files={"file": ("q.txt", "1. 题目\n".encode(), "text/plain")},
    )
    assert create.status_code == 201, create.text
    resource_id = create.json()["id"]
    owner_id = uuid.UUID(create.json()["owner_id"])

    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import LearningResource, User
    from eduagents_core.config import get_settings
    from eduagents_worker import resources as resource_worker
    from sqlalchemy import select

    async with plain_session() as session:
        other = await session.scalar(
            select(User).where(User.email.like("resource-owner-kc-other-%@test.eduagents.cn"))
        )
        assert other is not None
        other_id = other.id
    async with tenant_session(owner_id) as session:
        resource = await session.get(LearningResource, uuid.UUID(resource_id))
        resource.owner_id = other_id

    seen: list[uuid.UUID] = []

    async def fake_resolve(_session, _kb_id, _names, *, user_id=None):
        seen.append(user_id)
        return []

    monkeypatch.setattr(resource_worker, "_resolve_kc_ids", fake_resolve)
    result = await resource_worker.extract_learning_resource(
        {"settings": get_settings()}, str(owner_id), resource_id
    )
    assert result == "reviewing"
    assert seen == [other_id]


async def test_resource_kb_switch_rebuilds_question_kc_ids_and_clears_on_unlink(client):
    owner_id, headers = await _register_with_id(client, "resource-kb-remap")
    other_headers = await _register(client, "resource-kb-remap-other")
    kb_a = await client.post("/api/kb", headers=headers, json={"name": "知识库 A", "subject": "数学"})
    kb_b = await client.post("/api/kb", headers=headers, json={"name": "知识库 B", "subject": "数学"})
    assert kb_a.status_code == 201 and kb_b.status_code == 201
    kb_a_id, kb_b_id = kb_a.json()["id"], kb_b.json()["id"]

    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeBase, KnowledgeComponent

    async with tenant_session(uuid.UUID(owner_id)) as session:
        kb_a_row = await session.get(KnowledgeBase, uuid.UUID(kb_a_id))
        kb_b_row = await session.get(KnowledgeBase, uuid.UUID(kb_b_id))
        kc_a = KnowledgeComponent(
            tenant_id=uuid.UUID(owner_id),
            kb_id=kb_a_row.id,
            canonical_name="函数极值",
            aliases=[],
        )
        kc_b = KnowledgeComponent(
            tenant_id=uuid.UUID(owner_id),
            kb_id=kb_b_row.id,
            canonical_name="函数极值",
            aliases=[],
        )
        session.add_all([kc_a, kc_b])
        await session.flush()
        kc_a_id, kc_b_id = str(kc_a.id), str(kc_b.id)

    resource = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "知识点重映射", "resource_type": "worksheet"},
    )
    assert resource.status_code == 201, resource.text
    resource_id = resource.json()["id"]
    question = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "求函数最大值", "answer": "1", "kc_names": ["函数极值"]},
    )
    assert question.status_code == 201, question.text
    assert question.json()["kc_ids"] == []

    linked_a = await client.patch(
        f"/api/resources/{resource_id}", headers=headers, json={"linked_kb_id": kb_a_id}
    )
    assert linked_a.status_code == 200, linked_a.text
    questions = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert questions.status_code == 200
    assert questions.json()[0]["kc_ids"] == [kc_a_id]

    linked_b = await client.patch(
        f"/api/resources/{resource_id}", headers=headers, json={"linked_kb_id": kb_b_id}
    )
    assert linked_b.status_code == 200, linked_b.text
    questions = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert questions.json()[0]["kc_ids"] == [kc_b_id]
    assert kc_a_id not in questions.json()[0]["kc_ids"]

    unlinked = await client.patch(
        f"/api/resources/{resource_id}", headers=headers, json={"linked_kb_id": None}
    )
    assert unlinked.status_code == 200, unlinked.text
    questions = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert questions.json()[0]["kc_ids"] == []

    forbidden = await client.patch(
        f"/api/resources/{resource_id}", headers=other_headers, json={"linked_kb_id": kb_a_id}
    )
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == "NOT_FOUND"


async def test_resource_kb_switch_is_locked_after_error_or_assessment_evidence(client):
    headers = await _register(client, "resource-kb-evidence")
    first_kb = await client.post("/api/kb", headers=headers, json={"name": "锁定库 A", "subject": "数学"})
    second_kb = await client.post("/api/kb", headers=headers, json={"name": "锁定库 B", "subject": "数学"})
    assert first_kb.status_code == 201 and second_kb.status_code == 201
    kb_a_id, kb_b_id = first_kb.json()["id"], second_kb.json()["id"]

    resource = await client.post(
        "/api/resources", headers=headers, json={"title": "证据锁定", "resource_type": "worksheet"}
    )
    assert resource.status_code == 201
    resource_id = resource.json()["id"]
    question = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "错题证据", "answer": "正确答案"},
    )
    assert question.status_code == 201
    question_id = question.json()["id"]
    assert (await client.post(f"/api/resource-questions/{question_id}/verify", headers=headers, json={})).status_code == 200
    assert (await client.patch(f"/api/resources/{resource_id}", headers=headers, json={"linked_kb_id": kb_a_id})).status_code == 200
    confirmed = await client.post(
        f"/api/resources/{resource_id}/confirm",
        headers=headers,
        json={"resource_question_ids": [question_id]},
    )
    assert confirmed.status_code == 200, confirmed.text
    locked_by_error = await client.patch(
        f"/api/resources/{resource_id}", headers=headers, json={"linked_kb_id": kb_b_id}
    )
    assert locked_by_error.status_code == 409
    assert locked_by_error.json()["code"] == "RESOURCE_KB_LOCKED_AFTER_EVIDENCE"

    resource_2 = await client.post(
        "/api/resources", headers=headers, json={"title": "测评证据锁定", "resource_type": "worksheet"}
    )
    assert resource_2.status_code == 201
    resource_2_id = resource_2.json()["id"]
    question_2 = await client.post(
        f"/api/resources/{resource_2_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "测评题", "answer": "答案"},
    )
    assert question_2.status_code == 201
    question_2_id = question_2.json()["id"]
    assert (await client.post(f"/api/resource-questions/{question_2_id}/verify", headers=headers, json={})).status_code == 200
    assert (await client.patch(f"/api/resources/{resource_2_id}", headers=headers, json={"linked_kb_id": kb_a_id})).status_code == 200
    attempt = await client.post(
        "/api/assessment-attempts", headers=headers, json={"resource_id": resource_2_id}
    )
    assert attempt.status_code == 201
    answer = await client.put(
        f"/api/assessment-attempts/{attempt.json()['id']}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_2_id, "response": "答案"}]},
    )
    assert answer.status_code == 200
    locked_by_assessment = await client.patch(
        f"/api/resources/{resource_2_id}", headers=headers, json={"linked_kb_id": kb_b_id}
    )
    assert locked_by_assessment.status_code == 409
    assert locked_by_assessment.json()["code"] == "RESOURCE_KB_LOCKED_AFTER_EVIDENCE"


async def test_resource_extraction_does_not_requeue_finished_states(client):
    headers = await _register(client, "resource-extract-idempotent")
    created = await client.post(
        "/api/resources",
        headers=headers,
        files={"file": ("extract.txt", "1. 题目".encode(), "text/plain")},
    )
    assert created.status_code == 201, created.text
    resource_id = uuid.UUID(created.json()["id"])
    owner_id = uuid.UUID(created.json()["owner_id"])

    from eduagents_api.db import tenant_session
    from eduagents_api.models import LearningResource

    app = client._transport.app
    original_arq = app.state.arq
    calls = 0

    async def enqueue(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    app.state.arq = SimpleNamespace(enqueue_job=enqueue)
    try:
        async with tenant_session(owner_id) as session:
            resource = await session.get(LearningResource, resource_id)
            resource.status = "reviewing"
        reviewing = await client.post(f"/api/resources/{resource_id}/extract", headers=headers)
        assert reviewing.status_code == 202
        assert reviewing.json()["status"] == "reviewing"
        assert calls == 0

        async with tenant_session(owner_id) as session:
            resource = await session.get(LearningResource, resource_id)
            resource.status = "ready"
        ready = await client.post(f"/api/resources/{resource_id}/extract", headers=headers)
        assert ready.status_code == 202
        assert ready.json()["status"] == "ready"
        assert calls == 0

        async with tenant_session(owner_id) as session:
            resource = await session.get(LearningResource, resource_id)
            resource.status = "failed"
        retry = await client.post(f"/api/resources/{resource_id}/extract", headers=headers)
        assert retry.status_code == 202
        assert retry.json()["status"] == "extracting"
        assert calls == 1
    finally:
        app.state.arq = original_arq


async def test_resource_extract_verify_select_error_and_assessment(client):
    headers = await _register(client, "resource-flow")
    response = await client.post(
        "/api/resources",
        headers=headers,
        files={
            "file": (
                "卷子.txt",
                "1. 1+1 等于多少？\n2. 2+2 等于多少？\n",
                "text/plain",
            ),
            "title": (None, "两道题练习"),
            "resource_type": (None, "worksheet"),
        },
    )
    assert response.status_code == 201, response.text
    resource_body = response.json()
    resource_id = resource_body["id"]
    owner_id = uuid.UUID(response.json()["owner_id"])
    from eduagents_core.config import get_settings
    from eduagents_worker.resources import extract_learning_resource

    await extract_learning_resource(
        {"settings": get_settings()}, str(owner_id), resource_id
    )
    response = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert response.status_code == 200, response.text
    questions = response.json()
    assert len(questions) == 2
    for question in questions:
        verify = await client.post(
            f"/api/resource-questions/{question['id']}/verify",
            headers=headers,
            json={"answer": "2" if question["question_no"] == "1" else "4"},
        )
        assert verify.status_code == 200, verify.text
    rollback = await client.patch(
        f"/api/resource-questions/{questions[0]['id']}",
        headers=headers,
        json={"verification_status": "pending"},
    )
    assert rollback.status_code == 409
    assert rollback.json()["code"] == "VERIFICATION_ROLLBACK_FORBIDDEN"
    editable_after_verification = await client.patch(
        f"/api/resource-questions/{questions[0]['id']}",
        headers=headers,
        json={"stem": "1+1 等于多少（已校对）？"},
    )
    assert editable_after_verification.status_code == 200, editable_after_verification.text
    response = await client.post(
        f"/api/resources/{resource_id}/confirm",
        headers=headers,
        json={"resource_question_ids": [questions[0]["id"]]},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["error_item_ids"]) == 1
    error_id = response.json()["error_item_ids"][0]
    assert (await client.get("/api/quiz", headers=headers)).json() == []

    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        AssessmentAnswer,
        Attempt,
        ErrorItem,
        LearningEvent,
        LearningResource,
        Quiz,
    )
    from sqlalchemy import select

    async with tenant_session(owner_id) as session:
        error = await session.get(ErrorItem, uuid.UUID(error_id))
        assert error is not None and error.assessment_answer_id is None
        assert await session.scalar(select(Attempt).where(Attempt.question_id == error.question_id)) is None
        assert await session.scalar(select(Quiz).where(Quiz.user_id == error.user_id)) is None
        assert await session.get(LearningResource, uuid.UUID(resource_id)) is not None

    # 空选择绝不能把整份资料的所有 verified 题默认加入错题本；重复选择
    # 同一题则返回同一个 ErrorItem。
    empty_confirm = await client.post(
        f"/api/resources/{resource_id}/confirm", headers=headers, json={}
    )
    assert empty_confirm.status_code == 200
    assert empty_confirm.json()["error_item_ids"] == []
    retry_confirm = await client.post(
        f"/api/resources/{resource_id}/confirm",
        headers=headers,
        json={"error_question_ids": [questions[0]["id"]]},
    )
    assert retry_confirm.status_code == 200
    assert retry_confirm.json()["error_item_ids"] == [error_id]

    attempt = await client.post(
        "/api/assessment-attempts",
        headers=headers,
        json={"resource_id": resource_id, "attempt_type": "practice", "title": "第一次"},
    )
    assert attempt.status_code == 201, attempt.text
    attempt_id = attempt.json()["id"]
    answers = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": questions[1]["id"], "response": "3"}]},
    )
    assert answers.status_code == 200, answers.text
    assert (await client.get(f"/api/assessment-attempts/{attempt_id}", headers=headers)).json()["answers"][0]["question"].get("answer") is None
    assert (await client.post(f"/api/assessment-attempts/{attempt_id}/submit", headers=headers)).status_code == 200
    partial = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": False,
            "answers": [
                {
                    "resource_question_id": questions[1]["id"],
                    "response": "3",
                    "is_correct": False,
                    "score": 0,
                    "max_score": 1,
                }
            ],
        },
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["status"] == "submitted"
    pending_report = await client.get("/api/reports/learning", headers=headers)
    assert pending_report.status_code == 200
    assert pending_report.json()["metrics"]["attempt_count"] == 0
    assert all(point["attempts"] == 0 for point in pending_report.json()["trend"])
    async with tenant_session(owner_id) as session:
        assert not (
            await session.scalars(
                select(LearningEvent).where(LearningEvent.source_type == "assessment")
            )
        ).all()
    bypass = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": False,
            "answers": [
                {
                    "resource_question_id": questions[1]["id"],
                    "response": "3",
                    "is_correct": False,
                    "score": 0,
                    "max_score": 1,
                    "grading_status": "verified",
                }
            ],
        },
    )
    assert bypass.status_code == 409
    assert bypass.json()["code"] == "ATTEMPT_VERIFICATION_REQUIRED"
    graded = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [
                {
                    "resource_question_id": questions[1]["id"],
                    "response": "3",
                    "is_correct": False,
                    "score": 0,
                    "max_score": 1,
                }
            ],
        },
    )
    assert graded.status_code == 200, graded.text
    assert graded.json()["status"] == "graded"
    async with tenant_session(owner_id) as session:
        answer_id = await session.scalar(
            select(AssessmentAnswer.id).where(
                AssessmentAnswer.attempt_id == uuid.UUID(attempt_id)
            )
        )
        assessment_events = (
            await session.scalars(
                select(LearningEvent).where(
                    LearningEvent.source_type == "assessment",
                    LearningEvent.source_id == answer_id,
                )
            )
        ).all()
    assert len(assessment_events) == 1
    assert assessment_events[0].question_id == uuid.UUID(questions[1]["question_id"])

    # graded/verified 是终态：完全相同的重试幂等，改作答或分数明确拒绝。
    retry = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [
                {
                    "resource_question_id": questions[1]["id"],
                    "response": "3",
                    "is_correct": False,
                    "score": 0,
                    "max_score": 1,
                }
            ],
        },
    )
    assert retry.status_code == 200 and retry.json()["mastery_changes"] == []
    changed = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [
                {
                    "resource_question_id": questions[1]["id"],
                    "response": "4",
                    "is_correct": True,
                    "score": 1,
                    "max_score": 1,
                }
            ],
        },
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "ASSESSMENT_ALREADY_GRADED"
    assessment_error = next(
        item
        for item in (await client.get("/api/errors", headers=headers)).json()["items"]
        if item["question"]["id"] != questions[0]["question_id"]
        and item["source_import_id"] is None
    )
    reviewed = await client.post(
        f"/api/reviews/{assessment_error['id']}/answer",
        headers=headers,
        json={"response": "3"},
    )
    assert reviewed.status_code == 200, reviewed.text
    locked_question = await client.patch(
        f"/api/resource-questions/{questions[1]['id']}",
        headers=headers,
        json={"answer": "5"},
    )
    assert locked_question.status_code == 409
    assert locked_question.json()["code"] == "ERROR_ITEM_LOCKED_AFTER_REVIEW"


async def test_resource_source_is_owner_only_and_safe_filename(client):
    owner = await _register(client, "resource-source-owner")
    other = await _register(client, "resource-source-other")
    response = await client.post(
        "/api/resources",
        headers=owner,
        files={"file": ("../原卷.txt", b"source bytes", "text/plain")},
    )
    assert response.status_code == 201, response.text
    resource_body = response.json()
    resource_id = resource_body["id"]
    assert (await client.get(f"/api/resources/{resource_id}/source", headers=other)).status_code == 404
    response = await client.get(f"/api/resources/{resource_id}/source", headers=owner)
    assert response.status_code == 200
    assert response.content == b"source bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "filename*=UTF-8''%E5%8E%9F%E5%8D%B7.txt" in response.headers["content-disposition"]

    from eduagents_core.config import get_settings
    from eduagents_worker.export import export_user_data

    queued = await client.post("/api/account/export", headers=owner)
    assert queued.status_code == 202
    await export_user_data({"settings": get_settings()}, resource_body["owner_id"])
    exported = await client.get("/api/account/export/download", headers=owner)
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        path = f"uploads/resources/{resource_id}/原卷.txt"
        assert path in archive.namelist()
        assert archive.read(path) == b"source bytes"
        assert all(".." not in name for name in archive.namelist())


async def test_resource_question_orphans_and_delete_retry(client, monkeypatch):
    headers = await _register(client, "resource-delete")
    response = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "手动题目资料", "resource_type": "worksheet"},
    )
    assert response.status_code == 201, response.text
    resource = response.json()
    resource_id = uuid.UUID(resource["id"])
    owner_id = uuid.UUID(resource["owner_id"])
    question_response = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "需要被回收的题"},
    )
    assert question_response.status_code == 201, question_response.text
    question_id = uuid.UUID(question_response.json()["question_id"])

    deleted = await client.delete(f"/api/resources/{resource_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    from eduagents_api.db import tenant_session
    from eduagents_api.models import LearningResource, Question, ResourceQuestion

    async with tenant_session(owner_id) as session:
        assert await session.get(LearningResource, resource_id) is None
        assert await session.get(ResourceQuestion, uuid.UUID(question_response.json()["id"])) is None
        assert await session.get(Question, question_id) is None

    # 对象删除失败时保留可重试的删除标记，不能提前删掉数据库行。
    response = await client.post(
        "/api/resources",
        headers=headers,
        files={"file": ("retry.txt", b"retry source", "text/plain")},
    )
    assert response.status_code == 201, response.text
    retry_id = uuid.UUID(response.json()["id"])
    from eduagents_core.storage import LocalStorage

    async def fail_delete(_self, _prefix):
        raise OSError("temporary storage outage")

    monkeypatch.setattr(LocalStorage, "delete_prefix", fail_delete)
    failed = await client.delete(f"/api/resources/{retry_id}", headers=headers)
    assert failed.status_code == 503
    state = await client.get(f"/api/resources/{retry_id}", headers=headers)
    assert state.status_code == 200
    assert state.json()["status"] == "failed"
    assert state.json()["metadata"]["delete_pending"] is True
    monkeypatch.undo()
    assert (await client.delete(f"/api/resources/{retry_id}", headers=headers)).status_code == 204


async def test_resource_delete_rejects_assessment_history(client):
    headers = await _register(client, "resource-assessment-delete")
    response = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "有测评记录", "resource_type": "worksheet"},
    )
    assert response.status_code == 201, response.text
    resource_id = uuid.UUID(response.json()["id"])
    owner_id = uuid.UUID(response.json()["owner_id"])
    question = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "保留历史"},
    )
    assert question.status_code == 201, question.text
    question_id = question.json()["id"]
    assert (
        await client.post(
            f"/api/resource-questions/{question_id}/verify", headers=headers
        )
    ).status_code == 200
    attempt = await client.post(
        "/api/assessment-attempts",
        headers=headers,
        json={"resource_id": str(resource_id), "attempt_type": "practice"},
    )
    assert attempt.status_code == 201, attempt.text
    blocked = await client.delete(f"/api/resources/{resource_id}", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "RESOURCE_HAS_ASSESSMENTS"

    from eduagents_api.db import tenant_session
    from eduagents_api.models import AssessmentAttempt, LearningResource

    async with tenant_session(owner_id) as session:
        await session.delete(await session.get(AssessmentAttempt, uuid.UUID(attempt.json()["id"])))
        await session.flush()
        assert await session.get(LearningResource, resource_id) is not None
    assert (await client.delete(f"/api/resources/{resource_id}", headers=headers)).status_code == 204


async def test_resource_cleanup_does_not_hide_resource_questions(client):
    headers = await _register(client, "resource-cleanup")
    response = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "清理测试", "resource_type": "worksheet"},
    )
    assert response.status_code == 201, response.text
    resource_id = uuid.UUID(response.json()["id"])
    owner_id = uuid.UUID(response.json()["owner_id"])
    from eduagents_api.db import tenant_session
    from eduagents_api.models import LearningResource, Question, ResourceQuestion
    from sqlalchemy import select

    old = datetime.now(UTC) - timedelta(days=2)
    async with tenant_session(owner_id) as session:
        resource = await session.get(LearningResource, resource_id)
        resource.created_at = old
        resource.status = "uploaded"
        question = Question(
            tenant_id=owner_id,
            quiz_id=None,
            type="short",
            stem="清理时必须保留的题",
            answer="",
            source_type="document",
        )
        session.add(question)
        await session.flush()
        session.add(
            ResourceQuestion(
                tenant_id=owner_id,
                resource_id=resource.id,
                question_id=question.id,
                verification_status="pending",
            )
        )
        empty = LearningResource(
            tenant_id=owner_id,
            owner_type="personal",
            owner_id=owner_id,
            created_by=owner_id,
            resource_type="worksheet",
            title="无题过期资料",
            created_at=old,
            status="uploaded",
        )
        session.add(empty)
        await session.flush()
        empty_id = empty.id

    from eduagents_core.config import get_settings
    from eduagents_worker.error_import_cleanup import reap_stale_error_imports

    cleaned = await reap_stale_error_imports({"settings": get_settings()})
    assert cleaned >= 1
    async with tenant_session(owner_id) as session:
        assert await session.get(LearningResource, empty_id) is None
        kept = await session.get(LearningResource, resource_id)
        assert kept is not None
        assert await session.scalar(
            select(ResourceQuestion.id).where(ResourceQuestion.resource_id == resource_id)
        ) is not None
        # 测试数据清理：有题资源先删 RQ/题目，再删资源。
        await session.delete(question)
        await session.delete(kept)
