"""Educational domain policies and tools for the native agent runtime."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from ..gateway.base import (
    BudgetExceeded,
    Tier,
)
from ..logging import get_logger
from ..moderation import get_moderator, minor_system_guard
from ..retrieval.sanitize import sanitize_retrieved_text
from .context import QaState
from .prompts import load_review_prompt
from .toolkit import STRINGS, TEXT, EducationTools, schema

log = get_logger("qa_tools")

_MAX_WEB_RESULTS = 14

# 触发 web 补充倾向的时效性/搜索意图关键词 (规则路由, 命中即不调 LLM)

# direct 路径普通助教 prompt。文案对齐 apps/api routers/chat.py 的 _SYSTEM_PROMPT;
# core 不得反向依赖 apps, 故此处内联同款文案。
DIRECT_SYSTEM_PROMPT = (
    "你是苏问 AI 课堂的 AI 助教，面向自学者与学生。"
    "回答准确、结构清晰、循序渐进；所有数学表达式必须使用 LaTeX：行内公式严格写成 `$...$`，"
    "块公式严格写成 `$$...$$` 且两个 `$$` 各自独占一行。不要省略闭合定界符，"
    "不要把中文正文放进公式定界符。"
    "不确定的内容明确说明不确定，不要编造。"
)

# 拒答文案对齐 apps/api routers/chat.py 的 _NO_EVIDENCE_ANSWER (敢拒答是产品和玩具的分界)
NO_EVIDENCE_ANSWER = (
    "知识库中没有找到与该问题相关的依据。\n\n"
    "建议：换一种问法、确认文档已完成解析（状态为 ready），"
    "或在不绑定知识库的通用模式下提问。"
)

WEB_NO_RESULTS_ANSWER = "网页搜索暂未返回可用结果。请换一种问法或稍后重试。"

UNSUPPORTED_DISCLAIMER = "⚠️ 以下回答部分内容未能在知识库中找到直接依据"
UNSUPPORTED_WEB_DISCLAIMER = "⚠️ 以下回答部分内容未能在网页资料中找到直接依据"


# 画像注入 (F8-A2 "新会话主动关联历史卡点" + "回答深度由画像调制")。
# 摘要文本由 API 层 profile_summary.py 确定性拼装 (不经 LLM), 空串即不注入。
PROFILE_SECTION_TEMPLATE = (
    "\n\n## 学生画像（来自该学生的历史作答与掌握度记录，非本次提问内容）\n"
    "{{PROFILE}}\n"
    "【个性化指令】若本次问题涉及该学生的薄弱点，主动点明并关联其历史卡点"
    "（如“这正是你之前在 X 上失分的地方”）；回答深度按其掌握程度调整：薄弱知识点"
    "从基础与前置概念讲起并补充例子，已掌握的部分点到为止不赘述。"
    "画像仅用于调整讲法，不得替代资料作为事实依据，也不要罗列画像原文。"
    "画像含兴趣标签时，举例与应用情境可自然贴合其兴趣（用其熟悉的场景讲概念），"
    "不得生硬堆砌兴趣词，也不得因此改变知识点本身。"
)


def _normalize_web_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                parsed.query,
                "",
            )
        )
    except ValueError:
        return url.strip()


def _dedupe_web_results(results: list[dict], limit: int = _MAX_WEB_RESULTS) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for item in results:
        url = str(item.get("url", ""))
        key = _normalize_web_url(url) or f"{item.get('title', '')}\n{item.get('content', '')}"
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def moderate_web_results(state: QaState, raw_results: list[dict]) -> tuple[list[dict], int]:
    # 外部内容过审 (P0-5): web 结果与模型输出同标准 (check_output), 命中整条丢弃 —
    # sanitize 只防指令注入, 不防违法/成人内容被"洗"成带引用的可信答案。
    moderator = get_moderator()
    strict = bool(state.get("minor"))
    results = []
    dropped = 0
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        item = {
            "title": str(r.get("title", "")),
            "url": str(r.get("url", "")),
            "content": str(r.get("content", "")),
        }
        verdict = moderator.check_output(f"{item['title']}\n{item['content']}", strict=strict)
        if not verdict.allowed:
            dropped += 1
            log.warning(
                "qa_web_result_moderated",
                url=item["url"],
                category=verdict.category,
                matched=verdict.matched,
            )
            continue
        item["content"], _ = sanitize_retrieved_text(item["content"][:12000])
        item["title"], _ = sanitize_retrieved_text(item["title"])
        results.append(item)
    return results, dropped


class QuestionAnswerTools(EducationTools):
    """Evidence gathering and checked answer submission, selected by the agent."""

    def __init__(self, deps, record, emit):
        super().__init__(deps, record, emit)
        self.tier = Tier.strong if self.state.get("answer_tier") == "strong" else Tier.standard
        self.add_learning_context()
        self.add_evidence_tools()
        self.add(
            schema(
                "submit_answer",
                "提交最终回答、已检索来源 id 和至多三个追问建议；校验失败后修正。",
                {"answer": TEXT, "source_ids": STRINGS, "followups": STRINGS},
                ("answer",),
            ),
            self.submit,
        )
        self._submissions = 0

    def prepare_turn(self):
        for key in ("evidences", "web_results", "web_queries", "followups"):
            self.state[key] = []
        self.state["refused"] = False
        self.state["kb_searched"] = False
        if self.state.get("profile_summary"):
            self.trace(
                type="profile",
                weak_points=self.state.get("profile_weak_points") or [],
                chars=len(self.state["profile_summary"]),
            )

    def policy(self):
        prompt = self.teaching_policy("qa") + super().policy() + DIRECT_SYSTEM_PROMPT
        prompt += "引用在正文标注 [来源 N]，N 与 submit_answer 的 source_ids 列表顺序一致，只列实际支撑结论的资料。"
        if self.state.get("profile_summary"):
            prompt += PROFILE_SECTION_TEMPLATE.replace(
                "{{PROFILE}}", self.state["profile_summary"][:3000]
            )
        if self.state.get("requested_route") == "web":
            prompt += "用户明确要求联网，必须先调用 web_search，不能用知识库代替联网。"
        elif self.state.get("kb_id") or self.state.get("kb_ids"):
            prompt += "先检索授权知识库；依据不足时可补充网页或明确说明资料不足。"
        if self.state.get("minor"):
            prompt += minor_system_guard()
        return prompt

    async def submit(self, args):
        answer = str(args.get("answer") or "").strip()
        if not answer:
            return {"accepted": False, "errors": ["回答不能为空"]}
        self._submissions += 1
        evidence = self.state.get("evidences") or []
        web = self.state.get("web_results") or []
        available = {item["chunk_id"] for item in evidence} | {item["url"] for item in web}
        ids = list(dict.fromkeys(args.get("source_ids") or []))
        if any(value not in available for value in ids):
            return {"accepted": False, "errors": ["来源必须来自本任务检索结果"]}
        explicit_web = self.state.get("requested_route") == "web"
        bound = bool(self.state.get("kb_id") or self.state.get("kb_ids"))
        if explicit_web and not self.state.get("web_queries"):
            return {"accepted": False, "errors": ["请先实际调用网页搜索"]}
        if bound and not evidence and not web:
            if "search_knowledge_base" not in self.handlers or self.state.get("kb_searched"):
                answer = NO_EVIDENCE_ANSWER
                self.state["refused"] = True
            else:
                return {"accepted": False, "errors": ["请先检索授权知识库"]}
        if explicit_web and not web:
            answer = WEB_NO_RESULTS_ANSWER
            self.state["refused"] = True
        if available and not ids and not self.state["refused"]:
            return {"accepted": False, "errors": ["请附上实际引用的来源 id"]}
        if available and not self.state["refused"]:
            sources = [
                {"text": item["text"], "heading_path": item["heading_path"]}
                for item in evidence
                if item["chunk_id"] in ids
            ]
            sources.extend(
                {"text": item["content"], "heading_path": item["title"]}
                for item in web
                if item["url"] in ids
            )
            try:
                checked = await self.review(
                    "qa_verify_support",
                    load_review_prompt("qa"),
                    {"sources": sources, "answer": answer},
                )
            except BudgetExceeded:
                raise
            except Exception:
                checked = None
            if checked and checked.get("supported") is False:
                if self._submissions < 3:
                    return {
                        "accepted": False,
                        "errors": checked.get("unsupported_claims") or ["回答缺少来源支撑"],
                    }
                answer += "\n\n" + (UNSUPPORTED_WEB_DISCLAIMER if web else UNSUPPORTED_DISCLAIMER)
            elif not checked or checked.get("supported") is not True:
                answer += "\n\n回答依据暂未完成核验，请结合引用原文确认。"
        self.state.update(
            answer=answer,
            source_ids=ids,
            route="web" if web or explicit_web else "kb" if evidence or bound else "direct",
            followups=list(
                dict.fromkeys(v.strip()[:120] for v in (args.get("followups") or []) if v.strip())
            )[:3],
        )
        self.record.status = "completed"
        self.trace(type="generate", path=self.state["route"], regen=False)
        self.emit({"kind": "token", "text": answer, "attempt": 0})
        return {"accepted": True, "status": "completed"}
