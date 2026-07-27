"""F8-A2 主动开场 + G13 可解释推荐 (Wave-P2 P2-2/P2-5)。

GET /api/next-actions: 确定性聚合 (零 LLM), 每一条推荐都能回答"为什么推它":
- review: FSRS 今日到期错题数 —— 口径 = reviews.today_queue (活跃态 + is_due,
  含 §9 虚拟时钟, 空卡视为到期), 常量直接 import reviews 保持单一出处;
- resume_insight (F8-A2 核心): learner_insights 最近的 confused/misconception 条目,
  "上次你在『X』卡住了" —— confused 是对话反思里的学生原话, 链回 /chat?q= 续问;
  misconception 是辅导小结记录的易错点, 链到 /tutor 换题重练;
- weak_practice: 复用 profile_summary.weak_point_kcs 的 top 薄弱 KC, 理由沿用
  difficulty_rationale 的 zone/reason 定性词 (P2-7), 链到 /quiz?weak=1 定向出题卡;
- plan_task: 活跃计划 (latest_plan) 截至今天的未完成任务数, 链到 /plans;
- 冷启动兜底: 四类全空时给「摸底测评」「制定计划」两条 (话术对齐 reports._build_actions)。

优先级 review > resume_insight > weak_practice > plan_task, 共 ≤5 条。
脱敏口径 (P2-7): 理由只用定性词 (zone_label 两档等), 不带掌握度小数;
resume_insight 的学生原话只经本鉴权接口回给本人, 续问 q 也只进其本人的 prompt。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from .. import timectl
from ..db import tenant_session
from ..deps import AuthContext, api_rate_limited
from ..models import KnowledgeComponent, LearnerInsight
from ..plan_adjust import iter_tasks, latest_plan
from ..profile_summary import weak_point_kcs

# B3 到期数同源: 与 /api/reviews/today 的 total_due 调同一个函数 (唯一出处),
# 开场卡与复习页的"到期数"不再各查各的 (此前 5 vs 37 打架)
from .reviews import due_error_items

router = APIRouter(prefix="/api/next-actions", tags=["next-actions"])

MAX_ITEMS = 5  # 开场卡最多 5 条 (再多就不是"下一步"而是清单)
_WEAK_TOP = 2  # weak_practice 最多取 2 个薄弱 KC (review/resume/plan 各 1 时恰好凑满 5)
_RESUME_KINDS = ("confused", "misconception")  # understood 不是卡点, 不进开场卡
_RESUME_QUOTE_MAX = 60  # 理由里引用的学生原话截断长度
_RESUME_Q_MAX = 200  # /chat?q= 续问里带的原话截断长度 (进本人 prompt, 不出境他人)
_TITLE_LABEL_MAX = 12  # 无 KC 名时标题里原话片段的截断长度
_GOAL_MAX = 20  # plan_task 理由里计划目标的截断长度


def _ellipsis(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plan_today_snapshot(
    goal: str, created: date, structure: dict | None, today: date
) -> dict | None:
    """活跃计划"截至今天"的未完成任务快照; 无未完成任务返回 None (不推)。

    第 N 天 = 自创建日起算 (创建当天为第 1 天); 任务的日程号取 day.day (int),
    缺失时保守视为已到期 (宁可多提醒, 不静默漏任务)。全部完成 -> 不打扰。
    """
    day_no = max(1, (today - created).days + 1)
    undone = 0
    for _si, _di, _ti, _stage, day, task in iter_tasks(structure):
        day_number = day.get("day")
        due = not isinstance(day_number, int) or day_number <= day_no
        if due and not task.get("done"):
            undone += 1
    if undone == 0:
        return None
    return {"goal": goal, "day_no": day_no, "undone": undone}


def _resume_item(insight: dict) -> dict:
    """resume_insight 条目: 标题点名卡点, 理由引用记忆出处, 链接按 kind 分流。

    confused = 对话反思存的学生原话 -> /chat?q= 带原话续问 (chat 页 ?q= 自动发问,
    点击即"继续上次的疑问"); misconception = 辅导小结的易错点描述 -> /tutor 重练。
    原话是用户自己的话, 只进其本人的 prompt (P2-7 口径允许)。
    """
    kc_name = (insight.get("kc_name") or "").strip()
    kind = insight.get("kind") or ""
    text = (insight.get("text") or "").strip()
    label = kc_name or _ellipsis(text, _TITLE_LABEL_MAX)
    if kind == "misconception":
        reason = f"辅导小结记录过这个易错点：“{_ellipsis(text, _RESUME_QUOTE_MAX)}”"
        href = "/tutor"
    else:
        reason = f"长期记忆里记着你当时的原话：“{_ellipsis(text, _RESUME_QUOTE_MAX)}”"
        topic = f"学「{kc_name}」时" if kc_name else ""
        question = f"上次{topic}我有个疑问：{_ellipsis(text, _RESUME_Q_MAX)} —— 请接着帮我讲清楚。"
        href = "/chat?q=" + quote(question)
    return {
        "type": "resume_insight",
        "title": f"上次你在『{label}』卡住了，要继续吗",
        "reason": reason,
        "href": href,
    }


def compose_next_actions(
    *,
    due_count: int,
    insight: dict | None,
    weak_points: list[dict],
    plan: dict | None,
) -> list[dict]:
    """确定性拼装推荐条目 (纯函数, 单测入口): 优先级排布 + ≤5 条 + 冷启动兜底。

    入参均为已加载的快照: due_count 到期错题数; insight 最近卡点条目
    {kc_name, kind, text}; weak_points 为 weak_point_kcs 输出 (理由用其 reason/zone
    定性字段, 不触碰 p_mastery 小数); plan 为 plan_today_snapshot 输出。
    """
    items: list[dict] = []
    if due_count > 0:
        items.append(
            {
                "type": "review",
                "title": f"{due_count} 道错题到期了",
                "reason": "FSRS 间隔复习排程：这些错题今天到了最佳复习时点，现在重练巩固效果最好",
                "href": "/reviews",
            }
        )
    if insight is not None and (insight.get("text") or "").strip():
        items.append(_resume_item(insight))
    for wp in weak_points[:_WEAK_TOP]:
        # reason 沿用 difficulty_rationale 的定性话术 (zone 两档词, 无掌握度小数)
        reason = wp.get("reason") or f"掌握度{wp.get('zone', '待巩固')}，定向练习最见效"
        items.append(
            {
                "type": "weak_practice",
                "title": f"练一练薄弱点『{wp.get('name', '')}』",
                "reason": reason,
                "href": "/quiz?weak=1",
            }
        )
    if plan is not None:
        items.append(
            {
                "type": "plan_task",
                "title": f"继续学习计划：{plan['undone']} 个任务待完成",
                "reason": (
                    f"计划《{_ellipsis(plan['goal'], _GOAL_MAX)}》进行到第 {plan['day_no']} 天，"
                    f"按日程还有 {plan['undone']} 个任务没完成"
                ),
                "href": "/plans",
            }
        )
    if not items:
        # 冷启动: 无任何学习记录时的两条兜底 (话术对齐 reports._build_actions)
        items = [
            {
                "type": "quiz",
                "title": "完成 5-8 题摸底测评",
                "reason": "还没有学习记录：先摸底建立掌握度画像，之后的推荐才有依据",
                "href": "/quiz",
            },
            {
                "type": "plan",
                "title": "制定 1 份学习计划",
                "reason": "含摸底诊断：把目标拆成每天可执行的任务，学起来不迷路",
                "href": "/plans",
            },
        ]
    return items[:MAX_ITEMS]


async def _load_snapshots(
    ctx: AuthContext, now_dt: datetime
) -> tuple[int, dict | None, dict | None]:
    """一次事务读齐 (due_count, 最近卡点 insight, 活跃计划快照)。"""
    async with tenant_session(ctx.tenant_id) as session:
        # 到期数 = len(全量到期集合), 与 reviews.today 的 total_due 同一函数 (B3)
        due_count = len(await due_error_items(session, ctx.user_id, now_dt))

        insight_row = (
            await session.execute(
                select(LearnerInsight, KnowledgeComponent.canonical_name)
                .outerjoin(KnowledgeComponent, KnowledgeComponent.id == LearnerInsight.kc_id)
                .where(
                    LearnerInsight.user_id == ctx.user_id,
                    LearnerInsight.kind.in_(_RESUME_KINDS),
                )
                .order_by(LearnerInsight.created_at.desc(), LearnerInsight.id.desc())
                .limit(1)
            )
        ).first()
        insight = None
        if insight_row is not None:
            row, kc_name = insight_row
            insight = {"kc_name": kc_name or "", "kind": row.kind, "text": row.text}

        plan_row = await latest_plan(session, uuid.UUID(str(ctx.user_id)))
        plan = (
            plan_today_snapshot(
                plan_row.goal,
                plan_row.created_at.date(),
                plan_row.structure_json,
                now_dt.date(),
            )
            if plan_row is not None
            else None
        )
    return due_count, insight, plan


@router.get("")
async def next_actions(request: Request, ctx: AuthContext = Depends(api_rate_limited)):
    """可解释的"下一步"推荐 (G13): 全部确定性聚合, 逐条自带"为什么"。"""
    now_dt = await timectl.now(request.app.state.redis)
    due_count, insight, plan = await _load_snapshots(ctx, now_dt)
    # 薄弱点走既有共用函数 (自开事务); kb 不限定 —— 开场卡是全局视角
    weak = await weak_point_kcs(ctx.tenant_id, ctx.user_id, [])
    items = compose_next_actions(
        due_count=due_count, insight=insight, weak_points=weak, plan=plan
    )
    return {"items": items}
