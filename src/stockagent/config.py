"""Typed configuration for the stock agent.

Every tunable number the system uses lives here rather than being scattered as
literals through the code. Defaults encode the standing trading rules from
``knowledge/Trading-Rules.md``; a JSON file can override any of them:

    cfg = Config.load("config.json")

The Rules page of the Investing Command Center makes a point that "rules with
blanks are not rules". The same idea applies here: there are no ``None``
thresholds waiting to be filled in at runtime. Every field has a value, and
:meth:`Config.validate` refuses configurations that are internally inconsistent.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Raised when a configuration is internally inconsistent."""


@dataclass
class DataConfig:
    """Where price history lives and how much of it to trust."""

    data_dir: pathlib.Path = PROJECT_ROOT / "data"
    benchmark: str = "SPY"
    #: Symbol whose implied volatility is used as an external stress check.
    volatility_index: str = "VIX"
    #: Bars required before the system will say anything at all about a symbol.
    min_bars: int = 400
    #: Discovery universe. Falls back to whatever CSVs exist in ``data_dir``.
    universe: list[str] = field(default_factory=list)


@dataclass
class FeatureConfig:
    """Observation vector fed to the HMM."""

    #: Rolling window for realised volatility, in trading days.
    volatility_window: int = 21
    #: Rolling window used to z-score log volume against its own recent history.
    volume_window: int = 63
    #: Lookback for the standardisation of the full feature matrix.
    zscore_window: int = 252
    #: Winsorise standardised features to +/- this many sigma before fitting.
    #: Gaussian emissions are not fat-tailed; without this, one 1987-style bar
    #: can capture an entire hidden state.
    clip_sigma: float = 5.0


@dataclass
class HMMConfig:
    """Baum-Welch training and Viterbi/forward inference settings."""

    #: 3 -> Bull/Bear/Sideways. 5 -> adds Recovery and HighVolatility.
    n_states: int = 5
    covariance_type: str = "full"
    n_iter: int = 300
    tol: float = 1e-4
    #: Baum-Welch is EM: it finds a local optimum. Restart and keep the best
    #: log-likelihood, otherwise the regime map depends on the random seed.
    n_restarts: int = 12
    random_state: int = 20260807
    #: Added to the diagonal of each covariance to keep it positive definite.
    min_covar: float = 1e-4


@dataclass
class RiskConfig:
    """Hard capital-preservation limits. These are vetoes, not preferences."""

    #: Never risk more than this fraction of equity on a single trade.
    max_risk_per_trade: float = 0.01
    #: Sum of open risk across all positions.
    max_portfolio_heat: float = 0.06
    max_open_positions: int = 10
    #: Stop distance as a multiple of ATR.
    atr_stop_multiple: float = 2.0
    #: Take-profit targets as multiples of the initial risk (R).
    target_r_multiples: tuple[float, ...] = (1.5, 3.0)
    #: Reject any idea whose reward-to-risk is below this.
    min_reward_risk: float = 1.5
    #: Block new longs when the blackout flag is set (earnings, FOMC, CPI).
    news_blackout_days: int = 1
    #: Refuse to trade a symbol thinner than this (dollar volume, 20d median).
    min_dollar_volume: float = 5_000_000.0
    #: Allow fractional shares. Defaults to on: on a small account this is the
    #: difference between a usable system and one that refuses every trade. At
    #: $218 equity a 1% risk budget is $2.18, and a whole-share position in
    #: anything priced over ~$87 rounds to zero. Webull supports fractional
    #: equity orders. Set False if your broker requires whole shares.
    allow_fractional_shares: bool = True
    #: Decimal places for fractional quantities.
    fractional_precision: int = 5
    #: Skip positions worth less than this -- below it, the spread and the
    #: attention cost more than the position can plausibly return.
    min_position_value: float = 5.0


@dataclass
class DebateConfig:
    """How the five agents reach consensus."""

    #: Confidence below which the Manager will not approve anything.
    min_regime_confidence: float = 0.55
    #: Agents whose confidence differs by more than this are "in conflict".
    conflict_threshold: float = 0.25
    #: Maximum rounds of rebuttal before the Manager forces a decision.
    max_debate_rounds: int = 2
    #: A majority vote alone is explicitly not sufficient; the Manager holds a
    #: veto and these agents can each independently block a trade.
    blocking_agents: tuple[str, ...] = ("risk", "devils_advocate")
    #: Agents appointed to disagree. Their dissent is expected, so it does not
    #: count as deadlock -- their *evidence* is weighed instead. Without this,
    #: the designated objector objects to everything and nothing is ever
    #: approved.
    dissenting_agents: tuple[str, ...] = ("devils_advocate",)
    #: Weighted trade-objection tally (SERIOUS=2, CONCERN=1) at which the
    #: Manager rejects. This is the main dial for how selective the system is:
    #: lower approves less, higher approves more. Model- and data-scope
    #: findings never count toward it.
    max_objection_score: int = 5


