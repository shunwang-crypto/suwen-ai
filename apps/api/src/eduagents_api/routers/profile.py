"""F8 "AI 眼中的我": 画像全量读取 + 用户纠正 (最终解释权)。

- GET /api/profile: 掌握度全量 (join KC 名) + 错题分布 + 最近 10 条掌握度变化流水 (F8-A3)。
- GET /api/profile/weak-points: 定向出题将要覆盖的薄弱点预览 (与 POST /api/quiz
  的 from_weak_points 走同一函数, 预览即所得)。
- POST /api/profile/kc/{kc_id}/override: 用户直改 p_mastery, 确定性直写
  mastery_record + mastery_log(evidence_type="user_override", weight=1.0)。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eduagents_core.mastery.bkt import BktParams
from eduagents_core.mastery.thresholds import QUIZ_WEAK_THRESHOLD
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import ErrorItem, KnowledgeComponent, MasteryLog, MasteryRecord
from ..profile_summary import QUIZ_WEAK_TOP, weak_point_kcs

router = APIRouter(prefix="/api/profile", tags=["profile"])

_RECENT_LOG_LIMIT = 10


class OverrideIn(BaseModel):
    p_mastery: float = Field(ge=0.0, le=1.0)


@router.get("")
async def get_profile(ctx: AuthContext = Depends(api_rate_limited)):
    async with tenant_session(ctx.tenant_id) as session:
        # 掌握度全量 (join KC 名)
        mastery_rows = (
            await session.execute(
                select(MasteryRecord, KnowledgeComponent.canonical_name)
                .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
                .where(MasteryRecord.user_id == ctx.user_id)
                .order_by(MasteryRecord.p_mastery.asc())
            )
        ).all()
        mastery = [
            {
                "kc_id": str(r.kc_id),
                "name": name,
                "p_mastery": round(r.p_mastery, 4),
                "confidence": round(r.confidence, 4),
                "last_seen": r.last_seen,
            }
            for r, name in mastery_rows
        ]

        # 错题分布: 按错因五分类 / 按状态机各计数
        by_cause = {
            cause: int(n)
            for cause, n in (
                await session.execute(
                    select(ErrorItem.cause, func.count())
                    .where(ErrorItem.user_id == ctx.user_id)
                    .group_by(ErrorItem.cause)
                )
            ).all()
        }
        by_state = {
            state: int(n)
            for state, n in (
                await session.execute(
                    select(ErrorItem.state, func.count())
                    .where(ErrorItem.user_id == ctx.user_id)
                    .group_by(ErrorItem.state)
                )
            ).all()
        }

        # 最近 10 条掌握度变化流水 (F8-A3: 因 X 事件, Y 从 old→new)
        logs = (
            await session.scalars(
                select(MasteryLog)
                .where(MasteryLog.user_id == ctx.user_id)
                .order_by(MasteryLog.at.desc())
                .limit(_RECENT_LOG_LIMIT)
            )
        ).all()
        kc_names: dict[str, str] = {str(r.kc_id): name for r, name in mastery_rows}
        missing = {str(x.kc_id) for x in logs} - set(kc_names)
        if missing:
            for kc in (
                await session.scalars(
                    select(KnowledgeComponent).where(
                        KnowledgeComponent.id.in_([uuid.UUID(m) for m in missing])
                    )
                )
            ).all():
                kc_names[str(kc.id)] = kc.canonical_name
        recent_changes = [
            {
                "id": str(x.id),
                "kc_id": str(x.kc_id),
                "kc_name": kc_names.get(str(x.kc_id), ""),
                "old_p": round(x.old_p, 4),
                "new_p": round(x.new_p, 4),
                "evidence_type": x.evidence_type,
                "evidence_id": str(x.evidence_id) if x.evidence_id else None,
                "weight": x.weight,
                "at": x.at,
            }
            for x in logs
        ]

    return {
        "mastery": mastery,
        "error_distribution": {"by_cause": by_cause, "by_state": by_state},
        "recent_changes": recent_changes,
    }


@router.get("/weak-points")
async def get_weak_points(
    kb_id: uuid.UUID | None = None, ctx: AuthContext = Depends(api_rate_limited)
):
    """薄弱点预览 (M4-D2): 前端"针对我的薄弱点出题"按钮下方展示将要练的 KC 名。

    与 POST /api/quiz (from_weak_points=true) 共用 weak_point_kcs, 预览与实际一致;
    返回空列表即"数据不足", 前端据此禁用按钮。
    """
    items = await weak_point_kcs(ctx.tenant_id, ctx.user_id, [kb_id] if kb_id else [])
    return {
        "threshold": QUIZ_WEAK_THRESHOLD,
        "limit": QUIZ_WEAK_TOP,
        "weak_points": items,
    }


@router.post("/kc/{kc_id}/override")
async def override_mastery(
    kc_id: uuid.UUID, body: OverrideIn, ctx: AuthContext = Depends(api_rate_limited)
):
    """用户纠正画像 ("这个我已经会了"): 直写, 不经 LLM, 全程留审计日志。"""
    async with tenant_session(ctx.tenant_id) as session:
        kc = await session.get(KnowledgeComponent, kc_id)  # RLS 保证只见本租户 KC
        if kc is None:
            raise HTTPException(status_code=404, detail="KC_NOT_FOUND")
        record = await session.scalar(
            select(MasteryRecord).where(
                MasteryRecord.user_id == ctx.user_id, MasteryRecord.kc_id == kc_id
            )
        )
        old_p = record.p_mastery if record else BktParams().p_l0
        if record is None:
            record = MasteryRecord(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                kc_id=kc_id,
                p_mastery=body.p_mastery,
                confidence=1.0,
                last_seen=datetime.now(UTC),
            )
            session.add(record)
        else:
            record.p_mastery = body.p_mastery
            # 用户亲自断言 = 最强证据, 置信度拉满 (用户对自己画像的最终解释权)
            record.confidence = 1.0
            record.last_seen = datetime.now(UTC)
        log_row = MasteryLog(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            kc_id=kc_id,
            old_p=old_p,
            new_p=body.p_mastery,
            evidence_type="user_override",
            evidence_id=None,
            weight=1.0,
        )
        session.add(log_row)
        await session.flush()
        return {
            "kc_id": str(kc_id),
            "kc_name": kc.canonical_name,
            "old_p": round(old_p, 4),
            "new_p": round(body.p_mastery, 4),
            "log_id": str(log_row.id),
        }
