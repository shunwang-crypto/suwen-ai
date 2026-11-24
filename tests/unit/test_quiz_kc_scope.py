"""Quiz generation must keep generated KC names inside the requested scope."""

from __future__ import annotations

import json

from eduagents_core.education.quiz import validate_questions
from eduagents_core.gateway.base import ChatResult


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        if request.feature == "quiz_blueprint":
            text = json.dumps(
                {
                    "items": [{"kc_name": "章节 A", "type": "single", "difficulty": "mid"}],
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


def test_generated_kc_must_match_target_scope_after_bounded_retries():
    state = {
        "num_questions": 1,
        "kc_names": ["章节 A"],
        "ever_generated_nonempty": True,
        "questions": [
            {
                "type": "single",
                "stem": "题目",
                "options": ["A", "B", "C", "D"],
                "answer": "A",
                "explanation": "解析",
                "difficulty": "mid",
                "kc_names": ["越界"],
            }
        ],
    }
    for _ in range(3):
        state.update(validate_questions(state))
    assert state["terminal_outcome"] == "validation_failed"
    assert any("KC 白名单" in e for e in state["validation_errors"])
