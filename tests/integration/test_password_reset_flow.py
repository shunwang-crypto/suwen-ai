"""图形验证码 + 登录防爆破 + 密码找回 全链路集成测试。

覆盖: captcha 端点形状 / register 验证码开关 (含测试通道) / login 3 次失败后
强制验证码 / forgot 恒定 204 + token 落库 + 旧 token 作废 / reset 改密 +
旧 refresh 全部吊销 + token 单次性与过期。

表依赖协调: password_reset_tokens 由计费线迁移 0006 建; 0006 未落地时由
本文件的 _ensure_reset_table 夹具按同一结构 checkfirst 兜底建出 (migration
角色执行, 0001 的 default privileges 自动授权 edu_app), 0006 落地后自动
变成空操作 — 两条并行开发线互不阻塞。
"""
import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select, text

from .conftest import requires_services

pytestmark = requires_services


def _email() -> str:
    return f"it-{uuid.uuid4().hex[:12]}@test.eduagents.cn"


async def _mark_service(session) -> None:
    """password_reset_tokens 的 RLS 服务层 GUC (0006): 测试直读/直插同样要设。"""
    await session.execute(
        text("SELECT set_config('app.billing_service', 'true', true)")
    )


@pytest.fixture(scope="module", autouse=True)
def _ensure_reset_table():
    """0006 (计费线) 未落地时兜底建表 (checkfirst, 结构=ORM 定义=迁移约定)。"""
    if os.environ.get("INTEGRATION") != "1":
        yield
        return

    async def _create():
        from eduagents_api.models import PasswordResetToken
        from eduagents_core.config import Settings
        from sqlalchemy.ext.asyncio import create_async_engine

        s = Settings()
        engine = create_async_engine(s.migration_database_url or s.database_url)
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: PasswordResetToken.__table__.create(c, checkfirst=True)
            )
        await engine.dispose()

    asyncio.run(_create())
    yield


@pytest.fixture
async def raw_redis():
    """直连 Redis: 从服务端取验证码答案/调试链接 (测试专用, 不走 HTTP)。"""
    from eduagents_core.config import Settings
    from redis.asyncio import Redis

    r = Redis.from_url(Settings().redis_url, decode_responses=True)
    yield r
    await r.aclose()


async def _solve_captcha(client, raw_redis) -> tuple[str, str]:
    """取一张验证码并从 Redis 读出答案 (答案不在任何 HTTP 响应里)。"""
    r = await client.get("/api/auth/captcha")
    assert r.status_code == 200, r.text
    captcha_id = r.json()["captcha_id"]
    answer = await raw_redis.get(f"captcha:{captcha_id}")
    assert answer and answer.isdigit()
    return captcha_id, answer


def _make_env_client(monkeypatch, **env: str):
    """带环境覆盖的独立 app 客户端 (settings 缓存先清后建)。"""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from eduagents_core.config import get_settings

    get_settings.cache_clear()
    from eduagents_api.main import create_app

    return create_app()


# ---------- 验证码端点 ----------


async def test_captcha_endpoint_shape(client, raw_redis):
    r = await client.get("/api/auth/captcha")
    assert r.status_code == 200
    body = r.json()
    uuid.UUID(body["captcha_id"])
    assert body["image"].startswith("data:image/png;base64,")
    # 答案已入 Redis, TTL <= 300s
    ttl = await raw_redis.ttl(f"captcha:{body['captcha_id']}")
    assert 0 < ttl <= 300


# ---------- 注册验证码开关 ----------


async def test_register_captcha_enforced_when_enabled(monkeypatch, raw_redis):
    """CAPTCHA_REQUIRED_REGISTER=1: 缺/错验证码拒绝 (CAPTCHA_INVALID), 答对放行。"""
    app = _make_env_client(monkeypatch, CAPTCHA_REQUIRED_REGISTER="1")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            email = _email()
            # 不带验证码 -> 400
            r = await c.post(
                "/api/auth/register", json={"email": email, "password": "pass-12345"}
            )
            assert r.status_code == 400 and r.json()["code"] == "CAPTCHA_INVALID"

            # 答错 -> 400 (且校验即删: 同一张图答对也不能复用)
            captcha_id, answer = await _solve_captcha(c, raw_redis)
            wrong = "0000" if answer != "0000" else "1111"
            r = await c.post(
                "/api/auth/register",
                json={
                    "email": email,
                    "password": "pass-12345",
                    "captcha_id": captcha_id,
                    "captcha_answer": wrong,
                },
            )
            assert r.status_code == 400 and r.json()["code"] == "CAPTCHA_INVALID"
            r = await c.post(
                "/api/auth/register",
                json={
                    "email": email,
                    "password": "pass-12345",
                    "captcha_id": captcha_id,
                    "captcha_answer": answer,  # 答案正确但 key 已被上一次校验删除
                },
            )
            assert r.status_code == 400 and r.json()["code"] == "CAPTCHA_INVALID"

            # 新验证码答对 -> 201
            captcha_id, answer = await _solve_captcha(c, raw_redis)
            r = await c.post(
                "/api/auth/register",
                json={
                    "email": email,
                    "password": "pass-12345",
                    "captcha_id": captcha_id,
                    "captcha_answer": answer,
                },
            )
            assert r.status_code == 201, r.text


