"""VLSE 闭环三处断点的端到端验收 (F8-A2 / M4-D2 / §5.5 双通道之二)。

① 画像注入问答: chat 的图 payload 带 profile_summary, generate 的 system prompt 含
   画像段与薄弱 KC 名, trace 出现 {"type":"profile", weak_points:[...]};
② 画像联动出题: from_weak_points=true 时后端填 kc_names, blueprint 的 kc_name 全部
   落在该用户薄弱点集合内, 且出题 prompt 里确实带着这些薄弱 KC 名;
③ Reflection: 直调 reflect_session, 对话洞察以 weight=0.3 / evidence_type=
   chat reflection 只落 learner_insights，不把模型抽取当作掌握证据;
④ 有画像时答案缓存禁用 (cacheable=False 路径), 无画像用户仍走缓存;
⑤ 非 KB 会话 (P1-5): 直连路径同样注入画像段 + profile trace, 且答案缓存零触碰。

全程零真实 LLM: FakeGateway 脚本化 + FakeRetrieval 预置结果; PG/Redis/Qdrant 为真。
"""

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


# ---------- fakes ----------


class FakeGateway:
    """按 feature 分派的脚本化网关; 记录全部请求供 prompt 断言。

    缺脚本的 feature 返回 "{}" (调用方按解析失败降级), 不抛异常 —— 集成路径上
    额外的 cheap 调用不该让测试脆断。
    """

    def __init__(self, scripts: dict | None = None):
        self.requests = []
        self._scripts = {k: list(v) for k, v in (scripts or {}).items()}

    def prompts(self, feature: str) -> list[str]:
        return [
            "\n".join(m.content for m in r.messages)
            for r in self.requests
            if r.feature == feature
        ]

    async def chat(self, req):
        if req.feature.startswith("agent_"):
            from native_model import native_fixture_reply
            return await native_fixture_reply(self, req)
        from eduagents_core.gateway import ChatResult

        self.requests.append(req)
        queue = self._scripts.get(req.feature) or []
        if not queue:
            payload = "{}"
        elif len(queue) == 1:
            payload = queue[0]
        else:
            payload = queue.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ChatResult(
            text=text, model="fake", tier=str(req.tier), prompt_tokens=1,
            completion_tokens=1, cost_cny=0.0, latency_ms=0, mode="replay",
        )


class FakeRetrieval:
    """单条高置信证据: qa 图走快路径 (跳过 grade) 直达 generate。"""

    def __init__(self, doc_id: str):
        from eduagents_core.retrieval.base import RetrievalResult, RetrievedChunk

        self._result = RetrievalResult(
            chunks=[
                RetrievedChunk(
                    chunk_id=str(uuid.uuid4()),
                    text="导数的定义是差商在自变量增量趋于零时的极限。",
                    heading_path="第二章 > 导数",
                    doc_id=doc_id,
                    page_start=12,
                    page_end=12,
                    score=0.93,
                    source="fused",
                )
            ],
            dense_hits=1, sparse_hits=1, used_sparse=True, used_rerank=False,
            confident=True,
        )

    async def retrieve(self, tenant_id, kb_id, query):
        return self._result


class RecordingRuntime:
    """HarnessRuntime 代理: 记录进图的 payload (断言 profile_summary 确实注入)。"""

    def __init__(self, inner):
        self.inner = inner
        self.payloads: list[dict] = []

    def thread_id(self, *args):
        return self.inner.thread_id(*args)

    def parse_thread_id(self, thread_id):
        return self.inner.parse_thread_id(thread_id)

    def stream_task(self, kind, thread_id, payload, resume=None):
        if payload is not None:
            self.payloads.append(payload)
        return self.inner.stream_task(kind, thread_id, payload, resume=resume)


class SpyCache:
    """答案缓存探针: 只记录调用, get 恒为未命中。"""

    def __init__(self):
        self.gets = 0
        self.puts = 0

    async def get(self, *args, **kwargs):
        self.gets += 1
        return None

    async def put(self, *args, **kwargs):
        self.puts += 1


# ---------- 夹具工具 ----------


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


