from __future__ import annotations

import json

import xxhash

from .base import ChatRequest, message_text

_STUB_BANNER = "【开发桩响应 · 非真实模型输出】"


def cited_excerpt(sources):
    return _STUB_BANNER + "\n" + "\n".join(
        f"[来源 {i}] {source['text'][:120]}" for i, source in enumerate(sources, 1)
    )


def stub_text(req: ChatRequest) -> str:
    """Deterministic fake completion for dev/CI. Marked unmistakably so it can
    never be passed off as a real model answer (§14 anti-fraud rule)."""
    last_user = next(
        (message_text(m.content) for m in reversed(req.messages) if m.role == "user"), ""
    )
    digest = xxhash.xxh64_hexdigest(f"{req.tier.value}|{last_user}".encode())[:8]
    if req.feature == "qa_verify_support":
        try:
            data = json.loads(last_user)
            sources = data["sources"]
            verified = bool(sources) and data["answer"] == cited_excerpt(sources)
        except (ValueError, KeyError, TypeError):
            verified = False
        return json.dumps({"supported": verified, "unsupported_claims": [] if verified else ["缺少原文依据"]})
    if req.json_mode:
        # Valid JSON so downstream parsers don't crash in dev pipelines.
        safe = last_user[:80].replace('"', "'").replace("\n", " ")
        return (
            f'{{"stub": true, "tier": "{req.tier.value}", "digest": "{digest}", "echo": "{safe}"}}'
        )
    excerpt = last_user[:60].replace("\n", " ")
    return (
        f"{_STUB_BANNER}\n"
        f"档位={req.tier.value} 指纹={digest}\n"
        f"你的输入摘要:「{excerpt}」\n"
        "这是网关 stub 模式生成的确定性占位回答, 用于开发与 CI, "
        "切换 GATEWAY_MODE=live 后将由真实模型 (DeepSeek API) 生成。"
    )


def estimate_tokens(text: str) -> int:
    # Rough but deterministic: CJK chars count ~1 token, others ~1/4.
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return max(1, cjk + other // 4)
