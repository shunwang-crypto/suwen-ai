"""Educational domain policies and tools for the native agent runtime."""

from __future__ import annotations

import ipaddress
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

from ..gateway.base import Tier
from ..kc.types import normalize_kc_name
from .context import PlanState
from .toolkit import OBJECT, EducationTools, schema

# 容错 JSON 解析 (整体 -> 截取首尾大括号), 与全仓共用 json_utils.parse_json_object

# 本文件位于 packages/core/src/eduagents_core/education/ -> parents[5] 为仓库根
_PROMPTS_DIR = Path(__file__).resolve().parents[5] / "prompts"
_MAX_ATTEMPTS = 2  # 首次 + 重试一次
_DAY_MIN_RATIO = 0.85  # F3-A1: 每日任务分钟和与预算误差严格小于 15%
_DAY_MAX_RATIO = 1.15  # 上限 = 1.15 * 预算
_TOPIC_LIMIT = 5  # gather_resources 逐 topic 检索的 topic 上限
_GOAL_TOPIC_LIMIT = 8
_GOAL_SKILL_LIMIT = 8
_SKILL_NAME_LIMIT = 120
_SKILL_REASON_LIMIT = 80
_SKILL_GAP_LIMIT = 8
_GOAL_SUMMARY_LIMIT = 500
_GOAL_ANALYSIS_ITEM_LIMIT = 5
_GOAL_ANALYSIS_TEXT_LIMIT = 240
_KB_TOP_K = 3  # 每 topic 取知识库 top3
_WEB_TOP_K = 2  # 每 topic 取 web top2
_DIAGNOSIS_LIMIT = 20  # 进 prompt 的掌握度条目上限 (目标相关优先, 防跨学科全量膨胀)
_MAX_TASK_TITLE = 200
_MAX_TASK_OBJECTIVE = 600
_MAX_TASK_INSTRUCTION = 300
_MAX_TASK_CRITERIA = 600
_MAX_INSTRUCTIONS = 5
_MAX_TASKS_PER_DAY = 12
_ACTION_TYPES = {"read", "practice", "tutor", "review_errors", "mixed"}
_DIFFICULTIES = {"easy", "mid", "hard"}


def _topic_related(name: str, topics: list[str]) -> bool:
    """KC 名与目标主题的宽松相关判定 (归一化后相等或互为包含)。"""
    n = normalize_kc_name(name)
    for topic in topics:
        t = normalize_kc_name(topic)
        if t and (n == t or n in t or t in n):
            return True
    return False


def _render_diagnosis(
    diagnosis: dict, key_topics: list[str], baseline_topics: list[str] | None = None
) -> str:
    """Render verified evidence semantics without model-estimated probabilities."""
    zone_rank = {"较弱": 0, "待巩固": 1, "接近掌握": 2, "已掌握": 3}
    entries: list[tuple[str, str, str | None]] = []
    for name, raw in diagnosis.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(raw, dict):
            continue
        state = raw.get("evidence_state")
        band = raw.get("mastery_band")
        if state == "observed" and band in zone_rank:
            entries.append((name.strip(), "observed", str(band)))
        elif state in {"estimated", "unobserved"}:
            entries.append((name.strip(), str(state), None))
    entries.sort(
        key=lambda item: (
            not _topic_related(item[0], key_topics),
            0 if item[1] != "observed" else 1,
            zone_rank.get(item[2] or "", 4),
            item[0],
        )
    )
    lines: list[str] = []
    seen: set[str] = set()
    for name, state, band in entries[:_DIAGNOSIS_LIMIT]:
        normalized = normalize_kc_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if state == "observed" and band:
            lines.append(f"- {name}: {band}（真实作答证据充分）")
        else:
            lines.append(f"- {name}: 证据不足，先安排短验证")
    for topic in baseline_topics or []:
        if len(lines) >= _DIAGNOSIS_LIMIT or not isinstance(topic, str) or not topic.strip():
            continue
        normalized = normalize_kc_name(topic)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        lines.append(f"- {topic.strip()}: 尚未验证，先安排基线练习")
    return "\n".join(lines) or "(暂无真实学习证据，先安排基线任务)"


class PlanTaskContext(PlanState, total=False):
    """PlanState 的任务内部扩展 (契约 state.py 不动)。

    parsed_goal: parse_goal 节点产物
        {"subject", "key_topics", "level_hint", "skills"};
    kc_order: 主题先后约束 (list[str], KC 名)。来自真实先修 DAG: API 层
        (routers/plans.py) 预查 kc_edges 先修边, 经 kc.dag.topological_order
        计算拓扑序后注入 —— EducationDeps 无 kc_edges 字段, 任务内不查库,
        故沿用 state 直接注入通道 (F3-A2)。
    """

    parsed_goal: dict
    kc_order: list[str]
    # API 层从 weak_point_kcs 结果中裁剪出的定性薄弱点；不含精确
    # 掌握度、KC id 或错题原文，可安全进 checkpoint。
    weak_kcs: list[dict]
    # 目标所需技能 ∩ 当前薄弱点。与 diagnosis 独立，不产生或修改掌握度。
    skill_gaps: list[dict]
    # 可操作的目标拆解；只保留用户提供或从目标中忠实提取的文本。
    goal_analysis: dict
    baseline_topics: list[str]


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _required_level(value: object) -> int:
    """目标熟练度只允许 1..3；非法模型输出回落中性档 2。"""
    if isinstance(value, bool):
        return 2
    if isinstance(value, int) and 1 <= value <= 3:
        return value
    if isinstance(value, str) and value.strip() in {"1", "2", "3"}:
        return int(value.strip())
    return 2


