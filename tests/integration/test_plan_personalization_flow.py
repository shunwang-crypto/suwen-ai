"""计划个性化 (P1-3) 与 tutor 画像注入 (P1-4) 端到端集成验收。

① POST /api/plans: 路由层预查 mastery_records / kc_edges 注入图 —
   plan_diagnose 的 LLM prompt 不含有真实记录的 KC (只兜底无记录主题),
   plan_draft prompt 的 DIAGNOSIS 携带真实 BKT 值、KC_ORDER 为先修拓扑序;
② POST /api/tutor: respond 的 system prompt 实收脱敏画像段, 开场轮题目命中
   薄弱 KC 时主动点明关联, 且 F4-A1 结构性禁答令仍压轴不受画像影响;
③ 无画像用户: tutor prompt 不含画像段, 占位符不残留。

FakeGateway 脚本化 (swap runtime._deps.gateway, 同 test_tutor_profile.py);
PG/Redis 为真, 全程零真实 LLM。
"""

import json
import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"

BAN = "绝对禁止给出最终答案"  # F4-A1 结构性禁令 (代码注入 system prompt)
OPENING_MARK = "这正是你之前卡过的"


class FakeGateway:
    """脚本化网关: 按 feature 排队出词 (末项粘性), 记录全部请求供 prompt 断言。"""

    def __init__(self, script: dict[str, list] | None = None):
        self.requests = []
        self._script = {k: list(v) for k, v in (script or {}).items()}

    def prompts(self, feature: str) -> list[str]:
        return [
            "\n".join(m.content for m in r.messages) for r in self.requests if r.feature == feature
        ]

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        self.requests.append(req)
        queue = self._script.get(req.feature, [])
        if not queue:
            payload = "{}"
        elif len(queue) == 1:
            payload = queue[0]
        else:
            payload = queue.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ChatResult(
            text=text,
            model="fake",
            tier=str(req.tier),
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="replay",
        )


async def _register(client, prefix: str):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email))
    return str(user.id), headers


async def _seed_mastery_with_dag(uid: str) -> dict:
    """三个 KC + 真实掌握度 + 先修链 导数定义 -> 极值判定 -> 泰勒公式。

    掌握度: 导数定义 0.24 (较弱) / 极值判定 0.31 (待巩固) / 泰勒公式 0.92 (已掌握)。
    """
    from datetime import UTC, datetime

    from eduagents_api.db import tenant_session
    from eduagents_api.models import KcEdge, KnowledgeBase, KnowledgeComponent, MasteryRecord

    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=tid, name="高等数学", subject="math")
        s.add(kb)
        await s.flush()
        kcs = {}
        for key, name in (("deriv", "导数定义"), ("extremum", "极值判定"), ("taylor", "泰勒公式")):
            kc = KnowledgeComponent(tenant_id=tid, kb_id=kb.id, canonical_name=name, aliases=[])
            s.add(kc)
            await s.flush()
            kcs[key] = kc
        for key, p in (("deriv", 0.24), ("extremum", 0.31), ("taylor", 0.92)):
            s.add(
                MasteryRecord(
                    tenant_id=tid,
                    user_id=tid,
                    kc_id=kcs[key].id,
                    p_mastery=p,
                    confidence=0.6,
                    last_seen=datetime.now(UTC),
                )
            )
        s.add_all(
            [
                KcEdge(
                    tenant_id=tid,
                    from_kc=kcs["deriv"].id,
                    to_kc=kcs["extremum"].id,
                    type="prerequisite",
                ),
                KcEdge(
                    tenant_id=tid,
                    from_kc=kcs["extremum"].id,
                    to_kc=kcs["taylor"].id,
                    type="prerequisite",
                ),
            ]
        )
    return {"kb_id": str(kb.id), **{k: str(v.id) for k, v in kcs.items()}}


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


# ---------- ① 计划创建: 真实掌握度 + 先修拓扑序 (G1/G2) ----------


