"""Tests for the multiple-testing correction.

The property that matters: a strategy found by searching many variations must
be held to a higher bar than one tested once. These tests assert that the bar
actually rises with the number of trials, and that pure noise fails it.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from stockagent.significance import (assess_sharpe, deflated_sharpe_ratio,
                                     expected_max_sharpe,
                                     min_track_record_length,
                                     null_sharpe_variance,
                                     probabilistic_sharpe_ratio)

TRADING_DAYS = 252


def _returns(sharpe_annual: float, n: int = 2000, seed: int = 0,
             vol_annual: float = 0.15) -> np.ndarray:
    """Daily returns whose *realised* annualised Sharpe is exactly the target.

    Drawing from a normal with the right parameters gives a sample Sharpe that
    wanders — asking for 0.91 and getting 1.09 is ordinary sampling noise, and
    it makes threshold assertions flaky for reasons unrelated to the code under
    test. Standardising the draw first pins the realised value exactly.
    """
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, n)
    noise = (noise - noise.mean()) / noise.std(ddof=1)   # exact 0 mean, unit sd
    daily_vol = vol_annual / math.sqrt(TRADING_DAYS)
    return daily_vol * (noise + sharpe_annual / math.sqrt(TRADING_DAYS))


# ------------------------------------------------------- expected max Sharpe


def test_luck_threshold_rises_with_trials() -> None:
    """Search harder, and the bar for 'real' must get higher."""
    thresholds = [expected_max_sharpe(n) for n in (2, 10, 50, 500)]
    assert thresholds == sorted(thresholds)
    assert thresholds[0] > 0


def test_single_trial_needs_no_correction() -> None:
    assert expected_max_sharpe(1) == 0.0


def test_twenty_trials_produce_a_meaningful_bar() -> None:
    """The headline warning: 20 tries buys you a real-looking Sharpe for free.

    Scaled to the null sampling variance over ~8 years of daily bars.
    """
    n_obs = 2000
    annual = (expected_max_sharpe(20, sharpe_variance=null_sharpe_variance(n_obs))
              * math.sqrt(TRADING_DAYS))
    assert 0.3 < annual < 1.5, (
        f"expected the best of 20 worthless strategies to show ~0.7 annual "
        f"Sharpe, got {annual:.2f}")


def test_null_variance_scales_with_sample_length() -> None:
    """A longer record makes luck less able to fake a high Sharpe."""
    assert null_sharpe_variance(500) > null_sharpe_variance(5000)
    short = expected_max_sharpe(20, sharpe_variance=null_sharpe_variance(500))
    long = expected_max_sharpe(20, sharpe_variance=null_sharpe_variance(5000))
    assert short > long


# --------------------------------------------------------------------- PSR


def test_psr_rises_with_sharpe() -> None:
    low = probabilistic_sharpe_ratio(0.02, 1000)
    high = probabilistic_sharpe_ratio(0.10, 1000)
    assert 0.0 <= low < high <= 1.0


def test_psr_rises_with_sample_size() -> None:
    """The same Sharpe is more believable over a longer record."""
    short = probabilistic_sharpe_ratio(0.05, 100)
    long = probabilistic_sharpe_ratio(0.05, 5000)
    assert short < long


def test_negative_skew_reduces_confidence() -> None:
    """Crash-prone returns make a given Sharpe less trustworthy, not more."""
    symmetric = probabilistic_sharpe_ratio(0.05, 1000, skewness=0.0, kurt=3.0)
    skewed = probabilistic_sharpe_ratio(0.05, 1000, skewness=-1.5, kurt=3.0)
    assert skewed < symmetric


def test_fat_tails_reduce_confidence() -> None:
    normal = probabilistic_sharpe_ratio(0.05, 1000, kurt=3.0)
    fat = probabilistic_sharpe_ratio(0.05, 1000, kurt=12.0)
    assert fat < normal


def test_psr_of_zero_sharpe_is_a_coin_flip() -> None:
    assert probabilistic_sharpe_ratio(0.0, 1000) == pytest.approx(0.5, abs=1e-9)


# --------------------------------------------------------------------- DSR


def test_deflation_lowers_confidence() -> None:
    """Same result, more searching, less belief. The whole point."""
    once = deflated_sharpe_ratio(0.06, 2000, n_trials=1)
    searched = deflated_sharpe_ratio(0.06, 2000, n_trials=100)
    assert searched < once


def test_pure_noise_fails_after_searching() -> None:
    """The best of many random strategies must not read as significant."""
    rng = np.random.default_rng(7)
    best, series = -np.inf, None
    for _ in range(50):
        r = rng.normal(0.0, 0.01, 1500)          # zero true edge
        sr = r.mean() / r.std(ddof=1)
        if sr > best:
            best, series = sr, r
    assessment = assess_sharpe(series, n_trials=50)
    assert not assessment.is_significant, (
        f"noise passed with DSR {assessment.dsr:.1%} — the correction is not working")


def test_a_genuinely_strong_strategy_survives() -> None:
    """The correction must not reject everything; a real edge should pass."""
    assessment = assess_sharpe(_returns(1.6, n=3000, seed=3), n_trials=20)
    assert assessment.is_significant
    assert assessment.sharpe_annual > assessment.threshold_annual


# ------------------------------------------------------------- assessment


def test_assessment_reports_the_observed_sharpe() -> None:
    assessment = assess_sharpe(_returns(1.0, n=4000, seed=1), n_trials=1)
    assert assessment.sharpe_annual == pytest.approx(1.0, abs=0.25)


def test_verdict_names_the_failure_mode() -> None:
    weak = assess_sharpe(_returns(0.2, n=2000, seed=5), n_trials=100)
    assert not weak.is_significant
    assert "NOT SIGNIFICANT" in weak.verdict() or "INCONCLUSIVE" in weak.verdict()


def test_rejects_degenerate_input() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        assess_sharpe([0.01, 0.02])
    with pytest.raises(ValueError, match="zero variance"):
        assess_sharpe([0.01] * 100)


def test_min_track_record_is_infinite_below_the_bar() -> None:
    """No amount of extra data rescues a result that is behind to begin with."""
    assert min_track_record_length(0.01, benchmark=0.05) == float("inf")


def test_min_track_record_shrinks_as_edge_grows() -> None:
    modest = min_track_record_length(0.03)
    strong = min_track_record_length(0.12)
    assert strong < modest


def test_beating_noise_is_not_beating_the_benchmark() -> None:
    """The distinction the SPY run exposed.

    A strategy can clear the luck threshold comfortably and still be worse than
    the index it was trying to beat. Only the second question should make you
    trade.
    """
    returns = _returns(0.91, n=4008, seed=11)
    assessment = assess_sharpe(returns, n_trials=20, benchmark_sharpe_annual=0.92)
    assert assessment.is_significant, "should clear the luck bar"
    assert not assessment.beats_benchmark, "must not claim it beats buy-and-hold"
    assert assessment.dsr_vs_benchmark < 0.6
    assert "REAL BUT NOT USEFUL" in assessment.verdict()


def test_a_strategy_that_genuinely_beats_the_benchmark_says_so() -> None:
    assessment = assess_sharpe(_returns(1.8, n=4000, seed=12),
                               n_trials=20, benchmark_sharpe_annual=0.90)
    assert assessment.beats_benchmark
    assert "SIGNIFICANT" in assessment.verdict()


def test_effective_bar_is_the_harder_of_the_two() -> None:
    high_bench = assess_sharpe(_returns(1.0, n=3000, seed=13),
                               n_trials=2, benchmark_sharpe_annual=1.5)
    assert high_bench.effective_bar_annual == pytest.approx(1.5, abs=1e-9)
    high_luck = assess_sharpe(_returns(1.0, n=3000, seed=13),
                              n_trials=500, benchmark_sharpe_annual=0.05)
    assert high_luck.effective_bar_annual == high_luck.threshold_annual


def test_benchmark_defaults_to_off() -> None:
    """Omitting the benchmark must not silently invent one."""
    assessment = assess_sharpe(_returns(1.0, n=2000, seed=14), n_trials=5)
    assert assessment.benchmark_annual == 0.0
    assert "REAL BUT NOT USEFUL" not in assessment.verdict()


def test_our_spy_result_does_not_survive_deflation() -> None:
    """The project's own headline number, checked honestly.

    The walk-forward SPY strategy showed an annual Sharpe of 0.86 over ~4,010
    bars. We tried well more than one variation getting there.
    """
    sharpe_per_bar = 0.86 / math.sqrt(TRADING_DAYS)
    dsr = deflated_sharpe_ratio(sharpe_per_bar, 4010, n_trials=20,
                                skewness=-0.5, kurt=12.0)
    assert dsr < 0.95, (
        f"expected the SPY backtest to fail deflation, got DSR {dsr:.1%}")
