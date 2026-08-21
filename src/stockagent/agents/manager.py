"""Agent 1: Manager (Supervisor / CIO).

Reviews the other four and decides. The Manager never simply counts votes --
a majority is explicitly not sufficient here, because four agents agreeing on a
weak signal is four correlated errors, not four confirmations.

Review order, first match wins:

1. **Structural blocks** -- a BLOCKING finding from a blocking agent ends it.
2. **Weak reasoning** -- claims above INFO with no evidence attached.
3. **Missing information** -- data warnings, abstentions, unusable inputs.
4. **Conflict** -- agents whose confidence or direction genuinely disagree.
5. **Insufficient support** -- nothing actually argues *for* the trade.
6. **Approve**, with conditions attached.

Steps 2-4 return ``REQUEST_REANALYSIS`` on the first pass and harden to
``REJECT`` if the same problem survives the debate. Sending work back once is
diligence; sending it back forever is a way of never deciding, and the default
when a question stays unresolved is no trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import (Agent, AgentReport, AnalysisContext, Decision, Evidence,
                   Finding, Severity, Stance, TradeIdea)


@dataclass
class ManagerVerdict:
    """The Manager's decision and the reasoning that produced it."""

    decision: Decision
    rationale: str
    #: Conditions the trade must satisfy if approved.
    conditions: list[str] = field(default_factory=list)
    #: Specific questions the agents must answer on a re-run.
    questions: list[str] = field(default_factory=list)
    confidence: float = 0.0
    idea: TradeIdea | None = None
    reviewed: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.decision is Decision.APPROVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value, "rationale": self.rationale,
            "conditions": self.conditions, "questions": self.questions,
            "confidence": round(self.confidence, 4),
            "idea": self.idea.to_dict() if self.idea else None,
            "reviewed": self.reviewed,
        }


