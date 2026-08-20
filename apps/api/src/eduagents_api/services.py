from __future__ import annotations

import copy
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from eduagents_core.gateway import ChatMessage as GwMessage
from eduagents_core.gateway import ChatRequest as GwRequest
from eduagents_core.gateway import Tier
from eduagents_core.logging import get_logger
from eduagents_core.mastery.bkt import BktParams, bkt_update
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update

from .db import tenant_session
from .deps import AuthContext
from .metacognition import confidence_snapshot
from .models import (
    ERROR_IMPORT_QUIZ_KIND,
    Attempt,
    Chunk,
    Document,
    ErrorItem,
    KnowledgeBase,
    KnowledgeComponent,
    LearnerInsight,
    LearningEvent,
    MasteryLog,
    MasteryRecord,
    MetacognitionPrediction,
    Plan,
    PlanConcept,
    PlanResource,
    Question,
    Quiz,
)
from .plan_concepts import collect_plan_concepts

log = get_logger("services")

_SHORT_ANSWER_WEIGHT = 0.5  # §5.6: 主观题证据 0.5 权重进 BKT (写死)
_SHORT_PASS_SCORE = 0.6
_MIN_KC_MATCH_LEN = 2  # 归一化后过短的 KC 名不参与子串匹配 (防单字误伤)

# Quiz provenance is separate from Plan.plan_mode.  A quiz without a plan
# attribution can still be mastery-eligible (for example, a learner's
# ordinary practice against already observed KC records), but it is not a
# goal-only calibration activity merely because it has no KB foreign key.
QUIZ_PLAN_MODE_STANDALONE = "standalone"
_QUIZ_PLAN_MODES = {"course_bound", "goal_only", QUIZ_PLAN_MODE_STANDALONE}


@dataclass(frozen=True)
class KcReadiness:
    """The server-side barrier state for a KB-backed diagnostic quiz."""

    status: Literal["not_applicable", "preparing", "ready", "not_ready"]
    code: str | None
    eligible_kc_ids: tuple[str, ...] = ()
    reason: str = ""


