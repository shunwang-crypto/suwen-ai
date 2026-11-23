#!/usr/bin/env python3
"""Read-only pending-cleanup root-cause diagnostic.

This command is intentionally an inspection tool, not a worker launcher.  It
uses a dedicated ``READONLY_DATABASE_URL`` by default, sets every PostgreSQL
transaction read-only, and only performs metadata/count operations against
Qdrant, object storage, Redis, and checkpoint tables.  It never exposes a
payload, object key, point vector, document body, or raw row ID.

Production/staging invocation requires an explicit target label and explicit
read-only service endpoints, for example::

    READONLY_DATABASE_URL='postgresql+asyncpg://...' \
    READONLY_QDRANT_URL='https://...' \
    READONLY_REDIS_URL='rediss://...' \
    READONLY_CLEANUP_LOG_FILE='/var/log/eduagents/worker.ndjson' \
    READONLY_STORAGE_BACKEND=minio \
    python scripts/diagnose_pending_cleanup.py \
      --target-id prod-eu-1 --environment prod --json

Exit codes: 0=no pending rows, 1=explainable pending/drift, 2=incomplete or
service error, 3=target/permission contract is not satisfied.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from eduagents_core.config import get_settings
from eduagents_core.vectorstore.qdrant_store import QdrantStore
from eduagents_worker.cleanup_outbox import CleanupPayloadError, validate_cleanup_payload
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

_LEASE_SECONDS = 5 * 60
_STUCK_AGE_SECONDS = 2 * 60
_ALERT_ATTEMPTS = 5
_DEFAULT_PAGE_SIZE = 100
_DEFAULT_MAX_ROWS = 10_000
_DEFAULT_MAX_QDRANT_POINTS = 1_000_000
_DEFAULT_MAX_OBJECTS = 100_000
_DEFAULT_MAX_REDIS_KEYS = 100_000
_DEFAULT_MAX_LOG_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_LOG_LINES = 100_000
_MAX_PAGE_SIZE = 10_000
_MAX_MAX_ROWS = 1_000_000
_MAX_QDRANT_PAGE_SIZE = 1_000
_MAX_QDRANT_MAX_PAGES = 1_000
_MAX_TIMEOUT_SECONDS = 300.0
_ALLOWED_LOG_EVENTS = frozenset(
    {
        "cleanup_outbox_retry",
        "cleanup_outbox_stuck",
        "cleanup_outbox_lease_lost",
        "cleanup_outbox_completion_record_failed",
        "cleanup_outbox_failure_record_failed",
        "cleanup_outbox_done",
        "cleanup_outbox_claim_failed",
    }
)
_ALLOWED_PROGRESS_PHASES = frozenset({"executing", "retry_wait", "completed"})
_ALLOWED_PROGRESS_COMPONENTS = frozenset(
    {"qdrant", "storage", "redis", "checkpoint"}
)


class DiagnosticContractError(RuntimeError):
    """The target or read-only contract is not safe to inspect."""


class InspectionLimitError(RuntimeError):
    """A bounded metadata scan reached its configured safety limit."""


@dataclass(frozen=True, slots=True)
class PendingRow:
    row_id: str
    kind: str
    payload: object
    created_at: datetime
    attempts: int
    done: bool
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error_code: str | None = None
    locked_at: datetime | None = None
    locked_by: str | None = None
    progress_json: object | None = None


@dataclass(frozen=True, slots=True)
class ExternalSnapshot:
    available: bool = True
    qdrant_points: int | None = None
    object_objects: int | None = None
    redis_keys: int | None = None
    checkpoint_rows: int | None = None
    error_code: str | None = None

    @property
    def residual_types(self) -> list[str]:
        fields = (
            ("qdrant", self.qdrant_points),
            ("storage", self.object_objects),
            ("redis", self.redis_keys),
            ("checkpoint", self.checkpoint_rows),
        )
        return [name for name, count in fields if count is not None and count > 0]


@dataclass
class QdrantIndex:
    available: bool = True
    error_code: str | None = None
    malformed_points: int = 0
    doc_points: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    kb_points: Counter[tuple[str, str]] = field(default_factory=Counter)
    tenant_points: Counter[str] = field(default_factory=Counter)
    scanned_points: int = 0


@dataclass
class LogCorrelation:
    """Bounded correlation evidence extracted from a read-only JSON log file."""

    available: bool
    error_code: str | None = None
    scanned_lines: int = 0
    event_counts: Counter[str] = field(default_factory=Counter)
    per_row: dict[str, Counter[str]] = field(default_factory=dict)

    @property
    def matched_events(self) -> int:
        return sum(self.event_counts.values())


@dataclass
class DiagnosticReport:
    metadata: dict[str, Any]
    summary: dict[str, Any]
    rows: list[dict[str, Any]]
    incomplete: bool = False
    service_errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.incomplete or self.service_errors:
            return "error"
        if self.summary.get("pending_total", 0):
            return "pending"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "metadata": self.metadata,
            "summary": self.summary,
            "rows": self.rows,
            "service_errors": sorted(set(self.service_errors)),
        }


def _hash_id(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _hash_label(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _safe_code(value: object, default: str = "unknown") -> str:
    text_value = " ".join(str(value or default).split())
    if re.search(
        r"(?i)(?:https?://|s3://|redis://|postgres(?:ql)?://|\b(?:bearer|basic|password|passwd|token|secret|authorization|credential|api[_-]?key)\b)",
        text_value,
    ):
        return "REDACTED_CODE"
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", text_value)[:96] or default


def _canonical_uuid(value: object) -> str | None:
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _qdrant_ref_value(ref: object, name: str) -> object | None:
    if isinstance(ref, Mapping):
        return ref.get(name)
    return getattr(ref, name, None)


def _normalise_qdrant_ref(
    ref: object, *, require_doc_id: bool
) -> tuple[str, str, str] | None:
    """Return only canonical ownership IDs; malformed refs never enter counts."""
    point_id = _canonical_uuid(_qdrant_ref_value(ref, "point_id"))
    tenant_id = _canonical_uuid(_qdrant_ref_value(ref, "tenant_id"))
    kb_id = _canonical_uuid(_qdrant_ref_value(ref, "kb_id"))
    doc_id = _canonical_uuid(_qdrant_ref_value(ref, "doc_id"))
    if point_id is None or tenant_id is None or kb_id is None:
        return None
    if require_doc_id and doc_id is None:
        return None
    return tenant_id, kb_id, doc_id or ""


def _redact_progress(value: object | None, row_id: str) -> dict[str, Any] | None:
    """Project progress JSON onto a fixed, non-sensitive diagnostic shape."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return {"status": "malformed"}

    malformed = False
    phase = value.get("phase")
    if phase not in _ALLOWED_PROGRESS_PHASES:
        phase = "unknown"
        malformed = True

    result: dict[str, Any] = {"status": "ok", "phase": phase}
    worker_id = value.get("worker_id")
    if isinstance(worker_id, str) and len(worker_id) <= 128:
        result["worker_id_hash"] = _hash_id(worker_id)
    elif worker_id is not None:
        malformed = True

    claim_id = value.get("claim_id")
    if isinstance(claim_id, str) and len(claim_id) <= 128:
        result["claim_id_matches_row"] = claim_id == row_id
    elif claim_id is not None:
        malformed = True

    current_step = value.get("current_step")
    if current_step is None:
        result["current_step"] = None
    elif current_step in _ALLOWED_PROGRESS_COMPONENTS:
        result["current_step"] = current_step
    else:
        result["current_step"] = "unknown"
        malformed = True

    completed_steps = value.get("completed_steps")
    if completed_steps is None:
        result["completed_steps"] = []
    elif isinstance(completed_steps, list) and len(completed_steps) <= 16:
        safe_steps = [
            step for step in completed_steps if step in _ALLOWED_PROGRESS_COMPONENTS
        ]
        if len(safe_steps) != len(completed_steps):
            malformed = True
        result["completed_steps"] = list(dict.fromkeys(safe_steps))
    else:
        result["completed_steps"] = []
        malformed = True

    error_code = value.get("error_code")
    if error_code is not None:
        if isinstance(error_code, str) and len(error_code) <= 256:
            result["error_code"] = _safe_code(error_code)
        else:
            malformed = True

    if set(value) - {
        "phase",
        "worker_id",
        "claim_id",
        "current_step",
        "completed_steps",
        "error_code",
    }:
        malformed = True
    if malformed:
        result["status"] = "malformed"
    return result


