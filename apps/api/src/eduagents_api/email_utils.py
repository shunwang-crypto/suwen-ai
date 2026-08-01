"""Canonical email normalization used by authentication and abuse controls."""

from __future__ import annotations


def normalize_email(value: str) -> str:
    """Trim whitespace and compare email addresses case-insensitively.

    Provider-specific transformations (dots, plus tags) are intentionally not
    applied because they are not universal and could merge distinct mailboxes.
    """

    return value.strip().lower()
