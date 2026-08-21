"""Hidden Markov regime detection.

``hmm_model``  Forward / Viterbi / Baum-Welch over Gaussian emissions
``labeling``   mapping anonymous states onto named regimes
"""

from __future__ import annotations

from .hmm_model import FitReport, NotFittedError, RegimeHMM
from .labeling import ARCHETYPES, RegimeLabel, RegimeMap, StateStats, build_regime_map

__all__ = [
    "RegimeHMM", "FitReport", "NotFittedError",
    "RegimeLabel", "RegimeMap", "StateStats", "build_regime_map", "ARCHETYPES",
]
