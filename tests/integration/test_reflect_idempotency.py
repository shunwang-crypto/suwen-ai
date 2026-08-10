"""P1-7 幂等: reflect_session 重复执行 (worker 重试/重复入队) 不得重复写画像。

apply_mastery_evidence 以 (user_id, kc_id, evidence_type, evidence_id) 查重 —
同一会话的重复反思, mastery_log 只保留第一组, BKT 不被重复推。
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

_PAYLOAD = {
    "insights": [
        {
            "kc_name": "导数定义",
            "signal": "confused",
            "evidence": "为什么导数定义里不能直接把 x=1 代进去?",
        }
    ]
}


class _FakeGateway:
    def __init__(self):
        self.calls = 0

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        self.calls += 1
        return ChatResult(
            text=json.dumps(_PAYLOAD, ensure_ascii=False),
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="replay",
        )


async def test_reflect_session_is_idempotent_on_rerun(client):
    email = f"reflect-idem-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    uid = r.json()["id"]

    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        ChatMessage,
        ChatSession,
        KnowledgeBase,
        KnowledgeComponent,
        MasteryLog,
        MasteryRecord,
    )
    from eduagents_worker.reflection import reflect_session

    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=tid, name="高等数学", subject="math")
        s.add(kb)
        await s.flush()
        kc = KnowledgeComponent(
            tenant_id=tid, kb_id=kb.id, canonical_name="导数定义", aliases=[]
        )
        s.add(kc)
        chat_session = ChatSession(tenant_id=tid, user_id=tid, title="导数答疑")
        s.add(chat_session)
        await s.flush()
        kc_id, session_id = str(kc.id), str(chat_session.id)
        s.add(
            ChatMessage(
                tenant_id=tid,
                session_id=chat_session.id,
                role="user",
                content="为什么导数定义里不能直接把 x=1 代进去?",
            )
        )

    gateway = _FakeGateway()
    assert await reflect_session({}, uid, uid, session_id, gateway=gateway) == "applied:1"
    # 重复执行 (模拟 arq 重试/重复入队): 不再落新 mastery_log
    assert await reflect_session({}, uid, uid, session_id, gateway=gateway) == "applied:0"
    assert gateway.calls == 2  # LLM 层去重靠主线的 max_tries=1 + _job_id, 不在本层

    async with tenant_session(uid) as s:
        logs = (
            await s.scalars(
                select(MasteryLog).where(
                    MasteryLog.evidence_type == "chat_reflection",
                    MasteryLog.evidence_id == uuid.UUID(session_id),
                )
            )
        ).all()
        record = await s.scalar(
            select(MasteryRecord).where(MasteryRecord.kc_id == uuid.UUID(kc_id))
        )
    assert len(logs) == 1  # 只有第一次执行写入
    assert record is not None
    assert record.p_mastery == pytest.approx(logs[0].new_p)  # BKT 未被二次推
