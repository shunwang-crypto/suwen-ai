"""学生手动收录错题：待核验隔离、确认后入复习及租户权限。"""

import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


async def _register(client, prefix: str) -> tuple[uuid.UUID, dict[str, str]]:
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@test.eduagents.cn"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    user_id = uuid.UUID(response.json()["id"])
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return user_id, {"Authorization": f"Bearer {response.json()['access_token']}"}


def _payload(*, verified: bool) -> dict:
    return {
        "type": "single",
        "stem": "函数 f(x)=x^2 的导数是什么？",
        "options": ["A. x", "B. 2x", "C. x^2", "D. 2"],
        "my_answer": "A",
        "answer": "B" if verified else "",
        "explanation": "使用幂函数求导法则。" if verified else "",
        "kc_names": ["导数"],
        "difficulty": "easy",
        "kb_id": None,
        "cause": "formula_error",
        "answer_verified": verified,
    }


async def test_manual_error_import_verification_flow(client):
    user_id, headers = await _register(client, "err-import")
    _other_id, other_headers = await _register(client, "err-import-other")

    response = await client.post("/api/errors/imports/manual", headers=headers, json={})
    assert response.status_code == 201, response.text
    imported = response.json()
    assert imported["source_type"] == "manual" and imported["status"] == "draft"

    # 其他用户即使知道导入 ID 也无法确认。
    response = await client.post(
        f"/api/errors/imports/{imported['id']}/confirm",
        headers=other_headers,
        json=_payload(verified=False),
    )
    assert response.status_code == 404

    response = await client.post(
        f"/api/errors/imports/{imported['id']}/confirm",
        headers=headers,
        json=_payload(verified=False),
    )
    assert response.status_code == 200, response.text
    error_id = response.json()["error_item_id"]
    assert response.json()["verification_status"] == "pending"

    response = await client.get("/api/errors", headers=headers)
    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["items"] if row["id"] == error_id)
    assert item["verification_status"] == "pending"
    assert item["question"]["source_type"] == "manual"
    assert item["my_answer"] == "A"
    response = await client.get("/api/errors?state=active", headers=headers)
    assert error_id not in {row["id"] for row in response.json()["items"]}
    response = await client.get("/api/errors/stats", headers=headers)
    assert response.json()["pending_verification"] >= 1

    response = await client.get("/api/reviews/today", headers=headers)
    assert error_id not in {row["error_item_id"] for row in response.json()["items"]}
    response = await client.post(f"/api/reviews/{error_id}/variant", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "ANSWER_NOT_VERIFIED"

    response = await client.patch(
        f"/api/errors/{error_id}/content", headers=headers, json=_payload(verified=True)
    )
    assert response.status_code == 200, response.text
    assert response.json()["verification_status"] == "verified"
    response = await client.get("/api/reviews/today", headers=headers)
    assert error_id in {row["error_item_id"] for row in response.json()["items"]}
    response = await client.get("/api/errors?state=active", headers=headers)
    assert error_id in {row["id"] for row in response.json()["items"]}

    from eduagents_api.db import tenant_session
    from eduagents_api.models import Attempt, ErrorImport, ErrorItem, Question

    async with tenant_session(user_id) as session:
        error = await session.get(ErrorItem, uuid.UUID(error_id))
        question = await session.get(Question, error.question_id)
        imported_row = await session.get(ErrorImport, uuid.UUID(imported["id"]))
        attempt = await session.scalar(
            select(Attempt).where(
                Attempt.user_id == user_id, Attempt.question_id == question.id
            )
        )
        assert error.verification_status == "verified"
        assert question.source_type == "manual"
        assert question.answer == "B"
        assert imported_row.status == "confirmed"
        assert attempt.response == "A" and attempt.is_correct is False


async def test_document_error_import_keeps_real_source_and_editable_draft(client):
    _user_id, headers = await _register(client, "err-document")
    response = await client.post(
        "/api/errors/imports",
        headers=headers,
        files={
            "file": (
                "一道错题.txt",
                "题目：1+1等于多少？\n我的答案：3\n正确答案：2".encode(),
                "text/plain",
            )
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_type"] == "document"
    assert body["filename"] == "一道错题.txt"
    # 集成测试使用 stub 网关：必须明确要求人工校对，不能生成伪识别内容。
    assert body["draft"]["stem"] == ""
    assert "手动录入" in body["recognition_warning"]

    payload = {
        "type": "short",
        "stem": "1+1等于多少？",
        "options": [],
        "my_answer": "3",
        "answer": "2",
        "explanation": "基础加法。",
        "kc_names": [],
        "difficulty": "easy",
        "kb_id": None,
        "cause": "calc_slip",
        "answer_verified": True,
    }
    response = await client.post(
        f"/api/errors/imports/{body['id']}/confirm", headers=headers, json=payload
    )
    assert response.status_code == 200, response.text
    error_id = response.json()["error_item_id"]
    response = await client.get("/api/errors", headers=headers)
    item = next(row for row in response.json()["items"] if row["id"] == error_id)
    assert item["question"]["source_type"] == "document"
    assert item["question"]["source_meta"]["filename"] == "一道错题.txt"
