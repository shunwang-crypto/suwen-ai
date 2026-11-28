"""Deterministic checks for explicitly phased, dependency-linked team plans."""

from __future__ import annotations

import hashlib
import json
import re

from .plan import LEARNING_PHASES, STEP_KEY_PATTERN, _iter_days, normalize_kc_name

CHECKS = ("prerequisites", "baseline", "time_budget", "resources", "outcomes", "review_timeline")


def plan_digest(plan):
    # Server-generated ids/status are not teaching content and may change at confirmation.
    def content(value):
        if isinstance(value, dict):
            return {k: content(v) for k, v in value.items() if k not in {"task_id", "status"}}
        return [content(v) for v in value] if isinstance(value, list) else value

    return hashlib.sha256(json.dumps(content(plan), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def plan_issues(plan, state, *, resources=None):
    issues = []

    def issue(code, message, keys=(), suggestion="调整任务依赖、阶段或内容后重新提交"):
        issues.append({"code": code, "message": message, "task_keys": list(keys), "suggestion": suggestion})

    days = _iter_days(plan)
    if resources is not None:
        expected = max(1, int(state.get("total_days") or 1))
        if len(days) != expected:
            issue("PLAN_DAY_COUNT", f"计划需覆盖 {expected} 天，目前有 {len(days)} 天",
                  suggestion="按实际天数重新分配学习、练习和复习任务")
        for day, value in enumerate(days, 1):
            if not value["tasks"]:
                issue("PLAN_DAY_EMPTY", f"第 {day} 天没有任务", suggestion="为该天提供具体可执行的学习步骤")
    tasks = [(day, task) for day, value in enumerate(days, 1) for task in value["tasks"]]
    positions = {}
    for index, (day, task) in enumerate(tasks):
        key = task.get("step_key", "")
        if not isinstance(key, str):
            key = ""
        if not re.fullmatch(STEP_KEY_PATTERN, key):
            issue("TASK_KEY_REQUIRED", f"第 {day} 天任务需要唯一的英文 step_key",
                  suggestion="用简短英文键标记任务，并在后续任务中按原样引用")
        elif key in positions:
            issue("TASK_KEY_DUPLICATE", f"任务键 {key} 重复", [key])
        else:
            positions[key] = index
        if task.get("learning_phase") not in LEARNING_PHASES:
            issue("LEARNING_PHASE_REQUIRED", "任务需要明确 baseline/learn/practice/review/assessment 阶段", [key])
        if not isinstance(task.get("depends_on"), list):
            issue("DEPENDENCIES_REQUIRED", "任务需要 depends_on 列表；无依赖时填空列表", [key])
        ref = task.get("resource_ref")
        if resources is not None and ref is not None and (
            not isinstance(ref, int) or isinstance(ref, bool) or not 0 <= ref < len(resources)
        ):
            issue("RESOURCE_REF_UNAVAILABLE", f"任务引用的资料下标 {ref} 不存在", [key],
                  "从工具返回的 resources 选择合适下标，或填 null 并保留可执行步骤")
    learned, baselined, first_learning = set(), set(), {}
    ancestors = {}
    observed = {
        normalize_kc_name(name) for name, data in (state.get("diagnosis") or {}).items()
        if isinstance(data, dict) and data.get("evidence_state") == "observed"
    }
    mastered = {
        normalize_kc_name(name) for name, data in (state.get("diagnosis") or {}).items()
        if isinstance(data, dict) and data.get("evidence_state") == "observed"
        and data.get("mastery_band") in {"已掌握", "接近掌握"}
    }
    for index, (_day, task) in enumerate(tasks):
        key, phase = task.get("step_key", ""), task.get("learning_phase")
        if not isinstance(key, str):
            key = ""
        raw_dependencies = task.get("depends_on")
        dependencies = [dep for dep in raw_dependencies if isinstance(dep, str)] if isinstance(raw_dependencies, list) else []
        for dependency in dependencies:
            if dependency not in positions:
                issue("DEPENDENCY_MISSING", f"依赖 {dependency} 不存在", [key])
            elif positions[dependency] >= index:
                issue("DEPENDENCY_NOT_EARLIER", f"依赖 {dependency} 必须先于当前任务完成", [key, dependency])
        earlier = {dep for dep in dependencies if dep in positions and positions[dep] < index}
        ancestors[key] = earlier | {value for dep in earlier for value in ancestors.get(dep, ())}
        names = {normalize_kc_name(v) for v in task.get("kc_names") or []}
        if phase == "baseline":
            baselined.update(names)
        elif phase == "learn":
            for name in sorted(names - observed - baselined):
                issue("BASELINE_MISSING", f"未观测知识点 {name} 应先安排基线任务", [key])
            for name in names:
                first_learning.setdefault(name, index)
            learned.update(names)
        elif phase in ("practice", "assessment"):
            for name in sorted(names - learned - mastered):
                issue("PRACTICE_BEFORE_LEARNING", f"练习涉及的 {name} 尚未安排教学", [key])
            if names - mastered and not dependencies:
                issue("PRACTICE_DEPENDENCY_REQUIRED", "练习应明确依赖已完成的教学任务", [key])
            linked_topics = {normalize_kc_name(name) for dep in ancestors[key]
                             if tasks[positions[dep]][1].get("learning_phase") == "learn"
                             for name in tasks[positions[dep]][1].get("kc_names") or []}
            for name in sorted((names & learned) - mastered - linked_topics):
                issue("PRACTICE_DEPENDENCY_MISMATCH", f"练习应依赖 {name} 对应的教学步骤", [key])
        elif phase == "review":
            if not dependencies:
                issue("REVIEW_TARGET_REQUIRED", "复习需引用已经安排的练习或测验任务", [key])
            for dependency in dependencies:
                pos = positions.get(dependency)
                if pos is not None and tasks[pos][1].get("learning_phase") not in ("practice", "assessment", "review"):
                    issue("REVIEW_TARGET_INVALID", "错题复习的来源必须是此前练习或测验", [key, dependency])
    ordered = [normalize_kc_name(v) for v in state.get("kc_order") or []]
    actual = [first_learning[name] for name in ordered if name in first_learning]
    if actual != sorted(actual):
        issue("PREREQUISITE_LEARNING_ORDER", "知识点教学顺序违背服务端提供的先修排序")
    return issues[:24]
