"""Regression tests for the post-close V2 session watchdog."""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys
from zoneinfo import ZoneInfo

from stockagent.study_state import StudyState, V2_SESSION_TARGET, V2_START_DATE

ROOT = pathlib.Path(__file__).parent.parent
HEALTH_WORKFLOW = ROOT / ".github" / "workflows" / "study-health-check.yml"
HEALTH_SCRIPT = ROOT / "scripts" / "check_study_health.py"
HEALTH_SPEC = importlib.util.spec_from_file_location(
    "check_study_health", HEALTH_SCRIPT)
assert HEALTH_SPEC is not None and HEALTH_SPEC.loader is not None
health_module = importlib.util.module_from_spec(HEALTH_SPEC)
sys.modules[HEALTH_SPEC.name] = health_module
HEALTH_SPEC.loader.exec_module(health_module)
check_study_health = health_module.check_study_health


def _state(*, completed=0, last_session_date=None, status="active"):
    return StudyState(
        schema_version=1,
        start_date=V2_START_DATE,
        completed_sessions=completed,
        last_session_date=last_session_date,
        status=status,
    )


def test_exchange_holiday_is_healthy_without_a_completed_session(tmp_path):
    market_date = dt.date(2026, 11, 26)
    summary = tmp_path / "summary.md"

    healthy = check_study_health(
        _state(),
        market_date,
        calendar_sessions=[],
        summary_path=summary,
    )

    assert healthy is True
    assert "healthy" in summary.read_text(encoding="utf-8").lower()


def test_completed_market_day_is_healthy(tmp_path):
    market_date = dt.date(2026, 9, 29)
    summary = tmp_path / "summary.md"

    healthy = check_study_health(
        _state(completed=1, last_session_date=market_date),
        market_date,
        calendar_sessions=[market_date],
        summary_path=summary,
    )

    assert healthy is True
    assert "healthy" in summary.read_text(encoding="utf-8").lower()


def test_active_v2_missed_market_day_writes_recovery_instructions(tmp_path):
    market_date = dt.date(2026, 9, 30)
    summary = tmp_path / "summary.md"

    healthy = check_study_health(
        _state(),
        market_date,
        calendar_sessions=[market_date],
        summary_path=summary,
    )

    report = summary.read_text(encoding="utf-8")
    assert healthy is False
    assert market_date.isoformat() in report
    assert "0" in report
    assert "study/v2_state.json" in report
    assert "study/last_session.txt" in report
    assert "workflow_dispatch" in report
    assert "next open market session" in report.lower()


def test_active_session_is_pending_until_actual_calendar_close(tmp_path):
    market_date = dt.date(2026, 9, 30)
    summary = tmp_path / "summary.md"
    ny = ZoneInfo("America/New_York")

    healthy = check_study_health(
        _state(), market_date, calendar_sessions=[market_date],
        summary_path=summary, session_close=dt.time(16, 0),
        now=dt.datetime(2026, 9, 30, 15, 59, tzinfo=ny))

    assert healthy is True
    assert "pending" in summary.read_text(encoding="utf-8").lower()


def test_active_session_is_missed_after_calendar_close(tmp_path):
    market_date = dt.date(2026, 9, 30)
    summary = tmp_path / "summary.md"
    ny = ZoneInfo("America/New_York")

    healthy = check_study_health(
        _state(), market_date, calendar_sessions=[market_date],
        summary_path=summary, session_close=dt.time(16, 0),
        now=dt.datetime(2026, 9, 30, 16, 1, tzinfo=ny))

    assert healthy is False
    assert "MISSED MARKET SESSION" in summary.read_text(encoding="utf-8")


def test_early_close_uses_alpaca_close_time(tmp_path):
    market_date = dt.date(2026, 11, 27)
    summary = tmp_path / "summary.md"
    ny = ZoneInfo("America/New_York")

    healthy = check_study_health(
        _state(), market_date, calendar_sessions=[market_date],
        summary_path=summary, session_close=dt.time(13, 0),
        now=dt.datetime(2026, 11, 27, 13, 1, tzinfo=ny))

    assert healthy is False


def test_v2_not_started_does_not_report_a_missed_session(tmp_path):
    market_date = dt.date(2026, 9, 28)
    summary = tmp_path / "summary.md"

    healthy = check_study_health(
        _state(),
        market_date,
        calendar_sessions=[market_date],
        summary_path=summary,
    )

    assert healthy is True
    assert "not started" in summary.read_text(encoding="utf-8").lower()


def test_completed_v2_does_not_report_a_missed_session(tmp_path):
    market_date = dt.date(2026, 12, 17)
    last_session_date = dt.date(2026, 12, 16)
    summary = tmp_path / "summary.md"

    healthy = check_study_health(
        _state(
            completed=V2_SESSION_TARGET,
            last_session_date=last_session_date,
            status="complete",
        ),
        market_date,
        calendar_sessions=[market_date],
        summary_path=summary,
    )

    assert healthy is True
    assert "complete" in summary.read_text(encoding="utf-8").lower()


def test_health_workflow_is_calendar_aware_read_only_and_manually_runnable():
    workflow = HEALTH_WORKFLOW.read_text(encoding="utf-8")

    assert '- cron: "30 22 * * 1-5"' in workflow
    assert "  workflow_dispatch:" in workflow
    assert "permissions:\n  contents: read\n  actions: read" in workflow
    assert "ref: main" in workflow
    assert "pip install -r requirements.txt" in workflow
    assert "python scripts/check_study_health.py" in workflow
    assert "ALPACA_API_KEY: ${{ secrets.ALPACA_API_KEY }}" in workflow
    assert "ALPACA_SECRET_KEY: ${{ secrets.ALPACA_SECRET_KEY }}" in workflow


def test_broker_calendar_sessions_uses_typed_inclusive_alpaca_request():
    from stockagent.broker import AlpacaBroker

    start = dt.date(2026, 11, 26)
    end = dt.date(2026, 11, 26)
    calendar_date = dt.date(2026, 11, 27)

    class CalendarClient:
        request = None

        def get_calendar(self, request):
            self.request = request
            return [type("CalendarEntry", (), {"date": calendar_date})()]

    client = CalendarClient()
    broker = object.__new__(AlpacaBroker)
    broker._trading = client

    assert broker.calendar_sessions(start, end) == [calendar_date]
    assert client.request.start == start
    assert client.request.end == end


def test_broker_calendar_returns_actual_early_close_time():
    from stockagent.broker import AlpacaBroker

    market_date = dt.date(2026, 11, 27)

    class CalendarClient:
        def get_calendar(self, request):
            return [type("CalendarEntry", (), {
                "date": market_date, "close": "13:00",
            })()]

    broker = object.__new__(AlpacaBroker)
    broker._trading = CalendarClient()

    assert broker.calendar_sessions_with_closes(
        market_date, market_date) == [(market_date, dt.time(13, 0))]
