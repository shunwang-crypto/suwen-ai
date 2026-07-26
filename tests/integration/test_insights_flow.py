"""学习者长期记忆全链路集成测试 (Wave-P2 P2-1, G10)。

- reflect_session (fake gateway) -> learner_insights 落学生原话, 重复入队不重复落;
- apply_tutor_summary -> misconception 文本落库 (命中词表带 kc_id, 词表外带 None);
- GET /api/profile/insights (join KC 名) + DELETE 本人条目 (用户纠正权);
- 本人隔离: 他人看不到、删不动 (RLS + user_id 双保险, 404 不泄露存在性);
- 每用户 100 条上限: 超额裁最旧;
- 导出 (PIPL 可携带权): ZIP 含 learner_insights/user_preferences 两表 JSON。

真实 PG/Redis, stub 网关 (LLM 一律 fake)。注销级联清空已并入
test_lifecycle.test_account_deletion_wipes_all_tables (models_zero 含两新表)。
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"

_EVIDENCE = "为什么导数定义里不能直接把 x=1 代进去?"
_PAYLOAD = {
    "insights": [{"kc_name": "导数定义", "signal": "confused", "evidence": _EVIDENCE}]
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


async def _register_login(client, prefix="ins"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _seed_kc_and_chat(uid: uuid.UUID):
    """种一个 KB + KC(导数定义) + 含学生原话的会话, 返回 (kc_id, session_id)。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        ChatMessage,
        ChatSession,
        KnowledgeBase,
        KnowledgeComponent,
    )

    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=uid, name="高等数学", subject="math")
        s.add(kb)
        await s.flush()  # 父表先 flush 再插子表 (UoW 不懂 FK 顺序)
        kc = KnowledgeComponent(
            tenant_id=uid, kb_id=kb.id, canonical_name="导数定义", aliases=[]
        )
        chat = ChatSession(tenant_id=uid, user_id=uid, title="导数答疑")
        s.add(kc)
        s.add(chat)
        await s.flush()
        s.add(
            ChatMessage(
                tenant_id=uid, session_id=chat.id, role="user", content=_EVIDENCE
            )
        )
        return str(kc.id), str(chat.id)


def _ctx(uid: uuid.UUID):
    from eduagents_api.deps import AuthContext

    return AuthContext(user_id=uid, tenant_id=uid, role="student", jti="")


# ---- reflection 写入 + 读 API + 本人隔离 + 删除 ----


async def test_reflection_insight_crud_and_isolation(client):
    from eduagents_worker.reflection import reflect_session

    user_id, headers = await _register_login(client, "ins-a")
    other_id, other_headers = await _register_login(client, "ins-b")
    uid = uuid.UUID(user_id)
    kc_id, session_id = await _seed_kc_and_chat(uid)

    gateway = _FakeGateway()
    assert (
        await reflect_session({}, user_id, user_id, session_id, gateway=gateway)
        == "applied:1"
    )
    # 重复入队 (worker 重试): BKT 与原话都不重复落
    assert (
        await reflect_session({}, user_id, user_id, session_id, gateway=gateway)
        == "applied:0"
    )

    # 本人可见: join KC 名 + 学生原话 + 会话回链
    r = await client.get("/api/profile/insights", headers=headers)
    assert r.status_code == 200
    items = r.json()["insights"]
    assert len(items) == 1
    item = items[0]
    assert item["kc_id"] == kc_id and item["kc_name"] == "导数定义"
    assert item["source"] == "chat_reflection" and item["kind"] == "confused"
    assert item["text"] == _EVIDENCE
    assert item["session_id"] == session_id

    # 他人不可见 (RLS 租户隔离), 也删不动 (404 不泄露存在性)
    r = await client.get("/api/profile/insights", headers=other_headers)
    assert r.status_code == 200 and r.json()["insights"] == []
    r = await client.delete(f"/api/profile/insights/{item['id']}", headers=other_headers)
    assert r.status_code == 404
    r = await client.get("/api/profile/insights", headers=headers)
    assert len(r.json()["insights"]) == 1  # 未被他人误删

    # 本人删本人 (用户纠正权) -> 204 -> 列表空; 再删同 id -> 404
    r = await client.delete(f"/api/profile/insights/{item['id']}", headers=headers)
    assert r.status_code == 204
    r = await client.get("/api/profile/insights", headers=headers)
    assert r.json()["insights"] == []
    r = await client.delete(f"/api/profile/insights/{item['id']}", headers=headers)
    assert r.status_code == 404
    assert other_id != user_id


# ---- tutor misconception 写入 ----


