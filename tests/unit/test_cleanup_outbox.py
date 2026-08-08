"""Cleanup outbox payload, retry and redaction contracts."""

from __future__ import annotations

import uuid

import pytest
from eduagents_worker.cleanup_outbox import (
    CleanupPayloadError,
    retry_delay_seconds,
    sanitize_cleanup_error,
    validate_cleanup_payload,
)


def _ids() -> tuple[str, str, str]:
    return tuple(str(uuid.uuid4()) for _ in range(3))  # type: ignore[return-value]


def test_payload_validation_normalizes_each_supported_kind():
    tenant_id, kb_id, doc_id = _ids()

    doc = validate_cleanup_payload(
        "doc",
        {"tenant_id": tenant_id.upper(), "kb_id": kb_id, "doc_id": doc_id, "filename": "a.md"},
    )
    assert doc["tenant_id"] == tenant_id
    assert doc["filename"] == "a.md"
    assert validate_cleanup_payload("kb", {"tenant_id": tenant_id, "kb_id": kb_id})["kb_id"] == kb_id
    assert validate_cleanup_payload("tenant", {"tenant_id": tenant_id, "session_ids": ["s-1"]})[
        "session_ids"
    ] == ["s-1"]
    assert validate_cleanup_payload(
        "object_prefix", {"prefix": f"{tenant_id}/learning-resources/{doc_id}"}
    )["prefix"].startswith(tenant_id)


@pytest.mark.parametrize(
    ("kind", "payload", "code"),
    [
        ("unknown", {}, "UNKNOWN_KIND"),
        ("doc", {"tenant_id": str(uuid.uuid4())}, "INVALID_PAYLOAD"),
        ("tenant", {"tenant_id": str(uuid.uuid4()), "session_ids": "not-a-list"}, "INVALID_PAYLOAD"),
        ("object_prefix", {"prefix": "../../objects"}, "INVALID_PAYLOAD"),
    ],
)
def test_invalid_payload_is_rejected_before_external_cleanup(kind, payload, code):
    with pytest.raises(CleanupPayloadError) as exc_info:
        validate_cleanup_payload(kind, payload)
    assert exc_info.value.code == code


def test_retry_backoff_is_exponential_and_capped():
    delays = [retry_delay_seconds(attempts) for attempts in range(1, 8)]
    assert delays == sorted(delays)
    assert delays[1] == delays[0] * 2
    assert retry_delay_seconds(10_000) == 60 * 60


def test_cleanup_error_redaction_is_bounded_and_removes_credentials():
    secret_error = (
        "POST https://storage-user:storage-password@example.test/object?token=url-token "
        "Bearer bearer-token token=header-token secret=object-storage-secret "
        + "x" * 900
    )
    redacted = sanitize_cleanup_error(secret_error)
    assert len(redacted) <= 512
    assert "storage-password" not in redacted
    assert "url-token" not in redacted
    assert "bearer-token" not in redacted
    assert "header-token" not in redacted
    assert "object-storage-secret" not in redacted