async def _seed_kb(uid: str) -> dict:
    """知识库 + 三个 KC (无掌握度记录)。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KnowledgeBase, KnowledgeComponent

    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=tid, name="高等数学", subject="math")
        s.add(kb)
        await s.flush()
        doc = Document(
            tenant_id=tid, kb_id=kb.id, filename="高数讲义.pdf",
            content_hash=uuid.uuid4().hex, size_bytes=1024, status="ready",
        )
        s.add(doc)
        kcs = {
            "deriv": KnowledgeComponent(
                tenant_id=tid, kb_id=kb.id, canonical_name="导数定义",
                aliases=["导数的定义"], source_kind="chapter_fallback",
            ),
            "extremum": KnowledgeComponent(
                tenant_id=tid, kb_id=kb.id, canonical_name="极值判定",
                aliases=["极值"], source_kind="chapter_fallback",
            ),
            "taylor": KnowledgeComponent(
                tenant_id=tid, kb_id=kb.id, canonical_name="泰勒公式",
                aliases=[], source_kind="chapter_fallback",
            ),
        }
        s.add_all(list(kcs.values()))
        await s.flush()
        parent = Chunk(
            tenant_id=tid,
            doc_id=doc.id,
            text="高等数学讲义",
            heading_path="高等数学",
            content_hash=uuid.uuid4().hex,
            embedding_status="parent",
        )
        s.add(parent)
        await s.flush()
        for key, name in (("deriv", "导数定义"), ("extremum", "极值判定"), ("taylor", "泰勒公式")):
            s.add(
                Chunk(
                    tenant_id=tid,
                    doc_id=doc.id,
                    parent_id=parent.id,
                    text=f"{name}章节内容",
                    heading_path=name,
                    kc_ids=[str(kcs[key].id)],
                    content_hash=uuid.uuid4().hex,
                    embedding_status="done",
                )
            )
        return {
            "kb_id": str(kb.id),
            "doc_id": str(doc.id),
            **{k: str(v.id) for k, v in kcs.items()},
        }


async def _seed_profile(uid: str, ids: dict, *, error_state: str = "new") -> None:
    """薄弱点画像: 导数定义 0.24 / 极值判定 0.31 / 泰勒公式 0.92 + 一道概念混淆错题。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import ErrorItem, MasteryLog, MasteryRecord, Question, Quiz

    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        for key, p in (("deriv", 0.24), ("extremum", 0.31), ("taylor", 0.92)):
            s.add(
                MasteryRecord(
                    tenant_id=tid, user_id=tid, kc_id=uuid.UUID(ids[key]),
                    p_mastery=p, confidence=0.6, last_seen=datetime.now(UTC),
                )
            )
            for _ in range(3):
                s.add(
                    MasteryLog(
                        tenant_id=tid,
                        user_id=tid,
                        kc_id=uuid.UUID(ids[key]),
                        old_p=p,
                        new_p=p,
                        evidence_type="quiz_attempt",
                        evidence_id=uuid.uuid4(),
                        weight=1.0,
                    )
                )
        quiz = Quiz(tenant_id=tid, user_id=tid, title="旧练习", status="ready")
        s.add(quiz)
        await s.flush()
        question = Question(
            tenant_id=tid, quiz_id=quiz.id, type="single", stem="导数定义题",
            options_json=["A", "B", "C", "D"], answer="A", explanation="",
            kc_ids=[ids["deriv"]], difficulty="easy",
        )
        s.add(question)
        await s.flush()
        s.add(
            ErrorItem(
                tenant_id=tid, user_id=tid, question_id=question.id,
                kc_ids=[ids["deriv"]], cause="concept_confusion", state=error_state,
            )
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


def _qa_gateway() -> FakeGateway:
    return FakeGateway(
        {
            "qa_query_plan": [{"queries": ["导数的定义是什么"]}],
            "qa_generate": ["导数的定义就是差商的极限。"],
            "qa_verify_support": [{"supported": True, "unsupported_claims": []}],
            "qa_followups": [{"followups": []}],
        }
    )


async def _qa_runtime(app, gateway, doc_id: str):
    """装载 FakeGateway + FakeRetrieval 的 qa HarnessRuntime (Checkpointer 走真实 PG)。"""
    from eduagents_api.harness_runtime import HarnessRuntime
    from eduagents_core.config import get_settings
    from eduagents_core.education.context import EducationDeps

    settings = get_settings()
    runtime = HarnessRuntime(
        settings,
        EducationDeps(
            settings=settings, gateway=gateway,
            retrieval=FakeRetrieval(doc_id), web_search=None,
        ),
    )
    await runtime.start()
    return runtime


# ---------- ① 画像注入问答 (F8-A2) ----------


async def test_profile_injected_into_qa(client):
    uid, headers = await _register(client, "vlse-qa")
    ids = await _seed_kb(uid)
    await _seed_profile(uid, ids)

    app = client._transport.app
    gateway = _qa_gateway()
    runtime = await _qa_runtime(app, gateway, ids["doc_id"])
    recording = RecordingRuntime(runtime)
    old_runtime, old_cache = app.state.harness, app.state.answer_cache
    app.state.harness = recording
    app.state.answer_cache = None  # 缓存路径单独在 ④ 验证
    try:
        events = await _sse(
            client, "POST", "/api/chat", headers,
            {"message": "导数的定义是什么?", "kb_ids": [ids["kb_id"]]},
        )
    finally:
        app.state.harness = old_runtime
        app.state.answer_cache = old_cache
        await runtime.stop()

    # payload 确实带画像 (摘要点名薄弱 KC, 且 ≤300 字)
    assert recording.payloads, "chat 未进入 qa 子图"
    payload = recording.payloads[0]
    summary = payload["profile_summary"]
    assert summary and len(summary) <= 300
    assert "导数定义" in summary and "薄弱点" in summary
    assert "泰勒公式" in summary  # p=0.92 -> 已掌握段
    assert "概念混淆" in summary  # 错因分布进摘要
    assert payload["profile_weak_points"][:2] == ["导数定义", "极值判定"]
    assert payload["response_mode"] == "fast"
    assert events["node_update"][0] == {"node": "agent", "status": "running"}

    # trace 可见 (前端时间线渲染"已加载学情画像: N 个薄弱点")
    profile_events = [t for t in events.get("trace_event", []) if t.get("type") == "profile"]
    assert len(profile_events) == 1, events.get("trace_event")
    assert "导数定义" in profile_events[0]["weak_points"]

    # generate 的 system prompt 真的带了画像段与个性化指令
    gen_prompts = gateway.prompts("qa_generate")
    assert len(gen_prompts) == 1
    assert "学生画像" in gen_prompts[0]
    assert "导数定义" in gen_prompts[0]
    assert "薄弱点" in gen_prompts[0]

    assert events["done"][0]["route"] == "kb"


async def test_no_profile_no_injection(client):
    """无画像用户: payload 摘要为空串, 不发 profile trace, prompt 不含画像段。"""
    uid, headers = await _register(client, "vlse-noprof")
    ids = await _seed_kb(uid)

    app = client._transport.app
    gateway = _qa_gateway()
    runtime = await _qa_runtime(app, gateway, ids["doc_id"])
    recording = RecordingRuntime(runtime)
    old_runtime, old_cache = app.state.harness, app.state.answer_cache
    app.state.harness = recording
    app.state.answer_cache = None
    try:
        events = await _sse(
            client, "POST", "/api/chat", headers,
            {"message": "导数的定义是什么?", "kb_ids": [ids["kb_id"]]},
        )
    finally:
        app.state.harness = old_runtime
        app.state.answer_cache = old_cache
        await runtime.stop()

    assert recording.payloads[0]["profile_summary"] == ""
    assert [t for t in events.get("trace_event", []) if t.get("type") == "profile"] == []
    assert "学生画像" not in gateway.prompts("qa_generate")[0]


# ---------- ② 画像联动出题 (M4-D2) ----------


async def test_quiz_from_weak_points(client):
    uid, headers = await _register(client, "vlse-quiz")
    ids = await _seed_kb(uid)
    await _seed_profile(uid, ids, error_state="archived")

    # 预览接口 = 出题实际取的薄弱点 (所见即所练)
    r = await client.get("/api/profile/weak-points", headers=headers)
    assert r.status_code == 200, r.text
    preview = r.json()
    preview_names = [w["name"] for w in preview["weak_points"]]
    assert preview_names[:2] == ["导数定义", "极值判定"]
    assert "泰勒公式" not in preview_names  # p=0.92 不是薄弱点
    assert preview["weak_points"][0]["recent_error"] is True  # 近 7 天错过 -> 优先

    blueprint = {
        "items": [
            {"kc_name": "导数定义", "type": "single", "difficulty": "easy"},
            {"kc_name": "极值判定", "type": "single", "difficulty": "easy"},
        ],
        "summary": "薄弱点强化卷",
    }
    gateway = FakeGateway({"quiz_blueprint": [blueprint]})

    from eduagents_api.harness_runtime import HarnessRuntime
    from eduagents_core.config import get_settings
    from eduagents_core.education.context import EducationDeps

    app = client._transport.app
    settings = get_settings()
    runtime = HarnessRuntime(
        settings,
        EducationDeps(settings=settings, gateway=gateway, retrieval=None, web_search=None),
    )
    await runtime.start()
    old_runtime = app.state.harness
    app.state.harness = runtime
    try:
        events = await _sse(
            client, "POST", "/api/quiz", headers,
            {
                "from_weak_points": True,
                "num_questions": 2,
                "difficulty_mix": {"easy": 2, "mid": 0, "hard": 0},
            },
        )
    finally:
        app.state.harness = old_runtime
        await runtime.stop()

    # trace 先行下发选中的薄弱点 ("这卷题为什么是这些知识点"可核对)
    weak_traces = [
        t for t in events.get("trace_event", []) if t.get("event") == "weak_points_selected"
    ]
    assert len(weak_traces) == 1, events.get("trace_event")
    assert weak_traces[0]["kc_names"] == preview_names
    assert set(weak_traces[0]) == {
        "node",
        "event",
        "mode",
        "count",
        "kc_names",
        "difficulty_mix",
        "rationale",
    }
    assert weak_traces[0]["mode"] == "weak_kc"
    assert "p_mastery" not in weak_traces[0]
    assert "confidence" not in weak_traces[0]
    assert "mastery_band" not in weak_traces[0]

    # blueprint 的 kc_name 全部落在薄弱点集合内
    intr = events["interrupt"][0]
    assert intr["kind"] == "blueprint_confirm"
    items = intr["blueprint"]["items"]
    assert items and {it["kc_name"] for it in items} <= set(preview_names)

    # 出题 prompt 里确实带着薄弱 KC 名 (kc_names 由后端填, 非前端传入)
    prompt = gateway.prompts("quiz_blueprint")[0]
    assert "导数定义" in prompt and "极值判定" in prompt
    assert "薄弱点强化练习" in prompt  # topic 缺省值


async def test_quiz_from_weak_points_without_data_falls_back_to_assessment(client):
    """无掌握度数据 -> 不再 400 NO_WEAK_POINTS, 转 Assessment Mode (§5.3)。

    冷启动死锁的回归防线: 曾经"要有薄弱点才能出题, 要出题才能产生薄弱点", 前端
    据此禁用入口, 新用户永远迈不出第一步。这个用户连知识库都没有, 所以 KC 图为空,
    返回显式的前置缺失 NO_KC_AVAILABLE —— 那是真正无事可做, 与"证据不足"不同。
    """
    _uid, headers = await _register(client, "vlse-nowp")
    r = await client.get("/api/profile/weak-points", headers=headers)
    assert r.json()["weak_points"] == []
    r = await client.post("/api/quiz", headers=headers, json={"from_weak_points": True})
    assert r.status_code == 409
    assert r.json()["code"] == "NO_KC_AVAILABLE"


async def test_quiz_from_weak_points_empty_kb_does_not_fallback_cross_subject(client):
    """显式选中无薄弱点的科目时返回空，不得回退到其他科目混卷。"""
    uid, headers = await _register(client, "vlse-kb-scope")
    math_ids = await _seed_kb(uid)
    await _seed_profile(uid, math_ids)

    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeBase, KnowledgeComponent, MasteryRecord

    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        physics = KnowledgeBase(tenant_id=tid, name="大学物理", subject="physics")
        s.add(physics)
        await s.flush()
        mechanics = KnowledgeComponent(
            tenant_id=tid,
            kb_id=physics.id,
            canonical_name="机械能守恒",
            aliases=[],
        )
        s.add(mechanics)
        await s.flush()
        s.add(
            MasteryRecord(
                tenant_id=tid,
                user_id=tid,
                kc_id=mechanics.id,
                p_mastery=0.92,
                confidence=0.6,
                last_seen=datetime.now(UTC),
            )
        )
        physics_id = str(physics.id)

    r = await client.get(
        f"/api/profile/weak-points?kb_id={physics_id}", headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["weak_points"] == []

    r = await client.post(
        "/api/quiz",
        headers=headers,
        json={"from_weak_points": True, "kb_id": physics_id},
    )
    # 该库没有资料，因此必须进入 needs_upload；不能因为另一个数学库有 KC
    # 就跨范围混卷。已有记录但资料已 ready 的 maintenance 分支由统一 selector 覆盖。
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "NO_KC_AVAILABLE"


# ---------- ③ Reflection 对话洞察 (§5.5) ----------


async def test_reflect_session_writes_insights_without_mastery(client):
    uid, headers = await _register(client, "vlse-reflect")
    ids = await _seed_kb(uid)

    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        ChatMessage,
        ChatSession,
        LearnerInsight,
        MasteryLog,
        MasteryRecord,
    )
    from eduagents_worker.reflection import reflect_session

    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        chat_session = ChatSession(tenant_id=tid, user_id=tid, title="导数答疑")
        s.add(chat_session)
        await s.flush()
        session_id = str(chat_session.id)
        s.add_all(
            [
                ChatMessage(
                    tenant_id=tid, session_id=chat_session.id, role="user",
                    content="为什么导数定义里不能直接把 x=1 代进去?",
                ),
                ChatMessage(
                    tenant_id=tid, session_id=chat_session.id, role="assistant",
                    content="因为差商在代入前是 0/0 型, 必须先取极限。",
                ),
                ChatMessage(
                    tenant_id=tid, session_id=chat_session.id, role="user",
                    content="泰勒公式我懂了, 就是用多项式在一点附近逼近函数。",
                ),
            ]
        )

    gateway = FakeGateway(
        {
            "chat_reflect": [
                {
                    "insights": [
                        {
                            "kc_name": "导数定义",
                            "signal": "confused",
                            "evidence": "为什么导数定义里不能直接把 x=1 代进去?",
                        },
                        {
                            "kc_name": "泰勒公式",
                            "signal": "understood",
                            "evidence": "泰勒公式我懂了",
                        },
                        {
                            "kc_name": "词表外的量子力学",  # 词表外 -> 丢弃
                            "signal": "confused",
                            "evidence": "这个也不懂",
                        },
                    ]
                }
            ]
        }
    )
    result = await reflect_session({}, uid, uid, session_id, gateway=gateway)
    assert result == "recorded:2", result

    # 洞察抽取 prompt 带上了受控词表与对话原文
    prompt = gateway.prompts("chat_reflect")[0]
    assert "导数定义" in prompt and "学生:" in prompt

    async with tenant_session(uid) as s:
        insights = (await s.scalars(select(LearnerInsight))).all()
        logs = (await s.scalars(select(MasteryLog))).all()
        records = (await s.scalars(select(MasteryRecord))).all()
    assert {str(row.kc_id) for row in insights} == {ids["deriv"], ids["taylor"]}
    assert logs == [] and records == []


async def test_reflect_session_stub_gateway_skipped(client):
    """stub 网关 (测试默认 GATEWAY_MODE) 下不产生任何画像写入 (同 enrichment 约定)。"""
    uid, _headers = await _register(client, "vlse-stub")
    from eduagents_api.db import tenant_session
    from eduagents_api.models import ChatSession, MasteryLog
    from eduagents_worker.reflection import reflect_session

    tid = uuid.UUID(uid)
    async with tenant_session(uid) as s:
        chat_session = ChatSession(tenant_id=tid, user_id=tid, title="空会话")
        s.add(chat_session)
        await s.flush()
        session_id = str(chat_session.id)

    assert await reflect_session({}, uid, uid, session_id) == "skipped"
    async with tenant_session(uid) as s:
        assert (await s.scalars(select(MasteryLog))).all() == []


# ---------- ④ 有画像时禁用答案缓存 ----------


async def test_answer_cache_disabled_when_profile_present(client):
    app = client._transport.app
    old_runtime = app.state.harness
    old_cache, old_embedder = app.state.answer_cache, app.state.embedder

    # 有画像的用户: 答案已按其薄弱点个性化 -> 缓存整条链路都不该被触碰
    uid, headers = await _register(client, "vlse-cache-p")
    ids = await _seed_kb(uid)
    await _seed_profile(uid, ids)
    spy = SpyCache()
    gateway = _qa_gateway()
    runtime = await _qa_runtime(app, gateway, ids["doc_id"])
    app.state.harness = runtime
    app.state.answer_cache = spy
    app.state.embedder = object()  # 仅需非 None (cacheable 判据)
    try:
        await _sse(
            client, "POST", "/api/chat", headers,
            {"message": "导数的定义是什么?", "kb_ids": [ids["kb_id"]]},
        )
    finally:
        app.state.harness = old_runtime
        await runtime.stop()
    assert (spy.gets, spy.puts) == (0, 0)

    # 无画像的用户: 同样的单库首轮问答, 缓存照常读写
    uid2, headers2 = await _register(client, "vlse-cache-n")
    ids2 = await _seed_kb(uid2)
    spy2 = SpyCache()
    gateway2 = _qa_gateway()
    runtime2 = await _qa_runtime(app, gateway2, ids2["doc_id"])
    app.state.harness = runtime2
    app.state.answer_cache = spy2
    try:
        events2 = await _sse(
            client, "POST", "/api/chat", headers2,
            {"message": "导数的定义是什么?", "kb_ids": [ids2["kb_id"]]},
        )
    finally:
        app.state.harness = old_runtime
        app.state.answer_cache = old_cache
        app.state.embedder = old_embedder
        await runtime2.stop()
    assert spy2.gets == 1 and spy2.puts == 1
    assert events2["node_update"][:3] == [
        {"node": "cache_lookup", "status": "running"},
        {"node": "cache_lookup", "status": "done"},
        {"node": "agent", "status": "running"},
    ]


# ---------- ⑤ 非 KB 会话画像注入 + 缓存互斥 (P1-5) ----------


class StreamingFakeGateway(FakeGateway):
    """直连网关路径 (无 kb) 用: 网关流式协议 = 若干 StreamDelta + 末尾恰好一个 ChatResult。"""

    async def stream_chat(self, req):
        from eduagents_core.gateway import StreamDelta

        result = await self.chat(req)
        for i in range(0, len(result.text), 6):
            yield StreamDelta(result.text[i : i + 6])
        yield result


async def test_profile_injected_without_kb(client):
    """非 KB 会话: 画像段进直连路径 system prompt, trace 可见, 答案缓存零触碰。"""
    uid, headers = await _register(client, "vlse-nokb")
    ids = await _seed_kb(uid)
    await _seed_profile(uid, ids)

    app = client._transport.app
    gateway = StreamingFakeGateway({"chat": ["导数定义你之前不熟，我们从差商的极限讲起。"]})
    spy = SpyCache()
    old_gateway = app.state.gateway
    old_cache, old_embedder = app.state.answer_cache, app.state.embedder
    app.state.gateway = gateway
    app.state.answer_cache = spy
    app.state.embedder = object()  # 仅需非 None (缓存判据若误放行, spy 必有记录)
    try:
        events = await _sse(
            client, "POST", "/api/chat", headers, {"message": "导数的定义是什么?"}
        )
    finally:
        app.state.gateway = old_gateway
        app.state.answer_cache = old_cache
        app.state.embedder = old_embedder

    # 直连路径 (feature="chat") 实收 prompt 带画像段与薄弱 KC 名
    prompts = gateway.prompts("chat")
    assert len(prompts) == 1
    assert "学生画像" in prompts[0]
    assert "导数定义" in prompts[0] and "薄弱点" in prompts[0]

    # trace 可见 (与 kb 路径同款 profile 事件, 前端时间线"已加载学情画像")
    profile_events = [t for t in events.get("trace_event", []) if t.get("type") == "profile"]
    assert len(profile_events) == 1, events.get("trace_event")
    assert "导数定义" in profile_events[0]["weak_points"]

    # 缓存互斥覆盖 direct 路径: 有画像时答案缓存整条链路零触碰
    assert (spy.gets, spy.puts) == (0, 0)
    assert events["done"][0]["session_id"]


async def test_no_kb_no_profile_keeps_bare_prompt(client):
    """非 KB 且无画像: 裸助教 prompt, 无 profile trace; 直连路径本就不使用答案缓存。"""
    _uid, headers = await _register(client, "vlse-nokb-n")

    app = client._transport.app
    gateway = StreamingFakeGateway({"chat": ["导数是函数的瞬时变化率。"]})
    spy = SpyCache()
    old_gateway = app.state.gateway
    old_cache, old_embedder = app.state.answer_cache, app.state.embedder
    app.state.gateway = gateway
    app.state.answer_cache = spy
    app.state.embedder = object()
    try:
        events = await _sse(
            client, "POST", "/api/chat", headers, {"message": "导数的定义是什么?"}
        )
    finally:
        app.state.gateway = old_gateway
        app.state.answer_cache = old_cache
        app.state.embedder = old_embedder

    assert "学生画像" not in gateway.prompts("chat")[0]
    assert [t for t in events.get("trace_event", []) if t.get("type") == "profile"] == []
    assert (spy.gets, spy.puts) == (0, 0)
    assert events["done"][0]["session_id"]
