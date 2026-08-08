"""向量库对账 (PG child chunks ↔ Qdrant point references).

The audit compares identities, not only counts.  A valid vector point is
identified by ``(tenant_id, kb_id, point_id)`` and carries ``doc_id`` as an
additional consistency check.  Qdrant is scanned through a metadata-only,
paginated path: no vector or document text is loaded.

The database side is deliberately queried one tenant/KB at a time.  The
``knowledge_bases`` administrator policy can enumerate owners, while chunks
and documents remain behind their normal tenant RLS policies.  The global
Qdrant scan is only used by this administrator diagnostic path; it is not a
retrieval entry point.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from eduagents_core.logging import get_logger
from sqlalchemy import and_, func, select

from .db import tenant_session
from .models import Chunk, Document, KnowledgeBase

log = get_logger("vector_reconcile")

# The limits are intentionally explicit.  A partial global scan must be an
# error, otherwise it could be mistaken for a clean collection.
RECONCILE_SCAN_PAGE_SIZE = 1000
RECONCILE_SCAN_MAX_PAGES = 1000
RECONCILE_SCAN_MAX_POINTS = 1_000_000
RECONCILE_SCAN_TIMEOUT_SECONDS = 30.0
RECONCILE_KB_TIMEOUT_SECONDS = 30.0

# A document in one of these terminal states is expected to have at least one
# embedded child chunk.  ``partial_ready`` is included because it is still a
# searchable terminal state for successfully parsed pages.
_READY_DOCUMENT_STATUSES = ("ready", "partial_ready", "enriched")

# Kept for callers that imported the old constant.  Exact identity checks do
# not use a count tolerance.
DRIFT_TOLERANCE = 0


@dataclass(frozen=True, slots=True)
class PgChunkRef:
    """The PG identity needed to compare one done child chunk."""

    tenant_id: str
    kb_id: str
    point_id: str
    doc_id: str


ChunkRef = PgChunkRef


@dataclass
class KbReconcileRow:
    """One knowledge base, retaining the old count fields plus exact issues."""

    kb_id: str
    tenant_id: str
    name: str
    pg_done: int
    qdrant_points: int
    missing_points: list[str] = field(default_factory=list)
    extra_points: list[str] = field(default_factory=list)
    wrong_tenant_points: list[str] = field(default_factory=list)
    malformed_points: list[str] = field(default_factory=list)
    document_inconsistencies: list[dict] = field(default_factory=list)
    exact: bool = False

    @property
    def drift(self) -> int:
        """The legacy signed count difference (positive means PG is ahead)."""
        return self.pg_done - self.qdrant_points

    @property
    def healthy(self) -> bool:
        if not self.exact:
            return abs(self.drift) <= DRIFT_TOLERANCE
        return not (
            self.missing_points
            or self.extra_points
            or self.wrong_tenant_points
            or self.malformed_points
            or self.document_inconsistencies
        )

    def as_dict(self) -> dict:
        return {
            "kb_id": self.kb_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "pg_done": self.pg_done,
            "qdrant_points": self.qdrant_points,
            "drift": self.drift,
            "healthy": self.healthy,
            "missing_points": sorted(self.missing_points),
            "extra_points": sorted(self.extra_points),
            "wrong_tenant_points": sorted(self.wrong_tenant_points),
            "malformed_points": sorted(self.malformed_points),
            "document_inconsistencies": _sorted_issue_dicts(self.document_inconsistencies),
        }


@dataclass
class OrphanKbRow:
    """Qdrant points whose ``(tenant_id, kb_id)`` has no PG KB row."""

    kb_id: str
    tenant_id: str
    qdrant_points: int
    point_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kb_id": self.kb_id,
            "tenant_id": self.tenant_id,
            "qdrant_points": self.qdrant_points,
            "point_ids": sorted(self.point_ids),
        }


@dataclass
class ReconcileReport:
    rows: list[KbReconcileRow] = field(default_factory=list)
    orphan_kbs: list[OrphanKbRow] = field(default_factory=list)
    missing_points: list[str] = field(default_factory=list)
    extra_points: list[str] = field(default_factory=list)
    wrong_tenant_points: list[str] = field(default_factory=list)
    wrong_tenant_details: list[dict] = field(default_factory=list)
    malformed_points: list[dict] = field(default_factory=list)
    document_inconsistencies: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    exact: bool = False

    @property
    def missing_vectors(self) -> int:
        """Legacy aggregate: PG done chunks without their exact Qdrant point."""
        if self.exact:
            return len(self.missing_points)
        return sum(r.drift for r in self.rows if r.drift > 0)

    @property
    def orphan_points(self) -> int:
        """Legacy aggregate: valid extra points plus malformed point records."""
        if self.exact:
            return len(self.extra_points) + len(self.malformed_points)
        within_known = sum(-r.drift for r in self.rows if r.drift < 0)
        return within_known + sum(o.qdrant_points for o in self.orphan_kbs)

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if (
            self.unhealthy_rows
            or self.orphan_kbs
            or self.missing_points
            or self.extra_points
            or self.wrong_tenant_points
            or self.malformed_points
            or self.document_inconsistencies
        ):
            return "drift"
        return "ok"

    @property
    def unhealthy_rows(self) -> list[KbReconcileRow]:
        return [r for r in self.rows if not r.healthy]

    def as_dict(self, *, include_all: bool = False) -> dict:
        shown = self.rows if include_all else self.unhealthy_rows
        return {
            "status": self.status,
            "kb_total": len(self.rows),
            "kb_drifted": len(self.unhealthy_rows),
            "kb_orphaned": len(self.orphan_kbs),
            # Existing administrator/CLI fields.  Keep their names and
            # meanings stable while adding the exact fields below.
            "missing_vectors": self.missing_vectors,
            "orphan_points": self.orphan_points,
            "items": [r.as_dict() for r in shown],
            "orphans": [o.as_dict() for o in self.orphan_kbs],
            "errors": list(self.errors),
            "missing_points": sorted(self.missing_points),
            "extra_points": sorted(self.extra_points),
            "wrong_tenant_points": sorted(self.wrong_tenant_points),
            "wrong_tenant_details": _sorted_issue_dicts(self.wrong_tenant_details),
            "malformed_points": _sorted_issue_dicts(self.malformed_points),
            "document_inconsistencies": _sorted_issue_dicts(self.document_inconsistencies),
        }


def _sorted_issue_dicts(items: list[dict]) -> list[dict]:
    """Return deterministic, shallow-copied issue records for API output."""
    return sorted(
        (dict(item) for item in items),
        key=lambda item: tuple(
            str(item.get(name, ""))
            for name in ("tenant_id", "kb_id", "doc_id", "point_id", "reason")
        ),
    )


async def _list_knowledge_bases(admin_tenant_id: uuid.UUID | str) -> list[tuple]:
    """Enumerate all KB owners without widening chunk/document RLS."""
    async with tenant_session(admin_tenant_id, is_admin=True) as session:
        rows = await session.execute(
            select(KnowledgeBase.id, KnowledgeBase.tenant_id, KnowledgeBase.name).order_by(
                KnowledgeBase.id
            )
        )
        return list(rows.all())


async def _pg_done_chunks(tenant_id: uuid.UUID | str, kb_id: uuid.UUID | str) -> list[PgChunkRef]:
    """Read exact done child identities inside the owning tenant session."""
    async with tenant_session(tenant_id) as session:
        rows = await session.execute(
            select(Chunk.id, Chunk.tenant_id, Chunk.doc_id)
            .join(Document, Document.id == Chunk.doc_id)
            .where(
                Document.kb_id == kb_id,
                Document.tenant_id == tenant_id,
                Chunk.tenant_id == tenant_id,
                Chunk.embedding_status == "done",
                Chunk.parent_id.isnot(None),
            )
            .order_by(Chunk.id)
        )
        return [
            PgChunkRef(
                tenant_id=str(row_tenant),
                kb_id=str(kb_id),
                point_id=str(chunk_id),
                doc_id=str(doc_id),
            )
            for chunk_id, row_tenant, doc_id in rows.all()
        ]


async def _pg_done_count(tenant_id: uuid.UUID, kb_id: uuid.UUID) -> int:
    """Compatibility count helper retained for older callers and test fakes."""
    return len(await _pg_done_chunks(tenant_id, kb_id))


async def _pg_document_inconsistencies(
    tenant_id: uuid.UUID | str, kb_id: uuid.UUID | str
) -> list[dict]:
    """Find terminal documents that have no embedded child chunks."""
    done_child = and_(
        Chunk.doc_id == Document.id,
        Chunk.tenant_id == tenant_id,
        Chunk.parent_id.isnot(None),
        Chunk.embedding_status == "done",
    )
    async with tenant_session(tenant_id) as session:
        rows = await session.execute(
            select(Document.id, Document.status, func.count(Chunk.id))
            .outerjoin(Chunk, done_child)
            .where(
                Document.kb_id == kb_id,
                Document.tenant_id == tenant_id,
                Document.status.in_(_READY_DOCUMENT_STATUSES),
            )
            .group_by(Document.id, Document.status)
            .having(func.count(Chunk.id) == 0)
            .order_by(Document.id)
        )
        return [
            {
                "tenant_id": str(tenant_id),
                "kb_id": str(kb_id),
                "doc_id": str(doc_id),
                "status": status,
                "reason": "terminal_document_without_done_child_chunks",
                "done_child_chunks": int(done_count),
            }
            for doc_id, status, done_count in rows.all()
        ]


async def _await_with_timeout(awaitable, timeout_seconds: float | None):
    if timeout_seconds is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)


def _supported_kwargs(callable_obj, requested: dict[str, Any]) -> dict[str, Any]:
    """Allow small legacy fakes to implement scan_point_refs() without kwargs."""
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return requested
    if any(parameter.kind == parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return requested
    return {key: value for key, value in requested.items() if key in signature.parameters}


async def _scan_point_refs(
    vector_store,
    *,
    timeout_seconds: float | None,
    page_size: int,
    max_pages: int,
    max_points: int | None,
) -> list:
    scanner = getattr(vector_store, "scan_point_refs", None)
    if not callable(scanner):
        raise AttributeError("vector store does not implement scan_point_refs")
    kwargs = _supported_kwargs(
        scanner,
        {
            "page_size": page_size,
            "max_pages": max_pages,
            "timeout": max(1, int(timeout_seconds)) if timeout_seconds is not None else None,
            "max_points": max_points,
        },
    )
    return await _await_with_timeout(scanner(**kwargs), timeout_seconds)


def _ref_value(ref: object, name: str) -> object | None:
    if isinstance(ref, Mapping):
        value = ref.get(name)
        if value is None and name == "point_id":
            value = ref.get("id")
        return value
    value = getattr(ref, name, None)
    if value is None and name == "point_id":
        value = getattr(ref, "id", None)
    return value


def _safe_identifier(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


def _safe_display(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)[:128]


def _normalise_point_ref(ref: object) -> tuple[tuple[str, str, str, str] | None, dict | None]:
    values = {
        name: _ref_value(ref, name) for name in ("point_id", "tenant_id", "kb_id", "doc_id")
    }
    for name, value in values.items():
        if _safe_identifier(value) is None:
            reason = "missing_" + name if value is None else "invalid_" + name
            return None, {
                "point_id": _safe_display(values["point_id"]),
                "tenant_id": _safe_display(values["tenant_id"]),
                "kb_id": _safe_display(values["kb_id"]),
                "doc_id": _safe_display(values["doc_id"]),
                "field": name,
                "reason": reason,
            }
    return (
        (
            _safe_identifier(values["tenant_id"]),
            _safe_identifier(values["kb_id"]),
            _safe_identifier(values["point_id"]),
            _safe_identifier(values["doc_id"]),
        ),
        None,
    )


def _normalise_pg_ref(ref: object, tenant_id: str, kb_id: str) -> PgChunkRef:
    if isinstance(ref, PgChunkRef):
        values = (ref.tenant_id, ref.kb_id, ref.point_id, ref.doc_id)
    elif isinstance(ref, Mapping):
        values = (
            ref.get("tenant_id", tenant_id),
            ref.get("kb_id", kb_id),
            ref.get("point_id", ref.get("chunk_id", ref.get("id"))),
            ref.get("doc_id"),
        )
    elif isinstance(ref, (tuple, list)) and len(ref) == 4:
        values = tuple(ref)
    else:
        raise ValueError("invalid PG chunk reference shape")
    normalised = [_safe_identifier(value) for value in values]
    if any(value is None for value in normalised):
        raise ValueError("invalid PG chunk reference identifier")
    return PgChunkRef(*normalised)  # type: ignore[arg-type]


def _append_error(report: ReconcileReport, kb_id: str, operation: str, exc: Exception) -> None:
    error_text = f"{type(exc).__name__}: {exc}"
    if operation.startswith("scan_") or operation == "list_knowledge_bases":
        error_text = f"{operation}: {error_text}"
    report.errors.append(
        {
            "kb_id": kb_id,
            "error": error_text,
        }
    )


async def _reconcile_exact(
    vector_store,
    kb_rows: list[tuple],
    report: ReconcileReport,
    *,
    wanted: str | None,
    kb_timeout_seconds: float | None,
    scan_timeout_seconds: float | None,
    scan_page_size: int,
    scan_max_pages: int,
    scan_max_points: int | None,
) -> ReconcileReport:
    report.exact = True
    owner_by_kb = {str(row_id): str(row_tenant) for row_id, row_tenant, _ in kb_rows}
    selected_rows = [row for row in kb_rows if wanted is None or str(row[0]) == wanted]

    expected_by_key: dict[tuple[str, str, str], PgChunkRef] = {}
    document_issues: list[dict] = []
    successful_keys: set[tuple[str, str]] = set()
    failed_keys: set[tuple[str, str]] = set()

    for row_id, row_tenant, _row_name in selected_rows:
        kb_text = str(row_id)
        tenant_text = str(row_tenant)
        pair = (kb_text, tenant_text)
        try:
            pg_refs = await _await_with_timeout(
                _pg_done_chunks(row_tenant, row_id), kb_timeout_seconds
            )
            normalised_refs = [
                _normalise_pg_ref(ref, tenant_text, kb_text) for ref in pg_refs
            ]
            local_expected: dict[tuple[str, str, str], PgChunkRef] = {}
            for ref in normalised_refs:
                key = (ref.tenant_id, ref.kb_id, ref.point_id)
                if key in local_expected or key in expected_by_key:
                    raise ValueError(f"duplicate PG chunk point_id: {ref.point_id}")
            pg_doc_issues = await _await_with_timeout(
                _pg_document_inconsistencies(row_tenant, row_id), kb_timeout_seconds
            )
            for ref in normalised_refs:
                local_expected[(ref.tenant_id, ref.kb_id, ref.point_id)] = ref
            expected_by_key.update(local_expected)
            document_issues.extend(pg_doc_issues)
            successful_keys.add(pair)
        except Exception as exc:  # noqa: BLE001 - an audit must continue to other KBs
            failed_keys.add(pair)
            _append_error(report, kb_text, "pg", exc)

    try:
        raw_refs = await _scan_point_refs(
            vector_store,
            timeout_seconds=scan_timeout_seconds,
            page_size=scan_page_size,
            max_pages=scan_max_pages,
            max_points=scan_max_points,
        )
    except Exception as exc:  # noqa: BLE001 - status=error, never partial-ok
        _append_error(report, "*", "scan_point_refs", exc)
        raw_refs = []
        scan_succeeded = False
    else:
        scan_succeeded = True

    actual_by_key: dict[tuple[str, str], list[tuple[str, str, str, str]]] = {}
    actual_keys: set[tuple[str, str, str]] = set()
    valid_refs: list[tuple[str, str, str, str]] = []
    for raw_ref in raw_refs:
        normalised, malformed = _normalise_point_ref(raw_ref)
        if malformed is not None:
            report.malformed_points.append(malformed)
            continue
        assert normalised is not None
        tenant_text, kb_text, point_text, doc_text = normalised
        if wanted is not None and kb_text != wanted:
            continue
        pair = (kb_text, tenant_text)
        # If PG failed for this KB, don't manufacture missing/extra results
        # from an incomplete side of the comparison.
        if pair in failed_keys or (
            kb_text in owner_by_kb and (kb_text, owner_by_kb[kb_text]) in failed_keys
        ):
            continue
        key = (tenant_text, kb_text, point_text)
        if key in actual_keys:
            report.malformed_points.append(
                {
                    "point_id": point_text,
                    "tenant_id": tenant_text,
                    "kb_id": kb_text,
                    "doc_id": doc_text,
                    "reason": "duplicate_point_reference",
                }
            )
            continue
        actual_keys.add(key)
        valid_refs.append(normalised)
        actual_by_key.setdefault(pair, []).append(normalised)

    if scan_succeeded:
        for key, expected in expected_by_key.items():
            if key not in actual_keys:
                report.missing_points.append(expected.point_id)

    orphan_groups: dict[tuple[str, str], list[str]] = {}
    for tenant_text, kb_text, point_text, doc_text in valid_refs:
        key = (tenant_text, kb_text, point_text)
        expected = expected_by_key.get(key)
        if expected is not None:
            if expected.doc_id != doc_text:
                document_issues.append(
                    {
                        "tenant_id": tenant_text,
                        "kb_id": kb_text,
                        "doc_id": doc_text,
                        "expected_doc_id": expected.doc_id,
                        "point_id": point_text,
                        "reason": "point_document_mismatch",
                    }
                )
            continue

        report.extra_points.append(point_text)
        if kb_text not in owner_by_kb:
            orphan_groups.setdefault((tenant_text, kb_text), []).append(point_text)
            continue
        expected_tenant = owner_by_kb[kb_text]
        if tenant_text != expected_tenant:
            report.wrong_tenant_points.append(point_text)
            report.wrong_tenant_details.append(
                {
                    "point_id": point_text,
                    "tenant_id": tenant_text,
                    "expected_tenant_id": expected_tenant,
                    "kb_id": kb_text,
                    "doc_id": doc_text,
                    "reason": "point_tenant_mismatch",
                }
            )

    report.missing_points = sorted(report.missing_points)
    report.extra_points = sorted(report.extra_points)
    report.wrong_tenant_points = sorted(report.wrong_tenant_points)
    report.malformed_points = _sorted_issue_dicts(report.malformed_points)
    report.wrong_tenant_details = _sorted_issue_dicts(report.wrong_tenant_details)
    report.document_inconsistencies = _sorted_issue_dicts(document_issues)

    report.orphan_kbs = [
        OrphanKbRow(
            tenant_id=tenant_text,
            kb_id=kb_text,
            qdrant_points=len(point_ids),
            point_ids=sorted(point_ids),
        )
        for (tenant_text, kb_text), point_ids in sorted(orphan_groups.items())
    ]

    issues_by_pair: dict[tuple[str, str], list[dict]] = {}
    for issue in report.document_inconsistencies:
        pair = (str(issue.get("kb_id", "")), str(issue.get("tenant_id", "")))
        issues_by_pair.setdefault(pair, []).append(issue)

    rows: list[KbReconcileRow] = []
    for row_id, row_tenant, row_name in selected_rows:
        kb_text = str(row_id)
        tenant_text = str(row_tenant)
        pair = (kb_text, tenant_text)
        if pair not in successful_keys:
            continue
        expected_refs = [
            ref for key, ref in expected_by_key.items() if key[0] == tenant_text and key[1] == kb_text
        ]
        actual_refs = actual_by_key.get(pair, [])
        row = KbReconcileRow(
            kb_id=kb_text,
            tenant_id=tenant_text,
            name=row_name,
            pg_done=len(expected_refs),
            qdrant_points=len(actual_refs),
            exact=True,
            document_inconsistencies=list(issues_by_pair.get(pair, [])),
        )
        row.missing_points = (
            [
                ref.point_id
                for ref in expected_refs
                if (ref.tenant_id, ref.kb_id, ref.point_id) not in actual_keys
            ]
            if scan_succeeded
            else []
        )
        row.extra_points = sorted(
            point_text
            for tenant_text_actual, kb_text_actual, point_text, _doc_text in actual_refs
            if (tenant_text_actual, kb_text_actual, point_text) not in expected_by_key
        )
        row.wrong_tenant_points = sorted(
            detail["point_id"]
            for detail in report.wrong_tenant_details
            if detail["kb_id"] == kb_text and detail["expected_tenant_id"] == tenant_text
        )
        row.malformed_points = sorted(
            str(issue["point_id"])
            for issue in report.malformed_points
            if issue.get("tenant_id") == tenant_text and issue.get("kb_id") == kb_text
        )
        rows.append(row)
    report.rows = sorted(rows, key=lambda row: (row.tenant_id, row.kb_id))
    report.errors.sort(key=lambda error: (str(error.get("kb_id", "")), str(error.get("operation", ""))))
    return report


async def _reconcile_legacy(
    vector_store,
    kb_rows: list[tuple],
    report: ReconcileReport,
    *,
    wanted: str | None,
) -> ReconcileReport:
    """Keep old count-only fakes/callers usable during the API transition."""
    known_pairs = {(str(row_id), str(row_tenant)) for row_id, row_tenant, _ in kb_rows}
    for row_id, row_tenant, row_name in kb_rows:
        if wanted and str(row_id) != wanted:
            continue
        try:
            pg_done = await _pg_done_count(row_tenant, row_id)
            points = await vector_store.count_by_kb(str(row_tenant), str(row_id))
        except Exception as exc:  # noqa: BLE001 - preserve per-KB continuation
            _append_error(report, str(row_id), "count", exc)
            continue
        report.rows.append(
            KbReconcileRow(
                kb_id=str(row_id),
                tenant_id=str(row_tenant),
                name=row_name,
                pg_done=pg_done,
                qdrant_points=points,
            )
        )

    if wanted is None:
        try:
            for (tenant_text, kb_text), count in (await vector_store.scan_kb_ids()).items():
                if (kb_text, tenant_text) not in known_pairs:
                    report.orphan_kbs.append(
                        OrphanKbRow(
                            kb_id=kb_text,
                            tenant_id=tenant_text,
                            qdrant_points=count,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            _append_error(report, "*", "scan_kb_ids", exc)
    report.rows.sort(key=lambda row: (row.tenant_id, row.kb_id))
    report.orphan_kbs.sort(key=lambda row: (row.tenant_id, row.kb_id))
    return report


async def reconcile_vectors(
    vector_store,
    admin_tenant_id: uuid.UUID | str,
    kb_id: uuid.UUID | str | None = None,
    *,
    kb_timeout_seconds: float | None = RECONCILE_KB_TIMEOUT_SECONDS,
    scan_timeout_seconds: float | None = RECONCILE_SCAN_TIMEOUT_SECONDS,
    scan_page_size: int = RECONCILE_SCAN_PAGE_SIZE,
    scan_max_pages: int = RECONCILE_SCAN_MAX_PAGES,
    scan_max_points: int | None = RECONCILE_SCAN_MAX_POINTS,
) -> ReconcileReport:
    """Run a read-only exact reconciliation and return a three-state report.

    Each KB's PG work is isolated, so a timeout or malformed row in one KB
    does not discard successful rows from the others.  Qdrant scanning is a
    bounded all-reference operation; a timeout or page cap is reported as an
    ``error`` rather than silently accepting a partial scan.
    """
    report = ReconcileReport()
    wanted = str(kb_id) if kb_id else None
    try:
        kb_rows = await _list_knowledge_bases(admin_tenant_id)
    except Exception as exc:  # noqa: BLE001
        _append_error(report, "*", "list_knowledge_bases", exc)
        return report

    if callable(getattr(vector_store, "scan_point_refs", None)):
        report = await _reconcile_exact(
            vector_store,
            kb_rows,
            report,
            wanted=wanted,
            kb_timeout_seconds=kb_timeout_seconds,
            scan_timeout_seconds=scan_timeout_seconds,
            scan_page_size=scan_page_size,
            scan_max_pages=scan_max_pages,
            scan_max_points=scan_max_points,
        )
    else:
        report = await _reconcile_legacy(vector_store, kb_rows, report, wanted=wanted)

    if report.status != "ok":
        log.warning(
            "vector_reconcile_drift",
            kb_total=len(report.rows),
            kb_drifted=len(report.unhealthy_rows),
            kb_orphaned=len(report.orphan_kbs),
            missing_vectors=report.missing_vectors,
            orphan_points=report.orphan_points,
            missing_points=len(report.missing_points),
            extra_points=len(report.extra_points),
            wrong_tenant_points=len(report.wrong_tenant_points),
            malformed_points=len(report.malformed_points),
            document_inconsistencies=len(report.document_inconsistencies),
            errors=len(report.errors),
        )
    return report
