from __future__ import annotations

from types import SimpleNamespace

import pytest
from eduagents_api.routers import health as health_router
from eduagents_api.schemas import ChatRequestIn
from fastapi import Response


def test_legacy_chat_kb_id_is_marked_deprecated_in_openapi_schema():
    properties = ChatRequestIn.model_json_schema()["properties"]
    assert properties["kb_id"]["deprecated"] is True
    assert properties["kb_ids"].get("deprecated") is not True


def test_legacy_health_route_is_marked_deprecated():
    route = next(route for route in health_router.router.routes if route.path == "/api/health")
    assert route.deprecated is True


@pytest.mark.asyncio
async def test_legacy_health_returns_deprecation_headers(monkeypatch):
    async def ok(*_args, **_kwargs):
        return "ok"

    monkeypatch.setattr(health_router, "_check_postgres", ok)
    monkeypatch.setattr(health_router, "_check_redis", ok)
    state = SimpleNamespace(settings=SimpleNamespace(gateway_mode="stub"))
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    response = Response()

    body = await health_router.health(request, response)

    assert body["status"] == "ok"
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Link"] == '</api/health/ready>; rel="successor-version"'


@pytest.mark.asyncio
async def test_legacy_health_error_response_keeps_deprecation_headers(monkeypatch):
    async def postgres_down(*_args, **_kwargs):
        return "error: ConnectionError"

    async def redis_ok(*_args, **_kwargs):
        return "ok"

    monkeypatch.setattr(health_router, "_check_postgres", postgres_down)
    monkeypatch.setattr(health_router, "_check_redis", redis_ok)
    state = SimpleNamespace(settings=SimpleNamespace(gateway_mode="stub"))
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    result = await health_router.health(request, Response())

    assert result.status_code == 503
    assert result.headers["Deprecation"] == "true"
    assert result.headers["Link"] == '</api/health/ready>; rel="successor-version"'
