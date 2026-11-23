"""Behavioral bounds for model-directed research, with no graph/provider dependency."""

from __future__ import annotations

import asyncio
import json

import pytest
from eduagents_core.gateway.base import BudgetExceeded
from eduagents_core.graphs.research_harness import run_web_research
from eduagents_core.tools.web_search import WebSearchUnavailable


class ResearchTools:
    def __init__(self, decisions, outcomes=None):
        self.decisions = list(decisions)
        self.outcomes = list(outcomes or [])
        self.observations = []
        self.searches = []

    async def decide(self, observation):
        self.observations.append(json.loads(observation))
        decision = self.decisions.pop(0)
        if isinstance(decision, BaseException):
            raise decision
        return decision

    async def search(self, queries):
        self.searches.extend(queries)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome, False
        return [{"url": f"https://example.com/{len(self.searches)}", "content": "已有依据"}], False

    async def run(self):
        return await run_web_research(
            query="原始用户问题", history="最近对话", decide=self.decide, search=self.search,
            prepare=lambda sources: (sources, 0),
            merge=lambda sources: list({item["url"]: item for item in sources}.values()),
        )


def search(*queries):
    return {"action": "search", "queries": list(queries)}


FINISH = {"action": "finish"}


async def test_stops_when_evidence_is_sufficient_without_forced_second_round():
    tools = ResearchTools([search("精准查询"), FINISH])
    result = await tools.run()
    assert tools.searches == ["精准查询"]
    assert result.stop_reason == "finished"
    assert tools.observations[1]["sources"][0]["content"] == "已有依据"


async def test_hard_search_budget_and_three_round_limit():
    tools = ResearchTools([
        search("查询一", "查询二", "忽略额外查询"),
        search("查询三", "查询四"),
        search("查询五", "不能执行第六条"),
    ])
    result = await tools.run()
    assert tools.searches == ["查询一", "查询二", "查询三", "查询四", "查询五"]
    assert len(tools.observations) == 3
    assert result.search_calls == 5
    assert result.stop_reason == "search_budget"


async def test_does_not_repeat_normalized_queries_or_use_unknown_tools():
    tools = ResearchTools([search("  TOPIC   definition "), search("topic definition")])
    result = await tools.run()
    assert tools.searches == ["TOPIC definition"]
    assert result.stop_reason == "no_new_queries"


@pytest.mark.parametrize("decision", [None, {}, {"action": []}, {"action": True}, {"action": "bash", "command": "ls"}, FINISH, search("x", " ")])
async def test_malformed_or_premature_decision_attempts_original_query_once(decision):
    tools = ResearchTools([decision])
    result = await tools.run()
    assert tools.searches == ["原始用户问题"]
    assert result.sources
    assert result.stop_reason == "planner_unavailable"


async def test_invalid_later_decision_keeps_prior_sources():
    tools = ResearchTools([search("查询一"), None])
    result = await tools.run()
    assert len(result.sources) == 1
    assert result.stop_reason == "planner_unavailable"
    assert tools.searches == ["查询一"]


async def test_later_search_outage_preserves_sources():
    tools = ResearchTools(
        [search("查询一"), search("补充查询")],
        [[{"url": "https://example.com/source", "content": "可用资料"}], WebSearchUnavailable()],
    )
    result = await tools.run()
    assert result.sources[0]["content"] == "可用资料"
    assert result.stop_reason == "search_unavailable"
    assert result.trace[-2]["degraded"] is True


async def test_first_search_outage_is_not_misreported_as_empty_evidence():
    tools = ResearchTools([search("查询一")], [WebSearchUnavailable()])
    with pytest.raises(WebSearchUnavailable):
        await tools.run()


@pytest.mark.parametrize("location", ["decision", "search"])
async def test_budget_exhaustion_propagates_even_with_existing_sources(location):
    error = BudgetExceeded()
    tools = ResearchTools(
        [search("查询一"), error if location == "decision" else search("补充查询")],
        [[{"url": "https://example.com", "content": "正文"}], error],
    )
    with pytest.raises(BudgetExceeded):
        await tools.run()


async def test_cancellation_is_not_converted_to_partial_success():
    tools = ResearchTools([search("查询一")], [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await tools.run()


async def test_observations_include_later_sources_with_bounded_content():
    first = [{"url": f"https://example.com/{i}", "content": "a" * 50000} for i in range(10)]
    later = [{"url": "https://example.com/latest", "content": "新的关键证据"}]
    tools = ResearchTools([search("查询一"), search("查询二"), FINISH], [first, later])
    await tools.run()
    sources = tools.observations[2]["sources"]
    assert sources[-1]["content"] == "新的关键证据"
    assert all(len(item["content"]) <= 240 for item in sources)
