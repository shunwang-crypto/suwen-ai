"""数据生命周期与合规线集成测试：

- P0-4 注销: 12+ 张表零残留 + pending_cleanup 落 tenant 行 + 登录 401 + 旧 token 拒绝
  + purge_tenant 清 checkpoint/Redis/对象存储 (outbox 消费, 幂等)
- P0-3 缓存失效: 删文档 kb_version 自增 (L1 键/L2 filter 落空), 删库 delete_by_kb 清点位
- P1-3 封禁: disable 后 403 ACCOUNT_DISABLED, enable 恢复
- P1-4 机构成员: 踢人后缓存失效看不到机构库 / leave / 邀请码轮换 / 最后 admin 保护
- P1-5 outbox: 消费幂等, 未知 kind 不阻塞
- P1-6 配额: failed 文档不再占存储配额; P1-1 超限 413
- P2-16 密码 72 字节: 25 个汉字注册 -> 422 中文报错

真实 PG/Redis/Qdrant, stub 网关。account 路由主线接线前由本文件 client fixture 挂载。
"""

import io
import json
import os
import uuid
import zipfile

import pytest
from sqlalchemy import func, select, text, update

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


# ---- fixtures / helpers ----


@pytest.fixture
async def client():
    """与 conftest.client 同构, 但确保 account 路由已挂载 (主线接线前的测试挂载)。"""
    from eduagents_core.config import get_settings

    get_settings.cache_clear()
    from eduagents_api.main import create_app
    from eduagents_api.routers import account
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    if not any(getattr(r, "path", "") == "/api/account" for r in app.routes):
        app.include_router(account.router)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _register_login(client, prefix="lc"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}, email


class _StubEmbedder:
    """确定性单位向量: 同 query 恒同向量 -> 余弦 1.0, 不打真实嵌入 API。"""

    def __init__(self, dim: int):
        self._dim = dim

    async def embed(self, texts):
        vec = [0.0] * self._dim
        vec[0] = 1.0
        return [list(vec) for _ in texts]


def _worker_ctx():
    from eduagents_core.config import get_settings

    return {"settings": get_settings()}


# ---- P0-4 注销 + P1-5 outbox + purge ----


