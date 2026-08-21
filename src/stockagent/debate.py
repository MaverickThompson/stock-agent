"""The consensus process.

Implements the seven-step decision procedure:

1. Agents 2-5 analyse independently (no agent sees another's work).
2. Conclusions are compared and disagreements identified.
3. Agents debate: each is shown the others' reports and may rebut.
4. Agents revise.
5. The Manager reviews everything.
6. The Manager returns APPROVE, REJECT, or REQUEST_REANALYSIS.
7. Only approved decisions become recommendations.

Step 1 matters more than it looks. If agents saw each other's output while
forming a first view, they would anchor on whoever ran first and the debate
would measure ordering, not evidence. They are given the same frozen
:class:`~stockagent.agents.base.AnalysisContext` and run blind.

``REQUEST_REANALYSIS`` loops back to step 3 with the round counter advanced. On
the final permitted round the Manager is told so, and its escalation path
hardens from "ask again" to "reject" -- an unresolved question is a no, and a
process that can defer forever never has to be right.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .agents.base import (Agent, AgentReport, AnalysisContext, Decision,
                          Stance, TradeIdea)
from .agents.manager import ManagerAgent, ManagerVerdict
from .logging_setup import DecisionLog, get_logger

LOG = get_logger("debate")


@dataclass
class DebateRound:
    """One pass of analysis or rebuttal."""

    index: int
    reports: list[AgentReport]
    conflicts: list[str] = field(default_factory=list)
    verdict: ManagerVerdict | None = None

    def stances(self) -> dict[str, str]:
        return {r.agent: r.stance.value for r in self.reports}


@dataclass
class DebateResult:
    """The full record of one decision."""

    symbol: str
    asof: Any
    rounds: list[DebateRound]
    verdict: ManagerVerdict
    elapsed_s: float = 0.0

    @property
    def approved(self) -> bool:
        return self.verdict.approved

    @property
    def final_reports(self) -> list[AgentReport]:
        return self.rounds[-1].reports if self.rounds else []

    def recommendation(self) -> str:
        """BUY / SELL / HOLD. Only an approved verdict can be actionable."""
        if not self.verdict.approved or self.verdict.idea is None:
            return "HOLD"
        return "BUY" if self.verdict.idea.direction == "long" else "SELL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asof": str(self.asof),
            "recommendation": self.recommendation(),
            "verdict": self.verdict.to_dict(),
            "rounds": [
                {
                    "index": r.index,
                    "stances": r.stances(),
                    "conflicts": r.conflicts,
                    "reports": [rep.to_dict() for rep in r.reports],
                    "verdict": r.verdict.to_dict() if r.verdict else None,
                }
                for r in self.rounds
            ],
            "elapsed_s": round(self.elapsed_s, 3),
        }

    def explain(self) -> str:
        """A human-readable account of how the decision was reached."""
        lines = [
            f"{self.symbol} @ {self.asof} -> {self.recommendation()} "
            f"[{self.verdict.decision.value}]",
            f"  Manager: {self.verdict.rationale}",
            f"  Confidence: {self.verdict.confidence:.0%}",
        ]
        for round_ in self.rounds:
            lines.append(f"  --- round {round_.index} ---")
            for report in round_.reports:
                mark = " (revised)" if report.revised else ""
                lines.append(
                    f"    {report.agent:<16} {report.stance.value:<8} "
                    f"{report.confidence:>5.0%}{mark}  {report.summary}")
                for finding in report.findings:
                    if finding.severity.value >= 2 or finding.supports is False:
                        lines.append(f"        - {finding}")
            for conflict in round_.conflicts:
                lines.append(f"    CONFLICT: {conflict}")
        if self.verdict.conditions:
            lines.append("  Conditions:")
            lines.extend(f"    * {c}" for c in self.verdict.conditions)
        if self.verdict.questions:
            lines.append("  Open questions:")
            lines.extend(f"    ? {q}" for q in self.verdict.questions)
        return "\n".join(lines)


class DebateOrchestrator:
    """Runs the seven-step process over a set of agents."""

    def __init__(self, agents: list[Agent], manager: ManagerAgent,
                 *, max_rounds: int = 2,
                 decision_log: DecisionLog | None = None) -> None:
        if not agents:
            raise ValueError("at least one specialist agent is required")
        self.agents = agents
        self.manager = manager
        self.max_rounds = max(1, max_rounds)
        self.decision_log = decision_log

    def run(self, ctx: AnalysisContext) -> DebateResult:
        """Execute the process and return the full record."""
        started = time.perf_counter()
        rounds: list[DebateRound] = []
        verdict: ManagerVerdict | None = None

        self._log("debate_started", symbol=ctx.symbol, asof=str(ctx.asof),
                  regime=ctx.regime_label,
                  regime_confidence=round(ctx.regime_confidence, 4),
                  agents=[a.name for a in self.agents])

        # --- Step 1: independent analysis ------------------------------------
        reports = [self._safe(agent, ctx, None) for agent in self.agents]
        for report in reports:
            report.round_index = 0

        for round_index in range(self.max_rounds + 1):
            # --- Step 2: compare -------------------------------------------
            conflicts = self._conflicts(reports)
            round_ = DebateRound(index=round_index, reports=reports, conflicts=conflicts)

            # --- Step 5/6: the Manager reviews -----------------------------
            final = round_index >= self.max_rounds
            idea = self._extract_idea()
            verdict = self.manager.review(ctx, reports, idea=idea, final_round=final)
            round_.verdict = verdict
            rounds.append(round_)

            self._log(
                "debate_round", symbol=ctx.symbol, round=round_index,
                stances=round_.stances(), conflicts=conflicts,
                decision=verdict.decision.value, rationale=verdict.rationale,
                reports=[r.to_dict() for r in reports],
            )
            LOG.info("%s round %d -> %s (%s)", ctx.symbol, round_index,
                     verdict.decision.value, verdict.rationale[:110])

            if verdict.decision is not Decision.REQUEST_REANALYSIS:
                break

            # --- Steps 3/4: debate and revise -------------------------------
            LOG.info("%s: reanalysis requested; %d open question(s)",
                     ctx.symbol, len(verdict.questions))
            peers = list(reports)
            reports = [self._safe(agent, ctx, peers) for agent in self.agents]
            for report in reports:
                report.round_index = round_index + 1

        assert verdict is not None
        result = DebateResult(symbol=ctx.symbol, asof=ctx.asof, rounds=rounds,
                              verdict=verdict,
                              elapsed_s=time.perf_counter() - started)

        # --- Step 7: only approved decisions become recommendations ---------
        self._log("debate_decided", symbol=ctx.symbol, asof=str(ctx.asof),
                  decision=verdict.decision.value,
                  recommendation=result.recommendation(),
                  confidence=round(verdict.confidence, 4),
                  conditions=verdict.conditions,
                  idea=verdict.idea.to_dict() if verdict.idea else None)
        return result

    # -------------------------------------------------------------- helpers

    def _safe(self, agent: Agent, ctx: AnalysisContext,
              peers: list[AgentReport] | None) -> AgentReport:
        """Run an agent, converting a crash into an abstention.

        One agent raising must not take down the debate, but it must also not
        silently look like agreement. An abstention forces the Manager down its
        "missing information" branch, which is the correct response to an
        agent that could not do its job.
        """
        try:
            return agent.rebut(ctx, peers) if peers is not None else agent.analyze(ctx)
        except Exception as exc:  # noqa: BLE001 - deliberate isolation boundary
            LOG.exception("agent %s failed on %s: %s", agent.name, ctx.symbol, exc)
            self._log("agent_error", symbol=ctx.symbol, agent=agent.name, error=repr(exc))
            return AgentReport(
                agent=agent.name, stance=Stance.ABSTAIN, confidence=0.0,
                summary=f"failed with {type(exc).__name__}: {exc}",
            )

    def _extract_idea(self) -> TradeIdea | None:
        """The sized proposal, which only the Risk agent produces.

        Read from the agent rather than the report because sizing is the Risk
        agent's state, not a claim it publishes to its peers. The agent clears
        it at the start of every analysis, so a stale idea from a previous
        round or symbol can never be approved.
        """
        for agent in self.agents:
            idea = getattr(agent, "last_idea", None)
            if idea is not None:
                return idea
        return None

    @staticmethod
    def _conflicts(reports: list[AgentReport]) -> list[str]:
        """Describe every genuine disagreement in the round."""
        out: list[str] = []
        actionable = [r for r in reports if r.stance is not Stance.ABSTAIN]
        buyers = [r.agent for r in actionable if r.stance is Stance.BUY]
        avoiders = [r.agent for r in actionable if r.stance is Stance.AVOID]
        if buyers and avoiders:
            out.append(f"direction: {', '.join(buyers)} buy vs {', '.join(avoiders)} avoid")
        if len(actionable) >= 2:
            confidences = {r.agent: r.confidence for r in actionable}
            spread = max(confidences.values()) - min(confidences.values())
            if spread > 0.25:
                out.append(
                    "confidence spread "
                    f"{spread:.0%}: " + ", ".join(f"{a} {c:.0%}" for a, c in confidences.items())
                )
        blocking = [(r.agent, f.claim) for r in reports for f in r.blocking]
        for agent, claim in blocking:
            out.append(f"blocking objection from {agent}: {claim[:90]}")
        return out

    def _log(self, event: str, **payload: Any) -> None:
        if self.decision_log is not None:
            self.decision_log.record(event, **payload)
