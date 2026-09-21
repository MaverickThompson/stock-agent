"""Earnings gate and the adapter mappings.

The point of these is the failure direction: an earnings calendar that cannot
be consulted must never read as "clear". That is the single mapping in the
adapter most capable of putting a protocol violation into the trade log.
"""

import datetime as dt
import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).parent.parent / "src" / "stockagent"
pkg = types.ModuleType("sa2")
pkg.__path__ = [str(ROOT)]
sys.modules["sa2"] = pkg
for name in ("study_log", "study_rules", "session", "earnings"):
    spec = importlib.util.spec_from_file_location(f"sa2.{name}", ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"sa2.{name}"] = mod
    spec.loader.exec_module(mod)

earnings = sys.modules["sa2.earnings"]
session = sys.modules["sa2.session"]
NOW = dt.datetime(2026, 9, 22, 14, 0, tzinfo=dt.timezone.utc)

CSV = """symbol,name,reportDate,fiscalDateEnding,estimate,currency
AAPL,Apple Inc,2026-09-23,2026-09-30,1.52,USD
MSFT,Microsoft,2026-11-15,2026-09-30,3.10,USD
DUPE,Dupe Co,2026-12-01,2026-09-30,1.00,USD
DUPE,Dupe Co,2026-10-01,2026-09-30,1.00,USD
"""


# -- the calendar ----------------------------------------------------------

def test_parses_and_keeps_the_soonest_date():
    cal = earnings.EarningsCalendar.from_csv(CSV)
    assert cal.available
    # DUPE listed twice; the nearer date must win
    assert 0 < cal.hours_until("DUPE", now=NOW) < 24 * 10


def test_symbol_inside_the_blackout():
    cal = earnings.EarningsCalendar.from_csv(CSV)
    hours = cal.hours_until("AAPL", now=NOW)
    assert hours < 48.0, "AAPL reports tomorrow, so it is inside the 48h window"


def test_symbol_clear_of_the_blackout():
    cal = earnings.EarningsCalendar.from_csv(CSV)
    assert cal.hours_until("MSFT", now=NOW) > 48.0


def test_symbol_not_in_the_calendar_is_clear_not_unknown():
    cal = earnings.EarningsCalendar.from_csv(CSV)
    result = cal.hours_until("NOTLISTED", now=NOW)
    assert result is None
    assert not earnings.is_unknown(result)


def test_unavailable_calendar_returns_unknown_not_none():
    """The distinction the whole gate depends on."""
    cal = earnings.EarningsCalendar.unavailable("no api key")
    result = cal.hours_until("AAPL", now=NOW)
    assert earnings.is_unknown(result)
    assert result is not None, "UNKNOWN must not be confused with 'nothing scheduled'"


def test_empty_csv_is_unavailable_not_empty():
    cal = earnings.EarningsCalendar.from_csv("symbol,name,reportDate\n")
    assert not cal.available
    assert earnings.is_unknown(cal.hours_until("AAPL", now=NOW))


def test_load_without_key_and_without_cache_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    cal = earnings.EarningsCalendar.load(tmp_path)
    assert not cal.available


def test_load_uses_a_fresh_cache_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    (tmp_path / "earnings_calendar.csv").write_text(CSV, encoding="utf-8")
    cal = earnings.EarningsCalendar.load(tmp_path)
    assert cal.available
    assert cal.hours_until("MSFT", now=NOW) > 48.0


# -- the gate consumes it correctly ----------------------------------------

def test_unknown_earnings_rejects_the_entry():
    """An unverifiable calendar must fail the gate, never pass it."""
    rules = sys.modules["sa2.study_rules"]
    base = dict(entry_ask=101.00, stop=96.40, target_1_bid=110.20,
                target_2_bid=120.00, predicted_probability=0.55,
                entry_zone=(100.0, 102.0), nlv=100_000.0, open_positions=3,
                open_risk_fraction=0.03, cash_fraction=0.60,
                sector_weight_after=0.15, layer_1_passed=True,
                layer_2_favoured=True)
    # The adapter converts UNKNOWN to 0.0 hours, which the gate must reject.
    assert not rules.check_entry(**base, hours_to_earnings=0.0).passed
    assert "blackout" in rules.check_entry(**base, hours_to_earnings=0.0).reason
    # And a genuine None (checked, nothing scheduled) passes.
    assert rules.check_entry(**base, hours_to_earnings=None).passed


# -- adapter mappings ------------------------------------------------------

def _adapter():
    spec = importlib.util.spec_from_file_location(
        "sa2.study_adapter", ROOT / "study_adapter.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sa2.study_adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_entry_zone_scales_with_risk():
    a = _adapter()
    low, high = a.entry_zone_for(101.00, 96.40)      # risk 4.60, band 0.46
    assert low == pytest.approx(100.54)
    assert high == pytest.approx(101.46)
    # A wider-risk trade gets a proportionally wider zone
    low2, high2 = a.entry_zone_for(101.00, 91.00)    # risk 10.00, band 1.00
    assert high2 - low2 == pytest.approx(2.0)


def test_entry_zone_contains_the_proposed_entry():
    a = _adapter()
    low, high = a.entry_zone_for(250.0, 240.0)
    assert low < 250.0 < high


def test_falsification_prefers_manager_conditions():
    a = _adapter()
    assert a.falsification_for(["volume dries up", "regime flips"], 96.4) == \
        "volume dries up; regime flips"


def test_falsification_falls_back_to_the_stop():
    a = _adapter()
    assert a.falsification_for(None, 96.4) == "daily close below 96.40"
    assert a.falsification_for([], 96.4) == "daily close below 96.40"
    assert a.falsification_for(["  "], 96.4) == "daily close below 96.40"