async def test_account_deletion_wipes_all_tables(client):
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import (
        AssessmentAnswer,
        AssessmentAttempt,
        Attempt,
        ChatMessage,
        ChatSession,
        Chunk,
        Document,
        ErrorItem,
        ExplainBack,
        KnowledgeBase,
        KnowledgeComponent,
        LearnerInsight,
        LearnerMemoryPolicy,
        LearningResource,
        MasteryLog,
        MasteryRecord,
        MetacognitionPrediction,
        PendingCleanup,
        Plan,
        PlanAdjustment,
        Question,
        Quiz,
        RefreshToken,
        ResourceQuestion,
        UsageLog,
        UserPreference,
    )
    from eduagents_worker.cleanup_outbox import process_pending_cleanup

    user_id, headers, email = await _register_login(client, "del")
    uid = uuid.UUID(user_id)

    # 通过 API 造 kb + 文档 + 机构 (org_members 行)
    r = await client.post("/api/kb", json={"name": "注销测试库"}, headers=headers)
    assert r.status_code == 201
    kb_id = uuid.UUID(r.json()["id"])
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("note.md", "# 泰勒公式\n内容".encode(), "text/markdown")},
        headers=headers,
    )
    assert r.status_code == 201
    doc_id = uuid.UUID(r.json()["id"])
    r = await client.post("/api/orgs", json={"name": "注销测试机构"}, headers=headers)
    assert r.status_code == 201

    # ORM 直插补齐其余学情表 (含 0005 长期记忆/偏好, 16 表全覆盖)
    async with tenant_session(uid) as s:
        sess = ChatSession(tenant_id=uid, user_id=uid, title="t")
        s.add(sess)
        await s.flush()
        s.add(ChatMessage(tenant_id=uid, session_id=sess.id, role="user", content="q"))
        quiz = Quiz(tenant_id=uid, user_id=uid, title="t")
        s.add(quiz)
        await s.flush()
        q = Question(tenant_id=uid, quiz_id=quiz.id, type="single", stem="s", answer="A")
        s.add(q)
        await s.flush()
        attempt = Attempt(tenant_id=uid, user_id=uid, question_id=q.id, response="A")
        s.add(attempt)
        await s.flush()
        s.add(
            MetacognitionPrediction(
                tenant_id=uid,
                user_id=uid,
                attempt_id=attempt.id,
                question_id=q.id,
                predicted_confidence=4,
            )
        )
        s.add(
            ExplainBack(
                tenant_id=uid,
                user_id=uid,
                student_text="我用自己的话解释",
                feedback="表达完整",
            )
        )
        s.add(ErrorItem(tenant_id=uid, user_id=uid, question_id=q.id))
        plan = Plan(
            tenant_id=uid,
            user_id=uid,
            goal="g",
            plan_mode="goal_only",
            grounding_status="unreferenced",
        )
        s.add(plan)
        await s.flush()
        s.add(PlanAdjustment(tenant_id=uid, plan_id=plan.id, reason="r"))
        kc = KnowledgeComponent(tenant_id=uid, kb_id=kb_id, canonical_name="泰勒公式")
        s.add(kc)
        await s.flush()
        s.add(MasteryRecord(tenant_id=uid, user_id=uid, kc_id=kc.id))
        s.add(
            MasteryLog(
                tenant_id=uid, user_id=uid, kc_id=kc.id,
                old_p=0.3, new_p=0.5, evidence_type="quiz",
            )
        )
        # 0005 长期记忆/偏好 (P2-1): 注销必须随 users 外键级联清空
        s.add(
            LearnerInsight(
                tenant_id=uid, user_id=uid, kc_id=kc.id,
                source="chat_reflection", kind="confused", text="为什么要先展开?",
            )
        )
        s.add(UserPreference(user_id=uid, tenant_id=uid, interests=["天文"]))
        s.add(LearnerMemoryPolicy(user_id=uid, tenant_id=uid, revision=1,
                                  blocked_fingerprints=["a" * 64]))
        s.add(
            UsageLog(
                tenant_id=uid, request_id="req-del", user_id=uid,
                feature="qa_generate", model="m", tier="cheap",
            )
        )
        resource = LearningResource(
            tenant_id=uid,
            owner_type="personal",
            owner_id=uid,
            created_by=uid,
            resource_type="worksheet",
            title="注销资料",
            status="ready",
        )
        s.add(resource)
        await s.flush()
        # Use an explicit resource-owned Question so the account deletion test
        # catches the orphan path separately from the legacy Quiz cascade.
        orphan_question = Question(
            tenant_id=uid,
            quiz_id=None,
            type="short",
            stem="注销后不能残留的题",
            answer="答案",
            source_type="document",
        )
        s.add(orphan_question)
        await s.flush()
        resource_question = ResourceQuestion(
            tenant_id=uid,
            resource_id=resource.id,
            question_id=orphan_question.id,
            verification_status="verified",
        )
        s.add(resource_question)
        await s.flush()
        assessment = AssessmentAttempt(
            tenant_id=uid,
            resource_id=resource.id,
            student_id=uid,
            attempt_type="practice",
            status="submitted",
        )
        s.add(assessment)
        await s.flush()
        s.add(
            AssessmentAnswer(
                tenant_id=uid,
                attempt_id=assessment.id,
                resource_question_id=resource_question.id,
                response="作答",
            )
        )
        s.add(
            Chunk(
                tenant_id=uid, doc_id=doc_id, text="chunk 文本",
                content_hash="h", embedding_status="done",
            )
        )
        session_id = sess.id

    # Harness checkpoint contains plaintext learning state and must be deleted too.
    async with tenant_session(uid) as session:
        from eduagents_api.models import HarnessSession
        session.add(HarnessSession(thread_id=f"qa:{user_id}:{session_id}", tenant_id=uid, user_id=uid,
                                   kind="qa", state={"query": "private learning question"}))

    # 密码错误 -> 403, 不销号
    r = await client.request(
        "DELETE", "/api/account", json={"password": "wrong-pass"}, headers=headers
    )
    assert r.status_code == 403
    # 正确密码 -> 204
    r = await client.request(
        "DELETE", "/api/account", json={"password": PASSWORD}, headers=headers
    )
    assert r.status_code == 204, r.text

    # 12+ 张表零残留 (RLS 租户会话: 有残留必可见)
    models_zero = [
        Attempt, AssessmentAnswer, AssessmentAttempt, ChatSession, ChatMessage,
        ErrorItem, ExplainBack, LearningResource, MasteryLog, MasteryRecord,
        MetacognitionPrediction, Plan, PlanAdjustment,
        Quiz, Question, ResourceQuestion,
        KnowledgeBase, Document, Chunk, KnowledgeComponent,
        LearnerInsight, LearnerMemoryPolicy, UserPreference,
    ]
    async with tenant_session(uid) as s:
        for model in models_zero:
            count = await s.scalar(select(func.count()).select_from(model))
            assert count == 0, f"{model.__tablename__} 残留 {count} 行"
        # usage_log: 行留档但 user_id 已 SET NULL (审计留档决策)
        assert (
            await s.scalar(
                select(func.count()).select_from(UsageLog).where(UsageLog.user_id == uid)
            )
        ) == 0
        assert (
            await s.scalar(
                select(func.count()).select_from(UsageLog).where(UsageLog.user_id.is_(None))
            )
        ) >= 1
    async with plain_session() as s:
        # refresh_tokens 级联 = 全部撤销; org_members 级联
        assert (
            await s.scalar(
                select(func.count()).select_from(RefreshToken)
                .where(RefreshToken.user_id == uid)
            )
        ) == 0
        n_members = await s.scalar(
            text("SELECT count(*) FROM org_members WHERE user_id = :u"), {"u": user_id}
        )
        assert n_members == 0
        # pending_cleanup 落了 tenant 行 (与删除同事务)
        row = (
            await s.execute(
                select(PendingCleanup).where(
                    PendingCleanup.kind == "tenant",
                    PendingCleanup.payload["tenant_id"].astext == user_id,
                )
            )
        ).scalar_one()
        assert row.done is False
        outbox_id = row.id

    # 登录 401; 旧 access token 被拒 (jti 拉黑 401 / 账号状态 403)
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 401
    r = await client.get("/api/kb", headers=headers)
    assert r.status_code in (401, 403)

    # outbox 消费: purge_tenant 清 checkpoint/Redis/对象存储; 幂等
    ctx = _worker_ctx()
    assert await process_pending_cleanup(ctx) >= 1
    async with plain_session() as s:
        row = await s.get(PendingCleanup, outbox_id)
        assert row.done is True and row.attempts == 1
    async with tenant_session(uid) as s:
        leftover = await s.scalar(
            text("SELECT count(*) FROM harness_sessions WHERE thread_id LIKE :p"),
            {"p": f"%:{user_id}:%"},
        )
        assert leftover == 0
    # 第二趟: 已 done 的行不再被扫 (幂等), attempts 不变
    await process_pending_cleanup(ctx)
    async with plain_session() as s:
        row = await s.get(PendingCleanup, outbox_id)
        assert row.attempts == 1


