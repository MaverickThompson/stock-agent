#!/usr/bin/env python3
"""Skip scheduled attempts outside V2's date/count window or after today's run."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from stockagent.study_state import (  # noqa: E402
    StudyState,
    V2_SESSION_TARGET,
    V2_START_DATE,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = ROOT / "study" / "v2_state.json"
DEFAULT_MARKER_PATH = ROOT / "study" / "last_session.txt"

DEFAULT_SIGNALS_PATH = ROOT / "study" / "signals.csv"


def signals_logged_for(signals_path: pathlib.Path, today: dt.date) -> bool:
    """Whether the signal log already holds rows for ``today``.

    This is the only witness of an attempt that survives a failed run.
    ``v2_state.json`` and ``last_session.txt`` are staged by the workflow only
    when ``run_session`` succeeds, but ``signals.csv`` is staged ``if:
    always()``. On 2026-10-06 run #164 traded and then exited non-zero, so the
    state files stayed stale and four later cron arrivals re-ran the whole
    session, logging the same twenty evaluations five times.
    """
    if not signals_path.exists():
        return False
    stamp = today.isoformat()
    try:
        with signals_path.open(encoding="utf-8") as handle:
            next(handle, None)  # header row
            return any(line.startswith(stamp) for line in handle)
    except OSError:  # an unreadable log must not silently permit a re-run
        return True



def is_study_eligible(
    state: StudyState,
    today: dt.date,
    marker_contents: str = "",
    signals_logged: bool = False,
) -> bool:
    """Return whether a live V2 session may run on ``today``."""
    return (
        today >= V2_START_DATE
        and state.status == "active"
        and state.completed_sessions < V2_SESSION_TARGET
        and state.is_eligible(today)
        and marker_contents.strip() != today.isoformat()
        # An attempt that logged rows today is not retried, even when
        # it failed before writing the marker.
        and not signals_logged
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=pathlib.Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--marker", type=pathlib.Path, default=DEFAULT_MARKER_PATH)
    parser.add_argument(
        "--signals", type=pathlib.Path, default=DEFAULT_SIGNALS_PATH,
        help="signal log consulted for an already-attempted date")
    parser.add_argument(
        "--today",
        help="UTC ISO date override for deterministic tests (defaults to current UTC date)",
    )
    args = parser.parse_args()

    today = (
        dt.date.fromisoformat(args.today)
        if args.today
        else dt.datetime.now(dt.timezone.utc).date()
    )
    state = StudyState.load(args.state)
    marker_contents = (
        args.marker.read_text(encoding="utf-8") if args.marker.exists() else ""
    )
    signals_logged = signals_logged_for(args.signals, today)
    eligible = is_study_eligible(
        state, today, marker_contents, signals_logged)
    reason = (
        "eligible"
        if eligible
        else (
            f"ineligible: date={today.isoformat()}, start={V2_START_DATE.isoformat()}, "
            f"completed={state.completed_sessions}/{V2_SESSION_TARGET}, "
            f"status={state.status}, marker={marker_contents.strip() or 'absent'}, "
            f"signals_logged={signals_logged}"
        )
    )
    print(f"{reason}; skip={'false' if eligible else 'true'}")

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with pathlib.Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"skip={'false' if eligible else 'true'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
