"""Component-specific cleanup errors and non-sensitive progress callbacks."""

from __future__ import annotations

import pytest
from eduagents_worker.cleanup_progress import CleanupComponentError, run_cleanup_step


@pytest.mark.asyncio
async def test_cleanup_step_reports_started_and_completed():
    events: list[tuple[str, bool]] = []

    async def progress(component: str, completed: bool):
        events.append((component, completed))

    async def operation():
        return "ok"

    assert await run_cleanup_step(progress, "qdrant", operation) == "ok"
    assert events == [("qdrant", False), ("qdrant", True)]


@pytest.mark.asyncio
async def test_cleanup_step_maps_external_failure_to_stable_code():
    async def operation():
        raise RuntimeError("secret=must-not-become-a-code")

    with pytest.raises(CleanupComponentError) as error:
        await run_cleanup_step(None, "storage", operation)
    assert error.value.code == "STORAGE_DELETE_FAILED"
    assert "must-not-become-a-code" not in str(error.value)


@pytest.mark.asyncio
async def test_cleanup_step_rejects_unknown_component_before_progress_or_operation():
    called = False

    async def progress(_component: str, _completed: bool):
        raise AssertionError("progress must not receive an untrusted component")

    async def operation():
        nonlocal called
        called = True

    with pytest.raises(CleanupComponentError) as error:
        await run_cleanup_step(progress, "https://secret.example", operation)

    assert error.value.code == "INVALID_COMPONENT"
    assert error.value.component == "unknown"
    assert not called
    assert "secret.example" not in str(error.value)


@pytest.mark.asyncio
async def test_cleanup_step_does_not_accept_untrusted_component_error_code():
    async def operation():
        raise CleanupComponentError(
            "token=must-not-persist", "storage", ValueError("provider failure")
        )

    with pytest.raises(CleanupComponentError) as error:
        await run_cleanup_step(None, "storage", operation)

    assert error.value.code == "EXTERNAL_ERROR"
    assert "must-not-persist" not in str(error.value)
