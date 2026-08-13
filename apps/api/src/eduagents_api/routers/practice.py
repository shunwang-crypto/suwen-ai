"""Student-facing next-practice recommendation API."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..practice_recommendation import (
    PracticeAdjustmentUnavailable,
    PracticeRecommendation,
    recommend_practice,
)

router = APIRouter(prefix="/api/practice", tags=["practice"])


class PracticeRequest(BaseModel):
    """Only server-verifiable context is accepted from the browser."""

    model_config = ConfigDict(extra="forbid")

    scope_kb_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    task_id: str | None = None
    previous_quiz_id: uuid.UUID | None = None
    adjustment: Literal["easier", "harder", "switch_kc", "switch_kb"] | None = None


def _gateway_mode(request: Request) -> str:
    settings = getattr(request.app.state, "settings", None)
    mode = getattr(settings, "gateway_mode", "")
    if isinstance(mode, str) and mode:
        return mode
    from eduagents_core.config import get_settings

    return get_settings().gateway_mode


async def load_recommendation(
    request: Request, ctx: AuthContext, body: PracticeRequest | None = None
) -> PracticeRecommendation:
    body = body or PracticeRequest()
    now_dt = await timectl.now(request.app.state.redis)
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        try:
            return await recommend_practice(
                session,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                gateway_mode=_gateway_mode(request),
                org_ids=ctx.org_ids,
                now_dt=now_dt,
                scope_kb_id=body.scope_kb_id,
                plan_id=body.plan_id,
                task_id=body.task_id,
                previous_quiz_id=body.previous_quiz_id,
                adjustment=body.adjustment,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except PracticeAdjustmentUnavailable:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/next")
async def next_practice(
    request: Request, ctx: AuthContext = Depends(api_rate_limited)
):
    recommendation = await load_recommendation(request, ctx)
    return recommendation.to_dict()