# ---- P0-3 缓存失效 + P1-5 outbox (doc/kb) ----


async def test_doc_delete_bumps_kb_version_and_kills_cache(client):
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup
    from eduagents_core.cache import AnswerCache
    from eduagents_core.config import get_settings
    from eduagents_worker.cleanup_outbox import process_pending_cleanup

    user_id, headers, _ = await _register_login(client, "cache")
    settings = get_settings()
    embedder = _StubEmbedder(settings.embedding_dim)
    cache = AnswerCache(settings)  # redis=None: 只走 L2 (L1 键含版本, 数学上必失效)

    r = await client.post("/api/kb", json={"name": "缓存测试库"}, headers=headers)
    kb_id = r.json()["id"]
    assert r.json()["kb_version"] == 1
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("t.md", "# 文档\n体检报告内容".encode(), "text/markdown")},
        headers=headers,
    )
    assert r.status_code == 201
    doc_id = r.json()["id"]

    query = "体检报告写了什么"
    await cache.put(
        user_id, kb_id, 1, query, embedder,
        answer="缓存的答案 (含原文摘录)", citations=[{"highlight": "体检报告内容"}],
    )
    hit = await cache.get(user_id, kb_id, 1, query, embedder)
    assert hit is not None and hit.answer.startswith("缓存的答案")

    # 删文档 -> 事务内 kb_version 自增 + outbox 行
    r = await client.delete(f"/api/kb/{kb_id}/documents/{doc_id}", headers=headers)
    assert r.status_code == 204
    r = await client.get("/api/kb", headers=headers)
    kb_row = next(k for k in r.json() if k["id"] == kb_id)
    assert kb_row["kb_version"] == 2  # P0-3: 历史上恒为 1 的死值终于会动了
    # 同问题按新版本查 -> 不再命中 (chat 路径用 PG 里的当前 kb_version 查缓存)
    assert await cache.get(user_id, kb_id, 2, query, embedder) is None
    async with plain_session() as s:
        n = await s.scalar(
            select(func.count()).select_from(PendingCleanup).where(
                PendingCleanup.kind == "doc",
                PendingCleanup.payload["doc_id"].astext == doc_id,
            )
        )
        assert n == 1

    # 删库 -> delete_by_kb 直接清掉 L2 点位 (老版本键也查不到)
    await cache.put(user_id, kb_id, 2, query, embedder, answer="第二版缓存")
    r = await client.delete(f"/api/kb/{kb_id}", headers=headers)
    assert r.status_code == 204
    assert await cache.get(user_id, kb_id, 2, query, embedder) is None
    async with plain_session() as s:
        n = await s.scalar(
            select(func.count()).select_from(PendingCleanup).where(
                PendingCleanup.kind == "kb",
                PendingCleanup.payload["kb_id"].astext == kb_id,
            )
        )
        assert n == 1

    # outbox 消费 doc/kb 行 (真实 Qdrant/对象存储路径), 幂等: 未知 kind 不阻塞不炸
    async with plain_session() as s:
        s.add(PendingCleanup(kind="bogus", payload={}))
    ctx = _worker_ctx()
    assert await process_pending_cleanup(ctx) >= 2
    async with plain_session() as s:
        undone = (
            await s.execute(
                select(PendingCleanup.kind, PendingCleanup.attempts).where(
                    PendingCleanup.done.is_(False)
                )
            )
        ).all()
        assert ("doc", 1) not in undone and ("kb", 1) not in undone
        bogus = [a for k, a in undone if k == "bogus"]
        assert bogus and bogus[0] >= 1  # 毒行留在队列且有尝试痕迹, 不阻塞他行
        await s.execute(
            text("DELETE FROM pending_cleanup WHERE kind = 'bogus'")
        )


