"""Deterministic model substitute for dev/CI native-agent tool tests only."""

import json

from .base import message_text

BANNER = "【开发桩响应 · 非真实模型输出】"


def _object(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def agent_stub(req):
    available = {tool["function"]["name"] for tool in req.tools}
    start, payload = 0, {}
    for i, message in enumerate(req.messages):
        if message.role == "user":
            obj = _object(message_text(message.content))
            if "input" in obj:
                start, payload = i, obj.get("input") or {}
    recent = req.messages[start:]
    used = {call["function"]["name"] for message in recent for call in message.tool_calls}
    results = [_object(message_text(m.content)) for m in recent if m.role == "tool"]
    if results and (
        results[-1].get("status") in {"completed", "awaiting_confirmation", "failed"}
        or results[-1].get("retryable") is False
    ):
        return "finished", ()
    topic = str(payload.get("topic") or payload.get("query") or payload.get("goal") or "学习资料")

    def call(name, args):
        return "", (
            {
                "id": f"stub-{len(req.messages)}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            },
        )

    if req.feature == "agent_ablation_direct":
        task = _object(message_text(req.messages[-1].content)).get("task")
        key = {"qa": "answer", "quiz": "questions", "plan": "plan", "tutor": "reply"}.get(task)
        value = BANNER + "直接生成协议示例，不提供教学质量证据"
        if task == "quiz":
            value = [{"stem": BANNER + f"示例题 {i + 1}", "options": ["A", "B", "C", "D"], "answer": "A"}
                     for i in range(int(payload.get("num_questions") or 2))]
        elif task == "plan":
            value = {"stages": [{"name": BANNER + "协议示例", "days": [{"day": day, "tasks": [{
                "title": BANNER + "确认学习起点", "minutes": round(float(payload.get("budget_hours_per_day") or 1) * 60),
                "kc_names": payload.get("kc_order") or [], "action_type": "practice", "resource_ref": None,
                "objective": BANNER + "确认学习起点", "instructions": [BANNER + "回顾已知内容", "记录待学习主题"],
                "completion_criteria": BANNER + "列出待学习主题", "step_key": f"baseline-{day}",
                "learning_phase": "baseline", "depends_on": [],
            }]} for day in range(1, int(payload.get("total_days") or 1) + 1)]}]}
        return json.dumps({key: value}, ensure_ascii=False), ()

    if req.feature == "agent_plan_coordinator":
        if any("上限" in str(error) for r in results[-1:] for error in r.get("errors", [])):
            return call("report_team_failure", {"message": BANNER + "达到有限返工上限，仍有未解决问题"})
        policy = message_text(req.messages[0].content)
        index = policy.rfind('{"tasks":')
        team_context = _object(policy[index:])
        roster = team_context.get("tasks") or []
        if "publish_plan" in available:
            if any("用户编辑" in str(error) for r in results for error in r.get("errors", [])):
                confirmed_id = team_context.get("confirmed_plan_task_id")
                reviews = [t for t in roster if t["role"] == "reviewer" and confirmed_id in (t.get("input_task_ids") or [])]
                if not reviews:
                    return call("delegate_agents", {"tasks": [{"role": "reviewer", "name": "user-edit-review", "instructions": BANNER + "独立审查用户确认版本", "input_task_ids": [confirmed_id]}]})
                checked = [t for r in results for t in r.get("tasks", []) if t["id"] == reviews[-1]["id"] and t.get("artifact")]
                if not checked:
                    return call("collect_agent_results", {"task_ids": [reviews[-1]["id"]]})
                if not checked[-1]["artifact"].get("approved"):
                    return call("report_team_failure", {"message": BANNER + "用户编辑仍有未解决的审查问题"})
            return call("publish_plan", {})
        active = [t["id"] for t in roster if t["status"] in {"running", "queued"}]
        if active:
            return call("collect_agent_results", {"task_ids": active})
        research = [t for t in roster if t["role"] == "researcher" and t["status"] == "completed"]
        planners = [t for t in roster if t["role"] == "planner" and t["status"] == "completed"]
        reviewers = [t for t in roster if t["role"] == "reviewer" and t["status"] == "completed"]
        if any(t["status"] in {"failed", "cancelled"} for t in roster):
            return call("report_team_failure", {"message": BANNER + "子任务没有完成"})
        if not research:
            return call("delegate_agents", {"tasks": [{"role": "researcher", "name": "research", "instructions": BANNER + "收集学习资料", "input_task_ids": []}]})
        if not planners:
            return call("delegate_agents", {"tasks": [{"role": "planner", "name": "planner", "instructions": BANNER + "制定可执行计划", "input_task_ids": [research[0]["id"]]}]})
        if not reviewers or planners[-1]["id"] not in (reviewers[-1].get("input_task_ids") or []):
            return call("delegate_agents", {"tasks": [{"role": "reviewer", "name": f"reviewer-{len(reviewers)+1}", "instructions": BANNER + "独立审查计划", "input_task_ids": [planners[-1]["id"]]}]})
        collected = [t for r in results for t in r.get("tasks", []) if t.get("id") == reviewers[-1]["id"] and t.get("artifact")]
        if not collected:
            return call("collect_agent_results", {"task_ids": [reviewers[-1]["id"]]})
        if not collected[-1]["artifact"].get("approved"):
            return call("delegate_agents", {"tasks": [{"role": "planner", "name": f"planner-{len(planners)+1}", "instructions": BANNER + "按独立审查反馈修订", "input_task_ids": [research[0]["id"], planners[-1]["id"], reviewers[-1]["id"]]}]})
        return call("submit_reviewed_plan", {"draft_task_id": planners[-1]["id"], "review_task_id": reviewers[-1]["id"]})
    if req.feature == "agent_researcher" and "read_learning_context" not in used:
        return call("read_learning_context", {})
    if req.feature == "agent_planner":
        if "read_assignment" not in used:
            return call("read_assignment", {})
        if "read_learning_context" not in used:
            return call("read_learning_context", {})
        context = next((r for r in results if "kc_order" in r), {})
        names = context.get("kc_order") or [topic]
        count = max(1, int(payload.get("total_days") or 1))
        budget = max(3, int(float(payload.get("budget_hours_per_day") or 1) * 60))
        def item(key, phase, deps, minutes):
            return {"step_key": key, "learning_phase": phase, "depends_on": deps,
                    "title": BANNER + phase + " " + topic, "objective": "检验并理解本轮知识点",
                    "instructions": ["根据资料完成当前步骤", "用一个例子检验学习结果"],
                    "completion_criteria": "能独立解释步骤并完成一个例子", "minutes": minutes,
                    "kc_names": names, "resource_ref": None,
                    "action_type": "tutor" if phase == "learn" else "practice"}
        first = [item("baseline", "baseline", [], max(1, budget // 5)),
                 item("learn", "learn", ["baseline"], max(1, budget // 2)),
                 item("practice-1", "practice", ["learn"], max(1, budget - budget // 5 - budget // 2))]
        days = [{"day": 1, "tasks": first}]
        days.extend({"day": day, "tasks": [item(f"practice-{day}", "practice", ["learn"], budget)]} for day in range(2, count + 1))
        return call("submit_plan_draft", {"goal_outline": {"key_topics": names}, "plan": {"stages": [{"name": BANNER + "学习", "days": days}]}})
    if req.feature == "agent_reviewer":
        if "read_review_context" not in used:
            return call("read_review_context", {})
        review_context = next((r for r in results if "program_issues" in r), {})
        issues = review_context.get("program_issues") or []
        from ..education.plan_checks import CHECKS
        return call("submit_plan_review", {"approved": not issues,
                    "checks": {key: "fail" if issues else "pass" for key in CHECKS},
                    "issues": issues, "summary": BANNER + "确定性检查完成；不代表真实教育质量"})
    if (
        req.feature == "agent_quiz"
        and "submit_blueprint" in available
        and "submit_blueprint" not in used
    ):
        kcs = payload.get("kc_names") or [topic]
        count = int(payload.get("num_questions") or 5)
        mix = payload.get("difficulty_mix") or {}
        levels = [
            level for level in ("easy", "mid", "hard") for _ in range(int(mix.get(level) or 0))
        ]
        items = [
            {
                "kc_name": kcs[i % len(kcs)],
                "type": "single",
                "difficulty": levels[i] if i < len(levels) else "mid",
            }
            for i in range(count)
        ]
        return call("submit_blueprint", {"blueprint": {"items": items, "summary": BANNER + topic}})
    search = "web_search" if payload.get("requested_route") == "web" else "search_knowledge_base"
    if search not in available:
        search = "web_search"
    if search in available and search not in used:
        return call(search, {"queries": [topic]})
    if req.feature == "agent_researcher":
        sources = [source for result in results for source in result.get("sources", [])]
        return call("submit_research_report", {"summary": BANNER + "资料收集完成", "source_ids": list(dict.fromkeys(s["id"] for s in sources if s.get("id")))[:8]})
    if req.feature == "agent_qa":
        sources = [source for result in results for source in result.get("sources", [])]
        return call(
            "submit_answer",
            {
                "answer": BANNER + "\n学习资料中的相关说明。",
                "source_ids": list(dict.fromkeys(s.get("id") for s in sources if s.get("id")))[:8],
                "followups": ["需要一个例子吗？"],
            },
        )
    if req.feature == "agent_quiz":
        blueprint = next(
            (r.get("blueprint") for r in results if isinstance(r.get("blueprint"), dict)), None
        )
        if blueprint is None:
            # The current host policy contains the authoritative confirmed blueprint.
            policy = message_text(req.messages[0].content)
            index = policy.rfind('{"blueprint":')
            blueprint = _object(policy[index:]).get("blueprint") or {}
        items = blueprint.get("items") or [
            {"kc_name": topic, "type": "single", "difficulty": "mid"}
            for _ in range(int(payload.get("num_questions") or 5))
        ]
        questions = [
            {
                "type": item["type"],
                "stem": f"{BANNER} {item['kc_name']} 基础练习 {i + 1}",
                "options": ["示例一", "示例二", "示例三", "示例四"]
                if item["type"] in {"single", "multi"}
                else [],
                "answer": "A" if item["type"] in {"single", "multi"} else "示例答案",
                "explanation": BANNER + "示例解析",
                "kc_names": [item["kc_name"]],
                "difficulty": item["difficulty"],
            }
            for i, item in enumerate(items)
        ]
        return call("submit_quiz", {"questions": questions})
    if req.feature == "agent_plan":
        if "publish_plan" in available:
            return call("publish_plan", {})
        if "read_learning_context" not in used:
            return call("read_learning_context", {})
        if "analyze_learning_goal" in available and "analyze_learning_goal" not in used:
            return call(
                "analyze_learning_goal", {"goal_outline": {"subject": topic, "key_topics": [topic]}}
            )
        minutes = max(1, round(float(payload.get("budget_hours_per_day") or 1) * 60))
        days = max(1, int(payload.get("total_days") or 1))
        return call(
            "submit_plan_draft",
            {
                "goal_outline": {"subject": topic, "key_topics": [topic]},
                "plan": {
                    "stages": [
                        {
                            "name": BANNER + "学习阶段",
                            "days": [
                                {
                                    "day": day,
                                    "tasks": [
                                        {
                                            "title": "基线练习",
                                            "minutes": minutes,
                                            "kc_names": [topic],
                                            "action_type": "practice",
                                            "resource_ref": None,
                                        }
                                    ],
                                }
                                for day in range(1, days + 1)
                            ],
                        }
                    ]
                },
            },
        )
    if req.feature == "agent_tutor":
        user_text = " ".join(str(m.get("content", "")) for m in payload.get("messages") or [])
        if "assess_student_turn" not in used:
            return call(
                "assess_student_turn",
                {
                    "stuck_type": "unknown",
                    "asked_for_answer": "答案" in user_text,
                    "solved": "懂了" in user_text,
                },
            )
        return call(
            "submit_tutor_reply",
            {
                "reply": BANNER + "\n你能先说说自己的思路吗？",
                "summary": {"misconceptions": [], "review_suggestions": ["用一道练习检验理解"]},
            },
        )
    return "finished", ()
