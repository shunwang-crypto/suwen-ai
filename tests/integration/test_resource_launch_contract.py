"""Launch contracts for the question-resource -> assessment -> error path."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .conftest import requires_services

pytestmark = requires_services
PASSWORD = "pass-12345"
FRONTEND_UPLOAD_DIALOG = (
    Path(__file__).resolve().parents[2]
    / "apps/web/components/resources/resource-upload-dialog.tsx"
)
EXPECTED_UPLOAD_TYPES = [
    "exam_paper",
    "mock_paper",
    "workbook",
    "worksheet",
    "homework",
    "other",
]


async def _register(client, prefix: str):
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@test.eduagents.cn"
    response = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_frontend_resource_type_payloads_are_accepted_by_backend(client):
    source = FRONTEND_UPLOAD_DIALOG.read_text(encoding="utf-8")
    block = source.split("const RESOURCE_TYPES", 1)[1].split("];", 1)[0]
    submitted_types = re.findall(r'\["([a-z_]+)",', block)
    assert submitted_types == EXPECTED_UPLOAD_TYPES

    headers = await _register(client, "resource-type-contract")
    resource_ids: list[str] = []
    for resource_type in submitted_types:
        response = await client.post(
            "/api/resources",
            headers=headers,
            json={"title": f"契约测试-{resource_type}", "resource_type": resource_type},
        )
        assert response.status_code == 201, response.text
        resource_ids.append(response.json()["id"])

    for resource_id in resource_ids:
        assert (await client.delete(f"/api/resources/{resource_id}", headers=headers)).status_code == 204


async def test_reference_resources_cannot_enter_question_extraction(client):
    headers = await _register(client, "reference-guard")
    response = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "历史教材", "resource_type": "textbook"},
    )
    assert response.status_code == 201, response.text
    resource = response.json()
    assert resource["retrieval_policy"] == "reference"

    blocked = await client.post(f"/api/resources/{resource['id']}/extract", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "REFERENCE_RESOURCE_NOT_EXTRACTABLE"

    assert (await client.delete(f"/api/resources/{resource['id']}", headers=headers)).status_code == 204


async def test_linked_kb_is_scoped_and_can_be_attached_to_question_resource(client):
    owner = await _register(client, "resource-kb-owner")
    other = await _register(client, "resource-kb-other")
    kb = await client.post("/api/kb", headers=owner, json={"name": "数学教材", "subject": "数学"})
    assert kb.status_code == 201, kb.text
    kb_id = kb.json()["id"]

    resource = await client.post(
        "/api/resources",
        headers=owner,
        json={"title": "关联知识点的试卷", "resource_type": "exam_paper", "linked_kb_id": kb_id},
    )
    assert resource.status_code == 201, resource.text
    assert resource.json()["linked_kb_id"] == kb_id

    forbidden = await client.post(
        "/api/resources",
        headers=other,
        json={"title": "越权绑定", "resource_type": "worksheet", "linked_kb_id": kb_id},
    )
    assert forbidden.status_code == 404
    assert forbidden.json()["code"] == "KB_NOT_FOUND"

    assert (await client.patch(
        f"/api/resources/{resource.json()['id']}",
        headers=owner,
        json={"linked_kb_id": None},
    )).status_code == 200
    assert (await client.delete(f"/api/resources/{resource.json()['id']}", headers=owner)).status_code == 204
    assert (await client.delete(f"/api/kb/{kb_id}", headers=owner)).status_code == 204


async def test_assessment_draft_delete_empty_answers_and_scoring_defaults(client):
    headers = await _register(client, "assessment-launch-contract")
    resource = await client.post(
        "/api/resources",
        headers=headers,
        json={"title": "真实试卷作答", "resource_type": "exam_paper"},
    )
    assert resource.status_code == 201, resource.text
    resource_id = resource.json()["id"]
    question = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "请写出答案", "answer": "42", "points": 2},
    )
    assert question.status_code == 201, question.text
    question_id = question.json()["id"]
    verified = await client.post(
        f"/api/resource-questions/{question_id}/verify",
        headers=headers,
        json={"answer": "42"},
    )
    assert verified.status_code == 200, verified.text

    attempt = await client.post(
        "/api/assessment-attempts",
        headers=headers,
        json={"resource_id": resource_id, "attempt_type": "practice", "title": "期中考试记录"},
    )
    assert attempt.status_code == 201, attempt.text
    attempt_id = attempt.json()["id"]
    assert attempt.json()["attempt_type"] == "formal_exam"

    empty_submit = await client.post(f"/api/assessment-attempts/{attempt_id}/submit", headers=headers)
    assert empty_submit.status_code == 409
    assert empty_submit.json()["code"] == "ASSESSMENT_ANSWER_REQUIRED"
    empty_save = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_id, "response": ""}]},
    )
    assert empty_save.status_code == 200
    assert empty_save.json()["answer_count"] == 0

    explicit_unanswered = await client.put(
        f"/api/assessment-attempts/{attempt_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_id, "unanswered": True}]},
    )
    assert explicit_unanswered.status_code == 200
    assert explicit_unanswered.json()["answer_count"] == 1
    assert (await client.post(f"/api/assessment-attempts/{attempt_id}/submit", headers=headers)).status_code == 200
    verified_result = await client.post(
        f"/api/assessment-attempts/{attempt_id}/grade-or-verify",
        headers=headers,
        json={
            "verify": True,
            "answers": [{"resource_question_id": question_id, "response": "未作答", "is_correct": False}],
        },
    )
    assert verified_result.status_code == 200, verified_result.text
    assert verified_result.json()["score"] == 0
    assert verified_result.json()["total_score"] == 2
    assert len(
        [
            item
            for item in (await client.get("/api/errors", headers=headers)).json()["items"]
            if item["resource_question_id"] == question_id
        ]
    ) == 1

    submitted_delete = await client.delete(f"/api/assessment-attempts/{attempt_id}", headers=headers)
    assert submitted_delete.status_code == 409
    assert submitted_delete.json()["code"] == "ASSESSMENT_NOT_DRAFT"

    draft = await client.post(
        "/api/assessment-attempts",
        headers=headers,
        json={"resource_id": resource_id},
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["id"]
    saved = await client.put(
        f"/api/assessment-attempts/{draft_id}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": question_id, "response": "42"}]},
    )
    assert saved.status_code == 200, saved.text
    hidden = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert hidden.status_code == 200
    assert hidden.json()[0].get("answer") is None

    deleted = await client.delete(f"/api/assessment-attempts/{draft_id}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/assessment-attempts/{draft_id}", headers=headers)).status_code == 404
    visible = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert visible.status_code == 200
    assert visible.json()[0]["answer"] == "42"
