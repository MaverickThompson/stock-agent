"""Tests for the frozen Section 10 schemas.

A bug in this module corrupts sixty days of evidence and is not discoverable
until the end, so the schemas themselves are asserted literally rather than
derived from the code under test.
"""

import csv
import importlib.util
import pathlib
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "study_log", pathlib.Path(__file__).parent.parent / "src" / "stockagent" / "study_log.py")
study_log = importlib.util.module_from_spec(_SPEC)
sys.modules["study_log"] = study_log
_SPEC.loader.exec_module(study_log)

StudyLog = study_log.StudyLog

# The schemas as written in PROTOCOL.md Section 10, typed out by hand.
PROTOCOL_SIGNALS = ["timestamp", "ticker", "signal_type", "triggered_rule",
                    "action_taken", "reason_if_rejected", "price_at_signal", "notes"]
PROTOCOL_TRADES = ["entry_timestamp", "ticker", "direction", "entry_price", "size",
                   "thesis_at_entry", "invalidation_condition", "exit_timestamp",
                   "exit_price", "pnl", "pnl_pct", "exit_reason", "was_thesis_correct"]

THESIS = "entry 100-101, stop 96.40, T1 109, T2 118, p=0.55, falsified below 96.40 on close"
INVALIDATION = "daily close below 96.40"


def header(path):
    with path.open(newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh))


# -- schema ----------------------------------------------------------------

def test_headers_match_protocol_exactly(tmp_path):
    log = StudyLog(tmp_path)
    assert header(log.signals_path) == PROTOCOL_SIGNALS
    assert header(log.trades_path) == PROTOCOL_TRADES


def test_module_constants_match_protocol():
    assert list(study_log.SIGNALS_COLUMNS) == PROTOCOL_SIGNALS
    assert list(study_log.TRADES_COLUMNS) == PROTOCOL_TRADES


def test_mismatched_existing_header_refuses(tmp_path):
    (tmp_path / "signals.csv").write_text("timestamp,ticker,oops\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen Section 10 schema"):
        StudyLog(tmp_path)


def test_reopening_preserves_rows(tmp_path):
    StudyLog(tmp_path).log_signal(ticker="AAPL", signal_type="entry_candidate",
                                  triggered_rule="layer1", action_taken="SKIPPED")
    again = StudyLog(tmp_path)
    again.log_signal(ticker="MSFT", signal_type="entry_candidate",
                     triggered_rule="layer1", action_taken="SKIPPED")
    assert [r["ticker"] for r in again.read("signals")] == ["AAPL", "MSFT"]


# -- signals ---------------------------------------------------------------

def test_signal_row_contents(tmp_path):
    log = StudyLog(tmp_path)
    log.log_signal(ticker="NVDA", signal_type="entry_candidate",
                   triggered_rule="layer2_debate", action_taken="REJECTED",
                   price_at_signal=182.5, reason_if_rejected="R:R 1.4 below 2.0 floor",
                   notes="devils advocate carried")
    row = log.read("signals")[0]
    assert row["ticker"] == "NVDA"
    assert row["action_taken"] == "REJECTED"
    assert row["reason_if_rejected"] == "R:R 1.4 below 2.0 floor"
    assert row["price_at_signal"] == "182.5"


def test_rejected_without_reason_raises(tmp_path):
    with pytest.raises(ValueError, match="reason_if_rejected"):
        StudyLog(tmp_path).log_signal(ticker="F", signal_type="entry_candidate",
                                      triggered_rule="layer1", action_taken="REJECTED")


def test_unknown_action_raises(tmp_path):
    with pytest.raises(ValueError, match="action_taken must be"):
        StudyLog(tmp_path).log_signal(ticker="F", signal_type="x",
                                      triggered_rule="y", action_taken="MAYBE")


def test_system_error_uses_protocol_value(tmp_path):
    log = StudyLog(tmp_path)
    log.log_system_error(stage="market_data_fetch", detail="alpaca 503 after 3 retries")
    row = log.read("signals")[0]
    assert row["action_taken"] == "SYSTEM_ERROR"
    assert "503" in row["reason_if_rejected"]


def test_newlines_never_reach_the_file(tmp_path):
    log = StudyLog(tmp_path)
    log.log_signal(ticker="T", signal_type="entry_candidate", triggered_rule="layer1",
                   action_taken="SKIPPED", notes="line one\nline two\r\nline three")
    assert log.signals_path.read_text(encoding="utf-8").count("\n") == 2
    assert log.read("signals")[0]["notes"] == "line one line two line three"


# -- trades ----------------------------------------------------------------

def test_open_trade_leaves_exit_fields_empty(tmp_path):
    log = StudyLog(tmp_path)
    log.open_trade(entry_timestamp="2026-09-22T14:31:00Z", ticker="AAPL",
                   direction="long", entry_price=100.0, size=250,
                   thesis_at_entry=THESIS, invalidation_condition=INVALIDATION)
    row = log.read("trades")[0]
    assert row["thesis_at_entry"] == THESIS
    assert row["exit_timestamp"] == "" and row["exit_price"] == ""
    assert row["pnl"] == "" and row["was_thesis_correct"] == ""


