"""stockagent -- HMM market-regime detection with a five-agent review process.

Layout
------
``config``       every tunable number, validated on construction
``data_io``      loading and validating price history
``indicators``   RSI, MACD, EMA, ATR, Bollinger, OBV -- all causal
``features``     the return/volume/volatility observation vector
``regime``       the HMM itself (Forward, Viterbi, Baum-Welch) and state naming
``agents``       Manager, Regime, Discovery, Risk, Devil's Advocate
``debate``       the consensus process that runs them
``portfolio``    position sizing, stops, targets, exposure limits
``backtest``     walk-forward evaluation with no lookahead

Nothing in this package places an order. It produces analysis and explicitly
labelled recommendations; execution stays manual and human.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "Config", "RegimeHMM", "RegimeLabel", "build_features",
    "build_regime_map", "load_prices", "load_universe", "__version__",
]


def __getattr__(name: str):
    """Lazy re-exports, so ``import stockagent`` stays cheap."""
    if name == "Config":
        from .config import Config
        return Config
    if name in ("RegimeHMM",):
        from .regime.hmm_model import RegimeHMM
        return RegimeHMM
    if name in ("RegimeLabel", "build_regime_map"):
        from .regime import labeling
        return getattr(labeling, name)
    if name == "build_features":
        from .features import build_features
        return build_features
    if name in ("load_prices", "load_universe"):
        from . import data_io
        return getattr(data_io, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
