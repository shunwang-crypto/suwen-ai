"""Bounded vector-audit scheduling and aggregate Redis summary.

The worker is the only writer.  The API readiness path only reads the small
summary and never performs a full Qdrant/PG scan.  No point IDs, tenant IDs,
payloads, vectors, or document text are stored in this cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from eduagents_core.config import get_settings
from eduagents_core.logging import get_logger
from eduagents_core.vectorstore.qdrant_store import QdrantStore

from .vector_reconcile import reconcile_vectors

log = get_logger("vector_audit")

SUMMARY_KEY = "vector_audit:summary"
LOCK_KEY = "vector_audit:lock"
STREAK_KEY = "vector_audit:drift_streak"
SUMMARY_TTL_SECONDS = 20 * 60
STALE_AFTER_SECONDS = 15 * 60
LOCK_TTL_SECONDS = 12 * 60
ALERT_DEDUP_SECONDS = 60 * 60
_ADMIN_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000000")

_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _version() -> str:
    value = os.getenv("CODE_VERSION") or os.getenv("APP_VERSION") or "unknown"
    return "".join(char for char in value if char.isalnum() or char in "._:-")[:96] or "unknown"


def _counts(report: Any) -> dict[str, int]:
    return {
        "kb_total": len(report.rows),
        "kb_drifted": len(report.unhealthy_rows),
        "kb_orphaned": len(report.orphan_kbs),
        "missing_points": len(report.missing_points),
        "extra_points": len(report.extra_points),
        "wrong_tenant_points": len(report.wrong_tenant_points),
        "malformed_points": len(report.malformed_points),
        "document_inconsistencies": len(report.document_inconsistencies),
        "errors": len(report.errors),
    }


def _alert_level(status: str, counts: dict[str, int], drift_streak: int) -> str:
    if (
        status == "error"
        or counts["wrong_tenant_points"]
        or counts["kb_orphaned"]
        or counts["extra_points"]
        or counts["malformed_points"]
    ):
        return "high"
    if status == "drift" and drift_streak >= 2:
        return "warning"
    return "none"


def _summary_status(summary: dict[str, Any], now: datetime | None = None) -> str:
    now = now or _now()
    raw_finished = summary.get("finished_at")
    try:
        finished = datetime.fromisoformat(str(raw_finished)).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return "stale"
    age = max(0, int((now - finished).total_seconds()))
    if age > STALE_AFTER_SECONDS:
        return "stale"
    status = str(summary.get("status", "error"))
    return status if status in {"ok", "drift", "error"} else "error"


def _public_summary(summary: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    status = _summary_status(summary, now)
    finished_at = summary.get("finished_at")
    age_seconds: int | None = None
    try:
        age_seconds = max(
            0,
            int((now - datetime.fromisoformat(str(finished_at)).astimezone(UTC)).total_seconds()),
        )
    except (TypeError, ValueError, OverflowError):
        pass
    raw_counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    counts = {
        name: max(0, int(raw_counts.get(name, 0) or 0))
        for name in (
            "kb_total",
            "kb_drifted",
            "kb_orphaned",
            "missing_points",
            "extra_points",
            "wrong_tenant_points",
            "malformed_points",
            "document_inconsistencies",
            "errors",
        )
    }
    return {
        "status": status,
        "counts": counts,
        "started_at": summary.get("started_at"),
        "finished_at": finished_at,
        "age_seconds": age_seconds,
        "code_version": str(summary.get("code_version", "unknown"))[:96],
        # A stale cache is itself an operational alert even when the last
        # completed run had no drift. Keep this aggregate-only so readiness
        # remains a Redis read and never mutates the alert state.
        "alert_level": "high" if status == "stale" else str(summary.get("alert_level", "none")),
        "drift_streak": max(0, int(summary.get("drift_streak", 0) or 0)),
    }


async def load_audit_summary(redis, *, now: datetime | None = None) -> dict[str, Any]:
    """Read and validate the aggregate cache; Redis errors become stale."""
    try:
        raw = await redis.get(SUMMARY_KEY)
        if not raw:
            return _stale_summary()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("summary is not an object")
        return _public_summary(parsed, now)
    except Exception:  # noqa: BLE001 - readiness reports stale, never leaks cache details
        return _stale_summary()


def _counts_zero_names() -> tuple[str, ...]:
    return (
        "kb_total",
        "kb_drifted",
        "kb_orphaned",
        "missing_points",
        "extra_points",
        "wrong_tenant_points",
        "malformed_points",
        "document_inconsistencies",
        "errors",
    )


def _stale_summary() -> dict[str, Any]:
    """Return the fail-closed public shape when no trustworthy audit exists."""

    return {
        "status": "stale",
        "counts": {name: 0 for name in _counts_zero_names()},
        "age_seconds": None,
        "code_version": "unknown",
        "alert_level": "high",
        "drift_streak": 0,
    }


async def _acquire_lock(redis, token: str) -> bool:
    return bool(await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL_SECONDS))


async def _release_lock(redis, token: str) -> None:
    try:
        await redis.eval(_RELEASE_LOCK, 1, LOCK_KEY, token)
    except Exception:  # noqa: BLE001 - an expired lock is safe to leave behind
        log.warning("vector_audit_lock_release_failed")


async def _update_streak(redis, status: str) -> int:
    if status == "drift":
        streak = int(await redis.incr(STREAK_KEY))
        # A long scheduler outage breaks consecutiveness.  Expiring the streak
        # prevents the first drift after recovery from being misreported as a
        # second consecutive observation.
        await redis.expire(STREAK_KEY, SUMMARY_TTL_SECONDS)
        return streak
    await redis.set(STREAK_KEY, 0, ex=SUMMARY_TTL_SECONDS)
    return 0


async def _emit_alert(redis, status: str, counts: dict[str, int], streak: int) -> str:
    level = _alert_level(status, counts, streak)
    if level == "none":
        return level
    fingerprint = hashlib.sha256(
        json.dumps({"status": status, "counts": counts}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    dedup_key = f"vector_audit:alert:{fingerprint}"
    if await redis.set(dedup_key, "1", nx=True, ex=ALERT_DEDUP_SECONDS):
        log.error(
            "vector_audit_alert" if level == "high" else "vector_audit_drift_alert",
            alert_level=level,
            drift_streak=streak,
            **counts,
        )
    return level


async def _store_summary(redis, summary: dict[str, Any]) -> None:
    await redis.setex(SUMMARY_KEY, SUMMARY_TTL_SECONDS, json.dumps(summary, separators=(",", ":")))


async def run_vector_audit(ctx: dict) -> dict[str, Any] | None:
    """Run one bounded full audit under a Redis lock and cache aggregates."""
    redis = ctx.get("redis")
    if redis is None:
        log.warning("vector_audit_redis_missing")
        return None
    token = uuid.uuid4().hex
    if not await _acquire_lock(redis, token):
        log.info("vector_audit_lock_busy")
        return None
    started = _now()
    settings = ctx.get("settings") or get_settings()
    store = QdrantStore(settings)
    try:
        try:
            report = await reconcile_vectors(store, _ADMIN_TENANT)
            status = report.status
            counts = _counts(report)
        except Exception:  # noqa: BLE001 - cache an auditable error state
            status = "error"
            counts = {name: 0 for name in _counts_zero_names()}
            counts["errors"] = 1
            log.exception("vector_audit_failed")
        streak = await _update_streak(redis, status)
        alert_level = await _emit_alert(redis, status, counts, streak)
        finished = _now()
        summary = {
            "status": status,
            "counts": counts,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "code_version": _version(),
            "drift_streak": streak,
            "alert_level": alert_level,
        }
        await _store_summary(redis, summary)
        return _public_summary(summary, finished)
    finally:
        store.close()
        await _release_lock(redis, token)
