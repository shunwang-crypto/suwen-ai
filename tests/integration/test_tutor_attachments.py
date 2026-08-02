from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pymupdf
from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = requires_services
PASSWORD = "pass-12345"


class FakeGateway:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    async def chat(self, request):
        from eduagents_core.gateway import ChatResult

        self.requests.append(request)
        return ChatResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            model="fake",
            tier="standard",
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0,
            latency_ms=1,
            mode="stub",
        )


async def _register(client, prefix: str):
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@test.eduagents.cn"
    response = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return user_id, {"Authorization": f"Bearer {response.json()['access_token']}"}


def _pdf(page_count: int) -> bytes:
    document = pymupdf.open()
    for index in range(page_count):
        page = document.new_page()
        page.insert_text((72, 72), f"Question on page {index + 1} " * 5)
    data = document.tobytes()
    document.close()
    return data


async def test_pdf_upload_page_selection_queue_payload_and_owner_isolation(client):
    owner_id, owner = await _register(client, "tutor-pdf-owner")
    _, stranger = await _register(client, "tutor-pdf-stranger")
    app = client._transport.app
    original_arq = app.state.arq
    calls = []

    async def enqueue(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(job_id=kwargs.get("_job_id"))

    app.state.arq = SimpleNamespace(enqueue_job=enqueue)
    try:
        response = await client.post(
            "/api/tutor/drafts",
            headers=owner,
            files={"file": ("paper.pdf", _pdf(5), "application/pdf")},
        )
        assert response.status_code == 201, response.text
        draft = response.json()
        assert draft["status"] == "uploaded"
        assert draft["selected_pages"] == [1, 2, 3]
        assert draft["attachment"]["page_count"] == 5
        assert calls == []

        too_many = await client.post(
            f"/api/tutor/drafts/{draft['id']}/recognize",
            headers=owner,
            json={"pages": list(range(1, 10))},
        )
        assert too_many.status_code == 422 or too_many.status_code == 413

        recognized = await client.post(
            f"/api/tutor/drafts/{draft['id']}/recognize",
            headers=owner,
            json={"pages": [2, 4]},
        )
        assert recognized.status_code == 202, recognized.text
        assert recognized.json()["status"] == "recognizing"
        assert calls[0][0] == ("recognize_tutor_problem", owner_id, draft["id"])
        assert list(calls[0][1]) == ["_job_id"]
        duplicate = await client.post(
            f"/api/tutor/drafts/{draft['id']}/recognize",
            headers=owner,
            json={"pages": [2, 4]},
        )
        assert duplicate.status_code == 202
        assert len(calls) == 1

        assert (await client.get(f"/api/tutor/drafts/{draft['id']}", headers=stranger)).status_code == 404
        assert (await client.get(f"/api/tutor/drafts/{draft['id']}/source", headers=stranger)).status_code == 404
        assert (await client.delete(f"/api/tutor/drafts/{draft['id']}", headers=stranger)).status_code == 404
        source = await client.get(f"/api/tutor/drafts/{draft['id']}/source", headers=owner)
        assert source.status_code == 200
        assert source.content.startswith(b"%PDF")
    finally:
        app.state.arq = original_arq


async def test_worker_recognizes_multiple_questions_for_explicit_selection(client):
    user_id, headers = await _register(client, "tutor-worker")
    response = await client.post(
        "/api/tutor/drafts",
        headers=headers,
        files={"file": ("two.pdf", _pdf(2), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    draft_id = response.json()["id"]
    queued = await client.post(
        f"/api/tutor/drafts/{draft_id}/recognize", headers=headers, json={"pages": [1, 2]}
    )
    assert queued.status_code == 202, queued.text

    from eduagents_core.config import get_settings
    from eduagents_worker.tutor_recognition import recognize_tutor_problem

    gateway = FakeGateway(
        [
            {"question_no": "1", "page_no": 1, "type": "short", "stem": "第一题"},
            {"question_no": "2", "page_no": 2, "type": "short", "stem": "第二题"},
        ]
    )
    settings = get_settings().model_copy(update={"gateway_mode": "live"})
    result = await recognize_tutor_problem(
        {"settings": settings}, user_id, draft_id, gateway=gateway, settings=settings
    )
    assert result == "review"
    reviewed = await client.get(f"/api/tutor/drafts/{draft_id}", headers=headers)
    assert reviewed.status_code == 200
    assert [question["stem"] for question in reviewed.json()["questions"]] == ["第一题", "第二题"]
    selected = await client.patch(
        f"/api/tutor/drafts/{draft_id}",
        headers=headers,
        json={"selected_question_index": 1},
    )
    assert selected.status_code == 200
    assert selected.json()["stem"] == "第二题"


async def test_manual_review_confirmation_lock_and_server_snapshot(client):
    user_id, headers = await _register(client, "tutor-manual")
    response = await client.post(
        "/api/tutor/drafts/manual",
        headers=headers,
        json={
            "stem": "原始题干",
            "options": ["1", "2"],
            "known_conditions": "x > 0",
            "current_thought": "卡在变形",
        },
    )
    assert response.status_code == 201, response.text
    draft_id = response.json()["id"]
    confirmed = await client.post(
        f"/api/tutor/drafts/{draft_id}/confirm",
        headers=headers,
        json={
            "stem": "校对后的题干",
            "options": ["A", "B"],
            "known_conditions": "x = 2",
            "current_thought": "不会展开",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["status"] == "confirmed"
    assert body["confirmed_problem_text"] == "校对后的题干\n\nA. A\nB. B\n\n已知条件：x = 2\n\n当前思路或卡点：不会展开"
    assert body["thread_local_id"]

    again = await client.post(f"/api/tutor/drafts/{draft_id}/confirm", headers=headers, json={})
    assert again.status_code == 200
    assert again.json()["thread_local_id"] == body["thread_local_id"]
    locked = await client.patch(
        f"/api/tutor/drafts/{draft_id}", headers=headers, json={"stem": "偷偷换题"}
    )
    assert locked.status_code == 409
    assert locked.json()["code"] == "TUTOR_DRAFT_LOCKED"
    assert (await client.delete(f"/api/tutor/drafts/{draft_id}", headers=headers)).status_code == 409

    turn = await client.post(
        "/api/tutor",
        headers=headers,
        json={
            "problem": "客户端篡改的题干",
            "message": "请引导我",
            "thread_local_id": body["thread_local_id"],
            "tutor_draft_id": draft_id,
        },
    )
    assert turn.status_code == 200, turn.text
    restored = await client.get(f"/api/tutor/{body['thread_local_id']}", headers=headers)
    assert restored.status_code == 200, restored.text
    assert "校对后的题干" in restored.json()["problem"]
    assert "客户端篡改" not in restored.json()["problem"]

    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryLog, MasteryRecord

    async with tenant_session(user_id) as session:
        assert await session.scalar(select(func.count()).select_from(MasteryLog)) == 0
        assert await session.scalar(select(func.count()).select_from(MasteryRecord)) == 0


async def test_disguised_office_and_pdf_page_limit_are_rejected(client):
    _, headers = await _register(client, "tutor-security")
    disguised = await client.post(
        "/api/tutor/drafts",
        headers=headers,
        files={"file": ("fake.docx", b"PK\x03\x04not-an-office-file", "application/octet-stream")},
    )
    assert disguised.status_code == 400
    assert disguised.json()["code"] == "UNSAFE_OFFICE_ARCHIVE"

    oversized = await client.post(
        "/api/tutor/drafts",
        headers=headers,
        files={"file": ("long.pdf", _pdf(51), "application/pdf")},
    )
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "TUTOR_PDF_PAGE_LIMIT_EXCEEDED"


async def test_tutor_draft_and_original_file_are_in_user_export(client):
    user_id, headers = await _register(client, "tutor-export")
    created = await client.post(
        "/api/tutor/drafts",
        headers=headers,
        files={"file": ("export.pdf", _pdf(1), "application/pdf")},
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]

    from eduagents_core.config import get_settings
    from eduagents_core.storage import create_storage
    from eduagents_worker.export import export_user_data

    app = client._transport.app
    settings = get_settings()
    path = await export_user_data(
        {"settings": settings, "redis": app.state.redis}, user_id
    )
    archive_data = await create_storage(settings).load(path)
    with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
        drafts = json.loads(archive.read("tables/tutor_problem_drafts.json"))
        manifest = json.loads(archive.read("uploads_manifest.json"))
        assert any(row["id"] == draft_id for row in drafts)
        entry = next(row for row in manifest if row.get("filename") == "export.pdf")
        assert entry["available"] is True
        assert archive.read(entry["zip_path"]).startswith(b"%PDF")


async def test_expired_unconfirmed_draft_uses_outbox_for_object_cleanup(client):
    user_id, headers = await _register(client, "tutor-expiry")
    created = await client.post(
        "/api/tutor/drafts",
        headers=headers,
        files={"file": ("stale.pdf", _pdf(1), "application/pdf")},
    )
    assert created.status_code == 201, created.text
    draft_id = uuid.UUID(created.json()["id"])
    resource_id = uuid.UUID(created.json()["attachment"]["resource_id"])

    from eduagents_api.db import internal_cleanup_session, plain_session, tenant_session
    from eduagents_api.models import PendingCleanup, TutorProblemDraft
    from eduagents_core.config import get_settings
    from eduagents_core.storage import create_storage
    from eduagents_worker.cleanup_outbox import process_pending_cleanup
    from eduagents_worker.tutor_cleanup import reap_stale_tutor_drafts

    old = datetime.now(UTC) - timedelta(days=2)
    async with tenant_session(user_id) as session:
        draft = await session.get(TutorProblemDraft, draft_id)
        draft.updated_at = old
    settings = get_settings()
    cleaned = await reap_stale_tutor_drafts({"settings": settings})
    assert cleaned == 1
    async with internal_cleanup_session() as session:
        assert await session.get(TutorProblemDraft, draft_id) is None
    async with plain_session() as session:
        pending = await session.scalar(
            select(PendingCleanup).where(
                PendingCleanup.kind == "object_prefix",
                PendingCleanup.payload["prefix"].as_string()
                == f"{user_id}/learning-resources/{resource_id}",
            )
        )
        assert pending is not None and pending.done is False
    processed = await process_pending_cleanup({"settings": settings})
    assert processed >= 1
    storage = create_storage(settings)
    try:
        await storage.load(f"{user_id}/learning-resources/{resource_id}/stale.pdf")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expired tutor source was not removed")
