"""Walk-forward evaluation of the regime signal.

The only backtest worth running is one that could not have known the future, so
three rules are enforced structurally rather than by convention:

**Refit on trailing data only.** At each refit point ``t0`` the scaler, the
Baum-Welch fit and the regime map are all built from bars ``[0, t0)``. The model
is then frozen and used for the next ``refit_every`` bars. Nothing about bar
``t0 + 5`` influences the model that trades it.

**Filter, never smooth.** Exposure comes from
:meth:`~stockagent.regime.hmm_model.RegimeHMM.filter` -- the forward algorithm's
``P(q_t | o_1..o_t)``. Using ``predict_proba`` (forward-backward) here would let
the model see the whole series, and produces a beautiful equity curve that
cannot be traded. The forward recursion is causal, so filtering a prefix once
per block gives exactly the values a live system would have had, bar by bar.

**Execute after the signal.** A signal computed from bar ``t``'s close is acted
on at bar ``t+1``. ``execution_lag`` cannot be set below 1.

The output is compared against buy-and-hold, which is the benchmark that matters:
a strategy that underperforms an index fund while adding complexity, taxes and
screen time has a negative expected value even when its Sharpe looks fine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .config import Config
from .features import FeatureScaler, build_features
from .logging_setup import get_logger
from .regime import RegimeHMM, build_regime_map
from .significance import SharpeAssessment, assess_sharpe

LOG = get_logger("backtest")

TRADING_DAYS = 252


@dataclass
class BacktestResult:
    """Equity curves, per-bar detail, and summary statistics."""

    equity: pd.Series
    benchmark: pd.Series
    exposure: pd.Series
    returns: pd.Series
    benchmark_returns: pd.Series
    regimes: pd.Series
    stats: dict[str, Any] = field(default_factory=dict)
    refits: list[dict[str, Any]] = field(default_factory=list)
    #: Multiple-testing assessment of the strategy's Sharpe ratio.
    assessment: SharpeAssessment | None = None

    def summary(self) -> str:
        s = self.stats
        rows = [
            ("Period", f"{s['start']} -> {s['end']} ({s['bars']} bars, {s['years']:.1f}y)"),
            ("", ""),
            ("Strategy CAGR", f"{s['cagr']:>8.2%}"),
            ("Benchmark CAGR", f"{s['benchmark_cagr']:>8.2%}"),
            ("Excess CAGR", f"{s['excess_cagr']:>+8.2%}"),
            ("", ""),
            ("Strategy vol", f"{s['volatility']:>8.2%}"),
            ("Benchmark vol", f"{s['benchmark_volatility']:>8.2%}"),
            ("Strategy Sharpe", f"{s['sharpe']:>8.2f}"),
            ("Benchmark Sharpe", f"{s['benchmark_sharpe']:>8.2f}"),
            ("", ""),
            ("Max drawdown", f"{s['max_drawdown']:>8.2%}"),
            ("Benchmark max DD", f"{s['benchmark_max_drawdown']:>8.2%}"),
            ("Calmar", f"{s['calmar']:>8.2f}"),
            ("", ""),
            ("Avg exposure", f"{s['avg_exposure']:>8.1%}"),
            ("Time in market", f"{s['time_in_market']:>8.1%}"),
            ("Turnover (ann.)", f"{s['annual_turnover']:>8.2f}x"),
            ("Cost drag (ann.)", f"{s['annual_cost_drag']:>8.2%}"),
            ("Refits", f"{s['n_refits']:>8d}"),
        ]
        width = max(len(k) for k, _ in rows)
        lines = [f"{k.ljust(width)}  {v}" if k else "" for k, v in rows]
        if self.assessment is not None:
            lines += ["", "Is the Sharpe real, or the best of many tries?", "",
                      self.assessment.summary()]
        return "\n".join(lines)


def _max_drawdown(equity: pd.Series) -> float:
    return float((equity / equity.cummax() - 1.0).min())


def _annualised(returns: pd.Series) -> tuple[float, float, float]:
    """(CAGR, annualised volatility, Sharpe) from a per-bar return series."""
    returns = returns.dropna()
    if returns.empty:
        return 0.0, 0.0, 0.0
    total = float((1.0 + returns).prod())
    years = len(returns) / TRADING_DAYS
    cagr = total ** (1.0 / years) - 1.0 if years > 0 and total > 0 else -1.0
    vol = float(returns.std(ddof=0)) * np.sqrt(TRADING_DAYS)
    # Excess-of-zero Sharpe. With a non-zero cash rate the strategy's large
    # flat periods would look better, not worse, so this is the conservative
    # comparison against a fully-invested benchmark.
    sharpe = (float(returns.mean()) * TRADING_DAYS) / vol if vol > 0 else 0.0
    return cagr, vol, sharpe


def run_backtest(prices: pd.DataFrame, cfg: Config | None = None,
                 *, symbol: str = "?") -> BacktestResult:
    """Walk-forward backtest of the regime-driven exposure signal."""
    cfg = cfg or Config()
    bt = cfg.backtest

    features = build_features(prices, cfg.features)
    if len(features) <= bt.train_window + bt.refit_every:
        raise ValueError(
            f"{symbol}: {len(features)} feature rows is not enough for a "
            f"{bt.train_window}-bar training window plus a {bt.refit_every}-bar "
            "out-of-sample block. Fetch more history or shorten the window."
        )

    values = features.to_numpy(dtype=float)
    index = features.index
    n = len(features)

    exposure = pd.Series(np.nan, index=index, dtype=float, name="exposure")
    regimes = pd.Series("", index=index, dtype=object, name="regime")
    refits: list[dict[str, Any]] = []

    for t0 in range(bt.train_window, n, bt.refit_every):
        block_end = min(t0 + bt.refit_every, n)

        # Everything below is fitted on [0, t0) only.
        scaler = FeatureScaler(clip_sigma=cfg.features.clip_sigma)
        scaler.fit(values[:t0], list(features.columns))
        X = scaler.transform(values[:block_end])

        try:
            model = RegimeHMM(cfg.hmm).fit(X[:t0], list(features.columns))
            regime_map = build_regime_map(model, features["ret"].iloc[:t0], X[:t0])
        except (ValueError, RuntimeError) as exc:
            # A failed refit must not silently inherit the previous model's
            # view. Standing flat is the honest response to "no model".
            LOG.error("%s: refit at %s failed (%s); exposure 0 for this block",
                      symbol, index[t0].date(), exc)
            exposure.iloc[t0:block_end] = 0.0
            regimes.iloc[t0:block_end] = "NoModel"
            continue

        # The forward recursion is causal, so row t of the filtered posterior
        # over the prefix equals what a live system would have computed on
        # bar t. One pass per block instead of one per bar.
        filtered = model.filter(X)
        labels = [lab.value for lab in regime_map.labels()]
        for t in range(t0, block_end):
            row = filtered[t]
            exposure.iloc[t] = regime_map.expected_exposure(row)
            regimes.iloc[t] = labels[int(np.argmax(row))]

        refits.append({
            "date": str(index[t0].date()),
            "train_bars": t0,
            "log_likelihood": round(model.fit_report.log_likelihood, 2),
            "converged": model.fit_report.converged,
            "restart_spread": round(model.fit_report.restart_spread, 2),
            "labels": labels,
            "warnings": regime_map.warnings(),
        })
        LOG.info("%s refit %s: ll=%.1f labels=%s", symbol, index[t0].date(),
                 model.fit_report.log_likelihood, labels)

    # --- Execution -------------------------------------------------------
    bar_returns = np.expm1(features["ret"])          # log -> simple returns
    # Signal from bar t is acted on at bar t+lag. Shifting the *exposure* is
    # what enforces the lag; the returns are left untouched.
    position = exposure.shift(bt.execution_lag).fillna(0.0)

    cost_rate = bt.cost_bps / 10_000.0
    turnover = position.diff().abs().fillna(position.abs())
    costs = turnover * cost_rate

    strategy_returns = (position * bar_returns - costs).rename("strategy")
    benchmark_returns = bar_returns.rename("benchmark")

    tested = position.notna() & (exposure.notna().cumsum() > 0)
    first = int(np.argmax(tested.to_numpy())) if tested.any() else 0
    strategy_returns = strategy_returns.iloc[first:]
    benchmark_returns = benchmark_returns.iloc[first:]
    position = position.iloc[first:]
    costs = costs.iloc[first:]

    equity = (1.0 + strategy_returns).cumprod().rename("equity")
    benchmark = (1.0 + benchmark_returns).cumprod().rename("benchmark")

    cagr, vol, sharpe = _annualised(strategy_returns)
    b_cagr, b_vol, b_sharpe = _annualised(benchmark_returns)
    years = max(len(strategy_returns) / TRADING_DAYS, 1e-9)

    stats = {
        "symbol": symbol,
        "start": str(strategy_returns.index[0].date()),
        "end": str(strategy_returns.index[-1].date()),
        "bars": len(strategy_returns),
        "years": years,
        "cagr": cagr, "volatility": vol, "sharpe": sharpe,
        "benchmark_cagr": b_cagr, "benchmark_volatility": b_vol,
        "benchmark_sharpe": b_sharpe,
        "excess_cagr": cagr - b_cagr,
        "max_drawdown": _max_drawdown(equity),
        "benchmark_max_drawdown": _max_drawdown(benchmark),
        "calmar": cagr / abs(_max_drawdown(equity)) if _max_drawdown(equity) < 0 else 0.0,
        "avg_exposure": float(position.mean()),
        "time_in_market": float((position > 0.01).mean()),
        "annual_turnover": float(turnover.iloc[first:].sum() / years),
        "annual_cost_drag": float(costs.sum() / years),
        "n_refits": len(refits),
        "total_return": float(equity.iloc[-1] - 1.0),
        "benchmark_total_return": float(benchmark.iloc[-1] - 1.0),
    }

    # Deflate the Sharpe for however many variations were tried. A backtest
    # reports the strategy you kept, not the ones you discarded, and the best
    # of N worthless strategies still looks good.
    assessment: SharpeAssessment | None = None
    try:
        assessment = assess_sharpe(strategy_returns, n_trials=max(1, bt.n_trials),
                                   benchmark_sharpe_annual=b_sharpe)
        stats["deflated_sharpe"] = assessment.dsr
        stats["probabilistic_sharpe"] = assessment.psr
        stats["luck_threshold_sharpe"] = assessment.threshold_annual
        stats["sharpe_is_significant"] = assessment.is_significant
        stats["dsr_vs_benchmark"] = assessment.dsr_vs_benchmark
        stats["beats_benchmark"] = assessment.beats_benchmark
        LOG.info("%s: %s", symbol, assessment.verdict())
    except ValueError as exc:
        LOG.warning("%s: could not assess Sharpe significance (%s)", symbol, exc)

    return BacktestResult(
        equity=equity, benchmark=benchmark, exposure=position,
        returns=strategy_returns, benchmark_returns=benchmark_returns,
        regimes=regimes.iloc[first:], stats=stats, refits=refits,
        assessment=assessment,
    )


def regime_conditional_returns(result: BacktestResult) -> pd.DataFrame:
    """What the benchmark actually did while each regime was signalled.

    The honest test of a regime model: if bars labelled Bull do not have higher
    forward returns than bars labelled HighVolatility, the labels are decoration.
    """
    frame = pd.DataFrame({
        "regime": result.regimes,
        "benchmark_return": result.benchmark_returns,
    }).dropna()
    if frame.empty:
        return pd.DataFrame()

    grouped = frame.groupby("regime")["benchmark_return"]
    out = pd.DataFrame({
        "bars": grouped.size(),
        "share": grouped.size() / len(frame),
        "ann_return": grouped.mean() * TRADING_DAYS,
        "ann_volatility": grouped.std(ddof=0) * np.sqrt(TRADING_DAYS),
        "hit_rate": grouped.apply(lambda s: float((s > 0).mean())),
    })
    out["sharpe"] = (out["ann_return"] / out["ann_volatility"]).replace([np.inf, -np.inf], 0.0)
    return out.sort_values("ann_return", ascending=False)
