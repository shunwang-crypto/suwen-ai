"""Wave3 机构共享知识库: 建机构/凭码加入 -> org admin 建库上传 -> 成员只读可检索
-> 写操作 service 层 403 + RLS 层双保险 -> 个人+机构联合检索 (kb_ids) -> 非成员 403
-> 存量个人库行为回归。stub 网关 + 真实 PG/Redis/Qdrant。"""
import json
import uuid

from sqlalchemy import func, select, update

from .conftest import requires_services

pytestmark = requires_services

_TAYLOR_MD = """# 泰勒公式讲义

## 泰勒展开

泰勒公式将函数展开为多项式: $f(x) = \\sum_{k=0}^{n} \\frac{f^{(k)}(x_0)}{k!}(x-x_0)^k + R_n(x)$。
拉格朗日余项刻画了泰勒逼近的误差界。泰勒展开在近似计算中非常有用。
"""

_BAYES_MD = """# 贝叶斯定理教材

## 贝叶斯公式

贝叶斯定理描述条件概率的反转: $P(A|B) = P(B|A)P(A)/P(B)$。
先验概率经过似然更新得到后验概率, 这是贝叶斯推断的核心。
"""

_DECOY_MD = """# 干扰文档 (无关租户)

## 泰勒与贝叶斯的杂谈

泰勒公式与贝叶斯定理都是数学工具, 本段文字属于另一个租户, 绝不允许被检索到。
"""


async def _register_login(client, prefix="org"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    assert r.status_code == 201
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    assert r.status_code == 200
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}, email


async def _upload_and_ingest(client, headers, kb_id: str, owner_id: str, name: str, content: str):
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": (name, content.encode(), "text/markdown")},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    from eduagents_worker.ingestion import ingest_document

    assert await ingest_document({}, owner_id, doc_id) == "ready"
    return doc_id


async def _sse_chat(client, headers, payload: dict, expect_status=200) -> dict[str, list]:
    events: dict[str, list] = {}
    current = None
    async with client.stream(
        "POST", "/api/chat", json=payload, headers=headers, timeout=60
    ) as resp:
        assert resp.status_code == expect_status, await resp.aread()
        if resp.status_code != 200:
            return events
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current:
                events.setdefault(current, []).append(json.loads(line.split(":", 1)[1]))
    return events


