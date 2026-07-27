"""预付额度计费端到端 (INTEGRATION=1, 真 PG/Redis):

- 注册赠送: 与建号同事务, 唯一部分索引保幂等 (重放不重发);
- consume: usage_log 同事务落账 (persist_usage 接线点), Decimal 精确 ×3,
  同 request_id 重试不双扣, cost<=0 (stub/缓存命中) 零扣费;
- 余额 <=0: WalletService.precheck_balance 抛 InsufficientCredits, 组合 guard
  在 live 网关入口拦截 (未发出任何网络调用), API 层 402 信封含 balance;
- mock 支付: 建单白名单 -> mock-pay 入账 20.0000 -> 重复确认 409 (不双入账),
  prod 环境 mock-pay 403;
- RLS 越权: 他人钱包/流水在本人租户会话里是空集; 无 GUC 会话默认拒绝;
- wallet_ledger append-only: edu_app 的 UPDATE 权限已被收回;
- password_reset_tokens: 建表/唯一约束/仅服务层可读 (C 路密码找回用表)。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from .conftest import requires_services

pytestmark = requires_services


def _email() -> str:
    return f"bill-{uuid.uuid4().hex[:12]}@test.eduagents.cn"


async def _register_and_login(client) -> tuple[uuid.UUID, dict]:
    """注册+登录 -> (user_id, auth headers)。"""
    email = _email()
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 201, r.text
    user_id = uuid.UUID(r.json()["id"])
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 200, r.text
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _usage_event(user_id: uuid.UUID, cost: float, request_id: str, mode: str = "live"):
    from eduagents_core.gateway import UsageEvent

    return UsageEvent(
        request_id=request_id,
        tenant_id=str(user_id),
        user_id=str(user_id),
        feature="chat",
        model="test-model",
        tier="standard",
        prompt_tokens=10,
        completion_tokens=20,
        cached_tokens=0,
        cost_cny=cost,
        latency_ms=5,
        mode=mode,
    )


async def _ledger_rows(user_id: uuid.UUID):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import WalletLedger

    async with tenant_session(user_id) as session:
        return (
            await session.scalars(
                select(WalletLedger)
                .where(WalletLedger.user_id == user_id)
                .order_by(WalletLedger.created_at)
            )
        ).all()


# ---------- 注册赠送 ----------


async def test_signup_grant_and_frozen_contract_shapes(client):
    user_id, headers = await _register_and_login(client)

    # GET /wallet — 契约冻结形状
    r = await client.get("/api/billing/wallet", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"balance": "10.0000", "currency": "CNY"}

    # GET /ledger — signup_grant 单行
    r = await client.get("/api/billing/ledger?limit=50", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 1
    entry = data[0]
    assert set(entry) == {"id", "delta", "balance_after", "kind", "ref", "note", "created_at"}
    assert entry["kind"] == "signup_grant"
    assert entry["delta"] == "10.0000"
    assert entry["balance_after"] == "10.0000"

    # GET /pricing — 契约冻结形状
    r = await client.get("/api/billing/pricing", headers=headers)
    assert r.status_code == 200
    assert r.json() == {
        "markup": 3.0,
        "signup_grant": "10.00",
        "note": "按模型真实成本×3 透明计费, 缓存命中免费",
    }


async def test_signup_grant_idempotent(client):
    from eduagents_api.wallet import get_wallet_service, service_session

    user_id, headers = await _register_and_login(client)
    ws = get_wallet_service()
    # 重放赠送 (模拟注册重试): 唯一部分索引兜底, 余额不动、不加行
    async with service_session() as session:
        balance = await ws.grant_signup(session, user_id)
    assert balance == Decimal("10.0000")
    rows = await _ledger_rows(user_id)
    assert [r.kind for r in rows] == ["signup_grant"]

    r = await client.get("/api/billing/wallet", headers=headers)
    assert r.json()["balance"] == "10.0000"


# ---------- consume: usage_log 同事务 + 幂等 + Decimal ----------


async def test_consume_in_usage_log_transaction_and_retry_idempotent(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import UsageLog
    from eduagents_api.usage import persist_usage
    from eduagents_api.wallet import get_wallet_service

    user_id, headers = await _register_and_login(client)

    # 一次 live 调用: 成本 0.1 -> 扣 0.3000 (×3), usage_log 与账本同事务落库
    await persist_usage(_usage_event(user_id, 0.1, "bill-req-1"))
    balance = await get_wallet_service().balance(user_id)
    assert balance == Decimal("9.7000")
    async with tenant_session(user_id) as session:
        usage_count = len(
            (
                await session.scalars(
                    select(UsageLog).where(UsageLog.request_id == "bill-req-1")
                )
            ).all()
        )
    assert usage_count == 1

    # 同 request_id 重放: 不双扣 (唯一部分索引 (user_id, ref) WHERE kind='consume')
    await persist_usage(_usage_event(user_id, 0.1, "bill-req-1"))
    assert await get_wallet_service().balance(user_id) == Decimal("9.7000")

    # 新 request_id: 正常扣; float 噪声成本 (0.1+0.2) 必须精确收敛
    await persist_usage(_usage_event(user_id, 0.1 + 0.2, "bill-req-2"))
    assert await get_wallet_service().balance(user_id) == Decimal("8.8000")

    consume_rows = [r for r in await _ledger_rows(user_id) if r.kind == "consume"]
    assert len(consume_rows) == 2
    assert {r.ref for r in consume_rows} == {"bill-req-1", "bill-req-2"}
    assert consume_rows[0].delta == Decimal("-0.3000")

    # 流水接口按时间倒序可见
    r = await client.get("/api/billing/ledger", headers=headers)
    kinds = [e["kind"] for e in r.json()["data"]]
    assert kinds == ["consume", "consume", "signup_grant"]


async def test_zero_cost_is_free(client):
    """stub/replay/缓存命中 (cost_cny=0) 零扣费 — 演示口径: 缓存命中免费。"""
    from eduagents_api.usage import persist_usage
    from eduagents_api.wallet import get_wallet_service

    user_id, _ = await _register_and_login(client)
    await persist_usage(_usage_event(user_id, 0.0, "bill-free-1", mode="stub"))
    await persist_usage(_usage_event(user_id, 0.0, "bill-free-2", mode="cache"))
    assert await get_wallet_service().balance(user_id) == Decimal("10.0000")
    assert [r.kind for r in await _ledger_rows(user_id)] == ["signup_grant"]


# ---------- 余额不足: 入口拦截 (先钱包后预算) ----------


async def test_insufficient_credits_blocks_next_request(client):
    from eduagents_api.usage import persist_usage
    from eduagents_api.wallet import InsufficientCredits, get_wallet_service

    user_id, _ = await _register_and_login(client)
    ws = get_wallet_service()

    # 进行中的大额调用允许扣到负值, 不打断 (成本 4 -> 扣 12 -> 余额 -2)
    await persist_usage(_usage_event(user_id, 4.0, "bill-big-1"))
    assert await ws.balance(user_id) == Decimal("-2.0000")

    # 下一次请求在入口 precheck 被拦: 402 语义异常携带当前余额
    with pytest.raises(InsufficientCredits) as ei:
        await ws.precheck_balance(str(user_id))
    assert ei.value.balance == Decimal("-2.0000")
    assert ei.value.code == "INSUFFICIENT_CREDITS"


async def test_insufficient_credits_intercepts_gateway_before_network(client):
    """live 网关 + 不可达端点: 钱包拦截必须发生在任何网络调用之前 (同 budget e2e 口径)。"""
    from eduagents_api.wallet import InsufficientCredits, create_billing_guard, get_wallet_service
    from eduagents_core.config import Settings
    from eduagents_core.gateway import ChatMessage, ChatRequest, OpenAIGateway, Tier
    from redis.asyncio import Redis

    user_id, _ = await _register_and_login(client)
    # 调负余额 (admin_adjust 服务层通道)
    await get_wallet_service().admin_adjust(user_id, Decimal("-15"), note="测试调负")
    assert await get_wallet_service().balance(user_id) == Decimal("-5.0000")

    settings = Settings(
        _env_file=None,
        gateway_mode="live",
        app_env="dev",
        gateway_max_retries=1,
        gateway_timeout_seconds=1.0,
        standard_api_key="fake-key",
        standard_base_url="http://127.0.0.1:9",
    )
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        gateway = OpenAIGateway(
            settings, budget_guard=create_billing_guard(redis, settings)
        )
        req = ChatRequest(
            messages=(ChatMessage(role="user", content="你好"),),
            tier=Tier.standard,
            tenant_id=str(user_id),
            user_id=str(user_id),
            request_id="bill-blocked-1",
        )
        with pytest.raises(InsufficientCredits):
            await gateway.chat(req)
    finally:
        await redis.aclose()


# ---------- mock 支付 ----------


async def test_mock_payment_full_flow(client):
    user_id, headers = await _register_and_login(client)

    # 面额白名单外 -> 422 (统一错误信封)
    r = await client.post("/api/billing/orders", json={"amount_cny": 5}, headers=headers)
    assert r.status_code == 422
    assert r.json()["code"] == "AMOUNT_NOT_ALLOWED"

    # 建单 — 契约冻结形状
    r = await client.post("/api/billing/orders", json={"amount_cny": 10}, headers=headers)
    assert r.status_code == 201, r.text
    order = r.json()
    assert set(order) == {"id", "amount_cny", "credits", "status", "provider"}
    assert order["status"] == "pending" and order["provider"] == "mock"
    assert order["amount_cny"] == "10.00" and order["credits"] == "10.0000"

    # mock 确认 -> 入账 (10 赠送 + 10 充值)
    r = await client.post(f"/api/billing/orders/{order['id']}/mock-pay", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "paid", "balance": "20.0000"}

    # 账本如实标注演示通道, ref = 订单号
    rows = await _ledger_rows(user_id)
    recharge = [x for x in rows if x.kind == "recharge"]
    assert len(recharge) == 1
    assert recharge[0].note == "模拟支付-演示通道"
    assert recharge[0].ref == order["id"]
    assert recharge[0].delta == Decimal("10.0000")

    # 重复确认 -> 409, 不双入账 (FOR UPDATE + pending 闸)
    r = await client.post(f"/api/billing/orders/{order['id']}/mock-pay", headers=headers)
    assert r.status_code == 409
    assert r.json()["code"] == "ORDER_NOT_PENDING"
    r = await client.get("/api/billing/wallet", headers=headers)
    assert r.json()["balance"] == "20.0000"

    # 他人订单 -> 404 (不泄漏存在性)
    _, other_headers = await _register_and_login(client)
    r = await client.post(
        f"/api/billing/orders/{order['id']}/mock-pay", headers=other_headers
    )
    assert r.status_code == 404


async def test_mock_pay_forbidden_in_prod(app_client):
    from eduagents_api.deps import get_app_settings
    from eduagents_core.config import get_settings

    app, client = app_client
    _, headers = await _register_and_login(client)
    r = await client.post("/api/billing/orders", json={"amount_cny": 10}, headers=headers)
    assert r.status_code == 201
    order_id = r.json()["id"]

    # 复制现网 settings 仅改 app_env (jwt_secret 等保持一致, token 仍可解)
    prod_settings = get_settings().model_copy(update={"app_env": "prod"})
    app.dependency_overrides[get_app_settings] = lambda: prod_settings
    try:
        r = await client.post(f"/api/billing/orders/{order_id}/mock-pay", headers=headers)
        assert r.status_code == 403  # prod 一律拒绝模拟支付
    finally:
        app.dependency_overrides.pop(get_app_settings, None)


@pytest.fixture
async def app_client():
    """同 conftest.client, 但把 app 一并暴露 (prod 403 用 dependency_overrides)。"""
    from eduagents_core.config import get_settings

    get_settings.cache_clear()
    from eduagents_api.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield app, c


# ---------- RLS 越权与 append-only ----------


async def test_wallet_rls_blocks_cross_user_and_default_denies(client):
    from eduagents_api.db import plain_session, tenant_session
    from eduagents_api.models import WalletAccount, WalletLedger

    user_a, headers_a = await _register_and_login(client)
    user_b, headers_b = await _register_and_login(client)

    # B 的租户会话读 A 的钱包/流水: RLS 本人策略 -> 空集
    async with tenant_session(user_b) as session:
        assert (
            await session.scalar(
                select(WalletAccount.balance).where(WalletAccount.user_id == user_a)
            )
        ) is None
        rows = (
            await session.scalars(
                select(WalletLedger).where(WalletLedger.user_id == user_a)
            )
        ).all()
        assert rows == []
        # 无谓词全表扫也只看得到本人行
        visible = (await session.scalars(select(WalletAccount))).all()
        assert {r.user_id for r in visible} <= {user_b}

    # 无任何 GUC 的会话: 默认拒绝 (FORCE RLS, 表 owner 也不豁免)
    async with plain_session() as session:
        assert (await session.scalars(select(WalletAccount))).all() == []

    # HTTP 层: 各自只看到自己的 10 元
    r = await client.get("/api/billing/wallet", headers=headers_b)
    assert r.json()["balance"] == "10.0000"


async def test_wallet_ledger_append_only(client):
    """edu_app 的 UPDATE/DELETE 权限已被 0006 收回 — 账本行不可篡改。"""
    from eduagents_api.wallet import service_session
    from sqlalchemy.exc import ProgrammingError

    user_id, _ = await _register_and_login(client)
    with pytest.raises(ProgrammingError):  # permission denied for table wallet_ledger
        async with service_session() as session:
            await session.execute(
                text("UPDATE wallet_ledger SET delta = 999 WHERE user_id = :uid"),
                {"uid": user_id},
            )


# ---------- password_reset_tokens (C 路用表, 0006 建) ----------


async def test_password_reset_tokens_service_only(client):
    from eduagents_api.db import tenant_session
    from eduagents_api.models import PasswordResetToken
    from eduagents_api.wallet import service_session
    from sqlalchemy.exc import IntegrityError

    user_id, _ = await _register_and_login(client)
    token_hash = uuid.uuid4().hex + uuid.uuid4().hex  # 64 位十六进制 (sha256 形状)

    async with service_session() as session:
        session.add(
            PasswordResetToken(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=text("now() + interval '30 minutes'"),
            )
        )

    # token_hash 唯一约束
    with pytest.raises(IntegrityError):
        async with service_session() as session:
            session.add(
                PasswordResetToken(
                    user_id=user_id,
                    token_hash=token_hash,
                    expires_at=text("now() + interval '30 minutes'"),
                )
            )

    # 用户租户会话不可读 (仅服务层策略)
    async with tenant_session(user_id) as session:
        rows = (
            await session.scalars(
                select(PasswordResetToken).where(
                    PasswordResetToken.user_id == user_id
                )
            )
        ).all()
        assert rows == []

    # 服务层可读
    async with service_session() as session:
        row = await session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == token_hash
            )
        )
        assert row is not None and row.used_at is None
