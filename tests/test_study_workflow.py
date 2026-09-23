"""Regression coverage for the unattended study workflow."""

from __future__ import annotations

import pathlib


WORKFLOW = (
    pathlib.Path(__file__).parent.parent / ".github" / "workflows" / "study-session.yml"
)


def test_session_fetches_price_history_before_running() -> None:
    """A clean Actions checkout must bootstrap the ignored ``data/`` directory."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    fetch = workflow.index("python scripts/fetch_data.py")
    session = workflow.index("python scripts/run_session.py")

    assert fetch < session


def test_session_exposes_the_earnings_calendar_secret() -> None:
    """Candidate evaluation needs the calendar rather than an unknown fallback."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "ALPHAVANTAGE_API_KEY: ${{ secrets.ALPHAVANTAGE_API_KEY }}" in workflow


# --- Section 12 window boundary ------------------------------------------

def test_window_dates_span_60_market_days():
    """Day 1 to Day 60 inclusive, weekdays, minus Thanksgiving 2026-11-26."""
    import datetime as dt
    from stockagent import study_rules as rules
    holidays = {dt.date(2026, 11, 26)}
    d, n = rules.STUDY_DAY_1, 0
    while d <= rules.STUDY_DAY_60:
        if d.weekday() < 5 and d not in holidays:
            n += 1
        d += dt.timedelta(days=1)
    assert n == 60, f"window spans {n} market days, not 60"


def test_session_is_inert_after_the_window(monkeypatch, tmp_path):
    """After Day 60 the session must take no action at all."""
    import datetime as dt
    from stockagent import study_rules as rules
    from stockagent.session import run_session
    from stockagent.study_log import StudyLog

    class ExplodingBroker:
        def market_is_open(self):
            raise AssertionError("broker must not be touched after the window")
        def quote(self, *a, **k):
            raise AssertionError("broker must not be touched after the window")
        def submit(self, *a, **k):
            raise AssertionError("no orders after the window")

    log = StudyLog(tmp_path)
    after = dt.datetime.combine(rules.STUDY_DAY_60 + dt.timedelta(days=1),
                                dt.time(15, 0), tzinfo=dt.timezone.utc)
    result = run_session(broker=ExplodingBroker(), log=log, candidates=[],
                         thesis_for=lambda c: None, open_positions=[], now=after)
    assert result.entered == 0 and result.exited == 0
    rows = log.read("signals")
    assert any("window_closed" in str(r) for r in rows), rows