def _coerce_goal_skills(raw: object, key_topics: list[str]) -> list[dict]:
    """收紧 parse_goal 的模型输出：限量、限长、归一化去重，无有效技能时
    从 key_topics 确定性回退。
    """
    skills: list[dict] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if len(skills) >= _GOAL_SKILL_LIMIT or not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            clean_name = name.strip()[:_SKILL_NAME_LIMIT]
            normalized = normalize_kc_name(clean_name)
            if not normalized or normalized in seen:
                continue
            reason = item.get("reason")
            skills.append(
                {
                    "name": clean_name,
                    "required_level": _required_level(item.get("required_level")),
                    "reason": (
                        reason.strip()[:_SKILL_REASON_LIMIT]
                        if isinstance(reason, str) and reason.strip()
                        else ""
                    ),
                }
            )
            seen.add(normalized)
    if skills:
        return skills
    for topic in key_topics[:_GOAL_SKILL_LIMIT]:
        normalized = normalize_kc_name(topic)
        if not normalized or normalized in seen:
            continue
        skills.append({"name": topic, "required_level": 2, "reason": "达成目标所需"})
        seen.add(normalized)
    return skills


def skill_gap_analysis(skills: list[dict], weak_kcs: list[dict]) -> list[dict]:
    """计算「目标所需技能 ∩ 当前薄弱 KC」，只产出定性规划输入。

    名称匹配复用 KC 归一化规则，不做语义猜测；因此目标外的薄弱点不会
    被误当成技能差距。返回值不含精确掌握度，也不写 mastery。
    """
    if not isinstance(skills, list) or not isinstance(weak_kcs, list):
        return []
    zone_rank = {"较弱": 0, "待巩固": 1}
    weak_by_name: dict[str, tuple[int, str, str]] = {}
    for index, item in enumerate(weak_kcs):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        clean_name = name.strip()[:_SKILL_NAME_LIMIT]
        normalized = normalize_kc_name(clean_name)
        if not normalized:
            continue
        raw_zone = item.get("zone")
        zone = raw_zone if raw_zone in zone_rank else "待巩固"
        previous = weak_by_name.get(normalized)
        if previous is None or zone_rank[zone] < zone_rank[previous[2]]:
            weak_by_name[normalized] = (index, clean_name, zone)

    gaps: list[tuple[int, int, dict]] = []
    seen: set[str] = set()
    for skill_index, skill in enumerate(skills):
        if not isinstance(skill, dict):
            continue
        name = skill.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        clean_name = name.strip()[:_SKILL_NAME_LIMIT]
        normalized = normalize_kc_name(clean_name)
        if not normalized or normalized in seen or normalized not in weak_by_name:
            continue
        _weak_index, weak_name, zone = weak_by_name[normalized]
        level = _required_level(skill.get("required_level"))
        raw_reason = skill.get("reason")
        goal_reason = (
            raw_reason.strip()[:_SKILL_REASON_LIMIT]
            if isinstance(raw_reason, str) and raw_reason.strip()
            else ""
        )
        level_label = {1: "了解", 2: "独立应用", 3: "灵活迁移"}[level]
        gap_reason = f"达成目标需要「{clean_name}」达到{level_label}，当前{zone}"
        if goal_reason:
            gap_reason += f"；{goal_reason}"
        gaps.append(
            (
                skill_index,
                zone_rank[zone],
                {
                    "skill": clean_name,
                    "weak_kc": weak_name,
                    "required_level": level,
                    "zone": zone,
                    "goal_reason": goal_reason,
                    "gap_reason": gap_reason,
                },
            )
        )
        seen.add(normalized)
    gaps.sort(key=lambda item: (-item[2]["required_level"], item[1], item[0]))
    return [item[2] for item in gaps[:_SKILL_GAP_LIMIT]]


def _render_skill_gaps(skill_gaps: object) -> str:
    """技能差距出境渲染：只保留目标档位与当前定性区间。"""
    if not isinstance(skill_gaps, list):
        return "(当前目标与已知薄弱点无交集)"
    lines: list[str] = []
    for item in skill_gaps[:_SKILL_GAP_LIMIT]:
        if not isinstance(item, dict):
            continue
        skill = item.get("skill")
        zone = item.get("zone")
        if not isinstance(skill, str) or not skill.strip() or zone not in {"较弱", "待巩固"}:
            continue
        level = _required_level(item.get("required_level"))
        label = {1: "了解", 2: "独立应用", 3: "灵活迁移"}[level]
        reason = item.get("goal_reason")
        suffix = (
            f"；目标依据：{reason.strip()[:_SKILL_REASON_LIMIT]}"
            if isinstance(reason, str) and reason.strip()
            else ""
        )
        lines.append(f"- {skill.strip()[:_SKILL_NAME_LIMIT]}：目标{label}，当前{zone}{suffix}")
    return "\n".join(lines) or "(当前目标与已知薄弱点无交集)"


