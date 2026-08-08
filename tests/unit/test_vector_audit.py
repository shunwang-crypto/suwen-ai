"""A3/A4 aggregate audit cache, lock and alert semantics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from eduagents_api import vector_audit
from fakeredis import FakeAsyncRedis


@pytest.mark.asyncio
async def test_summary_missing_or_expired_is_stale():
    redis = FakeAsyncRedis(decode_responses=True)
    summary = await vector_audit.load_audit_summary(redis)
    assert summary["status"] == "stale"
    assert summary["alert_level"] == "high"
    await redis.set(
        vector_audit.SUMMARY_KEY,
        json.dumps(
            {
                "status": "drift",
                "counts": {"missing_points": 2},
                "finished_at": "2099-01-01T00:00:00+00:00",
                "code_version": "test",
            }
        ),
    )
    fresh = await vector_audit.load_audit_summary(redis)
    assert fresh["status"] == "drift"
    assert fresh["counts"]["missing_points"] == 2


@pytest.mark.asyncio
async def test_lock_is_single_owner_and_token_checked_on_release():
    redis = FakeAsyncRedis(decode_responses=True)
    assert await vector_audit._acquire_lock(redis, "first") is True
    assert await vector_audit._acquire_lock(redis, "second") is False
    await vector_audit._release_lock(redis, "second")
    assert await redis.exists(vector_audit.LOCK_KEY)
    await vector_audit._release_lock(redis, "first")
    assert not await redis.exists(vector_audit.LOCK_KEY)


@pytest.mark.asyncio
async def test_drift_streak_expires_when_audit_scheduler_stops():
    redis = FakeAsyncRedis(decode_responses=True)
    assert await vector_audit._update_streak(redis, "drift") == 1
    assert await redis.ttl(vector_audit.STREAK_KEY) > 0


def test_security_sensitive_drift_is_high_priority_immediately():
    base = {
        "wrong_tenant_points": 0,
        "kb_orphaned": 0,
        "extra_points": 0,
        "malformed_points": 0,
    }
    for field in ("wrong_tenant_points", "kb_orphaned", "extra_points", "malformed_points"):
        counts = {**base, field: 1}
        assert vector_audit._alert_level("drift", counts, drift_streak=1) == "high"


def test_stale_public_summary_is_high_priority():
    summary = vector_audit._public_summary(
        {
            "status": "ok",
            "counts": {},
            "finished_at": "2020-01-01T00:00:00+00:00",
        },
        now=datetime(2020, 1, 1, 0, 20, tzinfo=UTC),
    )
    assert summary["status"] == "stale"
    assert summary["alert_level"] == "high"


@pytest.mark.asyncio
async def test_audit_caches_aggregate_and_deduplicates_alert(monkeypatch):
    redis = FakeAsyncRedis(decode_responses=True)
    report = SimpleNamespace(
        status="drift",
        rows=[SimpleNamespace(healthy=False)],
        unhealthy_rows=[object()],
        orphan_kbs=[object()],
        missing_points=["redacted"],
        extra_points=[],
        wrong_tenant_points=[],
        malformed_points=[],
        document_inconsistencies=[],
        errors=[],
    )

    class _Store:
        def __init__(self, _settings):
            pass

        def close(self):
            pass

    monkeypatch.setattr(vector_audit, "QdrantStore", _Store)
    monkeypatch.setattr(vector_audit, "reconcile_vectors", lambda *_args: report)

    async def _reconcile(*_args):
        return report

    monkeypatch.setattr(vector_audit, "reconcile_vectors", _reconcile)
    result = await vector_audit.run_vector_audit({"redis": redis, "settings": object()})
    assert result["status"] == "drift"
    cached = await vector_audit.load_audit_summary(redis)
    assert cached["counts"]["kb_orphaned"] == 1
    assert cached["counts"]["missing_points"] == 1
    assert cached["alert_level"] == "high"
