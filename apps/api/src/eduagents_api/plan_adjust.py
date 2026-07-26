"""F3 "计划是活的": 任务勾选态 + 依据学情自动插入补强任务 (纯逻辑, 供路由与 services 共用)。

决策 D-F3-1 (任务完成态存哪): **内嵌进 structure_json 任务对象的 done 字段**, 不新增
plan_progress 列。理由:
1. 计划结构本身就是任务的唯一权威载体 —— 另起一列会产生"计划被调整插入了新任务、
   进度列却按旧下标记账"的二源不一致;
2. 不动 schema 即不需要 alembic 迁移, 与并行开发的其他模块零冲突;
3. 代价只是每次勾选整块回写 JSONB —— 计划上限 90 天 × 数个任务, 体量以 KB 计, 可忽略。

task_key 形如 "stage0.day3.task1": 三段分别是 stages / days / tasks 三层数组的
**0 基下标** (不是 day 字段的天编号)。key 由服务端读取时生成并随任务下发,
前端只回传不自造; 越界/不匹配一律 404, 不做模糊定位。

注: JSONB 列未挂 MutableDict, 原地改 dict 不会被 SQLAlchemy 标记为脏 —— 本模块
一律深拷贝后整体重新赋值 (structure_json = new_structure)。
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any

from eduagents_core.kc.types import normalize_kc_name
from eduagents_core.logging import get_logger
from eduagents_core.mastery.thresholds import REINFORCE_THRESHOLD
from sqlalchemy import select

from .models import KnowledgeComponent, MasteryRecord, Plan, PlanAdjustment

log = get_logger("plan_adjust")

TASK_KEY_RE = re.compile(r"^stage(\d+)\.day(\d+)\.task(\d+)$")

# 薄弱判据 = REINFORCE_THRESHOLD (掌握度阈值唯一来源 eduagents_core.mastery.thresholds)
_REINFORCE_PCT = round(REINFORCE_THRESHOLD * 100)  # 中文理由里的百分数表述
LOW_SCORE_THRESHOLD = 0.6  # 单次测评正确率 < 0.6 视为低分, 触发自动调整 (非掌握度阈值)
REINFORCE_MINUTES = 30  # 补强任务时长 (固定, 与 F3-A1 日预算校验解耦)
MAX_INSERTS_PER_ADJUST = 5  # 单次调整最多插入的补强任务数 (防一次灌满计划)


def make_task_key(stage_idx: int, day_idx: int, task_idx: int) -> str:
    return f"stage{stage_idx}.day{day_idx}.task{task_idx}"


def parse_task_key(task_key: str) -> tuple[int, int, int] | None:
    match = TASK_KEY_RE.match(task_key or "")
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _stages(structure: dict | None) -> list:
    stages = (structure or {}).get("stages")
    return stages if isinstance(stages, list) else []


def iter_tasks(structure: dict | None):
    """遍历 (stage_idx, day_idx, task_idx, stage, day, task); 非法层级静默跳过。"""
    for si, stage in enumerate(_stages(structure)):
        if not isinstance(stage, dict):
            continue
        days = stage.get("days")
        for di, day in enumerate(days if isinstance(days, list) else []):
            if not isinstance(day, dict):
                continue
            tasks = day.get("tasks")
            for ti, task in enumerate(tasks if isinstance(tasks, list) else []):
                if isinstance(task, dict):
                    yield si, di, ti, stage, day, task


def enrich_structure(structure: dict | None) -> dict:
    """读取侧加工: 每个任务补上 task_key 与 done (缺省 False), 返回可直接 JSON 化的副本。"""
    out = copy.deepcopy(structure or {})
    if not isinstance(out, dict):
        return {"stages": []}
    for si, di, ti, _stage, _day, task in iter_tasks(out):
        task["task_key"] = make_task_key(si, di, ti)
        task["done"] = bool(task.get("done"))
    out.setdefault("stages", [])
    return out


def plan_progress(structure: dict | None) -> dict:
    """{total, done, ratio}: 任务粒度的完成进度。"""
    total = 0
    done = 0
    for *_idx, task in iter_tasks(structure):
        total += 1
        if task.get("done"):
            done += 1
    return {"total": total, "done": done, "ratio": round(done / total, 4) if total else 0.0}


def set_task_done(structure: dict | None, task_key: str, done: bool | None) -> tuple[dict, bool]:
    """返回 (新 structure 副本, 该任务最终 done)。task_key 不存在抛 KeyError。

    done=None 表示取反 (前端只发切换意图时用)。
    """
    parsed = parse_task_key(task_key)
    if parsed is None:
        raise KeyError(task_key)
    si, di, ti = parsed
    new_structure = copy.deepcopy(structure or {})
    for csi, cdi, cti, _stage, _day, task in iter_tasks(new_structure):
        if (csi, cdi, cti) == (si, di, ti):
            final = (not bool(task.get("done"))) if done is None else bool(done)
            task["done"] = final
            return new_structure, final
    raise KeyError(task_key)


def _existing_kc_keys(structure: dict | None) -> set[str]:
    """计划里已被任务覆盖的 KC 名 (归一化)。"""
    keys: set[str] = set()
    for *_idx, task in iter_tasks(structure):
        for name in task.get("kc_names") or []:
            if isinstance(name, str) and name.strip():
                keys.add(normalize_kc_name(name))
    return keys


def _pending_day_slots(structure: dict | None) -> list[tuple[int, int]]:
    """未完成日 (有未勾选任务或空任务的日) 的 (stage_idx, day_idx), 按时间先后。

    全部日程都已完成时退回最后一日 —— 补强任务宁可挤在最后一天, 也不能凭空丢弃。
    """
    pending: list[tuple[int, int]] = []
    last: tuple[int, int] | None = None
    for si, stage in enumerate(_stages(structure)):
        if not isinstance(stage, dict):
            continue
        days = stage.get("days")
        for di, day in enumerate(days if isinstance(days, list) else []):
            if not isinstance(day, dict):
                continue
            last = (si, di)
            tasks = day.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            if not tasks or any(isinstance(t, dict) and not t.get("done") for t in tasks):
                pending.append((si, di))
    if pending:
        return pending
    return [last] if last is not None else []


def _day_number(structure: dict, stage_idx: int, day_idx: int) -> int:
    day = _stages(structure)[stage_idx]["days"][day_idx]
    value = day.get("day")
    return int(value) if isinstance(value, int) else day_idx + 1


async def _weak_kcs(session, user_id: uuid.UUID) -> list[tuple[str, str, float]]:
    """(kc_id, kc_name, p_mastery) 列表, p_mastery < 阈值, 由弱到强。"""
    rows = (
        await session.execute(
            select(
                MasteryRecord.kc_id,
                KnowledgeComponent.canonical_name,
                MasteryRecord.p_mastery,
            )
            .join(KnowledgeComponent, KnowledgeComponent.id == MasteryRecord.kc_id)
            .where(
                MasteryRecord.user_id == user_id,
                MasteryRecord.p_mastery < REINFORCE_THRESHOLD,
            )
            .order_by(MasteryRecord.p_mastery.asc())
        )
    ).all()
    return [(str(kc_id), name, float(p)) for kc_id, name, p in rows]


async def latest_plan(session, user_id: uuid.UUID) -> Plan | None:
    """当前活跃计划 = 该用户最近创建的一个 (M4 暂无 status 列, 以最新为准)。"""
    return await session.scalar(
        select(Plan).where(Plan.user_id == user_id).order_by(Plan.created_at.desc()).limit(1)
    )


async def adjust_plan(
    session,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    plan: Plan,
    *,
    trigger: str = "manual",
    reason_prefix: str = "",
) -> dict:
    """依据当前学情调整计划: 薄弱 KC 无对应任务 → 在最近未完成日插入补强任务。

    - 薄弱判据: mastery_records.p_mastery < REINFORCE_THRESHOLD (由弱到强, 最多插 5 个);
    - 已覆盖判据: 计划中任一任务的 kc_names 归一化后命中该 KC 名 → 跳过 (不重复插);
    - 落点: 未完成日按时间先后轮转分配 (避免全部堆在同一天); 计划已全部完成则落最后一日;
    - 有插入才写 plan_adjustments (无插入不是一次调整, 不留噪声审计)。

    调用方负责事务 (session 由外部 tenant_session 提供)。
    """
    structure = plan.structure_json or {}
    weak = await _weak_kcs(session, user_id)
    covered = _existing_kc_keys(structure)
    candidates = [
        (kc_id, name, p) for kc_id, name, p in weak if normalize_kc_name(name) not in covered
    ][:MAX_INSERTS_PER_ADJUST]

    if not candidates:
        reason = (
            f"当前没有掌握度低于 {_REINFORCE_PCT}% 且计划未覆盖的知识点, 计划无需调整。"
            if weak
            else f"当前没有掌握度低于 {_REINFORCE_PCT}% 的知识点, 计划无需调整。"
        )
        return {"inserted": [], "reason": reason_prefix + reason, "adjustment_id": None}

    new_structure = copy.deepcopy(structure)
    slots = _pending_day_slots(new_structure)
    if not slots:  # 空计划 (无任何日) → 无处可插, 如实返回
        return {
            "inserted": [],
            "reason": reason_prefix + "计划中没有可插入的日程, 未做调整。",
            "adjustment_id": None,
        }

    inserted: list[dict[str, Any]] = []
    for i, (kc_id, name, p) in enumerate(candidates):
        stage_idx, day_idx = slots[i % len(slots)]
        tasks = new_structure["stages"][stage_idx]["days"][day_idx].setdefault("tasks", [])
        task = {
            "title": f"补强: {name}",
            "minutes": REINFORCE_MINUTES,
            "kc_names": [name],
            "resource_ref": None,
            "done": False,
            "source": "adjust",  # 前端据此高亮"本次插入"
        }
        tasks.append(task)
        inserted.append(
            {
                "task_key": make_task_key(stage_idx, day_idx, len(tasks) - 1),
                "day": _day_number(new_structure, stage_idx, day_idx),
                "stage_name": new_structure["stages"][stage_idx].get("name", ""),
                "title": task["title"],
                "minutes": REINFORCE_MINUTES,
                "kc_id": kc_id,
                "kc_names": [name],
                "p_mastery": round(p, 4),
            }
        )

    detail = "、".join(f"{it['kc_names'][0]}({round(it['p_mastery'] * 100)}%)" for it in inserted)
    reason = (
        f"{reason_prefix}检测到 {len(inserted)} 个掌握度低于 {_REINFORCE_PCT}% 的知识点: {detail}; "
        f"已在最近未完成的日程中插入 {len(inserted)} 个补强任务 (每个 {REINFORCE_MINUTES} 分钟)。"
    )
    plan.structure_json = new_structure
    plan.version = (plan.version or 1) + 1
    adjustment = PlanAdjustment(
        tenant_id=tenant_id,
        plan_id=plan.id,
        reason=reason,
        diff_json={
            "trigger": trigger,
            "threshold": REINFORCE_THRESHOLD,
            "inserted": inserted,
        },
    )
    session.add(adjustment)
    await session.flush()
    log.info(
        "plan_adjusted", plan_id=str(plan.id), trigger=trigger, inserted=len(inserted)
    )
    return {"inserted": inserted, "reason": reason, "adjustment_id": str(adjustment.id)}


async def adjust_on_low_score(ctx, accuracy: float) -> dict | None:
    """低分测评 (正确率 < 0.6) 自动触发一次计划调整; 无活跃计划或异常时返回 None。

    本函数是 services.record_attempts 的尾部挂载点: 判分主流程已完成并提交,
    调整失败绝不能反噬作答结果, 因此整体吞异常只记日志 (向后兼容第一)。
    """
    from .db import tenant_session

    try:
        async with tenant_session(ctx.tenant_id) as session:
            plan = await latest_plan(session, ctx.user_id)
            if plan is None:
                return None
            result = await adjust_plan(
                session,
                ctx.user_id,
                ctx.tenant_id,
                plan,
                trigger="low_score_attempt",
                reason_prefix=f"本次测评正确率 {round(accuracy * 100)}% 低于 60% 阈值; ",
            )
            result["plan_id"] = str(plan.id)
            return result
    except Exception:  # noqa: BLE001 — 计划调整失败不影响测评判分结果
        log.exception("plan_auto_adjust_failed")
        return None
