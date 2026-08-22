"""F8 "AI 眼中的我": 可审计的画像与长期记忆读侧。

- GET /api/profile: 掌握度全量 (join KC 名, 附 FSRS 保持率快照 + kb/章节元信息,
  ?kb_id= 单库过滤 — 真实规模 152 KC 需折叠成 库→章→KC 树, B1) + 错题分布
  + 最近 10 条**有效**掌握度变化流水 (F8-A3; |Δp|<0.01 的零变化行默认过滤,
  ?include_noop=1 保留全量供审计口径, C1)。
- GET /api/profile/weak-points: 定向出题将要覆盖的薄弱点预览 (与 POST /api/quiz
  的 from_weak_points 走同一函数, 预览即所得)。
- GET/DELETE /api/profile/insights (P2-1): 长期记忆条目 (学生原话/误解语境)
  用户可见可删 — F8 规格"记忆条目"的最终解释权同样归用户。
- PATCH /api/profile/insights/{id} (0031): 软抑制开关 ("不要再提及") —
  保留原文只停注入, 与 DELETE (物理删除) 互补。
- GET /api/profile/memory/preview (0031): "AI 眼中的我" — 复用问答同一
  build_profile_context, 让用户看到注入 prompt 的真实摘要 (透明度)。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from eduagents_core.mastery import scheduler
from eduagents_core.mastery.thresholds import (
    QUIZ_WEAK_THRESHOLD,
    VALID_MASTERY_EVIDENCE_TYPES,
    evidence_state,
    evidence_state_label,
    evidence_strength,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited, get_redis
from ..kc_hierarchy import resolve_kc_meta
from ..models import (
    ErrorItem,
    KnowledgeComponent,
    LearnerInsight,
    MasteryLog,
    MasteryRecord,
)
from ..profile_summary import QUIZ_WEAK_TOP, build_profile_context, weak_point_kcs

router = APIRouter(prefix="/api/profile", tags=["profile"])

_RECENT_LOG_LIMIT = 10
# C1: 过滤零变化行后仍要凑满 10 条有效流水, 多取一些再截断 (单用户流水查询极轻)
_LOG_SCAN_LIMIT = 50
# BKT 极小步进 (高掌握度下证据只挪 p 万分位) 在 UI 上是"0.82→0.82"噪声行
NOOP_EPS = 0.01
_INSIGHTS_LIMIT = 50  # 记忆条目读取上限 (库内本就每用户 ≤100, 见 services)
# 遗忘快照 (P2-7) 的第二个时刻点: 现在 + 3 天 ("再不复习, 3 天后…" 的产品口径)
_RETENTION_HORIZON = timedelta(days=3)

# evidence_strength 唯一实现在 eduagents_core.mastery.thresholds (前后端同源),
# 此处再导出保持既有 import 路径可用
__all__ = ["evidence_strength", "router"]


def filter_noop_changes(logs: list, *, include_noop: bool, limit: int = _RECENT_LOG_LIMIT) -> list:
    """C1: 掌握度流水去噪 (纯函数, 单测入口)。

    默认丢弃 |new_p-old_p| < NOOP_EPS 的零变化行 (真实规模账号里这类行淹没了
    有效变化); include_noop=True 保留全量 (审计口径: 流水与 mastery_log 可逐行核对)。
    两种口径都在过滤后截前 limit 条 (入参 logs 已按 at 降序)。
    """
    if include_noop:
        return list(logs)[:limit]
    return [x for x in logs if abs(x.new_p - x.old_p) >= NOOP_EPS][:limit]


def _kc_retention_snapshots(
    err_rows: list, now_dt: datetime, at_3d: datetime
) -> dict[str, tuple[float, float]]:
    """按 KC 聚合 FSRS 保持率快照 (P2-7): kc_id -> (retention_now, retention_in_3d)。

    err_rows 为 (kc_ids, fsrs_card_json) 元组; 同一 KC 挂多张卡时取
    retention_now 最低的那张 (最危险的记忆), 两个时刻点取同一张卡保证自洽。
    从未复习过的卡 predicted_retention 返回 None, 此处跳过 (无预测依据)。
    """
    result: dict[str, tuple[float, float]] = {}
    for kc_ids, card_json in err_rows:
        r_now = scheduler.predicted_retention(card_json, now_dt)
        if r_now is None:
            continue
        r_3d = scheduler.predicted_retention(card_json, at_3d)
        for kc_id in kc_ids or []:
            key = str(kc_id)
            prev = result.get(key)
            if prev is None or r_now < prev[0]:
                result[key] = (r_now, r_3d)
    return result


@router.get("")
async def get_profile(
    request: Request,
    kb_id: uuid.UUID | None = None,
    include_noop: bool = False,
    ctx: AuthContext = Depends(api_rate_limited),
):
    # 业务时间经虚拟时钟 (§9): 保持率快照随 time-travel 前进而衰减, 可现场演示
    now_dt = await timectl.now(request.app.state.redis)
    at_3d = now_dt + _RETENTION_HORIZON
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        # 遗忘快照 (P2-7): 该用户全部未归档错题的 FSRS 卡, 按 KC 聚合两点保持率
        # (archived 是用户显式退出复习, 不再对其渲染"再不复习"提示)
        err_rows = (
            await session.execute(
                select(ErrorItem.kc_ids, ErrorItem.fsrs_card_json).where(
                    ErrorItem.user_id == ctx.user_id,
                    ErrorItem.state != "archived",
                    ErrorItem.verification_status == "verified",
                )
            )
        ).all()
        retention_by_kc = _kc_retention_snapshots(err_rows, now_dt, at_3d)

        # 掌握度全量 (join KC 名)
        mastery_rows = (
            await session.execute(
                select(
                    MasteryRecord,
                    KnowledgeComponent.canonical_name,
                    func.count(func.distinct(MasteryLog.evidence_id)),
                )
                .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
                .outerjoin(
                    MasteryLog,
                    (MasteryLog.user_id == MasteryRecord.user_id)
                    & (MasteryLog.kc_id == MasteryRecord.kc_id)
                    & MasteryLog.evidence_type.in_(VALID_MASTERY_EVIDENCE_TYPES),
                )
                .where(MasteryRecord.user_id == ctx.user_id)
                .group_by(MasteryRecord, KnowledgeComponent.canonical_name)
                .order_by(MasteryRecord.p_mastery.asc())
            )
        ).all()
        mastery = []
        for r, name, evidence_count in mastery_rows:
            retention = retention_by_kc.get(str(r.kc_id))
            state = evidence_state(int(evidence_count))
            mastery.append(
                {
                    "kc_id": str(r.kc_id),
                    "name": name,
                    "p_mastery": None if state == "unobserved" else round(r.p_mastery, 4),
                    "confidence": round(r.confidence, 4),
                    "evidence_strength": evidence_strength(r.confidence),
                    "evidence_state": state,
                    "evidence_state_label": evidence_state_label(state),
                    "last_seen": r.last_seen,
                    # 无 FSRS 卡 (没错过题 / 卡未复习过) 的条目为 null, 前端不渲染
                    "retention_now": round(retention[0], 4) if retention else None,
                    "retention_in_3d": round(retention[1], 4) if retention else None,
                }
            )

        # 错题分布: 按错因五分类 / 按状态机各计数
        by_cause = {
            cause: int(n)
            for cause, n in (
                await session.execute(
                    select(ErrorItem.cause, func.count())
                    .where(
                        ErrorItem.user_id == ctx.user_id,
                        ErrorItem.verification_status == "verified",
                    )
                    .group_by(ErrorItem.cause)
                )
            ).all()
        }
        by_state = {
            state: int(n)
            for state, n in (
                await session.execute(
                    select(ErrorItem.state, func.count())
                    .where(
                        ErrorItem.user_id == ctx.user_id,
                        ErrorItem.verification_status == "verified",
                    )
                    .group_by(ErrorItem.state)
                )
            ).all()
        }

        # 最近 10 条掌握度变化流水 (F8-A3: 因 X 事件, Y 从 old→new)。
        # C1: 多取 50 条再过滤零变化行, 保证去噪后仍有 10 条有效变化可看
        raw_logs = (
            await session.scalars(
                select(MasteryLog)
                .where(MasteryLog.user_id == ctx.user_id)
                .order_by(MasteryLog.at.desc())
                .limit(_LOG_SCAN_LIMIT)
            )
        ).all()
        logs = filter_noop_changes(raw_logs, include_noop=include_noop)
        kc_names: dict[str, str] = {
            str(r.kc_id): name for r, name, _count in mastery_rows
        }
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

    # B1: kb/章节元信息 (kc_hierarchy 确定性推导 + 5 分钟缓存), 前端折叠 库→章→KC 树
    meta = await resolve_kc_meta(
        ctx.tenant_id,
        [m["kc_id"] for m in mastery],
        redis=request.app.state.redis,
        org_ids=ctx.org_ids,
    )
    for m in mastery:
        info = meta.get(m["kc_id"]) or {}
        m["kb_id"] = info.get("kb_id")
        m["kb_name"] = info.get("kb_name") or ""
        m["subject"] = info.get("subject") or ""
        m["chapter"] = info.get("chapter") or ""
    if kb_id is not None:
        mastery = [m for m in mastery if m["kb_id"] == str(kb_id)]

    return {
        "mastery": mastery,
        "weak_threshold": QUIZ_WEAK_THRESHOLD,
        # kb_id 过滤只作用于 mastery (错题分布/流水是全局学习行为, 不随库切换清零)
        "kb_id": str(kb_id) if kb_id is not None else None,
        "error_distribution": {"by_cause": by_cause, "by_state": by_state},
        "recent_changes": recent_changes,
        "include_noop": include_noop,
    }


@router.get("/weak-points")
async def get_weak_points(
    kb_id: uuid.UUID | None = None, ctx: AuthContext = Depends(api_rate_limited)
):
    """薄弱点预览 (M4-D2): 前端"针对我的薄弱点出题"按钮下方展示将要练的 KC 名。

    与 POST /api/quiz (from_weak_points=true) 共用 weak_point_kcs, 预览与实际一致;
    返回空列表即"数据不足", 前端据此禁用按钮。
    """
    items = await weak_point_kcs(
        ctx.tenant_id,
        ctx.user_id,
        [kb_id] if kb_id else [],
        org_ids=ctx.org_ids,
    )
    return {
        "threshold": QUIZ_WEAK_THRESHOLD,
        "limit": QUIZ_WEAK_TOP,
        "weak_points": items,
    }


@router.get("/insights")
async def list_insights(ctx: AuthContext = Depends(api_rate_limited)):
    """长期记忆条目 (P2-1): 最近 50 条, join KC 名 (kc_id 可空故 outerjoin)。

    text 是学生自己的原话/误解描述, 只回给本人 (RLS 限租户 + user_id 限本人),
    是 F8-A2"上次你在 X 卡住"开场卡的数据源。
    """
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        rows = (
            await session.execute(
                select(LearnerInsight, KnowledgeComponent.canonical_name)
                .outerjoin(
                    KnowledgeComponent, KnowledgeComponent.id == LearnerInsight.kc_id
                )
                .where(LearnerInsight.user_id == ctx.user_id)
                .order_by(LearnerInsight.created_at.desc(), LearnerInsight.id.desc())
                .limit(_INSIGHTS_LIMIT)
            )
        ).all()
    return {
        "insights": [
            {
                "id": str(r.id),
                "kc_id": str(r.kc_id) if r.kc_id else None,
                "kc_name": name or "",
                "source": r.source,
                "kind": r.kind,
                "text": r.text,
                "session_id": str(r.session_id) if r.session_id else None,
                "suppressed": bool(r.suppressed),
                "created_at": r.created_at,
            }
            for r, name in rows
        ]
    }


class InsightPatch(BaseModel):
    """软抑制开关 (0031): True = "不要再提及" (保留原文, 注入侧过滤)。"""

    suppressed: bool


@router.patch("/insights/{insight_id}")
async def patch_insight(
    insight_id: uuid.UUID,
    body: InsightPatch,
    ctx: AuthContext = Depends(api_rate_limited),
):
    """切换一条记忆条目的软抑制 (用户纠正权; 与 DELETE 的物理删除互补)。"""
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await session.get(LearnerInsight, insight_id)
        if row is None or row.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="INSIGHT_NOT_FOUND")
        row.suppressed = body.suppressed
        await session.flush()
        return {
            "id": str(row.id),
            "suppressed": bool(row.suppressed),
        }


@router.get("/memory/preview")
async def memory_preview(
    ctx: AuthContext = Depends(api_rate_limited), redis=Depends(get_redis)
):
    """记忆摘要预览 (0031): 与问答注入同源同缓存 — 用户看到的就是 AI 看到的。"""
    pref_ctx = await build_profile_context(
        ctx.tenant_id,
        ctx.user_id,
        redis=redis,
        role="qa",
        org_ids=ctx.org_ids,
    )
    return {
        "summary": pref_ctx.summary,
        "weak_points": pref_ctx.weak_points,
        "mastered": pref_ctx.mastered,
    }


@router.delete("/insights/{insight_id}", status_code=204)
async def delete_insight(
    insight_id: uuid.UUID, ctx: AuthContext = Depends(api_rate_limited)
):
    """删除一条记忆条目 (用户纠正权: 本人删本人)。

    RLS 已限租户; 再显式核对 user_id (个人表 tenant_id 即 user_id, 双保险),
    不存在或不属于本人一律 404, 不泄露他人条目存在性。
    """
    async with tenant_session(ctx.tenant_id, org_ids=ctx.org_ids) as session:
        row = await session.get(LearnerInsight, insight_id)
        if row is None or row.user_id != ctx.user_id:
            raise HTTPException(status_code=404, detail="INSIGHT_NOT_FOUND")
        await session.delete(row)