# ---- P1-3 封禁/解封 ----


async def test_disable_enable_user(client):
    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    admin_id, _, admin_email = await _register_login(client, "adm")
    async with plain_session() as s:
        await s.execute(
            update(User).where(User.id == uuid.UUID(admin_id)).values(role="admin")
        )
    r = await client.post(
        "/api/auth/login", json={"email": admin_email, "password": PASSWORD}
    )
    admin_h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    victim_id, victim_h, _ = await _register_login(client, "vic")
    assert (await client.get("/api/kb", headers=victim_h)).status_code == 200

    # 非 admin 无权封禁
    r = await client.post(f"/api/admin/users/{admin_id}/disable", headers=victim_h)
    assert r.status_code == 403
    # 封禁立即生效 (Redis 标记, 不等 60s 缓存过期)
    r = await client.post(f"/api/admin/users/{victim_id}/disable", headers=admin_h)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    r = await client.get("/api/kb", headers=victim_h)
    assert r.status_code == 403
    assert r.json().get("code") == "ACCOUNT_DISABLED" or "ACCOUNT_DISABLED" in r.text
    # 管理员不能封自己
    r = await client.post(f"/api/admin/users/{admin_id}/disable", headers=admin_h)
    assert r.status_code == 400
    # 解封恢复
    r = await client.post(f"/api/admin/users/{victim_id}/enable", headers=admin_h)
    assert r.status_code == 200
    assert (await client.get("/api/kb", headers=victim_h)).status_code == 200


