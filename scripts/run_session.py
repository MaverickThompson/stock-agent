#!/usr/bin/env python3
"""Entry point for one study session. Invoked by .github/workflows/study-session.yml.

This is deliberately thin. It wires the broker, the log and the analysis layer
together and hands off to :func:`stockagent.session.run_session`; all the
protocol logic lives in ``study_rules`` and ``session`` where it is unit
tested. Keeping the scheduled entry point dumb means the thing that runs
unattended for sixty days is the thing the tests actually cover.

Exit codes:
  0  the session ran (including a session that legitimately did nothing)
  1  the session could not run at all, and said so in signals.csv
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from stockagent import observability  # noqa: E402
from stockagent.broker import AlpacaBroker  # noqa: E402
from stockagent.session import OpenPosition, run_session  # noqa: E402
from stockagent.study_log import StudyLog, dry_run_passed  # noqa: E402

STUDY_DIR = pathlib.Path(__file__).resolve().parent.parent / "study"
STATE_PATH = STUDY_DIR / "open_positions.json"

#: The UTC date of the last session that actually ran with the market open.
#: The workflow fires many times a day because GitHub's scheduler is late by
#: hours (Section 11 defect correction, 2026-09-24); this marker is how the
#: later firings know the day is already done. It is operational state, not a
#: study log -- the two frozen Section 10 schemas are untouched.
LAST_SESSION_PATH = STUDY_DIR / "last_session.txt"


def load_open_positions() -> list[OpenPosition]:
    """Open positions carried between sessions.

    Held as JSON beside the logs rather than reconstructed from the broker:
    the stop, the targets and the thesis are study state, not broker state,
    and Alpaca has no idea what any of them are.
    """
    if not STATE_PATH.exists():
        return []
    raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return [OpenPosition(**row) for row in raw]


def save_open_positions(positions: list[OpenPosition]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps([p.__dict__ for p in positions], indent=2, sort_keys=True),
        encoding="utf-8")


def main() -> int:
    observability.init_sentry(
        environment="study",
        release=os.environ.get("GITHUB_SHA", "local")[:12])

    log = StudyLog(STUDY_DIR)
    positions = load_open_positions()
    dry_run = os.environ.get("STUDY_DRY_RUN", "").strip().lower() in {
        "1", "true", "yes", "on"
    }

    try:
        broker = AlpacaBroker()
    except Exception as exc:  # noqa: BLE001
        # Section 10: a connector outage is a logged SYSTEM_ERROR row, never a
        # silent absence.
        log.log_system_error(stage="broker_init", detail=f"{type(exc).__name__}: {exc}")
        observability.report(exc, stage="broker_init")
        observability.flush()
        print(f"broker unavailable: {exc}", file=sys.stderr)
        return 1

    # Candidate generation is wired in the adapter below. Until it is
    # connected the session still runs, manages open positions and logs a
    # SYSTEM_ERROR describing the gap -- which is what a dry run needs to prove.
    candidates: list = []
    def thesis_for(_candidate):
        return None

    try:
        from stockagent.study_adapter import candidates_for_session, thesis_for as _t
        candidates = candidates_for_session()
        thesis_for = _t
    except ImportError:
        log.log_system_error(
            stage="analysis_adapter",
            detail="study_adapter not present; open positions were managed but "
                   "no new entries were evaluated this session")

    try:
        result = run_session(broker=broker, log=log, candidates=candidates,
                             thesis_for=thesis_for, open_positions=positions,
                             dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        log.log_system_error(stage="session", detail=f"{type(exc).__name__}: {exc}")
        observability.report(exc, stage="session")
        observability.flush()
        raise

    if not dry_run:
        save_open_positions(positions)
        if result.ran:
            LAST_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            LAST_SESSION_PATH.write_text(
                dt.datetime.now(dt.timezone.utc).date().isoformat() + "\n",
                encoding="utf-8")
    counts = log.row_counts()
    print(f"{result.summary()} | rows: signals={counts['signals']} "
          f"trades={counts['trades']} | dry-run-passing={dry_run_passed(counts)}")
    observability.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
