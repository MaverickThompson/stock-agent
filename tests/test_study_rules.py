"""Tests for Section 5 gates and Section 6 sizing.

The numbers asserted here are quoted from PROTOCOL.md rather than read back
out of the module, so a change to a frozen parameter fails a test instead of
silently redefining the study.
"""

import importlib.util
import math
import pathlib
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "study_rules", pathlib.Path(__file__).parent.parent / "src" / "stockagent" / "study_rules.py")
rules = importlib.util.module_from_spec(_SPEC)
sys.modules["study_rules"] = rules
_SPEC.loader.exec_module(rules)


# -- frozen constants ------------------------------------------------------

def test_section_6_constants_are_what_the_protocol_says():
    assert rules.RISK_PER_TRADE == 0.01
    assert rules.MAX_CONCURRENT_POSITIONS == 10
    assert rules.MAX_TOTAL_OPEN_RISK == 0.10
    assert rules.MAX_SINGLE_POSITION_WEIGHT == 0.25
    assert rules.MAX_SECTOR_WEIGHT == 0.40
    assert rules.MIN_CASH_RESERVE == 0.10


def test_section_5_constants():
    assert rules.MIN_REWARD_TO_RISK == 2.0
    assert rules.PROBABILITY_EDGE_REQUIRED == 0.10
    assert rules.EARNINGS_BLACKOUT_HOURS == 48.0
    assert rules.TIME_STOP_DAYS == 45


# -- arithmetic ------------------------------------------------------------

def test_breakeven_probability():
    assert rules.breakeven_probability(2.0) == pytest.approx(1 / 3)
    assert rules.breakeven_probability(3.0) == pytest.approx(0.25)


def test_reward_to_risk_uses_ask_and_bid():
    # entry at ask 101, stop 96, target1 bid 111 -> risk 5, reward 10 -> 2.0
    assert rules.reward_to_risk(101.0, 96.0, 111.0) == pytest.approx(2.0)


def test_expected_value():
    assert rules.expected_value(0.5, 10.0, 5.0) == pytest.approx(2.5)
    assert rules.expected_value(0.2, 10.0, 5.0) == pytest.approx(-2.0)


def test_sizing_rounds_down():
    # 1% of 100,000 = 1000 risk budget; risk/share 4.60 -> 217.39 -> 217
    assert rules.position_size(100_000.0, 101.00, 96.40) == 217
    assert rules.position_size(100_000.0, 101.00, 96.40) == math.floor(1000 / 4.60)


def test_sizing_never_rounds_up_even_when_close():
    # risk/share exactly 3.0 -> 333.33 -> 333, not 334
    assert rules.position_size(100_000.0, 100.00, 97.00) == 333


def test_sizing_rejects_stop_above_entry():
    with pytest.raises(ValueError, match="not below entry ask"):
        rules.position_size(100_000.0, 100.0, 100.0)


def test_derive_stop_takes_the_tighter_of_the_two():
    # 2x ATR gives 96.0; a swing low at 97.2 is tighter, so 97.2 wins
    assert rules.derive_stop(100.0, 2.0, 97.2) == pytest.approx(97.2)
    # swing low far below -> ATR stop is tighter
    assert rules.derive_stop(100.0, 2.0, 90.0) == pytest.approx(96.0)
    assert rules.derive_stop(100.0, 2.0, None) == pytest.approx(96.0)


@pytest.mark.parametrize("price,expected", [
    (100.00, True), (100.50, True), (99.50, True), (100.005, True),
    (96.40, False), (101.37, False), (100.26, False),
])
def test_round_number_detection(price, expected):
    assert rules.is_round_number(price) is expected


# -- the gate --------------------------------------------------------------

def good_entry(**overrides):
    base = dict(
        entry_ask=101.00, stop=96.40, target_1_bid=111.00, target_2_bid=120.00,
        predicted_probability=0.55, entry_zone=(100.0, 102.0), nlv=100_000.0,
        open_positions=3, open_risk_fraction=0.03, cash_fraction=0.60,
        sector_weight_after=0.15, hours_to_earnings=None,
        layer_1_passed=True, layer_2_favoured=True)
    base.update(overrides)
    return base


def test_a_clean_setup_passes():
    assert rules.check_entry(**good_entry()).passed


@pytest.mark.parametrize("override,fragment", [
    ({"layer_1_passed": False}, "layer 1"),
    ({"layer_2_favoured": False}, "layer 2"),
    ({"stop": 102.0}, "not below entry ask"),
    ({"stop": 96.50}, "round number"),
    ({"entry_ask": 104.0}, "outside recorded entry zone"),
    ({"target_1_bid": 106.0}, "below the 2.0 floor"),
    ({"predicted_probability": 0.30}, "below breakeven+10pp"),
    ({"target_2_bid": 110.0}, "not beyond target 1"),
    ({"hours_to_earnings": 12.0}, "48h blackout"),
    ({"open_positions": 10}, "10-position cap"),
    ({"open_risk_fraction": 0.10}, "ceiling"),
    ({"sector_weight_after": 0.45}, "sector weight"),
    ({"cash_fraction": 0.12}, "cash would fall"),
])
def test_each_gate_rejects_with_a_reason(override, fragment):
    result = rules.check_entry(**good_entry(**override))
    assert not result.passed
    assert fragment in result.reason
    assert result.reason, "every rejection must carry a reason for signals.csv"


def test_probability_floor_is_breakeven_plus_ten_points():
    """Entry 101.00, stop 96.40 -> risk 4.60. Target1 110.20 -> reward 9.20 -> R:R 2.0.

    breakeven = 1/(1+2.0) = 0.3333, so the gate requires p >= 0.4333.
    """
    assert rules.reward_to_risk(101.00, 96.40, 110.20) == pytest.approx(2.0)
    at_floor = good_entry(target_1_bid=110.20, predicted_probability=0.4334)
    assert rules.check_entry(**at_floor).passed
    just_under = good_entry(target_1_bid=110.20, predicted_probability=0.4332)
    assert not rules.check_entry(**just_under).passed
    assert "breakeven+10pp" in rules.check_entry(**just_under).reason


def test_position_weight_cap_blocks_an_oversized_name():
    # tight stop -> huge share count -> weight over 25%
    result = rules.check_entry(**good_entry(stop=100.73, target_1_bid=111.0))
    assert not result.passed
    assert "weight" in result.reason


def test_sized_to_zero_is_rejected_not_silently_skipped():
    result = rules.check_entry(**good_entry(nlv=100.0))
    assert not result.passed
    assert "zero shares" in result.reason
