#!/usr/bin/env python3
"""Wait until Alpaca reports that the regular market session is open."""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import time
from collections.abc import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from stockagent.broker import AlpacaBroker  # noqa: E402


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def wait_until_open(
    broker: AlpacaBroker,
    *,
    now_fn: Callable[[], dt.datetime] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Block until the broker clock enters regular market hours."""
    now_fn = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
    while True:
        clock = broker.market_clock()
        if clock.is_open:
            return
        seconds = max(
            0.0,
            (_as_utc(clock.next_open) - _as_utc(now_fn())).total_seconds(),
        )
        sleep_fn(seconds)


if __name__ == "__main__":
    wait_until_open(AlpacaBroker())
