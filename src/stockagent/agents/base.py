"""Shared vocabulary for the five agents.

Every agent speaks in :class:`Finding` objects: a claim, how strongly it is
held, and the :class:`Evidence` behind it. The Manager is entitled to reject any
finding that arrives without evidence, which is why ``evidence`` is not
optional in practice and :meth:`Finding.is_supported` exists to check it.

These agents are **deterministic and quantitative** -- they compute statistics
and apply explicit rules. They are not LLM calls. That is a deliberate choice:
the debate has to be reproducible and auditable, and a backtest over 5,000 bars
cannot make 25,000 model calls. :class:`Agent` defines the interface an
LLM-backed reasoner would implement, so one can be dropped in per agent without
touching the orchestration; see ``prompts/master_prompt.txt``.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


class Severity(enum.IntEnum):
    """How much weight a finding carries. Ordered, so ``max()`` is meaningful."""

    INFO = 0
    CONCERN = 1
    SERIOUS = 2
    #: A blocking finding from a blocking agent stops the trade outright,
    #: regardless of how the other four voted.
    BLOCKING = 3


class Scope(enum.Enum):
    """What a finding is actually about.

    This distinction exists because of a real failure. The Devil's Advocate was
    reporting "Baum-Welch restarts disagree by 284 nats" and "no state resembles
    Bear" as SERIOUS objections. Both are true, but they are properties of the
    *fitted model* and fire identically on every symbol you analyse. Counted as
    trade objections they vetoed everything, permanently, and a system that can
    never approve carries no information.

    Model- and data-scope findings are still reported -- they belong in the
    audit trail and in the conditions attached to an approval. They just do not
    count toward the per-trade objection tally.
    """

    #: Specific to this instrument and this setup, right now.
    TRADE = "trade"
    #: A property of the fitted model. Constant across symbols.
    MODEL = "model"
    #: Data quality. Constant across analyses of the same series.
    DATA = "data"


class Stance(enum.Enum):
    """An agent's position on the question put to it."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    AVOID = "avoid"
    #: Not enough information to have a view. Distinct from HOLD, which is an
    #: active judgement that doing nothing is correct.
    ABSTAIN = "abstain"

    @property
    def is_actionable(self) -> bool:
        return self in (Stance.BUY, Stance.SELL)


