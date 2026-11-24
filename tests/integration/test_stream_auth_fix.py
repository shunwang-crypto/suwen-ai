"""会话流与认证安全线集成验收：

- P0-6   流中途断开 → 后台继续生成并落完整答案；显式停止才截断
- 登出漏洞 过期 access token 登出仍 204 且 refresh 被撤销
- P0-1   登录账号维度桶: 同邮箱第 6 次 429, 不同邮箱不受影响
- P1-2   /api/health/ready: Qdrant 不可达 → 503; /live 恒 200
- P1-11  答案缓存作用域 = kb owner (机构库成员共享)
- P0-4   删会话 → 该会话 checkpoint 三表零残留
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from .conftest import requires_services

pytestmark = requires_services


def _email() -> str:
    return f"it-{uuid.uuid4().hex[:12]}@test.eduagents.cn"


PASSWORD = "pass-12345"


@pytest.fixture
async def app_client():
    """同时暴露 app 与 client: 断流测试需要直接驱动 ASGI 协议模拟客户端掉线。"""
    from eduagents_core.config import get_settings

    get_settings.cache_clear()
    from eduagents_api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield app, c


async def _register_login(client: AsyncClient, email: str | None = None) -> tuple[str, str]:
    """返回 (access_token, user_id)。"""
    email = email or _email()
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"], user_id


# ---------------------------------------------------------------------------
# P0-6: 断流不丢答案
# ---------------------------------------------------------------------------


class _DelayedGateway:
    """先下发首段，客户端断开后再完成，用来证明生成任务不依赖 SSE 连接。"""

    async def stream_chat(self, req):
        from eduagents_core.gateway import ChatResult, StreamDelta

        yield StreamDelta(text="勾股定理内容如下。")
        await asyncio.sleep(0.2)
        yield StreamDelta(text="直角三角形两直角边平方和等于斜边平方。")
        yield ChatResult(
            text="勾股定理内容如下。直角三角形两直角边平方和等于斜边平方。",
            model="test-model",
            tier="standard",
            prompt_tokens=10,
            completion_tokens=20,
            cost_cny=0.0,
            latency_ms=200,
            mode="live",
        )


class _HangingGateway:
    """用于显式停止测试：下发部分内容后保持运行，直到 stop 接口取消。"""

    async def stream_chat(self, req):
        from eduagents_core.gateway import StreamDelta

        yield StreamDelta(text="正在生成的前半段。")
        await asyncio.Event().wait()


async def test_stream_disconnect_continues_background_run(app_client):
    app, client = app_client
    token, _uid = await _register_login(client)
    real_gateway = app.state.gateway
    app.state.gateway = _DelayedGateway()
    try:
        body = json.dumps({"message": "讲讲勾股定理"}).encode()
        got_token_event = asyncio.Event()
        disconnect = asyncio.Event()
        sent_body = False

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and b"event: token" in (
                message.get("body") or b""
            ):
                got_token_event.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/chat",
            "raw_path": b"/api/chat",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"test"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"accept", b"text/event-stream"),
                (b"authorization", f"Bearer {token}".encode()),
            ],
            "client": ("127.0.0.1", 40001),
            "server": ("test", 80),
            "state": {},
        }
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(got_token_event.wait(), timeout=30)
        disconnect.set()  # 模拟客户端掉线 (uvicorn 会送 http.disconnect)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=15)
    finally:
        app.state.gateway = real_gateway

    headers = {"Authorization": f"Bearer {token}"}
    # SSE 已断开，但生成任务独立运行；轮询等待完整答案落库。
    assistant = None
    for _ in range(50):
        sessions = (await client.get("/api/chat/sessions", headers=headers)).json()
        if sessions:
            msgs = (
                await client.get(
                    f"/api/chat/sessions/{sessions[0]['id']}/messages", headers=headers
                )
            ).json()
            found = [m for m in msgs if m["role"] == "assistant"]
            if found:
                assistant = found[0]
                break
        await asyncio.sleep(0.1)
    assert assistant is not None, "断流后后台任务必须继续并落库 (P0-6)"
    assert assistant["content"] == (
        "勾股定理内容如下。直角三角形两直角边平方和等于斜边平方。"
    ), assistant
    assert assistant["meta"].get("truncated") is not True, assistant["meta"]


async def test_explicit_stop_cancels_background_run(app_client):
    app, client = app_client
    token, _uid = await _register_login(client)
    real_gateway = app.state.gateway
    app.state.gateway = _HangingGateway()
    body = json.dumps({"message": "写一篇很长的说明"}).encode()
    got_token_event = asyncio.Event()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and b"event: token" in (
            message.get("body") or b""
        ):
            got_token_event.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/chat",
        "raw_path": b"/api/chat",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"accept", b"text/event-stream"),
            (b"authorization", f"Bearer {token}".encode()),
        ],
        "client": ("127.0.0.1", 40002),
        "server": ("test", 80),
        "state": {},
    }
    request_task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(got_token_event.wait(), timeout=30)
        headers = {"Authorization": f"Bearer {token}"}
        sessions = (await client.get("/api/chat/sessions", headers=headers)).json()
        session_id = sessions[0]["id"]
        status = (
            await client.get(
                f"/api/chat/sessions/{session_id}/run", headers=headers
            )
        ).json()
        assert status["status"] == "running"
        stopped = await client.post(
            f"/api/chat/runs/{status['run_id']}/stop", headers=headers
        )
        assert stopped.status_code == 202, stopped.text
        await asyncio.wait_for(request_task, timeout=15)

        assistant = None
        for _ in range(50):
            msgs = (
                await client.get(
                    f"/api/chat/sessions/{session_id}/messages", headers=headers
                )
            ).json()
            found = [m for m in msgs if m["role"] == "assistant"]
            if found:
                assistant = found[0]
                break
            await asyncio.sleep(0.1)
        assert assistant is not None
        assert assistant["content"] == "正在生成的前半段。"
        assert assistant["meta"].get("truncated") is True
    finally:
        app.state.gateway = real_gateway
        if not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# 登出漏洞: 过期 access token 也必须完成撤销
# ---------------------------------------------------------------------------


async def test_logout_with_expired_token_still_revokes_refresh(app_client):
    app, client = app_client
    email = _email()
    _token, user_id = await _register_login(client, email)
    refresh_raw = client.cookies.get("edu_refresh")
    assert refresh_raw

    from eduagents_api.security import create_access_token

    settings = app.state.settings
    expired_token, _jti, _exp = create_access_token(
        settings,
        user_id=uuid.UUID(user_id),
        tenant_id=uuid.UUID(user_id),
        role="student",
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    # 确认该 token 确实已过期 (受保护端点拒绝)
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {expired_token}"}
    )
    assert r.status_code == 401

    # 过期 token 登出: 仍 204, 且 delete_cookie 生效
    r = await client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {expired_token}"}
    )
    assert r.status_code == 204, r.text
    assert "edu_refresh" in r.headers.get("set-cookie", "")

    # refresh cookie 已被撤销: 重放旧 cookie → 401 (共享电脑不再能静默续期进账号)
    import httpx

    client.cookies = httpx.Cookies()
    client.cookies.set("edu_refresh", refresh_raw, path="/api/auth")
    r = await client.post("/api/auth/refresh")
    assert r.status_code == 401, "登出后 refresh 必须失效"


# ---------------------------------------------------------------------------
# P0-1: 登录账号维度桶
# ---------------------------------------------------------------------------


@pytest.fixture
async def strict_auth_app(monkeypatch):
    """账号桶阈值恢复为 5/分钟的独立 app (conftest 默认放宽到 10000)。"""
    monkeypatch.setenv("AUTH_RATE_LIMIT_PER_MINUTE", "5")
    from eduagents_core.config import get_settings

    get_settings.cache_clear()
    from eduagents_api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        yield app
    get_settings.cache_clear()


def _client_for_ip(app, ip: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=(ip, 40000)),
        base_url="http://test",
    )


async def test_login_account_bucket_blocks_same_email_across_ips(strict_auth_app):
    app = strict_auth_app
    # 每次运行用随机网段, 避免真实 Redis 里的历史 IP 桶残留干扰
    seg = uuid.uuid4().int % 200 + 10
    email_victim, email_other = _email(), _email()

    async with _client_for_ip(app, f"10.{seg}.0.1") as c:
        for email in (email_victim, email_other):
            r = await c.post(
                "/api/auth/register", json={"email": email, "password": PASSWORD}
            )
            assert r.status_code == 201, r.text

    # 同一邮箱从 5 个不同 IP 各试错一次: IP 桶全新, 只有账号桶在累计。
    # 前 3 次 401 (坏凭证); 第 4/5 次落入登录防爆破 (连续失败 >=3 次强制图形
    # 验证码, routers/auth.py) → 400 CAPTCHA_REQUIRED — 但账号桶仍在无条件计数
    # (_auth_rate_limit 在验证码检查之前), 第 6 次的 429 断言不受影响。
    for i in range(5):
        async with _client_for_ip(app, f"10.{seg}.1.{i + 1}") as c:
            r = await c.post(
                "/api/auth/login", json={"email": email_victim, "password": "wrong-pw"}
            )
            if i < 3:
                assert r.status_code == 401, f"第{i + 1}次: {r.status_code}"
            else:
                assert r.status_code == 400, f"第{i + 1}次: {r.status_code}"
                assert r.json()["code"] == "CAPTCHA_REQUIRED"

    # 第 6 次换全新 IP 仍应 429 → 证明账号维度桶生效 (纯 IP 桶挡不住)
    async with _client_for_ip(app, f"10.{seg}.1.99") as c:
        r = await c.post(
            "/api/auth/login", json={"email": email_victim, "password": "wrong-pw"}
        )
        assert r.status_code == 429, "同邮箱第 6 次登录尝试必须 429 (账号桶)"

    # 不同邮箱完全不受影响: 新 IP + 正确密码 → 200
    async with _client_for_ip(app, f"10.{seg}.2.1") as c:
        r = await c.post(
            "/api/auth/login", json={"email": email_other, "password": PASSWORD}
        )
        assert r.status_code == 200, f"其他账号不应被牵连: {r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# P1-2: 健康检查拆分
# ---------------------------------------------------------------------------


async def test_ready_returns_503_when_qdrant_unreachable(monkeypatch):
    # 关掉本地嵌入模式并把 Qdrant 指到坏端口 → 就绪探针必须 503
    monkeypatch.setenv("QDRANT_LOCAL_PATH", "")
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:9")
    from eduagents_core.config import get_settings

    get_settings.cache_clear()
    from eduagents_api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/health/ready")
            assert r.status_code == 503, r.text
            body = r.json()
            assert body["checks"]["qdrant"].startswith("error"), body
            assert body["status"] == "down"
            # liveness 与依赖无关, 恒 200
            r = await c.get("/api/health/live")
            assert r.status_code == 200 and r.json()["status"] == "ok"
    get_settings.cache_clear()


async def test_ready_and_legacy_health_ok_with_healthy_deps(client):
    # 本地 Qdrant 模式 + PG/Redis 就绪: ready 200 (worker 未跑时 degraded 但不 503)
    r = await client.get("/api/health/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["qdrant"] == "ok"
    assert body["status"] in ("ok", "degraded")
    # 旧路径兼容: 形状不变
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.headers["Deprecation"] == "true"
    assert r.headers["Link"] == '</api/health/ready>; rel="successor-version"'
    assert r.json()["checks"]["postgres"] == "ok"


# ---------------------------------------------------------------------------
# P1-11: 缓存作用域 = kb owner
# ---------------------------------------------------------------------------


async def test_answer_cache_scope_is_kb_owner():
    from eduagents_core.cache.answer_cache import AnswerCache

    org_id, kb_id = str(uuid.uuid4()), str(uuid.uuid4())
    member_a, member_b = str(uuid.uuid4()), str(uuid.uuid4())
    question = "什么是勾股定理？"

    # 作用域传 kb owner (org id) 时, 两个成员构造出同一个 L1 键 → 缓存共享
    key_via_owner_for_a = AnswerCache.l1_key(org_id, kb_id, 1, question)
    key_via_owner_for_b = AnswerCache.l1_key(org_id, kb_id, 1, question)
    assert key_via_owner_for_a == key_via_owner_for_b

    # 回归警戒: 若作用域仍是请求者 tenant_id, 两成员键必然不同 (跨成员恒不命中)
    assert AnswerCache.l1_key(member_a, kb_id, 1, question) != AnswerCache.l1_key(
        member_b, kb_id, 1, question
    )

    # 源级守卫: chat.py 的 cache.get/put 必须传 kb owner 作用域 (owner_ids[0])
    import inspect

    from eduagents_api.routers import chat_stream as chat_module

    src = inspect.getsource(chat_module)
    assert "cache_scope = owner_ids[0]" in src
    assert re.search(r"cache\.get\(\s*cache_scope", src)
    assert re.search(r"cache\.put\(\s*cache_scope", src)


# ---------------------------------------------------------------------------
# P0-4 (checkpoint 部分): 删会话零残留
# ---------------------------------------------------------------------------


async def test_delete_session_purges_qa_checkpoints(app_client):
    app, client = app_client
    if getattr(app.state, "harness", None) is None:
        pytest.skip("graph runtime 不可用 (DSH/runtime 未就绪)")
    token, user_id = await _register_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.post("/api/kb", json={"name": "ckpt-验收库"}, headers=headers)
    assert r.status_code == 201, r.text
    kb_id = r.json()["id"]

    # 空库提问: qa 图走拒答路径, 但 checkpoint 照常落 PG
    r = await client.post(
        "/api/chat",
        json={"message": "这个库里有什么内容？", "kb_ids": [kb_id]},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    sessions = (await client.get("/api/chat/sessions", headers=headers)).json()
    assert sessions, "提问后应已创建会话"
    session_id = sessions[0]["id"]

    from eduagents_api.db import tenant_session
    from sqlalchemy import text as sql_text

    pattern = f"chat:{user_id}:{session_id}"

    async def _count(table: str) -> int:
        async with tenant_session(user_id) as s:
            return (
                await s.scalar(
                    sql_text(
                        f"SELECT count(*) FROM {table} WHERE thread_id LIKE :p"  # noqa: S608
                    ),
                    {"p": pattern},
                )
            ) or 0

    assert await _count("harness_sessions") > 0, "qa 提问后必须有会话前缀的 checkpoint"
    # thread_id 形如 qa:{uid}:{sid}:{turn_seq}, 首问 turn_seq=1
    async with tenant_session(user_id) as s:
        exact = await s.scalar(
            sql_text("SELECT count(*) FROM harness_sessions WHERE thread_id = :t"),
            {"t": f"chat:{user_id}:{session_id}"},
        )
    assert exact and exact > 0, "thread_id 必须绑定同一原生聊天 session"

    r = await client.delete(f"/api/chat/sessions/{session_id}", headers=headers)
    assert r.status_code == 204, r.text

    for table in ("harness_sessions",):
        assert await _count(table) == 0, f"删会话后 {table} 必须零残留 (P0-4)"