class ManagerAgent(Agent):
    """Supervises the other agents and holds final authority."""

    name = "manager"
    role = "Reviews all findings, demands evidence, and approves or denies decisions."

    def __init__(self, *, min_regime_confidence: float = 0.55,
                 conflict_threshold: float = 0.25,
                 blocking_agents: tuple[str, ...] = ("risk", "devils_advocate"),
                 dissenting_agents: tuple[str, ...] = ("devils_advocate",),
                 max_objection_score: int = 5) -> None:
        self.min_regime_confidence = min_regime_confidence
        self.conflict_threshold = conflict_threshold
        self.blocking_agents = blocking_agents
        #: Agents whose job is to disagree. Their dissent is the process
        #: working, not a deadlock, so it does not by itself count as conflict.
        #: Their *evidence* is still weighed -- see ``max_objection_score``.
        self.dissenting_agents = dissenting_agents
        #: Weighted trade-objection tally (SERIOUS=2, CONCERN=1) at which the
        #: Manager rejects. Raise it to approve more, lower it to approve less.
        self.max_objection_score = max_objection_score

    # The Manager reviews rather than analyses; it has no independent view of
    # the market, which is the point. Its report summarises the review.
    def analyze(self, ctx: AnalysisContext) -> AgentReport:
        return self._report(Stance.ABSTAIN, 0.0, [],
                            "Manager forms no independent market view; it reviews.")

    def review(self, ctx: AnalysisContext, reports: list[AgentReport],
               *, idea: TradeIdea | None = None,
               final_round: bool = False) -> ManagerVerdict:
        """Assess the debate and return a verdict."""
        by_agent = {r.agent: r for r in reports}
        reviewed = sorted(by_agent)
        escalate = Decision.REJECT if final_round else Decision.REQUEST_REANALYSIS

        # --- 1. Structural blocks -------------------------------------------
        blocks = [
            (r.agent, f) for r in reports if r.agent in self.blocking_agents
            for f in r.blocking
        ]
        if blocks:
            agent, finding = blocks[0]
            return ManagerVerdict(
                decision=Decision.REJECT,
                rationale=(f"{agent} raised a blocking objection: {finding.claim}. "
                           "A blocking agent's veto is not overridable by majority."),
                confidence=finding.confidence, reviewed=reviewed,
                conditions=[f"Resolve: {f.claim}" for _, f in blocks[:3]],
            )

        # --- 2. Weak reasoning ----------------------------------------------
        unsupported = [(r.agent, f) for r in reports for f in r.unsupported]
        if unsupported:
            return ManagerVerdict(
                decision=escalate,
                rationale=(f"{len(unsupported)} claims above INFO carry no evidence. "
                           "Conclusions without evidence do not count toward consensus."),
                questions=[f"{agent}: what evidence supports '{f.claim[:90]}'?"
                           for agent, f in unsupported[:4]],
                confidence=0.3, reviewed=reviewed,
            )

        # --- 3. Missing information ------------------------------------------
        abstentions = [r.agent for r in reports if r.stance is Stance.ABSTAIN]
        if abstentions:
            return ManagerVerdict(
                decision=escalate,
                rationale=(f"{', '.join(abstentions)} could not form a view. Deciding "
                           "while a specialist is unable to assess its own domain is "
                           "guessing with extra steps."),
                questions=[f"{agent}: what input would let you form a view?"
                           for agent in abstentions],
                confidence=0.2, reviewed=reviewed,
            )

        if ctx.regime_confidence < self.min_regime_confidence:
            return ManagerVerdict(
                decision=Decision.REJECT,
                rationale=(f"Regime posterior {ctx.regime_confidence:.0%} is below the "
                           f"{self.min_regime_confidence:.0%} floor. Without a regime "
                           "read, position sizing has no basis."),
                confidence=1.0 - ctx.regime_confidence, reviewed=reviewed,
                conditions=["Wait for the regime posterior to exceed "
                            f"{self.min_regime_confidence:.0%}"],
            )

        # --- 4. Conflict -----------------------------------------------------
        conflict = self._detect_conflict(reports)
        if conflict:
            return ManagerVerdict(
                decision=escalate,
                rationale=f"Unresolved disagreement: {conflict}",
                questions=[f"{r.agent}: defend {r.stance.value} at "
                           f"{r.confidence:.0%} against the opposing evidence"
                           for r in reports if r.stance is not Stance.ABSTAIN],
                confidence=0.35, reviewed=reviewed,
            )

        # --- 5. Weigh the dissent on its evidence ----------------------------
        score, dissent = self._weigh_dissent(reports)
        if score >= self.max_objection_score:
            return ManagerVerdict(
                decision=Decision.REJECT,
                rationale=(f"Trade-specific objections total {score}, at or above the "
                           f"{self.max_objection_score} threshold: "
                           + "; ".join(f.claim[:70] for f in dissent[:3])),
                confidence=min(0.95, 0.5 + 0.08 * score), reviewed=reviewed,
                conditions=[f"Unresolved: {f.claim}" for f in dissent[:4]],
            )

        # --- 6. Insufficient support -----------------------------------------
        supporters = [r for r in reports if r.stance is Stance.BUY]
        blockers = [r for r in reports
                    if r.stance is Stance.AVOID and r.agent not in self.dissenting_agents]
        if blockers:
            return ManagerVerdict(
                decision=Decision.REJECT,
                rationale=(f"{', '.join(r.agent for r in blockers)} recommend avoiding "
                           "this trade and no counter-evidence overturned them."),
                confidence=max(r.confidence for r in blockers), reviewed=reviewed,
            )
        if not supporters:
            return ManagerVerdict(
                decision=Decision.REJECT,
                rationale="No agent argues for the trade. The default is no position.",
                confidence=0.6, reviewed=reviewed,
            )

        risk_report = by_agent.get("risk")
        if risk_report is not None and risk_report.stance is not Stance.BUY:
            return ManagerVerdict(
                decision=Decision.REJECT,
                rationale=(f"Risk is at {risk_report.stance.value}, not buy: "
                           f"{risk_report.summary}. Risk must affirmatively clear a trade, "
                           "not merely fail to block it."),
                confidence=risk_report.confidence, reviewed=reviewed,
            )

        if idea is None or idea.validate():
            problems = idea.validate() if idea else ["no sized trade idea was produced"]
            return ManagerVerdict(
                decision=Decision.REJECT,
                rationale=f"The proposal is not executable: {'; '.join(problems)}.",
                confidence=0.8, reviewed=reviewed,
            )

        # --- 7. Approve, with conditions -------------------------------------
        conditions = self._conditions(ctx, reports, idea)
        # Standing caveats ride along on the approval. They did not veto the
        # trade, but the person placing it should still see them.
        for report in reports:
            for finding in report.standing_caveats():
                conditions.append(f"Standing caveat ({finding.scope.value}): {finding.claim}")
        confidence = self._consensus_confidence(reports)
        return ManagerVerdict(
            decision=Decision.APPROVE,
            rationale=(f"{len(supporters)} agents support the trade, risk cleared it, "
                       f"and dissent scored {score} against a {self.max_objection_score} "
                       f"threshold. Consensus confidence {confidence:.0%}."),
            conditions=conditions, confidence=confidence, idea=idea, reviewed=reviewed,
        )

    # ------------------------------------------------------------- internals

    def _detect_conflict(self, reports: list[AgentReport]) -> str | None:
        """Genuine disagreement, as opposed to the loyal opposition doing its job.

        The Devil's Advocate is *appointed* to argue against. Counting its
        dissent as an unresolved conflict deadlocked every decision the system
        ever made, because the designated objector always objects. Its stance
        is therefore excluded here; its evidence is weighed separately in
        :meth:`_weigh_dissent`, which is where it can actually stop a trade.
        """
        actionable = [r for r in reports
                      if r.stance is not Stance.ABSTAIN
                      and r.agent not in self.dissenting_agents]
        if len(actionable) < 2:
            return None

        buyers = [r for r in actionable if r.stance is Stance.BUY]
        avoiders = [r for r in actionable if r.stance is Stance.AVOID]
        if buyers and avoiders:
            return (
                f"{', '.join(r.agent for r in buyers)} want to buy while "
                f"{', '.join(r.agent for r in avoiders)} want to avoid"
            )

        confidences = [r.confidence for r in actionable]
        spread = max(confidences) - min(confidences)
        if spread > self.conflict_threshold:
            hi = max(actionable, key=lambda r: r.confidence)
            lo = min(actionable, key=lambda r: r.confidence)
            return (
                f"confidence spread {spread:.0%} exceeds the {self.conflict_threshold:.0%} "
                f"threshold ({hi.agent} {hi.confidence:.0%} vs {lo.agent} {lo.confidence:.0%})"
            )
        return None

    def _weigh_dissent(self, reports: list[AgentReport]) -> tuple[int, list]:
        """Score the dissenters' trade-specific objections.

        Only TRADE-scope objections count. Model-scope findings ("restarts
        disagree by 284 nats", "no state resembles Bear") are true, are
        reported, and are attached to any approval as standing caveats -- but
        they are identical for every symbol, so letting them veto trades vetoes
        all trades and the system stops carrying information.
        """
        objections = [f for r in reports if r.agent in self.dissenting_agents
                      for f in r.trade_objections()]
        score = sum(2 if f.severity >= Severity.SERIOUS else 1 for f in objections)
        return score, objections

    def _consensus_confidence(self, reports: list[AgentReport]) -> float:
        """Confidence in the approved decision.

        Weighted toward the sceptics: the risk and devil's-advocate views carry
        more weight than the ones looking for reasons to act, and every
        unresolved objection subtracts.
        """
        weights = {"regime": 1.0, "discovery": 0.8, "risk": 1.5, "devils_advocate": 1.2}
        total = numerator = 0.0
        for report in reports:
            weight = weights.get(report.agent, 1.0)
            direction = 1.0 if report.stance is Stance.BUY else (
                0.5 if report.stance is Stance.HOLD else 0.0)
            numerator += weight * report.confidence * direction
            total += weight
        base = numerator / total if total else 0.0

        # Penalise unresolved objections *to this trade*. Counting standing
        # model caveats here would drag every approval toward zero confidence
        # by the same fixed amount, which reports as uncertainty about the
        # trade when it is really a constant property of the model.
        objections = sum(len(r.trade_objections()) for r in reports)
        return round(max(0.0, min(1.0, base - 0.04 * objections)), 4)

    def _conditions(self, ctx: AnalysisContext, reports: list[AgentReport],
                    idea: TradeIdea) -> list[str]:
        """Standing conditions attached to an approval."""
        conditions = [
            f"Enter no higher than {idea.entry * 1.005:.2f} (0.5% slippage cap)",
            f"Stop at {idea.stop:.2f} placed at entry, not mentally",
            f"First target {idea.targets[0]:.2f} ({idea.reward_risk:.2f}R)",
            f"Maximum loss {idea.risk_amount:.2f} = {idea.risk_pct_equity:.2%} of equity",
        ]
        for report in reports:
            for finding in report.findings:
                if "Falsification trigger" in finding.claim:
                    conditions.append(finding.claim)
        if ctx.regime_label == "Recovery":
            conditions.append("Recovery regime: half the sized position, or wait for Bull")
        for warning in ctx.data_warnings or []:
            conditions.append(f"Note data limitation: {warning}")
        return conditions
