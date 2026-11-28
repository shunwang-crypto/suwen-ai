"""Research through the real chat API, SSE adapter and PostgreSQL checkpoints."""

from __future__ import annotations

import json
import uuid

import pytest
from eduagents_core.tools.web_search import WebSearchUnavailable
from httpx import ASGITransport, AsyncClient

from .conftest import requires_services

pytestmark = requires_services


class ResearchGateway:
    def __init__(self, *, unsupported=False):
        self.unsupported = unsupported
        self.decisions = [
            {"action": "search", "queries": ["目标原始资料"]},
            {"action": "search", "queries": ["目标独立评测"]},
            {"action": "finish"},
        ]
        self.requests = []

    async def chat(self, req):
        from eduagents_core.gateway.base import ChatResult

        self.requests.append(req)
        tool_calls = ()
        if req.feature == "agent_qa":
            if self.decisions:
                decision = self.decisions.pop(0)
                name = "web_search" if decision["action"] == "search" else "submit_answer"
                args = (
                    {"queries": decision["queries"]}
                    if name == "web_search"
                    else {
                        "answer": "原始资料与独立评测存在差异 [1][2]。",
                        "source_ids": [
                            "https://example.com/primary",
                            "https://example.com/independent",
                        ],
                    }
                )
            else:
                latest = next((m for m in reversed(req.messages) if m.role == "tool"), None)
                if latest and json.loads(latest.content).get("accepted") is False:
                    name = "submit_answer"
                    args = {
                        "answer": "原始资料与独立评测存在差异 [1][2]。",
                        "source_ids": [
                            "https://example.com/primary",
                            "https://example.com/independent",
                        ],
                    }
                else:
                    name = None
            if name:
                tool_calls = (
                    {
                        "id": f"call-{len(self.requests)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    },
                )
            text = "finished" if not tool_calls else ""
        elif req.feature == "qa_verify_support":
            text = json.dumps(
                {
                    "supported": not self.unsupported,
                    "unsupported_claims": ["无法确认差异"] if self.unsupported else [],
                }
            )
        elif req.feature == "qa_followups":
            text = json.dumps({"followups": []})
        else:
            raise AssertionError(f"Unexpected model call: {req.feature}")
        return ChatResult(
            tool_calls=tool_calls,
            text=text,
            model="research-test",
            tier=req.tier.value,
            prompt_tokens=1,
            completion_tokens=1,
            cost_cny=0,
            latency_ms=0,
            mode="stub",
        )

    async def stream_chat(self, req):
        from eduagents_core.gateway.base import StreamDelta

        result = await self.chat(req)
        yield StreamDelta(result.text)
        yield result


@pytest.fixture
async def research_client(monkeypatch):
    from eduagents_core.config import get_settings

    monkeypatch.setenv("STRONG_API_KEY", "test-only-key")
    monkeypatch.setenv("STRONG_MODEL", "research-test")
    monkeypatch.setenv("TAVILY_API_KEY", "test-only-key")
    get_settings.cache_clear()
    from eduagents_api.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield (app, client)
    get_settings.cache_clear()


def events(response):
    parsed = []
    name = ""
    for line in response.text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            parsed.append((name, json.loads(line[6:])))
    return parsed


async def login(client):
    email = f"research-{uuid.uuid4().hex[:12]}@test.eduagents.cn"
    body = {"email": email, "password": "pass-12345"}
    registered = await client.post("/api/auth/register", json=body)
    assert registered.status_code == 201, registered.text
    response = await client.post("/api/auth/login", json=body)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def web_search(query, **options):
    suffix = "primary" if query == "目标原始资料" else "independent"
    return (
        [
            {
                "url": f"https://example.com/{suffix}",
                "title": suffix,
                "content": f"{suffix} 正文：原始资料与独立评测存在差异",
            }
        ],
        False,
    )


@pytest.mark.parametrize("unsupported", [False, True])
async def test_research_progress_citations_regeneration_and_history(research_client, unsupported):
    app, client = research_client
    gateway = ResearchGateway(unsupported=unsupported)
    app.state.harness.deps.gateway = gateway
    app.state.harness.deps.web_search = web_search
    headers = await login(client)
    response = await client.post(
        "/api/chat", json={"message": "比较目标的实际表现", "mode": "research"}, headers=headers
    )
    assert response.status_code == 200, response.text
    parsed = events(response)
    assert not [data for name, data in parsed if name == "error"]
    research = [data for name, data in parsed if name == "trace_event" and "trace_id" in data]
    assert len(research) == 2
    assert len({data["trace_id"] for data in research}) == 2
    first_token = next((i for i, (name, _data) in enumerate(parsed) if name == "token"))
    assert all(
        (
            i < first_token
            for i, (name, data) in enumerate(parsed)
            if name == "trace_event" and "trace_id" in data
        )
    )
    citations = [data for name, data in parsed if name == "citation"]
    assert [(item["idx"], item["url"]) for item in citations] == [
        (1, "https://example.com/primary"),
        (2, "https://example.com/independent"),
    ]
    generated = [req for req in gateway.requests if req.feature == "qa_verify_support"]
    assert len(generated) == (3 if unsupported else 1)
    if unsupported:
        assert {data["attempt"] for name, data in parsed if name == "token"} == {0}
    done = next((data for name, data in parsed if name == "done"))
    history = await client.get(f"/api/chat/sessions/{done['session_id']}/messages", headers=headers)
    assert history.status_code == 200, history.text
    assistant = history.json()[-1]
    assert ("原始资料与独立评测" in assistant["content"]) is not unsupported
    assert ("当前资料无法支持" in assistant["content"]) is unsupported
    assert assistant["meta"]["chat_mode"] == "research"
    assert len(assistant["meta"]["citations"]) == (0 if unsupported else 2)


async def test_research_initial_search_outage_reports_search_error(research_client):
    app, client = research_client
    app.state.harness.deps.gateway = ResearchGateway()

    async def unavailable(*args, **kwargs):
        raise WebSearchUnavailable("offline")

    app.state.harness.deps.web_search = unavailable
    headers = await login(client)
    response = await client.post(
        "/api/chat", json={"message": "比较目标的实际表现", "mode": "research"}, headers=headers
    )
    errors = [data for name, data in events(response) if name == "error"]
    assert errors and errors[0]["code"] == "WEB_SEARCH_FAILED"
