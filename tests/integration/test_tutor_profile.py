"""辅导小结只形成可删除的 learner insight。

FakeGateway 脚本驱动 tutor 图到 summarize, 断言:
- misconceptions 命中已有 KC 词表时保留 KC 归因，但不写 mastery;
- 词表外的误解如实丢弃, 不新建 KC;
- 同 thread 后续轮次不重复计分 (summarize 只在本轮 trace 末项时落库)。
"""

import json
import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


class FakeGateway:
    """脚本化网关 (同 tests/unit/test_tutor_graph.py): 按 feature 排队出词, 末项粘性。"""

    def __init__(self, script: dict[str, list] | None = None):
        from eduagents_core.gateway.base import ChatRequest

        self.requests: list[ChatRequest] = []
        self._script = {k: list(v) for k, v in (script or {}).items()}

    async def chat(self, req):
        from eduagents_core.gateway.base import ChatResult

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
        uid = str(user.id)
    return email, uid, headers


async def _seed_kcs(uid: str) -> dict:
    """KC 词表: 导数定义 (alias: 导数的定义) / 极值判定 (alias: 极值); 均无既有掌握度记录。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeBase, KnowledgeComponent

    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=tid, name="高等数学", subject="math")
        s.add(kb)
        await s.flush()
        kc_deriv = KnowledgeComponent(
            tenant_id=tid, kb_id=kb.id, canonical_name="导数定义", aliases=["导数的定义"]
        )
        kc_extremum = KnowledgeComponent(
            tenant_id=tid, kb_id=kb.id, canonical_name="极值判定", aliases=["极值"]
        )
        s.add_all([kc_deriv, kc_extremum])
        await s.flush()
        return {"deriv": str(kc_deriv.id), "extremum": str(kc_extremum.id)}


async def test_tutor_summary_records_insight_without_mastery(client):
    _, uid, headers = await _register(client, "tut")
    ids = await _seed_kcs(uid)

    app = client._transport.app
    runtime = app.state.harness
    assert runtime is not None, "集成环境应有 HarnessRuntime"

    solved = {"stuck_type": "concept", "asked_for_answer": False, "solved": True}
    unsolved = {"stuck_type": "concept", "asked_for_answer": False, "solved": False}
    summary = {
        "misconceptions": [
            "把「导数定义」中的极限过程当成了直接代入求值",
            "以为导数为零的点一定是极值点",
            "对与词表无关的概念 XYZ 存在混淆",  # 词表外 -> 应被丢弃
        ],
        "review_suggestions": ["重做 3 道用定义求导的基础题"],
    }
    fake = FakeGateway(
        {
            "tutor_classify": [solved, unsolved],
            "tutor_respond": ["我们再看看这一步?"],
            "tutor_summarize": [summary],
        }
    )
    original = runtime.deps.gateway.gateway
    runtime.deps.gateway.gateway = fake
    try:
        # 第 1 轮: solved -> summarize -> 小结落画像
        r = await client.post(
            "/api/tutor",
            headers=headers,
            json={
                "problem": "用定义求 f(x)=x^2 在 x=1 处的导数",
                "message": "我懂了, 答案是 2, 谢谢老师!",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        thread_local_id = body["thread_local_id"]
        parsed = json.loads(body["session_summary"])
        assert parsed["misconceptions"] == summary["misconceptions"]

        insights = body["learner_insights"]
        assert {c["kc_id"] for c in insights} == {ids["deriv"], ids["extremum"]}

        # 第 2 轮 (同 thread, 未 solved): 不得重复计分
        r = await client.post(
            "/api/tutor",
            headers=headers,
            json={
                "problem": "用定义求 f(x)=x^2 在 x=1 处的导数",
                "message": "再帮我看一道类似的?",
                "thread_local_id": thread_local_id,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["learner_insights"] == []

        restored = await client.get(f"/api/tutor/{thread_local_id}", headers=headers)
        assert restored.status_code == 200, restored.text
        restored_body = restored.json()
        assert restored_body["problem"].startswith("用定义求")
        assert [message["role"] for message in restored_body["messages"]] == [
            "user",
            "user",
            "assistant",
        ]
        assert restored_body["messages"][-1]["content"] == "我们再看看这一步?"
        assert restored_body["pending"] is False
        assert json.loads(restored_body["session_summary"])["misconceptions"]
    finally:
        runtime.deps.gateway.gateway = original

    # DB 核对: 只有 insight，无 mastery 事实。
    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryLog, MasteryRecord

    async with tenant_session(uid) as s:
        logs = (await s.scalars(select(MasteryLog))).all()
        assert logs == []
        records = (await s.scalars(select(MasteryRecord))).all()
        assert records == []


async def test_tutor_summary_without_kc_match_no_mastery_rows(client):
    """词表为空 -> misconceptions 全部丢弃, 不产生任何画像写入。"""
    _, uid, headers = await _register(client, "tutn")

    app = client._transport.app
    runtime = app.state.harness
    assert runtime is not None

    fake = FakeGateway(
        {
            "tutor_classify": [{"stuck_type": "concept", "asked_for_answer": False, "solved": True}],
            "tutor_summarize": [
                {"misconceptions": ["对超纲概念的误解"], "review_suggestions": ["复习基础"]}
            ],
        }
    )
    original = runtime.deps.gateway.gateway
    runtime.deps.gateway.gateway = fake
    try:
        r = await client.post(
            "/api/tutor",
            headers=headers,
            json={"problem": "任意题目", "message": "我明白了, 谢谢!"},
        )
    finally:
        runtime.deps.gateway.gateway = original
    assert r.status_code == 200, r.text
    assert r.json()["learner_insights"] == []

    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryLog

    async with tenant_session(uid) as s:
        logs = (await s.scalars(select(MasteryLog))).all()
    assert logs == []