class QuizKCError(ValueError):
    """Stable, safe error raised before a quiz can write partial evidence."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or code
        super().__init__(self.message)


def _optional_kc_attr(row, field_name: str):
    """Read Wave 1B ORM fields while remaining compatible with pre-migration rows."""

    return getattr(row, field_name, None)


def _effective_kc_map(
    kc_rows: Sequence[tuple[str, str | None, str | None]],
) -> dict[str, str]:
    """Resolve superseded IDs without leaving the scoped KC set.

    ``kc_rows`` is deliberately a small, already tenant/KB-scoped projection:
    (id, source_kind, superseded_by_id).  A broken chain is rejected by
    omitting it; callers then return a stable not-found/not-ready error rather
    than inventing a replacement KC.
    """

    by_id = {str(row[0]): row for row in kc_rows}
    resolved: dict[str, str] = {}
    for raw_id in by_id:
        current = raw_id
        seen: set[str] = set()
        while True:
            if current in seen or current not in by_id:
                current = ""
                break
            seen.add(current)
            next_id = by_id[current][2]
            if not next_id:
                break
            current = str(next_id)
        if current:
            resolved[raw_id] = current
    return resolved


def assess_quiz_kc_readiness(
    *,
    gateway_mode: str,
    document_statuses: Sequence[str],
    kc_rows: Sequence[tuple[str, str | None, str | None]],
    child_kc_ids: Sequence[Sequence[str]],
    has_source_kind: bool = True,
) -> KcReadiness:
    """Pure readiness policy used by the API barrier and unit tests.

    A KB without documents is intentionally ``not_applicable`` so existing
    no-material/high-level Quiz paths remain usable.  Once documents exist,
    stub accepts only chapter fallback KCs; live/record/replay accepts only
    enrichment KCs and only after every document is enriched.
    """

    if not document_statuses:
        return KcReadiness("not_applicable", None, reason="no_documents")

    statuses = {str(status) for status in document_statuses}
    if "failed" in statuses:
        return KcReadiness("not_ready", "QUIZ_KC_NOT_READY", reason="document_failed")

    if gateway_mode == "stub":
        terminal_statuses = {"ready", "partial_ready", "enriched"}
        if not statuses <= terminal_statuses:
            return KcReadiness("preparing", "QUIZ_KC_PREPARING", reason="document_processing")
    elif statuses != {"enriched"}:
        return KcReadiness("preparing", "QUIZ_KC_PREPARING", reason="enrichment_pending")

    effective = _effective_kc_map(kc_rows)
    row_by_id = {str(row[0]): row for row in kc_rows}
    allowed_kinds = (
        {"chapter_fallback"} if gateway_mode == "stub" else {"enrichment"}
    )
    eligible: list[str] = []
    for raw_id, effective_id in effective.items():
        if raw_id != effective_id:
            continue
        source_kind = row_by_id[raw_id][1]
        # Before the Wave 1B migration there is no source_kind column.  Treat
        # those pre-existing rows as legacy-compatible, while the new schema
        # is strict and must label every row explicitly.
        if not has_source_kind or source_kind in allowed_kinds:
            if effective_id not in eligible:
                eligible.append(effective_id)
    if not eligible:
        return KcReadiness("not_ready", "QUIZ_KC_NOT_READY", reason="no_eligible_kc")

    if has_source_kind:
        if not child_kc_ids or any(not ids for ids in child_kc_ids):
            return KcReadiness(
                "not_ready", "QUIZ_KC_NOT_READY", reason="child_chunk_kc_missing"
            )
        for raw_ids in child_kc_ids:
            child_effective_ids: set[str] = set()
            for raw_id in raw_ids:
                effective_id = effective.get(str(raw_id))
                if effective_id is None:
                    return KcReadiness(
                        "not_ready", "QUIZ_KC_NOT_READY", reason="child_chunk_kc_out_of_scope"
                    )
                child_effective_ids.add(effective_id)
            # A child may retain a historical fallback (or another already
            # scoped binding) after semantic enrichment. Readiness requires at
            # least one KC from the mode's eligible set per child; it must not
            # invalidate a complete semantic binding merely because evidence
            # preserving fallback IDs remain in the JSON snapshot.
            if not child_effective_ids & set(eligible):
                return KcReadiness(
                    "not_ready", "QUIZ_KC_NOT_READY", reason="child_chunk_kc_out_of_scope"
                )

    return KcReadiness("ready", None, tuple(eligible), reason="kc_ready")


async def quiz_kc_readiness(
    session,
    *,
    tenant_id: uuid.UUID | str,
    kb_id: str | uuid.UUID,
    gateway_mode: str,
    org_ids: Sequence[str] = (),
) -> KcReadiness:
    """Load and enforce tenant/KB-scoped readiness for a Quiz preflight."""

    try:
        kb_uuid = uuid.UUID(str(kb_id))
        tenant_uuid = uuid.UUID(str(tenant_id))
    except (TypeError, ValueError):
        return KcReadiness("not_ready", "KB_NOT_FOUND", reason="invalid_scope")

    kb = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_uuid))
    if kb is None:
        return KcReadiness("not_ready", "KB_NOT_FOUND", reason="kb_missing")
    if kb.owner_type == "user" and kb.tenant_id != tenant_uuid:
        return KcReadiness("not_ready", "KB_NOT_FOUND", reason="kb_owner_mismatch")
    if kb.owner_type == "org" and str(kb.tenant_id) not in {str(item) for item in org_ids}:
        return KcReadiness("not_ready", "KB_NOT_FOUND", reason="org_membership_missing")

    documents = (
        await session.scalars(select(Document.status).where(Document.kb_id == kb_uuid))
    ).all()
    kc_rows_orm = (
        await session.scalars(
            select(KnowledgeComponent).where(
                KnowledgeComponent.kb_id == kb_uuid,
                KnowledgeComponent.tenant_id == kb.tenant_id,
            )
        )
    ).all()
    children = (
        await session.scalars(
            select(Chunk.kc_ids).where(
                Chunk.doc_id.in_(
                    select(Document.id).where(Document.kb_id == kb_uuid)
                ),
                Chunk.parent_id.isnot(None),
            )
        )
    ).all()
    has_source_kind = hasattr(KnowledgeComponent, "source_kind")
    kc_rows = [
        (
            str(row.id),
            _optional_kc_attr(row, "source_kind"),
            str(_optional_kc_attr(row, "superseded_by_id"))
            if _optional_kc_attr(row, "superseded_by_id")
            else None,
        )
        for row in kc_rows_orm
    ]
    return assess_quiz_kc_readiness(
        gateway_mode=gateway_mode,
        document_statuses=[str(status) for status in documents],
        kc_rows=kc_rows,
        child_kc_ids=[list(ids or []) for ids in children],
        has_source_kind=has_source_kind,
    )


async def write_learning_event(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    source_type: str,
    source_id: uuid.UUID,
    question_id: uuid.UUID | None,
    item_key: str | None,
    kc_ids: list[str],
    correct: bool,
    score: float | None,
    rating: int | None,
    difficulty_label: str | None,
    occurred_at: datetime,
    idempotency_key: str,
) -> LearningEvent:
    """Insert-or-replay one unified learning event inside the caller's UoW.

    Callers already hold the source row lock where a request can race (review
    and assessment).  The database unique constraint remains the final guard;
    a repeated semantic write returns the original row instead of advancing
    any state a second time.
    """

    existing = await session.scalar(
        select(LearningEvent).where(
            LearningEvent.user_id == user_id,
            LearningEvent.source_type == source_type,
            LearningEvent.source_id == source_id,
            LearningEvent.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.tenant_id != tenant_id
            or existing.question_id != question_id
            or existing.correct != correct
            or existing.rating != rating
            or existing.kc_ids_json != list(kc_ids)
        ):
            raise ValueError("learning event idempotency conflict")
        return existing

    event = LearningEvent(
        tenant_id=tenant_id,
        user_id=user_id,
        source_type=source_type,
        source_id=source_id,
        question_id=question_id,
        item_key=item_key,
        kc_ids_json=list(kc_ids),
        correct=bool(correct),
        score=score,
        rating=rating,
        difficulty_label=difficulty_label,
        occurred_at=occurred_at,
        idempotency_key=idempotency_key,
    )
    session.add(event)
    await session.flush()
    return event

INSIGHT_TEXT_MAX = 600  # learner_insights.text 截断上限 (学生原话/误解描述)
INSIGHTS_KEEP_PER_USER = 100  # 长期记忆每用户保留最近 100 条, 超出删最旧 (P2-1)


async def persist_learner_insights(
    session,
    ctx: AuthContext,
    rows: list[dict],
    *,
    source: str,
    session_id: str | None = None,
) -> int:
    """洞察条目入长期记忆表 (P2-1 唯一写入口, G10: 补上"他在什么语境下困惑")。

    rows: [{"kc_id": str|None, "kind": "confused"|"understood"|"misconception",
    "text": 学生原话或误解描述}]; text 统一截断 ≤ INSIGHT_TEXT_MAX, 空文本如实丢弃。
    写入后按 (created_at, id) 倒序裁剪, 每用户只保留最近 INSIGHTS_KEEP_PER_USER 条;
    返回裁掉的旧行数 (供日志)。
    幂等: reflection 侧由调用方复用 mastery_log 去重; 此外本函数自带内容级去重 ——
    同批次与保留窗口内已有相同 (kind, kc_id, text) 的条目跳过不插。记忆条目直接
    面向用户可见可删, summarize 轮被重试/重放时不得出现一字不差的重复行。
    """
    existing = {
        (kind, str(kc) if kc else None, text)
        for kind, kc, text in (
            await session.execute(
                select(LearnerInsight.kind, LearnerInsight.kc_id, LearnerInsight.text)
                .where(
                    LearnerInsight.user_id == ctx.user_id,
                    LearnerInsight.source == source,
                )
                .order_by(LearnerInsight.created_at.desc(), LearnerInsight.id.desc())
                .limit(INSIGHTS_KEEP_PER_USER)
            )
        ).all()
    }
    inserted = 0
    for row in rows:
        text_value = str(row.get("text") or "").strip()[:INSIGHT_TEXT_MAX]
        if not text_value:
            continue  # 无原话支撑的条目不入库 (与 match_insights 的丢弃口径一致)
        kc_id = row.get("kc_id")
        key = (row["kind"], str(kc_id) if kc_id else None, text_value)
        if key in existing:
            continue
        existing.add(key)
        session.add(
            LearnerInsight(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                kc_id=uuid.UUID(str(kc_id)) if kc_id else None,
                source=source,
                kind=row["kind"],
                text=text_value,
                session_id=uuid.UUID(str(session_id)) if session_id else None,
            )
        )
        inserted += 1
    if not inserted:
        return 0
    await session.flush()
    # 超额裁剪: 第 101 条起 (倒序 offset) 删除。同事务批次共享 now(), 以 id 兜底定序。
    stale_ids = (
        await session.scalars(
            select(LearnerInsight.id)
            .where(LearnerInsight.user_id == ctx.user_id)
            .order_by(LearnerInsight.created_at.desc(), LearnerInsight.id.desc())
            .offset(INSIGHTS_KEEP_PER_USER)
        )
    ).all()
    if stale_ids:
        await session.execute(delete(LearnerInsight).where(LearnerInsight.id.in_(stale_ids)))
    return len(stale_ids)


async def _resolve_kc_ids(
    session,
    kb_id: str | None,
    kc_names: list[str],
    user_id: uuid.UUID | None = None,
    *,
    tenant_id: uuid.UUID | str | None = None,
    org_ids: Sequence[str] = (),
) -> list[str]:
    """kc_name -> 归一化 kc_id (F5-A2: 禁止裸字符串)。无词表/未命中 -> 丢弃该名。

    词表口径: 绑库 = 该库全部 KC; 未绑库但有 user_id = 该用户有掌握度记录的 KC ——
    画像页「验证一下」与薄弱点出题都可以不绑库发起, 名称本就来自其掌握度记录,
    没有这条回退, 验证题会以 kc_ids=[] 落库, 判分永远写不动 BKT (重估空转)。
    """
    if not kc_names or (not kb_id and user_id is None):
        return []
    from eduagents_core.kc import KcEntry, KcVocabulary

    if kb_id:
        try:
            kb_uuid = uuid.UUID(str(kb_id))
        except (TypeError, ValueError):
            return []
        kb = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_uuid))
        if kb is None:
            return []
        if tenant_id is not None:
            tenant_uuid = uuid.UUID(str(tenant_id))
            if kb.owner_type == "user" and kb.tenant_id != tenant_uuid:
                return []
            if kb.owner_type == "org" and str(kb.tenant_id) not in {
                str(item) for item in org_ids
            }:
                return []
        query = select(KnowledgeComponent).where(
            KnowledgeComponent.kb_id == kb_uuid,
            KnowledgeComponent.tenant_id == kb.tenant_id,
        )
        mastery_effective_ids: set[str] | None = None
    else:
        scope_tenant = tenant_id or user_id
        query = select(KnowledgeComponent).where(
            KnowledgeComponent.tenant_id == scope_tenant,
        )
    rows = (await session.scalars(query)).all()
    row_projections = [
        (
            str(row.id),
            _optional_kc_attr(row, "source_kind"),
            str(_optional_kc_attr(row, "superseded_by_id"))
            if _optional_kc_attr(row, "superseded_by_id")
            else None,
        )
        for row in rows
    ]
    effective_map = _effective_kc_map(row_projections)
    if kb_id:
        mastery_effective_ids = None
    else:
        mastery_ids = set(
            str(kc_id)
            for kc_id in (
                await session.scalars(
                    select(MasteryRecord.kc_id).where(
                        MasteryRecord.tenant_id == (tenant_id or user_id),
                        MasteryRecord.user_id == user_id,
                    )
                )
            ).all()
        )
        mastery_effective_ids = {
            effective_map[kc_id] for kc_id in mastery_ids if kc_id in effective_map
        }
    vocab = KcVocabulary(
        entries=[
            KcEntry(
                kc_id=effective_map[str(r.id)],
                canonical_name=r.canonical_name,
                aliases=list(r.aliases),
                description=r.description,
                embedding=None,
            )
            for r in rows
            if effective_map.get(str(r.id)) == str(r.id)
            and (mastery_effective_ids is None or str(r.id) in mastery_effective_ids)
        ]
    )
    resolved: list[str] = []
    for name in kc_names:
        entry = vocab.by_name(name)
        if entry is not None and entry.kc_id not in resolved:
            resolved.append(entry.kc_id)
    return resolved


async def _resolve_persisted_kc_ids(
    session,
    raw_kc_ids: Sequence[object],
    *,
    kb_id: uuid.UUID | None,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    org_ids: Sequence[str] = (),
) -> list[str]:
    """Validate a persisted Question KC snapshot without creating anything."""

    mastery_ids: set[str] = set()
    if kb_id is not None:
        kb = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        if kb is None:
            raise QuizKCError("QUIZ_KC_NOT_FOUND")
        if kb.owner_type == "user" and kb.tenant_id != tenant_id:
            raise QuizKCError("QUIZ_KC_NOT_FOUND")
        if kb.owner_type == "org" and str(kb.tenant_id) not in {
            str(item) for item in org_ids
        }:
            raise QuizKCError("QUIZ_KC_NOT_FOUND")
        rows = (
            await session.scalars(
                select(KnowledgeComponent).where(
                    KnowledgeComponent.kb_id == kb_id,
                    KnowledgeComponent.tenant_id == kb.tenant_id,
                )
            )
        ).all()
    else:
        rows = (
            await session.scalars(
                select(KnowledgeComponent).where(KnowledgeComponent.tenant_id == tenant_id)
            )
        ).all()
        mastery_ids = {
            str(kc_id)
            for kc_id in (
                await session.scalars(
                    select(MasteryRecord.kc_id).where(
                        MasteryRecord.tenant_id == tenant_id,
                        MasteryRecord.user_id == user_id,
                    )
                )
            ).all()
        }

    projections = [
        (
            str(row.id),
            _optional_kc_attr(row, "source_kind"),
            str(_optional_kc_attr(row, "superseded_by_id"))
            if _optional_kc_attr(row, "superseded_by_id")
            else None,
        )
        for row in rows
    ]
    effective_map = _effective_kc_map(projections)
    allowed_effective_ids = {
        effective_map[mastery_id]
        for mastery_id in mastery_ids
        if mastery_id in effective_map
    }
    resolved: list[str] = []
    for raw_id in raw_kc_ids:
        try:
            source_id = str(uuid.UUID(str(raw_id)))
        except (TypeError, ValueError):
            raise QuizKCError("QUIZ_KC_NOT_FOUND") from None
        effective_id = effective_map.get(source_id)
        if effective_id is None:
            raise QuizKCError("QUIZ_KC_NOT_FOUND")
        if kb_id is None and effective_id not in allowed_effective_ids:
            raise QuizKCError("QUIZ_KC_NOT_FOUND")
        if effective_id not in resolved:
            resolved.append(effective_id)
    return resolved


async def finalize_quiz(ctx: AuthContext, local_id: str, state: dict) -> dict:
    """quiz 图完成 -> Quiz + Questions 落库 (kc_names 归一为 kc_ids)。"""
    questions = state.get("questions") or []
    if not isinstance(questions, list) or not questions:
        raise ValueError("QUIZ_FINALIZE_EMPTY")
    kb_id = state.get("kb_id") or None
    evidence_policy = state.get("evidence_policy") or "mastery_eligible"
    if evidence_policy not in {"mastery_eligible", "calibration_only"}:
        raise QuizKCError("INVALID_EVIDENCE_POLICY")
    raw_plan_mode = state.get("plan_mode")
    has_plan_attribution = bool(state.get("plan_id") and state.get("task_id"))
    if (
        isinstance(raw_plan_mode, str)
        and raw_plan_mode not in _QUIZ_PLAN_MODES
    ):
        raise QuizKCError("INVALID_QUIZ_PROVENANCE")
    plan_mode = (
        raw_plan_mode
        if isinstance(raw_plan_mode, str) and raw_plan_mode in _QUIZ_PLAN_MODES
        else (
            "goal_only"
            if has_plan_attribution and evidence_policy == "calibration_only"
            else ("course_bound" if kb_id else QUIZ_PLAN_MODE_STANDALONE)
        )
    )
    # Calibration is a property of an attributed goal-only plan task, never a
    # fallback inferred from a missing KB.  Reject inconsistent graph state
    # before any question or quiz row is written.
    if plan_mode == "goal_only":
        if not has_plan_attribution or evidence_policy != "calibration_only":
            raise QuizKCError("INVALID_QUIZ_PROVENANCE")
        # A caller may still have carried the course KB through a stale
        # checkpoint.  Goal-only attribution owns the scope, so discard it.
        kb_id = None
    elif plan_mode == "course_bound":
        if kb_id is None or evidence_policy != "mastery_eligible":
            raise QuizKCError("INVALID_QUIZ_PROVENANCE")
    elif evidence_policy != "mastery_eligible" or kb_id is not None:
        raise QuizKCError("INVALID_QUIZ_PROVENANCE")
    gateway_mode = state.get("gateway_mode")
    if not isinstance(gateway_mode, str) or not gateway_mode:
        from eduagents_core.config import get_settings

        gateway_mode = get_settings().gateway_mode
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        readiness = None
        kb_uuid = None
        if kb_id:
            try:
                kb_uuid = uuid.UUID(str(kb_id))
            except (TypeError, ValueError):
                raise QuizKCError("KB_NOT_FOUND") from None
            readiness = await quiz_kc_readiness(
                session,
                tenant_id=ctx.tenant_id,
                kb_id=kb_uuid,
                gateway_mode=gateway_mode,
                org_ids=ctx.org_ids,
            )
            if readiness.status in {"preparing", "not_ready"}:
                raise QuizKCError(readiness.code or "QUIZ_KC_NOT_READY")

        resolved_questions: list[tuple[dict, list[str]]] = []
        eligible = set(readiness.eligible_kc_ids) if readiness else set()
        for question in questions:
            if not isinstance(question, dict):
                raise QuizKCError("QUIZ_KC_REQUIRED")
            kc_ids = []
            if evidence_policy == "mastery_eligible":
                kc_ids = await _resolve_kc_ids(
                    session,
                    kb_id,
                    question.get("kc_names", []),
                    user_id=ctx.user_id,
                    tenant_id=ctx.tenant_id,
                    org_ids=ctx.org_ids,
                )
            if readiness is not None and readiness.status == "ready":
                if not kc_ids or not set(kc_ids) <= eligible:
                    raise QuizKCError("QUIZ_KC_REQUIRED")
            elif readiness is not None and readiness.status == "not_applicable" and kc_ids:
                # A documentless KB keeps the old path, but any explicit KC
                # still has to be a real, scoped row.
                kc_ids = await _resolve_persisted_kc_ids(
                    session,
                    kc_ids,
                    kb_id=kb_uuid,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    org_ids=ctx.org_ids,
                )
            resolved_questions.append((question, kc_ids))

        blueprint = dict(state.get("blueprint") or {})
        blueprint["_evidence_policy"] = evidence_policy
        blueprint["_plan_mode"] = plan_mode
        if state.get("plan_id") and state.get("task_id"):
            blueprint["_plan_task_attribution"] = {
                "plan_id": str(state["plan_id"]),
                "task_id": str(state["task_id"]),
            }
        quiz = Quiz(
            id=uuid.UUID(local_id),
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            kb_id=uuid.UUID(kb_id) if kb_id else None,
            title=state.get("topic", "")[:255],
            blueprint_json=blueprint,
            status="ready",
        )
        session.add(quiz)
        # 先 flush 父行: UoW 跨表排序不看数据库外键 (同 enrichment.py 的教训)
        await session.flush()
        out = []
        for q, kc_ids in resolved_questions:
            source_chunk = q.get("source_chunk_id")
            row = Question(
                tenant_id=ctx.tenant_id,
                quiz_id=quiz.id,
                type=q["type"],
                stem=q["stem"],
                options_json=q.get("options", []),
                answer=q["answer"],
                explanation=q.get("explanation", ""),
                kc_ids=kc_ids,
                source_chunk_id=uuid.UUID(source_chunk) if source_chunk else None,
                difficulty=q.get("difficulty", "mid"),
            )
            session.add(row)
            await session.flush()
            out.append(
                {
                    "id": str(row.id),
                    "type": row.type,
                    "stem": row.stem,
                    "options": row.options_json,
                    "difficulty": row.difficulty,
                    "kc_ids": kc_ids,
                }
            )
    return {"quiz_id": local_id, "questions": out}


async def finalize_plan(ctx: AuthContext, local_id: str, state: dict) -> dict:
    """Atomically save a confirmed plan and its stable resource snapshots.

    ``resource_ref`` is only a graph/UI draft convenience.  Every referenced
    retrieval result is re-checked against the visible KB document/chunk rows
    here, then assigned a UUID and copied into the task as ``resource_id``.
    Invalid or stale references degrade to an executable task without a
    resource; they can never attach another tenant's material.
    """
    from eduagents_core.graphs.plan_graph import is_safe_public_url

    try:
        plan_id = uuid.UUID(local_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="INVALID_PLAN_ID") from None

    raw_kb_id = state.get("kb_id") or ""
    plan_mode = state.get("plan_mode")
    if plan_mode not in {"course_bound", "goal_only"}:
        raise HTTPException(status_code=400, detail="INVALID_PLAN_MODE")
    try:
        kb_id = uuid.UUID(str(raw_kb_id)) if raw_kb_id else None
    except ValueError:
        raise HTTPException(status_code=400, detail="INVALID_KB_ID") from None
    if plan_mode == "course_bound" and kb_id is None:
        raise HTTPException(status_code=400, detail="KB_REQUIRED")
    if plan_mode == "goal_only" and kb_id is not None:
        raise HTTPException(status_code=400, detail="GOAL_ONLY_KB_FORBIDDEN")

    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        # Two confirmation requests may arrive from separate tabs. Serialize
        # this learner's lifecycle before replacing the active plan so both
        # transactions cannot race the partial unique index.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"plan-lifecycle:{ctx.tenant_id}:{ctx.user_id}"},
        )
        # Lifecycle is owned by plans: replace the current plan atomically in
        # this transaction so the partial unique index never sees two active
        # rows for the same learner.
        await session.execute(
            update(Plan)
            .where(
                Plan.tenant_id == ctx.tenant_id,
                Plan.user_id == ctx.user_id,
                Plan.status == "active",
            )
            .values(status="archived")
        )
        kb = None
        if kb_id is not None:
            kb = await session.get(KnowledgeBase, kb_id)
            if (
                kb is None
                or (kb.owner_type == "user" and kb.tenant_id != ctx.user_id)
                or (kb.owner_type == "org" and str(kb.tenant_id) not in set(ctx.org_ids))
            ):
                raise HTTPException(status_code=404, detail="KB_NOT_FOUND")

        source_rows = state.get("resources") or []
        resource_ids: dict[int, uuid.UUID] = {}
        resource_objects: list[PlanResource] = []
        for index, raw in enumerate(source_rows):
            if not isinstance(raw, dict):
                continue
            source_type = raw.get("type")
            title = str(raw.get("title") or "学习资料")[:512]
            if source_type in {"kb", "kb_chunk"}:
                try:
                    source_kb_id = uuid.UUID(str(raw.get("kb_id") or raw_kb_id))
                    doc_id = uuid.UUID(str(raw.get("doc_id")))
                    chunk_id = uuid.UUID(str(raw.get("chunk_id")))
                except (TypeError, ValueError):
                    continue
                if kb_id is None or source_kb_id != kb_id:
                    continue
                # The join verifies that both ids belong to the selected KB.
                valid = await session.scalar(
                    select(Chunk.id)
                    .join(Document, Document.id == Chunk.doc_id)
                    .where(
                        Chunk.id == chunk_id,
                        Chunk.doc_id == doc_id,
                        Document.kb_id == source_kb_id,
                    )
                )
                if valid is None:
                    continue
                row = PlanResource(
                    id=uuid.uuid4(),
                    tenant_id=ctx.tenant_id,
                    plan_id=plan_id,
                    source_type="kb_chunk",
                    title=title,
                    kb_id=source_kb_id,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                )
            elif source_type == "web":
                # Course-bound plans are an explicit closed-world scope.  Do
                # not silently downgrade or persist a web result supplied by
                # an edited plan/checkpoint; reject it before the transaction
                # can create any PlanResource rows.
                if plan_mode == "course_bound":
                    raise HTTPException(
                        status_code=400,
                        detail="COURSE_BOUND_WEB_RESOURCE_FORBIDDEN",
                    )
                if not is_safe_public_url(raw.get("url")):
                    continue
                row = PlanResource(
                    id=uuid.uuid4(),
                    tenant_id=ctx.tenant_id,
                    plan_id=plan_id,
                    source_type="web",
                    title=title,
                    url=str(raw["url"]),
                )
            else:
                continue
            resource_objects.append(row)
            resource_ids[index] = row.id

        structure = copy.deepcopy(state.get("plan") or {})
        goal_analysis = state.get("goal_analysis")
        if isinstance(structure, dict) and isinstance(goal_analysis, dict):
            # 确认时的目标快照随计划保存，后续查看不依赖 LangGraph checkpoint。
            structure["goal_analysis"] = copy.deepcopy(goal_analysis)
        skill_gaps = state.get("skill_gaps")
        if isinstance(structure, dict) and isinstance(skill_gaps, list):
            structure["skill_gaps"] = copy.deepcopy(skill_gaps)
        for stage in structure.get("stages", []) if isinstance(structure, dict) else []:
            for day in stage.get("days", []) if isinstance(stage, dict) else []:
                for task in day.get("tasks", []) if isinstance(day, dict) else []:
                    if not isinstance(task, dict):
                        continue
                    raw_task_id = task.get("task_id")
                    try:
                        task["task_id"] = str(uuid.UUID(str(raw_task_id)))
                    except (TypeError, ValueError):
                        task["task_id"] = str(uuid.uuid4())
                    ref = task.get("resource_ref")
                    task["resource_id"] = (
                        str(resource_ids[ref])
                        if isinstance(ref, int)
                        and not isinstance(ref, bool)
                        and ref in resource_ids
                        else None
                    )
                    if task.get("resource_id") is None and task.get("action_type") == "read":
                        task["action_type"] = "tutor"
                    task["evidence_policy"] = (
                        "calibration_only" if plan_mode == "goal_only" else "mastery_eligible"
                    )
                    task["concept_status"] = (
                        "provisional" if plan_mode == "goal_only" else "course_grounded"
                    )

        has_kb_resource = any(row.source_type == "kb_chunk" for row in resource_objects)
        has_web_resource = any(row.source_type == "web" for row in resource_objects)
        if plan_mode == "course_bound" and has_web_resource:
            # Defensive second check in case another source type is added to
            # the parser without receiving the closed-world guard above.
            raise HTTPException(status_code=400, detail="COURSE_BOUND_WEB_RESOURCE_FORBIDDEN")
        if plan_mode == "course_bound" and has_kb_resource:
            grounding_status = "kb_grounded"
        elif plan_mode == "goal_only" and has_web_resource:
            grounding_status = "web_assisted"
        else:
            grounding_status = "unreferenced"

        plan = Plan(
            id=plan_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            goal=str(state.get("goal") or "")[:2000],
            budget_hours=float(state.get("budget_hours_per_day") or 0),
            kb_id=kb_id,
            plan_mode=plan_mode,
            grounding_status=grounding_status,
            status="active",
            structure_json=structure,
        )
        session.add(plan)
        session.add_all(resource_objects)
        await session.flush()
        # 0034: 任务里匹配不到正式 KC 的概念落成"计划临时概念"。
        # 它们只用于计划展示与将来的归并候选, 绝不进入掌握度/证据路径 —— 否则
        # 模型猜的概念会污染 BKT 与成长报告 (见 plan_concepts 模块注释)。
        known_names: list[str] = []
        if kb_id is not None:
            known_names = list(
                (
                    await session.scalars(
                        select(KnowledgeComponent.canonical_name).where(
                            KnowledgeComponent.kb_id == kb_id,
                            KnowledgeComponent.superseded_by_id.is_(None),
                        )
                    )
                ).all()
            )
        provisional = collect_plan_concepts(structure, known_names)
        if provisional:
            session.add_all(
                PlanConcept(
                    tenant_id=ctx.tenant_id,
                    plan_id=plan.id,
                    display_name=item["display_name"],
                    normalized_name=item["normalized_name"],
                    ordinal=index,
                )
                for index, item in enumerate(provisional)
            )
            await session.flush()
        return {
            "plan_id": local_id,
            "plan": structure,
            "kb_id": str(kb_id) if kb_id else None,
            "plan_mode": plan_mode,
            "grounding_status": grounding_status,
            "resources": [_plan_resource_public(row) for row in resource_objects],
        }


def _plan_resource_public(row: PlanResource) -> dict:
    return {
        "id": str(row.id),
        "type": "kb" if row.source_type == "kb_chunk" else "web",
        "source_type": row.source_type,
        "title": row.title,
        "kb_id": str(row.kb_id) if row.kb_id else None,
        "doc_id": str(row.doc_id) if row.doc_id else None,
        "chunk_id": str(row.chunk_id) if row.chunk_id else None,
        "url": row.url,
        "created_at": row.created_at,
    }


async def apply_mastery_evidence(
    session,
    ctx: AuthContext,
    kc_ids: list[str],
    correct: bool,
    weight: float,
    evidence_id,
    evidence_type: str = "quiz_attempt",
    now_dt: datetime | None = None,
) -> list[dict]:
    """掌握度的**唯一**写入口 (EvidenceUpdater): BKT 直写 + mastery_log 审计 (F8-A3)。

    §16 要求所有学习证据走同一条更新链路。此前 quiz / review 等多处各自
    复制了同一段 BKT 逻辑, 其中 review 一路漏掉了幂等去重 —— 复习接口重试会重复
    推同一条证据。新增证据来源请调用本函数, 不要再复制这段逻辑。

    evidence_type must name one of the verified evidence sources.

    幂等 (P1-7): 同一 (user_id, kc_id, evidence_type, evidence_id) 已有 mastery_log
    即跳过，网络重试或并发重放不会重复推进 BKT。
    evidence_id is required so every evidence event is idempotent.
    """
    from eduagents_core.mastery.thresholds import VALID_MASTERY_EVIDENCE_TYPES

    if evidence_type not in VALID_MASTERY_EVIDENCE_TYPES:
        raise ValueError("invalid mastery evidence type")
    if evidence_id is None:
        raise ValueError("evidence_id is required for mastery evidence")
    changes = []
    business_now = now_dt or datetime.now(UTC)
    params = BktParams()
    applied: set[str] = set()
    if kc_ids:
        existing_logs = (
            await session.scalars(
                select(MasteryLog.kc_id).where(
                    MasteryLog.tenant_id == ctx.tenant_id,
                    MasteryLog.user_id == ctx.user_id,
                    MasteryLog.evidence_type == evidence_type,
                    MasteryLog.evidence_id == evidence_id,
                )
            )
        ).all()
        applied = {str(k) for k in existing_logs}
    for kc_uuid in sorted({uuid.UUID(str(value)) for value in kc_ids}, key=str):
        kc_id = str(kc_uuid)
        # Serialize one learner/KC stream before the idempotency recheck.  The
        # transaction lock also covers the no-record-yet case, where a row
        # lock cannot protect concurrent first evidence.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"mastery:{ctx.user_id}:{kc_uuid}"},
        )
        already_applied = await session.scalar(
            select(MasteryLog.id)
            .where(
                MasteryLog.tenant_id == ctx.tenant_id,
                MasteryLog.user_id == ctx.user_id,
                MasteryLog.kc_id == kc_uuid,
                MasteryLog.evidence_type == evidence_type,
                MasteryLog.evidence_id == evidence_id,
            )
            .limit(1)
        )
        if already_applied is not None:
            continue
        if str(kc_id) in applied:
            continue
        record = await session.scalar(
            select(MasteryRecord).where(
                MasteryRecord.user_id == ctx.user_id,
                MasteryRecord.kc_id == kc_uuid,
            )
            .with_for_update()
        )
        old_p = record.p_mastery if record else params.p_l0
        new_p = bkt_update(old_p, correct, params, weight=weight)
        if record is None:
            record = MasteryRecord(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                kc_id=kc_uuid,
                p_mastery=new_p,
                confidence=weight,
                last_seen=business_now,
            )
            session.add(record)
        else:
            record.p_mastery = new_p
            record.confidence = min(1.0, record.confidence + 0.1)
            record.last_seen = business_now
        session.add(
            MasteryLog(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                kc_id=kc_uuid,
                old_p=old_p,
                new_p=new_p,
                evidence_type=evidence_type,
                evidence_id=evidence_id,
                weight=weight,
            )
        )
        changes.append({"kc_id": kc_id, "old_p": round(old_p, 4), "new_p": round(new_p, 4)})
    return changes


async def correct_mastery_evidence(
    session,
    ctx: AuthContext,
    *,
    original_evidence_type: str,
    original_evidence_id: uuid.UUID,
    corrected_evidence_type: str,
    correct: bool,
    weight: float,
    now_dt: datetime | None = None,
) -> list[dict]:
    """Replace a graded event through the same serialized mastery boundary.

    Appeals are corrections, not a second observation.  For each affected KC
    this removes the original event, restores its pre-event state, then writes
    one auditable corrected event while holding the same advisory lock used by
    :func:`apply_mastery_evidence`.
    """
    originals = (
        await session.scalars(
            select(MasteryLog)
            .where(
                MasteryLog.user_id == ctx.user_id,
                MasteryLog.evidence_type == original_evidence_type,
                MasteryLog.evidence_id == original_evidence_id,
            )
            .order_by(MasteryLog.kc_id.asc(), MasteryLog.at.asc())
        )
    ).all()
    changes: list[dict] = []
    for original in originals:
        kc_id = original.kc_id
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"mastery:{ctx.user_id}:{kc_id}"},
        )
        duplicate = await session.scalar(
            select(MasteryLog.id)
            .where(
                MasteryLog.tenant_id == ctx.tenant_id,
                MasteryLog.user_id == ctx.user_id,
                MasteryLog.kc_id == kc_id,
                MasteryLog.evidence_type == corrected_evidence_type,
                MasteryLog.evidence_id == original_evidence_id,
            )
            .limit(1)
        )
        if duplicate is not None:
            continue
        record = await session.scalar(
            select(MasteryRecord)
            .where(
                MasteryRecord.user_id == ctx.user_id,
                MasteryRecord.kc_id == kc_id,
            )
            .with_for_update()
        )
        if record is None:
            continue
        previous_visible = record.p_mastery
        previous_confidence = record.confidence
        record.p_mastery = original.old_p
        # Keep the original audit row. Evidence-state readers count distinct
        # evidence_id values, so the correction remains one observation.
        corrected = await apply_mastery_evidence(
            session,
            ctx,
            [str(kc_id)],
            correct,
            weight,
            original_evidence_id,
            evidence_type=corrected_evidence_type,
            now_dt=now_dt,
        )
        if corrected:
            # A regrade replaces one observation; it must not increase the
            # amount of evidence merely because a grader corrected itself.
            record.confidence = previous_confidence
            corrected[0]["replaced_p"] = round(previous_visible, 4)
            changes.extend(corrected)
    return changes


def match_misconception_insights(
    misconceptions: list[str], vocab: list[tuple[str, str, list[str]]]
) -> tuple[list[str], dict[str, str], list[str | None]]:
    """误解文本批量匹配 KC 词表 (纯函数, 便于单测)。

    vocab: [(kc_id, canonical, aliases)] 按 created_at 序。返回三元组:
    - matched: 命中的 kc_id (词表序去重) — BKT 口径与旧内联实现逐位相同
      (KC canonical/alias 归一化后作为子串出现在任一误解文本中即命中);
    - name_by_id: kc_id -> canonical 名;
    - kc_by_text: 每条文本的首个命中 kc_id (词表序), 未命中为 None —
      供 learner_insights 落库时"能归一匹配 KC 就带 kc_id" (P2-1)。
    """
    from eduagents_core.kc.types import normalize_kc_name

    norm_texts = [normalize_kc_name(t) for t in misconceptions]
    matched: list[str] = []
    name_by_id: dict[str, str] = {}
    kc_by_text: list[str | None] = [None] * len(misconceptions)
    for kc_id, canonical, aliases in vocab:
        keys = [normalize_kc_name(canonical)] + [
            normalize_kc_name(a) for a in aliases if isinstance(a, str)
        ]
        keys = [k for k in keys if len(k) >= _MIN_KC_MATCH_LEN]
        hit = False
        for i, text_norm in enumerate(norm_texts):
            if any(k in text_norm for k in keys):
                hit = True
                if kc_by_text[i] is None:
                    kc_by_text[i] = kc_id
        if hit and kc_id not in matched:
            matched.append(kc_id)
            name_by_id[kc_id] = canonical
    return matched, name_by_id, kc_by_text


async def apply_tutor_summary(ctx: AuthContext, summary: dict) -> list[dict]:
    """Persist tutor misconceptions as learner insight only.

    匹配规则: KC canonical/alias 归一化后作为子串出现在误解文本中即命中;
    词表外的误解不进 BKT、不新建 KC (画像外键只认归一化 kc_id, F5-A2)。
    P2-1 追加: 误解文本本身写入 learner_insights (source=tutor_summary,
    kind=misconception; 能归一匹配 KC 就带 kc_id, 匹配不上也如实保留原文)。
    普通辅导小结不是受控验证事件，不得更新 BKT。
    """
    raw_texts = [
        m.strip() for m in (summary.get("misconceptions") or []) if isinstance(m, str) and m.strip()
    ]
    if not raw_texts:
        return []
    async with tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.scalars(
                select(KnowledgeComponent).order_by(KnowledgeComponent.created_at)
            )
        ).all()
        vocab = [(str(r.id), r.canonical_name, list(r.aliases or [])) for r in rows]
        matched, name_by_id, kc_by_text = match_misconception_insights(raw_texts, vocab)
        await persist_learner_insights(
            session,
            ctx,
            [
                {"kc_id": kc_by_text[i], "kind": "misconception", "text": text}
                for i, text in enumerate(raw_texts)
            ],
            source="tutor_summary",
        )
    return [
        {"kc_id": kc_id, "name": name_by_id.get(kc_id, ""), "recorded": True}
        for kc_id in matched
    ]


async def _validated_quiz_kc_snapshots(
    session,
    ctx: AuthContext,
    quiz: Quiz,
    questions: Sequence[Question],
) -> dict[str, list[str]]:
    """Validate every Quiz question before the first Attempt is inserted."""

    from eduagents_core.config import get_settings

    evidence_policy = (quiz.blueprint_json or {}).get("_evidence_policy", "mastery_eligible")
    if evidence_policy == "calibration_only":
        return {str(question.id): [] for question in questions}
    readiness = None
    if quiz.kb_id is not None:
        readiness = await quiz_kc_readiness(
            session,
            tenant_id=ctx.tenant_id,
            kb_id=quiz.kb_id,
            gateway_mode=get_settings().gateway_mode,
            org_ids=ctx.org_ids,
        )
        if readiness.status in {"preparing", "not_ready"}:
            raise HTTPException(
                status_code=409,
                detail=readiness.code or "QUIZ_KC_NOT_READY",
            )

    eligible = set(readiness.eligible_kc_ids) if readiness else set()
    snapshots: dict[str, list[str]] = {}
    for question in questions:
        raw_ids = list(question.kc_ids or [])
        if not raw_ids:
            # No KB/no documents is the deliberately preserved historical
            # validation path. A document-backed Quiz is different: it is a
            # candidate for the profile loop and cannot silently skip KC.
            if readiness is not None and readiness.status == "ready":
                raise HTTPException(status_code=409, detail="QUIZ_KC_REQUIRED")
            snapshots[str(question.id)] = []
            continue
        try:
            normalized = await _resolve_persisted_kc_ids(
                session,
                raw_ids,
                kb_id=quiz.kb_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                org_ids=ctx.org_ids,
            )
        except QuizKCError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from None
        if readiness is not None and readiness.status == "ready" and not set(normalized) <= eligible:
            raise HTTPException(status_code=409, detail="QUIZ_KC_REQUIRED")
        snapshots[str(question.id)] = normalized
    return snapshots


async def record_attempts(
    ctx: AuthContext,
    gateway,
    quiz_id: uuid.UUID,
    answers: list[dict],
    now_dt: datetime | None = None,
) -> dict:
    """作答判分: 客观题本地判 / 简答 LLM 评分附理由; 错题自动入本; BKT 直写 (F5-A3)。

    now_dt: 业务时间 (路由层从 timectl 虚拟时钟取, §9); 缺省回退系统时间保持向后兼容。
    """
    from eduagents_core.graphs.quiz_graph import grade_objective
    from eduagents_core.mastery.scheduler import new_card_json

    business_now = now_dt or datetime.now(UTC)
    results = []
    plan_task_completion = None
    seen_question_ids: set[str] = set()
    async with tenant_session(ctx.tenant_id) as session:
        quiz = await session.get(Quiz, quiz_id)
        if (
            quiz is None
            or quiz.user_id != ctx.user_id
            or (quiz.blueprint_json or {}).get("kind") == ERROR_IMPORT_QUIZ_KIND
        ):
            raise HTTPException(status_code=404, detail="NOT_FOUND")
        questions = {
            str(q.id): q
            for q in (
                await session.scalars(select(Question).where(Question.quiz_id == quiz_id))
            ).all()
        }
        blueprint = quiz.blueprint_json or {}
        evidence_policy = blueprint.get("_evidence_policy", "mastery_eligible")
        raw_plan_mode = blueprint.get("_plan_mode")
        has_plan_attribution = bool(
            isinstance(blueprint.get("_plan_task_attribution"), dict)
            and blueprint["_plan_task_attribution"].get("plan_id")
            and blueprint["_plan_task_attribution"].get("task_id")
        )
        plan_mode = (
            raw_plan_mode
            if isinstance(raw_plan_mode, str) and raw_plan_mode in _QUIZ_PLAN_MODES
            else (
                "goal_only"
                if has_plan_attribution and evidence_policy == "calibration_only"
                else ("course_bound" if quiz.kb_id else QUIZ_PLAN_MODE_STANDALONE)
            )
        )
        attribution = blueprint.get("_plan_task_attribution")
        if isinstance(raw_plan_mode, str) and raw_plan_mode not in _QUIZ_PLAN_MODES:
            raise HTTPException(status_code=409, detail="INVALID_QUIZ_PROVENANCE")
        if evidence_policy not in {"mastery_eligible", "calibration_only"}:
            raise HTTPException(status_code=409, detail="INVALID_QUIZ_PROVENANCE")
        if plan_mode == "goal_only":
            if not has_plan_attribution or evidence_policy != "calibration_only":
                raise HTTPException(status_code=409, detail="INVALID_QUIZ_PROVENANCE")
        elif plan_mode == "course_bound":
            if quiz.kb_id is None or evidence_policy != "mastery_eligible":
                raise HTTPException(status_code=409, detail="INVALID_QUIZ_PROVENANCE")
        elif evidence_policy != "mastery_eligible" or quiz.kb_id is not None:
            raise HTTPException(status_code=409, detail="INVALID_QUIZ_PROVENANCE")
        kc_snapshots = await _validated_quiz_kc_snapshots(
            session, ctx, quiz, list(questions.values())
        )
        for item in answers:
            q = questions.get(str(item["question_id"]))
            if q is None or str(q.id) in seen_question_ids:
                continue
            seen_question_ids.add(str(q.id))
            response = str(item.get("response", ""))
            llm_score = None
            scored_reason = None
            if q.type in ("single", "multi", "blank"):
                correct = grade_objective({"type": q.type, "answer": q.answer}, response)
                weight = 1.0
            else:  # short: LLM 评分, ≥0.6 记 correct, 0.5 权重 (§5.6)
                result = await gateway.chat(
                    GwRequest(
                        messages=(
                            GwMessage(
                                role="user",
                                content=(
                                    "你是评分助手。对照参考答案给学生作答打分。\n"
                                    f"题目: {q.stem}\n参考答案: {q.answer}\n"
                                    f"学生作答: {response}\n"
                                    '只输出 JSON: {"score": 0到1的小数, "reason": "评分理由"}。'
                                    "reason 中的数学表达式必须使用成对的 $...$, 不得把中文正文"
                                    "放进公式定界符, 不得裸输出完整 LaTeX 公式。"
                                ),
                            ),
                        ),
                        tier=Tier.standard,
                        json_mode=True,
                        feature="quiz_grade",
                        tenant_id=str(ctx.tenant_id),
                        user_id=str(ctx.user_id),
                    )
                )
                import orjson

                try:
                    parsed = orjson.loads(result.text)
                    llm_score = max(0.0, min(1.0, float(parsed.get("score", 0))))
                    scored_reason = str(parsed.get("reason", ""))[:2000]
                except Exception:  # noqa: BLE001 — 评分解析失败按 0 分并如实记录
                    llm_score = 0.0
                    scored_reason = "评分解析失败, 已按 0 分处理, 请人工复核"
                correct = llm_score >= _SHORT_PASS_SCORE
                weight = _SHORT_ANSWER_WEIGHT
            attempt = Attempt(
                at=business_now,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                question_id=q.id,
                response=response,
                is_correct=correct,
                llm_score=llm_score,
                scored_reason=scored_reason,
            )
            session.add(attempt)
            await session.flush()
            predicted_confidence = item.get("predicted_confidence")
            if (
                not isinstance(predicted_confidence, int)
                or isinstance(predicted_confidence, bool)
                or not 1 <= predicted_confidence <= 5
            ):
                predicted_confidence = None
            predicted_correct = item.get("predicted_correct")
            if not isinstance(predicted_correct, bool):
                predicted_correct = None
            if predicted_confidence is not None or predicted_correct is not None:
                session.add(
                    MetacognitionPrediction(
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        attempt_id=attempt.id,
                        question_id=q.id,
                        predicted_confidence=predicted_confidence,
                        predicted_correct=predicted_correct,
                    )
                )
            confidence_at_error, confidence_probability, confidence_scale_version = (
                confidence_snapshot(predicted_confidence, predicted_correct)
            )
            # The same normalized snapshot feeds ErrorItem, LearningEvent,
            # MasteryRecord and MasteryLog in this transaction.
            kc_ids = kc_snapshots[str(q.id)]
            changes = []
            if evidence_policy == "mastery_eligible":
                changes = await apply_mastery_evidence(
                    session,
                    ctx,
                    kc_ids,
                    correct,
                    weight,
                    attempt.id,
                    now_dt=business_now,
                )
            await write_learning_event(
                session,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                source_type="quiz_attempt",
                source_id=attempt.id,
                question_id=q.id,
                item_key=str(q.id),
                kc_ids=kc_ids,
                correct=correct,
                score=llm_score if llm_score is not None else (1.0 if correct else 0.0),
                rating=3 if correct else 1,
                difficulty_label=q.difficulty,
                occurred_at=business_now,
                idempotency_key=f"attempt:{attempt.id}",
            )
            if not correct:
                # F6-A1: 错题 100% 自动入本 (错因初值 knowledge_gap, M4 错因分类细化)
                # F6-A4: 入本即挂 FSRS 卡 (due=业务时间, 当天进复习队列)
                error_item_id = uuid.uuid4()
                # The Attempt, its confidence snapshot, and the ErrorItem are
                # all added to this same tenant transaction.  The unique
                # source_attempt_id constraint is the database backstop for a
                # retry of this write path.
                session.add(
                    ErrorItem(
                        id=error_item_id,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        question_id=q.id,
                        kc_ids=kc_ids,
                        source_attempt_id=attempt.id,
                        confidence_at_error=confidence_at_error,
                        confidence_probability_at_error=confidence_probability,
                        confidence_scale_version=confidence_scale_version,
                        fsrs_card_json=new_card_json(
                            business_now, business_card_id=error_item_id
                        ),
                    )
                )
            results.append(
                {
                    "question_id": str(q.id),
                    "correct": correct,
                    "llm_score": llm_score,
                    "scored_reason": scored_reason,
                    "explanation": q.explanation,
                    "answer": q.answer,
                    "mastery_changes": changes,
                }
            )
        if isinstance(attribution, dict) and len(results) == len(questions) and questions:
            from .plan_adjust import PlanTaskAttributionError, complete_plan_task

            try:
                plan_task_completion = await complete_plan_task(
                    session,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    plan_id=uuid.UUID(str(attribution.get("plan_id") or "")),
                    task_id=str(attribution.get("task_id") or ""),
                    source_type="quiz_attempt",
                    source_id=str(quiz_id),
                    completed_at=business_now,
                )
            except (ValueError, PlanTaskAttributionError) as exc:
                code = getattr(exc, "code", "PLAN_TASK_NOT_FOUND")
                log.warning(
                    "quiz_plan_task_completion_failed",
                    quiz_id=str(quiz_id),
                    code=code,
                )
                plan_task_completion = {"completed": False, "error_code": code}
    total = len(results)
    n_correct = sum(1 for r in results if r["correct"])
    # F3-A3 联动 (尾部追加, 判分主流程一行未改): 本次正确率 <0.6 且存在活跃计划时,
    # 自动按当前学情插入补强任务并写 plan_adjustments; 失败只记日志, 不影响判分返回。
    from .plan_adjust import LOW_SCORE_THRESHOLD, adjust_on_low_score

    plan_adjustment = None
    accuracy = (n_correct / total) if total else 1.0
    attributed_plan_id = None
    if isinstance(attribution, dict):
        try:
            attributed_plan_id = uuid.UUID(str(attribution.get("plan_id") or ""))
        except ValueError:
            attributed_plan_id = None
    if total and accuracy < LOW_SCORE_THRESHOLD:
        plan_adjustment = await adjust_on_low_score(
            ctx,
            accuracy,
            quiz_kb_id=quiz.kb_id,
            attributed_plan_id=attributed_plan_id,
            evidence_policy=evidence_policy,
        )
    return {
        "total": total,
        "correct": n_correct,
        "results": results,
        "plan_adjustment": plan_adjustment,  # 新增键, 老调用方忽略即可
        "plan_task_completion": plan_task_completion,
        "evidence_policy": evidence_policy,
    }