def _coerce_text_list(raw: object, *, limit: int = _GOAL_ANALYSIS_ITEM_LIMIT) -> list[str]:
    """模型文本数组的统一边界：去空、限长、归一化去重。"""
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if len(result) >= limit or not isinstance(item, str) or not item.strip():
            continue
        text = " ".join(item.split())[:_GOAL_ANALYSIS_TEXT_LIMIT]
        normalized = normalize_kc_name(text)
        if not normalized or normalized in seen:
            continue
        result.append(text)
        seen.add(normalized)
    return result


def _coerce_goal_analysis(data: object, state: PlanTaskContext) -> dict:
    """把目标解析收紧为可展示、可进 checkpoint 的安全契约。

    明确表单字段优先于模型提取；未提供验收标准时保持空列表，
    不为用户伪造分数、正确率或截止日期。
    """
    parsed = data if isinstance(data, dict) else {}
    goal = " ".join(str(state.get("goal") or "").split())[:_GOAL_SUMMARY_LIMIT]
    current_level = " ".join(str(state.get("current_level") or "").split())[:160]
    target_outcome = " ".join(str(state.get("target_outcome") or "").split())[
        :_GOAL_ANALYSIS_TEXT_LIMIT
    ]
    explicit_criteria = " ".join(str(state.get("success_criteria") or "").split())[
        :_GOAL_ANALYSIS_TEXT_LIMIT
    ]

    outcomes = _coerce_text_list(parsed.get("outcomes"))
    if target_outcome:
        outcomes = [target_outcome, *outcomes]
        outcomes = _coerce_text_list(outcomes)
    # 验收标准是用户对“完成”的权威定义；没有明确输入时不采信模型自行补出的数值或比率。
    criteria = [explicit_criteria] if explicit_criteria else []
    constraints = _coerce_text_list(parsed.get("constraints"), limit=4)

    raw_summary = parsed.get("goal_summary")
    if current_level or target_outcome:
        # 结构化输入是用户原话；用确定性拼装保证模型不会在摘要里改写目标。
        parts = [goal]
        if current_level:
            parts.append(f"当前基础：{current_level}")
        if target_outcome:
            parts.append(f"期望成果：{target_outcome}")
        summary = "；".join(parts)[:_GOAL_SUMMARY_LIMIT]
    else:
        summary = (
            " ".join(raw_summary.split())[:_GOAL_SUMMARY_LIMIT]
            if isinstance(raw_summary, str) and raw_summary.strip()
            else goal
        )
    allowed_missing = {"scope", "current_level", "target_outcome", "success_criteria"}
    missing = [
        item
        for item in _coerce_text_list(parsed.get("missing_information"), limit=4)
        if item in allowed_missing
    ]
    if current_level:
        missing = [item for item in missing if item != "current_level"]
    elif not current_level and not str(parsed.get("level_hint") or "").strip():
        missing = [*missing, "current_level"]
    if outcomes:
        missing = [item for item in missing if item != "target_outcome"]
    else:
        missing = [*missing, "target_outcome"]
    if criteria:
        missing = [item for item in missing if item != "success_criteria"]
    else:
        missing = [*missing, "success_criteria"]
    missing = list(dict.fromkeys(missing))[:4]
    supplied = 3 - sum(
        item in missing for item in ("current_level", "target_outcome", "success_criteria")
    )
    quality = "clear" if supplied == 3 else "partial" if supplied >= 1 else "vague"
    return {
        "summary": summary,
        "quality": quality,
        "outcomes": outcomes,
        "success_criteria": criteria,
        "constraints": constraints,
        "missing_information": missing,
    }


def _render_goal_analysis(goal_analysis: object) -> str:
    """目标拆解的 prompt 渲染；不展开未知复合字段。"""
    if not isinstance(goal_analysis, dict):
        return "(未能拆解，请仅依据原始目标安排基线诊断)"
    missing_labels = {
        "scope": "学习范围",
        "current_level": "当前基础",
        "target_outcome": "期望成果",
        "success_criteria": "验收标准",
    }
    summary = str(goal_analysis.get("summary") or "").strip()[:_GOAL_SUMMARY_LIMIT]
    outcomes = _coerce_text_list(goal_analysis.get("outcomes"))
    criteria = _coerce_text_list(goal_analysis.get("success_criteria"))
    constraints = _coerce_text_list(goal_analysis.get("constraints"), limit=4)
    missing = [
        missing_labels[item]
        for item in goal_analysis.get("missing_information") or []
        if item in missing_labels
    ]

    def block(items: list[str], empty: str) -> str:
        return "\n".join(f"- {item}" for item in items) or empty

    return (
        f"目标摘要：{summary or '(未提取)'}\n"
        f"可观察成果：\n{block(outcomes, '- (未明确)')}\n"
        f"验收标准：\n{block(criteria, '- (未明确，不得编造)')}\n"
        f"已知约束：\n{block(constraints, '- (无额外约束)')}\n"
        f"待明确：{'、'.join(missing) if missing else '(无)'}"
    )


def is_safe_public_url(value: object) -> bool:
    """Accept only ordinary public http(s) URLs from the safe search chain."""
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and (
            address.is_private or address.is_loopback or address.is_link_local
        ):
            return False
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- 计划硬校验 (纯函数, 可单测)


