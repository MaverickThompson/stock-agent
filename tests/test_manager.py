"""Tests for the Manager's review logic.

These exist because of a real failure: the system approved nothing, ever. Two
causes, both fixed here and both pinned below.

1. The Devil's Advocate is *appointed* to dissent. Treating its dissent as an
   unresolved conflict deadlocked every decision.
2. It reported model-level facts ("Baum-Welch restarts disagree by 284 nats",
   "no state resembles Bear") as SERIOUS objections. Those are true, but they
   are identical for every symbol, so counting them as trade objections vetoed
   every trade.

A system that can never approve carries no information -- it is not cautious,
it is inert. The tests below assert both that it *can* approve on ordinary
evidence and that it still refuses when the evidence against is real.
"""

from __future__ import annotations

import pandas as pd
import pytest

from stockagent.agents.base import (AgentReport, AnalysisContext, Decision,
                                    Evidence, Finding, Scope, Severity, Stance,
                                    TradeIdea)
from stockagent.agents.manager import ManagerAgent


def _ctx(**kwargs) -> AnalysisContext:
    frame = pd.DataFrame(
        {"close": [10.0] * 600, "high": [10.1] * 600, "low": [9.9] * 600,
         "volume": [1e6] * 600},
        index=pd.bdate_range("2024-01-01", periods=600),
    )
    defaults = dict(symbol="TEST", asof=frame.index[-1], frame=frame,
                    regime_label="Bull", regime_confidence=0.9,
                    regime_exposure=1.0, regime_probs={"Bull": 0.9})
    defaults.update(kwargs)
    return AnalysisContext(**defaults)


def _idea() -> TradeIdea:
    return TradeIdea(symbol="TEST", direction="long", entry=10.0, stop=9.0,
                     targets=[11.5, 13.0], shares=2.0, risk_amount=2.0,
                     risk_pct_equity=0.01)


def _finding(agent: str, severity: Severity, scope: Scope,
             claim: str = "objection") -> Finding:
    return Finding(agent=agent, claim=claim, severity=severity, confidence=0.7,
                   supports=False, scope=scope,
                   evidence=[Evidence("x", 1, source="computed")])


def _report(agent: str, stance: Stance, confidence: float,
            findings: list[Finding] | None = None) -> AgentReport:
    return AgentReport(agent=agent, stance=stance, confidence=confidence,
                       findings=findings or [], summary="")


def _standard(dissent: list[Finding]) -> list[AgentReport]:
    """A normal debate: regime and discovery constructive, risk clears it."""
    return [
        _report("regime", Stance.BUY, 0.75),
        _report("discovery", Stance.BUY, 0.70),
        _report("risk", Stance.BUY, 0.65),
        _report("devils_advocate", Stance.HOLD, 0.5, dissent),
    ]


def test_approves_on_ordinary_evidence() -> None:
    """The regression: routine dissent must not deadlock the decision."""
    verdict = ManagerAgent().review(
        _ctx(), _standard([_finding("devils_advocate", Severity.CONCERN, Scope.TRADE)]),
        idea=_idea(), final_round=False)
    assert verdict.decision is Decision.APPROVE
    assert verdict.idea is not None


def test_model_caveats_do_not_veto() -> None:
    """Four SERIOUS model-scope findings must not block a trade.

    This is exactly what the live system produced on every symbol: three
    'no state resembles X' warnings plus a restart-spread warning.
    """
    dissent = [
        _finding("devils_advocate", Severity.SERIOUS, Scope.MODEL, "no state resembles Bear"),
        _finding("devils_advocate", Severity.SERIOUS, Scope.MODEL, "no state resembles Bull"),
        _finding("devils_advocate", Severity.SERIOUS, Scope.MODEL, "no state resembles Recovery"),
        _finding("devils_advocate", Severity.SERIOUS, Scope.MODEL, "restarts disagree by 284 nats"),
    ]
    verdict = ManagerAgent().review(_ctx(), _standard(dissent), idea=_idea())
    assert verdict.decision is Decision.APPROVE


