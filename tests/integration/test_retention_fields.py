"""P2-7 遗忘快照字段全链路 (写好不跑, 验证阶段统一 INTEGRATION=1 执行)。

经 HTTP 验证 profile 掌握度明细与 reviews 到期队列的 retention_now/retention_in_3d:
未复习卡 → null (不假装能预测) → 复习一次建立记忆模型 → 两点保持率非空且衰减
→ time-travel 前进 3 天 → "现在"的保持率 == 3 天前预测的 retention_in_3d
(Clock 驱动的确定性衰减, D-A1 同款断言) → 归档错题 → 画像侧保持率回落 null。
"""

import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services


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


def _mastery_row(resp, kc_id: str) -> dict:
    assert resp.status_code == 200, resp.text
    rows = [m for m in resp.json()["mastery"] if m["kc_id"] == kc_id]
    assert rows, f"KC {kc_id} 不在掌握度明细里"
    return rows[0]


def _queue_item(resp, error_item_id: str) -> dict:
    assert resp.status_code == 200, resp.text
    items = [it for it in resp.json()["items"] if it["error_item_id"] == error_item_id]
    assert items, f"错题 {error_item_id} 不在到期队列里"
    return items[0]


async def test_retention_fields_flow(client):
    from eduagents_api import timectl
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import (
        ErrorItem,
        KnowledgeBase,
        KnowledgeComponent,
        MasteryRecord,
        Question,
        Quiz,
        User,
    )

    redis = client._transport.app.state.redis
    await timectl.set_offset(redis, 0)  # 全局偏移归零, 保证可重复运行

    email = f"ret-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    headers = await _register_login(client, email)
    uid = uuid.UUID(await _uid(email))

    # 直接构造 KB/KC/Quiz/Question/ErrorItem (空 fsrs 卡 = 未复习, 视为到期);
    # 父表先 flush 再插子表 (UoW 不懂 FK 顺序)
    async with tenant_session(uid) as session:
        kb = KnowledgeBase(tenant_id=uid, name="高数", subject="math")
        session.add(kb)
        await session.flush()
        kc1 = KnowledgeComponent(tenant_id=uid, kb_id=kb.id, canonical_name="泰勒公式")
        kc2 = KnowledgeComponent(tenant_id=uid, kb_id=kb.id, canonical_name="洛必达法则")
        session.add_all([kc1, kc2])
        await session.flush()
        quiz = Quiz(tenant_id=uid, user_id=uid, title="遗忘快照测试卷", status="ready")
        session.add(quiz)
        await session.flush()
        q1 = Question(
            tenant_id=uid, quiz_id=quiz.id, type="single",
            stem="泰勒公式在 $x_0=0$ 处的特例称为?",
            options_json=["麦克劳林公式", "洛必达法则", "拉格朗日中值定理", "柯西公式"],
            answer="A", explanation="展开点为 0 的特例。", kc_ids=[str(kc1.id)],
        )
        session.add(q1)
        await session.flush()
        e1 = ErrorItem(tenant_id=uid, user_id=uid, question_id=q1.id, kc_ids=[str(kc1.id)])
        # kc2: 只有掌握度、无错题卡 → 画像侧 retention 恒为 null 的对照组
        rec2 = MasteryRecord(
            tenant_id=uid, user_id=uid, kc_id=kc2.id, p_mastery=0.5, confidence=0.5,
        )
        session.add_all([e1, rec2])
        await session.flush()
        kc1_id, kc2_id, e1_id = str(kc1.id), str(kc2.id), str(e1.id)

    # a) 未复习的空卡: 队列条目带字段但为 null (无 last_review/stability, 不假预测)
    r = await client.get("/api/reviews/today", headers=headers)
    item = _queue_item(r, e1_id)
    assert item["retention_now"] is None
    assert item["retention_in_3d"] is None

    # b) 答对一次 → FSRS 卡建立记忆模型; 画像侧该 KC 两点保持率非空且单调
    r = await client.post(f"/api/reviews/{e1_id}/answer", headers=headers, json={"response": "A"})
    assert r.status_code == 200, r.text
    r = await client.get("/api/profile", headers=headers)
    row = _mastery_row(r, kc1_id)
    assert row["retention_now"] == 1.0  # 刚复习完 (elapsed 0 天)
    assert row["retention_in_3d"] is not None
    assert 0.0 < row["retention_in_3d"] < row["retention_now"]
    ret_3d_predicted = row["retention_in_3d"]
    # 无错题卡的 KC: 字段存在但为 null
    row2 = _mastery_row(r, kc2_id)
    assert row2["retention_now"] is None
    assert row2["retention_in_3d"] is None

    # c) 提权 admin → time-travel 前进 3 天 → "现在"的保持率 == 3 天前的预测值
    #    (保持率由 Clock 驱动确定性衰减, 预测即所得)
    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email))
        user.role = "admin"
    admin_headers = await _register_login(client, email)
    r = await client.post("/api/admin/time-travel", headers=admin_headers, json={"days": 3})
    assert r.status_code == 200, r.text
    r = await client.get("/api/profile", headers=headers)
    row = _mastery_row(r, kc1_id)
    assert row["retention_now"] == ret_3d_predicted
    assert row["retention_in_3d"] < row["retention_now"]

    # d) 3 天后重新到期: 队列条目的两点保持率非空且继续衰减
    r = await client.get("/api/reviews/today", headers=headers)
    item = _queue_item(r, e1_id)
    assert item["retention_now"] == ret_3d_predicted
    assert item["retention_in_3d"] < item["retention_now"]

    # e) 归档 (用户显式退出复习) → 画像侧不再渲染"再不复习"提示 → 回落 null
    r = await client.post(f"/api/errors/{e1_id}/archive", headers=headers)
    assert r.status_code == 200, r.text
    r = await client.get("/api/profile", headers=headers)
    row = _mastery_row(r, kc1_id)
    assert row["retention_now"] is None
    assert row["retention_in_3d"] is None

    await timectl.set_offset(redis, 0)  # 收尾归零, 不污染同进程后续用例
