"""画像可争辩「验证一下」重估全链路 (P2-6, 审查修复轮回归)。

审查曾确认: 不绑库发起的验证卷经 _resolve_kc_ids 的 `if not kb_id` 早退落库
kc_ids=[], 判分永远写不动 BKT —— 前端承诺的"判分结果将作为真实证据重估你的
掌握度"完全不发生 (重估空转)。修复 = 无 kb 时按该用户有掌握度记录的 KC 解析。
本测试锁死修复: 不绑库 + kc_names 定向 3 题 → 全对提交 → p_mastery 上升且
mastery_log 有 quiz_attempt 审计行 (F8-A3 口径可核对)。
"""

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

_KC_NAME = "导数定义"
_BLUEPRINT = {
    "items": [
        {"kc_name": _KC_NAME, "type": "single", "difficulty": "easy"}
        for _ in range(3)
    ],
    "summary": "验证卷",
}
_QUESTIONS = {
    "questions": [
        {
            "type": "single",
            "stem": f"验证题 {i}: 导数定义相关?",
            "options": ["对", "错", "无关", "不确定"],
            "answer": "A",
            "explanation": "定义即差商极限。",
            "kc_names": [_KC_NAME],
            "difficulty": "easy",
            "source_hint": None,
        }
        for i in range(3)
    ]
}


class _FakeQuizGateway:
    async def chat(self, req):
        if req.feature.startswith("agent_"):
            from native_model import native_fixture_reply
            return await native_fixture_reply(self, req)
        from eduagents_core.gateway import ChatResult

        payload = _BLUEPRINT if req.feature == "quiz_blueprint" else _QUESTIONS
        if not req.json_mode:
            payload = {"text": "ok"}
        return ChatResult(
            text=json.dumps(payload, ensure_ascii=False),
            model="fake", tier=req.tier.value, prompt_tokens=1,
            completion_tokens=1, cost_cny=0.0, latency_ms=1, mode="live",
        )


async def _sse(client, method, url, headers, body) -> dict[str, list]:
    events: dict[str, list] = {}
    current = None
    async with client.stream(method, url, json=body, headers=headers, timeout=60) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current:
                events.setdefault(current, []).append(json.loads(line.split(":", 1)[1]))
    return events


async def test_verify_quiz_without_kb_reassesses_mastery(client):
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.harness_runtime import HarnessRuntime
    from eduagents_api.models import (
        KnowledgeBase,
        KnowledgeComponent,
        MasteryLog,
        MasteryRecord,
        Question,
        User,
    )
    from eduagents_core.config import get_settings
    from eduagents_core.education.context import EducationDeps

    email = f"verify-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    async with plain_session() as s:
        uid = str(await s.scalar(select(User.id).where(User.email == email)))

    # 种子: 该用户在某库有 KC 与掌握度记录 (画像页"验证一下"的名称来源)
    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=tid, name="高等数学", subject="math")
        s.add(kb)
        await s.flush()
        kc = KnowledgeComponent(
            tenant_id=tid, kb_id=kb.id, canonical_name=_KC_NAME, aliases=["导数的定义"]
        )
        s.add(kc)
        await s.flush()
        kc_id = str(kc.id)
        s.add(
            MasteryRecord(
                tenant_id=tid, user_id=tid, kc_id=kc.id,
                p_mastery=0.35, confidence=0.6, last_seen=datetime.now(UTC),
            )
        )

    app = client._transport.app
    settings = get_settings()
    runtime = HarnessRuntime(
        settings,
        EducationDeps(settings=settings, gateway=_FakeQuizGateway(), retrieval=None, web_search=None),
    )
    await runtime.start()
    old_runtime = app.state.harness
    app.state.harness = runtime
    try:
        # 验证流入口口径 (quiz/page.tsx startVerifyQuiz): 不绑库 + kc_names 定向 + 3 题
        events = await _sse(client, "POST", "/api/quiz", headers, {
            "topic": _KC_NAME, "num_questions": 3, "kc_names": [_KC_NAME],
            "difficulty_mix": {"easy": 3, "mid": 0, "hard": 0},
        })
        intr = events["interrupt"][0]
        events2 = await _sse(
            client, "POST", intr["resume_url"], headers, {"value": {"approved": True}}
        )
        quiz_id = events2["done"][0]["result"]["quiz_id"]

        r = await client.get(f"/api/quiz/{quiz_id}", headers=headers)
        questions = r.json()["questions"]
        assert len(questions) == 3

        # 修复锁死点①: 无 kb 也按用户掌握度记录解析出 kc_ids, 不再空数组落库
        async with tenant_session(uid) as s:
            rows = (
                await s.scalars(
                    select(Question).where(Question.quiz_id == uuid.UUID(quiz_id))
                )
            ).all()
        assert all(row.kc_ids == [kc_id] for row in rows), [row.kc_ids for row in rows]

        # 3 题全对提交 -> 重估
        r = await client.post(
            f"/api/quiz/{quiz_id}/attempts", headers=headers,
            json={"answers": [{"question_id": q["id"], "response": "A"} for q in questions]},
        )
        assert r.json()["correct"] == 3, r.text
    finally:
        app.state.harness = old_runtime
        await runtime.stop()

    # 修复锁死点②: 掌握度真实上升 + mastery_log 有可核对的审计行 (F8-A3)
    async with tenant_session(uid) as s:
        record = await s.scalar(
            select(MasteryRecord).where(MasteryRecord.kc_id == uuid.UUID(kc_id))
        )
        logs = (
            await s.scalars(
                select(MasteryLog).where(MasteryLog.kc_id == uuid.UUID(kc_id))
            )
        ).all()
    assert record.p_mastery > 0.35, "验证流必须真实重估掌握度, 不许空转"
    assert len(logs) == 3
    assert all(log.evidence_type == "quiz_attempt" for log in logs)
