"""健康检查 (P1-2): liveness / readiness 拆分。

- /api/health/live  — 进程存活探针: 恒 200, 不探任何依赖 (供重启判定)。
- /api/health/ready — 就绪探针: PG / Redis / Qdrant / 生产图运行时任一挂 → 503 (供反代摘流量,
  compose healthcheck 打这里)。worker 心跳缺失只降级 (degraded, 仍 200):
  worker 挂不影响同步 API 流量, 不应让反代摘掉 api 实例。
- /api/health       — 旧路径兼容: 响应体形状不变, 但 PG/Redis 挂时返 503
  (修复"依赖挂掉仍 200, 探针永远绿"的问题)。
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import get_sessionmaker

router = APIRouter(tags=["health"])

# arq 默认队列的健康键: worker 每 health_check_interval 写入, 自带过期。
# worker 侧未自定义 queue_name (apps/worker/main.py), 故为默认值。
_ARQ_HEALTH_KEY = "arq:queue:health-check"
_PROBE_TIMEOUT = 3.0


async def _check_postgres() -> str:
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


async def _check_redis(request: Request) -> str:
    try:
        await request.app.state.redis.ping()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


async def _check_qdrant(request: Request) -> str:
    settings = request.app.state.settings
    if settings.qdrant_local_path:
        return "ok"  # 本地嵌入模式: 无服务端依赖
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/healthz")
        return "ok" if resp.status_code == 200 else f"error: HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


async def _check_worker(request: Request) -> str:
    """arq worker 心跳: Redis 里的 health key 存在即视为活着。"""
    try:
        exists = await request.app.state.redis.exists(_ARQ_HEALTH_KEY)
        return "ok" if exists else "missing"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


def _check_graph(request: Request) -> str:
    return "ok" if getattr(request.app.state, "graphs", None) is not None else "unavailable"


@router.get("/api/health/live")
async def live():
    """Liveness: 进程能应答即 200, 永不探依赖。"""
    return {"status": "ok"}


@router.get("/api/health/ready")
async def ready(request: Request):
    """Readiness: 硬依赖 (PG/Redis/Qdrant) 任一 fail → 503; worker 缺席只降级。"""
    checks = {
        "postgres": await _check_postgres(),
        "redis": await _check_redis(request),
        "qdrant": await _check_qdrant(request),
        "worker": await _check_worker(request),
    }
    if request.app.state.settings.app_env == "prod":
        checks["graph"] = _check_graph(request)
    hard_keys = ("postgres", "redis", "qdrant", "graph") if "graph" in checks else (
        "postgres",
        "redis",
        "qdrant",
    )
    hard_ok = all(checks[k] == "ok" for k in hard_keys)
    status = "ok" if hard_ok and checks["worker"] == "ok" else (
        "degraded" if hard_ok else "down"
    )
    body = {
        "status": status,
        "gateway_mode": request.app.state.settings.gateway_mode,
        "checks": checks,
    }
    if not hard_ok:
        return JSONResponse(body, status_code=503)
    return body


@router.get("/api/health")
async def health(request: Request):
    """旧路径兼容: 只探 PG/Redis (原有语义), 但依赖挂时如实返 503。"""
    checks = {
        "postgres": await _check_postgres(),
        "redis": await _check_redis(request),
    }
    healthy = all(v == "ok" for v in checks.values())
    body = {
        "status": "ok" if healthy else "degraded",
        "gateway_mode": request.app.state.settings.gateway_mode,
        "checks": checks,
    }
    if not healthy:
        return JSONResponse(body, status_code=503)
    return body
