"""Translate deterministic scenario artifacts into native model tool calls.

Only test fixtures use this adapter. DSH still selects/dispatches the actual
registered tools; production has no phase executor or legacy builders.
"""

import json
from dataclasses import replace

from eduagents_core.gateway.agent_stub import agent_stub
from eduagents_core.gateway.base import ChatResult


async def native_fixture_reply(fixture, req):
    text, calls = agent_stub(req)
    if calls:
        function = calls[0]["function"]
        name = function["name"]
        args = json.loads(function["arguments"])
        feature = {
            "submit_blueprint": "quiz_blueprint",
            "submit_quiz": "quiz_generate",
            "submit_answer": "qa_generate",
            "submit_tutor_reply": "tutor_respond",
            "assess_student_turn": "tutor_classify",
            "submit_plan_draft": "plan_draft",
            "analyze_learning_goal": "plan_parse_goal",
        }.get(name)
        if feature:
            result = await fixture.chat(replace(req, feature=feature, tools=(), json_mode=True))
            try:
                payload = json.loads(result.text)
            except (TypeError, ValueError):
                payload = None
            if name == "submit_blueprint":
                args = {"blueprint": payload if isinstance(payload, dict) else {}}
            elif name == "submit_quiz":
                args = {"questions": (payload or {}).get("questions", [])}
            elif name == "submit_answer":
                args["answer"] = result.text
            elif name == "assess_student_turn":
                args = payload if isinstance(payload, dict) else {"stuck_type": "unknown"}
            elif name == "submit_tutor_reply":
                args["reply"] = result.text
                if any(
                    m.role == "tool" and '"solved":true' in m.content.replace(" ", "")
                    for m in req.messages[
                        max(i for i, m in enumerate(req.messages) if m.role == "user") :
                    ]
                ):
                    summary = await fixture.chat(replace(req, feature="tutor_summarize", tools=()))
                    try:
                        args["summary"] = json.loads(summary.text)
                    except ValueError:
                        pass
            elif name == "analyze_learning_goal":
                args = {"goal_outline": payload or {}}
            elif name == "submit_plan_draft":
                outline = await fixture.chat(replace(req, feature="plan_parse_goal", tools=()))
                try:
                    args["goal_outline"] = json.loads(outline.text)
                except ValueError:
                    args["goal_outline"] = {}
                args["plan"] = payload or {}
            function["arguments"] = json.dumps(args, ensure_ascii=False)
    return ChatResult(text, "fixture", "standard", 1, 1, 0, 0, "stub", tool_calls=calls)


def blueprint_for_fixture(req):
    policy = req.messages[0].content
    return json.loads(policy[policy.rfind('{"blueprint":') :])["blueprint"]["items"]
