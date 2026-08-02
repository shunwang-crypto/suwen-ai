from __future__ import annotations

import base64
import uuid

import pymupdf
from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = requires_services

_PASSWORD = "pass-12345"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeVisionGateway:
    def __init__(self):
        self.requests = []

    async def chat(self, request):
        from eduagents_core.gateway import ChatResult

        self.requests.append(request)
        page = len(self.requests) % 2 or 2
        return ChatResult(
            text=f"# 第 {page} 页题目\n\n计算：$$\\sum_{{n=1}}^{{10}} n$$",
            model="fake-vision",
            tier="standard",
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0,
            latency_ms=1,
            mode="stub",
        )


def _mixed_pdf() -> bytes:
    document = pymupdf.open()
    mixed = document.new_page(width=400, height=500)
    mixed.insert_text((30, 35), "Mixed page title", fontsize=12)
    mixed.insert_image(pymupdf.Rect(20, 80, 380, 470), stream=_PNG)
    scanned = document.new_page(width=400, height=500)
    scanned.insert_image(scanned.rect, stream=_PNG)
    data = document.tobytes()
    document.close()
    return data


async def _register(client):
    email = f"pdf-ingest-{uuid.uuid4().hex[:10]}@test.eduagents.cn"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": _PASSWORD}
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return user_id, {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_mixed_pdf_ingestion_preserves_pages_and_is_retry_idempotent(
    client, monkeypatch
):
    user_id, headers = await _register(client)
    kb = await client.post(
        "/api/kb", headers=headers, json={"name": "mixed-pdf", "subject": "math"}
    )
    assert kb.status_code == 201, kb.text
    kb_id = kb.json()["id"]
    uploaded = await client.post(
        f"/api/kb/{kb_id}/documents",
        headers=headers,
        files={"file": ("mixed.pdf", _mixed_pdf(), "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    doc_id = uploaded.json()["id"]

    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, ErrorItem, Question
    from eduagents_core.config import get_settings
    from eduagents_worker import ingestion

    settings = get_settings().model_copy(
        update={"gateway_mode": "live", "vision_model": "fake-vision"}
    )
    monkeypatch.setattr(ingestion, "get_settings", lambda: settings)
    gateway = FakeVisionGateway()

    result = await ingestion.ingest_document(
        {"gateway": gateway, "redis": None}, user_id, doc_id
    )
    assert result == "ready"
    assert len(gateway.requests) == 2
    assert all(request.credential_policy == "platform" for request in gateway.requests)
    first_request_ids = [request.request_id for request in gateway.requests]

    async with tenant_session(user_id) as session:
        document = await session.get(Document, uuid.UUID(doc_id))
        assert document is not None
        assert document.parse_backend == "pdf-mixed"
        assert [page["extraction_method"] for page in document.recognition_meta_json["pages"]] == [
            "mixed",
            "scanned",
        ]
        chunks = (
            await session.scalars(
                select(Chunk).where(
                    Chunk.doc_id == uuid.UUID(doc_id), Chunk.parent_id.isnot(None)
                )
            )
        ).all()
        assert {chunk.page_start for chunk in chunks} == {1, 2}
        first_chunk_count = await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.doc_id == uuid.UUID(doc_id))
        )
        assert await session.scalar(select(func.count()).select_from(Question)) == 0
        assert await session.scalar(select(func.count()).select_from(ErrorItem)) == 0

    # A worker replay of the same document version replaces chunks/vectors and
    # reuses page request IDs, allowing wallet consumption to remain idempotent.
    assert (
        await ingestion.ingest_document(
            {"gateway": gateway, "redis": None}, user_id, doc_id
        )
        == "ready"
    )
    assert {request.request_id for request in gateway.requests[2:]} == set(first_request_ids)
    async with tenant_session(user_id) as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.doc_id == uuid.UUID(doc_id))
            )
            == first_chunk_count
        )

    async with tenant_session(user_id) as session:
        before_reindex = await session.get(Document, uuid.UUID(doc_id))
        assert before_reindex is not None
        before_version = before_reindex.version

    reindex = await client.post(
        f"/api/kb/{kb_id}/documents/{doc_id}/reindex",
        headers=headers,
    )
    assert reindex.status_code == 202, reindex.text

    async with tenant_session(user_id) as session:
        after_reindex = await session.get(Document, uuid.UUID(doc_id))
        assert after_reindex is not None
        assert after_reindex.version == before_version + 1

    assert (
        await ingestion.ingest_document(
            {"gateway": gateway, "redis": None}, user_id, doc_id
        )
        == "ready"
    )
    reindexed_request_ids = {request.request_id for request in gateway.requests[4:]}
    assert len(reindexed_request_ids) == 2
    assert reindexed_request_ids.isdisjoint(first_request_ids)
