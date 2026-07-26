"""P2-8 日志脱敏 processor: 敏感键哈希化 / 非敏感键原样 / 嵌套生效 / env 旁路。"""

from __future__ import annotations

import hashlib
import re

from eduagents_core.logging import SENSITIVE_KEYS, pii_scrub_processor

_PII_RE = re.compile(r"^<pii:[0-9a-f]{8}:len=\d+>$")


def _run(event_dict: dict) -> dict:
    return pii_scrub_processor(None, "info", dict(event_dict))


def test_sensitive_keys_replaced_with_fingerprint():
    secret = "我妈得了乳腺癌，该怎么和她沟通"
    for key in SENSITIVE_KEYS:
        out = _run({"event": "request", key: secret})
        assert secret not in str(out), key
        assert _PII_RE.match(out[key]), out[key]
    # 指纹确定性: sha256 前 8 位 + 原文长度, 便于跨行关联同一原文
    out = _run({"query": secret})
    digest = hashlib.sha256(secret.encode()).hexdigest()[:8]
    assert out["query"] == f"<pii:{digest}:len={len(secret)}>"


def test_non_sensitive_keys_untouched():
    out = _run(
        {
            "event": "request",
            "user_id": "u-123",
            "status": 200,
            "duration_ms": 42,
            "feature": "quiz_grade",
        }
    )
    assert out == {
        "event": "request",
        "user_id": "u-123",
        "status": 200,
        "duration_ms": 42,
        "feature": "quiz_grade",
    }


def test_event_key_and_non_string_values_untouched():
    # event 是事件名不脱敏; 敏感键但非字符串值 (如计数) 原样保留
    out = _run({"event": "answer", "answer": 3})
    assert out == {"event": "answer", "answer": 3}


def test_nested_dict_and_list_scrubbed():
    out = _run(
        {
            "event": "chat_done",
            "payload": {
                "query": "隐私问题原文",
                "n_chunks": 3,
                "citations": [{"text": "文档摘录原文", "page": 2}],
            },
        }
    )
    payload = out["payload"]
    assert _PII_RE.match(payload["query"])
    assert payload["n_chunks"] == 3
    assert _PII_RE.match(payload["citations"][0]["text"])
    assert payload["citations"][0]["page"] == 2


def test_plaintext_env_bypass(monkeypatch):
    monkeypatch.setenv("LOG_PII_PLAINTEXT", "1")
    out = _run({"event": "request", "query": "本地调试要看原文"})
    assert out["query"] == "本地调试要看原文"
    monkeypatch.delenv("LOG_PII_PLAINTEXT")
    out = _run({"event": "request", "query": "本地调试要看原文"})
    assert out["query"].startswith("<pii:")
