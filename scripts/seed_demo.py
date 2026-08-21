"""演示种子: 创建学生/管理员账号 + 学生知识库 + 摄取 corpus/calculus.md。
幂等: 重复执行会校正账号角色与密码, 复用已有知识库, 文档按内容哈希去重。
用法: make seed  (输出 tenant_id / kb_id, 供评估 CLI 与演示登录使用)
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import xxhash
from sqlalchemy import select

REPO = Path(__file__).resolve().parents[1]
DEMO_EMAIL = "demo@eduagents.cn"
DEMO_PASSWORD = "demo-pass-123"
ADMIN_EMAIL = "admin@eduagents.cn"
ADMIN_PASSWORD = "admin-pass-123"


async def main() -> None:
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import Document, KnowledgeBase, User
    from eduagents_api.security import hash_password
    from eduagents_api.wallet import get_wallet_service
    from eduagents_core.config import get_settings
    from eduagents_core.storage import create_storage
    from eduagents_worker.ingestion import ingest_document

    settings = get_settings()
    corpus = REPO / "corpus" / "calculus.md"
    if not corpus.exists():
        print("语料不存在, 先运行: python scripts/gen_corpus.py", file=sys.stderr)
        raise SystemExit(1)
    data = corpus.read_bytes()

    async with plain_session() as session:
        demo_user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if demo_user is None:
            demo_user = User(
                email=DEMO_EMAIL,
            )
            session.add(demo_user)
        demo_user.display_name = "林小溪"
        demo_user.role = "student"
        demo_user.password_hash = hash_password(DEMO_PASSWORD, settings.bcrypt_rounds)

        admin_user = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
        if admin_user is None:
            admin_user = User(email=ADMIN_EMAIL)
            session.add(admin_user)
        admin_user.display_name = "苏问管理员"
        admin_user.role = "admin"
        admin_user.password_hash = hash_password(ADMIN_PASSWORD, settings.bcrypt_rounds)

        await session.flush()
        # 直建账号也走与注册相同的幂等赠额，避免 live 模式被钱包预检拦截。
        wallet = get_wallet_service()
        await wallet.grant_signup(session, demo_user.id)
        await wallet.grant_signup(session, admin_user.id)
        user_id = str(demo_user.id)

    async with tenant_session(user_id) as session:
        kb = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == "高等数学讲义"))
        if kb is None:
            kb = KnowledgeBase(tenant_id=demo_user.id, name="高等数学讲义", subject="math")
            session.add(kb)
            await session.flush()
        kb_id = str(kb.id)
        content_hash = xxhash.xxh64_hexdigest(data)
        doc = await session.scalar(
            select(Document).where(Document.kb_id == kb.id, Document.content_hash == content_hash)
        )
        if doc is None:
            doc = Document(
                tenant_id=demo_user.id,
                kb_id=kb.id,
                filename="calculus.md",
                content_hash=content_hash,
                size_bytes=len(data),
                status="pending",
            )
            session.add(doc)
            await session.flush()
        doc_id = str(doc.id)
        needs_ingest = doc.status not in {"ready", "partial_ready", "enriched"}

    if needs_ingest:
        storage = create_storage(settings)
        await storage.save(f"{user_id}/{kb_id}/{doc_id}/calculus.md", data)
        outcome = await ingest_document({}, user_id, doc_id)
        if outcome not in {"ready", "partial_ready"}:
            print(f"摄取失败: {outcome}", file=sys.stderr)
            raise SystemExit(1)

    async with tenant_session(user_id) as session:
        document_status = await session.scalar(
            select(Document.status).where(Document.id == uuid.UUID(doc_id))
        )
    if settings.gateway_mode != "stub":
        if document_status != "enriched":
            print("资料已完成基础摄取；语义增强由 worker 异步处理，先继续准备本地演示数据。")
            # Do not block `make seed` on a paid/slow model.  The worker owns
            # the enrichment retry policy; the seeded course remains usable
            # for retrieval while it advances from ready to enriched.

    await seed_history(demo_user, kb_id, gateway_mode=settings.gateway_mode)

    print(f"学生账号: {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print(f"管理员账号: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
    print(f"tenant_id={user_id}")
    print(f"kb_id={kb_id}")
    print(f"doc_id={doc_id} status={document_status}")


async def seed_history(user, kb_id: str, *, gateway_mode: str = "live") -> None:
    """§9.6: seed 须含部分历史作答 — 否则演示首屏复习页/画像页全空。
    幂等: 演示卷已存在则跳过。作答走真实判分链路 (record_attempts),
    错题/BKT/审计流水全部由产品代码产生, 非手插结果行。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import Attempt, KcEdge, KnowledgeComponent, MasteryRecord, Question, Quiz
    from eduagents_api.services import record_attempts
    from sqlalchemy import select

    quiz_title = "演示·泰勒公式基础卷"
    async with tenant_session(str(user.id)) as session:
        existing = await session.scalar(select(Quiz).where(Quiz.title == quiz_title))
        if existing is None:
            kcs = {}
            for name, aliases in [
                ("泰勒公式", ["Taylor公式", "泰勒展开"]),
                ("拉格朗日余项", ["Lagrange余项"]),
                ("麦克劳林公式", ["Maclaurin公式"]),
            ]:
                kc = await session.scalar(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.kb_id == uuid.UUID(kb_id),
                        KnowledgeComponent.canonical_name == name,
                    )
                )
                if kc is None:
                    kc = KnowledgeComponent(
                        tenant_id=user.id,
                        kb_id=uuid.UUID(kb_id),
                        canonical_name=name,
                        aliases=aliases,
                        description=f"演示 KC: {name}",
                        source_kind=("chapter_fallback" if gateway_mode == "stub" else "enrichment"),
                    )
                    session.add(kc)
                    await session.flush()
                kcs[name] = str(kc.id)
                # These are unobserved placeholders, not fabricated mastery
                # evidence.  They let the standalone demo quiz resolve its
                # explicitly scoped KC ids while the course enriches.
                record = await session.scalar(
                    select(MasteryRecord).where(
                        MasteryRecord.user_id == user.id,
                        MasteryRecord.kc_id == kc.id,
                    )
                )
                if record is None:
                    session.add(
                        MasteryRecord(
                            tenant_id=user.id,
                            user_id=user.id,
                            kc_id=kc.id,
                            p_mastery=0.3,
                            confidence=0.0,
                        )
                    )
                    await session.flush()
            existing = Quiz(
                tenant_id=user.id,
                user_id=user.id,
                # Seed history is standalone evidence over explicitly scoped
                # KC ids.  It must remain reproducible while a live course is
                # still in the asynchronous ready -> enriched transition.
                kb_id=None,
                title=quiz_title,
                blueprint_json={
                    "_evidence_policy": "mastery_eligible",
                    "_plan_mode": "standalone",
                },
                status="ready",
            )
            session.add(existing)
            await session.flush()
            questions = [
                Question(
                    tenant_id=user.id,
                    quiz_id=existing.id,
                    type="single",
                    stem="泰勒公式在 $x_0=0$ 处的特例称为?",
                    options_json=["麦克劳林公式", "洛必达法则", "拉格朗日中值定理", "柯西公式"],
                    answer="A",
                    explanation="选 B 通常混淆了求极限工具与展开工具。",
                    kc_ids=[kcs["泰勒公式"], kcs["麦克劳林公式"]],
                    difficulty="easy",
                ),
                Question(
                    tenant_id=user.id,
                    quiz_id=existing.id,
                    type="blank",
                    stem="拉格朗日余项 $R_n(x)$ 依赖 $f$ 的 $n+1$ 阶____。",
                    options_json=[],
                    answer="导数|导函数",
                    explanation="余项公式含 $f^{(n+1)}(\\xi)$。",
                    kc_ids=[kcs["拉格朗日余项"]],
                    difficulty="mid",
                ),
                Question(
                    tenant_id=user.id,
                    quiz_id=existing.id,
                    type="single",
                    stem="$e^x$ 的麦克劳林展开的收敛域是?",
                    options_json=["全体实数", "$(-1,1)$", "$(-1,1]$", "$[0,+\\infty)$"],
                    answer="A",
                    explanation="指数函数展开处处收敛。",
                    kc_ids=[kcs["麦克劳林公式"]],
                    difficulty="mid",
                ),
            ]
            session.add_all(questions)
            await session.flush()
        else:
            # A previous interrupted seed may have created the quiz before
            # its first attempt. Keep the idempotent demo fixture standalone
            # so it does not depend on asynchronous course enrichment.
            existing.kb_id = None
            existing.blueprint_json = {
                **(existing.blueprint_json or {}),
                "_evidence_policy": "mastery_eligible",
                "_plan_mode": "standalone",
            }
            questions = (
                await session.scalars(
                    select(Question)
                    .where(Question.quiz_id == existing.id)
                    .order_by(Question.id.asc())
                )
            ).all()
        attempted_question_ids = set(
            await session.scalars(
                select(Attempt.question_id).where(
                    Attempt.user_id == user.id,
                    Attempt.question_id.in_([question.id for question in questions]),
                )
            )
        )
        # Standalone quizzes resolve persisted KC snapshots through the
        # learner's mastery-record scope. Ensure the demo's explicit KC list
        # has only unobserved records before the first real attempt.
        question_kc_ids = {
            uuid.UUID(str(raw_id))
            for question in questions
            for raw_id in (question.kc_ids or [])
        }
        existing_mastery_ids = {
            kc_id
            for kc_id in (
                await session.scalars(
                    select(MasteryRecord.kc_id).where(
                        MasteryRecord.user_id == user.id,
                        MasteryRecord.kc_id.in_(question_kc_ids),
                    )
                )
            ).all()
        }
        for kc_id in sorted(question_kc_ids - existing_mastery_ids, key=str):
            session.add(
                MasteryRecord(
                    tenant_id=user.id,
                    user_id=user.id,
                    kc_id=kc_id,
                    p_mastery=0.3,
                    confidence=0.0,
                )
            )
        if question_kc_ids - existing_mastery_ids:
            await session.flush()

        # 依赖图 demo 需真实先修边: 否则 /plans 知识点依赖图在 stub/演示下无边可画,
        # /growth 薄弱点的"源于前置未掌握"归因(reports 沿 kc_edges 回溯)也无从触发。
        # 泰勒公式是麦克劳林公式(x0=0 特例)与拉格朗日余项(其余项形式)的先修。
        kc_by_name = {
            row.canonical_name: row.id
            for row in (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.kb_id == uuid.UUID(kb_id),
                        KnowledgeComponent.canonical_name.in_(
                            ["泰勒公式", "拉格朗日余项", "麦克劳林公式"]
                        ),
                    )
                )
            ).all()
        }
        for from_name, to_name in [("泰勒公式", "麦克劳林公式"), ("泰勒公式", "拉格朗日余项")]:
            from_id = kc_by_name.get(from_name)
            to_id = kc_by_name.get(to_name)
            if from_id is None or to_id is None:
                continue
            edge_exists = await session.scalar(
                select(KcEdge).where(KcEdge.from_kc == from_id, KcEdge.to_kc == to_id)
            )
            if edge_exists is None:
                session.add(KcEdge(tenant_id=user.id, from_kc=from_id, to_kc=to_id))
        await session.flush()

        pending_questions = [
            question for question in questions if question.id not in attempted_question_ids
        ]
        if not pending_questions:
            print("历史作答已存在, 跳过 seed_history")
            return
        quiz_id = existing.id

    ctx = AuthContext(user_id=user.id, tenant_id=user.id, role="student", jti="seed")
    # 2 对 1 错: 错题入本 + BKT 更新 + mastery_log 全走产品链路 (客观题不需要网关)
    result = await record_attempts(
        ctx,
        None,
        quiz_id,
        [
            {
                "question_id": str(question.id),
                "response": "微分" if question.type == "blank" else "A",
            }
            for question in pending_questions
        ],
    )
    print(f"历史作答: {result['correct']}/{result['total']} 对 (1 错题入复习队列)")


if __name__ == "__main__":
    asyncio.run(main())
