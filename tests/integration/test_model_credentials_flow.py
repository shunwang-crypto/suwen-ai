from __future__ import annotations

import base64
import uuid

from eduagents_api.db import tenant_session
from eduagents_api.models import UserModelCredential
from eduagents_core.config import get_settings
from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services


def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


async def _register_login(client, prefix: str) -> tuple[dict, dict[str, str]]:
    email = _email(prefix)
    registered = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert registered.status_code == 201, registered.text
    login = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert login.status_code == 200, login.text
    return registered.json(), {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_model_credential_api_is_encrypted_private_and_redacted(client):
    settings = get_settings()
    settings.user_model_credentials_enabled = True
    settings.user_model_credentials_encryption_key = base64.urlsafe_b64encode(b"z" * 32).decode()
    settings.deepseek_byok_allowed_models = "deepseek-chat,deepseek-reasoner"

    first, first_headers = await _register_login(client, "byok-first")
    second, second_headers = await _register_login(client, "byok-second")
    raw_key = "sk-test-key-1234567890"
    response = await client.put(
        "/api/settings/model-credential",
        headers=first_headers,
        json={
            "api_key": raw_key,
            "normal_model": "deepseek-chat",
            "reasoning_model": "deepseek-reasoner",
            "fallback_to_platform": False,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["configured"] is True
    assert payload["key_last_four"] == raw_key[-4:]
    assert raw_key not in response.text
    assert "encrypted_api_key" not in response.text

    async with tenant_session(first["id"]) as session:
        row = await session.scalar(
            select(UserModelCredential).where(UserModelCredential.user_id == uuid.UUID(first["id"]))
        )
    assert row is not None
    assert raw_key.encode() not in row.encrypted_api_key
    assert row.encryption_key_version == 1

    own = await client.get("/api/settings/model-credential", headers=first_headers)
    other = await client.get("/api/settings/model-credential", headers=second_headers)
    assert own.status_code == 200 and own.json()["configured"] is True
    assert other.status_code == 200 and other.json()["configured"] is False

    from eduagents_api.model_credentials import resolve_user_credential
    from eduagents_core.gateway import ChatMessage, ChatRequest, Tier

    resolved = await resolve_user_credential(
        ChatRequest(
            messages=(ChatMessage(role="user", content="test"),),
            tier=Tier.standard,
            user_id=first["id"],
            tenant_id=first["id"],
        ),
        settings,
    )
    assert resolved is not None
    assert resolved.api_key == raw_key
    assert resolved.model == "deepseek-chat"
    assert (
        await resolve_user_credential(
            ChatRequest(
                messages=(ChatMessage(role="user", content="test"),),
                user_id=second["id"],
                tenant_id=second["id"],
            ),
            settings,
        )
        is None
    )

    invalid_replace = await client.put(
        "/api/settings/model-credential",
        headers=first_headers,
        json={
            "api_key": raw_key,
            "normal_model": "not-allowed",
            "reasoning_model": "deepseek-reasoner",
        },
    )
    assert invalid_replace.status_code == 422
    after_failure = await client.get("/api/settings/model-credential", headers=first_headers)
    assert after_failure.json()["key_last_four"] == raw_key[-4:]

    admin = await client.post(
        "/api/auth/register", json={"email": _email("byok-admin"), "password": "pass-12345"}
    )
    assert admin.status_code == 201
    admin_email = admin.json()["email"]
    from eduagents_api.db import plain_session
    from eduagents_api.models import User

    async with plain_session() as session:
        admin_row = await session.scalar(select(User).where(User.email == admin_email))
        admin_row.role = "admin"
    login = await client.post(
        "/api/auth/login", json={"email": admin_email, "password": "pass-12345"}
    )
    admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    detail = await client.get(f"/api/admin/users/{first['id']}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    assert raw_key not in detail.text
    assert "encrypted_api_key" not in detail.text
    assert detail.json()["model_credential"]["key_last_four"] == raw_key[-4:]
    listing = await client.get(
        "/api/admin/users", headers=admin_headers, params={"page_size": 100}
    )
    assert listing.status_code == 200, listing.text
    listing_payload = listing.json()
    assert {"items", "page", "page_size", "total", "has_more"} <= listing_payload.keys()
    listed = next(item for item in listing_payload["items"] if item["user_id"] == first["id"])
    assert listed["byok_call_count"] == 0
    assert raw_key not in listing.text
    assert "encrypted_api_key" not in listing.text

    deleted = await client.delete("/api/settings/model-credential", headers=first_headers)
    assert deleted.status_code == 204
    assert (await client.get("/api/settings/model-credential", headers=first_headers)).json()["configured"] is False