def _coerce_task(raw: object) -> dict | None:
    """任务字段容错落地; 非 dict 直接丢弃.

    This is also the schema boundary for model output and user-edited drafts.
    Client-supplied identities are intentionally discarded. A fresh task UUID
    is assigned at this schema boundary; stable resource ids are attached only
    by the API transaction after confirmation.
    """
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    title = (
        title.strip()[:_MAX_TASK_TITLE] if isinstance(title, str) and title.strip() else "学习任务"
    )
    try:
        minutes = int(round(float(raw.get("minutes", 0))))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        minutes = 30  # 无效分钟给中性值, 随后由日预算缩放兜底
    raw_kcs = raw.get("kc_names")
    kc_names = (
        [k.strip()[:120] for k in raw_kcs if isinstance(k, str) and k.strip()][:12]
        if isinstance(raw_kcs, list)
        else []
    )
    ref = raw.get("resource_ref")
    resource_ref = ref if isinstance(ref, int) and not isinstance(ref, bool) else None
    objective = raw.get("objective")
    objective = (
        objective.strip()[:_MAX_TASK_OBJECTIVE]
        if isinstance(objective, str) and objective.strip()
        else f"完成“{title}”并能用自己的话说明关键方法"
    )
    raw_instructions = raw.get("instructions")
    instructions = (
        [
            item.strip()[:_MAX_TASK_INSTRUCTION]
            for item in raw_instructions
            if isinstance(item, str) and item.strip()
        ][:_MAX_INSTRUCTIONS]
        if isinstance(raw_instructions, list)
        else []
    )
    if len(instructions) < 2:
        instructions = [
            f"明确本任务涉及的关键概念：{kc_names[0] if kc_names else title}",
            "完成任务后，用一个例子检验自己是否真正理解",
        ]
    completion = raw.get("completion_criteria")
    completion = (
        completion.strip()[:_MAX_TASK_CRITERIA]
        if isinstance(completion, str) and completion.strip()
        else "能够独立完成本任务，并准确解释关键步骤或结论"
    )
    action_type = raw.get("action_type")
    if action_type not in _ACTION_TYPES:
        if any(word in title for word in ("阅读", "精读", "资料", "概念")):
            action_type = "read"
        elif any(word in title for word in ("练习", "测验", "测试", "习题", "自测")):
            action_type = "practice"
        else:
            action_type = "tutor"
        if resource_ref is not None and action_type == "tutor":
            action_type = "mixed"
    practice_count = raw.get("practice_count")
    if not isinstance(practice_count, int) or isinstance(practice_count, bool):
        practice_count = None
    else:
        practice_count = min(max(practice_count, 1), 20)
    raw_difficulty = raw.get("difficulty")
    difficulty = (
        raw_difficulty
        if isinstance(raw_difficulty, str) and raw_difficulty in _DIFFICULTIES
        else None
    )
    result = {
        "task_id": str(uuid.uuid4()),
        "title": title,
        "objective": objective,
        "instructions": instructions,
        "completion_criteria": completion,
        "minutes": minutes,
        "kc_names": kc_names,
        "resource_ref": resource_ref,
        "action_type": action_type,
        "status": "pending",
    }
    if practice_count is not None:
        result["practice_count"] = practice_count
    if difficulty is not None:
        result["difficulty"] = difficulty
    return result


def _coerce_plan(data: object) -> dict | None:
    """LLM 草稿结构化落地: stages/days/tasks 逐层容错; 完全不可用返回 None。"""
    if not isinstance(data, dict):
        return None
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list):
        return None
    stages: list[dict] = []
    for idx, raw_stage in enumerate(raw_stages[:12]):
        if not isinstance(raw_stage, dict):
            continue
        raw_days = raw_stage.get("days")
        days: list[dict] = []
        for raw_day in raw_days[:90] if isinstance(raw_days, list) else []:
            if not isinstance(raw_day, dict):
                continue
            raw_tasks = raw_day.get("tasks")
            tasks = [
                task
                for task in (
                    _coerce_task(t)
                    for t in (raw_tasks[:_MAX_TASKS_PER_DAY] if isinstance(raw_tasks, list) else [])
                )
                if task is not None
            ]
            days.append({"day": 0, "tasks": tasks})  # day 编号统一由 _renumber_days 重排
        if not days:
            continue
        name = raw_stage.get("name")
        name = name.strip()[:120] if isinstance(name, str) and name.strip() else f"阶段{idx + 1}"
        stages.append({"name": name, "days": days})
    if not stages:
        return None
    return {"stages": stages}