@dataclass
class BacktestConfig:
    """Walk-forward evaluation settings."""

    #: Bars in the initial training window before the first out-of-sample bar.
    train_window: int = 1000
    #: Refit Baum-Welch every N bars. The model between refits is frozen.
    refit_every: int = 63
    #: Round-trip cost in basis points applied on every change of exposure.
    cost_bps: float = 5.0
    #: Signals are computed on bar t and executed at bar t+1's close.
    execution_lag: int = 1
    #: How many strategy variations you tried before keeping this one. Every
    #: parameter sweep, state count and feature set counts. Used to deflate the
    #: Sharpe ratio for multiple testing -- see :mod:`stockagent.significance`.
    #: 1 means "no correction", which is almost never the truth.
    n_trials: int = 1


@dataclass
class Config:
    """Root configuration object."""

    data: DataConfig = field(default_factory=DataConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    hmm: HMMConfig = field(default_factory=HMMConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    debate: DebateConfig = field(default_factory=DebateConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    log_dir: pathlib.Path = PROJECT_ROOT / "logs"
    artifact_dir: pathlib.Path = PROJECT_ROOT / "artifacts"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Fail loudly on configurations that cannot mean what they say."""
        if self.hmm.n_states not in (2, 3, 4, 5, 6):
            raise ConfigError(f"hmm.n_states must be in 2..6, got {self.hmm.n_states}")
        if not 0 < self.risk.max_risk_per_trade <= 0.05:
            raise ConfigError(
                f"risk.max_risk_per_trade={self.risk.max_risk_per_trade} is outside "
                "(0, 0.05]. Risking >5% of equity on one trade is not a survivable rule."
            )
        if self.risk.max_portfolio_heat < self.risk.max_risk_per_trade:
            raise ConfigError("portfolio heat cap is below the single-trade risk cap")
        if self.risk.max_open_positions < 1:
            raise ConfigError("risk.max_open_positions must be >= 1")
        if self.risk.atr_stop_multiple <= 0:
            raise ConfigError("risk.atr_stop_multiple must be positive")
        if not 0 <= self.risk.fractional_precision <= 8:
            raise ConfigError("risk.fractional_precision must be in 0..8")
        if self.risk.min_position_value < 0:
            raise ConfigError("risk.min_position_value cannot be negative")
        if not self.risk.target_r_multiples:
            raise ConfigError("at least one take-profit target is required")
        if self.backtest.train_window < self.data.min_bars:
            raise ConfigError(
                f"backtest.train_window={self.backtest.train_window} is below "
                f"data.min_bars={self.data.min_bars}; the first fit would be unreliable"
            )
        if self.backtest.execution_lag < 1:
            raise ConfigError(
                "backtest.execution_lag must be >= 1. Executing at the close of the "
                "same bar that generated the signal is lookahead bias."
            )
        if not 0 < self.debate.min_regime_confidence < 1:
            raise ConfigError("debate.min_regime_confidence must be a probability")
        if self.debate.max_objection_score < 1:
            raise ConfigError(
                "debate.max_objection_score must be >= 1; at 0 the Manager would "
                "reject every trade regardless of evidence"
            )

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: str | pathlib.Path | None = None) -> "Config":
        """Build a config, optionally overlaying a JSON file over the defaults."""
        if path is None:
            return cls()
        raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return cls(**_build_section(cls, raw))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable snapshot, for writing next to run artifacts."""
        return _as_jsonable(dataclasses.asdict(self))


def _build_section(cls: type, raw: dict[str, Any]) -> dict[str, Any]:
    """Recursively coerce a plain dict into nested dataclass kwargs."""
    kwargs: dict[str, Any] = {}
    by_name = {f.name: f for f in fields(cls)}
    for key, value in raw.items():
        if key not in by_name:
            raise ConfigError(f"unknown config key {key!r} for {cls.__name__}")
        target = by_name[key].type
        # Resolve the annotation to the actual class when it is a nested config.
        resolved = target if isinstance(target, type) else globals().get(str(target))
        if isinstance(value, dict) and is_dataclass(resolved):
            kwargs[key] = resolved(**_build_section(resolved, value))
        elif key in {"data_dir", "log_dir", "artifact_dir"}:
            kwargs[key] = pathlib.Path(value)
        elif key == "target_r_multiples":
            kwargs[key] = tuple(value)
        else:
            kwargs[key] = value
    return kwargs


def _as_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _as_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_as_jsonable(v) for v in obj]
    if isinstance(obj, pathlib.Path):
        return str(obj)
    return obj
