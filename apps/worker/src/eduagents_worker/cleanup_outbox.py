"""Lease-based consumer for the pending cleanup outbox.

Rows are claimed in a short transaction with ``FOR UPDATE SKIP LOCKED`` and
then processed outside that transaction.  The committed lease prevents other
workers from selecting the row; a heartbeat keeps a live long-running task's
lease fresh, while a lease left by a crashed worker becomes reclaimable.

The old ``done`` flag remains the compatibility source of truth.  A row is
marked done only after its external, idempotent cleanup has succeeded.  Bad
payloads and unknown kinds stay pending with bounded, redacted failure details
so an operator can repair the poison row without blocking newer work.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from eduagents_core.logging import get_logger
from sqlalchemy import and_, func, or_, select, update

from .ingestion import cleanup_document, cleanup_kb
from .purge import purge_tenant

log = get_logger("cleanup_outbox")

_ALERT_ATTEMPTS = 5
_BATCH = 20
_LEASE_SECONDS = 5 * 60
_BACKOFF_BASE_SECONDS = 15
_BACKOFF_MAX_SECONDS = 60 * 60
_STUCK_AGE_SECONDS = 2 * 60
_ERROR_TEXT_LIMIT = 512
_CLEANUP_KINDS = frozenset({"doc", "kb", "tenant", "object_prefix"})

_URL_RE = re.compile(
    r"(?i)\b(?:https?|s3|gs|redis|postgres(?:ql)?(?:\+[a-z0-9_]+)?|minio)://[^\s<>'\"]+"
)
_SECRET_KV_RE = re.compile(
    r"(?ix)\b("
    r"password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|"
    r"secret(?:[_-]?key)?|api[_-]?key|access[_-]?key|client[_-]?secret|"
    r"session[_-]?token|private[_-]?key|authorization|credential|signature|sig|"
    r"x-amz-[a-z0-9_-]+"
    r")\b\s*(?:=|:)\s*[\"']?[^,\s;\"'}]+[\"']?"
)
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{24,}(?![A-Za-z0-9])")


class CleanupPayloadError(ValueError):
    """A payload rejected before any external cleanup call is made."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _Claim:
    row_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    lease_token: str


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid_text(value: object, field: str) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str) or not value or len(value) > 64:
        raise CleanupPayloadError("INVALID_PAYLOAD", f"{field} must be a UUID")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise CleanupPayloadError("INVALID_PAYLOAD", f"{field} must be a UUID") from exc


def _validate_prefix(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise CleanupPayloadError("INVALID_PAYLOAD", "prefix must be a bounded storage prefix")
    if "\x00" in value or value.startswith("/"):
        raise CleanupPayloadError("INVALID_PAYLOAD", "prefix must be a relative storage prefix")
    parts = value.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        raise CleanupPayloadError("INVALID_PAYLOAD", "prefix must not contain traversal segments")
    _uuid_text(parts[0], "prefix tenant")
    return value


def validate_cleanup_payload(kind: str, payload: object) -> dict[str, Any]:
    """Validate and normalize the small allow-listed cleanup payload contract."""
    if kind not in _CLEANUP_KINDS:
        raise CleanupPayloadError("UNKNOWN_KIND", "unknown cleanup kind")
    if not isinstance(payload, dict):
        raise CleanupPayloadError("INVALID_PAYLOAD", "payload must be an object")

    validated = dict(payload)
    if kind == "doc":
        for field in ("tenant_id", "kb_id", "doc_id"):
            validated[field] = _uuid_text(payload.get(field), field)
        filename = payload.get("filename", "")
        if not isinstance(filename, str) or len(filename) > 512 or "\x00" in filename:
            raise CleanupPayloadError("INVALID_PAYLOAD", "filename must be bounded text")
        validated["filename"] = filename
    elif kind == "kb":
        validated["tenant_id"] = _uuid_text(payload.get("tenant_id"), "tenant_id")
        validated["kb_id"] = _uuid_text(payload.get("kb_id"), "kb_id")
    elif kind == "tenant":
        validated["tenant_id"] = _uuid_text(payload.get("tenant_id"), "tenant_id")
        session_ids = payload.get("session_ids")
        if session_ids is not None:
            if not isinstance(session_ids, list) or len(session_ids) > 1000:
                raise CleanupPayloadError("INVALID_PAYLOAD", "session_ids must be a bounded list")
            if any(not isinstance(value, str) or not value or len(value) > 512 for value in session_ids):
                raise CleanupPayloadError("INVALID_PAYLOAD", "session_ids contain invalid values")
            validated["session_ids"] = list(session_ids)
    else:
        validated["prefix"] = _validate_prefix(payload.get("prefix"))
    return validated


def sanitize_cleanup_error(value: object) -> str:
    """Return bounded error text with URL credentials and secret-shaped values removed."""
    text = " ".join(str(value or "").split())
    text = _URL_RE.sub("<redacted-url>", text)
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+", "<redacted-auth>", text)
    text = _SECRET_KV_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _LONG_TOKEN_RE.sub("<redacted>", text)
    text = " ".join(text.split())
    return (text or "cleanup failed")[:_ERROR_TEXT_LIMIT]


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, CleanupPayloadError):
        return exc.code
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "EXTERNAL_TIMEOUT"
    if isinstance(exc, (ConnectionError, OSError)):
        return "EXTERNAL_UNAVAILABLE"
    return "EXTERNAL_ERROR"


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, CleanupPayloadError):
        return sanitize_cleanup_error(str(exc))
    return sanitize_cleanup_error(f"{type(exc).__name__}: {exc}")


