"""学生手动收录错题：隔离、状态机、来源文件、清理及导出。"""

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

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

    profile = await client.get("/api/profile", headers=headers)
    assert profile.status_code == 200
    assert "formula_error" not in profile.json()["error_distribution"]["by_cause"]
    assert "new" not in profile.json()["error_distribution"]["by_state"]
    from eduagents_api.profile_summary import build_profile_context

    summary = await build_profile_context(user_id, user_id, redis=None)
    assert "公式用错" not in summary.summary
    report = await client.get("/api/reports/learning", headers=headers)
    assert report.status_code == 200
    report_body = report.json()
    assert report_body["open_errors"] == 0
    assert report_body["metrics"]["attempt_count"] == 0
    assert all(point["attempts"] == 0 for point in report_body["trend"])
    actions = (await client.get("/api/next-actions", headers=headers)).json()["items"]
    assert all(action["type"] != "review" for action in actions)

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
    response = await client.patch(
        f"/api/errors/{error_id}/content", headers=headers, json=_payload(verified=False)
    )
    assert response.status_code == 409
    assert response.json()["code"] == "VERIFICATION_ROLLBACK_FORBIDDEN"
    editable = _payload(verified=True)
    editable["stem"] = "修正后的函数导数题"
    response = await client.patch(
        f"/api/errors/{error_id}/content", headers=headers, json=editable
    )
    assert response.status_code == 200, response.text
    response = await client.get("/api/reviews/today", headers=headers)
    assert error_id in {row["error_item_id"] for row in response.json()["items"]}
    response = await client.get("/api/errors?state=active", headers=headers)
    assert error_id in {row["id"] for row in response.json()["items"]}

    response = await client.post(
        f"/api/reviews/{error_id}/answer", headers=headers, json={"response": "B"}
    )
    assert response.status_code == 200, response.text
    locked = _payload(verified=True)
    locked["answer"] = "C"
    response = await client.patch(
        f"/api/errors/{error_id}/content", headers=headers, json=locked
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ERROR_ITEM_LOCKED_AFTER_REVIEW"

    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import Attempt, ErrorImport, ErrorItem, Question, Quiz, User

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
        assert attempt is None
        assert error.original_response == "A"

        assert question.quiz_id is None
        imported_quiz = await session.scalar(
            select(Quiz).where(
                Quiz.user_id == user_id,
                Quiz.blueprint_json["kind"].astext == "error_import",
            )
        )
        assert imported_quiz is None
    assert (await client.get("/api/quiz", headers=headers)).json() == []
    async with plain_session() as session:
        user = await session.get(User, user_id)
        user.role = "teacher"
        email = user.email
    login = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    teacher_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    teacher_quizzes = await client.get("/api/teacher/quizzes", headers=teacher_headers)
    assert teacher_quizzes.status_code == 200
    assert teacher_quizzes.json() == []


async def test_document_error_import_keeps_real_source_and_editable_draft(client):
    user_id, headers = await _register(client, "err-document")
    _other_id, other_headers = await _register(client, "err-document-other")
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
    assert body["mime_type"] == "text/plain"
    assert body["source_url"] == f"/api/errors/imports/{body['id']}/source"
    # 集成测试使用 stub 网关：必须明确要求人工校对，不能生成伪识别内容。
    assert body["draft"]["stem"] == ""
    assert "手动录入" in body["recognition_warning"]

    response = await client.get(body["source_url"], headers=other_headers)
    assert response.status_code == 404
    response = await client.get(body["source_url"], headers=headers)
    assert response.status_code == 200
    assert response.content.startswith("题目：1+1".encode())
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "filename*=" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"

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
    assert item["source_import_id"] == body["id"]
    response = await client.get(f"/api/errors/{error_id}/source", headers=headers)
    assert response.status_code == 200
    response = await client.get(f"/api/errors/{error_id}/source", headers=other_headers)
    assert response.status_code == 404

    from eduagents_api.db import tenant_session
    from eduagents_api.models import ErrorImport
    from eduagents_core.config import get_settings
    from eduagents_worker.export import export_user_data

    missing_id = uuid.uuid4()
    async with tenant_session(user_id) as session:
        session.add(
            ErrorImport(
                id=missing_id,
                tenant_id=user_id,
                user_id=user_id,
                source_type="document",
                filename="../missing.txt",
                mime_type="text/plain",
                size_bytes=7,
                storage_key=f"{user_id}/error-imports/{missing_id}/missing.txt",
            )
        )
    await export_user_data({"settings": get_settings()}, str(user_id))
    response = await client.get("/api/account/export/download", headers=headers)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        zip_path = f"uploads/error_imports/{body['id']}/一道错题.txt"
        assert zip_path in archive.namelist()
        assert archive.read(zip_path) == "题目：1+1等于多少？\n我的答案：3\n正确答案：2".encode()
        manifest = json.loads(archive.read("uploads_manifest.json"))
        entry = next(row for row in manifest if row.get("error_import_id") == body["id"])
        assert entry["available"] is True and entry["zip_path"] == zip_path
        missing_entry = next(row for row in manifest if row.get("error_import_id") == str(missing_id))
        assert missing_entry["available"] is False
        assert missing_entry["error"] == "SOURCE_NOT_FOUND"


async def test_error_import_cancel_expiry_and_pending_limit(client):
    user_id, headers = await _register(client, "err-cleanup-cancel")
    from eduagents_core.config import get_settings
    from eduagents_core.storage import create_storage

    storage = create_storage(get_settings())
    response = await client.post(
        "/api/errors/imports",
        headers=headers,
        files={"file": ("cancel.txt", b"will be discarded", "text/plain")},
    )
    assert response.status_code == 201, response.text
    imported_id = response.json()["id"]
    cancel_key = f"{user_id}/error-imports/{imported_id}/cancel.txt"
    assert await storage.load(cancel_key) == b"will be discarded"
    response = await client.delete(f"/api/errors/imports/{imported_id}", headers=headers)
    assert response.status_code == 204, response.text
    try:
        await storage.load(cancel_key)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - object cleanup must be observable
        raise AssertionError("cancelled source object was not deleted")
    response = await client.get(
        f"/api/errors/imports/{imported_id}/source", headers=headers
    )
    assert response.status_code == 404

    expired_id, _expired_headers = await _register(client, "err-cleanup-expired")
    from eduagents_api.db import tenant_session
    from eduagents_api.error_import_cleanup import error_import_storage_prefix
    from eduagents_api.models import ErrorImport

    storage_key = f"{expired_id}/error-imports/{uuid.uuid4()}/stale.txt"
    stale_import_id = uuid.UUID(storage_key.split("/")[2])
    await storage.save(storage_key, b"stale")
    async with tenant_session(expired_id) as session:
        session.add(
            ErrorImport(
                id=stale_import_id,
                tenant_id=expired_id,
                user_id=expired_id,
                source_type="document",
                filename="stale.txt",
                mime_type="text/plain",
                size_bytes=5,
                storage_key=storage_key,
                created_at=datetime.now(UTC) - timedelta(days=2),
            )
        )
    response = await client.post(
        "/api/errors/imports/manual", headers=_expired_headers, json={}
    )
    assert response.status_code == 201, response.text
    async with tenant_session(expired_id) as session:
        assert await session.get(ErrorImport, stale_import_id) is None
    try:
        await storage.load(storage_key)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - object cleanup must be observable
        raise AssertionError("expired source object was not deleted")
    assert error_import_storage_prefix(expired_id, stale_import_id).endswith(str(stale_import_id))

    _limit_id, limit_headers = await _register(client, "err-cleanup-limit")
    for _ in range(20):
        response = await client.post("/api/errors/imports/manual", headers=limit_headers, json={})
        assert response.status_code == 201, response.text
    response = await client.post("/api/errors/imports/manual", headers=limit_headers, json={})
    assert response.status_code == 429
    assert response.json()["code"] == "TOO_MANY_PENDING_ERROR_IMPORTS"
