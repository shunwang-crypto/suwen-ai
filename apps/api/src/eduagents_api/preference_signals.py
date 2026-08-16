"""M3 评价→画像闭环: 从回答评分统计讲解风格有效性, 产出待确认的建议。

设计约束 (与全仓哲学一致):
- 纯确定性统计 (零 LLM 调用、零新增 cron): 在 POST /api/profile/answer-ratings
  提交后内联运行, 两条 SELECT + 至多一条 INSERT/DELETE, 毫秒级;
- 建议以 learner_insights(kind=preference_signal, source=ai_inferred) 落库,
  只在设置页"AI 的建议"区展示; 用户点"采纳"才经 PUT /api/preferences 真正改
  偏好 — 推断永不直接改偏好 (用户显式声明 > 推断);
- 去重: 同用户 7 天内已有 preference_signal 且文本相同则不重复写;
- 上限: 每用户最多保留 3 条未删除的 preference_signal, 超出删最旧;
- 评分链路 (review_grade/quiz_grade) 不读本模块 — 评价只影响"怎么讲",
  不影响"判对错"。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from eduagents_core.logging import get_logger
from sqlalchemy import delete, select

from .db import tenant_session
from .models import AnswerRating, LearnerInsight, UserPreference
from .profile_summary import STYLE_LABELS

log = get_logger("preference_signals")

# 触发阈值: 近窗口内该风格的有效评价数 / "没听懂"次数 / 占比下限
_STATS_WINDOW = timedelta(days=30)
_MIN_RATINGS = 3
_MIN_CONFUSING = 2
_MIN_CONFUSING_RATE = 0.5
_SUGGESTION_COOLDOWN = timedelta(days=7)
_MAX_OPEN_SUGGESTIONS = 3
# 建议的替代风格优先序 (排除当前风格后取首个)
_ALTERNATES = ("rigorous", "colloquial", "analogy")


def _style_of(rating: AnswerRating) -> str | None:
    fp = rating.fingerprint if isinstance(rating.fingerprint, dict) else {}
    style = fp.get("explanation_style")
    return style if isinstance(style, str) and style in STYLE_LABELS else None


def build_suggestion_text(
    style: str, total: int, confusing: int, alternate: str
) -> str:
    """确定性建议文案 (纯函数, 单测锚点)。style/alternate 用中文标签。"""
    cur = STYLE_LABELS.get(style, style)
    alt = STYLE_LABELS.get(alternate, alternate)
    return (
        f"最近 {total} 次「{cur}」讲解中 {confusing} 次被标记\"没听懂\"，"
        f"建议尝试「{alt}」讲解（可在学习偏好中确认修改）。"
    )


def pick_alternate(style: str) -> str:
    for candidate in _ALTERNATES:
        if candidate != style:
            return candidate
    return "rigorous"


def _should_suggest(
    total: int, confusing: int
) -> bool:
    return (
        total >= _MIN_RATINGS
        and confusing >= _MIN_CONFUSING
        and confusing / total >= _MIN_CONFUSING_RATE
    )


async def maybe_update_preference_signal(
    tenant_id: uuid.UUID | str, user_id: uuid.UUID | str
) -> str:
    """评分提交后的内联统计。返回状态串: no_signal/cooldown/written。

    任何异常只记日志不抛 — 评价主链路 (INSERT 已提交) 不因统计失败回滚。
    """
    uid = uuid.UUID(str(user_id))
    try:
        return await _run(str(tenant_id), uid)
    except Exception:  # noqa: BLE001 — 统计失败不影响已落库的评分
        log.exception("preference_signal_update_failed", user_id=str(uid))
        return "error"


async def _run(tenant_id: str, uid: uuid.UUID) -> str:
    now = datetime.now(UTC)
    window_start = now - _STATS_WINDOW
    async with tenant_session(tenant_id) as session:
        pref_row = await session.get(UserPreference, uid)
        current_style = pref_row.explanation_style if pref_row else None

        ratings = (
            await session.scalars(
                select(AnswerRating)
                .where(
                    AnswerRating.user_id == uid,
                    AnswerRating.created_at >= window_start,
                )
                .order_by(AnswerRating.created_at.desc())
                .limit(200)
            )
        ).all()

        # 只统计带风格指纹的 bad 评价与 good 评价, 按风格分组
        per_style: dict[str, dict[str, int]] = {}
        for r in ratings:
            style = _style_of(r)
            if style is None:
                continue
            bucket = per_style.setdefault(style, {"total": 0, "confusing": 0})
            bucket["total"] += 1
            if r.rating == "bad" and r.reason == "confusing":
                bucket["confusing"] += 1

        target_style: str | None = None
        confusing = 0
        total = 0
        # 当前风格优先 (最可行动); 无当前风格时取评价数最多的一档
        candidates = sorted(
            per_style.items(), key=lambda kv: (-kv[1]["total"], kv[0])
        )
        ordered = [current_style] if current_style in per_style else []
        ordered += [s for s, _ in candidates if s != current_style]
        for style in ordered:
            stats = per_style[style]
            if _should_suggest(stats["total"], stats["confusing"]):
                target_style = style
                confusing = stats["confusing"]
                total = stats["total"]
                break
        if target_style is None:
            return "no_signal"

        text = build_suggestion_text(
            target_style, total, confusing, pick_alternate(target_style)
        )

        # 去重: 7 天内已有同文本建议 → 冷却中
        cooldown_start = now - _SUGGESTION_COOLDOWN
        recent = (
            await session.scalars(
                select(LearnerInsight).where(
                    LearnerInsight.user_id == uid,
                    LearnerInsight.kind == "preference_signal",
                    LearnerInsight.created_at >= cooldown_start,
                )
            )
        ).all()
        if any(r.text == text for r in recent):
            return "cooldown"

        session.add(
            LearnerInsight(
                tenant_id=uuid.UUID(tenant_id),
                user_id=uid,
                kc_id=None,
                source="ai_inferred",
                kind="preference_signal",
                text=text,
                session_id=None,
            )
        )
        # 上限裁剪: 未删除的 preference_signal 只保留最近 3 条
        open_rows = (
            await session.scalars(
                select(LearnerInsight)
                .where(
                    LearnerInsight.user_id == uid,
                    LearnerInsight.kind == "preference_signal",
                )
                .order_by(LearnerInsight.created_at.desc(), LearnerInsight.id.desc())
            )
        ).all()
        for stale in open_rows[_MAX_OPEN_SUGGESTIONS - 1 :]:
            await session.execute(
                delete(LearnerInsight).where(LearnerInsight.id == stale.id)
            )
        log.info(
            "preference_signal_written",
            user_id=str(uid),
            style=target_style,
            confusing=confusing,
            total=total,
        )
        return "written"