async def test_register_captcha_test_env_channel(monkeypatch):
    """APP_ENV=test 通道: 即使开关打开, 注册也不要求验证码 (既有测试保障)。"""
    app = _make_env_client(
        monkeypatch, CAPTCHA_REQUIRED_REGISTER="1", APP_ENV="test"
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/api/auth/register",
                json={"email": _email(), "password": "pass-12345"},
            )
            assert r.status_code == 201, r.text


# ---------- 登录防爆破 ----------


async def test_login_requires_captcha_after_three_failures(client, raw_redis):
    email = _email()
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 201

    # 连续 3 次错密码 -> 计数器到阈值 (每次都还是 401)
    for _ in range(3):
        r = await client.post(
            "/api/auth/login", json={"email": email, "password": "wrong-pass"}
        )
        assert r.status_code == 401

    # 第 4 次: 即使密码正确, 未带验证码 -> 400 CAPTCHA_REQUIRED
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 400 and r.json()["code"] == "CAPTCHA_REQUIRED"

    # 带了但答错 -> 400 CAPTCHA_INVALID
    captcha_id, answer = await _solve_captcha(client, raw_redis)
    wrong = "0000" if answer != "0000" else "1111"
    r = await client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": "pass-12345",
            "captcha_id": captcha_id,
            "captcha_answer": wrong,
        },
    )
    assert r.status_code == 400 and r.json()["code"] == "CAPTCHA_INVALID"

    # 验证码答对 + 密码正确 -> 200, 且失败计数清零
    captcha_id, answer = await _solve_captcha(client, raw_redis)
    r = await client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": "pass-12345",
            "captcha_id": captcha_id,
            "captcha_answer": answer,
        },
    )
    assert r.status_code == 200, r.text

    # 清零后: 无验证码直接登录成功
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 200


# ---------- 密码找回: forgot ----------


