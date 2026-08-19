"""库级 KC 知识图端点 (Learning Map full 模式) 的集成契约。

覆盖: 鉴权与租户隔离 (他人库 404)、KC/先修边真实落库后出境、掌握度数值不出境、
无 KC 的库返回空图。stub 网关 + 真实 PG/Redis。
"""

import uuid

from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services

PASSWORD = "pass-12345"


async def _register_login(client, prefix="kcgraph"):
    email = f"{prefix}-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _create_kb(client, headers, name="微积分"):
    r = await client.post("/api/kb", json={"name": name, "subject": "数学"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_kcs(user_id: str, kb_id: str, names: list[str]) -> list[str]:
    """直接落 KC 与先修边 (本端点只读, 不依赖 enrich 全链路)。"""
    from eduagents_api.db import tenant_session
    from eduagents_api.models import KcEdge, KnowledgeComponent

    ids: list[str] = []
    async with tenant_session(uuid.UUID(user_id)) as session:
        for name in names:
            kc = KnowledgeComponent(
                tenant_id=uuid.UUID(user_id), kb_id=uuid.UUID(kb_id), canonical_name=name
            )
            session.add(kc)
            await session.flush()
            ids.append(str(kc.id))
        for from_id, to_id in zip(ids, ids[1:], strict=False):
            session.add(
                KcEdge(
                    tenant_id=uuid.UUID(user_id),
                    from_kc=uuid.UUID(from_id),
                    to_kc=uuid.UUID(to_id),
                    type="prerequisite",
                )
            )
    return ids


async def test_kc_graph_returns_real_kcs_and_prerequisite_edges(client):
    user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    ids = await _seed_kcs(user_id, kb_id, ["极限", "导数", "积分"])

    r = await client.get(f"/api/kb/{kb_id}/kc-graph", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kb_id"] == kb_id
    assert body["kc_count"] == 3
    # 同一事务写入的 KC created_at 相同, 稳定序由 (created_at, id) 兜底 —— 是
    # 确定性的但不等于插入序, 所以断言集合 + 两次调用同序 (可重复渲染)。
    assert {node["kc_name"] for node in body["nodes"]} == {"极限", "导数", "积分"}
    again = await client.get(f"/api/kb/{kb_id}/kc-graph", headers=headers)
    assert [node["kc_id"] for node in again.json()["nodes"]] == [
        node["kc_id"] for node in body["nodes"]
    ]
    assert sorted(
        (edge["from_kc_id"], edge["to_kc_id"]) for edge in body["edges"]
    ) == sorted([(ids[0], ids[1]), (ids[1], ids[2])])
    # 没有任何作答证据时一律 unobserved, 且不给定性档
    assert {node["evidence_state"] for node in body["nodes"]} == {"unobserved"}
    assert all(node["band"] is None for node in body["nodes"])
    # 章节推导兜底后仍是显式桶, 不是空串
    assert all(node["chapter"] for node in body["nodes"])
    assert body["chapter_count"] >= 1


async def test_kc_graph_never_leaks_mastery_numbers(client):
    user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    ids = await _seed_kcs(user_id, kb_id, ["极限"])

    from eduagents_api.db import tenant_session
    from eduagents_api.models import MasteryRecord

    async with tenant_session(uuid.UUID(user_id)) as session:
        session.add(
            MasteryRecord(
                tenant_id=uuid.UUID(user_id),
                user_id=uuid.UUID(user_id),
                kc_id=uuid.UUID(ids[0]),
                p_mastery=0.7345,
            )
        )

    r = await client.get(f"/api/kb/{kb_id}/kc-graph", headers=headers)
    assert r.status_code == 200, r.text
    assert "0.7345" not in r.text
    assert "p_mastery" not in r.text
    # 只有一条 MasteryRecord 而没有有效 MasteryLog 证据 -> unobserved, 不给档
    node = r.json()["nodes"][0]
    assert node["evidence_state"] == "unobserved"
    assert node["band"] is None


async def test_kc_graph_of_another_users_kb_is_not_found(client):
    owner_id, owner_headers = await _register_login(client, "owner")
    kb_id = await _create_kb(client, owner_headers)
    await _seed_kcs(owner_id, kb_id, ["极限"])

    _other_id, other_headers = await _register_login(client, "other")
    r = await client.get(f"/api/kb/{kb_id}/kc-graph", headers=other_headers)
    assert r.status_code == 404, r.text


async def test_kc_graph_of_empty_kb_is_an_empty_graph(client):
    _user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    r = await client.get(f"/api/kb/{kb_id}/kc-graph", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kc_count"] == 0
    assert body["nodes"] == [] and body["edges"] == [] and body["chapters"] == []


async def test_superseded_kcs_are_excluded(client):
    user_id, headers = await _register_login(client)
    kb_id = await _create_kb(client, headers)
    ids = await _seed_kcs(user_id, kb_id, ["极限", "导数"])

    from eduagents_api.db import tenant_session
    from eduagents_api.models import KnowledgeComponent

    async with tenant_session(uuid.UUID(user_id)) as session:
        merged = await session.scalar(
            select(KnowledgeComponent).where(KnowledgeComponent.id == uuid.UUID(ids[0]))
        )
        merged.superseded_by_id = uuid.UUID(ids[1])

    r = await client.get(f"/api/kb/{kb_id}/kc-graph", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [node["kc_id"] for node in body["nodes"]] == [ids[1]]
    # 归并掉的 KC 参与的边也不再出境 (端点只取存活 KC 的边)
    assert body["edges"] == []
