"""F2 Stage 2 (enrich) 集成测试: KC 词表构建→chunk 挂载→上下文前缀→重嵌入。
FakeGateway 驱动 (不打真实 LLM), 但 DB/向量库/归一化全真实。"""
import json
import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

_MD = """# 泰勒公式

## 定义

泰勒公式将函数展开为多项式, 即 $f(x)=\\sum f^{(k)}(x_0)(x-x_0)^k/k!$。

## 拉格朗日余项

余项 $R_n(x)$ 的拉格朗日形式给出误差上界, 依赖导数的连续性。
"""

_VOCAB_JSON = json.dumps(
    {
        "kcs": [
            {
                "canonical_name": "泰勒公式",
                "description": "函数的多项式逼近",
                "aliases": ["Taylor公式", "泰勒展开"],
            },
            {
                "canonical_name": "拉格朗日余项",
                "description": "泰勒逼近的误差形式",
                "aliases": [],
            },
            {"canonical_name": "导数", "description": "变化率", "aliases": []},
        ],
        "prerequisite_edges": [
            {"from": "导数", "to": "泰勒公式"},
            {"from": "泰勒公式", "to": "拉格朗日余项"},
            {"from": "拉格朗日余项", "to": "导数"},
        ],
    },
    ensure_ascii=False,
)


class FakeGateway:
    """按调用形态分派: 词表构建(json+章节结构) / chunk 抽取(json) / 前缀(纯文本)。"""

    def __init__(self):
        self.calls = {"vocab": 0, "extract": 0, "prefix": 0}
        self.extract_prompts = []
        self.return_fallback = False

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        prompt = req.messages[-1].content
        if req.json_mode and "章节结构" in prompt:
            self.calls["vocab"] += 1
            text = _VOCAB_JSON
        elif req.json_mode:
            self.calls["extract"] += 1
            self.extract_prompts.append(prompt)
            body = prompt.split("<文本块>\n", 1)[-1].split("\n</文本块>", 1)[0]
            quote = None
            if self.return_fallback and "未映射片段" in prompt:
                kcs = ["Taylor 公式", "定义"]
                quote = body
            elif "小节候选标题：拉格朗日余项" in prompt:
                kcs = ["导数"]
                quote = body
            else:
                kcs = ["Taylor 公式"]
            text = json.dumps({"kcs": kcs, "section_quote": quote}, ensure_ascii=False)
        else:
            self.calls["prefix"] += 1
            text = "该片段位于泰勒公式章节, 说明多项式逼近的核心结论。"
        return ChatResult(
            text=text, model="fake", tier=req.tier.value, prompt_tokens=10,
            completion_tokens=20, cost_cny=0.0, latency_ms=1, mode="live",
        )


async def _register_login(client):
    email = f"enr-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    await client.post("/api/auth/register", json={"email": email, "password": "pass-12345"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "pass-12345"})
    return r.json()["access_token"]


async def test_enrich_stage_full_cycle(client):
    token = await _register_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/kb", json={"name": "enrich库", "subject": "math"}, headers=headers)
    kb_id = r.json()["id"]
    r = await client.post(
        f"/api/kb/{kb_id}/documents",
        files={"file": ("taylor.md", _MD.encode(), "text/markdown")},
        headers=headers,
    )
    doc_id = r.json()["id"]

    from eduagents_api.deps import AuthContext  # noqa: F401 (确保应用已加载)
    from eduagents_worker.enrichment import enrich_document
    from eduagents_worker.ingestion import ingest_document

    me = await client.get("/api/auth/me", headers=headers)
    tenant_id = me.json()["id"]
    assert await ingest_document({}, tenant_id, doc_id) == "ready"

    gw = FakeGateway()
    outcome = await enrich_document({}, tenant_id, doc_id, gateway=gw)
    assert outcome == "enriched"
    assert gw.calls["vocab"] == 1 and gw.calls["extract"] >= 1 and gw.calls["prefix"] >= 1

    # 文档状态推进到 enriched
    r = await client.get(f"/api/kb/{kb_id}/documents", headers=headers)
    assert r.json()[0]["status"] == "enriched"

    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, KcEdge, KnowledgeComponent

    async with tenant_session(tenant_id) as session:
        kcs = (
            await session.scalars(
                select(KnowledgeComponent).where(
                    KnowledgeComponent.kb_id == uuid.UUID(kb_id)
                )
            )
        ).all()
        kc_ids = {str(k.id) for k in kcs}
        edges = (await session.scalars(select(KcEdge))).all()
        children = (
            await session.scalars(
                select(Chunk).where(
                    Chunk.doc_id == uuid.UUID(doc_id), Chunk.parent_id.isnot(None)
                )
            )
        ).all()

    # 语义词表落库 (3 个 enrichment KC)。stub 摄取留下的未命中章节
    # fallback 不能被静默删除，否则已有题目/画像证据会断链。
    enrichment_names = {
        k.canonical_name for k in kcs if k.source_kind == "enrichment"
    }
    fallback_names = {
        k.canonical_name for k in kcs if k.source_kind == "chapter_fallback"
    }
    assert {"泰勒公式", "拉格朗日余项", "导数"} <= enrichment_names
    assert "定义" in fallback_names
    assert gw.extract_prompts
    assert all(f"- {name}" in gw.extract_prompts[0] for name in ("泰勒公式", "导数"))
    assert any("小节候选标题：拉格朗日余项" in prompt for prompt in gw.extract_prompts)
    # KC-A1 归一化路径: chunk 抽取返回 "Taylor 公式" 但挂载的是已有"泰勒公式"的 kc_id, 无新建
    assert len(enrichment_names) == 3
    # 成环边 (拉格朗日余项→导数) 被丢弃: 只入 2 条
    assert len(edges) == 2
    # 每个子块 kc_ids 挂载且外键有效 (禁止裸字符串)
    assert children
    for c in children:
        assert c.kc_ids, "chunk 应挂载 KC"
        assert set(c.kc_ids) <= kc_ids
        assert c.context_prefix  # 前缀已生成

    fallback = next(k for k in kcs if k.canonical_name == "定义")
    parent_id = children[0].parent_id
    async with tenant_session(tenant_id) as session:
        session.add(
            Chunk(
                tenant_id=uuid.UUID(tenant_id),
                doc_id=uuid.UUID(doc_id),
                parent_id=parent_id,
                text="未映射片段",
                heading_path="定义",
                kc_ids=[str(fallback.id)],
                content_hash=uuid.uuid4().hex,
                embedding_status="done",
            )
        )
    gw.return_fallback = True
    assert await enrich_document({}, tenant_id, doc_id, gateway=gw) == "enriched"
    async with tenant_session(tenant_id) as session:
        promoted = await session.get(KnowledgeComponent, fallback.id)
    assert promoted.source_kind == "chapter_fallback"
