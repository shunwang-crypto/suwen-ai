"""Bounded web research loop, independent of the graph runtime.

The model chooses search or finish; the host owns tool access, query deduplication,
context limits and budgets. All model/search calls are injected by the caller.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..gateway.base import BudgetExceeded

MAX_SEARCH_CALLS = 5
MAX_SEARCH_ROUNDS = 3
MAX_QUERIES_PER_ROUND = 2
MAX_SOURCES = 14
MAX_QUERY_CHARS = 256

Decide = Callable[[str], Awaitable[dict[str, Any] | None]]
Search = Callable[[list[str]], Awaitable[tuple[list[dict], bool]]]
Prepare = Callable[[list[dict]], tuple[list[dict], int]]
Merge = Callable[[list[dict]], list[dict]]


@dataclass
class ResearchResult:
    sources: list[dict]
    queries: list[str]
    trace: list[dict]
    stop_reason: str
    search_calls: int


def _query_key(query: str) -> str:
    return " ".join(query.split()).casefold()


def _queries(value: Any, searched: set[str], remaining: int) -> list[str]:
    if not isinstance(value, list):
        return []
    queries: list[str] = []
    seen = set(searched)
    for item in value:
        if not isinstance(item, str):
            continue
        query = " ".join(item.split())[:MAX_QUERY_CHARS]
        key = _query_key(query)
        if len("".join(query.split())) < 2 or key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) >= min(MAX_QUERIES_PER_ROUND, remaining):
            break
    return queries


def _observation(query: str, history: str, sources: list[dict], queries: list[str]) -> str:
    # Include every retained source, rather than truncating a concatenated blob
    # that hides later rounds. Source text has already been moderated/sanitized.
    summaries = [
        {
            "idx": i,
            "title": str(item.get("title", ""))[:100],
            "url": str(item.get("url", ""))[:160],
            "content": str(item.get("content", ""))[:240],
        }
        for i, item in enumerate(sources[:MAX_SOURCES], 1)
    ]
    return json.dumps(
        {
            "question": query[:4000],
            "recent_conversation": history[:3000],
            "searched_queries": queries,
            "sources": summaries,
            "remaining_search_calls": MAX_SEARCH_CALLS - len(queries),
        },
        ensure_ascii=False,
    )


async def run_web_research(
    *,
    query: str,
    history: str,
    decide: Decide,
    search: Search,
    prepare: Prepare,
    merge: Merge,
    on_trace: Callable[[dict], None] | None = None,
) -> ResearchResult:
    sources: list[dict] = []
    queries: list[str] = []
    searched: set[str] = set()
    trace: list[dict] = []
    stop_reason = "search_budget"

    def emit(event: dict) -> None:
        event["trace_id"] = f"research-{len(trace)}"
        trace.append(event)
        if on_trace is not None:
            on_trace(event)

    for round_no in range(1, MAX_SEARCH_ROUNDS + 1):
        data = await decide(_observation(query, history, sources, queries))
        valid = (
            isinstance(data, dict)
            and isinstance(data.get("action"), str)
            and data["action"] in {"search", "finish"}
        )
        degraded = not valid
        if sources and (degraded or data["action"] == "finish"):
            stop_reason = "planner_unavailable" if degraded else "finished"
            break
        selected = _queries(
            data.get("queries") if valid and data["action"] == "search" else None,
            searched,
            MAX_SEARCH_CALLS - len(queries),
        )
        if not selected:
            if queries:
                stop_reason = "no_new_queries"
                break
            # A malformed decision or premature finish must still try the
            # original question once; it must never lead to an unsourced answer.
            selected = _queries([query], searched, MAX_SEARCH_CALLS)
            degraded = True
        if not selected:
            stop_reason = "invalid_query"
            break
        queries.extend(selected)
        searched.update(_query_key(item) for item in selected)
        emit(
            {
                "type": "research",
                "action": "search",
                "round": round_no,
                "reason": f"第 {round_no} 轮：检索所需资料",
                "degraded": degraded,
            }
        )
        try:
            raw, from_cache = await search(selected)
        except BudgetExceeded:
            raise
        except Exception:  # noqa: BLE001 - preserve earlier sources after a later tool failure
            if not sources:
                raise
            stop_reason = "search_unavailable"
            emit(
                {
                    "type": "web_search",
                    "round": round_no,
                    "queries": selected,
                    "returned": 0,
                    "total": len(sources),
                    "degraded": True,
                }
            )
            break
        cleaned, dropped = prepare(raw)
        previous = len(sources)
        sources = merge([*sources, *cleaned])[:MAX_SOURCES]
        emit(
            {
                "type": "web_search",
                "round": round_no,
                "queries": selected,
                "returned": max(0, len(sources) - previous),
                "total": len(sources),
                "from_cache": from_cache,
            }
        )
        if dropped:
            emit({"type": "moderation", "round": round_no, "dropped": dropped})
        if degraded:
            stop_reason = "planner_unavailable"
            break
        if len(queries) >= MAX_SEARCH_CALLS:
            break

    emit(
        {
            "type": "research",
            "action": "finish",
            "stop_reason": stop_reason,
            "search_calls": len(queries),
            "sources": len(sources),
            "reason": (
                "未找到可用资料" if not sources else {
                    "finished": "资料检索结束，开始整理回答",
                    "search_budget": "已到检索上限，按已有资料整理回答",
                    "planner_unavailable": "检索规划暂不可用，按已有资料整理回答",
                    "search_unavailable": "补充搜索暂不可用，按已有资料整理回答",
                    "no_new_queries": "没有新的搜索问题，按已有资料整理回答",
                }.get(stop_reason, "资料检索结束，整理已有依据")
            ),
        }
    )
    return ResearchResult(sources, queries, trace, stop_reason, len(queries))
