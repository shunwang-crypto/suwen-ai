"""演示种子: 创建演示账号 + 高等数学知识库 + 摄取 corpus/calculus.md。
幂等: 重复执行复用已有账号/知识库, 文档按内容哈希去重。
用法: make seed  (输出 tenant_id / kb_id, 供评估 CLI 与演示登录使用)
"""
from __future__ import annotations

import asyncio
import sys
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

    print(f"demo 账号: {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print(f"tenant_id={user_id}")
    print(f"kb_id={kb_id}")
    print(f"doc_id={doc_id} status=ready")


if __name__ == "__main__":
    asyncio.run(main())
