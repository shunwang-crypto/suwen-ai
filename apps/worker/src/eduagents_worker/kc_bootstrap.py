"""Deterministic chapter-KC bootstrap for stub ingestion.

The stub gateway must still leave a real, tenant-scoped KC graph behind so a
new learner can start a diagnostic flow.  This module deliberately does not
inspect chunk text or call a model: the only source of a fallback KC is the
structural heading path already persisted with the child chunk.

The database path uses an additive migration's ``(kb_id, stable_key)``
conditional unique index as its idempotency boundary.  The small compatibility
branch for pre-0028 metadata is useful while an old worker is being upgraded;
the production path always takes the stable-key conflict target.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from eduagents_core.logging import get_logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

log = get_logger("kc_bootstrap")

# A chapter KC is a coarse fallback, not a per-paragraph tag.  The cap is
# intentionally explicit and bounded; overflow is assigned to one stable
# document-level bucket below.
MAX_CHAPTER_KCS = 32
_OTHER_CHAPTER = "其他章节"
_UNSECTIONED = "未分章节"
_HEADING_SEPARATOR_RE = re.compile(r"\s*>\s*")


class KcBootstrapError(RuntimeError):
    """The document cannot be made safe for KC-backed practice."""


class KcBootstrapStale(KcBootstrapError):
    """A newer document generation replaced this bootstrap attempt."""


@dataclass(frozen=True, slots=True)
class ChapterKey:
    """A display name plus its stable, normalized identity."""

    stable_key: str
    canonical_name: str
    normalized_path: str


@dataclass(frozen=True, slots=True)
class ChildChapterAssignment:
    """The chapter KC assignment planned for one child chunk."""

    child_id: str
    stable_key: str
    canonical_name: str


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """Bounded audit result; it intentionally contains no document content."""

    tenant_id: str
    kb_id: str
    doc_id: str
    child_count: int
    chapter_count: int
    kc_ids: tuple[str, ...]


def _display_normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split()).strip()


def _key_normalize(value: object) -> str:
    # casefold makes ASCII/Latin heading variants converge while leaving
    # Chinese headings unchanged.  Separators are normalized by the caller.
    return _display_normalize(value).casefold()


def normalized_heading_parts(heading_path: object) -> tuple[str, ...]:
    """Return non-empty, whitespace-normalized heading components.

    Parser output uses ``>`` as the path separator, but accepting repeated or
    padded separators here makes IDs stable across parser versions and replay
    fixtures.  No chunk body is ever included in the result.
    """

    text = unicodedata.normalize("NFKC", str(heading_path or ""))
    parts = (_display_normalize(part) for part in _HEADING_SEPARATOR_RE.split(text))
    return tuple(part for part in parts if part)


def chapter_key_for_heading(
    heading_path: object,
    *,
    document_name: object,
    document_id: object,
) -> ChapterKey:
    """Build the stable chapter identity for one child chunk.

    Non-empty paths use the deepest title as the display name but retain the
    complete normalized path in the key.  Thus two ``定义`` headings below
    different parents do not collide.  Unsectioned content is document-scoped
    because otherwise every such document in a KB would share one fallback KC.
    """

    parts = normalized_heading_parts(heading_path)
    if parts:
        normalized_path = " > ".join(_key_normalize(part) for part in parts)
        return ChapterKey(
            stable_key=f"chapter:{normalized_path}",
            canonical_name=parts[-1],
            normalized_path=normalized_path,
        )

    display_document = _display_normalize(document_name) or "未命名文档"
    normalized_document = _key_normalize(display_document)
    document_token = _display_normalize(document_id)
    normalized_path = f"{normalized_document} · {_UNSECTIONED.casefold()} · doc:{document_token}"
    return ChapterKey(
        stable_key=f"chapter:{normalized_path}",
        canonical_name=f"{display_document} · {_UNSECTIONED}",
        normalized_path=normalized_path,
    )


def stable_kc_id(kb_id: uuid.UUID | str, stable_key: str) -> uuid.UUID:
    """Return the deterministic KC UUID for a KB-local stable key."""

    return uuid.uuid5(uuid.UUID(str(kb_id)), stable_key)


def _overflow_key(document_name: object) -> ChapterKey:
    display_document = _display_normalize(document_name) or "未命名文档"
    normalized_document = _key_normalize(display_document)
    normalized_path = f"{normalized_document} > {_OTHER_CHAPTER.casefold()}"
    return ChapterKey(
        stable_key=f"chapter:{normalized_path}",
        canonical_name=_OTHER_CHAPTER,
        normalized_path=normalized_path,
    )


def plan_chapter_assignments(
    children: Iterable[Mapping[str, Any]],
    *,
    document_name: object,
    document_id: object,
    max_chapters: int = MAX_CHAPTER_KCS,
) -> dict[str, ChildChapterAssignment]:
    """Plan bounded chapter assignments independent of input ordering.

    At most ``max_chapters`` distinct identities survive.  When the cap is
    exceeded, lexicographically stable paths are retained and the remainder
    goes to one document-scoped ``其他章节`` bucket.  Unsectioned content is
    retained preferentially so the explicit document-scoped fallback is not
    accidentally swallowed by the overflow bucket.
    """

    if max_chapters < 1:
        raise ValueError("max_chapters must be at least one")

    planned: dict[str, ChapterKey] = {}
    for child in children:
        if "id" not in child:
            raise KcBootstrapError("child chunk is missing an id")
        child_id = str(child["id"])
        planned[child_id] = chapter_key_for_heading(
            child.get("heading_path", ""),
            document_name=document_name,
            document_id=document_id,
        )

    distinct = {key.stable_key: key for key in planned.values()}
    if len(distinct) > max_chapters:
        unsectioned = [key for key in distinct.values() if ":doc:" in key.normalized_path]
        regular = sorted(
            (key for key in distinct.values() if key not in unsectioned),
            key=lambda key: key.stable_key,
        )
        keep: list[ChapterKey] = []
        if unsectioned:
            keep.append(unsectioned[0])
        if max_chapters == 1 and keep:
            # Preserve the explicit unsectioned identity even under the
            # smallest legal cap; every other child shares that one safe
            # document-scoped fallback rather than changing its meaning to a
            # generic bucket.
            planned = {
                child_id: keep[0] for child_id in planned
            }
            return {
                child_id: ChildChapterAssignment(
                    child_id=child_id,
                    stable_key=key.stable_key,
                    canonical_name=key.canonical_name,
                )
                for child_id, key in planned.items()
            }
        keep.extend(regular[: max(0, max_chapters - len(keep) - 1)])
        overflow = _overflow_key(document_name)
        # The overflow bucket itself counts toward the cap.  If the cap is 1,
        # all children intentionally share that one bucket.
        allowed = {key.stable_key: key for key in keep}
        allowed[overflow.stable_key] = overflow
        if len(allowed) > max_chapters:
            allowed = {overflow.stable_key: overflow}
        planned = {
            child_id: (key if key.stable_key in allowed else overflow)
            for child_id, key in planned.items()
        }

    return {
        child_id: ChildChapterAssignment(
            child_id=child_id,
            stable_key=key.stable_key,
            canonical_name=key.canonical_name,
        )
        for child_id, key in planned.items()
    }


def _source_kind(row: object) -> str:
    # Existing rows from before 0028 are semantically enrichment rows.  The
    # final schema always has the explicit field; this fallback keeps an old
    # worker from crashing during a rolling schema upgrade.
    return str(getattr(row, "source_kind", "enrichment") or "enrichment")


async def bootstrap_document_kcs(
    tenant_id: uuid.UUID | str,
    doc_id: uuid.UUID | str,
    *,
    max_chapters: int = MAX_CHAPTER_KCS,
    expected_version: int | None = None,
    expected_job_id: str | None = None,
) -> BootstrapResult:
    """Create chapter fallback KCs and bind every child chunk in one transaction.

    The document and all child rows are resolved inside the tenant-scoped
    session.  Inserts are ``ON CONFLICT DO NOTHING`` against the migration's
    unique stable-key index; no read-before-insert race is used.  Existing KC
    IDs on a chunk are preserved while the chapter ID is added.
    """

    from eduagents_api.db import tenant_session
    from eduagents_api.models import Chunk, Document, KnowledgeComponent

    tenant_uuid = uuid.UUID(str(tenant_id))
    document_uuid = uuid.UUID(str(doc_id))
    stable_attr = getattr(KnowledgeComponent, "stable_key", None)
    source_attr = getattr(KnowledgeComponent, "source_kind", None)

    async with tenant_session(tenant_uuid) as session:
        document = await session.get(Document, document_uuid, with_for_update=True)
        if document is None:
            raise KcBootstrapError("document not found")
        if uuid.UUID(str(document.tenant_id)) != tenant_uuid:
            raise KcBootstrapError("document tenant mismatch")
        if expected_version is not None and int(document.version) != int(expected_version):
            raise KcBootstrapStale("document generation changed during KC bootstrap")
        if expected_job_id is not None:
            meta = dict(document.recognition_meta_json or {})
            if meta.get("ingest_job_id") != expected_job_id:
                raise KcBootstrapStale("document ingestion claim changed during KC bootstrap")

        kb_uuid = uuid.UUID(str(document.kb_id))
        children = (
            await session.scalars(
                select(Chunk)
                .where(
                    Chunk.tenant_id == tenant_uuid,
                    Chunk.doc_id == document_uuid,
                    Chunk.parent_id.isnot(None),
                )
                .order_by(Chunk.created_at, Chunk.id)
            )
        ).all()
        if not children:
            raise KcBootstrapError("document has no child chunks")

        assignments = plan_chapter_assignments(
            (
                {"id": str(child.id), "heading_path": child.heading_path}
                for child in children
            ),
            document_name=document.filename,
            document_id=document_uuid,
            max_chapters=max_chapters,
        )
        keys = {assignment.stable_key: assignment for assignment in assignments.values()}
        deterministic_ids = {
            stable_key: stable_kc_id(kb_uuid, stable_key) for stable_key in keys
        }

        values: list[dict[str, Any]] = []
        for stable_key, assignment in keys.items():
            row: dict[str, Any] = {
                "id": deterministic_ids[stable_key],
                "tenant_id": tenant_uuid,
                "kb_id": kb_uuid,
                "canonical_name": assignment.canonical_name,
                "aliases": [],
                "description": "从文档章节结构建立的临时知识点",
            }
            if stable_attr is not None:
                row["stable_key"] = stable_key
            if source_attr is not None:
                row["source_kind"] = "chapter_fallback"
            values.append(row)

        statement = pg_insert(KnowledgeComponent).values(values)
        if stable_attr is not None:
            statement = statement.on_conflict_do_nothing(
                index_elements=[KnowledgeComponent.kb_id, KnowledgeComponent.stable_key],
                index_where=KnowledgeComponent.stable_key.is_not(None),
            )
        else:
            # Compatibility only for a worker started before migration 0028;
            # deterministic primary keys still make replay safe, but the
            # deployed schema always uses the stable-key target above.
            statement = statement.on_conflict_do_nothing(
                index_elements=[KnowledgeComponent.id]
            )
        await session.execute(statement)

        if stable_attr is not None:
            kc_rows = (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.tenant_id == tenant_uuid,
                        KnowledgeComponent.kb_id == kb_uuid,
                        KnowledgeComponent.stable_key.in_(tuple(keys)),
                    )
                )
            ).all()
            by_key = {str(row.stable_key): row for row in kc_rows}
        else:
            kc_rows = (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.tenant_id == tenant_uuid,
                        KnowledgeComponent.kb_id == kb_uuid,
                        KnowledgeComponent.id.in_(tuple(deterministic_ids.values())),
                    )
                )
            ).all()
            by_key = {
                stable_key: row
                for stable_key, expected_id in deterministic_ids.items()
                for row in kc_rows
                if row.id == expected_id
            }

        if set(by_key) != set(keys):
            raise KcBootstrapError("chapter KC upsert did not produce a complete set")
        if any(
            uuid.UUID(str(row.tenant_id)) != tenant_uuid
            or uuid.UUID(str(row.kb_id)) != kb_uuid
            or _source_kind(row) not in {"chapter_fallback", "enrichment"}
            for row in by_key.values()
        ):
            raise KcBootstrapError("chapter KC ownership or source invariant failed")

        # ``kc_ids`` is JSON rather than a database FK.  Preserve historical
        # bindings only after resolving them back through the current tenant
        # and KB; a stale/cross-KB value must not be carried into the newly
        # safe child binding.
        historical_candidates: set[uuid.UUID] = set()
        for child in children:
            for value in child.kc_ids or []:
                try:
                    historical_candidates.add(uuid.UUID(str(value)))
                except (TypeError, ValueError, AttributeError):
                    continue
        valid_historical_ids: set[str] = set()
        if historical_candidates:
            historical_rows = (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.tenant_id == tenant_uuid,
                        KnowledgeComponent.kb_id == kb_uuid,
                        KnowledgeComponent.id.in_(tuple(historical_candidates)),
                    )
                )
            ).all()
            valid_historical_ids = {str(row.id) for row in historical_rows}

        for child in children:
            assignment = assignments[str(child.id)]
            kc_id = str(by_key[assignment.stable_key].id)
            existing_ids = [
                str(value)
                for value in (child.kc_ids or [])
                if str(value) in valid_historical_ids
            ]
            merged_ids = list(dict.fromkeys([*existing_ids, kc_id]))
            result = await session.execute(
                update(Chunk)
                .where(
                    Chunk.id == child.id,
                    Chunk.tenant_id == tenant_uuid,
                    Chunk.doc_id == document_uuid,
                    Chunk.parent_id.isnot(None),
                )
                .values(kc_ids=merged_ids)
            )
            if result.rowcount != 1:
                raise KcBootstrapError("child KC binding lost its tenant/document guard")

        result = BootstrapResult(
            tenant_id=str(tenant_uuid),
            kb_id=str(kb_uuid),
            doc_id=str(document_uuid),
            child_count=len(children),
            chapter_count=len(by_key),
            kc_ids=tuple(sorted(str(row.id) for row in by_key.values())),
        )
        log.info(
            "chapter_kc_bootstrap_done",
            tenant_id=str(tenant_uuid),
            kb_id=str(kb_uuid),
            doc_id=str(document_uuid),
            children=result.child_count,
            chapters=result.chapter_count,
        )
        return result


__all__ = [
    "BootstrapResult",
    "ChapterKey",
    "ChildChapterAssignment",
    "KcBootstrapError",
    "KcBootstrapStale",
    "MAX_CHAPTER_KCS",
    "bootstrap_document_kcs",
    "chapter_key_for_heading",
    "normalized_heading_parts",
    "plan_chapter_assignments",
    "stable_kc_id",
]
