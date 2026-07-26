"""Wave1-包C: 辅导小结 → 画像联动 (F4→F8)。

FakeGateway 脚本驱动 tutor 图到 summarize, 断言:
- misconceptions 命中已有 KC 词表 (canonical/alias 归一化子串) 的 KC 被以
  correct=False、weight=0.5、evidence_type="tutor_summary" 写入 mastery_log;
- 词表外的误解如实丢弃, 不新建 KC;
- 同 thread 后续轮次不重复计分 (summarize 只在本轮 trace 末项时落库)。
"""

import json
import uuid

import pytest
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


async def test_tutor_summary_writes_mastery_with_tutor_evidence(client):
    _, uid, headers = await _register(client, "tut")
    ids = await _seed_kcs(uid)

    app = client._transport.app
    runtime = app.state.graphs
    assert runtime is not None, "集成环境应有 GraphRuntime"

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
    original = runtime._deps.gateway
    runtime._deps.gateway = fake
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

        changes = body["mastery_changes"]
        assert {c["kc_id"] for c in changes} == {ids["deriv"], ids["extremum"]}
        by_kc = {c["kc_id"]: c for c in changes}
        assert by_kc[ids["deriv"]]["name"] == "导数定义"
        assert by_kc[ids["extremum"]]["name"] == "极值判定"  # alias「极值」子串命中
        for c in changes:
            assert c["new_p"] < c["old_p"]  # 负向证据 -> 掌握度下调

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
        assert r.json()["mastery_changes"] == []
    finally:
        runtime._deps.gateway = original

    # DB 核对: 每个命中 KC 恰好 1 条 tutor_summary 流水, 权重 0.5, 无 evidence_id
    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryLog, MasteryRecord

    async with tenant_session(uid) as s:
        logs = (
            await s.scalars(
                select(MasteryLog).where(MasteryLog.evidence_type == "tutor_summary")
            )
        ).all()
        assert {str(x.kc_id) for x in logs} == {ids["deriv"], ids["extremum"]}
        assert len(logs) == 2
        for x in logs:
            assert x.weight == pytest.approx(0.5)
            assert x.evidence_id is None
        records = (await s.scalars(select(MasteryRecord))).all()
        # 词表外误解未新建 KC/记录: 恰好两条掌握度记录
        assert {str(x.kc_id) for x in records} == {ids["deriv"], ids["extremum"]}

    # 画像 API 可见辅导小结证据 (F8-A3 流水口径)
    r = await client.get("/api/profile", headers=headers)
    assert r.status_code == 200
    evidence_types = {c["evidence_type"] for c in r.json()["recent_changes"]}
    assert "tutor_summary" in evidence_types


async def test_tutor_summary_without_kc_match_no_mastery_rows(client):
    """词表为空 -> misconceptions 全部丢弃, 不产生任何画像写入。"""
    _, uid, headers = await _register(client, "tutn")

    app = client._transport.app
    runtime = app.state.graphs
    assert runtime is not None

    fake = FakeGateway(
        {
            "tutor_classify": [{"stuck_type": "concept", "asked_for_answer": False, "solved": True}],
            "tutor_summarize": [
                {"misconceptions": ["对超纲概念的误解"], "review_suggestions": ["复习基础"]}
            ],
        }
    )
    original = runtime._deps.gateway
    runtime._deps.gateway = fake
    try:
        r = await client.post(
            "/api/tutor",
            headers=headers,
            json={"problem": "任意题目", "message": "我明白了, 谢谢!"},
        )
    finally:
        runtime._deps.gateway = original
    assert r.status_code == 200, r.text
    assert r.json()["mastery_changes"] == []

    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryLog

    async with tenant_session(uid) as s:
        logs = (await s.scalars(select(MasteryLog))).all()
    assert logs == []
