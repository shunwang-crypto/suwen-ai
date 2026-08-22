"""可解释推荐 + 主动开场全链路集成测试 (Wave-P2 P2-2/P2-5, G13/F8-A2)。

- 造数: KC + 薄弱掌握度 + 长期记忆卡点 (learner_insights) + 到期错题 + 活跃计划;
- GET /api/next-actions: 断言 F8-A2 —— 响应含引用历史卡点的 resume_insight 条目
  (标题点名『导数定义』, 理由带学生原话), review 置顶且计数与到期错题一致,
  weak_practice 理由用定性词 (P2-7: 无掌握度小数), plan_task 收尾, 共 ≤5 条;
- 冷启动: 全新无资料用户返回空行动，让今日页展示上传/目标计划双入口;
- 本人隔离: 他人的记忆条目不会进我的开场卡。

真实 PG/Redis, stub 网关 (聚合本身零 LLM)。跑法:
INTEGRATION=1 pytest tests/integration/test_next_actions_flow.py -q
(需 make dev-infra + LD_LIBRARY_PATH=$HOME/miniforge3/envs/eduagents/lib)
"""

from __future__ import annotations

import re
import uuid

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"

_EVIDENCE = "为什么导数定义里不能直接把 x=1 代进去?"
_DECIMAL = re.compile(r"\d\.\d")  # 掌握度小数形如 0.35 (P2-7 理由脱敏红线)


