"""Common deterministic checks for offline plan drafts from every architecture."""

from __future__ import annotations

from eduagents_core.education.plan_checks import plan_issues

CHECKS = ("days", "daily_budget", "course_sources", "learning_order", "observable_outcome")
_GENERIC_CRITERIA = {
    "能够独立完成本任务，并准确解释关键步骤或结论",
    "能够独立完成本任务", "独立完成本任务", "完成任务", "完成练习", "掌握知识点", "学会了", "理解了",
    "correct", "completed", "finish the exercise", "understand the material",
}


def _explicit_criteria(task):
    criterion = task.get("completion_criteria")
    instructions = task.get("instructions")
    return (
        isinstance(criterion, str) and bool(criterion.strip())
        and criterion.strip().rstrip("。.!！").casefold() not in _GENERIC_CRITERIA
        and isinstance(instructions, list) and bool(instructions)
        and all(isinstance(item, str) and item.strip() for item in instructions)
    )


def plan_contracts(case, plan, resources):
    """Validate structure and references. Semantic fit is graded independently."""
    if not isinstance(plan, dict) or not isinstance(plan.get("stages"), list):
        return {name: {"status": "unobserved", "errors": []} for name in CHECKS}

    errors = {name: [] for name in CHECKS}
    days = []
    for stage in plan["stages"]:
        if not isinstance(stage, dict) or not isinstance(stage.get("days"), list):
            errors["days"].append("INVALID_STAGE")
            continue
        days.extend(stage["days"])
    days_shape_ok = all(
        isinstance(day, dict) and isinstance(day.get("tasks"), list)
        and all(isinstance(task, dict) for task in day["tasks"])
        for day in days
    )
    if not days_shape_ok:
        errors["days"].append("INVALID_TASK_STRUCTURE")
        return {name: {"status": "failed" if values else "unobserved", "errors": values}
                for name, values in errors.items()}

    expected = int(case.payload.get("total_days") or 1)
    if len(days) != expected or any(
        type(day.get("day")) is not int or day["day"] != index
        for index, day in enumerate(days, 1)
    ):
        errors["days"].append("DAY_COUNT_OR_NUMBERING")
    budget_minutes = float(case.payload.get("budget_hours_per_day") or 1) * 60
    actual_sources = {source.id: source for source in case.sources if source.text.strip()}
    kb = case.payload.get("kb_id")
    course_bound = case.payload.get("plan_mode") == "course_bound"
    cited = set()
    assessable_tasks = 0
    for day in days:
        tasks = day["tasks"]
        if not tasks:
            errors["days"].append("EMPTY_DAY")
        minutes = [task.get("minutes") for task in tasks]
        if not minutes or any(type(value) is not int or value <= 0 for value in minutes):
            errors["daily_budget"].append("INVALID_MINUTES")
        elif sum(minutes) > budget_minutes + 1e-7:
            errors["daily_budget"].append("DAILY_BUDGET_EXCEEDED")
        for task in tasks:
            ref = task.get("resource_ref")
            if ref is not None:
                entry = resources[ref] if type(ref) is int and 0 <= ref < len(resources) else None
                chunk_id = entry.get("chunk_id") if isinstance(entry, dict) else None
                source = actual_sources.get(chunk_id) if isinstance(chunk_id, str) else None
                if (not course_bound or source is None or entry.get("type") != "kb"
                        or entry.get("kb_id") != kb or entry.get("title", source.title) != source.title):
                    errors["course_sources"].append("UNVERIFIED_RESOURCE_REFERENCE")
                else:
                    cited.add(chunk_id)
            if task.get("learning_phase") not in ("practice", "assessment"):
                continue
            assessable_tasks += 1
            if not _explicit_criteria(task):
                errors["observable_outcome"].append("PRACTICE_WITHOUT_EXPLICIT_CRITERIA")
    if course_bound and actual_sources and not cited:
        errors["course_sources"].append("NO_VERIFIED_COURSE_CITATION")
    if not assessable_tasks:
        errors["observable_outcome"].append("NO_EXPLICIT_ASSESSABLE_TASK")
    if any(
        not isinstance(task.get("kc_names"), list)
        or not task["kc_names"]
        or any(not isinstance(name, str) or not name.strip() for name in task["kc_names"])
        for day in days for task in day["tasks"]
    ):
        errors["learning_order"].append("MISSING_OR_INVALID_TASK_TOPICS")
    else:
        errors["learning_order"].extend(
            issue["code"] for issue in plan_issues({"stages": [{"days": days}]}, case.payload)
        )
    unobserved = {"course_sources"} if course_bound and not actual_sources else set()
    if not days:
        unobserved.update({"daily_budget", "learning_order"})
    return {
        name: {"status": "failed" if values else "unobserved" if name in unobserved else "passed",
               "errors": sorted(set(values))}
        for name, values in errors.items()
    }
