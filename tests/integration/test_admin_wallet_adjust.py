from __future__ import annotations

import asyncio
import uuid

from eduagents_api.db import plain_session
from eduagents_api.models import User, WalletLedger
from eduagents_api.wallet import service_session
from eduagents_core.config import get_settings
from sqlalchemy import select, text

from .conftest import requires_services

pytestmark = requires_services


def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


async def _register(client, email: str):
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _admin_and_target(client) -> tuple[dict[str, str], dict]:
    admin_email = _email("wallet-admin")
    await _register(client, admin_email)
    async with plain_session() as session:
        admin = await session.scalar(select(User).where(User.email == admin_email))
        assert admin is not None
        admin.role = "admin"
    logged_in = await client.post(
        "/api/auth/login", json={"email": admin_email, "password": "pass-12345"}
    )
    assert logged_in.status_code == 200, logged_in.text
    target = await _register(client, _email("wallet-target"))
    return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}, target


async def test_admin_search_is_scoped_and_does_not_expose_secrets(client):
    settings = get_settings()
    settings.registration_mode = "open"
    admin_headers, target = await _admin_and_target(client)

    async with service_session() as session:
        index_name = await session.scalar(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'wallet_ledger' "
                "AND indexname = 'uq_wallet_ledger_admin_adjust_ref'"
            )
        )
    assert index_name == "uq_wallet_ledger_admin_adjust_ref"

    response = await client.get(
        "/api/admin/users", params={"query": target["email"], "limit": 1}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "user_id": target["id"],
            "email": target["email"],
            "role": "student",
            "status": "active",
            "wallet_balance": "10.0000",
            "created_at": target["created_at"],
        }
    ]
    assert "password_hash" not in response.text
    assert "token" not in response.text.lower()

    prefix = target["email"].split("@", 1)[0][:8]
    prefixed = await client.get(
        "/api/admin/users", params={"query": prefix}, headers=admin_headers
    )
    assert prefixed.status_code == 200
    assert any(item["user_id"] == target["id"] for item in prefixed.json())

    normal = await _register(client, _email("wallet-normal"))
    normal_login = await client.post(
        "/api/auth/login", json={"email": normal["email"], "password": "pass-12345"}
    )
    normal_headers = {"Authorization": f"Bearer {normal_login.json()['access_token']}"}
    forbidden = await client.get(
        "/api/admin/users", params={"query": target["email"]}, headers=normal_headers
    )
    assert forbidden.status_code == 403


async def test_admin_adjust_validates_and_is_idempotent(client):
    settings = get_settings()
    settings.registration_mode = "open"
    admin_headers, target = await _admin_and_target(client)
    path = f"/api/admin/users/{target['id']}/wallet-adjust"

    invalid_bodies = (
        {"delta": "0", "note": "原因", "reference": "ref-zero"},
        {"delta": "1000.00001", "note": "原因", "reference": "ref-scale"},
        {"delta": "1001", "note": "原因", "reference": "ref-range"},
        {"delta": 30, "note": "原因", "reference": "ref-number"},
        {"delta": "30", "note": " ", "reference": "ref-note"},
        {"delta": "30", "note": "原因", "reference": "ref space"},
    )
    for body in invalid_bodies:
        response = await client.post(path, json=body, headers=admin_headers)
        assert response.status_code == 422, (body, response.text)

    response = await client.post(
        path,
        json={
            "delta": "30.0000",
            "note": "  硅基流动有效邀请奖励  ",
            "reference": "siliconflow-referral:unique-1",
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    first = response.json()
    assert first == {
        "user_id": target["id"],
        "previous_balance": "10.0000",
        "delta": "30.0000",
        "balance": "40.0000",
        "note": "硅基流动有效邀请奖励",
        "reference": "siliconflow-referral:unique-1",
    }

    retry = await client.post(
        path,
        json={
            "delta": "30.0000",
            "note": "硅基流动有效邀请奖励",
            "reference": "siliconflow-referral:unique-1",
        },
        headers=admin_headers,
    )
    assert retry.status_code == 200
    assert retry.json() == first

    conflict = await client.post(
        path,
        json={"delta": "31", "note": "不同业务", "reference": "siliconflow-referral:unique-1"},
        headers=admin_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "WALLET_ADJUST_REFERENCE_CONFLICT"

    negative = await client.post(
        path,
        json={"delta": "-5.5000", "note": "人工回冲", "reference": "manual:reversal-1"},
        headers=admin_headers,
    )
    assert negative.status_code == 200
    assert negative.json()["previous_balance"] == "40.0000"
    assert negative.json()["balance"] == "34.5000"

    missing = await client.post(
        f"/api/admin/users/{uuid.uuid4()}/wallet-adjust",
        json={"delta": "1", "note": "原因", "reference": "missing-user"},
        headers=admin_headers,
    )
    assert missing.status_code == 404

    async with service_session() as session:
        account_balance = await session.scalar(
            text("SELECT balance FROM wallet_accounts WHERE user_id = :uid"),
            {"uid": target["id"]},
        )
        rows = (
            await session.scalars(
                select(WalletLedger)
                .where(
                    WalletLedger.user_id == uuid.UUID(target["id"]),
                    WalletLedger.kind == "admin_adjust",
                )
                .order_by(WalletLedger.created_at)
            )
        ).all()
    assert account_balance == 34.5000
    assert len(rows) == 2
    assert {row.ref for row in rows} == {
        "siliconflow-referral:unique-1",
        "manual:reversal-1",
    }


async def test_same_reference_concurrent_requests_credit_once(client):
    settings = get_settings()
    settings.registration_mode = "open"
    admin_headers, target = await _admin_and_target(client)
    path = f"/api/admin/users/{target['id']}/wallet-adjust"
    body = {
        "delta": "30.0000",
        "note": "并发邀请奖励",
        "reference": "concurrent:ref-1",
    }

    responses = await asyncio.gather(
        client.post(path, json=body, headers=admin_headers),
        client.post(path, json=body, headers=admin_headers),
    )
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["balance"] == "40.0000"

    async with service_session() as session:
        rows = (
            await session.scalars(
                select(WalletLedger).where(
                    WalletLedger.user_id == uuid.UUID(target["id"]),
                    WalletLedger.kind == "admin_adjust",
                    WalletLedger.ref == "concurrent:ref-1",
                )
            )
        ).all()
    assert len(rows) == 1
