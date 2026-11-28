"""Compile model-authored teaching activities into the standard learning plan."""

from __future__ import annotations

import logging
import math

from ..gateway.base import GatewayError
from ..kc.types import normalize_kc_name
from .plan import (
    _MAX_TASKS_PER_DAY,
    PlanTools,
    _coerce_plan,
    _iter_days,
    _phased_draft_schema,
    _renumber_days,
    normalize_goal,
)
from .plan_checks import plan_issues
from .toolkit import EducationTools, argument_errors, schema

log = logging.getLogger(__name__)
_ACTIVITY = {"type": "string", "minLength": 1, "maxLength": 300, "pattern": r"\S"}
_DURATION = {"baseline": 8, "learn": 30, "practice": 30, "assessment": 25, "review": 20}
_LABEL = {
    "baseline": "验证起点",
    "learn": "学习",
    "practice": "独立练习",
    "assessment": "验收",
    "review": "间隔复习",
}


def _activity(phase, key, topics, instruction, criteria, dependencies, *, resource=None):
    label = _LABEL[phase]
    return {
        "title": f"{label}：{'、'.join(topics)}",
        "objective": instruction,
        "instructions": [instruction, "完成后用验收标准检查并记录结果。"],
        "completion_criteria": criteria,
        "minutes": 1,
        "kc_names": topics,
        "resource_ref": resource,
        "action_type": "read"
        if resource is not None
        else "tutor"
        if phase == "learn"
        else "practice",
        "step_key": key,
        "learning_phase": phase,
        "depends_on": dependencies,
    }


def _daily_budget(state):
    return math.floor(float(state.get("budget_hours_per_day", 1)) * 60 + 1e-8)


