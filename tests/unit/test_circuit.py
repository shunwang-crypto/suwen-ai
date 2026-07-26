from eduagents_core.gateway.circuit import BreakerState, CircuitBreaker


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(ft: FakeTime) -> CircuitBreaker:
    return CircuitBreaker(
        window_seconds=60, min_samples=4, failure_rate=0.5, open_seconds=30, time_func=ft
    )


def test_opens_on_failure_rate():
    ft = FakeTime()
    b = make(ft)
    for _ in range(4):
        assert b.allow()
        b.record(False)
    assert b.state == BreakerState.open
    assert not b.allow()


def test_half_open_probe_then_close():
    ft = FakeTime()
    b = make(ft)
    for _ in range(4):
        b.record(False)
    assert b.state == BreakerState.open
    ft.t += 31  # cooldown elapsed -> half-open, single probe
    assert b.allow()
    assert not b.allow()  # only one probe in flight
    b.record(True)
    assert b.state == BreakerState.closed
    assert b.allow()


def test_half_open_probe_failure_reopens():
    ft = FakeTime()
    b = make(ft)
    for _ in range(4):
        b.record(False)
    ft.t += 31
    assert b.allow()
    b.record(False)
    assert b.state == BreakerState.open
    assert not b.allow()


def test_old_events_slide_out_of_window():
    ft = FakeTime()
    b = make(ft)
    for _ in range(3):
        b.record(False)
    ft.t += 61  # failures leave the window
    for _ in range(4):
        b.record(True)
    assert b.state == BreakerState.closed
