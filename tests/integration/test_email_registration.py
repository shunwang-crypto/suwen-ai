"""Public registration ownership proof with a fake sender."""

from __future__ import annotations

import re
import uuid

from eduagents_api.db import plain_session
from eduagents_api.models import User
from eduagents_core.config import get_settings
from sqlalchemy import select

from .conftest import requires_services

pytestmark = requires_services


class FakeSender:
    bodies: list[str] = []
    fail = False

    async def send(self, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise OSError("fake transport failure")
        self.bodies.append(body)


async def _enable_email_registration(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "email_verification_required", True)
    sender = FakeSender()
    FakeSender.bodies = []
    monkeypatch.setattr("eduagents_api.routers.auth.create_email_sender", lambda *_: sender)

    async def captcha_is_valid(*_args):
        return True

    monkeypatch.setattr("eduagents_api.routers.auth.verify_captcha", captcha_is_valid)
    return sender


async def test_registration_requires_captcha_then_email_code_and_normalizes(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "email_verification_required", True)
    email = f"Verify-{uuid.uuid4().hex[:10]}@Example.com"

    response = await client.post(
        "/api/auth/register/email-code",
        json={"email": email, "captcha_id": "not-valid", "captcha_answer": "0000"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "CAPTCHA_INVALID"

    sender = await _enable_email_registration(monkeypatch)
    response = await client.post(
        "/api/auth/register/email-code",
        json={"email": email, "captcha_id": "captcha", "captcha_answer": "1234"},
    )
    assert response.status_code == 202
    code = re.search(r"验证码是：(\d{6})", sender.bodies[-1]).group(1)

    response = await client.post(
        "/api/auth/register",
        json={
            "email": f" {email} ",
            "email_code": code,
            "password": "pass-12345",
            "display_name": "验证用户",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["email"] == email.lower()
    assert response.json()["email_verified_at"]
    assert response.json()["role"] == "student"

    response = await client.post(
        "/api/auth/login", json={"email": email.upper(), "password": "pass-12345"}
    )
    assert response.status_code == 200


async def test_wrong_email_code_does_not_create_user(client, monkeypatch):
    await _enable_email_registration(monkeypatch)
    email = f"wrong-code-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/auth/register/email-code",
        json={"email": email, "captcha_id": "captcha", "captcha_answer": "1234"},
    )
    assert response.status_code == 202
    response = await client.post(
        "/api/auth/register",
        json={"email": email, "email_code": "000000", "password": "pass-12345"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "EMAIL_CODE_INVALID"
    async with plain_session() as session:
        assert await session.scalar(select(User.id).where(User.email == email)) is None


async def test_smtp_failure_does_not_leave_registration_code(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "email_verification_required", True)
    sender = FakeSender()
    sender.fail = True
    monkeypatch.setattr("eduagents_api.routers.auth.create_email_sender", lambda *_: sender)

    async def captcha_is_valid(*_args):
        return True

    monkeypatch.setattr("eduagents_api.routers.auth.verify_captcha", captcha_is_valid)
    email = f"smtp-fail-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/auth/register/email-code",
        json={"email": email, "captcha_id": "captcha", "captcha_answer": "1234"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "EMAIL_SEND_FAILED"
    response = await client.post(
        "/api/auth/register",
        json={"email": email, "email_code": "123456", "password": "pass-12345"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "EMAIL_CODE_INVALID"


async def test_registered_email_has_uniform_accepted_response_and_no_message(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "email_verification_required", True)
    email = f"already-registered-{uuid.uuid4().hex[:10]}@example.com"
    async with plain_session() as session:
        session.add(User(email=email, password_hash="hash", role="student"))

    sender = await _enable_email_registration(monkeypatch)
    response = await client.post(
        "/api/auth/register/email-code",
        json={"email": email, "captcha_id": "captcha", "captcha_answer": "1234"},
    )
    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert sender.bodies == []