async def test_tutor_summary_writes_misconceptions(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import LearnerInsight
    from eduagents_api.services import apply_tutor_summary

    user_id, headers = await _register_login(client, "ins-t")
    uid = uuid.UUID(user_id)
    kc_id, _session_id = await _seed_kc_and_chat(uid)

    changes = await apply_tutor_summary(
        _ctx(uid),
        {
            "misconceptions": [
                "把导数定义当成了普通代入求值",  # 命中词表
                "以为洛必达对任何极限都能用",  # 词表外: 原文仍要保留
            ]
        },
    )
    assert [c["kc_id"] for c in changes] == [kc_id]  # BKT 只收词表内

    async with tenant_session(uid) as s:
        rows = (
            await s.scalars(
                select(LearnerInsight).order_by(LearnerInsight.created_at)
            )
        ).all()
    by_text = {r.text: r for r in rows if r.source == "tutor_summary"}
    assert set(by_text) == {
        "把导数定义当成了普通代入求值",
        "以为洛必达对任何极限都能用",
    }
    hit = by_text["把导数定义当成了普通代入求值"]
    miss = by_text["以为洛必达对任何极限都能用"]
    assert hit.kind == "misconception" and str(hit.kc_id) == kc_id
    assert miss.kind == "misconception" and miss.kc_id is None

    # 读 API: tutor 条目同样可见 (kc 未命中的 kc_name 为空串)
    r = await client.get("/api/profile/insights", headers=headers)
    kinds = {i["kind"] for i in r.json()["insights"]}
    assert kinds == {"misconception"}


# ---- 每用户 100 条上限 ----


async def test_insights_capped_at_100_per_user(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import LearnerInsight
    from eduagents_api.services import (
        INSIGHTS_KEEP_PER_USER,
        persist_learner_insights,
    )

    user_id, headers = await _register_login(client, "ins-cap")
    uid = uuid.UUID(user_id)

    # 直插 102 条"旧"记忆 (显式 created_at 递增, 绕开同事务共享 now())
    base = datetime.now(UTC) - timedelta(days=7)
    async with tenant_session(uid) as s:
        for i in range(102):
            s.add(
                LearnerInsight(
                    tenant_id=uid,
                    user_id=uid,
                    kc_id=None,
                    source="tutor_summary",
                    kind="misconception",
                    text=f"旧误解-{i:03d}",
                    created_at=base + timedelta(minutes=i),
                )
            )

    # 唯一写入口再写 1 条 -> 103 条, 裁 3 条最旧, 只留 100
    async with tenant_session(uid) as s:
        pruned = await persist_learner_insights(
            s,
            _ctx(uid),
            [{"kc_id": None, "kind": "misconception", "text": "最新误解"}],
            source="tutor_summary",
        )
    assert pruned == 3

    async with tenant_session(uid) as s:
        count = await s.scalar(
            select(func.count()).select_from(LearnerInsight).where(
                LearnerInsight.user_id == uid
            )
        )
        texts = set(
            (
                await s.scalars(
                    select(LearnerInsight.text).where(LearnerInsight.user_id == uid)
                )
            ).all()
        )
    assert count == INSIGHTS_KEEP_PER_USER == 100
    assert "最新误解" in texts
    assert {"旧误解-000", "旧误解-001", "旧误解-002"} & texts == set()  # 最旧被裁

    # 读 API 上限 50 条
    r = await client.get("/api/profile/insights", headers=headers)
    items = r.json()["insights"]
    assert len(items) == 50
    assert items[0]["text"] == "最新误解"  # 倒序: 最新在前


# ---- 导出含新表 (PIPL 可携带权) ----


async def test_export_includes_memory_tables(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import UserPreference
    from eduagents_api.services import apply_tutor_summary
    from eduagents_core.config import get_settings
    from eduagents_worker.export import export_user_data

    user_id, headers = await _register_login(client, "ins-exp")
    uid = uuid.UUID(user_id)
    _kc_id, _session_id = await _seed_kc_and_chat(uid)
    await apply_tutor_summary(
        _ctx(uid), {"misconceptions": ["把导数定义当成了普通代入求值"]}
    )
    async with tenant_session(uid) as s:
        s.add(
            UserPreference(
                user_id=uid,
                tenant_id=uid,
                explanation_style="analogy",
                verbosity="concise",
                interests=["天文", "足球"],
                daily_minutes=30,
                goal="期末高数 90 分",
            )
        )

    # 直接跑 worker 任务 (export_user_data 自己写 export:{uid} ready 键)
    path = await export_user_data({"settings": get_settings()}, user_id)
    r = await client.get("/api/account/export/download", headers=headers)
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"tables/learner_insights.json", "tables/user_preferences.json"} <= names

    insights = json.loads(zf.read("tables/learner_insights.json"))
    assert any(row["text"] == "把导数定义当成了普通代入求值" for row in insights)
    prefs = json.loads(zf.read("tables/user_preferences.json"))
    assert len(prefs) == 1
    assert prefs[0]["explanation_style"] == "analogy"
    assert prefs[0]["interests"] == ["天文", "足球"]
    assert prefs[0]["goal"] == "期末高数 90 分"
    assert path.startswith(f"exports/{user_id}/")
