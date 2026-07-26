"""U-A2/U-A3 相关: 注册-登录-刷新轮换-重放拒绝-登出黑名单 全链路."""
import uuid

import httpx

from .conftest import requires_services

pytestmark = requires_services


def _email() -> str:
    return f"it-{uuid.uuid4().hex[:12]}@test.eduagents.cn"


async def test_full_auth_lifecycle(client):
    email = _email()

    # 注册
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == email

    # 重复注册 -> 409 统一错误信封
    r = await client.post(
        "/api/auth/register", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "EMAIL_EXISTS" and body["request_id"]

    # 错误密码 -> 401
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "wrong-pass"}
    )
    assert r.status_code == 401

    # 登录: access token + httpOnly refresh cookie
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "pass-12345"}
    )
    assert r.status_code == 200, r.text
    access1 = r.json()["access_token"]
    refresh1 = client.cookies.get("edu_refresh")
    assert refresh1
    set_cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie

    # /me
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {access1}"}
    )
    assert r.status_code == 200 and r.json()["email"] == email

    # 刷新 -> 轮换出新 cookie
    r = await client.post("/api/auth/refresh")
    assert r.status_code == 200, r.text
    access2 = r.json()["access_token"]
    refresh2 = client.cookies.get("edu_refresh")
    assert refresh2 and refresh2 != refresh1

    # 重放已轮换的旧 refresh token -> 401 (U-A3)
    client.cookies = httpx.Cookies()
    client.cookies.set("edu_refresh", refresh1, path="/api/auth")
    r = await client.post("/api/auth/refresh")
    assert r.status_code == 401

    # 重放检测应已撤销全家: 新 token 也失效
    client.cookies = httpx.Cookies()
    client.cookies.set("edu_refresh", refresh2, path="/api/auth")
    r = await client.post("/api/auth/refresh")
    assert r.status_code == 401

    # 登出 -> access token 进黑名单
    r = await client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {access2}"}
    )
    assert r.status_code == 204
    r = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {access2}"}
    )
    assert r.status_code == 401
    assert r.json()["code"] == "TOKEN_REVOKED"


async def test_health_endpoint(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["redis"] == "ok"
