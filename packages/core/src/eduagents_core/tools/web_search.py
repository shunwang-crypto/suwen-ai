from __future__ import annotations

import time
from typing import Any

import httpx

from ..config import Settings
from ..logging import get_logger

log = get_logger("web_search")

_CACHE_TTL_SECONDS = 900  # §9: 短 TTL 缓存, Tavily 不可达时返回缓存并标注


class WebSearchUnavailable(RuntimeError):
    """显式联网请求无法访问搜索服务，且没有可用缓存。"""


class TavilySearch:
    """联网搜索 (Tavily) + 内存 TTL 缓存兜底。

    返回 (results, from_cache): 不可达且有缓存 -> (缓存, True);
    不可达且无缓存 -> ([], False) 由上层降级为纯知识库模式。
    """

    def __init__(self, settings: Settings):
        self._api_key = settings.tavily_api_key
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def search(
        self, query: str, max_results: int = 5, *, raise_on_failure: bool = False
    ) -> tuple[list[dict[str, Any]], bool]:
        if not self._api_key:
            return [], False
        cached = self._cache.get(query)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": self._api_key,
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                    },
                )
                resp.raise_for_status()
                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:1000],
                    }
                    for r in resp.json().get("results", [])
                ]
            self._cache[query] = (time.monotonic(), results)
            return results, False
        except Exception as exc:  # noqa: BLE001 — 网络失败走缓存兜底
            log.warning("tavily_failed", error=str(exc))
            if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
                return cached[1], True
            if raise_on_failure:
                raise WebSearchUnavailable("Tavily search failed") from exc
            return [], False
