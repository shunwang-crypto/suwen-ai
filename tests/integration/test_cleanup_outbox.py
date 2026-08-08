"""A1 cleanup outbox lease, retry and recovery integration checks."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from .conftest import requires_services

pytestmark = requires_services


@pytest.fixture(autouse=True)
async def _dispose_db_engine_between_loop_scoped_tests():
    """Do not carry asyncpg connections into the next function-scoped loop."""
    yield
    from eduagents_api.db import dispose_engine

    await dispose_engine()


def _doc_payload(tenant_id: uuid.UUID, kb_id: uuid.UUID, doc_id: uuid.UUID) -> dict[str, str]:
    return {
        "tenant_id": str(tenant_id),
        "kb_id": str(kb_id),
        "doc_id": str(doc_id),
        "filename": "cleanup.md",
    }


async def _add_pending(kind: str, payload: dict, **values):
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup

    async with plain_session() as session:
        row = PendingCleanup(kind=kind, payload=payload, **values)
        session.add(row)
        await session.flush()
        return row.id


async def _get_pending(row_id):
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup

    async with plain_session() as session:
        return await session.get(PendingCleanup, row_id)


async def test_two_workers_claim_one_row_and_expired_lease_is_recoverable(monkeypatch):
    from eduagents_worker import cleanup_outbox

    tenant_id, kb_id, doc_id = (uuid.uuid4() for _ in range(3))
    row_id = await _add_pending("doc", _doc_payload(tenant_id, kb_id, doc_id))
    active = 0
    max_active = 0
    calls = 0

    async def fake_cleanup(_ctx, *_args):
        nonlocal active, max_active, calls
        calls += 1
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1

    monkeypatch.setattr(cleanup_outbox, "cleanup_document", fake_cleanup)
    results = await asyncio.gather(
        cleanup_outbox.process_pending_cleanup({"cleanup_worker_id": "worker-a"}),
        cleanup_outbox.process_pending_cleanup({"cleanup_worker_id": "worker-b"}),
    )
    assert sorted(results) == [0, 1]
    assert calls == 1
    assert max_active == 1

    row = await _get_pending(row_id)
    assert row.done is True
    assert row.attempts == 1
    assert row.completed_at is not None
    assert row.locked_at is None and row.locked_by is None

    stale_id = await _add_pending(
        "doc",
        _doc_payload(tenant_id, kb_id, uuid.uuid4()),
        locked_at=datetime.now(UTC) - timedelta(hours=1),
        locked_by="dead-worker",
        next_attempt_at=datetime.now(UTC) - timedelta(hours=1),
    )
    assert await cleanup_outbox.process_pending_cleanup({"cleanup_worker_id": "worker-c"}) == 1
    stale = await _get_pending(stale_id)
    assert stale.done is True and stale.attempts == 1


async def test_stale_claim_token_cannot_finalize_a_row():
    from eduagents_worker import cleanup_outbox

    tenant_id, kb_id, doc_id = (uuid.uuid4() for _ in range(3))
    lease_token = uuid.uuid4().hex
    row_id = await _add_pending(
        "doc",
        _doc_payload(tenant_id, kb_id, doc_id),
        locked_at=datetime.now(UTC),
        locked_by=lease_token,
    )
    claim = cleanup_outbox._Claim(
        row_id=row_id,
        kind="doc",
        payload=_doc_payload(tenant_id, kb_id, doc_id),
        attempts=1,
        lease_token=lease_token,
    )
    stale_claim = cleanup_outbox._Claim(
        row_id=row_id,
        kind=claim.kind,
        payload=claim.payload,
        attempts=claim.attempts,
        lease_token=uuid.uuid4().hex,
    )

    assert await cleanup_outbox._mark_completed(stale_claim) is False
    current = await _get_pending(row_id)
    assert current.done is False and current.locked_by == lease_token
    assert await cleanup_outbox._mark_completed(claim) is True


async def test_external_failure_uses_backoff_then_recovers_and_redacts(monkeypatch):
    from eduagents_worker import cleanup_outbox

    tenant_id, kb_id, doc_id = (uuid.uuid4() for _ in range(3))
    row_id = await _add_pending("doc", _doc_payload(tenant_id, kb_id, doc_id))
    should_fail = True

    async def flaky_cleanup(_ctx, *_args):
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise RuntimeError(
                "storage failed https://user:password@example.test?token=url-token "
                "secret=object-storage-secret"
            )

    monkeypatch.setattr(cleanup_outbox, "cleanup_document", flaky_cleanup)
    assert await cleanup_outbox.process_pending_cleanup({"cleanup_worker_id": "worker-a"}) == 0
    failed = await _get_pending(row_id)
    assert failed.done is False
    assert failed.attempts == 1
    assert failed.last_error_code == "EXTERNAL_ERROR"
    assert failed.next_attempt_at is not None and failed.next_attempt_at > datetime.now(UTC)
    assert "password" not in (failed.last_error_text or "")
    assert "url-token" not in (failed.last_error_text or "")
    assert "object-storage-secret" not in (failed.last_error_text or "")

    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup
    from sqlalchemy import update

    async with plain_session() as session:
        await session.execute(
            update(PendingCleanup)
            .where(PendingCleanup.id == row_id)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await cleanup_outbox.process_pending_cleanup({"cleanup_worker_id": "worker-a"}) == 1
    recovered = await _get_pending(row_id)
    assert recovered.done is True and recovered.attempts == 2
    assert recovered.completed_at is not None
    assert recovered.next_attempt_at is None


async def test_invalid_payload_stays_pending_without_blocking_valid_row(monkeypatch):
    from eduagents_api.db import plain_session
    from eduagents_worker import cleanup_outbox

    tenant_id, kb_id, doc_id = (uuid.uuid4() for _ in range(3))
    poison_id = await _add_pending("unknown_kind", {})
    valid_id = await _add_pending("doc", _doc_payload(tenant_id, kb_id, doc_id))
    calls = 0

    async def fake_cleanup(_ctx, *_args):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(cleanup_outbox, "cleanup_document", fake_cleanup)
    assert await cleanup_outbox.process_pending_cleanup({"cleanup_worker_id": "worker-a"}) == 1
    poison = await _get_pending(poison_id)
    valid = await _get_pending(valid_id)
    assert calls == 1
    assert poison.done is False and poison.last_error_code == "UNKNOWN_KIND"
    assert poison.next_attempt_at is not None
    assert valid.done is True

    # Keep this test isolated even if a future implementation changes the
    # default backoff to immediate retry.
    async with plain_session() as session:
        await session.delete(poison)
