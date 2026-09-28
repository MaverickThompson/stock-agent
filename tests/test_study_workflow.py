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

def test_v2_window_is_start_date_plus_60_successful_sessions():
    """Completion is driven by counted market sessions, not calendar dates."""
    import datetime as dt
    from stockagent.study_state import V2_SESSION_TARGET, V2_START_DATE

    assert V2_START_DATE == dt.date(2026, 9, 29)
    assert V2_SESSION_TARGET == 60


def test_session_is_inert_after_the_window(monkeypatch, tmp_path):
    """After 60 successes the session must take no action at all."""
    import datetime as dt
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
    after = dt.datetime(2026, 12, 16, 15, 0, tzinfo=dt.timezone.utc)
    result = run_session(broker=ExplodingBroker(), log=log, candidates=[],
                         thesis_for=lambda c: None, open_positions=[], now=after,
                         completed_sessions=60)
    assert result.entered == 0 and result.exited == 0
    rows = log.read("signals")
    assert any("window_closed" in str(r) for r in rows), rows


# --- Section 11 defect correction 2026-09-24: scheduler lateness -----------
#
# GitHub ran the single 14:45 UTC cron entry between 3.5 and 6 hours late, and
# on day 1 it landed after the close. These tests pin the shape of the fix so
# it cannot quietly regress back to one scheduled time.

def _crons() -> list[str]:
    import re
    workflow = WORKFLOW.read_text(encoding="utf-8")
    return re.findall(r'- cron: "([^"]+)"', workflow)


def test_the_session_is_attempted_many_times_a_day() -> None:
    """One scheduled time cannot survive a scheduler that is hours late."""
    assert len(_crons()) >= 12, (
        "too few attempts to reliably land inside market hours")


def test_attempts_span_both_dst_regimes() -> None:
    """The window crosses 2026-11-01: the US open moves 13:30 -> 14:30 UTC."""
    minutes = sorted(int(c.split()[1]) * 60 + int(c.split()[0]) for c in _crons())
    assert minutes[0] <= 13 * 60 + 30, "no attempt before the EDT open"
    assert minutes[-1] >= 20 * 60 + 0, "no attempt late enough for the EST session"
    # No gap wide enough for a whole trading session to slip through.
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    assert max(gaps) <= 35, f"largest gap between attempts is {max(gaps)} minutes"


def test_crons_avoid_the_top_of_the_hour() -> None:
    """GitHub's scheduling backlog is worst at :00; don't queue behind it."""
    for cron in _crons():
        assert cron.split()[0] != "0", f"{cron} sits in the :00 backlog"


def test_guard_runs_before_the_expensive_fetch() -> None:
    """The 18 firings that have nothing to do must not fetch 500 symbols."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    guard = workflow.index("id: guard")
    fetch = workflow.index("python scripts/fetch_data.py")
    assert guard < fetch


def test_every_working_step_is_gated_by_the_guard() -> None:
    """A step that escapes the guard would re-run a completed session.

    Parsed by hand rather than with PyYAML: PyYAML is not a declared dependency
    and a test that silently skips on the runner is not a test.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    body = workflow[workflow.index("    steps:"):]
    blocks = body.split("\n      - ")[1:]

    guard_seen = False
    for block in blocks:
        if "id: guard" in block:
            guard_seen = True
            continue
        if not guard_seen:
            continue  # checkout, which must precede the guard's read of study/
        label = block.splitlines()[0].strip()
        assert "steps.guard.outputs.skip == 'false'" in block, (
            f"step {label!r} escapes the guard")
    assert guard_seen, "no guard step in the workflow"


def test_session_marks_the_day_as_run(monkeypatch, tmp_path) -> None:
    """``ran`` is what tells the later firings the market day is finished."""
    import datetime as dt
    from stockagent.study_state import V2_START_DATE
    from stockagent.session import run_session
    from stockagent.study_log import StudyLog

    from test_session import FakeBroker

    log = StudyLog(tmp_path)
    result = run_session(broker=FakeBroker({}), log=log, candidates=[],
                         thesis_for=lambda c: None, open_positions=[],
                         now=dt.datetime.combine(V2_START_DATE,
                                                 dt.time(14, 0),
                                                 tzinfo=dt.timezone.utc))
    assert result.ran is True


