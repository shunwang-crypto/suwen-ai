"""Tavily search 参数与失败缓存的离线契约测试。"""

from __future__ import annotations

import httpx
from eduagents_core.config import Settings
from eduagents_core.tools.web_search import TavilySearch


class _Response:
    def __init__(self, *, results: list[dict] | None = None, error: Exception | None = None):
        self._results = results or []
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict:
        return {"results": self._results}


class _AsyncClient:
    responses: list[_Response] = []
    requests: list[dict] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, _url: str, *, json: dict) -> _Response:
        self.requests.append(json)
        return self.responses.pop(0)


async def test_tavily_search_forwards_depth_and_result_limit(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    _AsyncClient.requests = []
    _AsyncClient.responses = [
        _Response(results=[{"title": "官方资料", "url": "https://example.com", "content": "正文"}])
    ]
    search = TavilySearch(Settings(_env_file=None, tavily_api_key="test-key"))

    results, from_cache = await search.search(
        "测试主题", max_results=7, search_depth="advanced"
    )

    assert results[0]["title"] == "官方资料"
    assert from_cache is False
    assert _AsyncClient.requests[0]["search_depth"] == "advanced"
    assert _AsyncClient.requests[0]["max_results"] == 7


async def test_tavily_cache_isolated_by_depth_and_result_limit(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    request = httpx.Request("POST", "https://api.tavily.com/search")
    failure = httpx.ConnectError("offline", request=request)
    cached_result = {"title": "缓存资料", "url": "https://example.com/cache", "content": "正文"}
    _AsyncClient.requests = []
    _AsyncClient.responses = [
        _Response(results=[cached_result]),
        _Response(error=failure),
        _Response(error=failure),
        _Response(error=failure),
    ]
    search = TavilySearch(Settings(_env_file=None, tavily_api_key="test-key"))

    await search.search("同一主题", max_results=5, search_depth="basic")
    advanced, advanced_cached = await search.search(
        "同一主题", max_results=5, search_depth="advanced"
    )
    larger, larger_cached = await search.search(
        "同一主题", max_results=10, search_depth="basic"
    )
    basic, basic_cached = await search.search(
        "同一主题", max_results=5, search_depth="basic"
    )

    assert (advanced, advanced_cached) == ([], False)
    assert (larger, larger_cached) == ([], False)
    assert basic == [cached_result]
    assert basic_cached is True
