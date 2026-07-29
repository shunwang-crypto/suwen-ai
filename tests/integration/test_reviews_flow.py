"""M4/F6/§9: FSRS 复习队列 + 错题状态机 + time-travel 虚拟时钟 全链路 (D-A1 核心)。

直接构造 Quiz/Question/ErrorItem 行 (比走 quiz 图更稳), 经 HTTP 验证:
空卡到期入队 → 答对间隔拉长出队 → time-travel 3 天重新到期 → 连对 2 次 mastered →
答错 streak 清零且 BKT 下降 → 变式题落库 variant_of 正确 → 非 admin 403。
"""

import json
import uuid
from datetime import datetime

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

_VARIANT_QUESTION = {
    "type": "single",
    "stem": "将 $\\sin x$ 在 $x_0=0$ 处展开, 其麦克劳林展开的前两个非零项是?",
    "options": ["$x - x^3/6$", "$1 + x$", "$x + x^2/2$", "$1 - x^2/2$"],
    "answer": "A",
    "explanation": "$\\sin x = x - x^3/3! + \\cdots$, 同考点换函数情境。",
    "kc_names": ["泰勒公式"],
    "difficulty": "mid",
    "source_hint": None,
}


class FakeReviewGateway:
    """变式生成返回固定合法题 json (F6-A3); 其余 json 请求返回低分评分兜底。"""

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        if req.feature == "review_variant":
            payload = _VARIANT_QUESTION
        else:
            payload = {"score": 0.2, "reason": "fake"}
        return ChatResult(
            text=json.dumps(payload, ensure_ascii=False),
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=1,
            mode="live",
        )


async def _register_login(client, email: str) -> dict:
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _uid(email: str) -> str:
    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email))
        return str(user.id)