def _fallback_plan(state: dict, total_days: int, budget_minutes: float) -> dict:
    """LLM 草稿两次解析失败后的确定性降级: 按目标主题轮转铺满天数。

    这不是冒充模型输出 — draft_plan 会在 trace 里记录 draft_fallback, 供上层提示降级。
    若存在定性技能差距，先排差距技能；这只改变主题顺序，不会伪造掌握度。
    """
    diagnosis = state.get("diagnosis") or {}
    parsed = state.get("parsed_goal") or {}
    key_topics = [t for t in (parsed.get("key_topics") or []) if isinstance(t, str) and t.strip()]
    gap_topics = [
        item["skill"].strip()
        for item in (state.get("skill_gaps") or [])
        if isinstance(item, dict) and isinstance(item.get("skill"), str) and item["skill"].strip()
    ][:_SKILL_GAP_LIMIT]
    if key_topics:
        # 目标主题优先, 掌握度记录只取与目标相关的 KC 补充 (防跨学科记录污染降级计划)
        related = [k for k in diagnosis if _topic_related(k, key_topics)]
        baseline = [
            topic
            for topic in (state.get("baseline_topics") or [])
            if isinstance(topic, str) and topic.strip()
        ]
        topics = list(dict.fromkeys([*gap_topics, *key_topics, *baseline, *related]))
    else:
        topics = gap_topics or [state.get("goal") or "自主学习"]
    days = []
    for day in range(1, total_days + 1):
        topic = topics[(day - 1) % len(topics)]
        days.append(
            {
                "day": day,
                "tasks": [
                    {
                        "task_id": str(uuid.uuid4()),
                        "title": f"学习: {topic}",
                        "objective": f"掌握{topic}的核心概念，并能独立应用",
                        "instructions": [
                            f"梳理{topic}的定义、关键公式或解题条件",
                            "完成一个例题或自拟例子，并检查推理过程",
                        ],
                        "completion_criteria": f"能不看资料解释{topic}，并独立完成一道基础题",
                        "minutes": max(int(round(budget_minutes)), 1),
                        "kc_names": [topic],
                        "resource_ref": None,
                        "action_type": "practice" if not state.get("kb_id") else "mixed",
                        "practice_count": 3,
                        "difficulty": "mid",
                        "status": "pending",
                    }
                ],
            }
        )
    return {"stages": [{"name": "基础学习", "days": days}]}


def _iter_days(plan: dict) -> list[dict]:
    return [day for stage in plan["stages"] for day in stage["days"]]


def _fix_total_days(plan: dict, total_days: int, budget_minutes: float, fixes: list[dict]) -> None:
    """总天数硬校验: 多则从尾部裁, 少则在末阶段补"综合复习"填充日。"""
    count = len(_iter_days(plan))
    if count == total_days:
        return
    if count > total_days:
        to_remove = count - total_days
        for stage in reversed(plan["stages"]):
            while stage["days"] and to_remove:
                stage["days"].pop()
                to_remove -= 1
        plan["stages"] = [s for s in plan["stages"] if s["days"]]
        fixes.append({"fix": "total_days_trimmed", "from": count, "to": total_days})
    else:
        filler = [
            {
                "day": 0,
                "tasks": [
                    {
                        "task_id": str(uuid.uuid4()),
                        "title": "综合复习与查漏补缺",
                        "objective": "整合当天知识并发现仍不熟练的部分",
                        "instructions": [
                            "回顾当天的关键概念和错因",
                            "完成一道综合题并记录仍需补强的知识点",
                        ],
                        "completion_criteria": "能够列出至少一个已掌握点和一个待巩固点",
                        "minutes": max(int(round(budget_minutes)), 1),
                        "kc_names": [],
                        "resource_ref": None,
                        "action_type": "mixed",
                        "practice_count": 3,
                        "difficulty": "mid",
                        "status": "pending",
                    }
                ],
            }
            for _ in range(total_days - count)
        ]
        if plan["stages"]:
            plan["stages"][-1]["days"].extend(filler)
        else:
            plan["stages"].append({"name": "综合复习", "days": filler})
        fixes.append({"fix": "total_days_padded", "from": count, "to": total_days})


def _renumber_days(plan: dict) -> None:
    for number, day in enumerate(_iter_days(plan), start=1):
        day["day"] = number


def _fix_kc_order(plan: dict, kc_order: list[str], fixes: list[dict]) -> None:
    """KC 先后约束 (F3-A2 简版): 任务按其 kc 在 kc_order 中的最小序稳定重排, 违序任务被顺延。

    只交换"带已排序 KC 的任务"所占的槽位, 无约束任务与日结构 (每天任务数) 保持不变;
    分钟数随任务移动, 因此本修正必须发生在每日分钟缩放之前。
    """
    if not kc_order:
        return
    # 匹配走 normalize_kc_name (与 diagnose 节点同一口径): 真实模型输出的全角/空格/大小写
    # 变体不该让先修约束静默失效
    rank_of: dict[str, int] = {}
    for i, name in enumerate(kc_order):
        rank_of.setdefault(normalize_kc_name(name), i)

    def rank(task: dict) -> int | None:
        ranks = [
            rank_of[normalize_kc_name(k)]
            for k in task["kc_names"]
            if normalize_kc_name(k) in rank_of
        ]
        return min(ranks) if ranks else None

    slots: list[tuple[list, int]] = []  # (所在日的 tasks 列表, 下标), 按时间顺序
    for day in _iter_days(plan):
        for i in range(len(day["tasks"])):
            slots.append((day["tasks"], i))
    ranked_slots = [(lst, i) for lst, i in slots if rank(lst[i]) is not None]
    if not ranked_slots:
        # 有先修序但草稿任务一个都没挂上: 约束落空必须可见, 不许静默 (F3-A2 可审计)
        fixes.append({"fix": "kc_order_unmatched", "kc_count": len(kc_order)})
        return
    ranked_tasks = [lst[i] for lst, i in ranked_slots]
    ranks_now = [rank(t) for t in ranked_tasks]
    if ranks_now == sorted(ranks_now):  # type: ignore[type-var]
        return
    reordered = sorted(ranked_tasks, key=rank)  # type: ignore[arg-type]  # 稳定排序: 同序任务相对位置不变
    moved = [t["title"] for old, t in zip(ranked_tasks, reordered, strict=True) if old is not t]
    for (lst, i), task in zip(ranked_slots, reordered, strict=True):
        lst[i] = task
    fixes.append({"fix": "kc_order_deferred", "moved": moved})


