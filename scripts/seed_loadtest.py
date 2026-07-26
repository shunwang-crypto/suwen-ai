"""压测账号池种子: 创建 N 个压测账号 + 各自知识库 + 摄取语料, 并预签 access token。

幂等: 重复执行复用已有账号/知识库; 文档按 xxhash 内容哈希去重; 文档已 ready 则跳过摄取。
预签 token 的原因见 loadtest/locustfile.py 顶部注释 (auth 限流 5 次/分钟/IP,
30 并发在 on_start 登录只会测出限流器)。

!!! 必须与被压测的 API 实例用同一套 stub env 跑本脚本 !!!
    EMBEDDING_MODE=stub RERANK_MODE=stub GATEWAY_MODE=stub
仓库根 .env 里 EMBEDDING_MODE/RERANK_MODE 默认是 live, 直接跑会用真实 embedding 写入向量;
而压测 API 起在 EMBEDDING_MODE=stub 下, 查询向量由 stub 生成 —— 两边口径不一致时,
语料向量与查询向量落在完全不同的空间里, 检索结果会错乱 (召回近似随机),
测出来的 TTFE/TTFT 不再对应真实的检索路径。所以 seed 与 API 必须同档。

用法 (stub 档, 与 scripts/run_loadtest.sh 里的 API 启动命令同档):
    cd /home/shunw/eduagents && export LD_LIBRARY_PATH=/home/shunw/miniforge3/envs/eduagents/lib && \
    GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub \
    /home/shunw/miniforge3/envs/eduagents/bin/python scripts/seed_loadtest.py \
        --count 10 --corpus corpus/calculus.md --out loadtest/accounts.json

产物 loadtest/accounts.json 含明文密码与预签 access token, 已在 .gitignore 中排除。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import xxhash
from sqlalchemy import select

REPO = Path(__file__).resolve().parents[1]
PASSWORD = "loadtest-pass-1"


def _email(index: int) -> str:
    return f"loadtest-{index}@eduagents.cn"


def _kb_name(index: int) -> str:
    return f"压测知识库-{index}"


async def seed_one(index: int, data: bytes, filename: str, settings) -> dict:
    """为第 index 个压测账号建账号/知识库/文档并摄取, 返回 accounts.json 的一条记录。"""
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import Document, KnowledgeBase, User
    from eduagents_api.security import create_access_token, hash_password
    from eduagents_core.storage import create_storage
    from eduagents_worker.ingestion import ingest_document

    email = _email(index)
    async with plain_session() as session:
        user = await session.scalar(select(User).where(User.email == email))
        created_user = user is None
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(PASSWORD, settings.bcrypt_rounds),
                role="student",
            )
            session.add(user)
            await session.flush()
        user_id = str(user.id)
        role = user.role

    tenant_uuid = uuid.UUID(user_id)
    async with tenant_session(user_id) as session:
        kb = await session.scalar(
            select(KnowledgeBase).where(KnowledgeBase.name == _kb_name(index))
        )
        if kb is None:
            kb = KnowledgeBase(tenant_id=tenant_uuid, name=_kb_name(index), subject="math")
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
                tenant_id=tenant_uuid,
                kb_id=kb.id,
                filename=filename,
                content_hash=content_hash,
                size_bytes=len(data),
                status="pending",
            )
            session.add(doc)
            await session.flush()
        doc_id = str(doc.id)
        # 两阶段语义: ready(basic) 之后的 enriching/enriched 同样可问答, 无需重摄取
        needs_ingest = doc.status not in ("ready", "enriching", "enriched")

    if needs_ingest:
        storage = create_storage(settings)
        await storage.save(f"{user_id}/{kb_id}/{doc_id}/{filename}", data)
        outcome = await ingest_document({}, user_id, doc_id)
        if outcome != "ready":
            raise RuntimeError(f"账号 {email} 摄取失败: {outcome}")

    # 预签 access token: 与 /api/auth/login 签发的是同一套 payload (sub/tid/role/jti),
    # 因此压测时可直接当 Bearer 用, 完全绕开 auth 限流。TTL = access_token_ttl_seconds (900s)。
    token, _jti, expires_at = create_access_token(
        settings, user_id=tenant_uuid, tenant_id=tenant_uuid, role=role
    )
    return {
        "index": index,
        "email": email,
        "password": PASSWORD,
        "tenant_id": user_id,
        "kb_id": kb_id,
        "access_token": token,
        "token_expires_at": expires_at.isoformat(),
        "_new_user": created_user,
        "_ingested": needs_ingest,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="压测账号池种子 (幂等)")
    parser.add_argument("--count", type=int, default=10, help="账号数量 (默认 10)")
    parser.add_argument("--corpus", default="corpus/calculus.md", help="语料路径 (相对仓库根)")
    parser.add_argument("--out", default="loadtest/accounts.json", help="输出 JSON 路径")
    args = parser.parse_args()

    from eduagents_core.config import get_settings

    settings = get_settings()
    corpus = Path(args.corpus)
    if not corpus.is_absolute():
        corpus = REPO / corpus
    if not corpus.exists():
        print(f"语料不存在: {corpus} (先运行 python scripts/gen_corpus.py)", file=sys.stderr)
        raise SystemExit(1)
    data = corpus.read_bytes()

    print(
        f"种子配置: count={args.count} corpus={corpus.name} "
        f"gateway={settings.gateway_mode} embedding={settings.embedding_mode} "
        f"rerank={settings.rerank_mode}"
    )
    if settings.embedding_mode != "stub" or settings.gateway_mode != "stub":
        print(
            "警告: 当前不是 stub 档。stub 档压测请用 "
            "GATEWAY_MODE=stub EMBEDDING_MODE=stub RERANK_MODE=stub 重跑本脚本, "
            "否则语料向量与压测实例的查询向量口径不一致, 检索会错乱 (见文件头注释)。",
            file=sys.stderr,
        )
    started = time.perf_counter()
    accounts: list[dict] = []
    # 串行摄取: 并发摄取会把 embedding 后端打满, 反而拖慢种子且引入不确定性
    for index in range(args.count):
        t0 = time.perf_counter()
        record = await seed_one(index, data, corpus.name, settings)
        elapsed = time.perf_counter() - t0
        tag = "新建" if record.pop("_new_user") else "复用"
        ing = "已摄取" if record.pop("_ingested") else "跳过摄取"
        print(
            f"  [{index + 1}/{args.count}] {record['email']} ({tag}, {ing}) "
            f"kb_id={record['kb_id']}  {elapsed:.2f}s"
        )
        accounts.append(record)

    out = Path(args.out)
    if not out.is_absolute():
        out = REPO / out
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seeded_at": datetime.now(UTC).isoformat(),
        "settings": {
            "embedding_mode": settings.embedding_mode,
            "rerank_mode": settings.rerank_mode,
            "gateway_mode": settings.gateway_mode,
        },
        "accounts": accounts,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    total = time.perf_counter() - started
    print(f"\n已写出 {len(accounts)} 个账号 -> {out}")
    print(f"密码统一为 {PASSWORD}; access token 已预签 (TTL {settings.access_token_ttl_seconds}s)")
    print(f"总耗时 {total:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
