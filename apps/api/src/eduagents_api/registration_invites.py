"""Generation and storage-safe normalization for platform registration invites."""

from __future__ import annotations

import hashlib
import secrets

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 24


def normalize_invite_code(value: str) -> str:
    return value.strip().upper()


def generate_invite_code() -> str:
    """Return a high-entropy, copy-friendly code; plaintext is caller-owned."""
    token = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
    return f"EDU-{token}"


def hash_invite_code(value: str) -> str:
    return hashlib.sha256(normalize_invite_code(value).encode("ascii")).hexdigest()