async def test_org_shared_kb_full_slice(client):
    admin_id, admin_h, _ = await _register_login(client, "adm")
    member_id, member_h, member_email = await _register_login(client, "mem")
    outsider_id, outsider_h, _ = await _register_login(client, "out")

    # ---- ① 建机构 + 邀请码加入 ----
    r = await client.post("/api/orgs", json={"name": "清北教研组"}, headers=admin_h)
    assert r.status_code == 201, r.text
    org = r.json()
    org_id, code = org["id"], org["invite_code"]
    assert org["role"] == "admin" and len(code) == 8

    r = await client.post("/api/orgs/join", json={"invite_code": "WRONG123"}, headers=member_h)
    assert r.status_code == 404 and r.json()["code"] == "INVITE_CODE_INVALID"
    r = await client.post("/api/orgs/join", json={"invite_code": code}, headers=member_h)
    assert r.status_code == 200
    assert r.json()["role"] == "member" and r.json()["invite_code"] is None
    r = await client.post("/api/orgs/join", json={"invite_code": code}, headers=member_h)
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_MEMBER"

    r = await client.get("/api/orgs", headers=member_h)
    mine = r.json()
    assert [o["id"] for o in mine] == [org_id] and mine[0]["role"] == "member"
    r = await client.get(f"/api/orgs/{org_id}/members", headers=member_h)
    members = r.json()
    assert {m["role"] for m in members} == {"admin", "member"}
    assert any(m["email"] == member_email for m in members)
    # 非成员看成员列表 -> 404 (不泄漏存在性)
    r = await client.get(f"/api/orgs/{org_id}/members", headers=outsider_h)
    assert r.status_code == 404

    # ---- ② org admin 建库上传; member 只读可检索, 写操作 403 (service 层) ----
    r = await client.post(f"/api/orgs/{org_id}/kb", json={"name": "统一教材库", "subject": "math"}, headers=member_h)
    assert r.status_code == 403  # member 不能建机构库
    r = await client.post(f"/api/orgs/{org_id}/kb", json={"name": "统一教材库", "subject": "math"}, headers=admin_h)
    assert r.status_code == 201, r.text
    org_kb = r.json()
    assert org_kb["owner_type"] == "org" and org_kb["org_id"] == org_id and org_kb["writable"]
    org_kb_id = org_kb["id"]

    org_doc_id = await _upload_and_ingest(client, admin_h, org_kb_id, org_id, "bayes.md", _BAYES_MD)
    r = await client.get(f"/api/kb/{org_kb_id}/documents", headers=admin_h)
    docs = {d["id"]: d for d in r.json()}
    assert docs[org_doc_id]["status"] == "ready"
    assert docs[org_doc_id]["uploaded_by"] == admin_id  # 最小审计: 谁传的

    # member 视角: 库列表含机构库 (带 org 名, 只读标记)
    r = await client.get("/api/kb", headers=member_h)
    kbs = {k["id"]: k for k in r.json()}
    assert org_kb_id in kbs
    assert kbs[org_kb_id]["owner_type"] == "org"
    assert kbs[org_kb_id]["org_name"] == "清北教研组"
    assert kbs[org_kb_id]["writable"] is False
    # member 只读: 文档列表 + 分块预览可见
    r = await client.get(f"/api/kb/{org_kb_id}/documents", headers=member_h)
    assert r.status_code == 200 and len(r.json()) == 1
    r = await client.get(f"/api/kb/{org_kb_id}/documents/{org_doc_id}/chunks", headers=member_h)
    assert r.status_code == 200 and len(r.json()) >= 1
    # member 可检索 (chat 走 qa 子图)
    events = await _sse_chat(
        client, member_h, {"message": "贝叶斯定理的公式是什么?", "kb_ids": [org_kb_id]}
    )
    assert "done" in events
    # member 写操作全 403 (service 层拦截)
    r = await client.post(
        f"/api/kb/{org_kb_id}/documents",
        files={"file": ("x.md", "# 越权上传".encode(), "text/markdown")},
        headers=member_h,
    )
    assert r.status_code == 403 and r.json()["code"] == "ORG_READONLY"
    r = await client.delete(f"/api/kb/{org_kb_id}/documents/{org_doc_id}", headers=member_h)
    assert r.status_code == 403
    r = await client.post(f"/api/kb/{org_kb_id}/documents/{org_doc_id}/reindex", headers=member_h)
    assert r.status_code == 403
    r = await client.delete(f"/api/kb/{org_kb_id}", headers=member_h)
    assert r.status_code == 403
    # 检索调试面板与写操作同权限: member 403, admin 200
    r = await client.post(
        f"/api/kb/{org_kb_id}/debug/retrieve", json={"query": "贝叶斯"}, headers=member_h
    )
    assert r.status_code == 403
    r = await client.post(
        f"/api/kb/{org_kb_id}/debug/retrieve", json={"query": "贝叶斯公式"}, headers=admin_h
    )
    assert r.status_code == 200 and r.json()["chunks"]

    # ---- ③ RLS 双保险 ----
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KnowledgeBase

    # 非成员 scoped 会话 (org_ids 为空) 查不到机构库行/文档行/分块行
    async with tenant_session(outsider_id, org_ids=()) as s:
        assert await s.get(KnowledgeBase, uuid.UUID(org_kb_id)) is None
        n_docs = await s.scalar(
            select(func.count()).select_from(Document).where(Document.kb_id == uuid.UUID(org_kb_id))
        )
        n_chunks = await s.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.doc_id == uuid.UUID(org_doc_id))
        )
        assert n_docs == 0 and n_chunks == 0
    # 成员 scoped 会话读得到 (org_ids GUC 放行), 但写被 RLS 写策略拦下 (0 行生效)
    async with tenant_session(member_id, org_ids=(org_id,)) as s:
        kb_row = await s.get(KnowledgeBase, uuid.UUID(org_kb_id))
        assert kb_row is not None
        result = await s.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.id == uuid.UUID(org_kb_id))
            .values(name="member 越权改名")
            .execution_options(synchronize_session=False)
        )
        assert result.rowcount == 0  # RLS tenant_write: owner 不匹配 -> 不可见即不可改

    # ---- ④ 联合检索: 个人库 + 机构库 kb_ids 同传 ----
    r = await client.post("/api/kb", json={"name": "我的笔记", "subject": "math"}, headers=member_h)
    personal_kb_id = r.json()["id"]
    await _upload_and_ingest(client, member_h, personal_kb_id, member_id, "taylor.md", _TAYLOR_MD)
    # 无关租户的干扰库 (同术语) — 绝不允许出现在结果里
    r = await client.post("/api/kb", json={"name": "decoy", "subject": "math"}, headers=outsider_h)
    decoy_kb_id = r.json()["id"]
    await _upload_and_ingest(client, outsider_h, decoy_kb_id, outsider_id, "decoy.md", _DECOY_MD)

    # 检索层直接断言: MatchAny 双 filter, 单次查询含两库且只含两库
    from eduagents_core.config import get_settings
    from eduagents_core.embedding.providers import create_embedder
    from eduagents_core.embedding.rerankers import create_reranker
    from eduagents_core.embedding.sparse import Bm25SparseEncoder
    from eduagents_core.retrieval.service import RetrievalService
    from eduagents_core.vectorstore.qdrant_store import QdrantStore

    settings = get_settings()
    service = RetrievalService(
        settings, create_embedder(settings), Bm25SparseEncoder(),
        QdrantStore(settings), create_reranker(settings),
    )
    result = await service.retrieve(
        [member_id, org_id], [personal_kb_id, org_kb_id], "泰勒公式与贝叶斯定理"
    )
    hit_kbs = {c.payload.get("kb_id") for c in result.chunks}
    assert hit_kbs == {personal_kb_id, org_kb_id}, hit_kbs  # 含两库
    assert {c.payload.get("tenant_id") for c in result.chunks} <= {member_id, org_id}
    assert all(c.payload.get("kb_id") != decoy_kb_id for c in result.chunks)  # 无关租户绝缘

    # 端到端: chat 传 kb_ids, 引用带库归属且只来自两库
    events = await _sse_chat(
        client, member_h,
        {"message": "泰勒公式和贝叶斯定理分别是什么?", "kb_ids": [personal_kb_id, org_kb_id]},
    )
    assert "done" in events and "citation" in events
    cited_kbs = {c["kb_id"] for c in events["citation"]}
    assert cited_kbs <= {personal_kb_id, org_kb_id} and len(cited_kbs) == 2
    assert {c["kb_name"] for c in events["citation"]} == {"我的笔记", "统一教材库"}

    # ---- ⑤ 非成员传机构 kb_id -> 403 (kb_ids 与旧 kb_id 两条入参路径都拦) ----
    await _sse_chat(client, outsider_h, {"message": "x", "kb_ids": [org_kb_id]}, expect_status=403)
    await _sse_chat(client, outsider_h, {"message": "x", "kb_id": org_kb_id}, expect_status=403)
    # 混入一个可见库也不放行 (整单 403)
    await _sse_chat(
        client, outsider_h,
        {"message": "x", "kb_ids": [decoy_kb_id, org_kb_id]}, expect_status=403,
    )

    # ---- ⑥ 存量个人库行为回归 (owner_type 默认 user, 旧 kb_id 单值链路不变) ----
    r = await client.get("/api/kb", headers=member_h)
    personal = next(k for k in r.json() if k["id"] == personal_kb_id)
    assert personal["owner_type"] == "user" and personal["writable"] is True
    assert personal["org_id"] is None and personal["org_name"] is None
    events = await _sse_chat(
        client, member_h, {"message": "泰勒公式是什么?", "kb_id": personal_kb_id}
    )
    assert "done" in events and "token" in events
    r = await client.post(
        f"/api/kb/{personal_kb_id}/debug/retrieve", json={"query": "泰勒展开"}, headers=member_h
    )
    assert r.status_code == 200 and r.json()["chunks"]
    # 个人库互相隔离照旧: 他人库 404
    r = await client.get(f"/api/kb/{decoy_kb_id}/documents", headers=member_h)
    assert r.json() == []
