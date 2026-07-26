"""演示种子: 创建演示账号 + 高等数学知识库 + 摄取 corpus/calculus.md。
幂等: 重复执行复用已有账号/知识库, 文档按内容哈希去重。
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


async def main() -> None:
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import Document, KnowledgeBase, User
    from eduagents_api.security import hash_password
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
        user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if user is None:
            user = User(
                email=DEMO_EMAIL,
                password_hash=hash_password(DEMO_PASSWORD, settings.bcrypt_rounds),
                role="student",
            )
            session.add(user)
            await session.flush()
        user_id = str(user.id)

    async with tenant_session(user_id) as session:
        kb = await session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.name == "高等数学讲义")
        )
        if kb is None:
            kb = KnowledgeBase(
                tenant_id=user.id, name="高等数学讲义", subject="math"
            )
            session.add(kb)
            await session.flush()
        kb_id = str(kb.id)
        content_hash = xxhash.xxh64_hexdigest(data)
        doc = await session.scalar(
            select(Document).where(
                Document.kb_id == kb.id, Document.content_hash == content_hash
            )
        )
        if doc is None:
            doc = Document(
                tenant_id=user.id,
                kb_id=kb.id,
                filename="calculus.md",
                content_hash=content_hash,
                size_bytes=len(data),
                status="pending",
            )
            session.add(doc)
            await session.flush()
        doc_id = str(doc.id)
        needs_ingest = doc.status != "ready"

    if needs_ingest:
        storage = create_storage(settings)
        await storage.save(f"{user_id}/{kb_id}/{doc_id}/calculus.md", data)
        outcome = await ingest_document({}, user_id, doc_id)
        if outcome != "ready":
            print(f"摄取失败: {outcome}", file=sys.stderr)
            raise SystemExit(1)

    await seed_history(user, kb_id)

    print(f"demo 账号: {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print(f"tenant_id={user_id}")
    print(f"kb_id={kb_id}")
    print(f"doc_id={doc_id} status=ready")


async def seed_history(user, kb_id: str) -> None:
    """§9.6: seed 须含部分历史作答 — 否则演示首屏复习页/画像页全空。
    幂等: 演示卷已存在则跳过。作答走真实判分链路 (record_attempts),
    错题/BKT/审计流水全部由产品代码产生, 非手插结果行。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.deps import AuthContext
    from eduagents_api.models import KnowledgeComponent, Question, Quiz
    from eduagents_api.services import record_attempts
    from sqlalchemy import select

    quiz_title = "演示·泰勒公式基础卷"
    async with tenant_session(str(user.id)) as session:
        existing = await session.scalar(select(Quiz).where(Quiz.title == quiz_title))
        if existing is not None:
            print("历史作答已存在, 跳过 seed_history")
            return
        kcs = {}
        for name, aliases in [
            ("泰勒公式", ["Taylor公式", "泰勒展开"]),
            ("拉格朗日余项", ["Lagrange余项"]),
            ("麦克劳林公式", ["Maclaurin公式"]),
        ]:
            kc = KnowledgeComponent(
                tenant_id=user.id,
                kb_id=uuid.UUID(kb_id),
                canonical_name=name,
                aliases=aliases,
                description=f"演示 KC: {name}",
            )
            session.add(kc)
            await session.flush()
            kcs[name] = str(kc.id)
        quiz = Quiz(
            tenant_id=user.id, user_id=user.id, kb_id=uuid.UUID(kb_id),
            title=quiz_title, status="ready",
        )
        session.add(quiz)
        await session.flush()
        quiz_id = quiz.id
        questions = [
            Question(
                tenant_id=user.id, quiz_id=quiz_id, type="single",
                stem="泰勒公式在 $x_0=0$ 处的特例称为?",
                options_json=["麦克劳林公式", "洛必达法则", "拉格朗日中值定理", "柯西公式"],
                answer="A",
                explanation="选 B 通常混淆了求极限工具与展开工具。",
                kc_ids=[kcs["泰勒公式"], kcs["麦克劳林公式"]], difficulty="easy",
            ),
            Question(
                tenant_id=user.id, quiz_id=quiz_id, type="blank",
                stem="拉格朗日余项 $R_n(x)$ 依赖 $f$ 的 $n+1$ 阶____。",
                options_json=[], answer="导数|导函数",
                explanation="余项公式含 $f^{(n+1)}(\\xi)$。",
                kc_ids=[kcs["拉格朗日余项"]], difficulty="mid",
            ),
            Question(
                tenant_id=user.id, quiz_id=quiz_id, type="single",
                stem="$e^x$ 的麦克劳林展开的收敛域是?",
                options_json=["全体实数", "$(-1,1)$", "$(-1,1]$", "$[0,+\\infty)$"],
                answer="A", explanation="指数函数展开处处收敛。",
                kc_ids=[kcs["麦克劳林公式"]], difficulty="mid",
            ),
        ]
        for q in questions:
            session.add(q)
        await session.flush()
        q_ids = [str(q.id) for q in questions]

    ctx = AuthContext(user_id=user.id, tenant_id=user.id, role="student", jti="seed")
    # 2 对 1 错: 错题入本 + BKT 更新 + mastery_log 全走产品链路 (客观题不需要网关)
    result = await record_attempts(
        ctx, None, quiz_id,
        [
            {"question_id": q_ids[0], "response": "A"},
            {"question_id": q_ids[1], "response": "微分"},
            {"question_id": q_ids[2], "response": "A"},
        ],
    )
    print(f"历史作答: {result['correct']}/{result['total']} 对 (1 错题入复习队列)")


if __name__ == "__main__":
    asyncio.run(main())
