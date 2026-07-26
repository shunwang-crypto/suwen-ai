"""U-A1: 多租户数据隔离 — API 层 + RLS 层 (含 asyncpg 连接池复用路径)."""
import uuid

from sqlalchemy import func, select

from .conftest import requires_services

pytestmark = requires_services


async def _register_login(client, password="pass-12345"):
    email = f"iso-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    r = await client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201
    user_id = r.json()["id"]
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _sse_chat(client, headers, message: str) -> dict:
    """POST /api/chat 并解析 SSE 事件流, 返回 {event_name: [payloads]}."""
    import json

    events: dict[str, list] = {}
    current_event = None
    async with client.stream(
        "POST", "/api/chat", json={"message": message}, headers=headers, timeout=30
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current_event:
                events.setdefault(current_event, []).append(
                    json.loads(line.split(":", 1)[1].strip())
                )
    return events


async def test_tenant_isolation_via_api_and_rls(client):
    user_a, headers_a = await _register_login(client)
    user_b, headers_b = await _register_login(client)

    # A 发起一次 SSE 对话 (stub 网关, 不调真实模型)
    events = await _sse_chat(client, headers_a, "什么是导数?")
    assert "token" in events and "done" in events
    session_id = events["done"][0]["session_id"]
    full_text = "".join(e["text"] for e in events["token"])
    assert "非真实模型输出" in full_text  # stub 模式的诚实标注

    # API 层: A 看得见自己的会话, B 看不见
    r = await client.get("/api/chat/sessions", headers=headers_a)
    assert [s["id"] for s in r.json()] == [session_id]
    r = await client.get("/api/chat/sessions", headers=headers_b)
    assert r.json() == []

    # B 直接拿 A 的 session_id 访问 -> 404 (不泄漏存在性)
    r = await client.get(f"/api/chat/sessions/{session_id}/messages", headers=headers_b)
    assert r.status_code == 404
    r = await client.delete(f"/api/chat/sessions/{session_id}", headers=headers_b)
    assert r.status_code == 404

    # RLS 层: 同一连接池上交替切换租户 GUC, 隔离必须始终成立 (U-A1 连接复用路径)
    from eduagents_api.db import get_sessionmaker, tenant_session
    from eduagents_api.models import ChatSession, UsageLog

    for _ in range(5):
        async with tenant_session(user_a) as s:
            count_a = await s.scalar(select(func.count()).select_from(ChatSession))
        async with tenant_session(user_b) as s:
            count_b = await s.scalar(select(func.count()).select_from(ChatSession))
        assert count_a == 1 and count_b == 0

    # 无租户上下文 (GUC 未设置) -> FORCE RLS 默认拒绝, 0 行
    async with get_sessionmaker()() as s:
        async with s.begin():
            bare = await s.scalar(select(func.count()).select_from(ChatSession))
    assert bare == 0

    # M1-D4: 这次对话的用量按 user/feature 归因落库
    async with tenant_session(user_a) as s:
        rows = (
            await s.scalars(select(UsageLog).where(UsageLog.feature == "chat"))
        ).all()
    assert len(rows) >= 1
    assert str(rows[0].user_id) == user_a
    assert rows[0].mode == "stub"
