"""The running configuration must be the one the protocol records."""

from __future__ import annotations

import dataclasses

import pytest

from stockagent import study_rules as rules
from stockagent.conformance import (
    V2_TARGET_R_MULTIPLES,
    check_conformance,
    config_fingerprint,
    record_session_provenance,
)
from stockagent.config import Config


def test_the_shipped_config_is_the_recorded_v2_mapping() -> None:
    assert check_conformance(Config()) is None


def test_v2_mapping_matches_the_constant() -> None:
    assert Config().risk.target_r_multiples == V2_TARGET_R_MULTIPLES


def test_the_v1_defect_is_now_caught_before_the_session_acts() -> None:
    cfg = Config()
    cfg.risk = dataclasses.replace(cfg.risk, target_r_multiples=(1.5, 3.0))
    problem = check_conformance(cfg)
    assert problem is not None
    assert "unsatisfiable by construction" in problem


def test_a_floor_that_disagrees_with_the_rules_module_is_caught() -> None:
    cfg = Config()
    cfg.risk = dataclasses.replace(cfg.risk, min_reward_risk=1.5)
    problem = check_conformance(cfg)
    assert problem is not None
    assert "disagrees" in problem


def test_fingerprint_changes_when_the_mapping_changes() -> None:
    cfg = Config()
    before = config_fingerprint(cfg)
    cfg.risk = dataclasses.replace(cfg.risk, target_r_multiples=(1.5, 3.0))
    assert config_fingerprint(cfg) != before


def test_provenance_row_names_the_version_that_ran(tmp_path) -> None:
    record_session_provenance(tmp_path, cfg=Config(),
                              timestamp="2026-09-30T13:40:00Z",
                              completed_sessions=1, git_sha="abc1234")
    text = (tmp_path / "sessions.csv").read_text(encoding="utf-8")
    assert "study_version" in text.splitlines()[0]
    assert "v2" in text
    assert "2/4" in text
    assert "OK" in text


def test_provenance_records_a_mismatch_rather_than_hiding_it(tmp_path) -> None:
    cfg = Config()
    cfg.risk = dataclasses.replace(cfg.risk, target_r_multiples=(1.5, 3.0))
    record_session_provenance(tmp_path, cfg=cfg,
                              timestamp="2026-09-30T13:40:00Z")
    text = (tmp_path / "sessions.csv").read_text(encoding="utf-8")
    assert "unsatisfiable by construction" in text


def test_rules_floor_is_unchanged_by_any_of_this() -> None:
    assert rules.MIN_REWARD_TO_RISK == pytest.approx(2.0)


def test_session_refuses_entries_under_a_non_recorded_config() -> None:
    """The v1 config must not be able to open a position, ever again."""
    import datetime as dt
    import pathlib
    import tempfile

    from stockagent.session import run_session
    from stockagent.study_log import StudyLog
    from test_session import FakeBroker  # the suite's existing stub

    cfg = Config()
    cfg.risk = dataclasses.replace(cfg.risk, target_r_multiples=(1.5, 3.0))

    with tempfile.TemporaryDirectory() as tmp:
        log = StudyLog(pathlib.Path(tmp))
        result = run_session(
            broker=FakeBroker({}), log=log, candidates=[],
            thesis_for=lambda c: None, open_positions=[],
            now=dt.datetime(2026, 9, 30, 14, 0, tzinfo=dt.timezone.utc),
            cfg=cfg)
        rows = log.read("signals")

    assert result.ran is True          # the session still ran and logged
    assert result.errors >= 1          # and recorded the mismatch
    assert any("not the recorded" in (r.get("reason_if_rejected") or "")
               for r in rows)


def test_session_runs_normally_under_the_recorded_config() -> None:
    import datetime as dt
    import pathlib
    import tempfile

    from stockagent.session import run_session
    from stockagent.study_log import StudyLog
    from test_session import FakeBroker

    with tempfile.TemporaryDirectory() as tmp:
        log = StudyLog(pathlib.Path(tmp))
        result = run_session(
            broker=FakeBroker({}), log=log, candidates=[],
            thesis_for=lambda c: None, open_positions=[],
            now=dt.datetime(2026, 9, 30, 14, 0, tzinfo=dt.timezone.utc),
            cfg=Config())
        rows = log.read("signals") if (pathlib.Path(tmp) / "signals.csv").exists() else []

    assert result.ran is True
    assert result.errors == 0
    assert not any("not the recorded" in (r.get("reason_if_rejected") or "")
                   for r in rows)