async def test_forgot_constant_204_and_token_persisted(client, raw_redis):
    from eduagents_api.db import plain_session
    from eduagents_api.models import PasswordResetToken, User

    email = _email()
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 201
    user_id = uuid.UUID(r.json()["id"])

    # 验证码答错 -> 400 (不触发发信/落库)
    captcha_id, answer = await _solve_captcha(client, raw_redis)
    wrong = "0000" if answer != "0000" else "1111"
    r = await client.post(
        "/api/auth/forgot",
        json={"email": email, "captcha_id": captcha_id, "captcha_answer": wrong},
    )
    assert r.status_code == 400 and r.json()["code"] == "CAPTCHA_INVALID"

    # 已注册邮箱 -> 204, token sha256 落库 (30min 过期, 未用)
    captcha_id, answer = await _solve_captcha(client, raw_redis)
    r = await client.post(
        "/api/auth/forgot",
        json={"email": email, "captcha_id": captcha_id, "captcha_answer": answer},
    )
    assert r.status_code == 204 and r.content == b""
    async with plain_session() as session:
        await _mark_service(session)
        rows = (
            await session.scalars(
                select(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
            )
        ).all()
    assert len(rows) == 1 and rows[0].used_at is None
    assert len(rows[0].token_hash) == 64  # sha256 hex, 非原文
    delta = rows[0].expires_at.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(minutes=28) < delta <= timedelta(minutes=30)
    first_hash = rows[0].token_hash

    # 重复申请 -> 旧未用 token 作废删除, 只留最新一条
    captcha_id, answer = await _solve_captcha(client, raw_redis)
    r = await client.post(
        "/api/auth/forgot",
        json={"email": email, "captcha_id": captcha_id, "captcha_answer": answer},
    )
    assert r.status_code == 204
    async with plain_session() as session:
        await _mark_service(session)
        rows = (
            await session.scalars(
                select(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
            )
        ).all()
    assert len(rows) == 1 and rows[0].token_hash != first_hash

    # 未注册邮箱 -> 同样 204 (防枚举), 且不落任何新行
    ghost = _email()
    captcha_id, answer = await _solve_captcha(client, raw_redis)
    r = await client.post(
        "/api/auth/forgot",
        json={"email": ghost, "captcha_id": captcha_id, "captcha_answer": answer},
    )
    assert r.status_code == 204 and r.content == b""
    async with plain_session() as session:
        await _mark_service(session)
        ghost_user = await session.scalar(select(User).where(User.email == ghost))
    assert ghost_user is None


# ---------- 密码找回: reset (dev 调试链路 e2e) ----------


async def test_forgot_reset_e2e_via_dev_debug_link(monkeypatch, raw_redis):
    """APP_ENV=dev + SMTP 未配置: DevEmailSender 把链接写 debug:reset:{email},
    据此走完 forgot -> reset -> 新密码登录 的完整用户路径 (token 不出 HTTP)。"""
    app = _make_env_client(monkeypatch, APP_ENV="dev")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            email = _email()
            r = await c.post(
                "/api/auth/register", json={"email": email, "password": "old-pass-123"}
            )
            assert r.status_code == 201

            captcha_id, answer = await _solve_captcha(c, raw_redis)
            r = await c.post(
                "/api/auth/forgot",
                json={
                    "email": email,
                    "captcha_id": captcha_id,
                    "captcha_answer": answer,
                },
            )
            assert r.status_code == 204
            # token 不在响应里; dev 调试链接在 Redis (仅 APP_ENV=dev)
            link = await raw_redis.get(f"debug:reset:{email}")
            assert link and "/reset?token=" in link
            token = parse_qs(urlparse(link).query)["token"][0]

            # 改密 -> 204
            r = await c.post(
                "/api/auth/reset",
                json={"token": token, "new_password": "new-pass-456"},
            )
            assert r.status_code == 204, r.text

            # 旧密码失效, 新密码可登录
            r = await c.post(
                "/api/auth/login", json={"email": email, "password": "old-pass-123"}
            )
            assert r.status_code == 401
            r = await c.post(
                "/api/auth/login", json={"email": email, "password": "new-pass-456"}
            )
            assert r.status_code == 200, r.text

            # token 单次性: 二次使用 -> 400
            r = await c.post(
                "/api/auth/reset",
                json={"token": token, "new_password": "another-pass-789"},
            )
            assert r.status_code == 400 and r.json()["code"] == "RESET_TOKEN_INVALID"


async def test_reset_revokes_all_refresh_tokens(client):
    """改密后既有登录态全部失效: 旧 refresh cookie 换不出新 access token。"""
    from eduagents_api.db import plain_session
    from eduagents_api.models import PasswordResetToken, User
    from eduagents_api.security import new_password_reset_token

    email = _email()
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 201
    user_id = uuid.UUID(r.json()["id"])
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 200
    old_refresh = client.cookies.get("edu_refresh")
    assert old_refresh

    # 直插一条有效 reset token (等价于走过 forgot; e2e 邮件链路已另测)
    raw, token_hash = new_password_reset_token()
    async with plain_session() as session:
        await _mark_service(session)
        session.add(
            PasswordResetToken(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
    r = await client.post(
        "/api/auth/reset", json={"token": raw, "new_password": "new-pass-456"}
    )
    assert r.status_code == 204

    # 旧 refresh cookie 已被吊销 -> 401
    client.cookies = httpx.Cookies()
    client.cookies.set("edu_refresh", old_refresh, path="/api/auth")
    r = await client.post("/api/auth/refresh")
    assert r.status_code == 401

    # used_at 已标记
    async with plain_session() as session:
        await _mark_service(session)
        row = await session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == token_hash
            )
        )
        user = await session.get(User, user_id)
    assert row is not None and row.used_at is not None
    assert user is not None


async def test_reset_rejects_expired_and_unknown_token(client):
    from eduagents_api.db import plain_session
    from eduagents_api.models import PasswordResetToken
    from eduagents_api.security import new_password_reset_token

    email = _email()
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 201
    user_id = uuid.UUID(r.json()["id"])

    # 过期 token -> 400
    raw, token_hash = new_password_reset_token()
    async with plain_session() as session:
        await _mark_service(session)
        session.add(
            PasswordResetToken(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    r = await client.post(
        "/api/auth/reset", json={"token": raw, "new_password": "new-pass-456"}
    )
    assert r.status_code == 400 and r.json()["code"] == "RESET_TOKEN_INVALID"

    # 凭空伪造 token -> 400; 密码未被改动
    r = await client.post(
        "/api/auth/reset",
        json={"token": "forged-" + uuid.uuid4().hex, "new_password": "new-pass-456"},
    )
    assert r.status_code == 400 and r.json()["code"] == "RESET_TOKEN_INVALID"
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 200
