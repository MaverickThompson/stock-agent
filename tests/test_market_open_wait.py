import datetime as dt
import importlib.util
import pathlib


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "wait_for_market_open.py"
SPEC = importlib.util.spec_from_file_location("wait_for_market_open", SCRIPT)
waiter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(waiter)


class Clock:
    def __init__(self, is_open, next_open):
        self.is_open = is_open
        self.next_open = next_open


class Broker:
    def __init__(self, clocks):
        self.clocks = iter(clocks)

    def market_clock(self):
        return next(self.clocks)


def test_wait_returns_without_sleep_when_market_is_open():
    slept = []
    clock = Clock(True, dt.datetime.now(dt.timezone.utc))

    waiter.wait_until_open(Broker([clock]), sleep_fn=slept.append)

    assert slept == []


def test_wait_sleeps_until_next_open_then_rechecks():
    now = dt.datetime(2026, 9, 24, 13, 20, tzinfo=dt.timezone.utc)
    opening = now + dt.timedelta(minutes=10)
    slept = []
    broker = Broker([
        Clock(False, opening),
        Clock(True, opening),
    ])

    ready = waiter.wait_until_open(
        broker,
        now_fn=lambda: now,
        sleep_fn=slept.append,
    )

    assert ready is True
    assert slept == [600.0]


def test_wait_skips_when_next_open_is_the_following_session():
    now = dt.datetime(2026, 9, 24, 20, 0, tzinfo=dt.timezone.utc)
    next_open = dt.datetime(2026, 9, 25, 13, 30, tzinfo=dt.timezone.utc)
    slept = []

    ready = waiter.wait_until_open(
        Broker([Clock(False, next_open)]),
        now_fn=lambda: now,
        sleep_fn=slept.append,
    )

    assert ready is False
    assert slept == []
