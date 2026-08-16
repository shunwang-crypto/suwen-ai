"""回答级评分 (0031 M2): ChatGPT "回复优秀/不佳" 的学习平台对应物。

- POST /api/profile/answer-ratings: 对一条 AI 回答点 👍/👎; bad 档可带快捷原因
  (太啰嗦/太简略/没听懂/不是我想问的) — 原因就是天然的风格偏好信号;
- 指纹分工: weak_points/profile_used 由前端从该消息的 profile trace 捕获;
  style/verbosity 落库时由服务端按当前偏好补全 (M3 统计只需近似归属);
- (user_id, message_id) 唯一: 同一条消息改主意 = UPDATE, 不堆行;
- 提交后内联跑 maybe_update_preference_signal (纯 SQL 毫秒级, 失败不影响评分);
- 这是画像遥测不是工单: 客服流程仍在 /api/feedback。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import AnswerRating, UserPreference
from ..preference_signals import maybe_update_preference_signal
from ..profile_summary import EXPLANATION_STYLES, VERBOSITY_LEVELS

router = APIRouter(prefix="/api/profile/answer-ratings", tags=["profile"])

RATINGS = ("good", "bad")
REASONS = ("too_long", "too_short", "confusing", "off_topic")
_WEAK_POINTS_MAX = 5
_WEAK_POINT_CHARS = 64


class AnswerRatingIn(BaseModel):
    message_id: str | None = Field(default=None, max_length=64)
    rating: str = Field(min_length=1, max_length=8)
    reason: str | None = Field(default=None, max_length=16)
    # 前端从该消息的 profile trace (type=profile) 捕获的注入快照
    weak_points: list[str] = Field(default_factory=list, max_length=8)
    profile_used: bool = False


def _row_out(row: AnswerRating) -> dict:
    return {
        "id": str(row.id),
        "message_id": str(row.message_id) if row.message_id else None,
        "rating": row.rating,
        "reason": row.reason,
        "fingerprint": row.fingerprint or {},
        "created_at": row.created_at,
    }


@router.post("", status_code=201)
async def submit_answer_rating(
    body: AnswerRatingIn, ctx: AuthContext = Depends(api_rate_limited)
):
    if body.rating not in RATINGS:
        raise HTTPException(status_code=422, detail="INVALID_RATING")
    if body.reason is not None and body.reason not in REASONS:
        raise HTTPException(status_code=422, detail="INVALID_REASON")

    message_id: uuid.UUID | None = None
    if body.message_id:
        try:
            message_id = uuid.UUID(body.message_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="INVALID_MESSAGE_ID") from None

    weak_points = [w[:_WEAK_POINT_CHARS] for w in body.weak_points][:_WEAK_POINTS_MAX]
    async with tenant_session(ctx.tenant_id) as session:
        # 服务端补全风格指纹 (统计只需近似归属; 精确到次的快照成本不值得)
        pref_row = await session.get(UserPreference, ctx.user_id)
        fingerprint = {
            "explanation_style": (
                pref_row.explanation_style
                if pref_row and pref_row.explanation_style in EXPLANATION_STYLES
                else None
            ),
            "verbosity": (
                pref_row.verbosity
                if pref_row and pref_row.verbosity in VERBOSITY_LEVELS
                else None
            ),
            "weak_points": weak_points,
            "profile_used": bool(body.profile_used),
        }
        row: AnswerRating | None = None
        if message_id is not None:
            row = (
                await session.scalars(
                    select(AnswerRating).where(
                        AnswerRating.user_id == ctx.user_id,
                        AnswerRating.message_id == message_id,
                    )
                )
            ).first()
        if row is None:
            row = AnswerRating(
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                message_id=message_id,
                rating=body.rating,
                reason=body.reason,
                fingerprint=fingerprint,
            )
            session.add(row)
        else:
            row.rating = body.rating
            row.reason = body.reason
            row.fingerprint = fingerprint
        await session.flush()
        payload = _row_out(row)

    # 内联统计 (M3): 纯 SQL, 失败只记日志 — 评分已提交, 不因统计回滚
    await maybe_update_preference_signal(ctx.tenant_id, ctx.user_id)
    return payload
