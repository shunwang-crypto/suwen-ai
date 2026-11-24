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
