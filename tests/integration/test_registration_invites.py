from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from eduagents_api.db import plain_session
from eduagents_api.models import RegistrationInvite, User
from eduagents_api.registration_invites import hash_invite_code
from eduagents_core.config import get_settings
from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services


async def _register(client, email: str, **extra):
    return await client.post(
        "/api/auth/register",
        json={"email": email, "password": "pass-12345", **extra},
    )


async def _admin_headers(client) -> dict[str, str]:
    email = f"invite-admin-{uuid.uuid4().hex[:10]}@example.com"
    created = await _register(client, email)
    assert created.status_code == 201, created.text
    async with plain_session() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = "admin"
    logged_in = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert logged_in.status_code == 200, logged_in.text
    return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}


async def test_closed_invite_and_open_modes(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "registration_mode", "closed")
    public_config = await client.get("/api/config/public")
    assert public_config.status_code == 200
    assert public_config.json() == {
        "registration_mode": "closed",
        "email_verification_required": False,
    }
    closed = await _register(client, f"closed-{uuid.uuid4().hex[:8]}@example.com")
    assert closed.status_code == 403
    assert closed.json()["code"] == "REGISTRATION_CLOSED"

    monkeypatch.setattr(settings, "registration_mode", "invite")
    missing = await _register(client, f"missing-{uuid.uuid4().hex[:8]}@example.com")
    assert missing.status_code == 400
    assert missing.json()["code"] == "INVITE_CODE_INVALID"

    monkeypatch.setattr(settings, "registration_mode", "open")
    opened = await _register(client, f"open-{uuid.uuid4().hex[:8]}@example.com")
    assert opened.status_code == 201, opened.text
    assert opened.json()["role"] == "student"


async def test_expired_invite_has_generic_failure(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "registration_mode", "open")
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/registration-invites",
        headers=headers,
        json={"expires_in_hours": 1},
    )
    code = created.json()["code"]
    async with plain_session() as session:
        row = await session.scalar(
            select(RegistrationInvite).where(RegistrationInvite.code_hash == hash_invite_code(code))
        )
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(settings, "registration_mode", "invite")
    response = await _register(
        client, f"expired-{uuid.uuid4().hex[:8]}@example.com", invite_code=code
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVITE_CODE_INVALID"


async def test_admin_create_batch_list_revoke_and_one_time_consumption(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "registration_mode", "open")
    headers = await _admin_headers(client)

    batch = await client.post(
        "/api/admin/registration-invites/batch",
        headers=headers,
        json={"count": 2, "expires_in_hours": 1},
    )
    assert batch.status_code == 201, batch.text
    created = batch.json()
    assert len(created) == 2
    code = created[0]["code"]
    assert code and code.startswith("EDU-")

    listed = await client.get("/api/admin/registration-invites", headers=headers)
    assert listed.status_code == 200
    assert all(item["code"] is None for item in listed.json())
    assert code not in json.dumps(listed.json())
    async with plain_session() as session:
        row = await session.scalar(
            select(RegistrationInvite).where(RegistrationInvite.code_hash == hash_invite_code(code))
        )
        assert row is not None
        assert code not in row.code_hash

    revoked = await client.post(
        f"/api/admin/registration-invites/{created[1]['id']}/revoke", headers=headers
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    monkeypatch.setattr(settings, "registration_mode", "invite")
    used = await _register(
        client, f"invited-{uuid.uuid4().hex[:8]}@example.com", invite_code=code
    )
    assert used.status_code == 201, used.text
    retry = await _register(
        client, f"retry-{uuid.uuid4().hex[:8]}@example.com", invite_code=code
    )
    assert retry.status_code == 400
    assert retry.json()["code"] == "INVITE_CODE_INVALID"

    revoked_try = await _register(
        client,
        f"revoked-{uuid.uuid4().hex[:8]}@example.com",
        invite_code=created[1]["code"],
    )
    assert revoked_try.status_code == 400
    assert revoked_try.json()["code"] == "INVITE_CODE_INVALID"


async def test_invite_failures_do_not_consume_and_non_admin_is_forbidden(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "registration_mode", "open")
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/registration-invites", headers=headers, json={}
    )
    code = created.json()["code"]

    normal = await _register(client, f"normal-{uuid.uuid4().hex[:8]}@example.com")
    normal_login = await client.post(
        "/api/auth/login",
        json={"email": normal.json()["email"], "password": "pass-12345"},
    )
    normal_headers = {"Authorization": f"Bearer {normal_login.json()['access_token']}"}
    forbidden = await client.get("/api/admin/registration-invites", headers=normal_headers)
    assert forbidden.status_code == 403

    monkeypatch.setattr(settings, "registration_mode", "invite")
    duplicate = await _register(client, normal.json()["email"], invite_code=code)
    assert duplicate.status_code == 409
    async with plain_session() as session:
        row = await session.scalar(
            select(RegistrationInvite).where(RegistrationInvite.code_hash == hash_invite_code(code))
        )
        assert row is not None and row.used_count == 0

    monkeypatch.setattr(settings, "email_verification_required", True)
    invalid_email_code = await _register(
        client,
        f"bad-code-{uuid.uuid4().hex[:8]}@example.com",
        email_code="000000",
        invite_code=code,
    )
    assert invalid_email_code.status_code == 400
    assert invalid_email_code.json()["code"] == "EMAIL_CODE_INVALID"
    async with plain_session() as session:
        row = await session.scalar(
            select(RegistrationInvite).where(RegistrationInvite.code_hash == hash_invite_code(code))
        )
        assert row is not None and row.used_count == 0
    monkeypatch.setattr(settings, "email_verification_required", False)


async def test_single_invite_is_atomic_under_concurrency(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "registration_mode", "open")
    headers = await _admin_headers(client)
    created = await client.post(
        "/api/admin/registration-invites", headers=headers, json={}
    )
    code = created.json()["code"]
    monkeypatch.setattr(settings, "registration_mode", "invite")

    async def attempt():
        return await _register(
            client, f"race-{uuid.uuid4().hex[:8]}@example.com", invite_code=code
        )

    responses = await asyncio.gather(attempt(), attempt())
    assert sorted(response.status_code for response in responses) == [201, 400]
    async with plain_session() as session:
        row = await session.scalar(
            select(RegistrationInvite).where(RegistrationInvite.code_hash == hash_invite_code(code))
        )
        assert row is not None and row.used_count == 1 and row.status == "used"