class Decision(enum.Enum):
    """The Manager's verdict."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_REANALYSIS = "REQUEST_REANALYSIS"


@dataclass(frozen=True)
class Evidence:
    """One measured fact. The unit of proof in this system."""

    name: str
    value: Any
    detail: str = ""
    #: Where the number came from: "hmm", "indicators", "prices", "live", "config".
    source: str = "computed"

    def __str__(self) -> str:
        value = f"{self.value:.4g}" if isinstance(self.value, float) else str(self.value)
        return f"{self.name}={value}" + (f" ({self.detail})" if self.detail else "")


@dataclass
class Finding:
    """A claim an agent is prepared to defend."""

    agent: str
    claim: str
    severity: Severity = Severity.INFO
    #: Subjective confidence in [0, 1].
    confidence: float = 0.5
    evidence: list[Evidence] = field(default_factory=list)
    #: True if it argues for the trade, False against, None if neutral.
    supports: bool | None = None
    #: Whether this is about the trade, the model, or the data.
    scope: Scope = Scope.TRADE

    def __post_init__(self) -> None:
        self.confidence = float(min(max(self.confidence, 0.0), 1.0))

    @property
    def is_trade_objection(self) -> bool:
        """An argument against *this trade*, as opposed to a standing caveat."""
        return self.supports is False and self.scope is Scope.TRADE

    @property
    def is_supported(self) -> bool:
        """Whether this finding carries any evidence at all."""
        return bool(self.evidence)

    def __str__(self) -> str:
        arrow = {True: "FOR", False: "AGAINST", None: "--"}[self.supports]
        body = "; ".join(str(e) for e in self.evidence) or "NO EVIDENCE"
        return f"[{self.severity.name}/{arrow}] {self.claim}  <{body}>"


@dataclass
class AgentReport:
    """Everything one agent concluded in one round."""

    agent: str
    stance: Stance
    #: Confidence in the stance, [0, 1].
    confidence: float
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    #: True when produced by :meth:`Agent.rebut` rather than the first pass.
    revised: bool = False
    round_index: int = 0

    def __post_init__(self) -> None:
        self.confidence = float(min(max(self.confidence, 0.0), 1.0))

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKING]

    @property
    def max_severity(self) -> Severity:
        return max((f.severity for f in self.findings), default=Severity.INFO)

    @property
    def unsupported(self) -> list[Finding]:
        """Findings above INFO that arrived without evidence."""
        return [f for f in self.findings
                if not f.is_supported and f.severity > Severity.INFO]

    def objections(self) -> list[Finding]:
        return [f for f in self.findings if f.supports is False]

    def trade_objections(self) -> list[Finding]:
        """Objections about this trade, excluding standing model/data caveats."""
        return [f for f in self.findings if f.is_trade_objection]

    def standing_caveats(self) -> list[Finding]:
        """Model- and data-scope concerns: reported, but not per-trade vetoes."""
        return [f for f in self.findings
                if f.supports is False and f.scope is not Scope.TRADE]

    def objection_score(self) -> int:
        """Weighted tally of trade objections: SERIOUS counts double."""
        return sum(2 if f.severity >= Severity.SERIOUS else 1
                   for f in self.trade_objections())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent, "stance": self.stance.value,
            "confidence": round(self.confidence, 4), "revised": self.revised,
            "round": self.round_index, "summary": self.summary,
            "findings": [
                {
                    "claim": f.claim, "severity": f.severity.name,
                    "scope": f.scope.value,
                    "confidence": round(f.confidence, 4), "supports": f.supports,
                    "evidence": [{"name": e.name, "value": e.value,
                                  "detail": e.detail, "source": e.source}
                                 for e in f.evidence],
                }
                for f in self.findings
            ],
        }


@dataclass
class TradeIdea:
    """A concrete, fully-specified proposal.

    Every field is required before the Manager will look at it. An idea without
    a stop is not a trade, it is a hope.
    """

    symbol: str
    direction: str            # "long" or "short"
    entry: float
    stop: float
    targets: list[float]
    #: Float, because fractional orders are supported.
    shares: float
    risk_amount: float        # currency at risk if the stop fills
    risk_pct_equity: float
    rationale: str = ""

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_risk(self) -> float:
        """Reward-to-risk to the first target."""
        if not self.targets or self.risk_per_share <= 0:
            return 0.0
        return abs(self.targets[0] - self.entry) / self.risk_per_share

    def validate(self) -> list[str]:
        """Structural problems that make the idea unusable as stated."""
        problems: list[str] = []
        if self.risk_per_share <= 0:
            problems.append("stop equals entry; risk per share is zero")
        if self.direction == "long" and self.stop >= self.entry:
            problems.append(f"long with stop {self.stop} at or above entry {self.entry}")
        if self.direction == "short" and self.stop <= self.entry:
            problems.append(f"short with stop {self.stop} at or below entry {self.entry}")
        if not self.targets:
            problems.append("no take-profit target defined")
        if self.shares <= 0:
            problems.append("position size rounds to zero shares")
        return problems

    @property
    def position_value(self) -> float:
        return self.shares * self.entry

    def quantity_str(self) -> str:
        return f"{self.shares:g}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "direction": self.direction,
            "entry": round(self.entry, 4), "stop": round(self.stop, 4),
            "targets": [round(t, 4) for t in self.targets],
            "shares": self.shares, "risk_amount": round(self.risk_amount, 2),
            "risk_pct_equity": round(self.risk_pct_equity, 5),
            "reward_risk": round(self.reward_risk, 3), "rationale": self.rationale,
        }


@dataclass
class AnalysisContext:
    """Everything the agents are allowed to look at.

    Passing one frozen context to every agent is what makes the debate fair:
    they disagree because they weigh the same facts differently, not because
    one of them saw a number the others did not.
    """

    symbol: str
    asof: pd.Timestamp
    #: Price history up to and including ``asof``, with indicators attached.
    frame: pd.DataFrame
    #: Regime posterior for ``asof``, keyed by regime name.
    regime_probs: dict[str, float] = field(default_factory=dict)
    regime_label: str = "Unknown"
    regime_confidence: float = 0.0
    #: Suggested exposure from the regime map, [0, 1].
    regime_exposure: float = 0.0
    #: Diagnostics from the fit: durations, warnings, restart spread.
    regime_diagnostics: dict[str, Any] = field(default_factory=dict)
    #: Account state: equity, cash, open positions, current heat.
    portfolio: dict[str, Any] = field(default_factory=dict)
    #: Optional live overlay refreshed from the MCP connectors.
    live: dict[str, Any] = field(default_factory=dict)
    #: Data-quality warnings that every agent must be able to see.
    data_warnings: list[str] = field(default_factory=list)
    config: Any = None

    @property
    def latest(self) -> pd.Series:
        """The most recent bar."""
        return self.frame.iloc[-1]

    def value(self, column: str, default: float = float("nan")) -> float:
        """Latest value of an indicator column, or ``default`` if unavailable."""
        if column not in self.frame.columns:
            return default
        val = self.frame[column].iloc[-1]
        return default if pd.isna(val) else float(val)


class Agent(abc.ABC):
    """Base class for the five specialists.

    Subclasses implement :meth:`analyze` (independent first pass) and may
    override :meth:`rebut` (revise after seeing the others' reports).
    """

    #: Short stable identifier used in logs and the audit trail.
    name: str = "agent"
    #: One line describing the agent's job, shown in reports.
    role: str = ""

    @abc.abstractmethod
    def analyze(self, ctx: AnalysisContext) -> AgentReport:
        """Form an independent view. Must not consult other agents."""

    def rebut(self, ctx: AnalysisContext, peers: list[AgentReport]) -> AgentReport:
        """Reconsider in light of peer reports.

        The default keeps the original position unchanged; agents that are
        supposed to move (or to dig in) override this.
        """
        report = self.analyze(ctx)
        report.revised = False
        return report

    def _report(self, stance: Stance, confidence: float,
                findings: list[Finding], summary: str) -> AgentReport:
        return AgentReport(agent=self.name, stance=stance, confidence=confidence,
                           findings=findings, summary=summary)

    def _finding(self, claim: str, *, severity: Severity = Severity.INFO,
                 confidence: float = 0.5, supports: bool | None = None,
                 evidence: list[Evidence] | None = None,
                 scope: Scope = Scope.TRADE) -> Finding:
        return Finding(agent=self.name, claim=claim, severity=severity,
                       confidence=confidence, supports=supports,
                       evidence=evidence or [], scope=scope)
