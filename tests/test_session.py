"""Session tests against a fake broker.

The point of these is the parts a live dry run would not reliably exercise:
a stop and a target hit on the same bar, a partial exit at Target 1, a broker
failure mid-session, and the guarantee that every candidate leaves a row
behind whatever happens to it.
"""

import datetime as dt
import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).parent.parent / "src" / "stockagent"

pkg = types.ModuleType("sa")
pkg.__path__ = [str(ROOT)]
sys.modules["sa"] = pkg
for name in ("study_log", "study_rules", "session"):
    spec = importlib.util.spec_from_file_location(f"sa.{name}", ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"sa.{name}"] = mod
    spec.loader.exec_module(mod)

study_log = sys.modules["sa.study_log"]
session = sys.modules["sa.session"]
StudyLog = study_log.StudyLog
Candidate, Thesis, OpenPosition = session.Candidate, session.Thesis, session.OpenPosition

NOW = dt.datetime(2026, 9, 22, 14, 31, tzinfo=dt.timezone.utc)


class FakeQuote:
    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask
        self.timestamp = "2026-09-22T14:30:55Z"
        self.age_seconds = 5.0


class FakeFill:
    def __init__(self, symbol, qty, side, price):
        self.order_id = f"ord-{symbol}-{side}"
        self.symbol, self.qty, self.side = symbol, qty, side
        self.filled_price = price
        self.filled_at = "2026-09-22T14:31:00Z"


class FakeAccount:
    equity = 100_000.0
    cash = 100_000.0
    buying_power = 100_000.0


class FakeBroker:
    def __init__(self, quotes, *, is_open=True, fail_on=()):
        self.quotes, self.is_open, self.fail_on = quotes, is_open, set(fail_on)
        self.orders = []

    def account(self):
        return FakeAccount()

    def positions(self):
        return {}

    def market_is_open(self):
        return self.is_open

    def quote(self, symbol):
        if symbol in self.fail_on:
            raise RuntimeError(f"connector outage for {symbol}")
        return self.quotes[symbol]

    def submit(self, symbol, qty, side, *, quote=None):
        price = (quote or self.quotes[symbol]).ask if side == "buy" else \
            (quote or self.quotes[symbol]).bid
        self.orders.append((symbol, qty, side, price))
        return FakeFill(symbol, qty, side, price)


def thesis_ok(**over):
    base = dict(entry_zone=(100.0, 102.0), stop=96.40, target_1=110.20,
                target_2=120.00, predicted_probability=0.55,
                falsification="daily close below 96.40", layer_2_favoured=True,
                rationale="test setup")
    base.update(over)
    return Thesis(**base)


# -- exits -----------------------------------------------------------------

def position(**over):
    base = dict(ticker="AAPL", direction="long", entry_price=101.0, size=200,
                remaining=200, stop=96.40, target_1=110.20, target_2=120.0,
                entry_timestamp="2026-09-01T14:31:00Z", thesis="t",
                invalidation="daily close below 96.40")
    base.update(over)
    return OpenPosition(**base)


def test_stop_wins_when_stop_and_target_both_touched():
    """A bar that hits both must resolve to the stop, not the flattering side."""
    pos = position()
    assert session.evaluate_exit(pos, bid=96.00, now=NOW) == ("stop", 200)


def test_target_1_closes_half():
    assert session.evaluate_exit(position(), bid=111.0, now=NOW) == ("target_1", 100)


def test_target_2_only_after_target_1():
    untouched = position()
    assert session.evaluate_exit(untouched, bid=125.0, now=NOW) == ("target_1", 100)
    after = position(target_1_hit=True, remaining=100)
    assert session.evaluate_exit(after, bid=125.0, now=NOW) == ("target_2", 100)


def test_falsification_exits_in_full_regardless_of_pnl():
    assert session.evaluate_exit(position(), bid=105.0, now=NOW,
                                 falsified=True) == ("falsification", 200)


def test_time_stop_at_45_days():
    old = position(entry_timestamp="2026-07-01T14:31:00Z")
    assert session.evaluate_exit(old, bid=105.0, now=NOW) == ("time_stop", 200)
    young = position(entry_timestamp="2026-09-20T14:31:00Z")
    assert session.evaluate_exit(young, bid=105.0, now=NOW) is None


# -- sessions --------------------------------------------------------------

def test_closed_market_logs_system_error_and_does_nothing(tmp_path):
    log = StudyLog(tmp_path)
    broker = FakeBroker({}, is_open=False)
    result = session.run_session(broker=broker, log=log, candidates=[
        Candidate("AAPL", 1.0)], thesis_for=lambda c: thesis_ok(),
        open_positions=[], now=NOW)
    assert result.errors == 1 and result.entered == 0
    assert log.read("signals")[0]["action_taken"] == "SYSTEM_ERROR"
    assert broker.orders == []


