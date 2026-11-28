"""Verify actual Ollama GPU placement before accepting a local GPU workload."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import gettempdir

import httpx

from ..config import Settings

_PREFLIGHT_SECONDS = 375
_INVENTORY_DELAYS = (0, 0.25, 0.75, 1.5)


@asynccontextmanager
async def _preflight_lock():
    import fcntl

    # API and worker run as separate processes and can use different aliases
    # for the same runner. Serialize strict startup checks for the local user.
    path = Path(gettempdir()) / f"eduagents-ollama-gpu-{os.getuid()}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            async with asyncio.timeout(_PREFLIGHT_SECONDS + 5):
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        await asyncio.sleep(0.1)
                    else:
                        break
        except TimeoutError as exc:
            raise RuntimeError("Timed out waiting for the local Ollama GPU preflight") from exc
        yield
    finally:
        os.close(fd)


async def _warm_and_verify(client, endpoint, model, payload):
    for _attempt in range(2):
        response = await client.post(
            endpoint, json={"model": model, **payload, "keep_alive": "15m"}
        )
        response.raise_for_status()
        names = {model, f"{model}:latest", response.json().get("model", model)}
        # A successful warm call can precede the inventory update. Poll first
        # so a lagging inventory does not trigger repeated model loads.
        for delay in _INVENTORY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            inventory = await client.get("/api/ps")
            inventory.raise_for_status()
            entry = next(
                (row for row in inventory.json().get("models") or [] if row.get("name") in names),
                {},
            )
            size, vram = entry.get("size"), entry.get("size_vram")
            if type(size) is not int or size <= 0 or type(vram) is not int:
                continue
            if vram < size:
                raise RuntimeError(f"Ollama model {model} is not fully resident on GPU")
            return entry
    raise RuntimeError(f"Ollama model {model} is not fully resident on GPU: placement unavailable")


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
    async with (
        _preflight_lock(),
        asyncio.timeout(_PREFLIGHT_SECONDS),
        httpx.AsyncClient(base_url=settings.ollama_base_url.rstrip("/"), timeout=180) as client,
    ):
        for endpoint, model, payload in models:
            entry = await _warm_and_verify(client, endpoint, model, payload)
            results[model] = {
                "size": entry["size"],
                "size_vram": entry["size_vram"],
                "context_length": entry.get("context_length"),
            }
            if endpoint == "/api/generate" and entry.get("context_length"):
                settings.harness_context_window_tokens = min(
                    settings.harness_context_window_tokens, entry["context_length"]
                )
    return results
