"""End-to-end analysis: prices in, reviewed recommendation out.

    load prices -> indicators -> features -> Baum-Welch -> forward filter
                -> regime map -> AnalysisContext -> five-agent debate -> verdict

:class:`RegimeEngine` owns the model. :func:`analyze_symbol` wires everything
together for a single decision.

On fitting over all available history
-------------------------------------
For a *live* decision the engine fits on every bar up to and including today.
That is not lookahead -- there is no future to leak. It does mean the regime
labels attached to old bars in :meth:`RegimeEngine.history` were produced by a
model that has since seen more data, so those labels are a *description* of the
past, not what the system would have said at the time. When the question is
"what would this have told me on that day", use :mod:`stockagent.backtest`,
which refits on trailing windows only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .agents.base import AnalysisContext
from .agents.devils_advocate import DevilsAdvocateAgent
from .agents.discovery_agent import DiscoveryAgent
from .agents.manager import ManagerAgent
from .agents.regime_agent import RegimeAgent
from .agents.risk_agent import RiskAgent
from .config import Config
from .data_io import assess_quality, load_prices, load_universe
from .debate import DebateOrchestrator, DebateResult
from .features import build_features, prepare_for_hmm
from .indicators import add_all_indicators
from .live import load_live_snapshot
from .logging_setup import DecisionLog, get_logger
from .portfolio import Portfolio
from .regime import RegimeHMM, RegimeMap, build_regime_map

LOG = get_logger("pipeline")


@dataclass
class RegimeState:
    """The regime read for one bar."""

    label: str
    confidence: float
    probs: dict[str, float]
    exposure: float
    diagnostics: dict[str, Any] = field(default_factory=dict)


class RegimeEngine:
    """Fits the HMM for one symbol and reports regimes from it."""

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self.model: RegimeHMM | None = None
        self.regime_map: RegimeMap | None = None
        self.features: pd.DataFrame | None = None
        self.X: np.ndarray | None = None
        self._filtered: np.ndarray | None = None

    def fit(self, prices: pd.DataFrame, *, train_end: int | None = None) -> "RegimeEngine":
        """Build features, run Baum-Welch, and name the resulting states."""
        self.features = build_features(prices, self.cfg.features)
        if len(self.features) < self.cfg.data.min_bars:
            raise ValueError(
                f"only {len(self.features)} usable feature rows; need at least "
                f"{self.cfg.data.min_bars}. Fetch more history."
            )
        self.X, _ = prepare_for_hmm(self.features, train_end, self.cfg.features)
        fit_slice = self.X if train_end is None else self.X[:train_end]
        returns = self.features["ret"] if train_end is None else self.features["ret"].iloc[:train_end]

        self.model = RegimeHMM(self.cfg.hmm).fit(fit_slice, list(self.features.columns))
        self.regime_map = build_regime_map(self.model, returns, fit_slice)
        self._filtered = self.model.filter(self.X)
        return self

    def state_at(self, position: int = -1) -> RegimeState:
        """Regime at one bar, from **filtered** (causal) posteriors."""
        if self.model is None or self.regime_map is None or self._filtered is None:
            raise RuntimeError("call fit() first")

        row = self._filtered[position]
        by_label = self.regime_map.probability_by_label(row)
        label, confidence = max(by_label.items(), key=lambda kv: kv[1])
        best_state = int(np.argmax(row))
        transmat = self.model.transition_matrix

        return RegimeState(
            label=label.value if hasattr(label, "value") else str(label),
            confidence=float(confidence),
            probs={(k.value if hasattr(k, "value") else str(k)): float(v)
                   for k, v in by_label.items()},
            exposure=self.regime_map.expected_exposure(row),
            diagnostics={
                "expected_duration": float(self.model.expected_duration()[best_state]),
                "transition_out_prob": float(1.0 - transmat[best_state, best_state]),
                "dominant_state": best_state,
                "model_warnings": self.regime_map.warnings(),
                "restart_spread": (self.model.fit_report.restart_spread
                                   if self.model.fit_report else None),
                "converged": (self.model.fit_report.converged
                              if self.model.fit_report else None),
                "log_likelihood": (round(self.model.fit_report.log_likelihood, 2)
                                   if self.model.fit_report else None),
            },
        )

    def history(self) -> pd.DataFrame:
        """Per-bar filtered probabilities, decoded label and Viterbi path.

        The Viterbi column is a whole-sequence estimate and can revise earlier
        bars; it is here to describe history, not to be traded.
        """
        if self.model is None or self.regime_map is None or self._filtered is None:
            raise RuntimeError("call fit() first")
        labels = [lab.value for lab in self.regime_map.labels()]
        out = pd.DataFrame(self._filtered, index=self.features.index,
                           columns=[f"p_state_{i}" for i in range(self.model.n_states)])
        for i, label in enumerate(labels):
            column = f"p_{label}"
            out[column] = out.get(column, 0.0) + self._filtered[:, i]
        out["filtered_label"] = [labels[i] for i in self._filtered.argmax(axis=1)]
        out["filtered_confidence"] = self._filtered.max(axis=1)
        out["viterbi_label"] = [labels[i] for i in self.model.viterbi(self.X)]
        out["exposure"] = [self.regime_map.expected_exposure(row) for row in self._filtered]
        return out


def build_analysis_context(symbol: str, prices: pd.DataFrame, state: RegimeState,
                           portfolio: Portfolio, cfg: Config,
                           *, live: dict[str, Any] | None = None,
                           extra_warnings: list[str] | None = None) -> AnalysisContext:
    """Assemble the frozen context every agent will see."""
    frame = add_all_indicators(prices)
    quality = assess_quality(prices, symbol)
    warnings = quality.warnings() + list(extra_warnings or [])
    return AnalysisContext(
        symbol=symbol,
        asof=frame.index[-1],
        frame=frame,
        regime_probs=state.probs,
        regime_label=state.label,
        regime_confidence=state.confidence,
        regime_exposure=state.exposure,
        regime_diagnostics=state.diagnostics,
        portfolio=portfolio.snapshot({symbol: float(frame["close"].iloc[-1])}),
        live=live or {},
        data_warnings=warnings,
        config=cfg,
    )


def vix_context(cfg: Config) -> dict[str, Any]:
    """VIX level and its percentile rank, computed from the local series."""
    path = cfg.data.data_dir / f"{cfg.data.volatility_index}.csv"
    if not path.exists():
        return {}
    try:
        vix = load_prices(path, symbol=cfg.data.volatility_index)
    except Exception as exc:  # noqa: BLE001 - VIX is an optional overlay
        LOG.warning("could not load %s: %s", path.name, exc)
        return {}
    close = vix["close"].dropna()
    if close.empty:
        return {}
    window = close.tail(756)
    return {
        "vix_level": float(close.iloc[-1]),
        "vix_percentile": float((window <= close.iloc[-1]).mean()),
        "vix_asof": str(close.index[-1].date()),
    }


def live_context(cfg: Config) -> dict[str, Any]:
    """The full live overlay: connector snapshot with a local VIX fallback.

    The snapshot wins where it has fresh values, because it came from a
    connector; the locally-computed VIX fills in when the snapshot is missing,
    stale, or predates the latest close.
    """
    overlay = dict(vix_context(cfg))
    snapshot = load_live_snapshot(cfg.data.data_dir / "live_snapshot.json")
    for key, value in snapshot.items():
        if value is not None and value != {} and value != []:
            overlay[key] = value
    return overlay


def build_agents(cfg: Config, portfolio: Portfolio,
                 universe: dict[str, pd.DataFrame]) -> tuple[list, ManagerAgent]:
    """Instantiate the four specialists and the Manager."""
    scored = {sym: add_all_indicators(df) for sym, df in universe.items()}
    specialists = [
        RegimeAgent(min_confidence=cfg.debate.min_regime_confidence),
        DiscoveryAgent(scored, min_dollar_volume=cfg.risk.min_dollar_volume),
        RiskAgent(portfolio, cfg.risk),
        DevilsAdvocateAgent(),
    ]
    manager = ManagerAgent(
        min_regime_confidence=cfg.debate.min_regime_confidence,
        conflict_threshold=cfg.debate.conflict_threshold,
        blocking_agents=cfg.debate.blocking_agents,
        dissenting_agents=cfg.debate.dissenting_agents,
        max_objection_score=cfg.debate.max_objection_score,
    )
    return specialists, manager


def analyze_symbol(symbol: str, cfg: Config | None = None, *,
                   equity: float = 10_000.0,
                   portfolio: Portfolio | None = None,
                   live: dict[str, Any] | None = None,
                   decision_log: DecisionLog | None = None,
                   universe: dict[str, pd.DataFrame] | None = None
                   ) -> tuple[DebateResult, RegimeEngine]:
    """Run the whole pipeline for one symbol and return the debate result."""
    cfg = cfg or Config()
    prices = load_prices(cfg.data.data_dir / f"{symbol}.csv", symbol=symbol)

    engine = RegimeEngine(cfg).fit(prices)
    state = engine.state_at(-1)
    LOG.info("%s regime: %s at %.0f%% (exposure %.0f%%)",
             symbol, state.label, state.confidence * 100, state.exposure * 100)

    portfolio = portfolio or Portfolio(equity, cfg.risk)
    if universe is None:
        universe = load_universe(cfg.data.data_dir, cfg.data.universe or None,
                                 min_bars=cfg.data.min_bars)

    overlay = live_context(cfg)
    overlay.update(live or {})

    ctx = build_analysis_context(symbol, prices, state, portfolio, cfg, live=overlay)
    specialists, manager = build_agents(cfg, portfolio, universe)
    orchestrator = DebateOrchestrator(
        specialists, manager, max_rounds=cfg.debate.max_debate_rounds,
        decision_log=decision_log,
    )
    return orchestrator.run(ctx), engine
