from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """All business time reads go through this. Never call datetime.now() in
    domain code: BKT decay, FSRS scheduling, daily-review queues and plan
    calendars must obey the virtual clock so `make time-travel` works."""

    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class OffsetClock:
    """System time shifted by a mutable offset (seconds).

    The offset is read through a provider callable so the API layer can back
    it with Redis (shared across api/worker processes) while unit tests use a
    plain variable.
    """

    def __init__(self, offset_provider: Callable[[], float], base: Clock | None = None):
        self._offset_provider = offset_provider
        self._base = base or SystemClock()

    def now(self) -> datetime:
        return self._base.now() + timedelta(seconds=self._offset_provider())


class FixedClock:
    """Test helper: time stands still until told otherwise."""

    def __init__(self, at: datetime):
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, seconds: float) -> None:
        self._at = self._at + timedelta(seconds=seconds)
