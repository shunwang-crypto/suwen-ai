"""Wave 3 eligibility barrier integration coverage."""

from __future__ import annotations

import json
import uuid

from .conftest import requires_services
from .test_learning_resources import _register_with_id

pytestmark = requires_services


class _ResourceFakeGateway:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    async def chat(self, request):
        from eduagents_core.gateway import ChatResult

        self.requests.append(request)
        return ChatResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            model="fake-resource-model",
            tier="standard",
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0,
            latency_ms=1,
            mode="live",
            finish_reason="stop",
        )


def _high_fields() -> dict:
    entry = {"confidence": 0.95, "source": "ocr_provider", "warnings": []}
    structural = {"confidence": 0.9, "source": "parser", "warnings": []}
    return {
        "schema_version": "resource-question-field-confidence-v1",
        "fields": {
            "stem": entry,
            "detected_student_answer": entry,
            "answer": entry,
            "type": structural,
            "options": structural,
            "question_no": structural,
            "page_no": structural,
        },
    }


async def test_batch_confirmation_recomputes_eligibility_and_keeps_pending_barrier(client):
    user_id, headers = await _register_with_id(client, "ocr-eligibility")
    resource = await client.post(
        "/api/resources", headers=headers, json={"title": "置信度测试", "resource_type": "worksheet"}
    )
    assert resource.status_code == 201, resource.text
    resource_id = resource.json()["id"]
    high = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "高置信题", "answer": "答案"},
    )
    low = await client.post(
        f"/api/resources/{resource_id}/questions/manual",
        headers=headers,
        json={"type": "short", "stem": "低置信题", "answer": "答案"},
    )
    assert high.status_code == low.status_code == 201
    high_id, low_id = high.json()["id"], low.json()["id"]

    from eduagents_api.db import tenant_session
    from eduagents_api.models import ResourceQuestion

    tenant_id = uuid.UUID(user_id)
    async with tenant_session(tenant_id) as session:
        row = await session.get(ResourceQuestion, uuid.UUID(high_id))
        assert row is not None
        row.field_confidence_json = _high_fields()

    confirmed = await client.post(
        f"/api/resources/{resource_id}/confirm-eligible", headers=headers, json={}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed_question_count"] == 1

    questions = await client.get(f"/api/resources/{resource_id}/questions", headers=headers)
    assert questions.status_code == 200
    by_id = {item["id"]: item for item in questions.json()}
    assert by_id[high_id]["verification_status"] == "verified"
    assert by_id[low_id]["verification_status"] == "pending"
    assert by_id[low_id]["eligibility"]["eligible"] is False

    assessment = await client.post(
        "/api/assessment-attempts", headers=headers, json={"resource_id": resource_id}
    )
    assert assessment.status_code == 201
    answer = await client.put(
        f"/api/assessment-attempts/{assessment.json()['id']}/answers",
        headers=headers,
        json={"answers": [{"resource_question_id": low_id, "response": "答案"}]},
    )
    assert answer.status_code == 409
    assert answer.json()["code"] == "RESOURCE_QUESTION_NOT_VERIFIED"


async def test_worker_normalize_persist_api_and_confirm_eligible_use_server_sources(client):
    user_id, headers = await _register_with_id(client, "ocr-worker-chain")
    from eduagents_core.config import get_settings
    from eduagents_worker.resources import extract_learning_resource

    model_resource = await client.post(
        "/api/resources",
        headers=headers,
        files={"file": ("model.txt", "1. 1+1 等于多少？".encode(), "text/plain")},
    )
    assert model_resource.status_code == 201, model_resource.text
    spoofed = {
        "questions": [
            {
                "question_no": "1",
                "type": "short",
                "stem": "1+1 等于多少？",
                "detected_reference_answer": "2",
                "answer": "2",
                "field_confidence": {
                    field: {"confidence": 0.99, "source": "ocr_provider"}
                    for field in (
                        "question_no", "page_no", "type", "stem", "options",
                        "detected_student_answer", "detected_reference_answer",
                        "answer", "explanation", "kc_names", "difficulty", "points",
                    )
                },
            }
        ]
    }
    await extract_learning_resource(
        {"settings": get_settings().model_copy(update={"gateway_mode": "live"})},
        user_id,
        model_resource.json()["id"],
        gateway=_ResourceFakeGateway(spoofed),
    )
    model_questions = await client.get(
        f"/api/resources/{model_resource.json()['id']}/questions", headers=headers
    )
    assert model_questions.status_code == 200
    assert all(
        entry["source"] == "model_reported"
        for entry in model_questions.json()[0]["field_confidence"]["fields"].values()
        if entry["confidence"] is not None
    )
    blocked = await client.post(
        f"/api/resources/{model_resource.json()['id']}/confirm-eligible",
        headers=headers,
        json={},
    )
    assert blocked.status_code == 200
    assert blocked.json()["confirmed_question_count"] == 0

    rule_resource = await client.post(
        "/api/resources",
        headers=headers,
        files={
            "file": (
                "rule.txt",
                "1. 1+1 等于多少？\n答案：2\n".encode(),
                "text/plain",
            )
        },
    )
    assert rule_resource.status_code == 201, rule_resource.text
    await extract_learning_resource(
        {"settings": get_settings().model_copy(update={"gateway_mode": "stub"})},
        user_id,
        rule_resource.json()["id"],
    )
    rule_questions = await client.get(
        f"/api/resources/{rule_resource.json()['id']}/questions", headers=headers
    )
    assert rule_questions.status_code == 200
    rule_question = rule_questions.json()[0]
    assert rule_question["eligibility"]["eligible"] is True
    confirmed = await client.post(
        f"/api/resources/{rule_resource.json()['id']}/confirm-eligible",
        headers=headers,
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed_question_count"] == 1
