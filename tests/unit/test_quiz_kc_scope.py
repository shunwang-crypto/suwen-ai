"""Quiz generation must keep generated KC names inside the requested scope."""

from __future__ import annotations

import json

from eduagents_core.config import Settings
from eduagents_core.gateway.base import ChatResult
from eduagents_core.harness.workflow import MemoryStore
from eduagents_core.workflows.quiz import build_quiz_workflow
from eduagents_core.workflows.state import WorkflowDeps


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        if request.feature == "quiz_blueprint":
            text = json.dumps(
                {
                    "items": [
                        {"kc_name": "章节 A", "type": "single", "difficulty": "mid"}
                    ],
                    "summary": "范围测试",
                },
                ensure_ascii=False,
            )
        else:
            text = json.dumps(
                {
                    "questions": [
                        {
                            "type": "single",
                            "stem": "测试题",
                            "options": ["A", "B", "C", "D"],
                            "answer": "A",
                            "explanation": "测试解析",
                            "kc_names": ["不在白名单"],
                            "difficulty": "mid",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        return ChatResult(
            text=text,
            model="fake",
            tier=request.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0.0,
            latency_ms=0,
            mode="stub",
        )


async def test_generated_kc_must_match_target_scope_after_retries():
    gateway = _Gateway()
    app = build_quiz_workflow(
        WorkflowDeps(settings=Settings(), gateway=gateway)
    ).with_store(MemoryStore())

    final = await app.ainvoke(
        {
            "tenant_id": "tenant",
            "user_id": "user",
            "request_id": "request",
            "thread_id": "quiz:tenant:user:scope",
            "confirmation_mode": "auto",
            "topic": "章节 A",
            "num_questions": 1,
            "difficulty_mix": {"easy": 0, "mid": 1, "hard": 0},
            "kc_names": ["章节 A"],
        },
        {"configurable": {"thread_id": "scope"}},
    )

    assert final["terminal_outcome"] == "validation_failed"
    assert final["ever_generated_nonempty"] is True
    assert any("KC 白名单" in error for error in final["validation_errors"])

