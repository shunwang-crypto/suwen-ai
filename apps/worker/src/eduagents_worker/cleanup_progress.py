"""Shared, non-sensitive progress hooks for lifecycle cleanup steps.

The outbox owns the lease and persistence of progress.  The concrete cleanup
functions only report a short component name and keep their external
operations idempotent.  This split makes a partially successful attempt
observable without putting payloads, object names, or exception text in
``progress_json``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

_COMPONENT_CODES = {
    "qdrant": "QDRANT_DELETE_FAILED",
    "storage": "STORAGE_DELETE_FAILED",
    "redis": "REDIS_CLEANUP_FAILED",
    "checkpoint": "CHECKPOINT_CLEANUP_FAILED",
}
_ALLOWED_ERROR_CODES = frozenset((*_COMPONENT_CODES.values(), "INVALID_COMPONENT"))


class CleanupComponentError(RuntimeError):
    """An external cleanup component failed with a stable redacted code."""

    def __init__(self, code: str, component: str, cause: BaseException) -> None:
        self.code = (
            code
            if isinstance(code, str) and code in _ALLOWED_ERROR_CODES
            else "EXTERNAL_ERROR"
        )
        self.component = (
            component
            if isinstance(component, str) and component in _COMPONENT_CODES
            else "unknown"
        )
        # Do not copy provider exception text into the public exception string;
        # the outbox stores only its separately sanitized error projection.
        self.cause_type = type(cause).__name__
        super().__init__(f"{self.component} cleanup failed")


class CleanupProgressError(RuntimeError):
    """The outbox could not persist a progress transition."""

    code = "PROGRESS_WRITE_FAILED"


CleanupProgress = Callable[[str, bool], Awaitable[None]]


async def run_cleanup_step(
    progress: CleanupProgress | None,
    component: str,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Report a component transition and execute one idempotent operation.

    Progress failures are deliberately allowed to escape unchanged.  An
    external failure is wrapped with a component-specific code; its original
    text is later passed through the outbox sanitizer before persistence.
    """
    if not isinstance(component, str) or component not in _COMPONENT_CODES:
        raise CleanupComponentError(
            "INVALID_COMPONENT", "unknown", ValueError("unsupported cleanup component")
        )
    if progress is not None:
        await progress(component, False)
    try:
        result = await operation()
    except CleanupComponentError:
        raise
    except Exception as exc:  # noqa: BLE001 - translated at the outbox boundary
        code = _COMPONENT_CODES.get(component, "EXTERNAL_ERROR")
        raise CleanupComponentError(code, component, exc) from exc
    if progress is not None:
        await progress(component, True)
    return result
