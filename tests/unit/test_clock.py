from datetime import UTC, datetime

from eduagents_core.clock import FixedClock, OffsetClock, SystemClock


def test_system_clock_is_utc_aware():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert abs((now - datetime.now(UTC)).total_seconds()) < 1


def test_offset_clock_applies_mutable_offset():
    offset = {"v": 0.0}
    clock = OffsetClock(lambda: offset["v"])
    base = clock.now()
    offset["v"] = 3 * 86400  # make time-travel DAYS=3
    shifted = clock.now()
    assert 3 * 86400 - 2 < (shifted - base).total_seconds() < 3 * 86400 + 2


def test_fixed_clock_advance():
    clock = FixedClock(datetime(2026, 7, 26, tzinfo=UTC))
    t0 = clock.now()
    clock.advance(60)
    assert (clock.now() - t0).total_seconds() == 60
