"""Native research tool budgets, search outages and cancellation boundaries."""

import asyncio

import pytest
from eduagents_core.education.qa import QuestionAnswerTools
from eduagents_core.gateway.base import BudgetExceeded
from eduagents_core.harness.task import TaskRecord
from eduagents_core.tools.web_search import WebSearchUnavailable

from .test_native_agent_tasks import ScriptedModel, deps


@pytest.mark.parametrize(
    "exception", [BudgetExceeded(), WebSearchUnavailable(), asyncio.CancelledError()]
)
async def test_first_search_failure_is_not_misreported_as_empty_evidence(exception):

    async def web(query):
        raise exception

    tools = QuestionAnswerTools(
        deps(ScriptedModel([]), web_search=web), TaskRecord(status="running"), lambda _: None
    )
    with pytest.raises(type(exception)):
        await tools.search_web({"queries": ["资料"]})


async def test_later_search_outage_preserves_existing_sources_with_trace():

    async def web(query):
        if query == "补充":
            raise WebSearchUnavailable()
        return [{"url": "https://example.com", "title": "教材", "content": "已有资料"}]

    tools = QuestionAnswerTools(
        deps(ScriptedModel([]), web_search=web), TaskRecord(status="running"), lambda _: None
    )
    await tools.search_web({"queries": ["资料"]})
    result = await tools.search_web({"queries": ["补充"]})
    assert result["sources"][0]["text"] == "已有资料"
    assert any(e.get("degraded") for e in tools.state["trace"])


async def test_tool_progress_emits_before_result_and_bounds_source_text():
    events = []

    async def web(query):
        return [{"url": "https://example.com", "title": "教材", "content": "a" * 50000}]

    tools = QuestionAnswerTools(
        deps(ScriptedModel([]), web_search=web), TaskRecord(status="running"), events.append
    )
    result = await tools.handlers["web_search"]({"queries": ["资料"]})
    assert len(result["sources"][0]["text"]) == 1600
    assert events[0] == {"kind": "tool", "tool": "web_search", "status": "running"}
    assert events[-1]["status"] == "done"
