"""按知识点结构化讲解端点的集成契约 (0033)。

重点守四条：
1. stub 网关下**如实失败**而不是把桩响应当讲解写库（零虚假实现红线）；
2. 每个 block 的出处必须落在本人资料的真实 chunk，编造 id 的块被丢弃；
3. 缓存命中；教学契约版本或资料变更 → 读接口如实标 stale，但**不清空内容**；
4. 重新生成失败时保留已有内容（一次失败不许清空学生正在读的讲解）。
"""

import json
import uuid

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


class FakeExplanationGateway:
    """按给定 payload 返回讲解 JSON (mode=live, 驱动真实生成分支)。"""

    def __init__(self, payload: dict | None, *, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.calls = 0

    async def chat(self, req):
        from eduagents_core.gateway import ChatResult

        self.calls += 1
        text = "这不是 JSON" if self.fail else json.dumps(self.payload, ensure_ascii=False)
        return ChatResult(
            text=text,
            model="fake",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=1,
            mode="live",
        )


async def _register_login(client, prefix="explain"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _seed_kc_with_chunk(user_id: str, kb_id: str, kc_name: str = "导数"):
    """落一个 KC + 一个挂在它上面的 chunk（讲解的出处必须是真实 chunk）。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KnowledgeComponent

    async with tenant_session(uuid.UUID(user_id)) as session:
        kc = KnowledgeComponent(
            tenant_id=uuid.UUID(user_id), kb_id=uuid.UUID(kb_id), canonical_name=kc_name
        )
        session.add(kc)
        doc = Document(
            tenant_id=uuid.UUID(user_id),
            kb_id=uuid.UUID(kb_id),
            filename="微积分讲义.md",
            status="done",
            content_hash=uuid.uuid4().hex,
            size_bytes=10,
        )
        session.add(doc)
        await session.flush()
        chunk = Chunk(
            tenant_id=uuid.UUID(user_id),
            doc_id=doc.id,
            text="导数是函数在某点的瞬时变化率。",
            heading_path="微积分讲义 > 第二章 导数 > 2.1 定义",
            page_start=12,
            kc_ids=[str(kc.id)],
            content_hash=uuid.uuid4().hex,
        )
        session.add(chunk)
        await session.flush()
        return str(kc.id), str(chunk.id)


def _blocks(chunk_id: str) -> dict:
    return {
        "blocks": [
            {
                "type": "concept",
                "title": "导数的定义",
                "content": "导数刻画瞬时变化率。",
                "source_refs": [chunk_id],
            },
            {
                "type": "misconception",
                "title": "常见误解",
                "content": "导数不是平均变化率。",
                "source_refs": [chunk_id],
            },
        ]
    }


async def _create_kb(client, headers):
    r = await client.post("/api/kb", json={"name": "微积分", "subject": "数学"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_stub_gateway_refuses_to_fake_an_explanation(client):
    """stub 桩响应没有 blocks -> 如实 502，且不写库（不把桩当讲解）。"""
    user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    kc_id, _chunk_id = await _seed_kc_with_chunk(user_id, kb_id)

    r = await client.post(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "EXPLANATION_UNAVAILABLE"

    r = await client.get(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
    assert r.status_code == 200
    assert r.json()["available"] is False


async def test_generate_then_cache_then_mark_stale_on_material_change(client):
    user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    kc_id, chunk_id = await _seed_kc_with_chunk(user_id, kb_id)

    app = client._transport.app
    stub_gateway = app.state.gateway
    fake = FakeExplanationGateway(_blocks(chunk_id))
    app.state.gateway = fake
    try:
        r = await client.post(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is True
        assert [block["type"] for block in body["blocks"]] == ["concept", "misconception"]
        assert body["stale"] is False
        assert body["band"] == "unobserved"  # 没有作答证据 -> 不按掌握度讲
        assert body["sources"][0]["id"] == chunk_id

        # 读接口命中缓存, 不再调模型
        before = fake.calls
        r = await client.get(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
        assert r.status_code == 200
        assert r.json()["stale"] is False
        assert fake.calls == before

        # 资料变了 -> context_hash 变 -> 如实标 stale, 但内容仍在
        from eduagents_api.db import tenant_session
        from eduagents_api.models import Chunk

        async with tenant_session(uuid.UUID(user_id)) as session:
            chunk = await session.get(Chunk, uuid.UUID(chunk_id))
            chunk.text = "导数是瞬时变化率, 本节新增了极限定义。"

        r = await client.get(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
        body = r.json()
        assert body["stale"] is True
        assert body["stale_reason"] == "material_changed"
        assert body["blocks"], "stale 不等于清空内容"
    finally:
        app.state.gateway = stub_gateway


async def test_blocks_with_fabricated_source_refs_are_dropped(client):
    """编造 chunk id 的块被丢弃；若一块都不剩则如实失败。"""
    user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    kc_id, chunk_id = await _seed_kc_with_chunk(user_id, kb_id)

    app = client._transport.app
    stub_gateway = app.state.gateway
    app.state.gateway = FakeExplanationGateway(
        {
            "blocks": [
                {
                    "type": "concept",
                    "title": "编造出处",
                    "content": "…",
                    "source_refs": ["00000000-0000-4000-8000-000000000000"],
                },
                {
                    "type": "worked_example",
                    "title": "真实出处",
                    "content": "按定义求导…",
                    "source_refs": [chunk_id],
                },
            ]
        }
    )
    try:
        r = await client.post(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
        assert r.status_code == 200, r.text
        blocks = r.json()["blocks"]
        assert [block["title"] for block in blocks] == ["真实出处"]
    finally:
        app.state.gateway = stub_gateway


async def test_failed_regeneration_keeps_existing_content(client):
    user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    kc_id, chunk_id = await _seed_kc_with_chunk(user_id, kb_id)

    app = client._transport.app
    stub_gateway = app.state.gateway
    try:
        app.state.gateway = FakeExplanationGateway(_blocks(chunk_id))
        assert (
            await client.post(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
        ).status_code == 200

        app.state.gateway = FakeExplanationGateway(None, fail=True)
        r = await client.post(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
        assert r.status_code == 502

        r = await client.get(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
        assert r.status_code == 200
        assert r.json()["blocks"], "一次生成失败不许清空已有讲解"
    finally:
        app.state.gateway = stub_gateway


async def test_kc_without_material_refuses_to_generate(client):
    """没有资料就没有出处可引 —— 409，而不是生成"看起来像讲解"的东西。"""
    user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeComponent

    async with tenant_session(uuid.UUID(user_id)) as session:
        kc = KnowledgeComponent(
            tenant_id=uuid.UUID(user_id), kb_id=uuid.UUID(kb_id), canonical_name="孤立知识点"
        )
        session.add(kc)
        await session.flush()
        kc_id = str(kc.id)

    r = await client.post(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "NO_SOURCE_MATERIAL"


async def test_another_users_kc_explanation_is_not_found(client):
    owner_id, owner_headers = await _register_login(client, "owner")
    kb_id = await _create_kb(client, owner_headers)
    kc_id, _chunk_id = await _seed_kc_with_chunk(owner_id, kb_id)

    _other_id, other_headers = await _register_login(client, "other")
    r = await client.get(f"/api/kb/{kb_id}/kc/{kc_id}/explanation", headers=other_headers)
    assert r.status_code == 404, r.text
