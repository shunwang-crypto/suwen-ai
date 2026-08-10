"""学习者长期记忆管道单测 (Wave-P2 P2-1, G10): 全离线零 PG/零真实 LLM。

覆盖四件事:
- match_insight_rows: 洞察连同学生原话 (evidence) 成行, 词表外/无原话如实丢弃,
  且 match_insights 旧三元组视图与之同源 (匹配语义单一出处);
- persist_learner_insights: 原话截断 ≤600 / 空文本丢弃 / 超 100 条裁最旧
  (fake session 记录 add/execute, 不触达 PG);
- match_misconception_insights: BKT 命中口径与旧内联实现一致 + 每条误解文本的
  首个命中 kc_id (未命中 None, 原文仍要落库);
- reflect_session 离线全链路: fake gateway (沿用现有 reflection 测试的
  _FakeGateway 写法) + monkeypatch DB 层, 学生原话写入长期记忆, 重复入队
  (幂等口径复用 mastery_log 去重) 不重复落原话。
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

from eduagents_api.services import (
    INSIGHT_TEXT_MAX,
    INSIGHTS_KEEP_PER_USER,
    match_misconception_insights,
    persist_learner_insights,
)
from eduagents_worker.reflection import (
    match_insight_rows,
    match_insights,
    reflect_session,
)

VOCAB = [
    ("kc-1", "导数定义", ["导数的定义"]),
    ("kc-2", "极值判定", ["极值"]),
    ("kc-3", "泰勒公式", []),
]


# ---- match_insight_rows: 学生原话成行 ----


def test_match_insight_rows_keeps_student_evidence():
    rows = match_insight_rows(
        [
            {"kc_name": "导数定义", "signal": "confused", "evidence": " 为什么不能直接代入? "},
            {"kc_name": "极值", "signal": "understood", "evidence": "我会判定了"},  # alias
            {"kc_name": "量子纠缠", "signal": "confused", "evidence": "词表外丢弃"},
            {"kc_name": "泰勒公式", "signal": "confused", "evidence": "  "},  # 无原话丢弃
        ],
        VOCAB,
    )
    assert rows == [
        {"kc_id": "kc-1", "kc_name": "导数定义", "kind": "confused", "text": "为什么不能直接代入?"},
        {"kc_id": "kc-2", "kc_name": "极值判定", "kind": "understood", "text": "我会判定了"},
    ]


def test_match_insights_view_stays_consistent_with_rows():
    # 旧三元组接口是 match_insight_rows 的视图: 两者对同输入必须同判
    insights = [
        {"kc_name": "导数的定义", "signal": "confused", "evidence": "第一次"},
        {"kc_name": "导数定义", "signal": "understood", "evidence": "第二次"},  # 同 KC 先出现者胜
        {"kc_name": "泰勒公式", "signal": "understood", "evidence": "能自己展开了"},
    ]
    confused, understood, names = match_insights(insights, VOCAB)
    rows = match_insight_rows(insights, VOCAB)
    assert confused == ["kc-1"] and understood == ["kc-3"]
    assert names == {"kc-1": "导数定义", "kc-3": "泰勒公式"}
    assert [(r["kc_id"], r["kind"]) for r in rows] == [
        ("kc-1", "confused"),
        ("kc-3", "understood"),
    ]


# ---- persist_learner_insights: 截断/丢空/超额裁剪 (fake session) ----


class _FakeScalarResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeSession:
    """记录 add/execute 的最小 session 假体; scalars 按队列出结果。"""

    def __init__(self, scalars_results=None, execute_results=None):
        self.added = []
        self.executed = []
        self.scalars_results = list(scalars_results or [])
        self.execute_results = list(execute_results or [])
        self.scalar_stmts = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def scalars(self, stmt):
        self.scalar_stmts.append(stmt)
        return _FakeScalarResult(self.scalars_results.pop(0) if self.scalars_results else [])

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeScalarResult(self.execute_results.pop(0) if self.execute_results else [])

    async def get(self, model, pk):  # reflect_session 首个块用
        return getattr(self, "get_result", None)


def _ctx():
    return SimpleNamespace(user_id=uuid.uuid4(), tenant_id=uuid.uuid4())


async def test_persist_truncates_text_and_drops_empty():
    session = _FakeSession(scalars_results=[[]])
    ctx = _ctx()
    sid = str(uuid.uuid4())
    pruned = await persist_learner_insights(
        session,
        ctx,
        [
            {"kc_id": "0" * 8 + str(uuid.uuid4())[8:], "kind": "confused", "text": "原" * 700},
            {"kc_id": None, "kind": "misconception", "text": "  "},  # 空文本丢弃
        ],
        source="chat_reflection",
        session_id=sid,
    )
    assert pruned == 0
    assert len(session.executed) == 1  # 仅去重预查, 未超额无 DELETE
    assert len(session.added) == 1
    row = session.added[0]
    assert row.text == "原" * INSIGHT_TEXT_MAX  # ≤600 截断
    assert row.source == "chat_reflection" and row.kind == "confused"
    assert row.user_id == ctx.user_id and row.tenant_id == ctx.tenant_id
    assert str(row.session_id) == sid


async def test_persist_prunes_oldest_beyond_100():
    stale = [uuid.uuid4() for _ in range(3)]  # 库里第 101/102/103 旧的三条
    session = _FakeSession(scalars_results=[stale])
    pruned = await persist_learner_insights(
        session,
        _ctx(),
        [{"kc_id": None, "kind": "misconception", "text": "新误解"}],
        source="tutor_summary",
    )
    assert pruned == 3
    # executed[0] = 内容级去重预查 (审查修复轮新增), executed[1] = DELETE ... WHERE id IN (stale)
    assert len(session.executed) == 2
    delete_sql = str(session.executed[1])
    assert "DELETE FROM learner_insights" in delete_sql
    # 裁剪的查询从第 101 条起 (保留最近 100 条)
    assert INSIGHTS_KEEP_PER_USER == 100
    prune_stmt = session.scalar_stmts[0]
    compiled = str(prune_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "OFFSET 100" in compiled and "ORDER BY" in compiled


async def test_persist_all_empty_rows_is_noop():
    session = _FakeSession()
    pruned = await persist_learner_insights(
        session, _ctx(), [{"kc_id": None, "kind": "confused", "text": ""}], source="chat_reflection"
    )
    assert pruned == 0 and not session.added and not session.scalar_stmts


# ---- match_misconception_insights: BKT 口径 + 每条文本首个命中 ----


def test_misconception_matching_and_per_text_attribution():
    texts = [
        "把导数定义当成了普通代入求值",
        "极值 判定时忘了检查二阶条件",  # alias + 归一化 (去空白)
        "完全词表外的误解",
    ]
    matched, name_by_id, kc_by_text = match_misconception_insights(texts, VOCAB)
    assert matched == ["kc-1", "kc-2"]  # 词表序, 去重 (旧 BKT 口径)
    assert name_by_id == {"kc-1": "导数定义", "kc-2": "极值判定"}
    assert kc_by_text == ["kc-1", "kc-2", None]  # 未命中原文仍应带 None 落库


def test_misconception_one_text_multiple_kcs_first_in_vocab_order_wins():
    matched, _names, kc_by_text = match_misconception_insights(
        ["导数定义和泰勒公式一起混着错"], VOCAB
    )
    assert matched == ["kc-1", "kc-3"]  # BKT 两个都计
    assert kc_by_text == ["kc-1"]  # 落库归因取词表序首个


# ---- reflect_session 离线全链路 (fake gateway, 沿用现有 reflection 测试写法) ----

_EVIDENCE = "为什么导数定义里不能直接把 x=1 代进去?"
_PAYLOAD = {"insights": [{"kc_name": "导数定义", "signal": "confused", "evidence": _EVIDENCE}]}


class _FakeGateway:
    def __init__(self, payload=_PAYLOAD):
        self.payload = payload
        self.calls = 0

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        self.calls += 1
        return ChatResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="replay",
        )


async def test_reflect_session_writes_student_words_once(monkeypatch):
    import eduagents_api.db as db_mod
    import eduagents_api.services as services_mod

    tenant_id = uuid.uuid4()
    user_id = tenant_id  # 个人语义: tenant 即 user
    session_id = uuid.uuid4()
    kc_id = uuid.uuid4()

    fake = _FakeSession()
    fake.get_result = SimpleNamespace(user_id=user_id)  # ChatSession 假体

    def _fresh_scalars():
        # 每次 reflect_session 消费两个 scalars: 消息 + KC 词表
        fake.scalars_results = [
            [SimpleNamespace(role="user", content=_EVIDENCE)],
            [SimpleNamespace(id=kc_id, canonical_name="导数定义", aliases=[])],
        ]

    @asynccontextmanager
    async def fake_tenant_session(tid, **kw):
        yield fake

    mastery_calls = []

    async def fake_update_mastery(session, ctx, kc_ids, **kw):
        mastery_calls.append(kc_ids)
        # 首次全部入账, 复跑模拟 mastery_log 去重后零变化 (幂等口径)
        if len(mastery_calls) == 1:
            return [{"kc_id": k, "old_p": 0.3, "new_p": 0.25} for k in kc_ids]
        return []

    persisted = []

    async def fake_persist(session, ctx, rows, *, source, session_id=None):
        persisted.append({"rows": rows, "source": source, "session_id": session_id})
        return 0

    monkeypatch.setattr(db_mod, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(services_mod, "apply_mastery_evidence", fake_update_mastery)
    monkeypatch.setattr(services_mod, "persist_learner_insights", fake_persist)

    gateway = _FakeGateway()
    _fresh_scalars()
    out1 = await reflect_session({}, str(tenant_id), str(user_id), str(session_id), gateway=gateway)
    _fresh_scalars()
    out2 = await reflect_session({}, str(tenant_id), str(user_id), str(session_id), gateway=gateway)

    assert out1 == "applied:1" and out2 == "applied:0"
    # 长期记忆只落一次 (复跑 changes 为空 -> 不重复写原话), 且带学生原话与 KC 归因
    assert len(persisted) == 1
    call = persisted[0]
    assert call["source"] == "chat_reflection"
    assert call["session_id"] == str(session_id)
    assert call["rows"] == [
        {"kc_id": str(kc_id), "kc_name": "导数定义", "kind": "confused", "text": _EVIDENCE}
    ]


def test_retention_constants():
    # 上限口径: 文本 ≤600 字 / 每用户最近 100 条 (brief-5 §4 P2-1)
    assert INSIGHT_TEXT_MAX == 600
    assert INSIGHTS_KEEP_PER_USER == 100


async def test_persist_dedupes_within_batch_and_against_existing():
    """内容级去重 (审查修复轮): 同批重复与窗口内已有的 (kind, kc_id, text) 跳过不插。

    记忆条目直接面向用户可见可删, summarize 轮被重试/重放时不得出现一字不差的重复行。
    """
    from eduagents_api.services import persist_learner_insights

    kc = uuid.uuid4()
    # execute 队列首项 = 去重预查结果: 库里已有一条 (misconception, kc, "洛必达随便用")
    session = _FakeSession(
        scalars_results=[[]],
        execute_results=[[("misconception", kc, "洛必达随便用")]],
    )
    ctx = _ctx()
    await persist_learner_insights(
        session,
        ctx,
        [
            {"kc_id": str(kc), "kind": "misconception", "text": "洛必达随便用"},  # 与库重复
            {"kc_id": str(kc), "kind": "misconception", "text": "链式法则漏内导"},  # 新
            {"kc_id": str(kc), "kind": "misconception", "text": "链式法则漏内导"},  # 批内重复
        ],
        source="tutor_summary",
    )
    texts = [row.text for row in session.added]
    assert texts == ["链式法则漏内导"]
