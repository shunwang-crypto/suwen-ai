"""反馈通道与判分申诉全链路。

feedback: 提交(带 request_id)/我的列表/越权不可见/admin 列表+关闭/非 admin 403;
appeal: FakeGateway 高分复核 → 分差 ≥0.3 且跨 0.6 线 → attempt 修正 + mastery
按 mastery_log old_p 回滚再以新证据重写 (evidence_type="appeal_regrade");
低差复核 → 维持原判、掌握度不动; stub 网关 → 202 如实登记待人工复核;
客观题 → 422; 他人 attempt → 404。
main.py 接线由集成方完成, 本测试自挂 router (惯例同 test_reviews_flow)。
"""

import json
import uuid

from sqlalchemy import select, text

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


class FakeGradeGateway:
    """判分/复核返回固定分数 (mode=live, 驱动真实复核分支)。"""

    def __init__(self, score: float, reason: str = "fake-review"):
        self.score = score
        self.reason = reason
        self.features: list[str] = []

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        self.features.append(req.feature)
        return ChatResult(
            text=json.dumps({"score": self.score, "reason": self.reason}, ensure_ascii=False),
            model="fake", tier=req.tier.value, prompt_tokens=1,
            completion_tokens=1, cost_cny=0.0, latency_ms=1, mode="live",
        )


def _mount(app) -> None:
    from eduagents_api.routers import feedback as feedback_router
    from eduagents_api.routers import reviews as reviews_router

    paths = {getattr(r, "path", "") for r in app.router.routes}
    if "/api/feedback" not in paths:
        app.include_router(feedback_router.router)
        app.include_router(feedback_router.admin_router)
    if "/api/appeals" not in paths:
        app.include_router(reviews_router.appeals_router)


async def _register_login(client, email: str) -> dict:
    await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _uid(email: str) -> uuid.UUID:
    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email))
        return user.id


# ---------------------------------------------------------------- P2-1 反馈通道


async def test_feedback_submit_list_admin_close(client):
    app = client._transport.app
    _mount(app)
    tag = uuid.uuid4().hex[:8]
    email_a = f"fb-a-{tag}@test.eduagents.cn"
    email_b = f"fb-b-{tag}@test.eduagents.cn"
    email_admin = f"fb-adm-{tag}@test.eduagents.cn"
    headers_a = await _register_login(client, email_a)
    headers_b = await _register_login(client, email_b)

    # 提交: 分类校验 + request_id 透传
    r = await client.post(
        "/api/feedback",
        headers=headers_a,
        json={
            "category": "bug",
            "content": f"上传按钮点了没反应 {tag}",
            "page_url": "/kb",
            "request_id": f"req-{tag}",
        },
    )
    assert r.status_code == 201, r.text
    fb = r.json()
    assert fb["status"] == "open" and fb["request_id"] == f"req-{tag}"
    fb_id = fb["id"]

    r = await client.post(
        "/api/feedback", headers=headers_a, json={"category": "nope", "content": "x"}
    )
    assert r.status_code == 422

    # 未带 request_id: 回落当前请求 id (非空)
    r = await client.post(
        "/api/feedback", headers=headers_a, json={"category": "other", "content": "第二条"}
    )
    assert r.status_code == 201 and r.json()["request_id"]

    # 我的列表: A 看到自己 2 条; B 一条都看不到 (RLS + user_id 双保险)
    r = await client.get("/api/feedback", headers=headers_a)
    assert r.status_code == 200
    assert {it["id"] for it in r.json()["items"]} >= {fb_id}
    assert r.json()["total"] == 2
    r = await client.get("/api/feedback", headers=headers_b)
    assert r.status_code == 200 and r.json()["total"] == 0

    # 非 admin 摸 admin 端点 → 403
    r = await client.get("/api/admin/feedback", headers=headers_b)
    assert r.status_code == 403
    r = await client.patch(
        f"/api/admin/feedback/{fb_id}", headers=headers_b, json={"status": "closed"}
    )
    assert r.status_code == 403

    # 提权 admin → 全量列表可见 (含用户 email), 关闭工单
    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    await _register_login(client, email_admin)
    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email_admin))
        user.role = "admin"
    headers_admin = await _register_login(client, email_admin)

    r = await client.get("/api/admin/feedback?status=open", headers=headers_admin)
    assert r.status_code == 200, r.text
    by_id = {it["id"]: it for it in r.json()["items"]}
    assert fb_id in by_id
    assert by_id[fb_id]["email"] == email_a
    assert by_id[fb_id]["category"] == "bug"

    r = await client.patch(
        f"/api/admin/feedback/{fb_id}", headers=headers_admin, json={"status": "closed"}
    )
    assert r.status_code == 200 and r.json()["status"] == "closed"
    r = await client.get("/api/admin/feedback?status=open", headers=headers_admin)
    assert fb_id not in {it["id"] for it in r.json()["items"]}
    r = await client.get("/api/admin/feedback?status=closed", headers=headers_admin)
    assert fb_id in {it["id"] for it in r.json()["items"]}

    # 非法状态/不存在 id
    r = await client.patch(
        f"/api/admin/feedback/{fb_id}", headers=headers_admin, json={"status": "wat"}
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/api/admin/feedback/{uuid.uuid4()}", headers=headers_admin, json={"status": "closed"}
    )
    assert r.status_code == 404

    # 直查 DB: 行确实带 request_id (报障可与日志串联)
    from eduagents_api.db import tenant_session

    uid_a = await _uid(email_a)
    async with tenant_session(uid_a) as s:
        rid = await s.scalar(
            text("SELECT request_id FROM feedback WHERE id = :id"), {"id": fb_id}
        )
    assert rid == f"req-{tag}"


