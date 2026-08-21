"""Is this Sharpe ratio real, or the best of many things you tried?

A backtest reports the performance of the strategy you kept. It says nothing
about the ones you discarded — and if you tried twenty variations and kept the
best, the winner's Sharpe is inflated by selection alone. The more you search,
the higher the best result climbs *even when every strategy is worthless*.

This module implements the correction from Bailey & López de Prado:

``probabilistic_sharpe_ratio``
    P(true Sharpe > benchmark), given the observed Sharpe, sample length, and
    the skew and kurtosis of the returns. Financial returns are neither normal
    nor independent, and both distort the usual standard error — a negatively
    skewed, fat-tailed series makes a given Sharpe *less* trustworthy.

``expected_max_sharpe``
    The Sharpe you should expect from the *best* of N independent worthless
    strategies. This is the bar a real strategy has to clear.

``deflated_sharpe_ratio``
    The two combined: P(true Sharpe > 0) after accounting for the search.

The practical consequence is uncomfortable and worth stating plainly. Testing
20 strategies on 4,000 daily bars, the best worthless one will show an
annualised Sharpe near 0.5 by luck. So a 0.86 backtest Sharpe found after
twenty attempts is not the evidence it appears to be.

References
----------
Bailey & López de Prado (2012), "The Sharpe Ratio Efficient Frontier",
*Journal of Risk* 15(2). López de Prado (2018), *Advances in Financial Machine
Learning*, ch. 8 and 11.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import kurtosis, norm, skew

__all__ = [
    "SharpeAssessment", "probabilistic_sharpe_ratio", "expected_max_sharpe",
    "deflated_sharpe_ratio", "assess_sharpe", "min_track_record_length",
    "null_sharpe_variance",
]

#: Euler-Mascheroni constant, from the expected maximum of N Gaussians.
EULER_MASCHERONI = 0.5772156649015329

TRADING_DAYS = 252


@dataclass(frozen=True)
class SharpeAssessment:
    """The verdict on an observed Sharpe ratio."""

    sharpe_annual: float
    n_observations: int
    n_trials: int
    skew: float
    kurtosis: float
    #: Annualised Sharpe expected from the best of ``n_trials`` worthless strategies.
    threshold_annual: float
    #: P(true Sharpe > 0), ignoring the search. Optimistic.
    psr: float
    #: P(true Sharpe > luck threshold). Beats noise, but says nothing about
    #: whether it beats the thing you would otherwise have held.
    dsr: float
    #: Observations needed for the result to become significant at 95%.
    min_track_record: float
    #: Annualised Sharpe of the alternative -- normally buy-and-hold.
    benchmark_annual: float = 0.0
    #: P(true Sharpe > the harder of {luck threshold, benchmark}). The number a
    #: trading decision actually turns on.
    dsr_vs_benchmark: float = 0.0

    @property
    def effective_bar_annual(self) -> float:
        """The bar that actually has to be cleared: luck *and* the benchmark."""
        return max(self.threshold_annual, self.benchmark_annual)

    @property
    def is_significant(self) -> bool:
        """Survives the multiple-testing correction (vs noise, not vs holding)."""
        return self.dsr >= 0.95

    @property
    def beats_benchmark(self) -> bool:
        """The one that decides whether to trade it."""
        return self.dsr_vs_benchmark >= 0.95

    def verdict(self) -> str:
        if self.sharpe_annual <= self.threshold_annual:
            return (f"NOT SIGNIFICANT — Sharpe {self.sharpe_annual:.2f} is at or below "
                    f"{self.threshold_annual:.2f}, which is what the best of "
                    f"{self.n_trials} worthless strategies would show by luck alone.")
        if self.benchmark_annual > 0 and not self.beats_benchmark:
            return (
                f"REAL BUT NOT USEFUL — the signal beats noise (DSR {self.dsr:.1%}), "
                f"but P(better than the {self.benchmark_annual:.2f}-Sharpe benchmark) "
                f"is only {self.dsr_vs_benchmark:.1%}. Having an edge over cash is not "
                "the same as beating what you would have held instead."
            )
        if self.is_significant:
            return (f"SIGNIFICANT — DSR {self.dsr:.1%} after correcting for "
                    f"{self.n_trials} trials"
                    + (f", and {self.dsr_vs_benchmark:.1%} against the benchmark."
                       if self.benchmark_annual > 0 else "."))
        return (f"INCONCLUSIVE — DSR {self.dsr:.1%} is below the 95% bar. "
                f"Needs about {self.min_track_record:,.0f} observations "
                f"({self.min_track_record / TRADING_DAYS:.1f} years) to settle.")

    def summary(self) -> str:
        lines = [
            f"  Observed Sharpe (annual) : {self.sharpe_annual:>8.2f}",
            f"  Trials searched          : {self.n_trials:>8d}",
            f"  Luck threshold (annual)  : {self.threshold_annual:>8.2f}",
            f"  Return skew              : {self.skew:>8.2f}",
            f"  Return kurtosis          : {self.kurtosis:>8.2f}",
            f"  PSR  P(true SR > 0)      : {self.psr:>8.1%}",
            f"  DSR  P(true SR > luck)   : {self.dsr:>8.1%}",
        ]
        if self.benchmark_annual > 0:
            lines += [
                f"  Benchmark Sharpe         : {self.benchmark_annual:>8.2f}",
                f"  P(better than benchmark) : {self.dsr_vs_benchmark:>8.1%}",
            ]
        return "\n".join(lines + ["", f"  {self.verdict()}"])


def _per_observation(sharpe_annual: float, periods_per_year: int) -> float:
    """Annualised Sharpe -> per-observation Sharpe."""
    return sharpe_annual / math.sqrt(periods_per_year)


def probabilistic_sharpe_ratio(sharpe: float, n_observations: int,
                               *, skewness: float = 0.0, kurt: float = 3.0,
                               benchmark: float = 0.0) -> float:
    """P(true Sharpe > ``benchmark``). All Sharpes are **per-observation**.

    ``kurt`` is the raw (non-excess) kurtosis, 3.0 for a normal distribution.
    """
    if n_observations < 2:
        return 0.5
    # The standard error of a Sharpe estimate grows with negative skew and with
    # fat tails, which is exactly the shape real return series have.
    variance = 1.0 - skewness * sharpe + 0.25 * (kurt - 1.0) * sharpe**2
    if variance <= 0:
        return 0.5
    z = (sharpe - benchmark) * math.sqrt(n_observations - 1) / math.sqrt(variance)
    return float(norm.cdf(z))


def null_sharpe_variance(n_observations: int) -> float:
    """Sampling variance of a per-observation Sharpe estimate under the null.

    A Sharpe estimated from ``T`` observations of pure noise has a standard
    error of roughly ``1/sqrt(T)``, so its variance is ``1/T``. This is the
    right scale for :func:`expected_max_sharpe` when you did not record the
    actual spread of Sharpes across your trials -- and getting it wrong by
    using 1.0 puts the luck threshold at an impossible ~30 annualised.
    """
    return 1.0 / max(int(n_observations), 2)


def expected_max_sharpe(n_trials: int, *, sharpe_variance: float = 1.0) -> float:
    """Per-observation Sharpe expected from the best of ``n_trials`` null strategies.

    ``sharpe_variance`` is the variance of the Sharpes across the trials you
    ran. When you do not have the trial Sharpes, pass
    ``null_sharpe_variance(n_observations)`` -- which is what
    :func:`assess_sharpe` and :func:`deflated_sharpe_ratio` do by default.
    """
    if n_trials < 2:
        return 0.0
    e = math.e
    quantile_1 = norm.ppf(1.0 - 1.0 / n_trials)
    quantile_2 = norm.ppf(1.0 - 1.0 / (n_trials * e))
    return float(
        math.sqrt(sharpe_variance)
        * ((1.0 - EULER_MASCHERONI) * quantile_1 + EULER_MASCHERONI * quantile_2)
    )


def deflated_sharpe_ratio(sharpe: float, n_observations: int, n_trials: int,
                          *, skewness: float = 0.0, kurt: float = 3.0,
                          sharpe_variance: float | None = None) -> float:
    """P(true Sharpe > 0) after correcting for having searched ``n_trials``.

    ``sharpe_variance`` defaults to the null sampling variance ``1/T``.
    """
    if sharpe_variance is None:
        sharpe_variance = null_sharpe_variance(n_observations)
    threshold = expected_max_sharpe(n_trials, sharpe_variance=sharpe_variance)
    return probabilistic_sharpe_ratio(
        sharpe, n_observations, skewness=skewness, kurt=kurt, benchmark=threshold)


def min_track_record_length(sharpe: float, *, benchmark: float = 0.0,
                            skewness: float = 0.0, kurt: float = 3.0,
                            confidence: float = 0.95) -> float:
    """Observations needed before the Sharpe clears ``benchmark`` at ``confidence``.

    Returns ``inf`` when the observed Sharpe is at or below the benchmark — no
    amount of additional data rescues a result that is not ahead to begin with.
    """
    if sharpe <= benchmark:
        return float("inf")
    variance = 1.0 - skewness * sharpe + 0.25 * (kurt - 1.0) * sharpe**2
    if variance <= 0:
        return float("inf")
    z = norm.ppf(confidence)
    return float(1.0 + variance * (z / (sharpe - benchmark)) ** 2)


def assess_sharpe(returns, n_trials: int = 1, *,
                  periods_per_year: int = TRADING_DAYS,
                  sharpe_variance: float | None = None,
                  benchmark_sharpe_annual: float = 0.0) -> SharpeAssessment:
    """Full assessment of a return series.

    Parameters
    ----------
    returns:
        Per-period simple returns.
    n_trials:
        How many strategy variations were tried before keeping this one. Be
        honest: every parameter sweep, every state count, every feature set you
        evaluated counts. Under-reporting this is how people fool themselves.
    benchmark_sharpe_annual:
        Annualised Sharpe of the alternative you would otherwise hold. Supply
        it. "Better than noise" and "better than an index fund" are different
        questions, and only the second one should make you trade: a strategy
        can clear the luck threshold comfortably and still be worse than doing
        nothing, which is exactly what this project's HMM turned out to be.
    """
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    if n < 3:
        raise ValueError(f"need at least 3 return observations, got {n}")

    std = float(values.std(ddof=1))
    # Not `<= 0`: a constant series leaves float noise around 1e-18 rather than
    # an exact zero, which would slip through and produce a nonsense Sharpe.
    if not math.isfinite(std) or std < 1e-12:
        raise ValueError("returns have zero variance; Sharpe is undefined")

    sr_period = float(values.mean()) / std
    sr_annual = sr_period * math.sqrt(periods_per_year)
    series_skew = float(skew(values))
    series_kurt = float(kurtosis(values, fisher=False))  # raw, 3.0 = normal

    if sharpe_variance is None:
        sharpe_variance = null_sharpe_variance(n)
    threshold = expected_max_sharpe(n_trials, sharpe_variance=sharpe_variance)
    psr = probabilistic_sharpe_ratio(sr_period, n, skewness=series_skew,
                                     kurt=series_kurt, benchmark=0.0)
    dsr = probabilistic_sharpe_ratio(sr_period, n, skewness=series_skew,
                                     kurt=series_kurt, benchmark=threshold)
    mtrl = min_track_record_length(sr_period, benchmark=threshold,
                                   skewness=series_skew, kurt=series_kurt)

    # The real bar is whichever is harder: not being fooled by the search, and
    # not being worse than what you would have held anyway.
    bench_period = _per_observation(benchmark_sharpe_annual, periods_per_year)
    dsr_bench = probabilistic_sharpe_ratio(
        sr_period, n, skewness=series_skew, kurt=series_kurt,
        benchmark=max(threshold, bench_period))

    return SharpeAssessment(
        sharpe_annual=sr_annual, n_observations=n, n_trials=n_trials,
        skew=series_skew, kurtosis=series_kurt,
        threshold_annual=threshold * math.sqrt(periods_per_year),
        psr=psr, dsr=dsr, min_track_record=mtrl,
        benchmark_annual=benchmark_sharpe_annual,
        dsr_vs_benchmark=dsr_bench,
    )
