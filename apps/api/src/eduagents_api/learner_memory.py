"""Host-owned memory admission and retrieval, scoped by authenticated identity."""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import UTC, datetime

from eduagents_core.education.memory import (
    effective_status,
    fingerprint,
    latest_observations,
    recall,
)
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
    def __init__(self, embedder=None, *, semantic_enabled=False, timeout=4.0):
        self.embedder, self.semantic_enabled, self.timeout = embedder, semantic_enabled, timeout

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
            policy = await session.get(LearnerMemoryPolicy, uid)
            revision = policy.revision if policy else 0
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
        scores, mode = {}, "lexical"
        if self.semantic_enabled and self.embedder is not None and query.strip():
            eligible = [v for v in latest_observations(items) if effective_status(v, datetime.now(UTC)) == "active"]
            if eligible:
                try:
                    vectors = await asyncio.wait_for(self.embedder.embed(
                        [query[:500], *[(v.get("kc_name", "") + " " + v["text"])[:800] for v in eligible]]
                    ), self.timeout)
                    if len(vectors) != len(eligible) + 1:
                        raise ValueError("memory embedding count mismatch")
                    query_vector = vectors[0]
                    query_norm = math.sqrt(sum(v * v for v in query_vector))
                    for item, vector in zip(eligible, vectors[1:], strict=True):
                        if len(vector) != len(query_vector):
                            raise ValueError("memory embedding dimension mismatch")
                        norm = math.sqrt(sum(v * v for v in vector))
                        value = sum(a * b for a, b in zip(query_vector, vector, strict=True)) / max(query_norm * norm, 1e-12)
                        if not math.isfinite(value):
                            raise ValueError("invalid memory embedding")
                        scores[item["id"]] = value
                    mode = "hybrid"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Retrieval is optional; no secret-bearing provider errors enter the response.
                    scores = {}
                current = await self.snapshot(state)
                if current["revision"] != revision or not current["enabled"]:
                    return {"memories": [], "memory_changed": True, "retrieval_mode": mode}
        return {**recall(items, query[:500], kc_names[:8], limit=limit, semantic_scores=scores), "retrieval_mode": mode}
