"""F3 计划活化 + F6 完整错题本 全链路集成测试。

覆盖:
① 建计划 → toggle 任务 → 详情 done/进度正确;
② 造低掌握度 KC → POST adjust 插入补强任务 + plan_adjustments 留痕 + 历史可查;
③ 低分测评 (正确率 <0.6) 自动触发调整;
④ 错题列表按 state/cause/kc_id 筛选 + 分页 + 聚合统计;
⑤ archive 后退出 today 复习队列;
⑥ 错因修正持久化 + 复习答错时错因自动分类 (fake 网关; stub 网关不分类)。
"""

import json
import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services


class FakeCauseGateway:
    """错因分类返回 misread; 其余 json 请求返回低分兜底 (避免真实 LLM)。"""

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        if req.feature == "error_cause":
            payload = {"cause": "misread", "reason": "看错了条件"}
        else:
            payload = {"score": 0.1, "reason": "fake"}
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


def _structure(day_count: int = 3) -> dict:
    """两阶段计划: 阶段一 day_count 天 (每天 1 任务), 阶段二 1 天 (2 个任务)。"""
    return {
        "stages": [
            {
                "name": "打基础",
                "days": [
                    {
                        "day": d,
                        "tasks": [
                            {
                                "title": f"第 {d} 天: 极限概念",
                                "minutes": 60,
                                "kc_names": ["极限"],
                                "resource_ref": None,
                            }
                        ],
                    }
                    for d in range(1, day_count + 1)
                ],
            },
            {
                "name": "冲刺",
                "days": [
                    {
                        "day": day_count + 1,
                        "tasks": [
                            {
                                "title": "综合复习",
                                "minutes": 40,
                                "kc_names": [],
                                "resource_ref": None,
                            },
                            {
                                "title": "自测",
                                "minutes": 20,
                                "kc_names": [],
                                "resource_ref": None,
                            },
                        ],
                    }
                ],
            },
        ]
    }


async def _register_login(client, email: str) -> dict:
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _uid(email: str) -> uuid.UUID:
    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email))
        return user.id


def _all_tasks(structure: dict) -> list[dict]:
    return [t for st in structure["stages"] for d in st["days"] for t in d["tasks"]]