def test_closed_market_does_not_mark_the_day_as_run(tmp_path) -> None:
    import datetime as dt
    from stockagent.study_state import V2_START_DATE
    from stockagent.session import run_session
    from stockagent.study_log import StudyLog

    from test_session import FakeBroker

    log = StudyLog(tmp_path)
    result = run_session(broker=FakeBroker({}, is_open=False), log=log, candidates=[],
                         thesis_for=lambda c: None, open_positions=[],
                         now=dt.datetime.combine(V2_START_DATE,
                                                 dt.time(12, 0),
                                                 tzinfo=dt.timezone.utc))
    assert result.ran is False


def test_repeat_firings_log_one_market_closed_row_per_day(tmp_path) -> None:
    """Nineteen firings on a holiday must not write nineteen identical rows."""
    import datetime as dt
    from stockagent.study_state import V2_START_DATE
    from stockagent.session import run_session
    from stockagent.study_log import StudyLog

    from test_session import FakeBroker

    broker = FakeBroker({}, is_open=False)
    log = StudyLog(tmp_path)
    day = V2_START_DATE
    for hour in (13, 14, 15, 16, 17):
        run_session(broker=broker, log=log, candidates=[],
                    thesis_for=lambda c: None, open_positions=[],
                    now=dt.datetime.combine(day, dt.time(hour, 0),
                                            tzinfo=dt.timezone.utc))

    rows = [r for r in log.read("signals") if r["triggered_rule"] == "market_closed"]
    assert len(rows) == 1, f"{len(rows)} market_closed rows for one day"

    # A different day still gets its own row.
    run_session(broker=broker, log=log, candidates=[],
                thesis_for=lambda c: None, open_positions=[],
                now=dt.datetime.combine(day + dt.timedelta(days=1),
                                        dt.time(15, 0), tzinfo=dt.timezone.utc))
    rows = [r for r in log.read("signals") if r["triggered_rule"] == "market_closed"]
    assert len(rows) == 2


def test_marker_path_matches_between_script_and_workflow() -> None:
    """The guard reads exactly the file the session writes, or it is useless."""
    script = (pathlib.Path(__file__).parent.parent
              / "scripts" / "run_session.py").read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'LAST_SESSION_PATH = STUDY_DIR / "last_session.txt"' in script
    assert "marker=study/last_session.txt" in workflow
    assert "study/last_session.txt" in workflow[workflow.index("Commit the logs"):], (
        "the marker is written but never committed, so it resets every run")


def test_marker_is_only_written_after_a_session_that_ran() -> None:
    """Writing it unconditionally would skip the day the market was open."""
    script = (pathlib.Path(__file__).parent.parent
              / "scripts" / "run_session.py").read_text(encoding="utf-8")
    write = script.index("LAST_SESSION_PATH.write_text")
    guard = script.index("if result.ran and not dry_run:")
    assert guard < write


def test_commit_step_keeps_all_session_state_and_logs_together() -> None:
    """Progress and its related artifacts must survive the same workflow run."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    commit = workflow[workflow.index("- name: Commit the logs"):]

    for path in (
        "study/signals.csv",
        "study/trades.csv",
        "study/open_positions.json",
        "study/v2_state.json",
        "study/last_session.txt",
    ):
        assert path in commit, f"{path} is not committed with the study run"


def test_checkout_resolves_the_branch_not_the_triggering_sha() -> None:
    """A queued firing must not restore a tree from before the marker landed.

    The default checkout pins to github.sha, the tip when the run was created.
    Under the concurrency group a run can sit queued while the real session
    runs and pushes; a defaulted checkout would then read a stale marker and
    trade the same day twice.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    checkout = workflow.index("actions/checkout")
    guard = workflow.index("id: guard")
    assert "ref: main" in workflow[checkout:guard], (
        "checkout does not pin to the branch, so the guard can read a stale marker")