# ---- P1-4 机构成员生命周期 ----


async def test_org_member_lifecycle(client):
    admin_id, admin_h, _ = await _register_login(client, "oadm")
    member_id, member_h, _ = await _register_login(client, "omem")
    third_id, third_h, _ = await _register_login(client, "othr")

    r = await client.post("/api/orgs", json={"name": "成员生命周期机构"}, headers=admin_h)
    org_id, code = r.json()["id"], r.json()["invite_code"]
    r = await client.post("/api/orgs/join", json={"invite_code": code}, headers=member_h)
    assert r.status_code == 200
    r = await client.post(f"/api/orgs/{org_id}/kb", json={"name": "机构库"}, headers=admin_h)
    assert r.status_code == 201
    org_kb_id = r.json()["id"]

    def _sees_org_kb(kbs):
        return any(k["id"] == org_kb_id for k in kbs)

    assert _sees_org_kb((await client.get("/api/kb", headers=member_h)).json())

    # member 无权踢人/轮换
    r = await client.delete(f"/api/orgs/{org_id}/members/{admin_id}", headers=member_h)
    assert r.status_code == 403
    r = await client.post(f"/api/orgs/{org_id}/invite-code/rotate", headers=member_h)
    assert r.status_code == 403
    # 最后一个 admin 不可被删 (自己也一样)
    r = await client.delete(f"/api/orgs/{org_id}/members/{admin_id}", headers=admin_h)
    assert r.status_code == 400
    # 踢人 -> 缓存立即失效, 机构库从列表消失
    r = await client.delete(f"/api/orgs/{org_id}/members/{member_id}", headers=admin_h)
    assert r.status_code == 204
    assert not _sees_org_kb((await client.get("/api/kb", headers=member_h)).json())

    # 邀请码轮换: 旧码立即失效, 新码可入会
    r = await client.post(f"/api/orgs/{org_id}/invite-code/rotate", headers=admin_h)
    assert r.status_code == 200
    new_code = r.json()["invite_code"]
    assert new_code != code and len(new_code) == 8
    r = await client.post("/api/orgs/join", json={"invite_code": code}, headers=third_h)
    assert r.status_code == 404
    r = await client.post("/api/orgs/join", json={"invite_code": new_code}, headers=third_h)
    assert r.status_code == 200
    assert _sees_org_kb((await client.get("/api/kb", headers=third_h)).json())

    # 有其他成员时最后一个 admin 不能 leave; 成员 leave 后可
    r = await client.post(f"/api/orgs/{org_id}/leave", headers=admin_h)
    assert r.status_code == 400
    r = await client.post(f"/api/orgs/{org_id}/leave", headers=third_h)
    assert r.status_code == 204
    assert not _sees_org_kb((await client.get("/api/kb", headers=third_h)).json())
    r = await client.post(f"/api/orgs/{org_id}/leave", headers=admin_h)
    assert r.status_code == 204  # 只剩自己时允许退出


# ---- P1-6 配额排除 failed + P1-1 上传 413 ----


