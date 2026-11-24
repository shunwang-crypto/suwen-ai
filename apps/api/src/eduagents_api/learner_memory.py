"""Host-owned memory admission and retrieval, scoped by authenticated identity."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eduagents_core.education.memory import fingerprint, recall
from sqlalchemy import select, text

from .db import tenant_session
from .models import (
    KnowledgeComponent,
    LearnerInsight,
    LearnerMemoryPolicy,
    OrgMember,
    UserPreference,
)


async def lock_memory(session, user_id):
    # Serializes extraction and correction even before the first policy row.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"learner-memory:{user_id}"},
    )


async def revise_memory(session, ctx, *, rejected_text=None, stop_old_extraction=False):
    """Caller holds lock_memory. Keep no plaintext copy of erased memories."""
    policy = await session.get(LearnerMemoryPolicy, ctx.user_id)
    if policy is None:
        policy = LearnerMemoryPolicy(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id, revision=0, blocked_fingerprints=[]
        )
        session.add(policy)
    policy.revision += 1
    policy.updated_at = datetime.now(UTC)
    if stop_old_extraction:
        policy.extraction_after = policy.updated_at
    if rejected_text:
        policy.blocked_fingerprints = sorted({*(policy.blocked_fingerprints or []), fingerprint(rejected_text)})
    await session.flush()
    # Remove recalled tool history and compaction summaries immediately. The
    # original chat messages and submitted business artifacts remain intact.
    await session.execute(text(
        "UPDATE harness_sessions SET native_archive='{}'::jsonb,native_session_id='' WHERE user_id=:uid"
    ), {"uid": ctx.user_id})


def insight_payload(row, name="", aliases=None):
    return {
        "id": str(row.id), "kc_id": str(row.kc_id) if row.kc_id else None,
        "kc_name": name or "", "aliases": aliases or [],
        "source": row.source, "kind": row.kind, "text": row.text,
        "session_id": str(row.session_id) if row.session_id else None,
        "source_message_id": str(row.source_message_id) if row.source_message_id else None,
        "observed_at": row.observed_at, "expires_at": row.expires_at,
        "updated_at": row.updated_at, "revision": row.revision,
        "user_corrected": bool(row.user_corrected), "status": row.status,
        "suppressed": bool(row.suppressed), "created_at": row.created_at,
    }


class LearnerMemoryService:
    async def snapshot(self, state):
        uid, tid = uuid.UUID(state["user_id"]), uuid.UUID(state["tenant_id"])
        async with tenant_session(tid) as session:
            pref = await session.get(UserPreference, uid)
            policy = await session.get(LearnerMemoryPolicy, uid)
            has_memories = await session.scalar(select(LearnerInsight.id).where(
                LearnerInsight.user_id == uid,
            ).limit(1)) is not None
            return {"revision": policy.revision if policy else 0,
                    "enabled": pref.personalization_enabled if pref else True,
                    "has_memories": has_memories}

    async def search(self, state, query, kc_names, limit=4):
        uid, tid = uuid.UUID(state["user_id"]), uuid.UUID(state["tenant_id"])
        # Membership comes from the host, never from model tool arguments.
        async with tenant_session(tid) as session:
            org_ids = tuple(str(v) for v in (await session.scalars(
                select(OrgMember.org_id).where(OrgMember.user_id == uid)
            )).all())
        async with tenant_session(tid, org_ids=org_ids) as session:
            pref = await session.get(UserPreference, uid)
            if pref is not None and not pref.personalization_enabled:
                return {"memories": [], "disabled": True}
            stmt = (
                select(LearnerInsight, KnowledgeComponent.canonical_name, KnowledgeComponent.aliases)
                .outerjoin(KnowledgeComponent, KnowledgeComponent.id == LearnerInsight.kc_id)
                .where(LearnerInsight.user_id == uid, LearnerInsight.tenant_id == tid)
                .order_by(LearnerInsight.created_at.desc(), LearnerInsight.id.desc()).limit(100)
            )
            kb_ids = state.get("kb_ids") or ([state["kb_id"]] if state.get("kb_id") else [])
            if kb_ids:
                stmt = stmt.where(KnowledgeComponent.kb_id.in_([uuid.UUID(k) for k in kb_ids]))
            items = [insight_payload(row, name, aliases) for row, name, aliases in (await session.execute(stmt)).all()]
        return recall(items, query[:500], kc_names[:8], limit=limit)
