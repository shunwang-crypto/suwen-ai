"""An unavailable or invalid grader is not evidence that an answer is wrong."""

import math

import orjson
from eduagents_core.gateway import ChatMessage, ChatRequest, Tier
from eduagents_core.logging import get_logger

log = get_logger("grading")


def parse_grade(raw: str) -> tuple[float, str] | None:
    try:
        parsed = orjson.loads(raw)
        if not isinstance(parsed, dict):
            return None
        score = parsed.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        score = float(score)
        if not math.isfinite(score) or not 0 <= score <= 1:
            return None
        reason = parsed.get("reason", "")
        if not isinstance(reason, str):
            return None
        return score, reason[:2000]
    except (ValueError, TypeError, OverflowError):
        return None


async def grade_short(gateway, ctx, question, response: str, *, feature: str, request_id: str = ""):
    try:
        result = await gateway.chat(
            ChatRequest(
                messages=(
                    ChatMessage(
                        role="user",
                        content=(
                            "你是评分助手。对照参考答案给学生作答打分。\n"
                            f"题目: {question.stem}\n参考答案: {question.answer}\n"
                            f"学生作答: {response}\n"
                            '只输出 JSON: {"score": 0到1的小数, "reason": "评分理由"}。'
                            "reason 中的数学表达式必须使用成对的 $...$, 不得把中文正文"
                            "放进公式定界符, 不得裸输出完整 LaTeX 公式。"
                        ),
                    ),
                ),
                tier=Tier.standard,
                json_mode=True,
                feature=feature,
                tenant_id=str(ctx.tenant_id),
                user_id=str(ctx.user_id),
                request_id=request_id,
            )
        )
        return parse_grade(result.text)
    except Exception:  # noqa: BLE001 — preserve the answer, never invent a wrong result
        log.warning("grader_unavailable", feature=feature, request_id=request_id)
        return None
