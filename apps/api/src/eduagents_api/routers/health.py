from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from ..db import get_sessionmaker

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health(request: Request):
    checks: dict[str, str] = {}
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"
    healthy = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "gateway_mode": request.app.state.settings.gateway_mode,
        "checks": checks,
    }