def _load_log_correlation(
    path: str | os.PathLike[str] | None,
    row_ids: set[str],
    *,
    max_bytes: int = _DEFAULT_MAX_LOG_BYTES,
    max_lines: int = _DEFAULT_MAX_LOG_LINES,
) -> LogCorrelation:
    """Read bounded JSON logs without exposing arbitrary log fields.

    Logs are a diagnostic input, never a source of truth.  A malformed or
    truncated stream is reported as incomplete even when some events matched.
    """
    if not path:
        return LogCorrelation(False, "CLEANUP_LOG_SOURCE_NOT_CONFIGURED")
    if max_bytes <= 0 or max_lines <= 0:
        return LogCorrelation(False, "CLEANUP_LOG_LIMIT_INVALID")

    raw_to_row = {str(row_id): _hash_id(row_id) for row_id in row_ids}
    canonical_to_row = {
        canonical: _hash_id(row_id)
        for row_id in row_ids
        if (canonical := _canonical_uuid(row_id)) is not None
    }
    counts: Counter[str] = Counter()
    per_row: dict[str, Counter[str]] = {}
    scanned_lines = 0
    parse_error = False
    try:
        log_path = Path(path)
        if not log_path.is_file():
            return LogCorrelation(False, "CLEANUP_LOG_SOURCE_UNAVAILABLE")
        if log_path.stat().st_size > max_bytes:
            return LogCorrelation(False, "CLEANUP_LOG_SIZE_LIMIT")
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                scanned_lines += 1
                if scanned_lines > max_lines:
                    return LogCorrelation(
                        False,
                        "CLEANUP_LOG_LINE_LIMIT",
                        scanned_lines - 1,
                        counts,
                        per_row,
                    )
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    parse_error = True
                    continue
                if not isinstance(record, dict):
                    parse_error = True
                    continue
                event = record.get("event")
                if not isinstance(event, str) or event not in _ALLOWED_LOG_EVENTS:
                    continue
                raw_id = record.get("id")
                if not isinstance(raw_id, str):
                    continue
                row_id_hash = raw_to_row.get(raw_id)
                if row_id_hash is None:
                    row_id_hash = canonical_to_row.get(_canonical_uuid(raw_id))
                if row_id_hash is None:
                    continue
                counts[event] += 1
                per_row.setdefault(row_id_hash, Counter())[event] += 1
    except (OSError, UnicodeError):
        return LogCorrelation(
            False, "CLEANUP_LOG_SOURCE_UNAVAILABLE", scanned_lines, counts, per_row
        )

    return LogCorrelation(
        not parse_error,
        "CLEANUP_LOG_PARSE_FAILED" if parse_error else None,
        scanned_lines,
        counts,
        per_row,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _age_seconds(created_at: datetime, now: datetime) -> int:
    return max(0, int((now - (_aware(created_at) or now)).total_seconds()))


def _age_bucket(age_seconds: int) -> str:
    if age_seconds < 5 * 60:
        return "<5m"
    if age_seconds < 60 * 60:
        return "5m-1h"
    if age_seconds < 24 * 60 * 60:
        return "1h-24h"
    if age_seconds < 7 * 24 * 60 * 60:
        return "1d-7d"
    return ">=7d"


def _attempt_bucket(attempts: int) -> str:
    if attempts <= 0:
        return "0"
    if attempts == 1:
        return "1"
    if attempts <= 3:
        return "2-3"
    if attempts <= 9:
        return "4-9"
    return ">=10"


def _ratio_counts(counter: Counter[str], total: int) -> dict[str, dict[str, float | int]]:
    return {
        key: {"count": count, "ratio": round(count / total, 6) if total else 0.0}
        for key, count in sorted(counter.items())
    }


def _state(row: PendingRow, now: datetime) -> str:
    locked_at = _aware(row.locked_at)
    if row.locked_by and locked_at is not None:
        if locked_at > now - timedelta(seconds=_LEASE_SECONDS):
            return "active_lease"
        return "expired_lease"
    if row.locked_by or locked_at:
        return "security_anomaly"
    if row.next_attempt_at is not None and (_aware(row.next_attempt_at) or now) > now:
        return "backoff"
    return "executable"


def _stuck(row: PendingRow, now: datetime) -> bool:
    return row.attempts >= _ALERT_ATTEMPTS or _age_seconds(row.created_at, now) >= _STUCK_AGE_SECONDS


def _payload_error(row: PendingRow) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    try:
        normalized = validate_cleanup_payload(row.kind, row.payload)
    except CleanupPayloadError as exc:
        return "payload_error", None, {"code": _safe_code(exc.code), "type": "payload"}
    except Exception:
        return "payload_error", None, {"code": "INVALID_PAYLOAD", "type": "payload"}

    allowed = {
        "doc": {"tenant_id", "kb_id", "doc_id", "filename"},
        "kb": {"tenant_id", "kb_id"},
        "tenant": {"tenant_id", "session_ids"},
        "object_prefix": {"prefix"},
    }.get(row.kind, set())
    if isinstance(row.payload, dict) and set(row.payload) - allowed:
        return "security_anomaly", normalized, {
            "code": "UNEXPECTED_PAYLOAD_FIELDS",
            "type": "payload_shape",
        }
    return None, normalized, None


def _prefixes(kind: str, payload: dict[str, Any]) -> list[str]:
    if kind == "doc":
        return [f"{payload['tenant_id']}/{payload['kb_id']}/{payload['doc_id']}"]
    if kind == "kb":
        return [f"{payload['tenant_id']}/{payload['kb_id']}"]
    if kind == "tenant":
        return [str(payload["tenant_id"]), f"exports/{payload['tenant_id']}"]
    if kind == "object_prefix":
        return [str(payload["prefix"])]
    return []


def _residue_state(snapshot: ExternalSnapshot, kind: str) -> str:
    if not snapshot.available:
        return "external_unavailable"
    if kind == "object_prefix":
        dimensions = [snapshot.object_objects or 0]
    elif kind in {"doc", "kb"}:
        dimensions = [snapshot.qdrant_points or 0, snapshot.object_objects or 0]
    elif kind == "tenant":
        dimensions = [
            snapshot.qdrant_points or 0,
            snapshot.object_objects or 0,
            snapshot.redis_keys or 0,
            snapshot.checkpoint_rows or 0,
        ]
    else:
        return "unknown_kind"
    if all(value > 0 for value in dimensions):
        return "fully_residual"
    if any(value > 0 for value in dimensions):
        return "partially_cleaned"
    return "already_cleaned_but_pending"


def _root_cause(
    row: PendingRow,
    state: str,
    classification: str,
    snapshot: ExternalSnapshot,
) -> str:
    if classification in {"invalid_payload", "unknown_kind", "security_anomaly"}:
        return classification
    if classification == "external_unavailable":
        return snapshot.error_code or "external_unavailable"
    if state in {"active_lease", "expired_lease", "backoff", "security_anomaly"}:
        return state
    residual = snapshot.residual_types
    if residual == ["storage"]:
        return "qdrant_already_cleaned_storage_residual"
    if residual == ["qdrant"]:
        return "storage_already_cleaned_qdrant_residual"
    if "redis" in residual or "checkpoint" in residual:
        return "redis_or_checkpoint_residual"
    if row.last_error_code:
        return _safe_code(row.last_error_code)
    return classification


def _snapshot_for_invalid(kind: str) -> ExternalSnapshot:
    return ExternalSnapshot(available=False, error_code="NOT_EXECUTABLE")


def _external_error_code(prefix: str, exc: BaseException) -> str:
    name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in name or "timed out" in message:
        return f"{prefix}_READ_TIMEOUT"
    if isinstance(exc, PermissionError) or "permission" in name or "permission" in message:
        return f"{prefix}_PERMISSION_DENIED"
    if isinstance(exc, InspectionLimitError):
        return f"{prefix}_LIST_LIMIT"
    return f"{prefix}_READ_FAILED"


def _qdrant_error_code(exc: BaseException) -> str:
    if isinstance(exc, InspectionLimitError):
        return "QDRANT_SCAN_LIMIT"
    message = str(exc).casefold()
    if "point scan point limit exceeded" in message or "point scan page limit exceeded" in message:
        return "QDRANT_SCAN_LIMIT"
    if "repeated offset" in message:
        return "QDRANT_PAGINATION_ERROR"
    return _external_error_code("QDRANT", exc)


async def _scan_qdrant(
    settings,
    *,
    page_size: int,
    max_pages: int,
    max_points: int,
    timeout_seconds: float = 10.0,
) -> QdrantIndex:
    index = QdrantIndex()
    if (
        page_size <= 0
        or page_size > _MAX_QDRANT_PAGE_SIZE
        or max_pages <= 0
        or max_pages > _MAX_QDRANT_MAX_PAGES
        or max_points <= 0
        or max_points > _DEFAULT_MAX_QDRANT_POINTS
    ):
        index.available = False
        index.error_code = "QDRANT_SCAN_LIMIT"
        return index
    store = None
    try:
        store = QdrantStore(settings)
        scanned = 0
        # Preserve order while avoiding a second scan when a misconfigured
        # primary collection is already named answer_cache.
        collections = tuple(dict.fromkeys((settings.qdrant_collection, "answer_cache")))
        for collection in collections:
            remaining = max_points - scanned
            if remaining <= 0:
                # Do not probe the next collection: doing so would exceed the
                # caller's hard point budget.  A conservative incomplete
                # result is safer than claiming the full inventory was read.
                index.available = False
                index.error_code = "QDRANT_SCAN_LIMIT"
                break
            refs = await store.scan_point_refs(
                collection=collection,
                page_size=min(page_size, remaining),
                max_pages=max_pages,
                timeout=max(1, int(timeout_seconds)),
                max_points=remaining,
            )
            scanned += len(refs)
            index.scanned_points = scanned
            is_document_collection = collection != "answer_cache"
            for ref in refs:
                normalised = _normalise_qdrant_ref(
                    ref, require_doc_id=is_document_collection
                )
                if normalised is None:
                    index.malformed_points += 1
                    continue
                tenant, kb, doc = normalised
                if tenant:
                    index.tenant_points[tenant] += 1
                # answer_cache has tenant_id/kb_id but is not removed by the
                # doc/kb cleanup handlers.  It belongs in tenant-purge counts
                # only; otherwise a cache point makes a completed doc/kb look
                # like it still has vector residue.
                if is_document_collection and tenant and kb:
                    index.kb_points[(tenant, kb)] += 1
                if is_document_collection and tenant and kb and doc:
                    index.doc_points[(tenant, kb, doc)] += 1
        if index.malformed_points:
            index.available = False
            index.error_code = "QDRANT_MALFORMED_PAYLOAD"
    except Exception as exc:
        index.available = False
        index.error_code = _qdrant_error_code(exc)
    finally:
        if store is not None:
            try:
                store.close()
            except Exception as exc:  # noqa: BLE001 - close failure is a read failure
                index.available = False
                index.error_code = _qdrant_error_code(exc)
    return index


class ObjectInspector:
    """HEAD/LIST-only object prefix inspector."""

    def __init__(self, settings, *, max_objects: int = _DEFAULT_MAX_OBJECTS) -> None:
        self._settings = settings
        self._max_objects = max_objects
        self._cache: dict[str, tuple[int | None, str | None]] = {}

    async def count(self, prefix: str) -> tuple[int | None, str | None]:
        if prefix in self._cache:
            return self._cache[prefix]
        try:
            if self._settings.storage_backend == "local":
                root = Path(
                    self._settings.local_storage_dir
                    or Path.home() / ".local/share/eduagents/objects"
                )
                target = root / prefix
                if not target.exists():
                    result = (0, None)
                elif target.is_file():
                    result = (1, None)
                else:
                    count = 0
                    for item in target.rglob("*"):
                        if item.is_file():
                            count += 1
                            if count > self._max_objects:
                                raise InspectionLimitError("object list limit exceeded")
                    result = (count, None)
            else:
                from minio import Minio

                client = Minio(
                    self._settings.minio_endpoint,
                    access_key=self._settings.minio_app_access_key,
                    secret_key=self._settings.minio_app_secret_key,
                    secure=self._settings.minio_secure,
                )

                def _list() -> int:
                    # list_objects is explicitly the allowed LIST operation;
                    # never call get_object or download a body.
                    count = 0
                    for _ in client.list_objects(
                        self._settings.minio_bucket, prefix=prefix, recursive=True
                    ):
                        count += 1
                        if count > self._max_objects:
                            raise InspectionLimitError("object list limit exceeded")
                    return count

                result = (await asyncio.to_thread(_list), None)
        except Exception as exc:
            result = (None, _external_error_code("STORAGE", exc))
            self._cache[prefix] = result
            return result
        self._cache[prefix] = result
        return result


class RedisInspector:
    """Redis inspector restricted to SCAN/EXISTS/TTL calls."""

    def __init__(self, redis, *, max_keys: int = _DEFAULT_MAX_REDIS_KEYS) -> None:
        self._redis = redis
        self._max_keys = max_keys
        self._tenant_cache: dict[tuple[str, tuple[str, ...]], tuple[int | None, str | None]] = {}

    async def tenant_count(
        self, tenant_id: str, session_ids: list[str] | None
    ) -> tuple[int | None, str | None]:
        key = (tenant_id, tuple(session_ids or ()))
        if key in self._tenant_cache:
            return self._tenant_cache[key]
        try:
            count = 0

            async def _observe(exact: str) -> None:
                nonlocal count
                if await self._redis.exists(exact):
                    await self._redis.ttl(exact)
                    count += 1
                    if count > self._max_keys:
                        raise InspectionLimitError("redis scan limit exceeded")

            for exact in (
                f"orgs:{tenant_id}",
                f"export:{tenant_id}",
                f"export:pending:{tenant_id}",
            ):
                await _observe(exact)
            for session_id in session_ids or []:
                await _observe(f"reflect:{session_id}")
            for pattern in (f"profile_summary:*:{tenant_id}*", f"budget:{tenant_id}:*"):
                async for found in self._redis.scan_iter(match=pattern, count=500):
                    # The key itself is not retained or output; TTL is the
                    # only value read for an observed key.
                    await self._redis.ttl(found)
                    count += 1
                    if count > self._max_keys:
                        raise InspectionLimitError("redis scan limit exceeded")
            result = (count, None)
        except Exception as exc:
            result = (None, _external_error_code("REDIS", exc))
        self._tenant_cache[key] = result
        return result


async def _checkpoint_count(
    conn: AsyncConnection, tenant_id: str
) -> tuple[int | None, str | None]:
    try:
        total = 0
        for table in ("harness_sessions", "checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            exists = await conn.scalar(
                text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
            )
            if exists is None:
                continue
            value = await conn.scalar(
                text(f"SELECT count(*) FROM {table} WHERE thread_id LIKE :pattern"),
                {"pattern": f"%:{tenant_id}:%"},
            )
            total += int(value or 0)
        return total, None
    except Exception as exc:
        return None, _external_error_code("CHECKPOINT", exc)


async def _load_rows(
    conn: AsyncConnection,
    *,
    page_size: int,
    max_rows: int,
    timeout_seconds: float,
) -> tuple[int, datetime | None, list[PendingRow], tuple[str, ...]]:
    # ``SET TRANSACTION READ ONLY`` is the first transaction command.  The
    # timeout is also transaction-local and is not a write to application data.
    await conn.execute(text("SET TRANSACTION READ ONLY"))
    await conn.execute(
        text("SELECT set_config('statement_timeout', :value, true)"),
        {"value": str(max(1, int(timeout_seconds * 1000)))},
    )
    totals = (
        await conn.execute(
            text(
                "SELECT count(*) AS pending_total, min(created_at) AS oldest_created_at "
                "FROM pending_cleanup WHERE done IS FALSE"
            )
        )
    ).mappings().one()
    pending_total = int(totals["pending_total"] or 0)
    oldest = _aware(totals["oldest_created_at"])
    rows: list[PendingRow] = []
    offset = 0
    while offset < min(pending_total, max_rows):
        result = await conn.execute(
            text(
                "SELECT id, kind, payload, created_at, attempts, done, "
                "last_attempt_at, next_attempt_at, last_error_code, locked_at, "
                "(locked_by IS NOT NULL) AS has_locked_by, progress_json "
                "FROM pending_cleanup WHERE done IS FALSE "
                "ORDER BY created_at, id LIMIT :limit OFFSET :offset"
            ),
            {"limit": min(page_size, max_rows - offset), "offset": offset},
        )
        page = result.mappings().all()
        if not page:
            break
        rows.extend(
            PendingRow(
                row_id=str(item["id"]),
                kind=str(item["kind"]),
                payload=item["payload"],
                created_at=item["created_at"],
                attempts=int(item["attempts"] or 0),
                done=bool(item["done"]),
                last_attempt_at=item["last_attempt_at"],
                next_attempt_at=item["next_attempt_at"],
                last_error_code=item["last_error_code"],
                locked_at=item["locked_at"],
                locked_by="present" if item["has_locked_by"] else None,
                progress_json=item.get("progress_json"),
            )
            for item in page
        )
        offset += len(page)
        if len(page) < page_size:
            break
    heads: tuple[str, ...]
    try:
        head_rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
        heads = tuple(sorted(str(row[0]) for row in head_rows.all()))
    except Exception:
        heads = ("unknown",)
    return pending_total, oldest, rows, heads


def _validate_target(args: argparse.Namespace) -> None:
    if not args.target_id or not args.target_id.strip():
        raise DiagnosticContractError("explicit --target-id is required")
    if args.environment not in {"prod", "staging"}:
        raise DiagnosticContractError("production acceptance requires --environment prod or staging")
    target = args.target_id.casefold()
    if target in {"dev", "local", "localhost", "test", "development"}:
        raise DiagnosticContractError("local/dev target is not an acceptance target")
    required = {
        "READONLY_DATABASE_URL": args.database_url,
        "READONLY_QDRANT_URL": args.qdrant_url,
        "READONLY_REDIS_URL": args.redis_url,
    }
    if any(not value for value in required.values()):
        missing = ",".join(name for name, value in required.items() if not value)
        raise DiagnosticContractError(f"missing dedicated read-only endpoint: {missing}")
    for label, value, schemes in (
        ("database", args.database_url, {"postgresql", "postgresql+asyncpg"}),
        ("qdrant", args.qdrant_url, {"http", "https"}),
        ("redis", args.redis_url, {"redis", "rediss"}),
    ):
        parsed = urlparse(value)
        if parsed.scheme not in schemes or not parsed.hostname:
            raise DiagnosticContractError(f"{label} endpoint scheme/host is invalid")
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            raise DiagnosticContractError(f"{label} endpoint points at local dev")
    if args.storage_backend != "minio":
        raise DiagnosticContractError("production acceptance requires read-only MinIO/LIST access")
    minio_endpoint = str(getattr(args, "minio_endpoint", "") or "")
    if not minio_endpoint:
        raise DiagnosticContractError("read-only MinIO endpoint is required")
    minio_url = minio_endpoint if "://" in minio_endpoint else f"//{minio_endpoint}"
    minio_host = urlparse(minio_url).hostname
    if not minio_host:
        raise DiagnosticContractError("MinIO endpoint host is invalid")
    if minio_host in {"127.0.0.1", "localhost", "::1"}:
        raise DiagnosticContractError("storage endpoint points at local dev")


async def _row_report(
    row: PendingRow,
    *,
    now: datetime,
    qdrant: QdrantIndex,
    storage: ObjectInspector,
    redis: RedisInspector,
    conn: AsyncConnection,
    timeout_seconds: float = 30.0,
    log_correlation: LogCorrelation | None = None,
) -> tuple[dict[str, Any], ExternalSnapshot]:
    state = _state(row, now)
    payload_status, payload, payload_error = _payload_error(row)
    if row.kind not in {"doc", "kb", "tenant", "object_prefix"}:
        classification = "unknown_kind"
        snapshot = _snapshot_for_invalid(row.kind)
    elif payload_status == "payload_error":
        classification = "invalid_payload"
        snapshot = _snapshot_for_invalid(row.kind)
    elif payload_status == "security_anomaly":
        classification = "security_anomaly"
        snapshot = _snapshot_for_invalid(row.kind)
    else:
        assert payload is not None
        prefixes = _prefixes(row.kind, payload)
        object_count: int | None = None
        object_error: str | None = None
        if prefixes:
            object_count = 0
            for prefix in prefixes:
                try:
                    count, error = await asyncio.wait_for(
                        storage.count(prefix), timeout=timeout_seconds
                    )
                except TimeoutError:
                    count, error = None, "STORAGE_READ_TIMEOUT"
                if count is not None:
                    object_count += count
                if error is not None:
                    object_error = error
        qdrant_count: int | None = None
        qdrant_malformed = qdrant.malformed_points > 0
        if qdrant.available and not qdrant_malformed:
            if row.kind == "doc":
                qdrant_count = qdrant.doc_points[
                    (payload["tenant_id"], payload["kb_id"], payload["doc_id"])
                ]
            elif row.kind == "kb":
                qdrant_count = qdrant.kb_points[(payload["tenant_id"], payload["kb_id"])]
            elif row.kind == "tenant":
                qdrant_count = qdrant.tenant_points[payload["tenant_id"]]
        qdrant_error = (
            qdrant.error_code
            if not qdrant.available
            else "QDRANT_MALFORMED_PAYLOAD"
            if qdrant_malformed
            else None
        )
        redis_count: int | None = None
        redis_error: str | None = None
        checkpoint_count: int | None = None
        checkpoint_error: str | None = None
        if row.kind == "tenant":
            try:
                redis_count, redis_error = await asyncio.wait_for(
                    redis.tenant_count(payload["tenant_id"], payload.get("session_ids")),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                redis_count, redis_error = None, "REDIS_READ_TIMEOUT"
            try:
                checkpoint_count, checkpoint_error = await asyncio.wait_for(
                    _checkpoint_count(conn, payload["tenant_id"]), timeout=timeout_seconds
                )
            except TimeoutError:
                checkpoint_count, checkpoint_error = None, "CHECKPOINT_READ_TIMEOUT"
        errors = [code for code in (qdrant_error, object_error, redis_error, checkpoint_error) if code]
        snapshot = ExternalSnapshot(
            available=not errors,
            qdrant_points=qdrant_count,
            object_objects=object_count,
            redis_keys=redis_count,
            checkpoint_rows=checkpoint_count,
            error_code=errors[0] if errors else None,
        )
        classification = _residue_state(snapshot, row.kind)
    report = {
        "row_id_hash": _hash_id(row.row_id),
        "kind": row.kind if row.kind in {"doc", "kb", "tenant", "object_prefix"} else "unknown",
        "attempts": row.attempts,
        "attempt_bucket": _attempt_bucket(row.attempts),
        "age_seconds": _age_seconds(row.created_at, now),
        "age_bucket": _age_bucket(_age_seconds(row.created_at, now)),
        "state": state,
        "stuck": _stuck(row, now),
        "last_error_code": _safe_code(row.last_error_code) if row.last_error_code else None,
        "classification": classification,
        "root_cause": _root_cause(row, state, classification, snapshot),
        "payload_error": payload_error,
        "progress_json": _redact_progress(row.progress_json, row.row_id),
        "log_events": dict(
            sorted(
                (
                    log_correlation.per_row.get(_hash_id(row.row_id), Counter())
                    if log_correlation
                    else {}
                ).items()
            )
        ),
        "residual_types": snapshot.residual_types,
        "residual_counts": {
            "qdrant_points": snapshot.qdrant_points,
            "object_objects": snapshot.object_objects,
            "redis_keys": snapshot.redis_keys,
            "checkpoint_rows": snapshot.checkpoint_rows,
        },
    }
    return report, snapshot


async def _build_report(args: argparse.Namespace) -> DiagnosticReport:
    settings = get_settings().model_copy(
        update={
            "database_url": args.database_url,
            "qdrant_url": args.qdrant_url,
            "qdrant_local_path": "",
            "redis_url": args.redis_url,
            "storage_backend": args.storage_backend,
            "minio_endpoint": args.minio_endpoint,
            "minio_bucket": args.minio_bucket,
            "minio_secure": args.minio_secure,
            "minio_app_access_key": args.minio_access_key,
            "minio_app_secret_key": args.minio_secret_key,
        }
    )
    engine = create_async_engine(args.database_url, pool_pre_ping=True)
    now = datetime.now(UTC)
    service_errors: list[str] = []
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                pending_total, oldest, rows, heads = await _load_rows(
                    conn,
                    page_size=args.page_size,
                    max_rows=args.max_rows,
                    timeout_seconds=args.timeout_seconds,
                )
                log_correlation = _load_log_correlation(
                    getattr(args, "log_file", None),
                    {row.row_id for row in rows},
                )
                if log_correlation.error_code:
                    service_errors.append(log_correlation.error_code)
                qdrant = await _scan_qdrant(
                    settings,
                    page_size=args.qdrant_page_size,
                    max_pages=args.qdrant_max_pages,
                    max_points=args.qdrant_max_points,
                    timeout_seconds=args.timeout_seconds,
                )
                if not qdrant.available:
                    service_errors.append(qdrant.error_code or "QDRANT_READ_FAILED")
                storage = ObjectInspector(settings, max_objects=_DEFAULT_MAX_OBJECTS)
                from redis.asyncio import Redis

                redis_client = Redis.from_url(
                    args.redis_url,
                    decode_responses=True,
                    socket_timeout=args.timeout_seconds,
                    socket_connect_timeout=args.timeout_seconds,
                )
                redis = RedisInspector(redis_client, max_keys=_DEFAULT_MAX_REDIS_KEYS)
                try:
                    row_reports: list[dict[str, Any]] = []
                    for row in rows:
                        redacted, _snapshot = await _row_report(
                            row,
                            now=now,
                            qdrant=qdrant,
                            storage=storage,
                            redis=redis,
                            conn=conn,
                            timeout_seconds=args.timeout_seconds,
                            log_correlation=log_correlation,
                        )
                        row_reports.append(redacted)
                        if redacted["classification"] == "external_unavailable":
                            service_errors.append(str(redacted["root_cause"]))
                finally:
                    await redis_client.aclose()
    finally:
        await engine.dispose()

    age = _age_seconds(oldest, now) if oldest else None
    state_counts = Counter(item["state"] for item in row_reports)
    classification_counts = Counter(item["classification"] for item in row_reports)
    root_causes = Counter(item["root_cause"] for item in row_reports)
    residual_types = Counter(
        residual_type
        for item in row_reports
        for residual_type in item["residual_types"]
    )
    kind_counts = Counter(item["kind"] for item in row_reports)
    attempts = Counter(item["attempt_bucket"] for item in row_reports)
    ages = Counter(item["age_bucket"] for item in row_reports)
    errors = Counter(item["last_error_code"] for item in row_reports if item["last_error_code"])
    stuck = sum(1 for item in row_reports if item["stuck"])
    summary = {
        "pending_total": pending_total,
        "rows_inspected": len(row_reports),
        "oldest_age_seconds": age,
        "executable": state_counts["executable"],
        "backoff": state_counts["backoff"],
        "active_lease": state_counts["active_lease"],
        "expired_lease": state_counts["expired_lease"],
        "stuck": stuck,
        "kind": _ratio_counts(kind_counts, len(row_reports)),
        "attempts": _ratio_counts(attempts, len(row_reports)),
        "last_error_code": _ratio_counts(errors, len(row_reports)),
        "age": _ratio_counts(ages, len(row_reports)),
        "state": _ratio_counts(state_counts, len(row_reports)),
        "classification": _ratio_counts(classification_counts, len(row_reports)),
        "root_cause": _ratio_counts(root_causes, len(row_reports)),
        "residual_type": _ratio_counts(residual_types, len(row_reports)),
        "log_events": dict(sorted(log_correlation.event_counts.items())),
        "log_rows_correlated": len(log_correlation.per_row),
        "row_limit_reached": pending_total > args.max_rows,
    }
    metadata = {
        "target_id_hash": _hash_label(args.target_id),
        "environment": args.environment,
        "captured_at": now.isoformat(),
        "code_version": _safe_code(os.getenv("CODE_VERSION") or os.getenv("APP_VERSION"), "unknown"),
        "migration_heads": list(heads),
        "qdrant_points_scanned": qdrant.scanned_points,
        "qdrant_malformed_points": qdrant.malformed_points,
        "cleanup_logs": {
            "configured": bool(getattr(args, "log_file", None)),
            "available": log_correlation.available,
            "lines_scanned": log_correlation.scanned_lines,
            "events_matched": log_correlation.matched_events,
        },
        "services": {
            "postgres": "readonly_select",
            "qdrant": "metadata_only",
            "object_storage": "head_list_only",
            "redis": "scan_exists_ttl_only",
            "checkpoint": "readonly_count_only",
        },
    }
    incomplete = pending_total > args.max_rows or bool(service_errors)
    return DiagnosticReport(
        metadata=metadata,
        summary=summary,
        rows=row_reports,
        incomplete=incomplete,
        service_errors=service_errors,
    )


def _print_human(report: DiagnosticReport) -> None:
    summary = report.summary
    print(
        f"target={report.metadata['target_id_hash']} env={report.metadata['environment']} "
        f"captured_at={report.metadata['captured_at']}"
    )
    print(
        "pending={pending_total} oldest_age_seconds={oldest_age_seconds} executable={executable} "
        "backoff={backoff} active_lease={active_lease} expired_lease={expired_lease} stuck={stuck}".format(
            **summary
        )
    )
    for row in report.rows:
        print(
            "row={row_id_hash} kind={kind} attempts={attempts} age={age_bucket} "
            "state={state} classification={classification} root_cause={root_cause} "
            "last_error_code={last_error_code} residual={residual_types}".format(**row)
        )
    if report.service_errors:
        print("diagnostic_errors=" + ",".join(sorted(set(report.service_errors))), file=sys.stderr)


def _status_exit_code(report: DiagnosticReport) -> int:
    if any(
        error.endswith("_PERMISSION_DENIED") or error == "READONLY_PERMISSION_DENIED"
        for error in report.service_errors
    ):
        return 3
    if report.incomplete:
        return 2
    return 0 if report.summary.get("pending_total", 0) == 0 else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--environment", choices=("prod", "staging"), required=True)
    parser.add_argument("--database-url", default=os.getenv("READONLY_DATABASE_URL"))
    parser.add_argument("--qdrant-url", default=os.getenv("READONLY_QDRANT_URL"))
    parser.add_argument("--redis-url", default=os.getenv("READONLY_REDIS_URL"))
    parser.add_argument("--storage-backend", default=os.getenv("READONLY_STORAGE_BACKEND", ""))
    parser.add_argument("--minio-endpoint", default=os.getenv("READONLY_MINIO_ENDPOINT", ""))
    parser.add_argument("--minio-bucket", default=os.getenv("READONLY_MINIO_BUCKET", ""))
    parser.add_argument("--minio-access-key", default=os.getenv("READONLY_MINIO_ACCESS_KEY", ""))
    parser.add_argument("--minio-secret-key", default=os.getenv("READONLY_MINIO_SECRET_KEY", ""))
    parser.add_argument("--minio-secure", action="store_true")
    parser.add_argument("--log-file", default=os.getenv("READONLY_CLEANUP_LOG_FILE"))
    parser.add_argument("--page-size", type=int, default=_DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-rows", type=int, default=_DEFAULT_MAX_ROWS)
    parser.add_argument("--qdrant-page-size", type=int, default=_DEFAULT_PAGE_SIZE)
    parser.add_argument("--qdrant-max-pages", type=int, default=1000)
    parser.add_argument("--qdrant-max-points", type=int, default=_DEFAULT_MAX_QDRANT_POINTS)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--json", action="store_true")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    _validate_target(args)
    if (
        args.page_size <= 0
        or args.max_rows <= 0
        or args.qdrant_page_size <= 0
        or args.qdrant_max_pages <= 0
        or args.qdrant_max_points <= 0
        or args.page_size > _MAX_PAGE_SIZE
        or args.max_rows > _MAX_MAX_ROWS
        or args.qdrant_page_size > _MAX_QDRANT_PAGE_SIZE
        or args.qdrant_max_pages > _MAX_QDRANT_MAX_PAGES
        or args.qdrant_max_points > _DEFAULT_MAX_QDRANT_POINTS
        or not math.isfinite(args.timeout_seconds)
        or args.timeout_seconds <= 0
        or args.timeout_seconds > _MAX_TIMEOUT_SECONDS
    ):
        raise DiagnosticContractError("page and timeout limits must be positive")
    if (
        args.storage_backend != "minio"
        or not args.minio_endpoint
        or not args.minio_bucket
        or not args.minio_access_key
        or not args.minio_secret_key
    ):
        raise DiagnosticContractError("read-only MinIO endpoint, bucket, and credentials are required")
    report = await _build_report(args)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return _status_exit_code(report)


def main() -> int:
    args = _parser().parse_args()
    try:
        return asyncio.run(_async_main(args))
    except DiagnosticContractError as exc:
        print(f"diagnostic_contract_error={_safe_code(exc)}", file=sys.stderr)
        return 3
    except PermissionError:
        print("diagnostic_permission_error=readonly_access_required", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - CLI must use the documented error exit
        if "permission" in type(exc).__name__.casefold() or "permission" in str(exc).casefold():
            print("diagnostic_permission_error=readonly_access_required", file=sys.stderr)
            return 3
        print(f"diagnostic_error={_safe_code(type(exc).__name__)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