def _fix_resource_refs(plan: dict, n_resources: int, fixes: list[dict]) -> None:
    next_resource = 0
    for day in _iter_days(plan):
        for task in day["tasks"]:
            ref = task["resource_ref"]
            if ref is not None and not (0 <= ref < n_resources):
                task["resource_ref"] = None
                fixes.append({"fix": "resource_ref_nulled", "task": task["title"], "ref": ref})
                ref = None
            # A retrieved resource is a real, server-validated candidate.  Keep
            # resource-less legacy/model tasks executable, but make read/mixed
            # tasks useful when the model omitted an otherwise safe reference.
            # The API still re-checks the selected KB/doc/chunk at confirmation.
            if n_resources and ref is None and task["action_type"] in {"read", "mixed"}:
                task["resource_ref"] = next_resource % n_resources
                fixes.append(
                    {
                        "fix": "resource_ref_attached",
                        "task": task["title"],
                        "ref": task["resource_ref"],
                    }
                )
                next_resource += 1
            elif not n_resources and task["action_type"] == "read":
                task["action_type"] = "tutor"
                task["resource_ref"] = None
                fixes.append({"fix": "unreferenced_read_replaced", "task": task["title"]})


def _fix_day_minutes(plan: dict, budget_minutes: float, fixes: list[dict]) -> None:
    """每日任务分钟和硬校验: 不在 (0.85, 1.15)*预算内时按比例缩放 (F3-A1)。"""
    low = _DAY_MIN_RATIO * budget_minutes
    high = _DAY_MAX_RATIO * budget_minutes
    target = max(int(round(budget_minutes)), 1)
    for day in _iter_days(plan):
        tasks = day["tasks"]
        if not tasks:
            tasks.append(
                {
                    "task_id": str(uuid.uuid4()),
                    "title": "自主复习",
                    "objective": "回顾本阶段知识并找出仍需巩固的内容",
                    "instructions": [
                        "回看关键概念和当天笔记",
                        "完成一道自测题并记录疑问",
                    ],
                    "completion_criteria": "能说出一个已掌握点和一个待巩固点",
                    "minutes": target,
                    "kc_names": [],
                    "resource_ref": None,
                    "action_type": "mixed",
                    "practice_count": 1,
                    "difficulty": "mid",
                    "status": "pending",
                }
            )
            fixes.append({"fix": "empty_day_filled", "day": day["day"]})
            continue
        total = sum(t["minutes"] for t in tasks)
        if low < total < high:
            continue
        factor = budget_minutes / total
        for task in tasks:
            task["minutes"] = max(int(round(task["minutes"] * factor)), 1)
        # 取整误差回填到最大任务, 保证缩放后日总和恰为预算值 (必然落在区间内)
        diff = target - sum(t["minutes"] for t in tasks)
        largest = max(tasks, key=lambda t: t["minutes"])
        largest["minutes"] = max(largest["minutes"] + diff, 1)
        fixes.append(
            {
                "fix": "day_minutes_scaled",
                "day": day["day"],
                "from": total,
                "to": sum(t["minutes"] for t in tasks),
            }
        )


def _harden_plan(
    plan: dict,
    *,
    total_days: int,
    budget_minutes: float,
    kc_order: list[str],
    n_resources: int,
    fixes: list[dict],
) -> None:
    """对已结构化计划执行统一硬校验；生成与用户编辑路径必须共用此顺序。"""
    _fix_total_days(plan, total_days, budget_minutes, fixes)
    _renumber_days(plan)
    _fix_kc_order(plan, kc_order, fixes)
    _fix_resource_refs(plan, n_resources, fixes)
    _fix_day_minutes(plan, budget_minutes, fixes)


# ---------------------------------------------------------------- 任务构建