async def test_plan_create_uses_real_mastery_and_dag_order(client):
    uid, headers = await _register(client, "plan-pers")
    await _seed_mastery_with_dag(uid)

    app = client._transport.app
    runtime = app.state.graphs
    assert runtime is not None, "集成环境应有 GraphRuntime"
    fake = FakeGateway(
        {
            # 一个主题有真实记录 (导数定义), 一个无记录 (级数) -> LLM 只该摸底后者
            "plan_parse_goal": [
                {
                    "subject": "高等数学",
                    "key_topics": ["导数定义", "级数"],
                    "level_hint": "",
                    "skills": [
                        {
                            "name": "导数定义",
                            "required_level": 3,
                            "reason": "是微分推理的基础",
                        },
                        {"name": "级数", "required_level": 2, "reason": "完成目标所需"},
                    ],
                }
            ],
            "plan_diagnose": [{"diagnosis": {"级数": 0.3}}],
            "plan_draft": [
                {
                    "stages": [
                        {
                            "name": "基础",
                            "days": [
                                {
                                    "day": 1,
                                    "tasks": [
                                        {
                                            "title": "学习导数定义",
                                            "minutes": 60,
                                            "kc_names": ["导数定义"],
                                            "resource_ref": None,
                                        }
                                    ],
                                },
                                {
                                    "day": 2,
                                    "tasks": [
                                        {
                                            "title": "学习级数",
                                            "minutes": 60,
                                            "kc_names": ["级数"],
                                            "resource_ref": None,
                                        }
                                    ],
                                },
                            ],
                        }
                    ]
                }
            ],
        }
    )
    original = runtime._deps.gateway
    runtime._deps.gateway = fake
    try:
        events = await _sse(
            client,
            "POST",
            "/api/plans",
            headers,
            {"goal": "两周补齐微积分薄弱点", "budget_hours_per_day": 1.0, "total_days": 2},
        )
    finally:
        runtime._deps.gateway = original

    # 计划流走到 interrupt 确认 (F3), 计划已生成
    assert events.get("interrupt"), events
    assert events["interrupt"][0]["kind"] == "plan_confirm"
    assert events["interrupt"][0]["plan"]["stages"]

    # G1: LLM 摸底只对无记录主题兜底 — prompt 里没有任何有真实记录的 KC
    diag_prompts = fake.prompts("plan_diagnose")
    assert len(diag_prompts) == 1
    assert "级数" in diag_prompts[0]
    for known in ("导数定义", "极值判定", "泰勒公式"):
        assert known not in diag_prompts[0], f"有真实记录的 {known} 不该再让 LLM 猜"

    # 真实 BKT 进 draft 的 DIAGNOSIS, 但只以定性档出境 (P2-7 脱敏: 精确概率不发第三方 LLM)
    draft_prompt = fake.prompts("plan_draft")[0]
    assert "导数定义: 较弱" in draft_prompt and "泰勒公式: 已掌握" in draft_prompt
    for decimal in ("0.24", "0.31", "0.92"):
        assert decimal not in draft_prompt, "精确掌握度小数不得出境第三方 LLM"
    assert "级数" in draft_prompt

    # 目标技能 ∩ weak_point_kcs 的定性差距独立进 prompt；目标外薄弱 KC
    # (极值判定) 不得混入。区段中不含精确概率或内部 ID。
    gap_section = draft_prompt.split("技能差距（目标要求与当前定性状态）>", 1)[1].split(
        "</技能差距", 1
    )[0]
    assert "导数定义：目标灵活迁移，当前较弱" in gap_section
    assert "极值判定" not in gap_section and "泰勒公式" not in gap_section
    assert all(decimal not in gap_section for decimal in ("0.24", "0.31", "0.92"))

    # G2: 先修 DAG 拓扑序注入 KC_ORDER (kc_edges -> dag.topological_order)
    assert "导数定义 -> 极值判定 -> 泰勒公式" in draft_prompt


# ---------- ② tutor 画像注入 + 开场关联卡点 (G9) ----------


async def test_tutor_prompt_receives_profile_with_opening_weak_kc(client):
    uid, headers = await _register(client, "tutor-pers")
    await _seed_mastery_with_dag(uid)

    app = client._transport.app
    runtime = app.state.graphs
    unsolved = {"stuck_type": "concept", "asked_for_answer": False, "solved": False}
    fake = FakeGateway({"tutor_classify": [unsolved], "tutor_respond": ["我们先从定义本身说起?"]})
    original = runtime._deps.gateway
    runtime._deps.gateway = fake
    try:
        r = await client.post(
            "/api/tutor",
            headers=headers,
            json={
                "problem": "用导数定义求 f(x)=x^2 在 x=1 处的导数",
                "message": "这道题我不会",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["hint_level"] == 0

        # 第 2 轮 (同 thread): 开场关联指令只在开场轮出现
        r = await client.post(
            "/api/tutor",
            headers=headers,
            json={
                "problem": "用导数定义求 f(x)=x^2 在 x=1 处的导数",
                "message": "还是没思路",
                "thread_local_id": body["thread_local_id"],
            },
        )
        assert r.status_code == 200, r.text
    finally:
        runtime._deps.gateway = original

    systems = fake.prompts("tutor_respond")
    assert len(systems) == 2
    # 画像段实收 (P2-7 脱敏: 定性词 + KC 名, 无掌握度小数)
    assert "学生画像" in systems[0]
    assert "导数定义" in systems[0] and "薄弱点" in systems[0]
    assert "0.24" not in systems[0]
    # 开场轮命中薄弱 KC -> 主动点明; 第 2 轮不再重复
    assert OPENING_MARK in systems[0]
    assert OPENING_MARK not in systems[1]
    # F4-A1 不受画像影响: 禁答令仍在且压轴 (位于画像段之后)
    for system in systems:
        assert BAN in system
        assert system.rfind(BAN) > system.rfind("学生画像")


async def test_tutor_without_profile_has_no_section(client):
    _uid, headers = await _register(client, "tutor-nopf")

    app = client._transport.app
    runtime = app.state.graphs
    unsolved = {"stuck_type": "unknown", "asked_for_answer": False, "solved": False}
    fake = FakeGateway({"tutor_classify": [unsolved], "tutor_respond": ["说说你的思路?"]})
    original = runtime._deps.gateway
    runtime._deps.gateway = fake
    try:
        r = await client.post(
            "/api/tutor",
            headers=headers,
            json={"problem": "求 1+2+...+100 的和", "message": "不会做"},
        )
        assert r.status_code == 200, r.text
    finally:
        runtime._deps.gateway = original

    system = fake.prompts("tutor_respond")[0]
    assert "学生画像" not in system
    assert "{{PROFILE_SECTION}}" not in system
    assert BAN in system