async def _register_login(client, prefix="na"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _ctx(uid: uuid.UUID):
    from eduagents_api.deps import AuthContext

    return AuthContext(user_id=uid, tenant_id=uid, role="student", jti="")


async def _seed_learning_state(uid: uuid.UUID) -> str:
    """种齐四类数据源, 返回 kc_id:

    KC(导数定义) + 薄弱掌握度(p=0.35, zone_label 定性=待巩固段) + confused 记忆条目(学生原话)
    + 1 道到期错题 (空 FSRS 卡视为到期, 同 reviews.today_queue 口径)
    + 1 份含今日未完成任务的计划。父表先 flush 再插子表 (UoW 不懂 FK 顺序)。
    """
    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        ErrorItem,
        KnowledgeBase,
        KnowledgeComponent,
        MasteryLog,
        MasteryRecord,
        Plan,
        Question,
        Quiz,
    )
    from eduagents_api.services import persist_learner_insights

    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=uid, name="高等数学", subject="math")
        s.add(kb)
        await s.flush()
        kc = KnowledgeComponent(
            tenant_id=uid, kb_id=kb.id, canonical_name="导数定义", aliases=[]
        )
        quiz = Quiz(tenant_id=uid, user_id=uid, title="摸底")
        s.add(kc)
        s.add(quiz)
        await s.flush()
        s.add(
            MasteryRecord(
                tenant_id=uid, user_id=uid, kc_id=kc.id, p_mastery=0.35, confidence=0.5
            )
        )
        for _ in range(3):
            s.add(
                MasteryLog(
                    tenant_id=uid,
                    user_id=uid,
                    kc_id=kc.id,
                    old_p=0.35,
                    new_p=0.35,
                    evidence_type="quiz_attempt",
                    evidence_id=uuid.uuid4(),
                    weight=1.0,
                )
            )
        question = Question(
            tenant_id=uid,
            quiz_id=quiz.id,
            type="single",
            stem="f(x)=x^2 在 x=1 处的导数是?",
            options_json=["A. 1", "B. 2", "C. 0", "D. x"],
            answer="B",
            kc_ids=[str(kc.id)],
        )
        s.add(question)
        await s.flush()
        # 空 fsrs_card_json = 到期 (reviews.today_queue "空卡视为到期" 口径)
        s.add(
            ErrorItem(
                tenant_id=uid,
                user_id=uid,
                question_id=question.id,
                kc_ids=[str(kc.id)],
                state="new",
                fsrs_card_json={},
            )
        )
        # 活跃计划: 创建当天 = 第 1 天, day1 有 1 个未完成任务
        s.add(
            Plan(
                tenant_id=uid,
                user_id=uid,
                goal="期末高数 90 分",
                budget_hours=1.0,
                plan_mode="goal_only",
                grounding_status="unreferenced",
                structure_json={
                    "stages": [
                        {
                            "name": "打地基",
                            "days": [
                                {
                                    "day": 1,
                                    "tasks": [
                                        {
                                            "task_id": str(uuid.uuid4()),
                                            "title": "复习导数定义",
                                            "minutes": 30,
                                            "status": "pending",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            )
        )
        # 历史卡点走唯一写入口 (P2-1): 学生原话 + KC 归因
        await persist_learner_insights(
            s,
            _ctx(uid),
            [{"kc_id": str(kc.id), "kind": "confused", "text": _EVIDENCE}],
            source="chat_reflection",
        )
        return str(kc.id)


# ---- F8-A2: 开场推荐引用历史卡点 + 四类聚合 + 优先级 ----


async def test_next_actions_reference_history_stuck_point(client):
    user_id, headers = await _register_login(client, "na-full")
    uid = uuid.UUID(user_id)
    await _seed_learning_state(uid)

    r = await client.get("/api/next-actions", headers=headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert 0 < len(items) <= 5
    by_type = {it["type"]: it for it in items}

    # F8-A2 核心断言: 存在引用历史卡点的条目 —— 点名 KC + 带学生原话的理由
    resume = by_type["resume_insight"]
    assert "上次你在『导数定义』卡住了" in resume["title"]
    assert _EVIDENCE in resume["reason"]  # 记忆条目的原话原样回给本人
    assert resume["href"].startswith("/chat?q=")  # confused -> 回对话续问

    # review 置顶且计数与到期错题一致 (FSRS 口径, 空卡到期)
    assert items[0]["type"] == "review"
    assert "1 道错题到期" in items[0]["title"]
    assert items[0]["href"] == "/reviews"

    # weak_practice: 理由 = 定性词 (thresholds.zone_label: 0.3<=p<0.6 -> "待巩固"), 不出现掌握度小数
    weak = by_type["weak_practice"]
    assert "导数定义" in weak["title"]
    assert "待巩固" in weak["reason"]
    assert weak["href"] == "/quiz?weak=1"

    # plan_task 收尾
    plan = by_type["plan_task"]
    assert "复习导数定义" in plan["title"]
    assert "期末高数 90 分" in plan["reason"] and "第 1 天" in plan["reason"]
    assert "1 个任务没完成" in plan["reason"]
    assert "plan_id=" in plan["href"] and "task_id=" in plan["href"]

    # 优先级全序 + P2-7 脱敏: 任何 title/reason 无小数
    types = [it["type"] for it in items]
    assert types == ["review", "resume_insight", "weak_practice", "plan_task"]
    for it in items:
        assert not _DECIMAL.search(it["title"]), it
        assert not _DECIMAL.search(it["reason"]), it


# ---- 冷启动兜底 ----


async def test_no_material_returns_no_action(client):
    _user_id, headers = await _register_login(client, "na-cold")
    r = await client.get("/api/next-actions", headers=headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert items == []


# ---- 本人隔离: 他人卡点不进我的开场卡 ----


async def test_other_users_insights_not_leaked(client):
    owner_id, _owner_headers = await _register_login(client, "na-own")
    await _seed_learning_state(uuid.UUID(owner_id))
    _other_id, other_headers = await _register_login(client, "na-other")

    r = await client.get("/api/next-actions", headers=other_headers)
    assert r.status_code == 200
    items = r.json()["items"]
    # 他人没有自己的资料时只该看到空行动, 绝不该看到 owner 的卡点/错题/计划
    assert items == []
    assert _EVIDENCE not in str(items)
