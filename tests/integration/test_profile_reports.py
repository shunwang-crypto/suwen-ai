"""M4: 学情报告 (F7) + "AI 眼中的我" (F8) + 运营成本面板 (F10) 的后端 API。

真实数据直写 DB (KC/mastery/attempts/error/usage), 断言聚合口径与作答记录可核对
(F7-A1), 归因下钻沿先修边 (A→B), 建议引用具体 KC 名/数量 (F7-A2/A3),
跨租户聚合仅 admin 且靠 app.is_admin GUC 放行 (F10)。
main.py 接线由集成方完成, 本测试自行挂载三个 router。
"""
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


@pytest.fixture
async def api(client):
    from eduagents_api.routers import ops, profile, reports

    app = client._transport.app  # ASGITransport 持有 app
    for r in (profile.router, reports.router, ops.router):
        app.include_router(r)
    return client


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


async def _seed_learning_data(uid: str) -> dict:
    """构造: KC 极限(A, p=0.2 弱)→导数定义(B, p=0.35 弱) 先修边, 泰勒公式(C, p=0.9 强);
    B 挂 2 道错题; 今天 2 答 1 对 + 前天 1 答 1 对。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import (
        Attempt,
        Chunk,
        Document,
        ErrorItem,
        KcEdge,
        KnowledgeBase,
        KnowledgeComponent,
        MasteryLog,
        MasteryRecord,
        Question,
        Quiz,
    )

    tid = uuid.UUID(uid)
    now = datetime.now(UTC)
    async with tenant_session(uid) as s:
        kb = KnowledgeBase(tenant_id=tid, name="高等数学", subject="math")
        s.add(kb)
        await s.flush()
        kc_a = KnowledgeComponent(tenant_id=tid, kb_id=kb.id, canonical_name="极限")
        kc_b = KnowledgeComponent(tenant_id=tid, kb_id=kb.id, canonical_name="导数定义")
        kc_c = KnowledgeComponent(tenant_id=tid, kb_id=kb.id, canonical_name="泰勒公式")
        s.add_all([kc_a, kc_b, kc_c])
        await s.flush()
        doc = Document(
            tenant_id=tid,
            kb_id=kb.id,
            filename="calculus.md",
            content_hash=uuid.uuid4().hex,
            status="ready",
        )
        s.add(doc)
        await s.flush()
        s.add_all(
            [
                Chunk(
                    tenant_id=tid,
                    doc_id=doc.id,
                    text="极限",
                    heading_path="高等数学 > 第一章 极限 > 1.1 极限定义",
                    kc_ids=[str(kc_a.id)],
                    content_hash=uuid.uuid4().hex,
                    embedding_status="ready",
                ),
                Chunk(
                    tenant_id=tid,
                    doc_id=doc.id,
                    text="导数定义",
                    heading_path="高等数学 > 第二章 导数 > 2.1 导数定义",
                    kc_ids=[str(kc_b.id)],
                    content_hash=uuid.uuid4().hex,
                    embedding_status="ready",
                ),
                Chunk(
                    tenant_id=tid,
                    doc_id=doc.id,
                    text="泰勒公式",
                    heading_path="高等数学 > 第十二章 泰勒公式 > 12.1 泰勒展开",
                    kc_ids=[str(kc_c.id)],
                    content_hash=uuid.uuid4().hex,
                    embedding_status="ready",
                ),
            ]
        )
        # 先修边: 必须先掌握 A(极限) 才能学 B(导数定义)
        s.add(KcEdge(tenant_id=tid, from_kc=kc_a.id, to_kc=kc_b.id, type="prerequisite"))
        s.add_all(
            [
                MasteryRecord(
                    tenant_id=tid, user_id=tid, kc_id=kc_a.id,
                    p_mastery=0.2, confidence=0.5, last_seen=now,
                ),
                MasteryRecord(
                    tenant_id=tid, user_id=tid, kc_id=kc_b.id,
                    p_mastery=0.35, confidence=0.5, last_seen=now,
                ),
                MasteryRecord(
                    tenant_id=tid, user_id=tid, kc_id=kc_c.id,
                    p_mastery=0.9, confidence=0.8, last_seen=now,
                ),
            ]
        )
        # 活动量: B 3 条 > A 1 条 > C 0 条 (radar 排序依据)
        for _ in range(3):
            s.add(
                MasteryLog(
                    tenant_id=tid, user_id=tid, kc_id=kc_b.id,
                    old_p=0.3, new_p=0.35, evidence_type="quiz_attempt",
                    evidence_id=uuid.uuid4(), weight=1.0,
                )
            )
        s.add(
            MasteryLog(
                tenant_id=tid, user_id=tid, kc_id=kc_a.id,
                old_p=0.3, new_p=0.2, evidence_type="quiz_attempt",
                evidence_id=uuid.uuid4(), weight=1.0,
            )
        )
        quiz = Quiz(tenant_id=tid, user_id=tid, title="导数摸底")
        s.add(quiz)
        await s.flush()
        q1 = Question(
            tenant_id=tid, quiz_id=quiz.id, type="single", stem="导数定义?",
            options_json=["A", "B"], answer="A", kc_ids=[str(kc_b.id)],
        )
        q2 = Question(
            tenant_id=tid, quiz_id=quiz.id, type="single", stem="导数几何意义?",
            options_json=["A", "B"], answer="B", kc_ids=[str(kc_b.id)],
        )
        s.add_all([q1, q2])
        await s.flush()
        s.add_all(
            [
                Attempt(
                    tenant_id=tid, user_id=tid, question_id=q1.id,
                    response="A", is_correct=True, at=now,
                ),
                Attempt(
                    tenant_id=tid, user_id=tid, question_id=q2.id,
                    response="A", is_correct=False, at=now,
                ),
                Attempt(
                    tenant_id=tid, user_id=tid, question_id=q1.id,
                    response="A", is_correct=True, at=now - timedelta(days=2),
                ),
            ]
        )
        s.add_all(
            [
                ErrorItem(
                    tenant_id=tid, user_id=tid, question_id=q2.id,
                    kc_ids=[str(kc_b.id)], cause="concept_confusion",
                ),
                ErrorItem(
                    tenant_id=tid, user_id=tid, question_id=q2.id,
                    kc_ids=[str(kc_b.id)], cause="knowledge_gap",
                ),
            ]
        )
    return {
        "kb_id": str(kb.id),
        "kc_a": str(kc_a.id),
        "kc_b": str(kc_b.id),
        "kc_c": str(kc_c.id),
    }


async def test_profile_structure_and_evidence_state(api):
    """F8: 画像显式区分未观测、估计中与已观测。"""
    _, uid, headers = await _register(api, "prof")
    ids = await _seed_learning_data(uid)

    r = await api.get("/api/profile", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    by_kc = {m["kc_id"]: m for m in body["mastery"]}
    assert set(by_kc) == {ids["kc_a"], ids["kc_b"], ids["kc_c"]}
    assert by_kc[ids["kc_b"]]["name"] == "导数定义"
    assert by_kc[ids["kc_b"]]["p_mastery"] == pytest.approx(0.35)
    assert by_kc[ids["kc_b"]]["evidence_strength"] == "moderate"
    assert by_kc[ids["kc_c"]]["evidence_strength"] == "strong"
    assert by_kc[ids["kc_a"]]["evidence_state"] == "estimated"
    assert by_kc[ids["kc_b"]]["evidence_state"] == "observed"
    assert by_kc[ids["kc_c"]]["evidence_state"] == "unobserved"
    assert by_kc[ids["kc_c"]]["p_mastery"] is None
    assert body["weak_threshold"] == pytest.approx(0.6)
    assert by_kc[ids["kc_b"]]["last_seen"] is not None
    assert body["error_distribution"]["by_cause"] == {"concept_confusion": 1, "knowledge_gap": 1}
    assert body["error_distribution"]["by_state"] == {"new": 2}
    assert 1 <= len(body["recent_changes"]) <= 10
    assert all(
        {"kc_id", "old_p", "new_p", "evidence_type", "weight", "at"} <= set(x)
        for x in body["recent_changes"]
    )

    # 手工覆盖掌握度的绕过入口已删除。
    r = await api.post(
        f"/api/profile/kc/{uuid.uuid4()}/override", headers=headers, json={"p_mastery": 0.5}
    )
    assert r.status_code == 404


async def test_reports_learning(api):
    """F7: radar/trend/observed weak points 与归因下钻。"""
    _, uid, headers = await _register(api, "rep")
    ids = await _seed_learning_data(uid)

    r = await api.get("/api/reports/learning", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()

    # 全局 radar: 该账号只有一个知识库，自动下钻为章节轴，
    # 避免只有一根知识库轴的无信息量图表。
    assert body["axes_kind"] == "chapter"
    assert [x["name"] for x in body["radar"]] == [
        "第一章 极限",
        "第二章 导数",
        "第十二章 泰勒公式",
    ]
    assert [x["kcs"][0]["kc_id"] for x in body["radar"]] == [
        ids["kc_a"],
        ids["kc_b"],
        ids["kc_c"],
    ]
    assert [x["p_mastery"] for x in body["radar"]] == [None, pytest.approx(0.35), None]
    assert [x["weak_count"] for x in body["radar"]] == [0, 1, 0]
    assert all(x["kb_id"] == ids["kb_id"] for x in body["radar"])

    # 兼容字段仍保留活动量 top KC，旧消费方不会被静默破坏。
    assert len(body["radar_kcs"]) == 3
    assert body["radar_kcs"][0]["kc_id"] == ids["kc_b"]
    assert body["radar_kcs"][0]["p_mastery"] == pytest.approx(0.35)

    # 单库 radar: 轴切为章节，中文章号按数值排序（第十二章不能排到第二章前）。
    r = await api.get(f"/api/reports/learning?kb_id={ids['kb_id']}", headers=headers)
    assert r.status_code == 200, r.text
    chapter_body = r.json()
    assert chapter_body["axes_kind"] == "chapter"
    assert [x["name"] for x in chapter_body["radar"]] == [
        "第一章 极限",
        "第二章 导数",
        "第十二章 泰勒公式",
    ]
    assert [x["kcs"][0]["kc_id"] for x in chapter_body["radar"]] == [
        ids["kc_a"],
        ids["kc_b"],
        ids["kc_c"],
    ]
    assert chapter_body["radar"] == body["radar"]

    # trend: 恰好 30 天, 与作答记录可核对 (F7-A1); P2-11 后日切按 Asia/Shanghai
    trend = body["trend"]
    assert len(trend) == 30
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    by_date = {t["date"]: t for t in trend}
    assert trend[-1]["date"] == today.isoformat()
    assert by_date[today.isoformat()]["attempts"] == 2
    assert by_date[today.isoformat()]["correct"] == 1
    assert by_date[today.isoformat()]["accuracy"] == pytest.approx(0.5)
    d2 = (today - timedelta(days=2)).isoformat()
    assert by_date[d2]["attempts"] == 1 and by_date[d2]["accuracy"] == pytest.approx(1.0)
    assert by_date[(today - timedelta(days=1)).isoformat()]["attempts"] == 0

    # weak_points: 只有三条独立证据的 B 能被稳定定性为薄弱。
    weak = {w["kc_id"]: w for w in body["weak_points"]}
    assert set(weak) == {ids["kc_b"]}
    b = weak[ids["kc_b"]]
    assert b["error_count"] == 2
    roots = {rc["kc_id"] for rc in b["possible_root_cause"]}
    assert roots == set()

    assert "actions" not in body

    # 首屏只返回持久化快照，不等待 LLM；同 scope 再读命中数据库快照。
    assert body["cached"] is False
    cached = await api.get("/api/reports/learning", headers=headers)
    assert cached.status_code == 200
    assert cached.json()["cached"] is True

    # 后台刷新是独立端点；stub 下不伪造正文，且不影响已返回的确定性报告。
    refreshed = await api.post("/api/reports/learning/refresh", headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    refreshed_body = refreshed.json()
    assert refreshed_body["radar"] == body["radar"]

    # narrative: stub 网关下如实 null + note, 不许桩文本冒充 (§14)
    from eduagents_core.config import get_settings

    if get_settings().gateway_mode == "stub":
        assert refreshed_body["report_status"] == "unavailable"
        assert refreshed_body["narrative"] is None
        assert refreshed_body["narrative_note"] == "需 live 网关生成"

        # 模拟真实模型完成路径：更新后的 server timestamp 必须显式 refresh，防止
        # SQLAlchemy 在响应序列化阶段触发 MissingGreenlet。
        from eduagents_api.db import tenant_session
        from eduagents_api.deps import get_app_settings
        from eduagents_api.models import LearningReportSnapshot
        from eduagents_core.gateway import ChatResult
        from eduagents_worker.reports import refresh_learning_report

        async with tenant_session(uid) as session:
            snapshot = await session.scalar(
                select(LearningReportSnapshot).where(
                    LearningReportSnapshot.user_id == uuid.UUID(uid),
                    LearningReportSnapshot.scope_key == "all",
                )
            )
            snapshot.status = "pending"
            snapshot.prompt_version = "outdated"

        class FakeGateway:
            async def chat(self, _request):
                return ChatResult(
                    text="现状：测试诊断。\n归因：测试归因。\n建议：完成 3 道练习。",
                    model="fake-live",
                    tier="standard",
                    prompt_tokens=10,
                    completion_tokens=20,
                    cost_cny=0.0,
                    latency_ms=1,
                    mode="live",
                )

        app = api._transport.app
        live_settings = get_settings().model_copy(update={"gateway_mode": "live"})
        app.dependency_overrides[get_app_settings] = lambda: live_settings
        try:
            live_refresh = await api.post("/api/reports/learning/refresh", headers=headers)
        finally:
            app.dependency_overrides.pop(get_app_settings, None)
        assert live_refresh.status_code == 200, live_refresh.text
        assert live_refresh.json()["report_status"] == "generating"

        async with tenant_session(uid) as session:
            snapshot = await session.scalar(
                select(LearningReportSnapshot).where(
                    LearningReportSnapshot.user_id == uuid.UUID(uid),
                    LearningReportSnapshot.scope_key == "all",
                )
            )
            snapshot_id = str(snapshot.id)
            signature = snapshot.data_signature
        assert (
            await refresh_learning_report(
                {},
                uid,
                uid,
                snapshot_id,
                signature,
                gateway=FakeGateway(),
                settings=live_settings,
            )
            == "completed"
        )
        completed = await api.get("/api/reports/learning", headers=headers)
        assert completed.json()["report_status"] == "completed"
        assert completed.json()["narrative"].startswith("现状")
    else:
        assert refreshed_body["narrative"]


async def test_ops_usage_admin_cross_tenant(api):
    """F10: 非 admin 403; admin 经 app.is_admin GUC 看到跨租户合计; latency 分位数正确。"""
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import UsageLog, User

    email_x, uid_x, headers_x = await _register(api, "opsx")
    _, uid_y, headers_y = await _register(api, "opsy")

    feat = f"f{uuid.uuid4().hex[:10]}"  # 唯一 feature 名, 与库中其他测试数据隔离
    model_a = f"m-alpha-{uuid.uuid4().hex[:8]}"
    model_b = f"m-beta-{uuid.uuid4().hex[:8]}"
    for uid, cost, latency, model, tier, mode, pt, ct in [
        (uid_x, 1.5, 100, model_a, "standard", "stub", 100, 50),
        (uid_y, 2.5, 200, model_b, "cheap", "live", 30, 20),
    ]:
        async with tenant_session(uid) as s:
            s.add(
                UsageLog(
                    tenant_id=uuid.UUID(uid), request_id="r1", user_id=uuid.UUID(uid),
                    feature=feat, model=model, tier=tier, prompt_tokens=pt,
                    completion_tokens=ct, cost_cny=cost, latency_ms=latency, mode=mode,
                )
            )

    # 非 admin -> 403
    r = await api.get("/api/admin/usage", headers=headers_x)
    assert r.status_code == 403
    r = await api.get("/api/admin/usage/latency", headers=headers_y)
    assert r.status_code == 403

    # DB 提升 X 为 admin, 重新登录拿 admin token
    async with plain_session() as s:
        user = await s.scalar(select(User).where(User.email == email_x))
        user.role = "admin"
    r = await api.post("/api/auth/login", json={"email": email_x, "password": PASSWORD})
    headers_admin = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = await api.get("/api/admin/usage?days=7", headers=headers_admin)
    assert r.status_code == 200, r.text
    body = r.json()
    # 跨租户合计 = 两租户之和 -> is_admin GUC 放行生效 (X 的 tenant GUC 本看不到 Y 的行)
    feat_row = next(x for x in body["by_feature"] if x["feature"] == feat)
    assert feat_row["cost_cny"] == pytest.approx(4.0)
    assert feat_row["calls"] == 2 and feat_row["tokens"] == 200
    assert body["totals"]["cost_cny"] >= 4.0
    models = {x["model"]: x for x in body["by_model"]}
    assert models[model_a]["tier"] == "standard"
    assert models[model_a]["cost_cny"] == pytest.approx(1.5)
    assert models[model_b]["cost_cny"] == pytest.approx(2.5)
    assert {x["mode"] for x in body["by_mode"]} >= {"stub", "live"}
    assert body["daily"] and all({"date", "calls", "cost_cny", "tokens"} <= set(d) for d in body["daily"])
    assert body["top_users"] and all(len(u["user"]) == 8 for u in body["top_users"])

    r = await api.get("/api/admin/usage/latency?days=7", headers=headers_admin)
    assert r.status_code == 200, r.text
    lat = next(x for x in r.json()["by_feature"] if x["feature"] == feat)
    assert lat["calls"] == 2
    assert lat["p50_ms"] == pytest.approx(150.0)  # percentile_cont([100,200], 0.5)
    assert lat["p95_ms"] == pytest.approx(195.0)  # percentile_cont([100,200], 0.95)


async def test_profile_tenant_isolation(api):
    """普通用户 profile 只见自己数据: A 有画像, B 全空。"""
    _, uid_a, headers_a = await _register(api, "isoa")
    await _seed_learning_data(uid_a)
    _, _, headers_b = await _register(api, "isob")

    r = await api.get("/api/profile", headers=headers_b)
    assert r.status_code == 200
    body = r.json()
    assert body["mastery"] == []
    assert body["recent_changes"] == []
    assert body["error_distribution"] == {"by_cause": {}, "by_state": {}}

    # A 自己能看到 (对照组, 排除"两边都空"假阳性)
    r = await api.get("/api/profile", headers=headers_a)
    assert len(r.json()["mastery"]) == 3