class PlanTools(EducationTools):
    feature = "agent_plan"
    tier = Tier.plan
    max_tokens = 10000

    def __init__(self, deps, record, emit):
        super().__init__(deps, record, emit)
        self.add_learning_context()
        if self.state.get("plan_confirmed"):
            self.add(
                schema(
                    "publish_plan",
                    "发布服务端已经校验且用户已经确认的计划，模型不能覆盖已确认内容。",
                    {},
                ),
                self.publish,
            )
        else:
            course_bound = self.state.get("plan_mode") == "course_bound"
            self.add_evidence_tools(kb=course_bound, web=not course_bound)
            self.add(
                schema(
                    "analyze_learning_goal",
                    "分析目标技能与已验证学情的差距，返回未知主题和基线练习建议，不估计掌握概率。",
                    {"goal_outline": OBJECT},
                    ("goal_outline",),
                ),
                self.analyze_goal,
            )
            self.add(
                schema(
                    "submit_plan_draft",
                    "提交目标拆解和可执行计划草案。服务端修正预算、天数、先修顺序及资料引用后等待用户确认。",
                    {"goal_outline": OBJECT, "plan": OBJECT},
                    ("plan",),
                ),
                self.submit,
            )

    def prepare_turn(self):
        checked = verified_diagnosis(self.state)
        self.state.update({key: value for key, value in checked.items() if key != "trace"})

    def policy(self):
        return (
            super().policy()
            + (
                "你负责学习规划。读取真实学情，根据目标决定需要哪些资料并调用工具。"
                "goal_outline 可包含 subject、key_topics、skills、goal_analysis。"
                "plan 格式为 stages:[{name,days:[{day,tasks:[{title,minutes,kc_names,resource_ref,action_type,objective,success_criteria}]}]}。"
                "必须覆盖用户要求的全部天数，每日任务时长符合预算。缺少学习证据的主题安排基线练习。"
                "resource_ref 是工具返回 resources 的下标；没有资料时使用 null。"
                "提交草案后等待用户确认。已确认时只调用 publish_plan，不修改内容。"
            )
            + json.dumps(
                {
                    "mode": self.state.get("plan_mode"),
                    "confirmed": bool(self.state.get("plan_confirmed")),
                },
                ensure_ascii=False,
            )
        )

    async def analyze_goal(self, args):
        self.state.update(
            {
                k: v
                for k, v in normalize_goal(self.state, args["goal_outline"]).items()
                if k != "trace"
            }
        )
        self.prepare_turn()
        return {
            key: self.state.get(key)
            for key in (
                "parsed_goal",
                "goal_analysis",
                "skill_gaps",
                "diagnosis",
                "baseline_topics",
                "kc_order",
            )
        }

    def resources(self):
        course_bound = self.state.get("plan_mode") == "course_bound"
        allowed_kbs = set(self.state.get("kb_ids") or [self.state.get("kb_id")])
        old = self.state.get("resources") or []
        resources, remap = [], {}
        for index, resource in enumerate(old):
            if not isinstance(resource, dict):
                continue
            allowed = (
                course_bound
                and resource.get("type") == "kb"
                and resource.get("kb_id") in allowed_kbs
                and resource.get("chunk_id")
            ) or (
                not course_bound
                and resource.get("type") == "web"
                and is_safe_public_url(resource.get("url") or "")
            )
            if allowed:
                remap[index] = len(resources)
                resources.append(resource)
        for day in _iter_days(self.state.get("plan") or {"stages": []}):
            for task in day.get("tasks") or []:
                task["resource_ref"] = remap.get(task.get("resource_ref"))
        self.state["resources"] = resources
        known = {r.get("chunk_id") or r.get("url") for r in resources}
        for item in (self.state.get("evidences") or []) if course_bound else []:
            if item["chunk_id"] not in known:
                known.add(item["chunk_id"])
                resources.append(
                    {
                        "type": "kb",
                        "title": item["heading_path"][:512],
                        "kb_id": item.get("kb_id") or self.state.get("kb_id", ""),
                        "doc_id": item["doc_id"],
                        "chunk_id": item["chunk_id"],
                    }
                )
        for item in (self.state.get("web_results") or []) if not course_bound else []:
            if item["url"] not in known and is_safe_public_url(item["url"]):
                known.add(item["url"])
                resources.append({"type": "web", "title": item["title"][:512], "url": item["url"]})
        return resources

    async def search_kb(self, args):
        result = await super().search_kb(args)
        result["resources"] = self.resources()
        return result

    async def search_web(self, args):
        result = await super().search_web(args)
        result["resources"] = self.resources()
        return result

    async def submit(self, args):
        outline = args.get("goal_outline") or {}
        normalized = normalize_goal(self.state, outline if isinstance(outline, dict) else {})
        self.state.update({key: value for key, value in normalized.items() if key != "trace"})
        self.prepare_turn()
        plan = _coerce_plan(args.get("plan"))
        if plan is None:
            return {
                "accepted": False,
                "errors": ["计划结构无效，请按 stages/days/tasks 结构重新提交"],
            }
        fixes = []
        resources = self.resources()
        _harden_plan(
            plan,
            total_days=max(1, int(self.state.get("total_days") or 1)),
            budget_minutes=max(1.0, float(self.state.get("budget_hours_per_day") or 1) * 60),
            kc_order=self.state.get("kc_order") or [],
            n_resources=len(resources),
            fixes=fixes,
        )
        self.state["plan"] = plan
        self.record.pending = {
            "kind": "plan_confirm",
            "plan": plan,
            "resources": resources,
            "goal_analysis": self.state.get("goal_analysis") or {},
            "skill_gaps": self.state.get("skill_gaps") or [],
            "plan_mode": self.state.get("plan_mode") or "goal_only",
        }
        self.record.status = "awaiting_confirmation"
        self.trace(node="draft_plan", event="drafted", fix_count=len(fixes))
        for fix in fixes:
            self.trace(node="draft_plan", event="fix", **fix)
        return {
            "accepted": True,
            "status": "awaiting_confirmation",
            "instruction": "停止并等待真实用户确认",
        }

    def confirm(self, decision):
        self.resources()
        self.record.pending = None
        if not isinstance(decision, dict) or decision.get("approved") is not True:
            self.record.status = "cancelled"
            self.state["plan_confirmed"] = False
            return
        modified = _coerce_plan(decision.get("plan")) if "plan" in decision else None
        if modified is not None:
            _harden_plan(
                modified,
                total_days=max(1, int(self.state.get("total_days") or 1)),
                budget_minutes=max(1.0, float(self.state.get("budget_hours_per_day") or 1) * 60),
                kc_order=self.state.get("kc_order") or [],
                n_resources=len(self.state.get("resources") or []),
                fixes=[],
            )
            self.state["plan"] = modified
        self.state["plan_confirmed"] = True

    async def publish(self, _args):
        if not self.state.get("plan_confirmed"):
            return {"accepted": False, "errors": ["计划尚未获得用户确认"]}
        self.resources()
        self.record.status = "completed"
        self.trace(node="confirm_plan", event="plan_approved")
        return {"accepted": True, "status": "completed"}