def retry_delay_seconds(attempts: int) -> int:
    """Deterministic exponential backoff, capped so a poison row stays observable."""
    exponent = max(0, min(attempts - 1, 20))
    return min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2**exponent))


def _worker_id(ctx: dict) -> str:
    # Observability label only; lease ownership is always the per-claim token.
    value = str(ctx.get("cleanup_worker_id") or f"cleanup:{uuid.uuid4().hex}")
    return value[:128]


async def _claim_pending_cleanup(worker_id: str, *, limit: int = _BATCH) -> list[_Claim]:
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup

    now = _now()
    stale_before = now - timedelta(seconds=_LEASE_SECONDS)
    eligible = and_(
        PendingCleanup.done.is_(False),
        or_(PendingCleanup.next_attempt_at.is_(None), PendingCleanup.next_attempt_at <= now),
        or_(PendingCleanup.locked_at.is_(None), PendingCleanup.locked_at <= stale_before),
    )
    async with plain_session() as session:
        rows = (
            await session.execute(
                select(
                    PendingCleanup.id,
                    PendingCleanup.kind,
                    PendingCleanup.payload,
                    PendingCleanup.attempts,
                )
                .where(eligible)
                .order_by(PendingCleanup.created_at, PendingCleanup.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not rows:
            return []

        claims: list[_Claim] = []
        for row_id, kind, payload, attempts in rows:
            # ``locked_by`` stores a per-claim capability, not the process or
            # worker name. A stale invocation can therefore never renew or
            # finalize a row after another invocation reclaimed it.
            lease_token = uuid.uuid4().hex
            result = await session.execute(
                update(PendingCleanup)
                .where(PendingCleanup.id == row_id, PendingCleanup.done.is_(False))
                .values(
                    attempts=PendingCleanup.attempts + 1,
                    last_attempt_at=now,
                    next_attempt_at=None,
                    locked_at=now,
                    locked_by=lease_token,
                    progress_json={
                        "phase": "executing",
                        "worker_id": worker_id,
                        "claim_id": str(row_id),
                    },
                )
            )
            if result.rowcount == 1:
                claims.append(
                    _Claim(
                        row_id=row_id,
                        kind=kind,
                        payload=payload or {},
                        attempts=int(attempts) + 1,
                        lease_token=lease_token,
                    )
                )
        return claims


async def _renew_leases(claims: list[_Claim], stop: asyncio.Event) -> None:
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup

    remaining = claims
    interval = max(1.0, _LEASE_SECONDS / 3)
    while remaining:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        try:
            now = _now()
            still_owned: list[_Claim] = []
            async with plain_session() as session:
                for claim in remaining:
                    result = await session.execute(
                        update(PendingCleanup)
                        .where(
                            PendingCleanup.id == claim.row_id,
                            PendingCleanup.done.is_(False),
                            PendingCleanup.locked_by == claim.lease_token,
                        )
                        .values(locked_at=now)
                    )
                    if result.rowcount == 1:
                        still_owned.append(claim)
            remaining = still_owned
        except Exception:  # noqa: BLE001 - a later owner/error path remains authoritative
            log.warning("cleanup_outbox_lease_renew_failed", claims=len(remaining))


async def _execute_cleanup(ctx: dict, claim: _Claim) -> None:
    payload = validate_cleanup_payload(claim.kind, claim.payload)
    if claim.kind == "doc":
        await cleanup_document(
            ctx,
            payload["tenant_id"],
            payload["kb_id"],
            payload["doc_id"],
            payload["filename"],
        )
    elif claim.kind == "kb":
        await cleanup_kb(ctx, payload["tenant_id"], payload["kb_id"])
    elif claim.kind == "tenant":
        await purge_tenant(ctx, payload["tenant_id"], payload.get("session_ids"))
    else:
        from eduagents_core.config import get_settings
        from eduagents_core.storage import create_storage

        settings = ctx.get("settings") or get_settings()
        await create_storage(settings).delete_prefix(payload["prefix"])


async def _mark_completed(claim: _Claim) -> bool:
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup

    async with plain_session() as session:
        result = await session.execute(
            update(PendingCleanup)
            .where(
                PendingCleanup.id == claim.row_id,
                PendingCleanup.done.is_(False),
                PendingCleanup.locked_by == claim.lease_token,
            )
            .values(
                done=True,
                completed_at=_now(),
                next_attempt_at=None,
                locked_at=None,
                locked_by=None,
                progress_json={"phase": "completed"},
            )
        )
    return result.rowcount == 1


async def _mark_failed(claim: _Claim, exc: BaseException) -> bool:
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup

    code = _error_code(exc)
    error_text = _error_text(exc)
    now = _now()
    next_attempt = now + timedelta(seconds=retry_delay_seconds(claim.attempts))
    async with plain_session() as session:
        result = await session.execute(
            update(PendingCleanup)
            .where(
                PendingCleanup.id == claim.row_id,
                PendingCleanup.done.is_(False),
                PendingCleanup.locked_by == claim.lease_token,
            )
            .values(
                last_error_code=code,
                last_error_text=error_text,
                next_attempt_at=next_attempt,
                locked_at=None,
                locked_by=None,
                progress_json={"phase": "retry_wait", "error_code": code},
            )
        )
    return result.rowcount == 1


async def _metrics(now: datetime, success: int, failure: int) -> None:
    from eduagents_api.db import plain_session
    from eduagents_api.models import PendingCleanup

    try:
        async with plain_session() as session:
            pending_filter = PendingCleanup.done.is_(False)
            pending = int(
                await session.scalar(
                    select(func.count()).select_from(PendingCleanup).where(pending_filter)
                )
                or 0
            )
            oldest = await session.scalar(
                select(func.min(PendingCleanup.created_at))
                .select_from(PendingCleanup)
                .where(pending_filter)
            )
            stuck = int(
                await session.scalar(
                    select(func.count())
                    .select_from(PendingCleanup)
                    .where(
                        pending_filter,
                        or_(
                            PendingCleanup.attempts >= _ALERT_ATTEMPTS,
                            PendingCleanup.created_at <= now - timedelta(seconds=_STUCK_AGE_SECONDS),
                        ),
                    )
                )
                or 0
            )
        oldest_age = None
        if oldest is not None:
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            oldest_age = max(0, int((now - oldest).total_seconds()))
        log.info(
            "cleanup_outbox_metrics",
            pending=pending,
            oldest_age_seconds=oldest_age,
            success=success,
            failure=failure,
            stuck=stuck,
        )
    except Exception:  # noqa: BLE001 - metrics must not stop cleanup retries
        log.warning("cleanup_outbox_metrics_failed")


async def _process_claim(
    ctx: dict,
    claim: _Claim,
) -> tuple[bool, bool]:
    try:
        await _execute_cleanup(ctx, claim)
    except Exception as exc:  # noqa: BLE001 - one poison row must not block its batch
        try:
            updated = await _mark_failed(claim, exc)
        except Exception:  # noqa: BLE001 - lease expiry makes the row recoverable
            log.warning("cleanup_outbox_failure_record_failed", id=str(claim.row_id))
            return False, True
        if updated:
            code = _error_code(exc)
            event = "cleanup_outbox_stuck" if claim.attempts >= _ALERT_ATTEMPTS else "cleanup_outbox_retry"
            getattr(log, "error" if event.endswith("stuck") else "warning")(
                event,
                id=str(claim.row_id),
                kind=claim.kind,
                attempts=claim.attempts,
                error_code=code,
            )
        else:
            log.warning("cleanup_outbox_lease_lost", id=str(claim.row_id), kind=claim.kind)
        return False, True

    try:
        completed = await _mark_completed(claim)
    except Exception:  # noqa: BLE001 - a committed lease can be reclaimed later
        log.warning("cleanup_outbox_completion_record_failed", id=str(claim.row_id))
        return False, False
    if completed:
        log.info("cleanup_outbox_done", id=str(claim.row_id), kind=claim.kind)
        return True, False
    log.warning("cleanup_outbox_lease_lost", id=str(claim.row_id), kind=claim.kind)
    return False, False


async def process_pending_cleanup(ctx: dict) -> int:
    """Claim a smaller batch and consume it while retaining cleanup ordering."""
    worker_id = _worker_id(ctx)
    try:
        claims = await _claim_pending_cleanup(worker_id)
    except Exception:  # noqa: BLE001 - cron must survive a transient database outage
        log.warning("cleanup_outbox_claim_failed")
        return 0

    completed = 0
    failures = 0
    heartbeat_stop = asyncio.Event()
    heartbeat = (
        asyncio.create_task(_renew_leases(claims, heartbeat_stop)) if claims else None
    )
    try:
        for claim in claims:
            succeeded, failed = await _process_claim(ctx, claim)
            completed += int(succeeded)
            failures += int(failed)
    finally:
        heartbeat_stop.set()
        if heartbeat is not None:
            await heartbeat

    await _metrics(_now(), completed, failures)
    if claims:
        log.info(
            "cleanup_outbox_processed",
            scanned=len(claims),
            done=completed,
            failures=failures,
        )
    return completed
