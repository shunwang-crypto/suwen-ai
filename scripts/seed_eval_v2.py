"""本地评估语料库 v2 种子：创建评估账号和“评估语料库v2”知识库，
摄取三篇语料 (calculus.md 主文档 + linear_algebra.md / probability.md 干扰文档)。

幂等: 账号/知识库复用; 文档按 (filename, content_hash) 判断 ——
- 同名同哈希且 ready: 跳过;
- 同名不同哈希 (语料重新生成过): 删旧 (PG chunks + Qdrant 点位) 再重新摄取,
  保证向量库里没有旧版本残留污染评估。

用法 (cwd = 仓库根, 需 PG/Redis/Qdrant 在跑):
    python scripts/seed_eval_v2.py
输出 tenant_id / kb_id, 供 eval/run_retrieval_eval.py 使用。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import xxhash
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

REPO = Path(__file__).resolve().parents[1]
EVAL_EMAIL = "eval-v2@eduagents.cn"
EVAL_PASSWORD = "eval-pass-123"
KB_NAME = "评估语料库v2"
CORPUS_FILES = ["calculus.md", "linear_algebra.md", "probability.md"]


async def main() -> None:
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import Chunk, Document, KnowledgeBase, User
    from eduagents_api.security import hash_password
    from eduagents_core.config import get_settings
    from eduagents_core.storage import create_storage
    from eduagents_core.vectorstore.qdrant_store import QdrantStore
    from eduagents_worker.ingestion import ingest_document

    settings = get_settings()
    corpora: dict[str, bytes] = {}
    for name in CORPUS_FILES:
        path = REPO / "corpus" / name
        if not path.exists():
            print(
                f"语料不存在: {path}\n先运行: python scripts/gen_corpus.py "
                f"&& python scripts/gen_corpus_v2.py",
                file=sys.stderr,
            )
            raise SystemExit(1)
        corpora[name] = path.read_bytes()

    t0 = time.perf_counter()
    async with plain_session() as session:
        user = await session.scalar(select(User).where(User.email == EVAL_EMAIL))
        if user is None:
            user = User(
                email=EVAL_EMAIL,
                password_hash=hash_password(EVAL_PASSWORD, settings.bcrypt_rounds),
                role="student",
            )
            session.add(user)
            await session.flush()
        user_id = str(user.id)

    async with tenant_session(user_id) as session:
        kb = await session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.name == KB_NAME)
        )
        if kb is None:
            kb = KnowledgeBase(tenant_id=user.id, name=KB_NAME, subject="math")
            session.add(kb)
            await session.flush()
        kb_id = str(kb.id)

    storage = create_storage(settings)
    store = QdrantStore(settings)
    await store.ensure_ready()

    try:
        for filename, data in corpora.items():
            content_hash = xxhash.xxh64_hexdigest(data)
            async with tenant_session(user_id) as session:
                same = await session.scalar(
                    select(Document).where(
                        Document.kb_id == kb.id, Document.content_hash == content_hash
                    )
                )
                if same is not None and same.status in ("ready", "enriching", "enriched"):
                    print(f"  跳过 (已摄取): {filename} status={same.status}")
                    continue
                # 同名旧版本 (哈希不同) -> 删干净再摄取, 避免旧向量污染
                stale_rows = (
                    await session.scalars(
                        select(Document).where(
                            Document.kb_id == kb.id, Document.filename == filename
                        )
                    )
                ).all()
                for stale in stale_rows:
                    if str(stale.content_hash) == content_hash and stale.status == "pending":
                        continue  # 同哈希 pending 复用, 下面直接摄取
                    print(f"  清理旧版本: {filename} doc_id={stale.id}")
                    await store.delete_by_doc(user_id, str(stale.id))
                    await session.execute(sa_delete(Chunk).where(Chunk.doc_id == stale.id))
                    await session.execute(sa_delete(Document).where(Document.id == stale.id))
                doc = await session.scalar(
                    select(Document).where(
                        Document.kb_id == kb.id, Document.content_hash == content_hash
                    )
                )
                if doc is None:
                    doc = Document(
                        tenant_id=user.id,
                        kb_id=kb.id,
                        filename=filename,
                        content_hash=content_hash,
                        size_bytes=len(data),
                        status="pending",
                    )
                    session.add(doc)
                    await session.flush()
                doc_id = str(doc.id)

            await storage.save(f"{user_id}/{kb_id}/{doc_id}/{filename}", data)
            outcome = await ingest_document({}, user_id, doc_id)
            if outcome not in ("ready", "enriching", "enriched"):
                print(f"摄取失败: {filename} -> {outcome}", file=sys.stderr)
                raise SystemExit(1)
            print(f"  摄取完成: {filename} doc_id={doc_id} status={outcome}")
    finally:
        store.close()

    elapsed = time.perf_counter() - t0
    print(f"\n评估账号: {EVAL_EMAIL} / {EVAL_PASSWORD}")
    print(f"tenant_id={user_id}")
    print(f"kb_id={kb_id}")
    print(f"embedding_mode={settings.embedding_mode} rerank_mode={settings.rerank_mode}")
    print(f"耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
