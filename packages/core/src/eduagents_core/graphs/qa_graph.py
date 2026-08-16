"""qa 子图 — Agentic RAG 自省环 (§5.3)。

router(规则先行, 命中即不调 LLM) → query_plan(多查询改写) → retrieve(多查询 RRF 融合)
  → 置信度门控(confident 快路径跳过 grade) → grade(cheap 批量相关性)
  → rewrite 重检环(retry_count 硬上限 2) → web_fallback → generate → verify_support(kb 二次校验)
  → followups(cheap 追问建议, 失败降级为空)
全路径 trace 事件可观测 (API 层转发为 SSE trace_event); 检索/网络文本经 sanitize
过滤指令性内容 (§8 第二层防线)。deps 由构图闭包捕获, 不进 state (state 需可序列化)。

图内真流式 (包A): generate 经 langgraph get_stream_writer() 逐 token 写自定义流,
事件形如 {"type":"token","text":...,"attempt":n} (attempt=重生成序号, verify 触发
二次生成时前端按 attempt 清空重排, 防拼接双答案)。ainvoke 场景 writer 为 no-op。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from ..gateway.base import (
    BudgetExceeded,
    ChatMessage,
    ChatRequest,
    GatewayError,
    StreamDelta,
    Tier,
)
from ..json_utils import parse_json_object
from ..logging import get_logger
from ..moderation import get_moderator, minor_system_guard
from ..retrieval.base import rrf_fuse
from ..retrieval.sanitize import sanitize_retrieved_text
from .state import GraphDeps, QaState

log = get_logger("qa_graph")

# 本文件位于 packages/core/src/eduagents_core/graphs/ -> parents[5] 为仓库根
_PROMPTS_DIR = Path(__file__).resolve().parents[5] / "prompts"
_MAX_RETRY = 2  # 查询改写重检硬上限 (§5.3)
_EVIDENCE_SNIPPET_CHARS = 600  # grade/verify 摘要单条截断长度
_MAX_PLAN_QUERIES = 3  # 原查询 + 至多 2 个改写
_MAX_WEB_QUERIES = 3
_MAX_WEB_RESULTS = 10
_WEB_HISTORY_MESSAGES = 6
_WEB_HISTORY_CHARS = 500
_MAX_FOLLOWUPS = 3  # 追问 chips 数量上限
_FOLLOWUP_ANSWER_CHARS = 1500  # 追问 prompt 中回答的截断长度 (控 cheap 档上下文)
_TOKEN_FALLBACK_CHUNK = 24  # 仅实现 chat 的网关 (如测试 Fake) 整段取回后的分块写出粒度

# 触发 web 补充倾向的时效性/搜索意图关键词 (规则路由, 命中即不调 LLM)
_WEB_KEYWORDS = ("最新", "今年", "新闻", "搜索", "近期", "联网")

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

STRICT_REGEN_INSTRUCTION = (
    "\n\n【强化指令】只使用资料中明确内容作答；资料未明确覆盖的论断一律删去，"
    "宁可说明知识库资料不足，也不要推测或补充资料之外的内容。"
)

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


class _QaGraphState(QaState, total=False):
    """构图私有扩展通道: 多查询计划与 verify 重生成控制, 不进 QaState 公共契约。"""

    queries: list[str]  # query_plan 产出的检索查询列表 (rewrite 后重置为单条)
    web_queries: list[str]  # 结合对话上下文消解后的独立网页搜索词
    regen_count: int  # verify 不支持触发的重生成次数 (上限 1 次)
    verify_action: str  # verify_support 的路由信号: regen | end


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _format_sources(evidences: list[dict]) -> str:
    """evidences -> <source> 包裹块 (与 chat.py 的防注入第一层约定一致)。"""
    parts = []
    for i, ev in enumerate(evidences, 1):
        parts.append(
            f'<source idx="{i}" doc="{ev.get("doc_id", "")}" '
            f'loc="{ev.get("heading_path", "")}" page="{ev.get("page_start") or ""}">\n'
            f"{ev.get('text', '')}\n</source>"
        )
    return "\n\n".join(parts)


def _format_web_results(web_results: list[dict]) -> str:
    """web 结果 -> <source> 包裹块; 内容同样过 sanitize (网页文本同属不可信输入)。"""
    parts = []
    for i, r in enumerate(web_results, 1):
        content, hits = sanitize_retrieved_text(str(r.get("content", "")))
        if hits:
            log.warning("qa_web_text_sanitized", url=r.get("url", ""), patterns=hits)
        parts.append(
            f'<source idx="{i}" title="{r.get("title", "")}" url="{r.get("url", "")}">\n'
            f"{content}\n</source>"
        )
    return "\n\n".join(parts)


def _web_history(messages: list[dict], query: str) -> str:
    """仅保留最近对话供搜索词消歧，限制长度避免规划调用膨胀。"""
    lines: list[str] = []
    for message in messages[-_WEB_HISTORY_MESSAGES:]:
        role = message.get("role")
        content = str(message.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content or content == query:
            continue
        label = "用户" if role == "user" else "助教"
        lines.append(f"{label}: {content[:_WEB_HISTORY_CHARS]}")
    return "\n".join(lines) or "（无历史对话）"


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


def _evidence_digest(evidences: list[dict]) -> str:
    """grade/verify 用的编号摘要 (单条截断, 控 cheap 档上下文)。"""
    lines = []
    for i, ev in enumerate(evidences, 1):
        text = str(ev.get("text", ""))[:_EVIDENCE_SNIPPET_CHARS]
        lines.append(f"[{i}] ({ev.get('heading_path', '')}) {text}")
    return "\n".join(lines)


def _rule_route(query: str, has_kb: bool, web_ok: bool) -> tuple[str, str] | None:
    """规则路由: 命中返回 (route, reason), 未命中返回 None 交 LLM 兜底。

    唯一的模糊场景: 查询有联网意图但 web_search 不可用 (kb/direct 二选一交 LLM)。
    """
    if any(kw in query for kw in _WEB_KEYWORDS):
        if web_ok:
            return "web", "规则: 查询含时效性/搜索意图关键词"
        return None
    if has_kb:
        return "kb", "规则: 已绑定知识库, 默认知识库问答"
    return "direct", "规则: 未绑定知识库且无时效性需求"


def build_qa_graph(deps: GraphDeps) -> StateGraph:
    """构建 qa 子图 (未编译)。deps 经闭包捕获, 不进 state。"""

    async def _chat_json(
        state: _QaGraphState, *, feature: str, prompt: str, temperature: float = 0.2
    ) -> dict[str, Any] | None:
        """cheap 档 json_mode 辅助调用; 网关错误与解析失败统一返回 None (调用方降级)。"""
        req = ChatRequest(
            messages=(ChatMessage(role="user", content=prompt),),
            tier=Tier.cheap,
            temperature=temperature,
            json_mode=True,
            feature=feature,
            tenant_id=state.get("tenant_id", ""),
            user_id=state.get("user_id", ""),
            request_id=state.get("request_id", ""),
        )
        try:
            result = await deps.gateway.chat(req)
        except GatewayError as exc:
            if isinstance(exc, BudgetExceeded):
                # P1-9: 预算熔断必须冒头到 API 层转 429 (SSE 发 BUDGET_EXCEEDED),
                # 不许当解析失败静默降级继续跑后续节点烧钱
                raise
            log.warning("qa_llm_call_failed", feature=feature, error=str(exc))
            return None
        return parse_json_object(result.text)

    # ---- 节点 ----

    async def router(state: _QaGraphState) -> dict:
        query = state.get("query", "")
        has_kb = bool(state.get("kb_id")) and deps.retrieval is not None
        web_ok = deps.web_search is not None
        requested = state.get("requested_route", "")
        if requested == "web" and web_ok:
            return {
                "route": "web",
                "route_reason": "用户显式选择网页搜索",
                "trace": [
                    {
                        "type": "router",
                        "route": "web",
                        "reason": "用户显式选择网页搜索",
                        "rule_hit": True,
                    }
                ],
            }
        if requested == "kb" and has_kb:
            return {
                "route": "kb",
                "route_reason": "用户显式选择知识库",
                "trace": [
                    {
                        "type": "router",
                        "route": "kb",
                        "reason": "用户显式选择知识库",
                        "rule_hit": True,
                    }
                ],
            }
        ruled = _rule_route(query, has_kb, web_ok)
        if ruled is not None:
            route, reason = ruled
            return {
                "route": route,
                "route_reason": reason,
                "trace": [
                    {"type": "router", "route": route, "reason": reason, "rule_hit": True}
                ],
            }
        prompt = (
            _load_prompt("qa_router.md")
            .replace("{{QUERY}}", query)
            .replace("{{HAS_KB}}", "是" if has_kb else "否")
            .replace("{{WEB_AVAILABLE}}", "是" if web_ok else "否")
        )
        data = await _chat_json(state, feature="qa_router", prompt=prompt)
        allowed = {"direct"} | ({"kb"} if has_kb else set()) | ({"web"} if web_ok else set())
        if data is not None and data.get("route") in allowed:
            route = str(data["route"])
            reason = str(data.get("reason") or "LLM 路由")
        else:
            # 解析失败降级: 有知识库默认 kb, 否则 direct
            route = "kb" if has_kb else "direct"
            reason = "LLM 路由解析失败, 按默认路径降级"
        return {
            "route": route,
            "route_reason": reason,
            "trace": [{"type": "router", "route": route, "reason": reason, "rule_hit": False}],
        }

    async def query_plan(state: _QaGraphState) -> dict:
        query = state.get("query", "")
        # UI 的“极速”是显式延迟策略，而非下方由检索置信度决定的 fast_path。
        # 这里只短路首轮多查询 LLM 规划；retrieve 后的 grade 置信度门控、rewrite
        # 自省环和生成后的 verify 均保持不变。
        response_mode = state.get("response_mode")
        if response_mode == "fast" and state.get("route") == "kb":
            return {
                "queries": [query],
                "trace": [
                    {
                        "type": "query_plan",
                        "queries": [query],
                        "degraded": False,
                        "skipped": True,
                        "mode": "fast",
                        "reason": "fast_kb_original_query",
                    }
                ],
            }
        prompt = _load_prompt("qa_query_plan.md").replace("{{QUERY}}", query)
        data = await _chat_json(state, feature="qa_query_plan", prompt=prompt)
        queries = [query]
        degraded = True
        if data is not None and isinstance(data.get("queries"), list):
            degraded = False
            for item in data["queries"]:
                if isinstance(item, str) and item.strip() and item.strip() not in queries:
                    queries.append(item.strip())
            queries = queries[:_MAX_PLAN_QUERIES]
        return {
            "queries": queries,
            "trace": [{"type": "query_plan", "queries": queries, "degraded": degraded}],
        }

    async def web_query_plan(state: _QaGraphState) -> dict:
        """显式联网时结合最近对话消解指代，产出可独立搜索的查询。"""
        query = state.get("query", "")
        if state.get("requested_route") != "web":
            return {
                "web_queries": [query],
                "trace": [
                    {
                        "type": "web_query_plan",
                        "queries": [query],
                        "degraded": False,
                    }
                ],
            }
        prompt = (
            _load_prompt("qa_web_query_plan.md")
            .replace("{{HISTORY}}", _web_history(state.get("messages") or [], query))
            .replace("{{QUERY}}", query)
        )
        data = await _chat_json(state, feature="qa_web_query_plan", prompt=prompt)
        queries: list[str] = []
        if data is not None and isinstance(data.get("queries"), list):
            for item in data["queries"]:
                if isinstance(item, str) and item.strip() and item.strip() not in queries:
                    queries.append(item.strip())
        degraded = not queries
        if degraded:
            queries = [query]
        queries = queries[:_MAX_WEB_QUERIES]
        return {
            "web_queries": queries,
            "trace": [
                {"type": "web_query_plan", "queries": queries, "degraded": degraded}
            ],
        }

    async def retrieve(state: _QaGraphState) -> dict:
        # Wave3 联合检索: kb_ids/owner_ids 存在时多库单次查询 (Qdrant MatchAny);
        # 旧单库路径 (仅 kb_id/tenant_id) 原样兼容
        tenant_ref = state.get("owner_ids") or state.get("tenant_id", "")
        kb_ref = state.get("kb_ids") or state.get("kb_id", "")
        queries = state.get("queries") or [state.get("query", "")]
        results = []
        errors = 0
        for q in queries:
            try:
                results.append(await deps.retrieval.retrieve(tenant_ref, kb_ref, q))
            except Exception:  # noqa: BLE001 — 单查询失败不拖垮整轮检索
                log.exception("qa_retrieve_failed")
                results.append(None)
                errors += 1
        valid = [r for r in results if r is not None]
        # chunk_id 去重合并, 同 id 保留最高分实例
        chunk_map: dict[str, Any] = {}
        for r in valid:
            for c in r.chunks:
                prev = chunk_map.get(c.chunk_id)
                if prev is None or c.score > prev.score:
                    chunk_map[c.chunk_id] = c
        if len(valid) > 1:
            # 多查询结果用 RRF 融合排序 (retrieval.base.rrf_fuse, k=60)
            fused = rrf_fuse([[c.chunk_id for c in r.chunks] for r in valid])
            ordered_ids = sorted(chunk_map, key=lambda cid: -fused.get(cid, 0.0))
        else:
            ordered_ids = list(
                dict.fromkeys(c.chunk_id for r in valid for c in r.chunks)
            )
        evidences = []
        sanitized_count = 0
        for cid in ordered_ids:
            c = chunk_map[cid]
            # 防注入第二层 (§8): 指令性内容在进入任何 LLM 上下文之前过滤
            clean_text, hits = sanitize_retrieved_text(c.text)
            if hits:
                sanitized_count += 1
                log.warning(
                    "qa_retrieved_text_sanitized", chunk_id=c.chunk_id, patterns=hits
                )
            evidences.append(
                {
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "kb_id": (c.payload or {}).get("kb_id", ""),  # 联合检索: 标注来源库
                    "text": clean_text,
                    "heading_path": c.heading_path,
                    "page_start": c.page_start,
                    "score": c.score,
                    "sanitized": bool(hits),
                }
            )
        first = results[0] if results else None
        confident = bool(first.confident) if first is not None else False
        trace_event = {
            "type": "retrieval",
            "queries": len(queries),
            "returned": len(evidences),
            "dense_hits": first.dense_hits if first is not None else 0,
            "sparse_hits": first.sparse_hits if first is not None else 0,
            "used_sparse": first.used_sparse if first is not None else False,
            "used_rerank": first.used_rerank if first is not None else False,
            "confident": confident,
            "fast_path": confident and bool(evidences),
            "sanitized_chunks": sanitized_count,
        }
        if errors:
            trace_event["errors"] = errors
        return {"evidences": evidences, "confident": confident, "trace": [trace_event]}

    async def grade(state: _QaGraphState) -> dict:
        evidences = state.get("evidences") or []
        if not evidences:
            return {
                "grade_passed": False,
                "trace": [
                    {
                        "type": "grade",
                        "relevant": 0,
                        "total": 0,
                        "passed": False,
                        "skipped": "no_evidence",
                    }
                ],
            }
        prompt = (
            _load_prompt("qa_grade.md")
            .replace("{{QUERY}}", state.get("query", ""))
            .replace("{{EVIDENCES}}", _evidence_digest(evidences))
        )
        data = await _chat_json(state, feature="qa_grade", prompt=prompt, temperature=0.0)
        grades = data.get("grades") if data is not None else None
        parse_failed = not isinstance(grades, list)
        if parse_failed:
            # 宽松降级 (§5.3): 解析失败视为全相关, 宁可多生成也不误拒
            relevant = len(evidences)
        else:
            relevant_idx = {
                item["idx"]
                for item in grades
                if isinstance(item, dict)
                and item.get("relevant") is True
                and isinstance(item.get("idx"), int)
                and 1 <= item["idx"] <= len(evidences)
            }
            relevant = len(relevant_idx)
        passed = relevant >= 2
        trace_event = {
            "type": "grade",
            "relevant": relevant,
            "total": len(evidences),
            "passed": passed,
        }
        if parse_failed:
            trace_event["parse_failed"] = True
        return {"grade_passed": passed, "trace": [trace_event]}

    async def rewrite(state: _QaGraphState) -> dict:
        query = state.get("query", "")
        retry = state.get("retry_count", 0) + 1
        # 复用 query_plan 提示词: 取第一个与当前查询不同的候选作为改写
        prompt = _load_prompt("qa_query_plan.md").replace("{{QUERY}}", query)
        data = await _chat_json(state, feature="qa_rewrite", prompt=prompt, temperature=0.7)
        new_query = query
        if data is not None and isinstance(data.get("queries"), list):
            for item in data["queries"]:
                if isinstance(item, str) and item.strip() and item.strip() != query:
                    new_query = item.strip()
                    break
        return {
            "query": new_query,
            "queries": [new_query],
            "retry_count": retry,
            "trace": [
                {"type": "rewrite", "from": query, "to": new_query, "retry_count": retry}
            ],
        }

    async def _search_web_queries(
        queries: list[str], *, required: bool, search_depth: str, max_results: int
    ) -> tuple[list[dict], bool]:
        calls = [
            deps.web_search(
                query,
                required=required,
                search_depth=search_depth,
                max_results=max_results,
            )
            for query in queries
        ]
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
        combined: list[dict] = []
        from_cache = False
        errors: list[BaseException] = []
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                errors.append(outcome)
                continue
            if isinstance(outcome, tuple):
                items, cached = outcome
                from_cache = from_cache or bool(cached)
            else:
                items = outcome or []
            combined.extend(item for item in items if isinstance(item, dict))
        if not combined and required and errors:
            invalid = next(
                (error for error in errors if error.__class__.__name__ == "WebSearchInvalidQuery"),
                None,
            )
            raise invalid or errors[0]
        return _dedupe_web_results(combined), from_cache

    def _moderate_web_results(state: _QaGraphState, raw_results: list[dict]) -> tuple[list[dict], int]:
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
            verdict = moderator.check_output(
                f"{item['title']}\n{item['content']}", strict=strict
            )
            if not verdict.allowed:
                dropped += 1
                log.warning(
                    "qa_web_result_moderated",
                    url=item["url"],
                    category=verdict.category,
                    matched=verdict.matched,
                )
                continue
            results.append(item)
        return results, dropped

    async def web_fallback(state: _QaGraphState) -> dict:
        explicit = state.get("requested_route") == "web"
        queries = state.get("web_queries") or [state.get("query", "")]
        raw_results, from_cache = await _search_web_queries(
            queries,
            required=explicit,
            search_depth="advanced" if state.get("answer_tier") == "strong" else "basic",
            max_results=5,
        )
        results, dropped = _moderate_web_results(state, raw_results)
        trace_events: list[dict] = [
            {
                "type": "web_search",
                "round": 1,
                "queries": queries,
                "returned": len(results),
                "total": len(results),
                "from_cache": from_cache,
            }
        ]
        if dropped:
            trace_events.append({"type": "moderation", "dropped": dropped})
        # 全部被丢弃时 web_results 为空, after_web_fallback 按原空结果路径走拒答
        return {"web_results": results, "trace": trace_events}

    async def web_expand(state: _QaGraphState) -> dict:
        """深度搜索第二轮：基于首轮摘要识别信息缺口并补充检索。"""
        current = state.get("web_results") or []
        prompt = (
            _load_prompt("qa_web_followup_plan.md")
            .replace("{{QUERY}}", state.get("query", ""))
            .replace("{{SEARCHED_QUERIES}}", "\n".join(state.get("web_queries") or []))
            .replace("{{WEB_RESULTS}}", _format_web_results(current)[:6000])
        )
        data = await _chat_json(state, feature="qa_web_followup_plan", prompt=prompt)
        previous = set(state.get("web_queries") or [])
        queries: list[str] = []
        if data is not None and isinstance(data.get("queries"), list):
            for item in data["queries"]:
                if (
                    isinstance(item, str)
                    and item.strip()
                    and item.strip() not in previous
                    and item.strip() not in queries
                ):
                    queries.append(item.strip())
        queries = queries[:2]
        if not queries:
            return {
                "trace": [
                    {
                        "type": "web_search",
                        "round": 2,
                        "queries": [],
                        "returned": 0,
                        "total": len(current),
                        "skipped": True,
                    }
                ]
            }
        extra_raw, from_cache = await _search_web_queries(
            queries, required=False, search_depth="advanced", max_results=5
        )
        extra, dropped = _moderate_web_results(state, extra_raw)
        combined = _dedupe_web_results([*current, *extra], limit=14)
        trace: list[dict] = [
            {
                "type": "web_search",
                "round": 2,
                "queries": queries,
                "returned": max(0, len(combined) - len(current)),
                "total": len(combined),
                "from_cache": from_cache,
            }
        ]
        if dropped:
            trace.append({"type": "moderation", "dropped": dropped, "round": 2})
        return {"web_results": combined, "trace": trace}

    def _gen_path(state: _QaGraphState) -> str:
        """生成路径判定: web_results 优先 (kb 路径 grade 失败后 web 兜底也走此分支)。"""
        if state.get("web_results"):
            return "web"
        if state.get("route") == "kb" and state.get("evidences"):
            return "kb"
        return "direct"

    async def generate(state: _QaGraphState) -> dict:
        query = state.get("query", "")
        path = _gen_path(state)
        attempt = state.get("regen_count", 0)  # 重生成序号: 首次 0, verify 重生成 1
        regen = attempt > 0
        if path == "kb":
            system = _load_prompt("qa_generate_kb.md").replace(
                "{{SOURCES}}", _format_sources(state.get("evidences") or [])
            )
            if regen:
                system += STRICT_REGEN_INSTRUCTION
        elif path == "web":
            system = _load_prompt("qa_generate_web.md").replace(
                "{{WEB_RESULTS}}", _format_web_results(state.get("web_results") or [])
            )
        else:
            system = DIRECT_SYSTEM_PROMPT
        # 画像注入: 三条生成路径通吃 (kb/web/direct), 摘要为空即完全不改 prompt
        profile_summary = (state.get("profile_summary") or "").strip()
        if profile_summary:
            system += PROFILE_SECTION_TEMPLATE.replace("{{PROFILE}}", profile_summary)
        if state.get("minor"):  # 未成年模式 (P0-5): 安全约束段追加在 system 末尾
            system += "\n\n" + minor_system_guard()
        history = [
            ChatMessage(role=m["role"], content=str(m.get("content", "")))
            for m in state.get("messages") or []
            if m.get("role") in ("user", "assistant")
        ]
        if history and history[-1].role == "user" and history[-1].content == query:
            history = history[:-1]  # 当前查询若已入 messages, 避免重复追加
        req = ChatRequest(
            messages=(
                ChatMessage(role="system", content=system),
                *history,
                ChatMessage(role="user", content=query),
            ),
            tier=Tier.strong if state.get("answer_tier") == "strong" else Tier.standard,
            allow_tier_fallback=state.get("answer_tier") != "strong",
            # P1-15: 主回答上限 1500 token — 最坏路径 (9~10 次调用) 不再可能
            # 单次问答打满 8000 token 输出穿透成本模型
            max_tokens=1500,
            feature="qa_generate",
            tenant_id=state.get("tenant_id", ""),
            user_id=state.get("user_id", ""),
            request_id=state.get("request_id", ""),
        )
        # 图内真流式: 逐 token 写自定义流 (stub/replay 网关的 stream_chat 也按录制
        # 文本分块产出 StreamDelta, 同样经此路径); astream 之外 writer 为 no-op。
        writer = get_stream_writer()
        stream = getattr(deps.gateway, "stream_chat", None)
        result = None
        if callable(stream):
            parts: list[str] = []
            async for item in stream(req):
                if isinstance(item, StreamDelta):
                    parts.append(item.text)
                    writer({"type": "token", "text": item.text, "attempt": attempt})
                else:  # 协议约定: 末尾恰好一个 ChatResult
                    result = item
            answer = result.text if result is not None else "".join(parts)
        else:
            # 仅实现 chat 的网关 (如单测 Fake 的降级形态): 整段取回后分块写出
            result = await deps.gateway.chat(req)
            answer = result.text
            for i in range(0, len(answer), _TOKEN_FALLBACK_CHUNK):
                writer(
                    {
                        "type": "token",
                        "text": answer[i : i + _TOKEN_FALLBACK_CHUNK],
                        "attempt": attempt,
                    }
                )
        trace: list[dict] = []
        if profile_summary and not regen:
            # 前端时间线据此显示"已加载学情画像: N 个薄弱点" (画像可见 = 可核对);
            # verify 重生成时不重复上报
            trace.append(
                {
                    "type": "profile",
                    "weak_points": list(state.get("profile_weak_points") or []),
                    "chars": len(profile_summary),
                }
            )
        trace.append(
            {
                "type": "generate",
                "path": path,
                "regen": regen,
                "degraded": bool(result.degraded) if result is not None else False,
                "profile_injected": bool(profile_summary),
            }
        )
        return {"answer": answer, "trace": trace}

    async def verify_support(state: _QaGraphState) -> dict:
        answer = state.get("answer", "")
        prompt = (
            _load_prompt("qa_verify.md")
            .replace("{{SOURCES}}", _evidence_digest(state.get("evidences") or []))
            .replace("{{ANSWER}}", answer)
        )
        data = await _chat_json(
            state, feature="qa_verify_support", prompt=prompt, temperature=0.0
        )
        parse_failed = data is None or not isinstance(data.get("supported"), bool)
        supported = True if parse_failed else bool(data["supported"])
        claims: list[str] = []
        if data is not None and isinstance(data.get("unsupported_claims"), list):
            claims = [c for c in data["unsupported_claims"] if isinstance(c, str)][:5]
        regen_count = state.get("regen_count", 0)
        update: dict[str, Any] = {}
        if supported:
            action = "end"
        elif regen_count < 1 and state.get("retry_count", 0) < _MAX_RETRY:
            action = "regen"  # 重生成一次 (generate 侧追加强化指令)
            update["regen_count"] = regen_count + 1
        else:
            action = "end"
            disclaimer = UNSUPPORTED_DISCLAIMER
            if claims:
                disclaimer += "：" + "；".join(claims)
            update["answer"] = f"{answer}\n\n{disclaimer}"
        trace_event = {
            "type": "verify",
            "supported": supported,
            "action": action,
            "unsupported_claims": claims,
        }
        if parse_failed:
            trace_event["parse_failed"] = True  # 宽松降级: 校验解析失败按支撑处理
        update["verify_action"] = action
        update["trace"] = [trace_event]
        return update

    async def followups(state: _QaGraphState) -> dict:
        """轻量末节点: 同一次运行内用 cheap 档 json_mode 产 3 条递进追问建议。

        任何失败 (网关错误/解析失败/字段缺失) 降级为空列表, 绝不阻塞主回答;
        拒答路径 (refuse) 不经过本节点。
        """
        prompt = (
            _load_prompt("qa_followups.md")
            .replace("{{QUERY}}", state.get("query", ""))
            .replace("{{ANSWER}}", (state.get("answer") or "")[:_FOLLOWUP_ANSWER_CHARS])
        )
        data = await _chat_json(
            state, feature="qa_followups", prompt=prompt, temperature=0.5
        )
        degraded = data is None or not isinstance(data.get("followups"), list)
        items: list[str] = []
        if not degraded:
            for it in data["followups"]:
                if isinstance(it, str) and it.strip() and it.strip() not in items:
                    items.append(it.strip())
            items = items[:_MAX_FOLLOWUPS]
        return {
            "followups": items,
            "trace": [{"type": "followups", "count": len(items), "degraded": degraded}],
        }

    async def refuse(state: _QaGraphState) -> dict:
        if state.get("route") == "web":
            return {
                "answer": WEB_NO_RESULTS_ANSWER,
                "refused": True,
                "trace": [{"type": "refuse", "reason": "web_no_results"}],
            }
        return {
            "answer": NO_EVIDENCE_ANSWER,
            "refused": True,
            "trace": [{"type": "refuse"}],
        }

    # ---- 条件边 ----

    def after_router(state: _QaGraphState) -> str:
        return state.get("route", "direct")

    def after_retrieve(state: _QaGraphState) -> str:
        # 置信度门控：高置信走快路径跳过 grade，降低延迟。
        if state.get("confident") and state.get("evidences"):
            return "generate"
        return "grade"

    def after_grade(state: _QaGraphState) -> str:
        if state.get("grade_passed"):
            return "generate"
        if state.get("retry_count", 0) < _MAX_RETRY:
            return "rewrite"
        return "web_fallback" if deps.web_search is not None else "refuse"

    def after_web_fallback(state: _QaGraphState) -> str:
        if not state.get("web_results"):
            return "refuse"
        if state.get("requested_route") == "web" and state.get("answer_tier") == "strong":
            return "web_expand"
        return "generate"

    def after_generate(state: _QaGraphState) -> str:
        # kb 路径先过支撑校验; 其余路径直接进追问建议末节点
        return "verify" if _gen_path(state) == "kb" else "followups"

    def after_verify(state: _QaGraphState) -> str:
        return "generate" if state.get("verify_action") == "regen" else "followups"

    # ---- 组图 ----

    graph = StateGraph(_QaGraphState)
    graph.add_node("router", router)
    graph.add_node("query_plan", query_plan)
    graph.add_node("web_query_plan", web_query_plan)
    graph.add_node("retrieve", retrieve)
    graph.add_node("grade", grade)
    graph.add_node("rewrite", rewrite)
    graph.add_node("web_fallback", web_fallback)
    graph.add_node("web_expand", web_expand)
    graph.add_node("generate", generate)
    graph.add_node("verify_support", verify_support)
    graph.add_node("followups", followups)
    graph.add_node("refuse", refuse)

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        after_router,
        {"kb": "query_plan", "web": "web_query_plan", "direct": "generate"},
    )
    graph.add_edge("web_query_plan", "web_fallback")
    graph.add_edge("query_plan", "retrieve")
    graph.add_conditional_edges(
        "retrieve", after_retrieve, {"generate": "generate", "grade": "grade"}
    )
    graph.add_conditional_edges(
        "grade",
        after_grade,
        {
            "generate": "generate",
            "rewrite": "rewrite",
            "web_fallback": "web_fallback",
            "refuse": "refuse",
        },
    )
    graph.add_edge("rewrite", "retrieve")
    graph.add_conditional_edges(
        "web_fallback",
        after_web_fallback,
        {"generate": "generate", "web_expand": "web_expand", "refuse": "refuse"},
    )
    graph.add_edge("web_expand", "generate")
    graph.add_conditional_edges(
        "generate", after_generate, {"verify": "verify_support", "followups": "followups"}
    )
    graph.add_conditional_edges(
        "verify_support", after_verify, {"generate": "generate", "followups": "followups"}
    )
    graph.add_edge("followups", END)
    graph.add_edge("refuse", END)
    return graph
