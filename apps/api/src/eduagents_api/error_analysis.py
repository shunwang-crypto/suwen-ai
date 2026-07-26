"""F6-A2 错因自动分类: 对照参考答案判定错误归因 (五分类)。

- 只在**复习作答判错时**调用 (routers/reviews.py), 不改 services.record_attempts 的
  错题创建逻辑 —— 入本仍是保守初值 knowledge_gap, 分类是随后的一次细化;
- cheap 档 + json_mode: 错因是标签任务, 不值得上 standard;
- 网关处于 stub 模式 (dev/CI) 时**不发请求**直接返回 knowledge_gap ——
  stub 输出是固定占位 JSON, 拿它当错因等于伪造分类结果 (§14 反冒充);
- 任何解析/调用异常一律回落 knowledge_gap, 分类失败不能打断复习闭环。
"""

from __future__ import annotations

import orjson
from eduagents_core.gateway import ChatMessage as GwMessage
from eduagents_core.gateway import ChatRequest as GwRequest
from eduagents_core.gateway import Tier
from eduagents_core.logging import get_logger

log = get_logger("error_analysis")

# 错因五分类 (与 models.ErrorItem.cause 注释一致)
CAUSES: tuple[str, ...] = (
    "concept_confusion",  # 概念混淆
    "formula_error",  # 公式用错
    "calc_slip",  # 计算失误
    "misread",  # 审题偏差
    "knowledge_gap",  # 知识缺口
)
DEFAULT_CAUSE = "knowledge_gap"

_PROMPT = """你是学习诊断助手。学生做错了一道题, 请判断最主要的错误原因。

题目: {stem}
选项: {options}
参考答案: {answer}
学生作答: {response}
题目解析: {explanation}

错因只能从以下五类中选一个 (输出英文标识):
- concept_confusion: 概念混淆 (把两个相近概念搞混, 理解方向错了)
- formula_error: 公式用错 (记错/套错公式或定理条件)
- calc_slip: 计算失误 (思路对, 算错了数或符号)
- misread: 审题偏差 (看错条件/问的是什么, 答非所问)
- knowledge_gap: 知识缺口 (完全没学过或毫无思路, 含空作答)

只输出 JSON: {{"cause": "五选一", "reason": "一句话中文说明"}}"""


def _gateway_is_stub(gateway) -> bool:
    """真实 OpenAIGateway 处于 stub 模式才算 stub; 测试注入的 fake 网关 (无 _s) 照常调用。"""
    settings = getattr(gateway, "_s", None)
    return getattr(settings, "gateway_mode", None) == "stub"


async def classify_error_cause(
    gateway,
    question,
    response: str,
    correct_answer: str,
    *,
    tenant_id: str = "",
    user_id: str = "",
    request_id: str = "",
) -> str:
    """返回错因五分类之一; 不可用/不可解析时返回 knowledge_gap (保守初值)。"""
    if gateway is None or _gateway_is_stub(gateway):
        return DEFAULT_CAUSE
    options = getattr(question, "options_json", None) or []
    prompt = _PROMPT.format(
        stem=getattr(question, "stem", ""),
        options="; ".join(str(o) for o in options) or "(无选项)",
        answer=correct_answer,
        response=response.strip() or "(未作答)",
        explanation=(getattr(question, "explanation", "") or "(无解析)")[:800],
    )
    try:
        result = await gateway.chat(
            GwRequest(
                messages=(GwMessage(role="user", content=prompt),),
                tier=Tier.cheap,
                temperature=0.0,
                json_mode=True,
                feature="error_cause",
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
            )
        )
        cause = str(orjson.loads(result.text).get("cause", "")).strip()
    except Exception:  # noqa: BLE001 — 分类失败保守回落, 不打断复习闭环
        log.warning("error_cause_classify_failed", exc_info=True)
        return DEFAULT_CAUSE
    return cause if cause in CAUSES else DEFAULT_CAUSE
