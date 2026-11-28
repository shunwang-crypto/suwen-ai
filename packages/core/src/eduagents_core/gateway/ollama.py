"""Verify actual Ollama GPU placement before accepting a local GPU workload."""

import asyncio

import httpx

from ..config import Settings


async def verify_ollama_gpu(settings: Settings) -> dict:
    if not settings.ollama_require_gpu:
        return {}
    models = []
    if settings.embedding_mode == "ollama":
        models.append(("/api/embed", settings.embedding_model, {"input": ["向量索引"]}))
    if settings.ollama_chat_model and settings.gateway_mode in {"live", "record"}:
        models.append(
            ("/api/generate", settings.ollama_chat_model, {"prompt": "", "stream": False})
        )
    if not models:
        raise ValueError(
            "OLLAMA_REQUIRE_GPU requires an active local embedding or generation model"
        )
    results = {}
    async with httpx.AsyncClient(
        base_url=settings.ollama_base_url.rstrip("/"), timeout=180
    ) as client:
        for endpoint, model, payload in models:
            # A second service can warm a different model between our request
            # and /api/ps on a single-runner installation. Recheck missing models.
            for attempt in range(3):
                response = await client.post(
                    endpoint, json={"model": model, **payload, "keep_alive": "15m"}
                )
                response.raise_for_status()
                inventory = await client.get("/api/ps")
                inventory.raise_for_status()
                names = {model, f"{model}:latest", response.json().get("model", model)}
                entry = next(
                    (row for row in inventory.json()["models"] if row["name"] in names), {}
                )
                if entry or attempt == 2:
                    break
                await asyncio.sleep(1 + attempt)
            size = entry.get("size") or 0
            if size <= 0 or entry.get("size_vram", 0) < size:
                raise RuntimeError(f"Ollama model {model} is not fully resident on GPU")
            results[model] = {
                "size": size,
                "size_vram": entry["size_vram"],
                "context_length": entry.get("context_length"),
            }
            if endpoint == "/api/generate" and entry.get("context_length"):
                settings.harness_context_window_tokens = min(
                    settings.harness_context_window_tokens, entry["context_length"]
                )
    return results
