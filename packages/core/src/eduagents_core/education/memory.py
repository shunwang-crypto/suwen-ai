"""Bounded lexical recall of historical observations; never mastery evidence."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta

MEMORY_TTL = timedelta(days=90)
MAX_MEMORIES = 4
MAX_CONTEXT_CHARS = 6000
_STOP_TOKENS = {"什么", "为什", "怎么", "这个", "那个", "现在", "可以", "一下", "一个", "还是", "不懂", "理解", "the", "a", "is", "i", "to", "of", "how", "why", "what"}


def fingerprint(text: str) -> str:
    normalized = "".join(unicodedata.normalize("NFKC", text).casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    result = set(re.findall(r"[a-z0-9_]+", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(run[i:i + 2] for i in range(len(run) - 1))
    return result - _STOP_TOKENS


def latest_observations(items: list[dict]) -> list[dict]:
    """A newer observation or explicit correction prevents an old card resurfacing.

    Collapse before filtering suppression/expiry, so an expired 'understood'
    observation does not bring back an earlier 'confused' one.
    """
    latest = {}
    unlinked = []
    for item in items:
        if item.get("kind") == "preference_signal":
            continue
        kc = item.get("kc_id")
        if not kc:
            unlinked.append(item)
            continue
        key = (
            bool(item.get("user_corrected")),
            item.get("observed_at") or item["created_at"],
            item["id"],
        )
        if kc not in latest or key > latest[kc][0]:
            latest[kc] = (key, item)
    return [pair[1] for pair in latest.values()] + unlinked


def effective_status(item: dict, now: datetime) -> str:
    if item.get("status", "active") != "active":
        return "obsolete"
    if item.get("suppressed"):
        return "suppressed"
    expiry = item.get("expires_at")
    observed = item.get("observed_at") or item["created_at"]
    if not item.get("user_corrected") and now >= (expiry or observed + MEMORY_TTL):
        return "expired"
    return "active"


def recall(items: list[dict], query: str, kc_names: list[str], *, now=None, limit=MAX_MEMORIES) -> dict:
    now = now or datetime.now(UTC)
    wanted = {unicodedata.normalize("NFKC", name).casefold().strip() for name in kc_names if name.strip()}
    search = tokens(query + " " + " ".join(kc_names))
    ranked = []
    for item in latest_observations(items):
        if effective_status(item, now) != "active":
            continue
        names = [item.get("kc_name", ""), *(item.get("aliases") or [])]
        exact = bool(wanted & {unicodedata.normalize("NFKC", n).casefold().strip() for n in names})
        overlap = search & tokens(item["text"] + " " + " ".join(names))
        name_overlap = search & tokens(" ".join(names))
        if not exact and not name_overlap and len(overlap) < 2:
            continue
        score = 10 * exact + len(overlap) / max(1, len(search))
        ranked.append((score, item.get("observed_at") or item["created_at"], item["id"], item))
    result = {"memories": [], "interpretation": "historical_observation_not_mastery_evidence"}
    for _, _, _, item in sorted(ranked, reverse=True)[:max(1, min(limit, MAX_MEMORIES))]:
        memory = {key: item.get(key) for key in (
            "id", "kc_id", "kc_name", "kind", "source", "session_id", "source_message_id",
            "observed_at", "updated_at", "revision", "user_corrected",
        )}
        memory["text"] = item["text"][:600]
        memory["observed_at"] = memory["observed_at"] or item["created_at"]
        memory = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in memory.items()}
        result["memories"].append(memory)
        if len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
            result["memories"].pop()
            break
    return result