def test_model_caveats_ride_along_on_the_approval() -> None:
    """Not vetoing is not the same as hiding. They must reach the human."""
    dissent = [_finding("devils_advocate", Severity.SERIOUS, Scope.MODEL,
                        "restarts disagree by 284 nats")]
    verdict = ManagerAgent().review(_ctx(), _standard(dissent), idea=_idea())
    assert verdict.decision is Decision.APPROVE
    assert any("Standing caveat" in c and "284 nats" in c for c in verdict.conditions)


def test_real_trade_objections_still_reject() -> None:
    """Three SERIOUS trade-scope objections score 6, past the threshold of 5."""
    dissent = [
        _finding("devils_advocate", Severity.SERIOUS, Scope.TRADE, "base rate is 41%"),
        _finding("devils_advocate", Severity.SERIOUS, Scope.TRADE, "OBV divergence"),
        _finding("devils_advocate", Severity.SERIOUS, Scope.TRADE, "28% above the 200-day EMA"),
    ]
    verdict = ManagerAgent().review(_ctx(), _standard(dissent), idea=_idea())
    assert verdict.decision is Decision.REJECT
    assert "objections total 6" in verdict.rationale


def test_threshold_is_the_selectivity_dial() -> None:
    """The same evidence flips with max_objection_score. It is the one knob."""
    dissent = [
        _finding("devils_advocate", Severity.CONCERN, Scope.TRADE, "overbought"),
        _finding("devils_advocate", Severity.CONCERN, Scope.TRADE, "extended"),
        _finding("devils_advocate", Severity.CONCERN, Scope.TRADE, "weak base rate"),
    ]
    assert ManagerAgent(max_objection_score=5).review(
        _ctx(), _standard(dissent), idea=_idea()).decision is Decision.APPROVE
    assert ManagerAgent(max_objection_score=3).review(
        _ctx(), _standard(dissent), idea=_idea()).decision is Decision.REJECT


def test_risk_veto_is_still_absolute() -> None:
    """Loosening the dissent logic must not weaken the Risk agent's veto."""
    reports = _standard([])
    reports[2] = _report("risk", Stance.AVOID, 0.95,
                         [_finding("risk", Severity.BLOCKING, Scope.TRADE,
                                   "Bear regime; long exposure not warranted")])
    verdict = ManagerAgent().review(_ctx(), reports, idea=_idea())
    assert verdict.decision is Decision.REJECT
    assert "blocking objection" in verdict.rationale


def test_genuine_disagreement_between_non_dissenters_still_conflicts() -> None:
    reports = _standard([])
    reports[0] = _report("regime", Stance.AVOID, 0.8)
    verdict = ManagerAgent().review(_ctx(), reports, idea=_idea(), final_round=False)
    assert verdict.decision is Decision.REQUEST_REANALYSIS
    assert "disagreement" in verdict.rationale.lower()


def test_unsupported_claims_still_send_work_back() -> None:
    naked = Finding(agent="discovery", claim="looks strong",
                    severity=Severity.SERIOUS, confidence=0.9, supports=True)
    reports = _standard([])
    reports[1] = _report("discovery", Stance.BUY, 0.7, [naked])
    verdict = ManagerAgent().review(_ctx(), reports, idea=_idea(), final_round=False)
    assert verdict.decision is Decision.REQUEST_REANALYSIS
    assert "no evidence" in verdict.rationale


def test_approved_confidence_is_not_crushed_by_caveats() -> None:
    """An approval reporting 12% confidence is a broken signal, not caution."""
    dissent = [_finding("devils_advocate", Severity.SERIOUS, Scope.MODEL, f"caveat {i}")
               for i in range(8)]
    verdict = ManagerAgent().review(_ctx(), _standard(dissent), idea=_idea())
    assert verdict.decision is Decision.APPROVE
    assert verdict.confidence > 0.4, (
        f"confidence {verdict.confidence:.0%} collapsed under standing caveats")


def test_low_regime_confidence_still_rejects() -> None:
    verdict = ManagerAgent().review(
        _ctx(regime_confidence=0.30), _standard([]), idea=_idea())
    assert verdict.decision is Decision.REJECT
    assert "floor" in verdict.rationale


def test_no_idea_means_no_approval() -> None:
    verdict = ManagerAgent().review(_ctx(), _standard([]), idea=None)
    assert verdict.decision is Decision.REJECT
    assert "not executable" in verdict.rationale