def compile_activities(units, assessment, state, *, review=None):
    """Order selected topics, build their dependencies and schedule within the real budget."""
    ranked = {normalize_kc_name(name): i for i, name in enumerate(state.get("kc_order") or [])}
    ordered = sorted(
        units, key=lambda unit: ranked.get(normalize_kc_name(unit["topic"]), len(ranked))
    )
    observed = {
        normalize_kc_name(name)
        for name, item in (state.get("diagnosis") or {}).items()
        if item.get("evidence_state") == "observed"
    }
    steps, practices = [], []
    previous_learning = None
    for number, unit in enumerate(ordered, 1):
        topic = unit["topic"].strip()
        dependencies = [previous_learning] if previous_learning else []
        if normalize_kc_name(topic) not in observed:
            baseline = f"baseline-{number}"
            steps.append(
                _activity(
                    "baseline",
                    baseline,
                    [topic],
                    unit.get("baseline")
                    or f"先尝试这项练习的起始步骤，列出已知条件和拟用方法：{unit['practice']}",
                    "留下独立作答或初步方法，标出需要学习的步骤。",
                    [],
                )
            )
            dependencies.append(baseline)
        learned = f"learn-{number}"
        steps.append(
            _activity(
                "learn",
                learned,
                [topic],
                unit["learn"],
                "能用自己的话解释本步骤的方法和适用条件。",
                dependencies,
                resource=unit.get("resource_ref"),
            )
        )
        practice = f"practice-{number}"
        steps.append(
            _activity("practice", practice, [topic], unit["practice"], unit["check"], [learned])
        )
        practices.append(steps[-1])
        previous_learning = learned

    topics = [unit["topic"].strip() for unit in ordered]
    practice_keys = [task["step_key"] for task in practices]
    criteria = state.get("success_criteria") or " ".join(unit["check"] for unit in ordered)
    if review:
        steps.append(_activity("review", "review", topics, review, criteria, practice_keys))

    total_days = int(state.get("total_days", 1))
    minutes = _daily_budget(state)
    num_required = len(steps) + bool(assessment)
    if minutes < 1 or num_required > total_days * min(minutes, _MAX_TASKS_PER_DAY):
        raise ValueError(
            "现有天数和每日分钟数放不下这些活动。请减少独立主题，保留必要的基线、教学和练习。"
        )
    for offset in range(total_days - num_required):
        previous = practices[offset % len(practices)]
        steps.append(
            _activity(
                "review",
                f"review-{offset + 1}",
                previous["kc_names"],
                f"找一个新例子重做这项练习，比较两次方法：{previous['objective']}",
                f"独立完成新例子，并按原来的标准核对：{previous['completion_criteria']}",
                [previous["step_key"]],
            )
        )
    if assessment:
        dependencies = ["review"] if review else practice_keys
        steps.append(
            _activity("assessment", "assessment", topics, assessment, criteria, dependencies)
        )

    days = []
    for day in range(1, total_days + 1):
        length = math.ceil(len(steps) / (total_days - day + 1))
        tasks = steps[:length]
        del steps[:length]
        # Reserve time for feedback or adjustments whenever it fits.
        available = max(len(tasks), minutes - min(5, minutes // 4))
        allocated = len(tasks)
        while allocated < available:
            candidates = [
                task for task in tasks if task["minutes"] < _DURATION[task["learning_phase"]]
            ]
            if not candidates:
                break
            target = min(
                candidates, key=lambda task: task["minutes"] / _DURATION[task["learning_phase"]]
            )
            target["minutes"] += 1
            allocated += 1
        days.append({"day": day, "tasks": tasks})
    return {"stages": [{"name": "学习与练习", "days": days}]}


def _used_resources(plan, resources):
    tasks = [task for day in _iter_days(plan) for task in day["tasks"]]
    indexes = sorted(
        {task["resource_ref"] for task in tasks if task.get("resource_ref") is not None}
    )
    remap = {old: new for new, old in enumerate(indexes)}
    for task in tasks:
        task["resource_ref"] = remap.get(task.get("resource_ref"))
    return [resources[index] for index in indexes]


def _edited_plan(value):
    errors = argument_errors(value, _phased_draft_schema(allow_extra_fields=True), path="plan")
    if errors:
        raise GatewayError("PLAN_EDIT_INVALID", "请修正草案结构。 " + " ".join(errors))
    has_practice = False
    for number, day in enumerate(_iter_days(value), 1):
        if day["day"] != number:
            raise GatewayError("PLAN_EDIT_INVALID", "天数必须从第 1 天连续编号。")
        for task in day["tasks"]:
            content = [
                task["title"],
                task["objective"],
                task["completion_criteria"],
                *task["instructions"],
            ]
            if any(not item.strip() for item in content):
                raise GatewayError(
                    "PLAN_EDIT_INVALID", f"第 {number} 天的任务需要完整的活动和验收标准。"
                )
            if not task["kc_names"] or any(
                not normalize_kc_name(name) for name in task["kc_names"]
            ):
                raise GatewayError("PLAN_EDIT_INVALID", f"第 {number} 天的任务需要有效知识点。")
            has_practice |= task["learning_phase"] in {"practice", "assessment"}
    if not has_practice:
        raise GatewayError("PLAN_EDIT_INVALID", "编辑后的计划需要保留至少一个独立练习或验收步骤。")
    plan = _coerce_plan(value)
    _renumber_days(plan)
    return plan


class GuidedPlanTools(PlanTools):
    max_tokens = 4500

    def __init__(self, deps, record, emit):
        EducationTools.__init__(self, deps, record, emit)
        self.add_learning_context()
        course = self.state.get("plan_mode") == "course_bound"
        self.add_evidence_tools(kb=course, web=not course)
        unit = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "topic": {
                    **_ACTIVITY,
                    "maxLength": 120,
                    "description": "知识点名称。适用时沿用输入 kc_order 的名称。",
                },
                "baseline": {
                    **_ACTIVITY,
                    "description": "尚无作答证据时用的短起点题。只写题目或尝试步骤。",
                },
                "learn": {**_ACTIVITY, "description": "具体要学会的方法和如何学。"},
                "practice": {
                    **_ACTIVITY,
                    "description": "学完后独立完成的练习。围绕用户要做成的事，写出条件。",
                },
                "check": {**_ACTIVITY, "description": "独立练习完成后可观察、可判断的验收标准。"},
                "resource_ref": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": "支持该教学活动的材料编号，来自输入 materials 或检索 resources 的从零起始下标。没有合适资料时省略。",
                },
            },
            "required": ["topic", "learn", "practice", "check"],
        }
        self.add(
            schema(
                "submit_learning_activities",
                "提交针对学习目标的教学和独立练习活动。服务端安排天数、分钟数、阶段和依赖，再等待用户确认。",
                {
                    "units": {"type": "array", "minItems": 1, "maxItems": 8, "items": unit},
                    "review": {
                        **_ACTIVITY,
                        "description": "如目标需要复习，安排学员依据前面真实完成的练习重新解释或比较。",
                    },
                    "assessment": {
                        **_ACTIVITY,
                        "description": "需要综合应用时，写出最后应提交的成果和要覆盖的边界。",
                    },
                },
                ("units",),
            ),
            self.submit_activities,
        )

    async def prepare_context(self):
        if "search_knowledge_base" not in self.handlers or self.state.get("kb_searched"):
            return
        goal = self.state.get("goal", "")
        try:
            await self.search_kb({"queries": [goal]})
        except Exception:
            log.warning("Initial plan retrieval failed", exc_info=True)
            self.trace(node="gather_resources", event="retrieval_unavailable")

    async def search_kb(self, args):
        result = await super().search_kb(args)
        known = {item.get("chunk_id") for item in result["resources"]}
        self.state["evidences"] = [
            item for item in self.state.get("evidences") or [] if item["chunk_id"] in known
        ]
        if "sources" in result:
            result["sources"] = [item for item in result["sources"] if item["id"] in known]
        return result

    def input(self, messages):
        resources = self.resources()
        excerpts = {e["chunk_id"]: e["text"] for e in self.state.get("evidences") or []}
        return {
            **EducationTools.input(self, messages),
            "kc_order": (self.state.get("kc_order") or [])[:100],
            "diagnosis": dict(list((self.state.get("diagnosis") or {}).items())[:20]),
            "materials": [
                {
                    "resource_ref": index,
                    "title": item["title"],
                    "excerpt": excerpts.get(item.get("chunk_id"), "")[:650],
                }
                for index, item in enumerate(resources[:6])
            ],
        }

    def policy(self):
        return (
            EducationTools.policy(self)
            + "\n你负责给定目标的教学内容。根据 learning context 和资料，每个相关知识点只提交一次。"
            "直接调用 submit_learning_activities。每个单元填 topic、learn、practice、check。用户需要综合成果时加 assessment。"
            "写具体可执行的练习和验收。请为实际支持该教学活动的资料填写 resource_ref，有材料缺口时在活动里说明。"
            "安排日期、分钟数、阶段、步骤键和依赖由服务端负责。"
        )

    def repair_tool_call(self):
        spec = next(item for item in self.schemas if item["name"] == "submit_learning_activities")
        return spec["name"], self.input([]), spec["parameters"]

    async def submit_activities(self, args):
        units = args["units"]
        keys = [normalize_kc_name(unit["topic"]) for unit in units]
        if not all(keys) or len(set(keys)) != len(keys):
            return {"accepted": False, "errors": ["每个单元必须有不同的知识点名称。"]}
        resources = self.resources()
        bad_refs = [
            unit["topic"]
            for unit in units
            if unit.get("resource_ref") is not None
            and not 0 <= unit["resource_ref"] < len(resources)
        ]
        if bad_refs:
            return {
                "accepted": False,
                "errors": [f"资料编号不可用：{'、'.join(bad_refs)}。请选择返回的编号或省略。"],
            }
        try:
            compiled = compile_activities(
                units, args.get("assessment"), self.state, review=args.get("review")
            )
        except ValueError as exc:
            return {"accepted": False, "errors": [str(exc)]}
        plan = _coerce_plan(compiled)
        _renumber_days(plan)
        errors = plan_issues(plan, self.state, resources=resources)
        if errors:
            return {"accepted": False, "errors": [item["message"] for item in errors]}
        resources = _used_resources(plan, resources)
        outline = normalize_goal(self.state, {"key_topics": [unit["topic"] for unit in units]})
        self.state.update(
            plan=plan,
            resources=resources,
            parsed_goal=outline["parsed_goal"],
            goal_analysis=outline["goal_analysis"],
            skill_gaps=outline["skill_gaps"],
        )
        self.record.pending = {
            "kind": "plan_confirm",
            "plan": plan,
            "resources": resources,
            "goal_analysis": outline["goal_analysis"],
            "skill_gaps": outline["skill_gaps"],
            "plan_mode": self.state.get("plan_mode") or "goal_only",
        }
        self.record.status = "awaiting_confirmation"
        self.trace(node="draft_plan", event="drafted", plan_generation_mode="guided")
        return {"accepted": True, "status": "awaiting_confirmation"}

    def confirm(self, decision):
        if not isinstance(decision, dict) or decision.get("approved") is not True:
            self.record.pending = None
            self.state["plan_confirmed"] = False
            self.record.status = "cancelled"
            return
        plan = self.state["plan"]
        if "plan" in decision and decision["plan"] is not None:
            plan = _edited_plan(decision["plan"])
        resources = self.state.get("resources") or []
        errors = [item["message"] for item in plan_issues(plan, self.state, resources=resources)]
        for day in _iter_days(plan):
            if sum(task["minutes"] for task in day["tasks"]) > _daily_budget(self.state):
                errors.append(f"第 {day['day']} 天超出学习时长上限。")
        if errors:
            raise GatewayError("PLAN_EDIT_INVALID", " ".join(dict.fromkeys(errors)))
        self.state.update(
            plan=plan, resources=_used_resources(plan, resources), plan_confirmed=True
        )
        self.record.pending = None
        self.record.status = "completed"
        self.trace(node="confirm_plan", event="plan_approved")
