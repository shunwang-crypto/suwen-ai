"""Prepare the same local OpenAI-compatible model for every evaluation tier."""

import time

import httpx


async def prepare_ollama(settings, model):
    base = settings.ollama_base_url.rstrip("/")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=300) as client:
        warmup = await client.post(
            base + "/api/generate",
            json={"model": model, "prompt": "", "stream": False, "keep_alive": "10m"},
        )
        warmup.raise_for_status()
        inventory = await client.get(base + "/api/ps")
        inventory.raise_for_status()
    aliases = {model, warmup.json().get("model", model), f"{model}:latest"}
    loaded = next(
        (
            entry
            for entry in inventory.json()["models"]
            if entry.get("model") in aliases or entry.get("name") in aliases
        ),
        {},
    )
    options = {
        "gateway_mode": "live",
        "ollama_chat_model": model,
        "model_routing_enabled": False,
        "gateway_max_retries": 1,
        "gateway_replay_fallback_stub": False,
        "agent_collaboration_max_parallel": 1,
    }
    for tier in ("cheap", "standard", "strong", "plan"):
        options.update(
            {
                f"{tier}_api_key": "ollama",
                f"{tier}_model": model,
                f"{tier}_base_url": base + "/v1",
                f"{tier}_enable_thinking": None,
                f"{tier}_input_price_per_m": 0.0,
                f"{tier}_output_price_per_m": 0.0,
            }
        )
    window = loaded.get("context_length")
    if settings.ollama_require_gpu and (
        not loaded.get("size") or loaded.get("size_vram", 0) < loaded["size"]
    ):
        raise RuntimeError(f"Ollama model {model} is not fully resident on GPU")
    if isinstance(window, int) and window > 0:
        options["harness_context_window_tokens"] = min(
            window, settings.harness_context_window_tokens
        )
    configured = settings.model_copy(update=options)
    return configured, {
        "model": model,
        "warmup_ms": round((time.monotonic() - started) * 1000),
        "harness_context_window_tokens": configured.harness_context_window_tokens,
        "ollama_runtime": {
            key: loaded[key] for key in ("digest", "context_length", "size", "size_vram") if key in loaded
        },
    }