def _queue_ids(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [it["error_item_id"] for it in resp.json()["items"]]


async def test_reviews_fsrs_time_travel_flow(client):
    from eduagents_api import timectl
    from eduagents_api.routers import admin as admin_router
    from eduagents_api.routers import reviews as reviews_router

    app = client._transport.app
    # 测试自挂路由 (main.py 接线由集成方完成), 幂等防重复
    if not any(getattr(r, "path", "") == "/api/reviews/today" for r in app.router.routes):
        app.include_router(reviews_router.router)
        app.include_router(admin_router.router)
    redis = app.state.redis
    await timectl.set_offset(redis, 0)  # 全局偏移归零, 保证可重复运行

    email = f"rev-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    headers = await _register_login(client, email)
    uid = uuid.UUID(await _uid(email))

    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import (
        ErrorItem,
        KnowledgeBase,
        KnowledgeComponent,
        MasteryLog,
        MasteryRecord,
        Question,
        Quiz,
        User,
    )

    # 直接构造 KB/KC/Quiz/Question/ErrorItem (空 fsrs 卡 = 历史行, 视为到期)
    async with tenant_session(uid) as session:
        kb = KnowledgeBase(tenant_id=uid, name="高数", subject="math")
        session.add(kb)
        await session.flush()
        kc = KnowledgeComponent(tenant_id=uid, kb_id=kb.id, canonical_name="泰勒公式")
        session.add(kc)
        await session.flush()
        quiz = Quiz(tenant_id=uid, user_id=uid, title="复习流测试卷", status="ready")
        session.add(quiz)
        await session.flush()
        q1 = Question(
            tenant_id=uid,
            quiz_id=quiz.id,
            type="single",
            stem="泰勒公式在 $x_0=0$ 处的特例称为?",
            options_json=["麦克劳林公式", "洛必达法则", "拉格朗日中值定理", "柯西公式"],
            answer="A",
            explanation="展开点为 0 的特例。",
            kc_ids=[str(kc.id)],
        )
        session.add(q1)
        await session.flush()
        e1 = ErrorItem(tenant_id=uid, user_id=uid, question_id=q1.id, kc_ids=[str(kc.id)])
        session.add(e1)
        await session.flush()
        kc_id, quiz_id, q1_id, e1_id = str(kc.id), str(quiz.id), str(q1.id), str(e1.id)

    # a) 空 fsrs 卡视为到期 → 出现在今日队列
    r = await client.get("/api/reviews/today", headers=headers)
    assert e1_id in _queue_ids(r)

    # g) 非 admin 调 time-travel → 403
    r = await client.post("/api/admin/time-travel", headers=headers, json={"days": 3})
    assert r.status_code == 403
    r = await client.get("/api/admin/time-travel", headers=headers)
    assert r.status_code == 403

    # b) 答对 → new→reviewing, next_due 在虚拟未来, 队列消失
    r = await client.post(f"/api/reviews/{e1_id}/answer", headers=headers, json={"response": "A"})
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["correct"] is True and body["state"] == "reviewing"
    assert body["answer"] == "A"
    assert body["explanation"] == "展开点为 0 的特例。"
    assert body["mastery_changes"] and body["mastery_changes"][0]["kc_id"] == kc_id
    assert datetime.fromisoformat(body["next_due"]) > await timectl.now(redis)
    r = await client.get("/api/reviews/today", headers=headers)
    assert e1_id not in _queue_ids(r)

    # c) 提权 admin (直接 DB 改 role) → 重新登录 → time-travel 3 天 → 重新到期
    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email))
        user.role = "admin"
    admin_headers = await _register_login(client, email)  # 重新 login 拿 admin claim
    r = await client.post("/api/admin/time-travel", headers=admin_headers, json={"days": 3})
    assert r.status_code == 200, r.text
    assert abs(r.json()["offset_days"] - 3.0) < 0.01
    r = await client.get("/api/admin/time-travel", headers=admin_headers)
    assert abs(r.json()["offset_days"] - 3.0) < 0.01
    # D-A1 断言核心: FSRS 到期由虚拟时钟驱动, 3 天后该题重新出现
    r = await client.get("/api/reviews/today", headers=headers)
    assert e1_id in _queue_ids(r)

    # d) 再答对 (连对 2 次) → mastered, 时钟再前进也不入队
    r = await client.post(f"/api/reviews/{e1_id}/answer", headers=headers, json={"response": "A"})
    assert r.json()["state"] == "mastered"
    await client.post("/api/admin/time-travel", headers=admin_headers, json={"days": 30})
    r = await client.get("/api/reviews/today", headers=headers)
    assert e1_id not in _queue_ids(r)
    async with tenant_session(uid) as session:
        item = await session.get(ErrorItem, uuid.UUID(e1_id))
        assert item.state == "mastered" and item.fsrs_card_json["streak"] == 2

    # e) 答错路径: streak 清零 + BKT 下降 + mastery_log(evidence_type="review")
    async with tenant_session(uid) as session:
        e2 = ErrorItem(tenant_id=uid, user_id=uid, question_id=uuid.UUID(q1_id), kc_ids=[kc_id])
        session.add(e2)
        await session.flush()
        e2_id = str(e2.id)
        record = await session.scalar(
            select(MasteryRecord).where(
                MasteryRecord.user_id == uid, MasteryRecord.kc_id == uuid.UUID(kc_id)
            )
        )
        p_before = record.p_mastery
    r = await client.post(f"/api/reviews/{e2_id}/answer", headers=headers, json={"response": "B"})
    body = r.json()
    assert body["correct"] is False and body["state"] == "reviewing"
    assert body["answer"] == "A"
    assert body["explanation"] == "展开点为 0 的特例。"
    async with tenant_session(uid) as session:
        item = await session.get(ErrorItem, uuid.UUID(e2_id))
        assert item.fsrs_card_json["streak"] == 0
        record = await session.scalar(
            select(MasteryRecord).where(
                MasteryRecord.user_id == uid, MasteryRecord.kc_id == uuid.UUID(kc_id)
            )
        )
        assert record.p_mastery < p_before  # BKT 因答错下降
        logs = (
            await session.scalars(
                select(MasteryLog).where(
                    MasteryLog.user_id == uid, MasteryLog.evidence_type == "review"
                )
            )
        ).all()
        assert len(logs) == 3  # b/d/e 三次复习作答各一条审计
        assert any(entry.new_p < entry.old_p for entry in logs)

    # f) 变式重练: FakeGateway 返回合法题 → 新题落库 (quiz_id 沿用) + variant_of 指向原题
    old_gateway = app.state.gateway
    app.state.gateway = FakeReviewGateway()
    try:
        r = await client.post(f"/api/reviews/{e2_id}/variant", headers=headers)
    finally:
        app.state.gateway = old_gateway
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["variant_of"] == q1_id
    new_q_id = body["question"]["id"]
    assert new_q_id != q1_id and "answer" not in body["question"]
    async with tenant_session(uid) as session:
        new_q = await session.get(Question, uuid.UUID(new_q_id))
        assert new_q is not None and str(new_q.quiz_id) == quiz_id
        assert new_q.stem == _VARIANT_QUESTION["stem"]
        assert [str(k) for k in new_q.kc_ids] == [kc_id]  # 考点沿用原题归一化 kc_ids
        item = await session.get(ErrorItem, uuid.UUID(e2_id))
        assert str(item.variant_of) == q1_id  # 指向原题
        assert str(item.question_id) == new_q_id  # 后续复习答变式题

    await timectl.set_offset(redis, 0)  # 收尾: 不污染其他测试
