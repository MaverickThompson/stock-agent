"""Detect missed V2 live-session evaluations after the market close."""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
from collections.abc import Iterable
from zoneinfo import ZoneInfo

from stockagent.broker import AlpacaBroker
from stockagent.study_state import StudyState, V2_SESSION_TARGET

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = ROOT / "study" / "v2_state.json"
DEFAULT_MARKER_PATH = ROOT / "study" / "last_session.txt"


class StudyHealthAlert(RuntimeError):
    """Carries a watchdog finding into Sentry as its own issue group.

    A missed session is *silence* -- no exception is raised anywhere, so
    nothing would otherwise reach Sentry. Sentry catches crashes; the two
    failures this study has actually suffered (a cron firing 5h16m late, and
    a state file reading 0 while two positions were open) were both silent.
    This class is what turns absence into an event.
    """


def _alert(message: str, *, stage: str) -> None:
    """Report a watchdog finding to Sentry if a DSN is configured.

    Deliberately swallows everything: the watchdog's exit code is the
    contract with CI, and a failure to *report* a missed session must never
    change whether that session is recorded as missed.
    """
    try:
        from stockagent import observability
        if not observability.init_sentry(environment="watchdog"):
            return
        try:
            raise StudyHealthAlert(message)
        except StudyHealthAlert as exc:
            observability.report(exc, stage=stage)
        observability.flush()
    except Exception:  # noqa: BLE001 - alerting must not affect the verdict
        pass


def _write_summary(summary_path: pathlib.Path | str, text: str) -> None:
    if str(summary_path) == "-":
        print(text)
        return
    destination = pathlib.Path(summary_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text.rstrip() + "\n", encoding="utf-8")


def check_study_health(
    state: StudyState,
    market_date: dt.date,
    calendar_sessions: Iterable[dt.date],
    summary_path: pathlib.Path | str,
    *,
    session_close: dt.time | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Return false only when an eligible Alpaca session was not completed."""
    if not isinstance(state, StudyState):
        raise TypeError("state must be a StudyState")
    if type(market_date) is not dt.date:
        raise TypeError("market_date must be a date")
    if session_close is not None and not isinstance(session_close, dt.time):
        raise TypeError("session_close must be a time")

    is_session = market_date in set(calendar_sessions)
    new_york = ZoneInfo("America/New_York")
    local_now = (now.astimezone(new_york) if now and now.tzinfo
                 else now.replace(tzinfo=new_york) if now
                 else None)
    is_current_session = bool(
        is_session and local_now and local_now.date() == market_date)

    if not is_session:
        message = (
            f"## Study health: healthy\n\n"
            f"Alpaca calendar has no market session on {market_date.isoformat()}; "
            "no V2 evaluation was expected."
        )
    elif state.status == "complete":
        message = (
            "## Study health: healthy\n\n"
            f"V2 is complete at {state.completed_sessions}/{V2_SESSION_TARGET} "
            "successful live market-session evaluations."
        )
    elif market_date < state.start_date:
        message = (
            "## Study health: healthy\n\n"
            f"V2 has not started yet (start date {state.start_date.isoformat()}); "
            f"{market_date.isoformat()} was not eligible."
        )
    elif (state.last_session_date is not None
          and state.last_session_date >= market_date):
        message = (
            "## Study health: healthy\n\n"
            f"The V2 state records a completed session on or after "
            f"{market_date.isoformat()} "
            f"({state.completed_sessions}/{V2_SESSION_TARGET} successful "
            "evaluations)."
        )
    else:
        if is_current_session:
            if session_close is None:
                raise ValueError(
                    "Alpaca calendar close time is required for today's session")
            close_at = dt.datetime.combine(
                market_date, session_close, tzinfo=new_york)
            if local_now < close_at:
                message = (
                    "## Study health: healthy\n\n"
                    f"The Alpaca session for {market_date.isoformat()} is still "
                    f"in progress (scheduled close {session_close.isoformat()} "
                    "New York time); the V2 evaluation is pending."
                )
                _write_summary(summary_path, message)
                return True
        state_path = DEFAULT_STATE_PATH.relative_to(ROOT).as_posix()
        marker_path = DEFAULT_MARKER_PATH.relative_to(ROOT).as_posix()
        message = (
            "## Study health: MISSED MARKET SESSION\n\n"
            f"- Market date: **{market_date.isoformat()}**\n"
            f"- Completed V2 live market-session evaluations: "
            f"**{state.completed_sessions}/{V2_SESSION_TARGET}**\n"
            f"- Expected state file: `{state_path}`\n"
            f"- Expected daily marker: `{marker_path}` "
            f"(expected value: `{market_date.isoformat()}`)\n\n"
            "To recover, use `workflow_dispatch` to run the **Study session** "
            "workflow during the next open market session:\n\n"
            "```sh\n"
            "gh workflow run study-session.yml --ref main\n"
            "```\n\n"
            "The session workflow will re-check V2 eligibility and the Alpaca "
            "market clock before evaluating. Scheduled attempts and this "
            "watchdog improve recovery and detection but do not guarantee "
            "uptime; GitHub and provider availability are required."
        )
        _write_summary(summary_path, message)
        return False

    _write_summary(summary_path, message)
    return True


def main() -> int:
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    market_date = now.date()
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "-")
    try:
        state = StudyState.load(DEFAULT_STATE_PATH)
        broker = AlpacaBroker()
        sessions = broker.calendar_sessions_with_closes(market_date, market_date)
        calendar_sessions = [session_date for session_date, _close in sessions]
        session_close = next(
            (close for session_date, close in sessions
             if session_date == market_date), None)
        if market_date in calendar_sessions and session_close is None:
            raise ValueError("Alpaca calendar did not return today's session close")
    except Exception as error:  # noqa: BLE001
        message = (
            "## Study health: unable to verify\n\n"
            f"- New York market date: **{market_date.isoformat()}**\n"
            f"- State file: `{DEFAULT_STATE_PATH.relative_to(ROOT).as_posix()}`\n"
            f"- Error: `{type(error).__name__}: {error}`\n\n"
            "Check the Alpaca calendar API, the repository's "
            "`ALPACA_API_KEY` and `ALPACA_SECRET_KEY` secrets, and the state "
            "file; then rerun this workflow. The watchdog could not determine "
            "whether a market session was missed."
        )
        _write_summary(summary_path, message)
        _alert(
            f"Study health check could not verify {market_date.isoformat()}: "
            f"{type(error).__name__}: {error}",
            stage="study_health_unverifiable")
        if summary_path != "-":
            print(f"Study health verification failed for {market_date}: {error}",
                  file=sys.stderr)
        return 1

    healthy = check_study_health(
        state, market_date, calendar_sessions, summary_path,
        session_close=session_close,
        now=now)
    if not healthy:
        _alert(
            f"V2 study missed the Alpaca market session on "
            f"{market_date.isoformat()} (completed_sessions="
            f"{state.completed_sessions}, last_session_date="
            f"{state.last_session_date})",
            stage="study_health_missed_session")
    if summary_path != "-":
        print(
            "Study health: healthy" if healthy else
            f"Study health: missed Alpaca session on {market_date}",
            file=sys.stdout if healthy else sys.stderr,
        )
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
