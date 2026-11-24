"""Assemble OpenAI-compatible tool fragments without exposing them as answer text."""

import json


class StreamMetadata:
    def __init__(self):
        self.calls = {}
        self.reasoning = []
        self.finish_reason = None

    def feed(self, chunk):
        if not chunk.choices:
            return
        choice = chunk.choices[0]
        self.finish_reason = getattr(choice, "finish_reason", None) or self.finish_reason
        delta = choice.delta
        thought = getattr(delta, "reasoning_content", None)
        if isinstance(thought, str):
            self.reasoning.append(thought)
        for fragment in getattr(delta, "tool_calls", None) or []:
            call = self.calls.setdefault(fragment.index, {
                "id": "", "type": "function", "function": {"name": "", "arguments": ""},
            })
            call["id"] = getattr(fragment, "id", None) or call["id"]
            function = getattr(fragment, "function", None)
            if function:
                call["function"]["name"] += getattr(function, "name", None) or ""
                call["function"]["arguments"] += getattr(function, "arguments", None) or ""

    @property
    def received(self):
        return bool(self.calls or self.reasoning)

    @property
    def cost_text(self):
        return "".join(self.reasoning) + (json.dumps(self.calls, ensure_ascii=False) if self.calls else "")

    def result_fields(self):
        return {
            "tool_calls": tuple(self.calls[index] for index in sorted(self.calls)),
            "reasoning_content": "".join(self.reasoning) or None,
            "finish_reason": self.finish_reason,
        }
