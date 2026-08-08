#!/usr/bin/env python3
"""Plan or explicitly execute an idempotent repair of A0-confirmed missing points.

The default is a read-only dry-run.  A plan is built only from the exact
``missing_points`` result of ``reconcile_vectors`` and then re-reads the
corresponding child chunks by ID.  It never reparses source objects, changes
PostgreSQL chunk state, or deletes a point.  The ``--execute`` path is
reserved for a separately authorized maintenance window and requires a
bounded batch plus a known, explicitly covered cost.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from eduagents_api.vector_reconcile import reconcile_vectors
from eduagents_core.config import get_settings
from eduagents_core.vectorstore.base import VectorPoint
from eduagents_core.vectorstore.qdrant_store import QdrantStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_DEFAULT_MAX_POINTS = 100
_DEFAULT_BATCH_SIZE = 32
_UNKNOWN_COST_SAFE_POINTS = 100
_MAX_REPAIR_POINTS = 100
_MAX_BATCH_SIZE = 32
_MAX_CONCURRENCY = 8
_MAX_QDRANT_PAGE_SIZE = 1000
_MAX_QDRANT_MAX_PAGES = 1000
_MAX_QDRANT_MAX_POINTS = 1_000_000
_DB_ID_BATCH_SIZE = 500
_RETRIEVAL_SMOKE_LIMIT = 10
_MAX_INPUT_CHARACTERS = 1_000_000


class RepairContractError(RuntimeError):
    """The repair plan is not sufficiently bounded or authorized."""


@dataclass(frozen=True, slots=True)
class RepairCandidate:
    chunk_id: str
    tenant_id: str
    kb_id: str
    doc_id: str
    text: str
    heading_path: str
    context_prefix: str | None
    parent_id: str
    page_start: int | None
    page_end: int | None
    kc_ids: list[Any]

    @property
    def embedding_text(self) -> str:
        pieces = [part for part in (self.context_prefix, self.heading_path, self.text) if part]
        return "\n".join(pieces)


@dataclass(frozen=True, slots=True)
class RepairPlan:
    candidates: tuple[RepairCandidate, ...]
    missing_point_count: int
    selected_point_count: int
    skipped_missing_text: int
    truncated: bool
    total_chars: int
    api_batches: int
    estimated_tokens: int
    estimated_cost_cny: float | None
    cost_rate_known: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "dry_run",
            "missing_points_confirmed_by_a0": self.missing_point_count,
            # This count is deliberately taken after --kb/--doc scoping and
            # before --max-points truncation.  Keep the explicit name so an
            # operator cannot confuse a global A0 count with this plan scope.
            "missing_points_in_scope": self.missing_point_count,
            "selected_points": self.selected_point_count,
            "skipped_missing_text": self.skipped_missing_text,
            "truncated_by_max_points": self.truncated,
            "total_input_characters": self.total_chars,
            "estimated_tokens": self.estimated_tokens,
            "api_batches": self.api_batches,
            "estimated_cost_cny": self.estimated_cost_cny,
            "cost_rate_known": self.cost_rate_known,
            "candidate_ids": [hash_id(item.chunk_id) for item in self.candidates],
        }


def hash_id(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def embedding_text(candidate: RepairCandidate) -> str:
    return candidate.embedding_text


def _normalise_uuid(value: object, *, error_code: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RepairContractError(error_code) from exc


def _normalise_missing_ids(missing_ids: list[str]) -> list[str]:
    """Validate and de-duplicate A0 IDs without exposing them in errors/output."""
    normalised: list[str] = []
    seen: set[str] = set()
    for value in missing_ids:
        item = _normalise_uuid(value, error_code="A0_MISSING_ID_INVALID")
        if item not in seen:
            seen.add(item)
            normalised.append(item)
    return normalised


def _missing_scope_by_id(report, missing_ids: list[str]) -> dict[str, tuple[str, str]]:
    """Recover the A0 tenant/KB scope for every missing point ID."""
    scope: dict[str, tuple[str, str]] = {}
    for row in getattr(report, "rows", []):
        tenant_id = _normalise_uuid(
            getattr(row, "tenant_id", None), error_code="A0_SCOPE_TENANT_INVALID"
        )
        kb_id = _normalise_uuid(
            getattr(row, "kb_id", None), error_code="A0_SCOPE_KB_INVALID"
        )
        for raw_id in getattr(row, "missing_points", []):
            point_id = _normalise_uuid(raw_id, error_code="A0_SCOPE_POINT_INVALID")
            previous = scope.setdefault(point_id, (tenant_id, kb_id))
            if previous != (tenant_id, kb_id):
                raise RepairContractError("A0_SCOPE_AMBIGUOUS")
    expected = set(missing_ids)
    if set(scope) != expected:
        raise RepairContractError("A0_SCOPE_INCOMPLETE")
    return scope


def _scope_groups(
    chunk_ids: list[str],
    scope_by_id: dict[str, tuple[str, str]],
    *,
    kb_id: str | None,
) -> dict[tuple[str, str], list[str]]:
    groups: dict[tuple[str, str], list[str]] = {}
    normalised_kb = (
        _normalise_uuid(kb_id, error_code="invalid kb UUID") if kb_id else None
    )
    for chunk_id in chunk_ids:
        scope = scope_by_id.get(chunk_id)
        if scope is None:
            raise RepairContractError("A0_SCOPE_INCOMPLETE")
        if normalised_kb and scope[1] != normalised_kb:
            raise RepairContractError("A0_SCOPE_KB_MISMATCH")
        groups.setdefault(scope, []).append(chunk_id)
    return groups


def select_missing_ids(
    missing_ids: list[str],
    *,
    matching_ids: set[str] | None,
    max_points: int,
) -> tuple[list[str], int, bool]:
    """Filter the A0 set before applying the repair batch cap.

    ``matching_ids`` is populated by the read-only PostgreSQL scope query when
    ``--doc`` is supplied.  The order from A0 is retained so a dry-run and a
    later explicitly authorized run choose the same bounded prefix.
    """
    if max_points <= 0 or max_points > _MAX_REPAIR_POINTS:
        raise RepairContractError("max-points out of bounds")
    normalised = _normalise_missing_ids(missing_ids)
    normalised_matching = (
        None
        if matching_ids is None
        else {
            _normalise_uuid(value, error_code="A0_SCOPE_ID_INVALID")
            for value in matching_ids
        }
    )
    scoped = [
        item for item in normalised if normalised_matching is None or item in normalised_matching
    ]
    selected = scoped[:max_points]
    return selected, len(scoped), len(scoped) > len(selected)


def _validate_candidate(candidate: RepairCandidate) -> None:
    for name in ("chunk_id", "tenant_id", "kb_id", "doc_id", "parent_id"):
        _normalise_uuid(getattr(candidate, name), error_code="CANDIDATE_IDENTIFIER_INVALID")
    if candidate.chunk_id == candidate.parent_id:
        raise RepairContractError("CANDIDATE_PARENT_SELF_REFERENCE")
    if not candidate.embedding_text.strip():
        raise RepairContractError("CANDIDATE_TEXT_EMPTY")


def _validate_plan(plan: RepairPlan) -> None:
    if plan.selected_point_count != len(plan.candidates):
        raise RepairContractError("PLAN_COUNT_MISMATCH")
    if plan.selected_point_count > _MAX_REPAIR_POINTS:
        raise RepairContractError("PLAN_POINT_LIMIT_EXCEEDED")
    if plan.missing_point_count < 0 or plan.skipped_missing_text < 0:
        raise RepairContractError("PLAN_COUNT_INVALID")
    for candidate in plan.candidates:
        _validate_candidate(candidate)


def estimate_plan(
    candidates: list[RepairCandidate],
    *,
    batch_size: int,
    price_per_million_tokens: float | None,
    missing_point_count: int,
    skipped_missing_text: int,
    truncated: bool,
) -> RepairPlan:
    if batch_size <= 0 or batch_size > _MAX_BATCH_SIZE:
        raise RepairContractError("batch-size out of bounds")
    if missing_point_count < 0 or skipped_missing_text < 0:
        raise RepairContractError("plan counts must be non-negative")
    if price_per_million_tokens is not None and (
        not math.isfinite(price_per_million_tokens) or price_per_million_tokens < 0
    ):
        raise RepairContractError("embedding price must be finite and non-negative")
    total_chars = sum(len(embedding_text(candidate)) for candidate in candidates)
    if total_chars > _MAX_INPUT_CHARACTERS:
        raise RepairContractError("INPUT_CHARACTER_LIMIT_EXCEEDED")
    estimated_tokens = math.ceil(total_chars / 4) if total_chars else 0
    estimated_cost = (
        estimated_tokens / 1_000_000 * price_per_million_tokens
        if price_per_million_tokens is not None
        else None
    )
    if price_per_million_tokens is None and len(candidates) > _UNKNOWN_COST_SAFE_POINTS:
        raise RepairContractError("UNKNOWN_COST_LARGE_BATCH")
    if len(candidates) > _MAX_REPAIR_POINTS:
        raise RepairContractError("PLAN_POINT_LIMIT_EXCEEDED")
    plan = RepairPlan(
        candidates=tuple(candidates),
        missing_point_count=missing_point_count,
        selected_point_count=len(candidates),
        skipped_missing_text=skipped_missing_text,
        truncated=truncated,
        total_chars=total_chars,
        api_batches=math.ceil(len(candidates) / batch_size) if candidates else 0,
        estimated_tokens=estimated_tokens,
        estimated_cost_cny=estimated_cost,
        cost_rate_known=price_per_million_tokens is not None,
    )
    _validate_plan(plan)
    return plan


def validate_execute_budget(plan: RepairPlan, max_cost_cny: float | None) -> None:
    _validate_plan(plan)
    if max_cost_cny is not None and (
        not math.isfinite(max_cost_cny) or max_cost_cny < 0
    ):
        raise RepairContractError("max-cost-cny must be finite and non-negative")
    if plan.selected_point_count == 0:
        return
    if not plan.cost_rate_known:
        # A caller-provided ceiling cannot prove what a provider will charge.
        # Execute is therefore forbidden until the embedding rate is known;
        # the ceiling remains an additional guard for a known rate.
        raise RepairContractError("EXECUTE_REQUIRES_KNOWN_COST")
    assert plan.estimated_cost_cny is not None
    if max_cost_cny is None or plan.estimated_cost_cny > max_cost_cny:
        raise RepairContractError("COST_LIMIT_EXCEEDED")


async def _read_candidates(
    database_url: str,
    chunk_ids: list[str],
    *,
    kb_id: str | None,
    doc_id: str | None,
    scope_by_id: dict[str, tuple[str, str]],
) -> tuple[list[RepairCandidate], int]:
    if not chunk_ids:
        return [], 0
    chunk_ids = _normalise_missing_ids(chunk_ids)
    if doc_id:
        doc_id = _normalise_uuid(doc_id, error_code="invalid doc UUID")
    groups = _scope_groups(chunk_ids, scope_by_id, kb_id=kb_id)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    found_by_id: dict[str, RepairCandidate] = {}
    try:
        async with engine.connect() as conn:
            for (tenant_id, scoped_kb_id), group_ids in groups.items():
                async with conn.begin():
                    # RLS context is transaction-local and the transaction is
                    # explicitly read-only.  This is the same isolation
                    # boundary used by apps.api.db.tenant_session.
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                    await conn.execute(
                        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                        {"tenant_id": tenant_id},
                    )
                    for start in range(0, len(group_ids), _DB_ID_BATCH_SIZE):
                        batch = group_ids[start : start + _DB_ID_BATCH_SIZE]
                        placeholders = ", ".join(
                            f":id_{index}" for index in range(len(batch))
                        )
                        params: dict[str, Any] = {
                            f"id_{index}": uuid.UUID(chunk_id)
                            for index, chunk_id in enumerate(batch)
                        }
                        params.update(
                            {
                                "scope_tenant": uuid.UUID(tenant_id),
                                "scope_kb": uuid.UUID(scoped_kb_id),
                            }
                        )
                        filters = [
                            f"c.id IN ({placeholders})",
                            "c.parent_id IS NOT NULL",
                            "c.embedding_status = 'done'",
                            "c.tenant_id = :scope_tenant",
                            "c.tenant_id = d.tenant_id",
                            "d.kb_id = :scope_kb",
                            "d.tenant_id = k.tenant_id",
                        ]
                        if doc_id:
                            filters.append("c.doc_id = :doc_id")
                            params["doc_id"] = uuid.UUID(doc_id)
                        result = await conn.execute(
                            text(
                                "SELECT c.id, c.tenant_id, d.kb_id, c.doc_id, c.text, "
                                "c.heading_path, c.context_prefix, c.parent_id, c.page_start, "
                                "c.page_end, c.kc_ids "
                                "FROM chunks c "
                                "JOIN documents d ON d.id = c.doc_id "
                                "JOIN knowledge_bases k ON k.id = d.kb_id "
                                f"WHERE {' AND '.join(filters)}"
                            ),
                            params,
                        )
                        for row in result.mappings().all():
                            chunk_text = str(row["text"] or "")
                            if not chunk_text.strip():
                                continue
                            raw_kc_ids = row["kc_ids"]
                            if raw_kc_ids is not None and not isinstance(raw_kc_ids, list):
                                raise RepairContractError("CANDIDATE_KC_IDS_INVALID")
                            candidate = RepairCandidate(
                                chunk_id=str(row["id"]),
                                tenant_id=str(row["tenant_id"]),
                                kb_id=str(row["kb_id"]),
                                doc_id=str(row["doc_id"]),
                                text=chunk_text,
                                heading_path=str(row["heading_path"] or ""),
                                context_prefix=(
                                    str(row["context_prefix"])
                                    if row["context_prefix"]
                                    else None
                                ),
                                parent_id=str(row["parent_id"]),
                                page_start=row["page_start"],
                                page_end=row["page_end"],
                                kc_ids=list(raw_kc_ids or []),
                            )
                            _validate_candidate(candidate)
                            if (candidate.tenant_id, candidate.kb_id) != (
                                tenant_id,
                                scoped_kb_id,
                            ):
                                raise RepairContractError("CANDIDATE_SCOPE_MISMATCH")
                            found_by_id[candidate.chunk_id] = candidate
            found = [found_by_id[item] for item in chunk_ids if item in found_by_id]
            return found, len(chunk_ids) - len(found)
    finally:
        await engine.dispose()


async def _read_matching_chunk_ids(
    database_url: str,
    chunk_ids: list[str],
    *,
    kb_id: str | None,
    doc_id: str,
    scope_by_id: dict[str, tuple[str, str]],
) -> set[str]:
    """Read only IDs to scope ``--doc`` before applying ``--max-points``.

    The query deliberately does not select chunk text.  This keeps a broad A0
    result bounded while still finding a target document whose missing IDs are
    not at the front of the global sorted list.
    """
    if not chunk_ids:
        return set()
    normalised_ids = _normalise_missing_ids(chunk_ids)
    doc_id = _normalise_uuid(doc_id, error_code="invalid doc UUID")
    groups = _scope_groups(normalised_ids, scope_by_id, kb_id=kb_id)
    engine = create_async_engine(database_url, pool_pre_ping=True)
    matched: set[str] = set()
    try:
        async with engine.connect() as conn:
            for (tenant_id, scoped_kb_id), group_ids in groups.items():
                async with conn.begin():
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                    await conn.execute(
                        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                        {"tenant_id": tenant_id},
                    )
                    for start in range(0, len(group_ids), _DB_ID_BATCH_SIZE):
                        batch = group_ids[start : start + _DB_ID_BATCH_SIZE]
                        placeholders = ", ".join(
                            f":id_{index}" for index in range(len(batch))
                        )
                        params: dict[str, Any] = {
                            f"id_{index}": uuid.UUID(chunk_id)
                            for index, chunk_id in enumerate(batch)
                        }
                        params.update(
                            {
                                "scope_tenant": uuid.UUID(tenant_id),
                                "scope_kb": uuid.UUID(scoped_kb_id),
                                "doc_id": uuid.UUID(doc_id),
                            }
                        )
                        result = await conn.execute(
                            text(
                                "SELECT c.id "
                                "FROM chunks c "
                                "JOIN documents d ON d.id = c.doc_id "
                                "JOIN knowledge_bases k ON k.id = d.kb_id "
                                f"WHERE {f'c.id IN ({placeholders})'} "
                                "AND c.parent_id IS NOT NULL "
                                "AND c.embedding_status = 'done' "
                                "AND c.tenant_id = :scope_tenant "
                                "AND c.tenant_id = d.tenant_id "
                                "AND d.kb_id = :scope_kb "
                                "AND d.tenant_id = k.tenant_id "
                                "AND c.doc_id = :doc_id"
                            ),
                            params,
                        )
                        matched.update(str(row[0]) for row in result.all())
    finally:
        await engine.dispose()
    return matched


def _point(candidate: RepairCandidate, dense: list[float], sparse: dict[int, float]) -> VectorPoint:
    return VectorPoint(
        id=candidate.chunk_id,
        dense=dense,
        sparse=sparse,
        payload={
            "tenant_id": candidate.tenant_id,
            "kb_id": candidate.kb_id,
            "doc_id": candidate.doc_id,
            "text": candidate.text,
            "parent_id": candidate.parent_id,
            "heading_path": candidate.heading_path,
            "page_start": candidate.page_start,
            "page_end": candidate.page_end,
            "is_table": False,
            "context_prefix": candidate.context_prefix,
            "kc_ids": candidate.kc_ids,
        },
    )


async def execute_plan(
    plan: RepairPlan,
    settings,
    *,
    batch_size: int,
    concurrency: int,
    max_cost_cny: float | None = None,
) -> int:
    """Explicit maintenance path; never called by the default CLI mode.

    The smoke check uses the vectors produced for the just-written points and
    Qdrant's tenant-filtered read API.  It therefore does not invoke another
    embedding or LLM request, and it cannot accidentally query another tenant.
    """
    _validate_plan(plan)
    if batch_size <= 0 or batch_size > _MAX_BATCH_SIZE:
        raise RepairContractError("batch-size out of bounds")
    if concurrency <= 0 or concurrency > _MAX_CONCURRENCY:
        raise RepairContractError("concurrency out of bounds")
    validate_execute_budget(plan, max_cost_cny)
    if not plan.candidates:
        return 0
    from eduagents_core.embedding.providers import create_embedder
    from eduagents_core.embedding.sparse import Bm25SparseEncoder

    embedder = create_embedder(settings)
    sparse_encoder = Bm25SparseEncoder()
    store = QdrantStore(settings)
    semaphore = asyncio.Semaphore(concurrency)
    dense_by_id: dict[str, list[float]] = {}
    sparse_by_id: dict[str, dict[int, float]] = {}

    async def write_batch(batch: tuple[RepairCandidate, ...]) -> None:
        async with semaphore:
            texts = [embedding_text(candidate) for candidate in batch]
            dense = await embedder.embed(texts)
            if len(dense) != len(batch):
                raise RepairContractError("EMBEDDING_RESULT_COUNT_MISMATCH")
            points: list[VectorPoint] = []
            for index, candidate in enumerate(batch):
                dense_vector = [float(value) for value in dense[index]]
                sparse_vector = sparse_encoder.encode_document(texts[index])
                dense_by_id[candidate.chunk_id] = dense_vector
                sparse_by_id[candidate.chunk_id] = sparse_vector
                points.append(_point(candidate, dense_vector, sparse_vector))
            await store.upsert(points)

    try:
        batches = [
            tuple(plan.candidates[start : start + batch_size])
            for start in range(0, len(plan.candidates), batch_size)
        ]
        tasks = [asyncio.create_task(write_batch(batch)) for batch in batches]
        try:
            await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        await _run_retrieval_smoke(store, plan.candidates, dense_by_id, sparse_by_id)
    finally:
        store.close()
    return len(plan.candidates)


async def _run_retrieval_smoke(
    store,
    candidates: tuple[RepairCandidate, ...],
    dense_by_id: dict[str, list[float]],
    sparse_by_id: dict[str, dict[int, float]],
) -> int:
    """Confirm one freshly repaired point per tenant/KB through read filters."""
    samples: dict[tuple[str, str], RepairCandidate] = {}
    for candidate in candidates:
        samples.setdefault((candidate.tenant_id, candidate.kb_id), candidate)
    for (tenant_id, kb_id), candidate in samples.items():
        dense_hits = await store.search_dense(
            tenant_id,
            kb_id,
            dense_by_id[candidate.chunk_id],
            _RETRIEVAL_SMOKE_LIMIT,
        )
        sparse_vector = sparse_by_id[candidate.chunk_id]
        sparse_hits = (
            await store.search_sparse(tenant_id, kb_id, sparse_vector, _RETRIEVAL_SMOKE_LIMIT)
            if sparse_vector
            else []
        )
        hit_ids = {hit.id for hit in (*dense_hits, *sparse_hits)}
        if candidate.chunk_id not in hit_ids:
            raise RepairContractError("RETRIEVAL_SMOKE_FAILED")
    return len(samples)


def _post_reconcile_summary(plan: RepairPlan, report) -> dict[str, Any]:
    """Reduce a post-write exact report to non-sensitive aggregate evidence."""
    if not getattr(report, "exact", False) or getattr(report, "errors", None):
        raise RepairContractError("POST_RECONCILIATION_INCOMPLETE")
    repaired_ids = {candidate.chunk_id for candidate in plan.candidates}
    remaining_ids = repaired_ids.intersection(set(report.missing_points))
    if remaining_ids:
        raise RepairContractError("POST_RECONCILIATION_TARGETS_STILL_MISSING")
    return {
        "status": str(report.status),
        "verified_points": len(repaired_ids),
        "remaining_missing_points_in_scope": len(report.missing_points),
        "remaining_extra_points_in_scope": len(report.extra_points),
        "remaining_wrong_tenant_points_in_scope": len(report.wrong_tenant_points),
        "remaining_malformed_points_in_scope": len(report.malformed_points),
    }


async def verify_execute_plan(
    plan: RepairPlan,
    settings,
    *,
    admin_tenant: str,
    kb_id: str | None,
    qdrant_page_size: int,
    qdrant_max_pages: int,
    qdrant_max_points: int,
) -> dict[str, Any]:
    """Run the required post-execute exact reconciliation as a read-only step."""
    _validate_plan(plan)
    if not plan.candidates:
        return {
            "status": "skipped",
            "verified_points": 0,
            "remaining_missing_points_in_scope": 0,
            "remaining_extra_points_in_scope": 0,
            "remaining_wrong_tenant_points_in_scope": 0,
            "remaining_malformed_points_in_scope": 0,
        }
    store = QdrantStore(settings)
    try:
        report = await reconcile_vectors(
            store,
            admin_tenant,
            kb_id=kb_id,
            scan_page_size=qdrant_page_size,
            scan_max_pages=qdrant_max_pages,
            scan_max_points=qdrant_max_points,
        )
        return _post_reconcile_summary(plan, report)
    finally:
        store.close()


def _validate_target(args: argparse.Namespace) -> None:
    if not str(args.target_id or "").strip() or args.environment not in {"prod", "staging"}:
        raise RepairContractError("explicit prod/staging target is required")
    database = urlparse(args.database_url or "")
    database_host = (database.hostname or "").lower().rstrip(".")
    if (
        not args.database_url
        or database.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}
        or not database_host
        or database_host in {"127.0.0.1", "localhost", "::1"}
    ):
        raise RepairContractError("database endpoint is missing or local dev")
    qdrant = urlparse(args.qdrant_url or "")
    qdrant_host = (qdrant.hostname or "").lower().rstrip(".")
    if (
        not args.qdrant_url
        or qdrant.scheme not in {"http", "https"}
        or not qdrant_host
        or qdrant_host in {"127.0.0.1", "localhost", "::1"}
    ):
        raise RepairContractError("qdrant endpoint is missing or local dev")


async def _build_plan(args: argparse.Namespace) -> tuple[RepairPlan, Any]:
    previous_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = args.database_url
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={"database_url": args.database_url, "qdrant_url": args.qdrant_url, "qdrant_local_path": ""}
    )
    store = QdrantStore(settings)
    try:
        report = await reconcile_vectors(
            store,
            args.admin_tenant,
            kb_id=args.kb,
            scan_page_size=args.qdrant_page_size,
            scan_max_pages=args.qdrant_max_pages,
            scan_max_points=args.qdrant_max_points,
        )
    finally:
        store.close()
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()
    if report.errors:
        raise RepairContractError("A0_RECONCILIATION_INCOMPLETE")
    if not getattr(report, "exact", False):
        raise RepairContractError("A0_EXACT_RECONCILIATION_REQUIRED")
    missing = _normalise_missing_ids(list(report.missing_points))
    if len(missing) > _MAX_QDRANT_MAX_POINTS:
        raise RepairContractError("A0_MISSING_POINT_LIMIT_EXCEEDED")
    scope_by_id = _missing_scope_by_id(report, missing)
    matching_ids = None
    if args.doc:
        matching_ids = await _read_matching_chunk_ids(
            args.database_url,
            missing,
            kb_id=args.kb,
            doc_id=args.doc,
            scope_by_id=scope_by_id,
        )
    selected_ids, scoped_missing_count, truncated = select_missing_ids(
        missing,
        matching_ids=matching_ids,
        max_points=args.max_points,
    )
    candidates, skipped = await _read_candidates(
        args.database_url,
        selected_ids,
        kb_id=args.kb,
        doc_id=args.doc,
        scope_by_id=scope_by_id,
    )
    plan = estimate_plan(
        candidates,
        batch_size=args.batch_size,
        price_per_million_tokens=args.embedding_price_per_million_tokens,
        missing_point_count=scoped_missing_count,
        skipped_missing_text=skipped,
        truncated=truncated,
    )
    return plan, settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--environment", choices=("prod", "staging"), required=True)
    parser.add_argument("--database-url", default=os.getenv("READONLY_DATABASE_URL"))
    parser.add_argument("--qdrant-url", default=os.getenv("READONLY_QDRANT_URL"))
    parser.add_argument("--admin-tenant", default="00000000-0000-0000-0000-000000000000")
    parser.add_argument("--kb")
    parser.add_argument("--doc")
    parser.add_argument("--max-points", type=int, default=_DEFAULT_MAX_POINTS)
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--qdrant-page-size", type=int, default=100)
    parser.add_argument("--qdrant-max-pages", type=int, default=1000)
    parser.add_argument("--qdrant-max-points", type=int, default=1_000_000)
    parser.add_argument("--embedding-price-per-million-tokens", type=float)
    parser.add_argument("--max-cost-cny", type=float)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="explicitly upsert A0-confirmed missing points",
    )
    parser.add_argument("--json", action="store_true")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    _validate_target(args)
    if args.max_points <= 0 or args.max_points > _MAX_REPAIR_POINTS:
        raise RepairContractError("max-points out of bounds")
    if args.batch_size <= 0 or args.batch_size > _MAX_BATCH_SIZE:
        raise RepairContractError("batch-size out of bounds")
    if args.concurrency <= 0 or args.concurrency > _MAX_CONCURRENCY:
        raise RepairContractError("concurrency out of bounds")
    if args.qdrant_page_size <= 0 or args.qdrant_page_size > _MAX_QDRANT_PAGE_SIZE:
        raise RepairContractError("qdrant page-size out of bounds")
    if args.qdrant_max_pages <= 0 or args.qdrant_max_pages > _MAX_QDRANT_MAX_PAGES:
        raise RepairContractError("qdrant max-pages out of bounds")
    if args.qdrant_max_points <= 0 or args.qdrant_max_points > _MAX_QDRANT_MAX_POINTS:
        raise RepairContractError("qdrant max-points out of bounds")
    if args.execute and args.embedding_price_per_million_tokens is None:
        raise RepairContractError("EXECUTE_REQUIRES_KNOWN_COST")
    for name, value in (("kb", args.kb), ("doc", args.doc), ("admin-tenant", args.admin_tenant)):
        if value is None:
            continue
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise RepairContractError(f"invalid {name} UUID") from exc
    if args.embedding_price_per_million_tokens is not None and (
        not math.isfinite(args.embedding_price_per_million_tokens)
        or args.embedding_price_per_million_tokens < 0
    ):
        raise RepairContractError("embedding price must be finite and non-negative")
    if args.max_cost_cny is not None and (
        not math.isfinite(args.max_cost_cny) or args.max_cost_cny < 0
    ):
        raise RepairContractError("max-cost-cny must be finite and non-negative")
    plan, settings = await _build_plan(args)
    if args.execute:
        validate_execute_budget(plan, args.max_cost_cny)
        repaired = await execute_plan(
            plan,
            settings,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            max_cost_cny=args.max_cost_cny,
        )
        post_reconciliation = await verify_execute_plan(
            plan,
            settings,
            admin_tenant=args.admin_tenant,
            kb_id=args.kb,
            qdrant_page_size=args.qdrant_page_size,
            qdrant_max_pages=args.qdrant_max_pages,
            qdrant_max_points=args.qdrant_max_points,
        )
        output = {
            **plan.as_dict(),
            "mode": "execute",
            "upserted_points": repaired,
            "retrieval_smoke": {
                "status": "passed" if repaired else "skipped",
                "kb_count": len({(item.tenant_id, item.kb_id) for item in plan.candidates}),
            },
            "post_reconciliation": post_reconciliation,
        }
    else:
        output = plan.as_dict()
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "mode={mode} confirmed_missing={missing_points_confirmed_by_a0} "
            "selected={selected_points} chars={total_input_characters} batches={api_batches} "
            "estimated_cost_cny={estimated_cost_cny}".format(**output)
        )
    return 0 if plan.missing_point_count == 0 or args.execute else 1


def main() -> int:
    try:
        return asyncio.run(_async_main(_parser().parse_args()))
    except RepairContractError as exc:
        print(f"repair_contract_error={str(exc)[:96]}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - documented CLI failure exit
        print(f"repair_error={type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