async def test_quota_excludes_failed_and_oversize_413():
    from eduagents_core.config import get_settings

    overrides = {"MAX_UPLOAD_MB": "1", "MAX_STORAGE_MB_PER_USER": "2"}
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    try:
        from eduagents_api.db import tenant_session
        from eduagents_api.main import create_app
        from httpx import ASGITransport, AsyncClient

        app = create_app()
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                user_id, headers, _ = await _register_login(client, "quota")
                r = await client.post("/api/kb", json={"name": "配额库"}, headers=headers)
                kb_id = r.json()["id"]

                def _blob(seed: str, kib: int) -> bytes:
                    raw = seed.encode()
                    needed = kib * 1024
                    return (raw * (needed // len(raw) + 1))[:needed]

                # A: 0.9MB 入库后置 failed
                r = await client.post(
                    f"/api/kb/{kb_id}/documents",
                    files={"file": ("a.txt", _blob("甲失败文档", 900), "text/plain")},
                    headers=headers,
                )
                assert r.status_code == 201, r.text
                doc_a = r.json()["id"]
                # RLS FORCE: 必须带租户 GUC 才能改到自己的行
                async with tenant_session(uuid.UUID(user_id)) as s:
                    await s.execute(
                        text("UPDATE documents SET status='failed' WHERE id = :d"),
                        {"d": doc_a},
                    )
                # B: 0.9MB 正常占额
                r = await client.post(
                    f"/api/kb/{kb_id}/documents",
                    files={"file": ("b.txt", _blob("乙正常文档", 900), "text/plain")},
                    headers=headers,
                )
                assert r.status_code == 201
                # C: 0.9MB — 若 failed 仍占额则 0.9*3 > 2MB 被 400;
                # 修复后 failed 不计 -> 0.9*2 < 2MB 放行 (P1-6)
                r = await client.post(
                    f"/api/kb/{kb_id}/documents",
                    files={"file": ("c.txt", _blob("丙验证文档", 900), "text/plain")},
                    headers=headers,
                )
                assert r.status_code == 201, f"failed 文档仍占配额: {r.text}"
                # D: 1.5MB 超 max_upload_mb=1 -> 分块读提前 413 (P1-1)
                r = await client.post(
                    f"/api/kb/{kb_id}/documents",
                    files={"file": ("d.txt", _blob("丁超大文档", 1500), "text/plain")},
                    headers=headers,
                )
                assert r.status_code == 413
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        get_settings.cache_clear()


# ---- P1-8 导出 ----


async def test_export_user_data(client):
    from eduagents_worker.export import export_user_data

    user_id, headers, _ = await _register_login(client, "exp")
    r = await client.post("/api/kb", json={"name": "导出库"}, headers=headers)
    kb_id = r.json()["id"]
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("e.md", "# 导出\n正文".encode(), "text/markdown")},
        headers=headers,
    )
    assert r.status_code == 201

    # 请求导出 (入队) -> pending; 重复请求幂等
    assert (await client.get("/api/account/export", headers=headers)).json()["status"] == "none"
    r = await client.post("/api/account/export", headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert (await client.post("/api/account/export", headers=headers)).json()["status"] == "pending"
    assert (await client.get("/api/account/export", headers=headers)).json()["status"] == "pending"
    r = await client.get("/api/account/export/download", headers=headers)
    assert r.status_code == 404  # 未生成

    # 直接跑 worker 任务 (队列消费由主线注册 cron/functions)
    path = await export_user_data(_worker_ctx(), user_id)
    assert path.startswith(f"exports/{user_id}/")

    r = await client.get("/api/account/export", headers=headers)
    assert r.json()["status"] == "ready"
    r = await client.get("/api/account/export/download", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"profile.json", "chunks.json", "uploads_manifest.json"} <= names
    tables = {n for n in names if n.startswith("tables/")}
    assert "tables/model_credentials.json" in tables
    assert len(tables) >= 22  # 0017 新增脱敏凭据导出；后续迁移可继续增加表
    assert {
        "tables/learner_insights.json",
        "tables/user_preferences.json",
        "tables/learning_report_snapshots.json",
        "tables/chat_attachments.json",
        "tables/error_imports.json",
        "tables/metacognition_predictions.json",
        "tables/explain_backs.json",
        "tables/assessment_submissions.json",
    } <= tables
    kbs = json.loads(zf.read("tables/knowledge_bases.json"))
    assert any(row["id"] == kb_id for row in kbs)
    manifest = json.loads(zf.read("uploads_manifest.json"))
    assert manifest and manifest[0]["filename"] == "e.md"


# ---- P2-16 密码 72 字节 ----


async def test_password_over_72_bytes_422(client):
    email = f"pw-{uuid.uuid4().hex[:10]}@test.eduagents.cn"
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "汉" * 25}
    )
    assert r.status_code == 422
    assert "密码过长" in r.text  # 中文可读报错, 而非 500 INTERNAL_ERROR