# ---------------------------------------------------------------- P2-3 判分申诉


async def _seed_quiz(uid: uuid.UUID, questions: list[dict]) -> dict:
    """构造 KB/Quiz + 指定题目 (每题独立 KC, 隔离各题对掌握度的影响),
    返回 {quiz_id, q_ids, kc_ids} (下标一一对应)。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeBase, KnowledgeComponent, Question, Quiz

    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=uid, name="高数", subject="math")
        s.add(kb)
        await s.flush()
        quiz = Quiz(
            tenant_id=uid,
            user_id=uid,
            kb_id=kb.id,
            title="申诉流测试卷",
            status="ready",
        )
        s.add(quiz)
        await s.flush()
        q_ids, kc_ids = [], []
        for i, spec in enumerate(questions):
            kc = KnowledgeComponent(
                tenant_id=uid, kb_id=kb.id, canonical_name=f"考点{i}-{spec['type']}"
            )
            s.add(kc)
            await s.flush()
            q = Question(
                tenant_id=uid,
                quiz_id=quiz.id,
                type=spec["type"],
                stem=spec["stem"],
                options_json=spec.get("options", []),
                answer=spec["answer"],
                explanation="",
                kc_ids=[str(kc.id)],
            )
            s.add(q)
            await s.flush()
            q_ids.append(str(q.id))
            kc_ids.append(str(kc.id))
        return {"quiz_id": str(quiz.id), "q_ids": q_ids, "kc_ids": kc_ids}


async def _attempt_row(uid: uuid.UUID, question_id: str):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Attempt

    async with tenant_session(uid) as s:
        rows = (
            await s.scalars(
                select(Attempt)
                .where(Attempt.question_id == uuid.UUID(question_id))
                .order_by(Attempt.at.desc())
            )
        ).all()
        return rows[0]


async def _mastery_p(uid: uuid.UUID, kc_id: str) -> float:
    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryRecord

    async with tenant_session(uid) as s:
        record = await s.scalar(
            select(MasteryRecord).where(
                MasteryRecord.user_id == uid,
                MasteryRecord.kc_id == uuid.UUID(kc_id),
            )
        )
        return float(record.p_mastery)


async def test_appeal_regrade_rollback_keep_and_stub(client):
    app = client._transport.app
    _mount(app)
    tag = uuid.uuid4().hex[:8]
    email = f"ap-{tag}@test.eduagents.cn"
    headers = await _register_login(client, email)
    uid = await _uid(email)
    seeded = await _seed_quiz(
        uid,
        [
            {"type": "short", "stem": "简述泰勒公式的意义", "answer": "局部多项式逼近"},
            {"type": "short", "stem": "麦克劳林公式与泰勒公式关系", "answer": "x0=0 特例"},
            {"type": "single", "stem": "泰勒公式展开点记作?", "options": ["x0", "y0"],
             "answer": "A"},
        ],
    )
    quiz_id = seeded["quiz_id"]
    q_short1, q_short2, q_single = seeded["q_ids"]
    kc1, kc2, _kc3 = seeded["kc_ids"]

    # 提交作答: 两道主观题 LLM 判 0.2 分 (错), 客观题答对
    stub_gateway = app.state.gateway
    app.state.gateway = FakeGradeGateway(0.2, reason="要点未覆盖")
    try:
        r = await client.post(
            f"/api/quiz/{quiz_id}/attempts",
            headers=headers,
            json={
                "answers": [
                    {"question_id": q_short1, "response": "泰勒公式是用多项式逼近函数"},
                    {"question_id": q_short2, "response": "不知道"},
                    {"question_id": q_single, "response": "A"},
                ]
            },
        )
        assert r.status_code == 200, r.text
        by_q = {x["question_id"]: x for x in r.json()["results"]}
        assert by_q[q_short1]["llm_score"] == 0.2 and by_q[q_short1]["correct"] is False
        assert by_q[q_single]["llm_score"] is None and by_q[q_single]["correct"] is True

        attempt1 = await _attempt_row(uid, q_short1)
        p_after_wrong = await _mastery_p(uid, kc1)

        # 分支 1: 高分复核 (0.2 → 0.9, 差 ≥0.3 且跨 0.6 线) → 修正 + mastery 回滚重写
        app.state.gateway = FakeGradeGateway(0.9, reason="要点其实已覆盖")
        r = await client.post(
            "/api/appeals",
            headers=headers,
            json={"attempt_id": str(attempt1.id), "reason": "参考答案的逼近要点我答到了"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "adjusted" and body["adjusted"] is True
        assert body["crossed"] is True
        assert body["old_score"] == 0.2 and body["new_score"] == 0.9
        assert body["mastery_changes"], "跨线必须重写掌握度"

        from eduagents_api.db import tenant_session
        from eduagents_api.models import MasteryLog

        attempt1 = await _attempt_row(uid, q_short1)
        assert attempt1.llm_score == 0.9 and attempt1.is_correct is True
        assert "申诉复核修正" in (attempt1.scored_reason or "")
        async with tenant_session(uid) as s:
            logs = (
                await s.scalars(
                    select(MasteryLog)
                    .where(MasteryLog.evidence_id == attempt1.id)
                    .order_by(MasteryLog.at.asc())
                )
            ).all()
        types = [entry.evidence_type for entry in logs]
        assert types[0] == "quiz_attempt" and types[-1] == "appeal_regrade"
        original, regrade = logs[0], logs[-1]
        # 回滚证明: 复核证据从"判分前"的 old_p 起算, 而非在错误低分之上叠加
        assert regrade.old_p == original.old_p
        assert regrade.new_p > original.new_p
        p_after_appeal = await _mastery_p(uid, kc1)
        assert p_after_appeal == regrade.new_p
        assert p_after_appeal > p_after_wrong

        # 分支 2: 低差复核 (0.2 → 0.4, 差 <0.3) → 维持原判, 掌握度不动; 走 question_id 路径
        p_before_keep = await _mastery_p(uid, kc2)
        app.state.gateway = FakeGradeGateway(0.4, reason="仍有要点缺失")
        r = await client.post(
            "/api/appeals",
            headers=headers,
            json={"question_id": q_short2, "reason": "我觉得答得没那么差"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "kept" and body["adjusted"] is False
        assert body["old_score"] == 0.2 and body["new_score"] == 0.4
        assert body["mastery_changes"] == []
        attempt2 = await _attempt_row(uid, q_short2)
        assert attempt2.llm_score == 0.2 and attempt2.is_correct is False
        assert "维持原判" in (attempt2.scored_reason or "")
        assert await _mastery_p(uid, kc2) == p_before_keep

        # 客观题 → 422 (规则判分无复核空间)
        attempt3 = await _attempt_row(uid, q_single)
        r = await client.post(
            "/api/appeals",
            headers=headers,
            json={"attempt_id": str(attempt3.id), "reason": "我不服"},
        )
        assert r.status_code == 422
        assert r.json()["code"] == "APPEAL_NOT_ALLOWED"

        # 参数校验: 两个 id 都传 / 都不传 → 422
        r = await client.post(
            "/api/appeals",
            headers=headers,
            json={"attempt_id": str(attempt1.id), "question_id": q_short1, "reason": "x"},
        )
        assert r.status_code == 422
        r = await client.post("/api/appeals", headers=headers, json={"reason": "x"})
        assert r.status_code == 422

    finally:
        app.state.gateway = stub_gateway

    # 越权: 他人 attempt → 404 (归属校验先于一切复核逻辑, 网关是否 stub 无关)
    other_headers = await _register_login(client, f"ap-b-{tag}@test.eduagents.cn")
    r = await client.post(
        "/api/appeals",
        headers=other_headers,
        json={"attempt_id": str(attempt2.id), "reason": "越权试探"},
    )
    assert r.status_code == 404

    # 分支 3: stub 网关 (mode != live) → 202 如实登记待人工复核, 分数不动
    r = await client.post(
        "/api/appeals",
        headers=headers,
        json={"attempt_id": str(attempt2.id), "reason": "请人工复核"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending_manual_review"
    assert body["new_score"] is None and body["adjusted"] is False
    attempt2 = await _attempt_row(uid, q_short2)
    assert attempt2.llm_score == 0.2
    assert "申诉已登记待人工复核" in (attempt2.scored_reason or "")