@pytest.mark.parametrize("field", ["thesis_at_entry", "invalidation_condition"])
def test_entry_requires_thesis_and_invalidation(tmp_path, field):
    kwargs = dict(entry_timestamp="2026-09-22T14:31:00Z", ticker="AAPL",
                  direction="long", entry_price=100.0, size=10,
                  thesis_at_entry=THESIS, invalidation_condition=INVALIDATION)
    kwargs[field] = "   "
    with pytest.raises(ValueError, match="Section 5"):
        StudyLog(tmp_path).open_trade(**kwargs)


def test_long_pnl_math(tmp_path):
    log = StudyLog(tmp_path)
    result = log.close_trade(entry_timestamp="2026-09-22T14:31:00Z", ticker="AAPL",
                             direction="long", entry_price=100.0, size=200,
                             thesis_at_entry=THESIS, invalidation_condition=INVALIDATION,
                             exit_timestamp="2026-10-01T15:00:00Z", exit_price=109.0,
                             exit_reason="target_1", was_thesis_correct=True)
    assert result["pnl"] == pytest.approx(1800.0)
    assert result["pnl_pct"] == pytest.approx(0.09)
    assert log.read("trades")[0]["was_thesis_correct"] == "true"


def test_short_pnl_sign_is_inverted(tmp_path):
    result = StudyLog(tmp_path).close_trade(
        entry_timestamp="2026-09-22T14:31:00Z", ticker="AAPL", direction="short",
        entry_price=100.0, size=100, thesis_at_entry=THESIS,
        invalidation_condition=INVALIDATION, exit_timestamp="2026-10-01T15:00:00Z",
        exit_price=90.0, exit_reason="target_1")
    assert result["pnl"] == pytest.approx(1000.0)
    assert result["pnl_pct"] == pytest.approx(0.10)


def test_losing_trade_is_negative(tmp_path):
    result = StudyLog(tmp_path).close_trade(
        entry_timestamp="2026-09-22T14:31:00Z", ticker="AAPL", direction="long",
        entry_price=100.0, size=50, thesis_at_entry=THESIS,
        invalidation_condition=INVALIDATION, exit_timestamp="2026-09-30T15:00:00Z",
        exit_price=96.40, exit_reason="stop", was_thesis_correct=False)
    assert result["pnl"] == pytest.approx(-180.0)


def test_target1_partial_then_remainder(tmp_path):
    """Section 5: T1 closes 50%, T2 closes the rest. Three rows, one position."""
    log = StudyLog(tmp_path)
    entry = "2026-09-22T14:31:00Z"
    common = dict(entry_timestamp=entry, ticker="AAPL", direction="long",
                  entry_price=100.0, thesis_at_entry=THESIS,
                  invalidation_condition=INVALIDATION)
    log.open_trade(size=250, **common)
    log.close_trade(size=125, exit_timestamp="2026-10-01T15:00:00Z", exit_price=109.0,
                    exit_reason="target_1", was_thesis_correct=True, **common)
    log.close_trade(size=125, exit_timestamp="2026-10-09T15:00:00Z", exit_price=118.0,
                    exit_reason="target_2", was_thesis_correct=True, **common)

    rows = log.read("trades")
    assert len(rows) == 3
    assert all(r["entry_timestamp"] == entry and r["ticker"] == "AAPL" for r in rows)
    assert [r["exit_reason"] for r in rows] == ["", "target_1", "target_2"]
    assert sum(float(r["pnl"]) for r in rows if r["pnl"]) == pytest.approx(3375.0)


def test_append_only_earlier_rows_are_never_rewritten(tmp_path):
    log = StudyLog(tmp_path)
    common = dict(entry_timestamp="2026-09-22T14:31:00Z", ticker="AAPL",
                  direction="long", entry_price=100.0, thesis_at_entry=THESIS,
                  invalidation_condition=INVALIDATION)
    log.open_trade(size=250, **common)
    first_snapshot = log.trades_path.read_text(encoding="utf-8")
    log.close_trade(size=250, exit_timestamp="2026-10-01T15:00:00Z", exit_price=109.0,
                    exit_reason="target_1", **common)
    assert log.trades_path.read_text(encoding="utf-8").startswith(first_snapshot)


def test_zero_size_entry_raises(tmp_path):
    with pytest.raises(ValueError, match="size must be positive"):
        StudyLog(tmp_path).open_trade(
            entry_timestamp="2026-09-22T14:31:00Z", ticker="AAPL", direction="long",
            entry_price=100.0, size=0, thesis_at_entry=THESIS,
            invalidation_condition=INVALIDATION)


# -- dry run ---------------------------------------------------------------

def test_dry_run_needs_rows(tmp_path):
    log = StudyLog(tmp_path)
    assert log.row_counts() == {"signals": 0, "trades": 0}
    assert not study_log.dry_run_passed(log.row_counts())
    log.log_signal(ticker="AAPL", signal_type="entry_candidate",
                   triggered_rule="layer1", action_taken="SKIPPED")
    assert study_log.dry_run_passed(log.row_counts())