def test_clean_entry_writes_both_files(tmp_path):
    log = StudyLog(tmp_path)
    broker = FakeBroker({"AAPL": FakeQuote(100.9, 101.0)})
    result = session.run_session(broker=broker, log=log,
                                 candidates=[Candidate("AAPL", 1.0, "tech")],
                                 thesis_for=lambda c: thesis_ok(),
                                 open_positions=[], now=NOW)
    assert result.entered == 1
    trade = log.read("trades")[0]
    assert trade["ticker"] == "AAPL" and trade["exit_timestamp"] == ""
    assert "stop 96.40" in trade["thesis_at_entry"]
    assert trade["invalidation_condition"] == "daily close below 96.40"
    # 1% of 100k / (101.00 - 96.40) = 217
    assert trade["size"] == "217"
    assert log.read("signals")[0]["action_taken"] == "ENTERED"


def test_dry_run_logs_decisions_without_submitting_orders(tmp_path):
    """The dispatch flag must make dry runs incapable of changing broker state."""
    log = StudyLog(tmp_path)
    held = [position()]
    broker = FakeBroker({"AAPL": FakeQuote(111.0, 111.1),
                         "MSFT": FakeQuote(100.9, 101.0)})

    result = session.run_session(
        broker=broker, log=log, candidates=[Candidate("MSFT", 1.0, "tech")],
        thesis_for=lambda c: thesis_ok(), open_positions=held, now=NOW,
        dry_run=True)

    assert broker.orders == []
    assert result.skipped == 2
    assert held[0].remaining == 200
    assert log.read("trades") == []
    assert [row["action_taken"] for row in log.read("signals")] == ["SKIPPED", "SKIPPED"]


def test_rejection_is_logged_with_its_reason(tmp_path):
    log = StudyLog(tmp_path)
    broker = FakeBroker({"AAPL": FakeQuote(100.9, 101.0)})
    result = session.run_session(
        broker=broker, log=log, candidates=[Candidate("AAPL", 1.0)],
        thesis_for=lambda c: thesis_ok(predicted_probability=0.20),
        open_positions=[], now=NOW)
    assert result.rejected == 1 and broker.orders == []
    row = log.read("signals")[0]
    assert row["action_taken"] == "REJECTED"
    assert "breakeven+10pp" in row["reason_if_rejected"]


def test_broker_outage_logs_system_error_and_continues(tmp_path):
    log = StudyLog(tmp_path)
    broker = FakeBroker({"AAPL": FakeQuote(100.9, 101.0),
                         "MSFT": FakeQuote(100.9, 101.0)}, fail_on=["AAPL"])
    result = session.run_session(
        broker=broker, log=log,
        candidates=[Candidate("AAPL", 2.0), Candidate("MSFT", 1.0)],
        thesis_for=lambda c: thesis_ok(), open_positions=[], now=NOW)
    assert result.errors == 1 and result.entered == 1
    rows = {r["ticker"]: r for r in log.read("signals")}
    assert rows["AAPL"]["action_taken"] == "SYSTEM_ERROR"
    assert "outage" in rows["AAPL"]["reason_if_rejected"]
    assert rows["MSFT"]["action_taken"] == "ENTERED"


def test_position_cap_skips_the_rest_and_logs_each(tmp_path):
    log = StudyLog(tmp_path)
    held = [position(ticker=f"H{i}") for i in range(10)]
    broker = FakeBroker({f"H{i}": FakeQuote(105.0, 105.1) for i in range(10)}
                        | {"AAPL": FakeQuote(100.9, 101.0)})
    result = session.run_session(broker=broker, log=log,
                                 candidates=[Candidate("AAPL", 1.0)],
                                 thesis_for=lambda c: thesis_ok(),
                                 open_positions=held, now=NOW)
    assert result.entered == 0 and result.skipped == 1
    assert [r for r in log.read("signals")
            if r["ticker"] == "AAPL"][0]["action_taken"] == "SKIPPED"


def test_partial_exit_moves_stop_to_entry_and_halves_position(tmp_path):
    log = StudyLog(tmp_path)
    held = [position()]
    broker = FakeBroker({"AAPL": FakeQuote(111.0, 111.1)})
    result = session.run_session(broker=broker, log=log, candidates=[],
                                 thesis_for=lambda c: None,
                                 open_positions=held, now=NOW)
    assert result.exited == 1
    assert held[0].remaining == 100
    assert held[0].target_1_hit is True
    assert held[0].stop == pytest.approx(101.0)      # Section 5: stop to entry
    row = log.read("trades")[0]
    assert row["exit_reason"] == "target_1" and row["size"] == "100"
    assert float(row["pnl"]) == pytest.approx((111.0 - 101.0) * 100)


def test_every_candidate_leaves_a_row(tmp_path):
    """Section 10's survivorship rule, asserted directly."""
    log = StudyLog(tmp_path)
    broker = FakeBroker({"A": FakeQuote(100.9, 101.0), "B": FakeQuote(100.9, 101.0),
                         "C": FakeQuote(100.9, 101.0)}, fail_on=["C"])
    theses = {"A": thesis_ok(), "B": thesis_ok(predicted_probability=0.2), "C": thesis_ok()}
    session.run_session(broker=broker, log=log,
                        candidates=[Candidate(s, 1.0) for s in "ABC"],
                        thesis_for=lambda c: theses[c.symbol],
                        open_positions=[], now=NOW)
    logged = {r["ticker"] for r in log.read("signals")}
    assert logged == {"A", "B", "C"}
