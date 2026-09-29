"""Section 5 entry gates and Section 6 sizing, frozen for the 60-day study.

Why this is separate from ``portfolio.py``
------------------------------------------
Section 6 declares a DELIBERATE DEVIATION from the operator's live rules: the
study raises total open risk from 4% to 10%, removes the two-per-week cap and
removes the consecutive-loss trigger. ``portfolio.py`` implements the live
rules. Reusing it would silently apply live constraints to a study that
declared different ones in advance -- so the study's limits live here, written
out literally, and the two cannot drift into each other.

Every number below is quoted from PROTOCOL.md. None may be changed during the
window; Section 11 forbids parameter changes motivated by results or deadlines.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Final

#: Section 6, verbatim.
RISK_PER_TRADE: Final[float] = 0.01          # 1.0% of net liquidation value
MAX_CONCURRENT_POSITIONS: Final[int] = 10
MAX_TOTAL_OPEN_RISK: Final[float] = 0.10     # 10.0% of NLV
MAX_SINGLE_POSITION_WEIGHT: Final[float] = 0.25
MAX_SECTOR_WEIGHT: Final[float] = 0.40
MIN_CASH_RESERVE: Final[float] = 0.10

#: Section 5.
MIN_REWARD_TO_RISK: Final[float] = 2.0
PROBABILITY_EDGE_REQUIRED: Final[float] = 0.10   # +10 percentage points
EARNINGS_BLACKOUT_HOURS: Final[float] = 48.0
TIME_STOP_DAYS: Final[int] = 45

#: Section 5 prohibits "round-number stops" without defining the term. This
#: implementation treats a stop within a cent of a whole or half dollar as
#: round. Recorded here, before day 1, so the choice is auditable rather than
#: discovered later.
ROUND_NUMBER_TOLERANCE: Final[float] = 0.01


@dataclasses.dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.passed


def breakeven_probability(reward_to_risk: float) -> float:
    """1 / (1 + R:R) -- Section 5."""
    if reward_to_risk <= 0:
        raise ValueError(f"reward_to_risk must be positive, got {reward_to_risk}")
    return 1.0 / (1.0 + reward_to_risk)


def expected_value(probability: float, reward: float, risk: float) -> float:
    """EV = p x reward - (1 - p) x risk -- Section 5."""
    return probability * reward - (1.0 - probability) * risk


def reward_to_risk(entry_ask: float, stop: float, target_1_bid: float) -> float:
    """Computed with entry at the ASK and exits at the BID, per Section 5."""
    risk = entry_ask - stop
    if risk <= 0:
        raise ValueError(f"stop {stop} is not below entry ask {entry_ask}")
    return (target_1_bid - entry_ask) / risk


def is_round_number(price: float, tolerance: float = ROUND_NUMBER_TOLERANCE) -> bool:
    """True at a whole or half dollar, within ``tolerance``."""
    doubled = price * 2.0
    return abs(doubled - round(doubled)) < tolerance * 2.0


def position_size(nlv: float, entry_ask: float, stop: float) -> int:
    """shares = floor( (0.01 x NLV) / (entry_ask - stop) ).

    Section 6: "Rounding is always DOWN. Rounding up breaches the risk cap by
    construction."
    """
    risk_per_share = entry_ask - stop
    if risk_per_share <= 0:
        raise ValueError(f"stop {stop} is not below entry ask {entry_ask}")
    return int(math.floor((RISK_PER_TRADE * nlv) / risk_per_share))


def derive_stop(entry: float, atr_14: float, structural_level: float | None) -> float:
    """Tighter of 2x ATR(14) and the structural level -- Section 5.

    "Tighter" means closer to entry, i.e. the higher stop for a long.
    """
    atr_stop = entry - 2.0 * atr_14
    if structural_level is None:
        return atr_stop
    return max(atr_stop, structural_level)


def check_entry(*, entry_ask: float, stop: float, target_1_bid: float,
                target_2_bid: float, predicted_probability: float,
                entry_zone: tuple[float, float], nlv: float,
                open_positions: int, open_risk_fraction: float,
                cash_fraction: float, sector_weight_after: float,
                hours_to_earnings: float | None,
                layer_1_passed: bool, layer_2_favoured: bool) -> GateResult:
    """Every Section 5 and Section 6 condition, in the order the protocol lists them.

    Returns the FIRST failure with its reason, which becomes
    ``reason_if_rejected`` in signals.csv. Section 10 requires every rejection
    to be logged with a reason, so no gate may fail silently.
    """
    if not layer_1_passed:
        return GateResult(False, "layer 1 not passed")
    if not layer_2_favoured:
        return GateResult(False, "layer 2 did not conclude in favour")

    if stop >= entry_ask:
        return GateResult(False, f"stop {stop:.2f} is not below entry ask {entry_ask:.2f}")
    if is_round_number(stop):
        return GateResult(False, f"stop {stop:.2f} is a round number (Section 5)")

    low, high = sorted(entry_zone)
    if not (low <= entry_ask <= high):
        return GateResult(
            False, f"ask {entry_ask:.2f} outside recorded entry zone {low:.2f}-{high:.2f}")

    rr = reward_to_risk(entry_ask, stop, target_1_bid)
    if rr < MIN_REWARD_TO_RISK:
        return GateResult(False, f"R:R {rr:.2f} below the {MIN_REWARD_TO_RISK:.1f} floor")

    required = breakeven_probability(rr) + PROBABILITY_EDGE_REQUIRED
    if predicted_probability < required:
        return GateResult(
            False, f"p {predicted_probability:.3f} below breakeven+10pp ({required:.3f})")

    risk = entry_ask - stop
    ev = expected_value(predicted_probability, target_1_bid - entry_ask, risk)
    if ev <= 0:
        return GateResult(False, f"expected value {ev:.4f} is not positive")

    if target_2_bid <= target_1_bid:
        return GateResult(
            False, f"target 2 {target_2_bid:.2f} is not beyond target 1 {target_1_bid:.2f}")

    if hours_to_earnings is not None and hours_to_earnings < EARNINGS_BLACKOUT_HOURS:
        return GateResult(
            False, f"earnings in {hours_to_earnings:.1f}h, inside the 48h blackout")

    # -- Section 6 limits ---------------------------------------------------
    if open_positions >= MAX_CONCURRENT_POSITIONS:
        return GateResult(False,
                          f"already at the {MAX_CONCURRENT_POSITIONS}-position cap")

    shares = position_size(nlv, entry_ask, stop)
    if shares < 1:
        return GateResult(False, "sized position rounds down to zero shares")

    if open_risk_fraction + RISK_PER_TRADE > MAX_TOTAL_OPEN_RISK + 1e-9:
        return GateResult(
            False,
            f"open risk {open_risk_fraction:.1%} + 1.0% exceeds the "
            f"{MAX_TOTAL_OPEN_RISK:.0%} ceiling")

    weight = (shares * entry_ask) / nlv if nlv else float("inf")
    if weight > MAX_SINGLE_POSITION_WEIGHT + 1e-9:
        return GateResult(
            False, f"position weight {weight:.1%} exceeds "
                   f"{MAX_SINGLE_POSITION_WEIGHT:.0%}")

    if sector_weight_after > MAX_SECTOR_WEIGHT + 1e-9:
        return GateResult(
            False, f"sector weight {sector_weight_after:.1%} exceeds "
                   f"{MAX_SECTOR_WEIGHT:.0%}")

    if cash_fraction - weight < MIN_CASH_RESERVE - 1e-9:
        return GateResult(
            False, f"cash would fall to {cash_fraction - weight:.1%}, below the "
                   f"{MIN_CASH_RESERVE:.0%} reserve")

    return GateResult(True, "")