def _run_session_script(monkeypatch, tmp_path, state, *, is_open=True,
                       dry_run=False, run_session=None):
    import datetime as dt
    import importlib.util
    import sys
    import types

    from stockagent import study_state
    from test_session import FakeBroker

    script = pathlib.Path(__file__).parent.parent / "scripts" / "run_session.py"
    spec = importlib.util.spec_from_file_location("study_run_session", script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    study_dir = tmp_path / "study"
    monkeypatch.setattr(runner, "STUDY_DIR", study_dir)
    monkeypatch.setattr(runner, "STATE_PATH", study_dir / "open_positions.json")
    monkeypatch.setattr(runner, "V2_STATE_PATH", study_dir / "v2_state.json")
    monkeypatch.setattr(runner, "LAST_SESSION_PATH", study_dir / "last_session.txt")
    runner.V2_STATE_PATH.parent.mkdir(parents=True)
    state.save(runner.V2_STATE_PATH)

    real_datetime = dt.datetime

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 29, 15, 0, tzinfo=tz)

    monkeypatch.setattr(runner.dt, "datetime", FrozenDateTime)
    monkeypatch.setenv("STUDY_DRY_RUN", "true" if dry_run else "false")
    monkeypatch.setattr(runner.observability, "init_sentry", lambda **kwargs: None)
    monkeypatch.setattr(runner.observability, "flush", lambda: None)
    monkeypatch.setattr(
        runner, "AlpacaBroker",
        lambda: FakeBroker({}, is_open=is_open))

    adapter = types.ModuleType("stockagent.study_adapter")
    adapter.candidates_for_session = lambda: []
    adapter.thesis_for = lambda candidate: None
    monkeypatch.setitem(sys.modules, "stockagent.study_adapter", adapter)
    if run_session is not None:
        monkeypatch.setattr(runner, "run_session", run_session)

    status = runner.main()
    return status, runner, study_state.StudyState.load(runner.V2_STATE_PATH)


def test_runner_records_one_successful_live_session(monkeypatch, tmp_path):
    import datetime as dt
    from stockagent.study_state import StudyState, V2_START_DATE

    initial = StudyState(1, V2_START_DATE, 0, None, "active")

    status, runner, updated = _run_session_script(monkeypatch, tmp_path, initial)

    assert status == 0
    assert updated.completed_sessions == 1
    assert updated.last_session_date == dt.date(2026, 9, 29)
    assert runner.LAST_SESSION_PATH.read_text(encoding="utf-8").strip() == "2026-09-29"


def test_runner_does_not_increment_the_same_date_twice(monkeypatch, tmp_path):
    import datetime as dt
    from stockagent.study_state import StudyState, V2_START_DATE

    initial = StudyState(1, V2_START_DATE, 1, dt.date(2026, 9, 29), "active")

    def unexpected_run(**kwargs):
        raise AssertionError("a duplicate date must not run again")

    status, runner, unchanged = _run_session_script(
        monkeypatch, tmp_path, initial, run_session=unexpected_run)

    assert status == 0
    assert unchanged == initial
    assert not runner.LAST_SESSION_PATH.exists()


def test_runner_does_not_increment_a_closed_market_attempt(monkeypatch, tmp_path):
    from stockagent.study_state import StudyState, V2_START_DATE

    initial = StudyState(1, V2_START_DATE, 0, None, "active")

    status, runner, unchanged = _run_session_script(
        monkeypatch, tmp_path, initial, is_open=False)

    assert status == 0
    assert unchanged == initial
    assert not runner.LAST_SESSION_PATH.exists()


def test_runner_does_not_persist_v2_progress_for_a_dry_run(monkeypatch, tmp_path):
    from stockagent.study_state import StudyState, V2_START_DATE

    initial = StudyState(1, V2_START_DATE, 0, None, "active")

    status, runner, unchanged = _run_session_script(
        monkeypatch, tmp_path, initial, dry_run=True)

    assert status == 0
    assert unchanged == initial
    assert not runner.LAST_SESSION_PATH.exists()
