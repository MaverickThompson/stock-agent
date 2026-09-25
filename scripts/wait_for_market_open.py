#!/usr/bin/env python3
"""Wait until Alpaca reports that the regular market session is open."""

from __future__ import annotations

import datetime as dt
import os
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
    max_wait_seconds: float = 15 * 60,
) -> bool:
    """Wait for today's open; return false instead of waiting overnight."""
    now_fn = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
    while True:
        clock = broker.market_clock()
        if clock.is_open:
            return True
        seconds = max(
            0.0,
            (_as_utc(clock.next_open) - _as_utc(now_fn())).total_seconds(),
        )
        if seconds > max_wait_seconds:
            print("next market open is outside this session's wait window; skipping")
            return False
        sleep_fn(seconds)


if __name__ == "__main__":
    ready = wait_until_open(AlpacaBroker())
    output = pathlib.Path(os.environ["GITHUB_OUTPUT"]) if "GITHUB_OUTPUT" in os.environ else None
    if output:
        with output.open("a", encoding="utf-8") as handle:
            handle.write(f"ready={'true' if ready else 'false'}\n")
    raise SystemExit(0)
