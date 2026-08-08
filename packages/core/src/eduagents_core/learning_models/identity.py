"""Stable question identity for offline/shadow item models.

Question UUIDs are stable database row identities, but an immediately
generated question is not necessarily the same item when it is generated
again.  Giving such a row an independent item-parameter estimate would turn
one-observation rows into misleading Elo values.  This module therefore
requires an explicit reusable identity (or a durable source identity) before
an event is eligible for item Elo.

The returned identifiers are namespaced SHA-256 digests.  They are stable,
bounded, and do not copy question text or arbitrary metadata into an offline
result.  An explicit ``canonical_item_id``/``item_family_id`` remains the
preferred source; the question UUID is only a conservative fallback for
durable non-generated questions.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ITEM_IDENTITY_VERSION = "item-identity-v1"

_EXPLICIT_CANONICAL_KEYS = (
    "canonical_item_id",
    "canonical_id",
    "item_key",
)
_EXPLICIT_FAMILY_KEYS = (
    "item_family_id",
    "family_id",
    "item_family_key",
)
_INSTANT_SOURCE_TYPES = {"generated", "ai_generated", "instant_generated"}
_DURABLE_SOURCE_TYPES = {"manual", "document", "image", "import", "assessment"}


@dataclass(frozen=True)
class ItemIdentity:
    """Resolved item identity and the reason it is or is not reusable."""

    canonical_item_id: str | None
    item_family_id: str | None
    reusable: bool
    source: str
    reason: str

    @property
    def eligible_for_elo(self) -> bool:
        return self.reusable and self.canonical_item_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": ITEM_IDENTITY_VERSION,
            "canonical_item_id": self.canonical_item_id,
            "item_family_id": self.item_family_id,
            "reusable": self.reusable,
            "source": self.source,
            "reason": self.reason,
            "eligible_for_elo": self.eligible_for_elo,
        }


def _metadata(question: Mapping[str, Any]) -> dict[str, Any]:
    raw = question.get("source_meta_json", question.get("source_meta"))
    return dict(raw) if isinstance(raw, Mapping) else {}


def _first_value(question: Mapping[str, Any], metadata: Mapping[str, Any], keys: tuple[str, ...]):
    for key in keys:
        value = question.get(key)
        if value is not None and str(value).strip():
            return value, "question"
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return value, "source_meta"
    return None, ""


def _normalize_token(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return " ".join(text.split())


def _digest(prefix: str, *parts: Any) -> str:
    payload = json.dumps(
        [ITEM_IDENTITY_VERSION, *[_normalize_token(part) for part in parts]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()}"


def _stable_or_digest(prefix: str, value: Any) -> str:
    """Keep an already serialized ID stable across JSON export/re-import."""

    text = str(value).strip()
    if len(text) == len(prefix) + 65 and text.startswith(f"{prefix}_"):
        try:
            int(text[len(prefix) + 1 :], 16)
        except ValueError:
            pass
        else:
            return text
    return _digest(prefix, value)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "reusable"}


def identity_from_question(question: Mapping[str, Any]) -> ItemIdentity:
    """Resolve a stable identity from a question-shaped mapping.

    ``source_meta_json`` may contain ``reusable``/``instant_generated`` and
    explicit identity keys.  A generated question without such a key is
    deliberately returned as ineligible, even though its database UUID is
    available.  This is the guard against fitting an independent item Elo to
    an item that will never be seen again.
    """

    metadata = _metadata(question)
    source_type = _normalize_token(question.get("source_type", metadata.get("source_type", "")))
    explicit_canonical, canonical_source = _first_value(
        question, metadata, _EXPLICIT_CANONICAL_KEYS
    )
    explicit_family, family_source = _first_value(question, metadata, _EXPLICIT_FAMILY_KEYS)

    explicitly_instant = _truthy(metadata.get("instant_generated")) or str(
        metadata.get("reuse_policy", "")
    ).strip().casefold() in {"instant", "ephemeral", "single_use"}
    explicit_reusable = metadata.get("reusable")
    if explicit_reusable is None:
        explicit_reusable = question.get("reusable")
    if explicit_reusable is None:
        explicit_reusable = question.get("item_reusable")

    if explicitly_instant:
        return ItemIdentity(None, None, False, "metadata", "instant_generated")

    if explicit_reusable is not None and not _truthy(explicit_reusable):
        return ItemIdentity(None, None, False, "metadata", "marked_non_reusable")

    if explicit_canonical is not None:
        canonical_id = _stable_or_digest("ci", explicit_canonical)
        family_id = (
            _stable_or_digest("if", explicit_family)
            if explicit_family is not None
            else canonical_id
        )
        return ItemIdentity(
            canonical_id,
            family_id,
            True,
            canonical_source,
            "explicit_reusable_identity",
        )

    if source_type in _INSTANT_SOURCE_TYPES and not _truthy(explicit_reusable):
        return ItemIdentity(None, None, False, "source_type", "generated_without_reusable_identity")

    if explicit_family is not None:
        # A family key alone is enough to group variants, but not enough to
        # claim that two variants are the same item.  Use a durable variant
        # key when supplied; otherwise remain conservative.
        variant_key = question.get("variant_key") or metadata.get("variant_key")
        if variant_key is None:
            return ItemIdentity(None, _digest("if", explicit_family), False, family_source, "family_without_item_identity")
        canonical_id = _digest("ci", explicit_family, variant_key)
        return ItemIdentity(
            canonical_id,
            _digest("if", explicit_family),
            True,
            family_source,
            "explicit_family_and_variant",
        )

    question_id = question.get("id", question.get("question_id"))
    if question_id is not None and (source_type in _DURABLE_SOURCE_TYPES or _truthy(explicit_reusable)):
        canonical_id = _digest("ci", "question", question_id)
        return ItemIdentity(canonical_id, canonical_id, True, "question_id", "durable_question_id")

    return ItemIdentity(None, None, False, "none", "missing_reusable_identity")