def verified_diagnosis(state: PlanTaskContext) -> dict:
    # Only verified evidence may supply a mastery band. Unobserved topics
    # are scheduled for a baseline check; the planner never asks an LLM to
    # invent a probability for them.
    raw_diagnosis = state.get("diagnosis") or {}
    existing: dict[str, dict[str, str]] = {}
    for key, value in raw_diagnosis.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(value, dict):
            continue
        evidence = value.get("evidence_state")
        band = value.get("mastery_band")
        if evidence not in {"unobserved", "estimated", "observed"}:
            continue
        item = {"evidence_state": str(evidence)}
        if evidence == "observed" and band in {"较弱", "待巩固", "接近掌握", "已掌握"}:
            item["mastery_band"] = str(band)
        existing[key.strip()] = item
    observed_keys = {
        normalize_kc_name(name)
        for name, item in existing.items()
        if item.get("evidence_state") == "observed"
    }
    parsed = state.get("parsed_goal") or {}
    topics = parsed.get("key_topics") or []
    baseline = [
        topic
        for topic in topics
        if isinstance(topic, str)
        and topic.strip()
        and normalize_kc_name(topic) not in observed_keys
    ]
    return {
        "diagnosis": existing,
        "baseline_topics": baseline,
        "trace": [
            {
                "node": "diagnose",
                "event": "evidence_state_used",
                "kc_count": len(existing),
                "observed_count": len(observed_keys),
                "baseline_count": len(baseline),
            }
        ],
    }


def qualitative_weak_kcs(items):
    selected, seen = [], set()
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:120]
        key = normalize_kc_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        clean = {"name": name}
        if item.get("zone") in {"较弱", "待巩固"}:
            clean["zone"] = item["zone"]
        if item.get("mastery_band") in {"较弱", "待巩固", "接近掌握", "已掌握"}:
            clean["mastery_band"] = item["mastery_band"]
        if item.get("evidence_state") in {"unobserved", "estimated", "observed"}:
            clean["evidence_state"] = item["evidence_state"]
        selected.append(clean)
    return selected


def normalize_goal(state, data):
    goal = state.get("goal", "")
    parsed = {"subject": "", "key_topics": [], "level_hint": "", "skills": []}
    degraded = data is None
    if data is not None:
        subject = data.get("subject")
        parsed["subject"] = subject.strip()[:120] if isinstance(subject, str) else ""
        topics = data.get("key_topics")
        if isinstance(topics, list):
            seen_topics: set[str] = set()
            for topic in topics:
                if len(parsed["key_topics"]) >= _GOAL_TOPIC_LIMIT:
                    break
                if not isinstance(topic, str) or not topic.strip():
                    continue
                clean_topic = topic.strip()[:_SKILL_NAME_LIMIT]
                normalized = normalize_kc_name(clean_topic)
                if normalized and normalized not in seen_topics:
                    parsed["key_topics"].append(clean_topic)
                    seen_topics.add(normalized)
        hint = data.get("level_hint")
        parsed["level_hint"] = hint.strip()[:160] if isinstance(hint, str) else ""
    if state.get("current_level"):
        # 用户明确自报的基础比模型归纳更权威。
        parsed["level_hint"] = str(state["current_level"]).strip()[:160]
    if not parsed["key_topics"] and goal.strip():
        # 兜底: 目标本身充当唯一主题, 保证后续节点仍有输入
        parsed["key_topics"] = [goal.strip()[:30]]
    parsed["skills"] = _coerce_goal_skills(
        data.get("skills") if isinstance(data, dict) else None,
        parsed["key_topics"],
    )
    goal_analysis = _coerce_goal_analysis(data, state)
    skill_gaps = skill_gap_analysis(parsed["skills"], state.get("weak_kcs") or [])
    event = "llm_parse_failed_fallback" if degraded else "parsed"
    return {
        "parsed_goal": parsed,
        "goal_analysis": goal_analysis,
        "skill_gaps": skill_gaps,
        "trace": [
            {
                "node": "parse_goal",
                "event": event,
                "parsed_goal": parsed,
                "goal_quality": goal_analysis["quality"],
                "skill_gap_count": len(skill_gaps),
            }
        ],
    }