async def test_plan_alive_and_error_book(client):
    from eduagents_api import timectl
    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        ErrorItem,
        KnowledgeBase,
        KnowledgeComponent,
        MasteryRecord,
        Plan,
        PlanAdjustment,
        Question,
        Quiz,
    )

    app = client._transport.app
    await timectl.set_offset(app.state.redis, 0)  # 与其他集成测试共享 redis, 归零

    email = f"plan-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    headers = await _register_login(client, email)
    uid = await _uid(email)

    # ---- 造数据: KB / 两个 KC / 计划 / 一份测评卷
    async with tenant_session(uid) as session:
        kb = KnowledgeBase(tenant_id=uid, name="高数", subject="math")
        session.add(kb)
        await session.flush()
        kc_weak = KnowledgeComponent(tenant_id=uid, kb_id=kb.id, canonical_name="洛必达法则")
        kc_quiz = KnowledgeComponent(tenant_id=uid, kb_id=kb.id, canonical_name="定积分")
        session.add_all([kc_weak, kc_quiz])
        await session.flush()
        plan = Plan(
            tenant_id=uid,
            user_id=uid,
            goal="两周掌握微积分基础",
            budget_hours=1.5,
            structure_json=_structure(),
        )
        session.add(plan)
        quiz = Quiz(tenant_id=uid, user_id=uid, title="低分测评卷", status="ready")
        session.add(quiz)
        await session.flush()
        q_quiz = Question(
            tenant_id=uid,
            quiz_id=quiz.id,
            type="single",
            stem="$\\int_0^1 x\\,dx$ 的值是?",
            options_json=["1/2", "1", "2", "0"],
            answer="A",
            explanation="牛顿-莱布尼茨公式。",
            kc_ids=[str(kc_quiz.id)],
        )
        session.add(q_quiz)
        await session.flush()
        plan_id, quiz_id = str(plan.id), str(quiz.id)
        kc_weak_id, kc_quiz_id = str(kc_weak.id), str(kc_quiz.id)

    # ---- ① 计划详情 + 任务勾选 --------------------------------------------------
    r = await client.get(f"/api/plans/{plan_id}", headers=headers)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["progress"] == {
        "total": 5,
        "done": 0,
        "skipped": 0,
        "pending": 5,
        "ratio": 0.0,
        "effective_ratio": 0.0,
    }
    tasks = _all_tasks(detail["structure"])
    assert all(t["done"] is False for t in tasks)
    # task_key = 三层 0 基下标
    keys = [t["task_key"] for t in tasks]
    assert keys == [
        "stage0.day0.task0",
        "stage0.day1.task0",
        "stage0.day2.task0",
        "stage1.day0.task0",
        "stage1.day0.task1",
    ]

    r = await client.post(
        f"/api/plans/{plan_id}/tasks/stage0.day1.task0/toggle", headers=headers, json={}
    )
    assert r.status_code == 200, r.text
    assert r.json()["done"] is True and r.json()["progress"]["done"] == 1

    r = await client.get(f"/api/plans/{plan_id}", headers=headers)
    detail = r.json()
    assert detail["progress"]["done"] == 1
    done_flags = [t["done"] for t in _all_tasks(detail["structure"])]
    assert done_flags == [False, True, False, False, False]

    # 幂等置位 + 取反回滚
    r = await client.post(
        f"/api/plans/{plan_id}/tasks/stage0.day1.task0/toggle",
        headers=headers,
        json={"done": True},
    )
    assert r.json()["done"] is True
    r = await client.post(
        f"/api/plans/{plan_id}/tasks/stage0.day2.task0/toggle",
        headers=headers,
        json={"status": "skipped"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "skipped"
    assert r.json()["done"] is False
    assert r.json()["progress"]["skipped"] == 1
    assert r.json()["progress"]["pending"] == 3
    r = await client.post(
        f"/api/plans/{plan_id}/tasks/stage0.day1.task0/toggle", headers=headers, json={}
    )
    assert r.json()["done"] is False and r.json()["progress"]["done"] == 0
    # 不存在的 task_key → 404
    r = await client.post(
        f"/api/plans/{plan_id}/tasks/stage9.day9.task9/toggle", headers=headers, json={}
    )
    assert r.status_code == 404

    # ---- ② 低掌握度 KC → adjust 插入补强任务 -------------------------------------
    r = await client.post(f"/api/plans/{plan_id}/adjust", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == [] and "无需调整" in body["reason"]  # 尚无薄弱 KC

    async with tenant_session(uid) as session:
        session.add(
            MasteryRecord(
                tenant_id=uid,
                user_id=uid,
                kc_id=uuid.UUID(kc_weak_id),
                p_mastery=0.15,
                confidence=0.8,
            )
        )

    r = await client.post(f"/api/plans/{plan_id}/adjust", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["inserted"]) == 1
    ins = body["inserted"][0]
    assert ins["title"] == "补强: 洛必达法则"
    assert ins["minutes"] == 30 and ins["kc_names"] == ["洛必达法则"]
    assert ins["kc_id"] == kc_weak_id
    assert "洛必达法则" in body["reason"] and "补强任务" in body["reason"]

    # 补强任务真的进了计划结构 (且落在第一个未完成日)
    r = await client.get(f"/api/plans/{plan_id}", headers=headers)
    detail = r.json()
    assert detail["progress"]["total"] == 6
    inserted_task = next(
        t for t in _all_tasks(detail["structure"]) if t["title"] == "补强: 洛必达法则"
    )
    assert inserted_task["done"] is False and inserted_task["source"] == "adjust"
    assert inserted_task["task_key"] == ins["task_key"] == "stage0.day0.task1"

    # 幂等: 该 KC 已被任务覆盖 → 不重复插
    r = await client.post(f"/api/plans/{plan_id}/adjust", headers=headers)
    assert r.json()["inserted"] == []

    # plan_adjustments 有且仅有一条 (无插入不留噪声审计)
    async with tenant_session(uid) as session:
        rows = (
            await session.scalars(
                select(PlanAdjustment).where(PlanAdjustment.plan_id == uuid.UUID(plan_id))
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].diff_json["trigger"] == "manual"
        assert rows[0].diff_json["inserted"][0]["kc_id"] == kc_weak_id

    r = await client.get(f"/api/plans/{plan_id}/adjustments", headers=headers)
    assert r.status_code == 200, r.text
    history = r.json()
    assert len(history) == 1 and history[0]["trigger"] == "manual"
    assert len(history[0]["inserted"]) == 1

    # ---- ③ 低分测评自动触发调整 --------------------------------------------------
    r = await client.post(
        f"/api/quiz/{quiz_id}/attempts",
        headers=headers,
        json={"answers": [{"question_id": str(q_quiz.id), "response": "C"}]},
    )
    assert r.status_code == 200, r.text
    attempt_body = r.json()
    assert attempt_body["correct"] == 0 and attempt_body["total"] == 1
    auto = attempt_body["plan_adjustment"]
    assert auto is not None, "低分测评应自动触发计划调整"
    assert auto["plan_id"] == plan_id
    assert len(auto["inserted"]) == 1
    assert auto["inserted"][0]["kc_id"] == kc_quiz_id
    assert auto["inserted"][0]["title"] == "补强: 定积分"
    assert "正确率" in auto["reason"] and "低于 60%" in auto["reason"]

    r = await client.get(f"/api/plans/{plan_id}/adjustments", headers=headers)
    history = r.json()
    assert len(history) == 2  # 时间倒序: 自动触发在前
    assert history[0]["trigger"] == "low_score_attempt"

    # ---- ④ 错题本列表 / 筛选 / 统计 ---------------------------------------------
    # 低分测评已自动入本一条 (kc_quiz, cause=knowledge_gap, state=new)
    async with tenant_session(uid) as session:
        q2 = Question(
            tenant_id=uid,
            quiz_id=uuid.UUID(quiz_id),
            type="single",
            stem="洛必达法则的使用前提是?",
            options_json=["0/0 或 ∞/∞", "任意极限", "连续", "可积"],
            answer="A",
            explanation="未定式才可用。",
            kc_ids=[kc_weak_id],
        )
        session.add(q2)
        await session.flush()
        e_mastered = ErrorItem(
            tenant_id=uid,
            user_id=uid,
            question_id=q2.id,
            kc_ids=[kc_weak_id],
            cause="calc_slip",
            state="mastered",
        )
        e_active = ErrorItem(
            tenant_id=uid,
            user_id=uid,
            question_id=q2.id,
            kc_ids=[kc_weak_id],
            cause="concept_confusion",
            state="new",
        )
        session.add_all([e_mastered, e_active])
        await session.flush()
        e_mastered_id, e_active_id, q2_id = str(e_mastered.id), str(e_active.id), str(q2.id)

    r = await client.get("/api/errors", headers=headers)
    assert r.status_code == 200, r.text
    listing = r.json()
    assert listing["total"] == 3 and len(listing["items"]) == 3
    quiz_item = next(i for i in listing["items"] if i["question"]["id"] == str(q_quiz.id))
    assert quiz_item["question"]["answer"] == "A"  # 错题本给出正确答案与解析
    assert quiz_item["question"]["explanation"] == "牛顿-莱布尼茨公式。"
    assert quiz_item["my_answer"] == "C" and quiz_item["error_count"] == 1
    assert quiz_item["kc_names"] == ["定积分"]
    assert quiz_item["next_due"] is not None

    # 按状态筛选
    r = await client.get("/api/errors?state=mastered", headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [e_mastered_id]
    r = await client.get("/api/errors?state=new", headers=headers)
    assert sorted(i["id"] for i in r.json()["items"]) == sorted([e_active_id, quiz_item["id"]])
    # state=active 别名 (new|reviewing) 对应前端"待复习"统计卡
    r = await client.get("/api/errors?state=active", headers=headers)
    assert sorted(i["id"] for i in r.json()["items"]) == sorted([e_active_id, quiz_item["id"]])
    # 按错因筛选
    r = await client.get("/api/errors?cause=concept_confusion", headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [e_active_id]
    r = await client.get("/api/errors?cause=knowledge_gap", headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [quiz_item["id"]]
    # 按 KC 筛选 + 组合筛选
    r = await client.get(f"/api/errors?kc_id={kc_weak_id}", headers=headers)
    assert sorted(i["id"] for i in r.json()["items"]) == sorted([e_mastered_id, e_active_id])
    r = await client.get(f"/api/errors?kc_id={kc_weak_id}&state=mastered", headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [e_mastered_id]
    # 非法枚举 → 422
    assert (await client.get("/api/errors?state=zzz", headers=headers)).status_code == 422
    assert (await client.get("/api/errors?cause=zzz", headers=headers)).status_code == 422
    # 分页
    r = await client.get("/api/errors?page=1&page_size=2", headers=headers)
    page1 = r.json()
    assert len(page1["items"]) == 2 and page1["total"] == 3 and page1["pages"] == 2
    r = await client.get("/api/errors?page=2&page_size=2", headers=headers)
    assert len(r.json()["items"]) == 1

    r = await client.get("/api/errors/stats", headers=headers)
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["total"] == 3
    assert stats["by_cause"]["calc_slip"] == 1
    assert stats["by_cause"]["concept_confusion"] == 1
    assert stats["by_cause"]["knowledge_gap"] == 1
    assert stats["by_cause"]["misread"] == 0  # 五分类补零
    assert stats["by_state"] == {"new": 2, "reviewing": 0, "mastered": 1, "archived": 0}
    by_kc = {row["kc_id"]: row for row in stats["by_kc"]}
    assert by_kc[kc_weak_id]["count"] == 2 and by_kc[kc_weak_id]["name"] == "洛必达法则"
    assert by_kc[kc_quiz_id]["count"] == 1

    # ---- ⑤ 归档 → 退出 today 队列 ------------------------------------------------
    r = await client.get("/api/reviews/today", headers=headers)
    assert e_active_id in [i["error_item_id"] for i in r.json()["items"]]
    r = await client.post(f"/api/errors/{e_active_id}/archive", headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "archived"
    r = await client.get("/api/reviews/today", headers=headers)
    assert e_active_id not in [i["error_item_id"] for i in r.json()["items"]]
    r = await client.get("/api/errors?state=archived", headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [e_active_id]

    # 已归档错题可恢复，原卡片/历史不重建，重新进入活跃复习状态。
    r = await client.post(f"/api/errors/{e_active_id}/restore", headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "reviewing"
    r = await client.get("/api/reviews/today", headers=headers)
    assert e_active_id in [i["error_item_id"] for i in r.json()["items"]]

    # ---- ⑥ 错因修正持久化 + 复习答错自动分类 --------------------------------------
    r = await client.post(
        f"/api/errors/{e_active_id}/cause", headers=headers, json={"cause": "formula_error"}
    )
    assert r.status_code == 200 and r.json()["cause"] == "formula_error"
    r = await client.get("/api/errors?cause=formula_error", headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [e_active_id]
    async with tenant_session(uid) as session:
        assert (await session.get(ErrorItem, uuid.UUID(e_active_id))).cause == "formula_error"
    # 非法错因 → 422 且不改库
    r = await client.post(
        f"/api/errors/{e_active_id}/cause", headers=headers, json={"cause": "hallucination"}
    )
    assert r.status_code == 422
    async with tenant_session(uid) as session:
        assert (await session.get(ErrorItem, uuid.UUID(e_active_id))).cause == "formula_error"

    # 复习答错: stub 网关不分类 (保守保留入本初值 knowledge_gap)
    async with tenant_session(uid) as session:
        e_stub = ErrorItem(
            tenant_id=uid, user_id=uid, question_id=uuid.UUID(q2_id), kc_ids=[kc_weak_id]
        )
        e_fake = ErrorItem(
            tenant_id=uid, user_id=uid, question_id=uuid.UUID(q2_id), kc_ids=[kc_weak_id]
        )
        session.add_all([e_stub, e_fake])
        await session.flush()
        e_stub_id, e_fake_id = str(e_stub.id), str(e_fake.id)

    r = await client.post(
        f"/api/reviews/{e_stub_id}/answer", headers=headers, json={"response": "B"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["correct"] is False and r.json()["cause"] == "knowledge_gap"

    old_gateway = app.state.gateway
    app.state.gateway = FakeCauseGateway()
    try:
        r = await client.post(
            f"/api/reviews/{e_fake_id}/answer", headers=headers, json={"response": "D"}
        )
    finally:
        app.state.gateway = old_gateway
    assert r.status_code == 200, r.text
    assert r.json()["correct"] is False and r.json()["cause"] == "misread"
    async with tenant_session(uid) as session:
        assert (await session.get(ErrorItem, uuid.UUID(e_fake_id))).cause == "misread"

    # 答对不改错因 (分类只在答错时触发)
    async with tenant_session(uid) as session:
        item = await session.get(ErrorItem, uuid.UUID(e_fake_id))
        item.cause = "calc_slip"
    app.state.gateway = FakeCauseGateway()
    try:
        r = await client.post(
            f"/api/reviews/{e_fake_id}/answer", headers=headers, json={"response": "A"}
        )
    finally:
        app.state.gateway = old_gateway
    assert r.json()["correct"] is True and r.json()["cause"] == "calc_slip"
