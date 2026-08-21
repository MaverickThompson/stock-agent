"""Agent 2: Market Regime.

Reads the HMM output computed in :mod:`stockagent.regime` -- filtered (causal)
posteriors from the Forward algorithm, the Viterbi path, and Baum-Welch fit
diagnostics -- and turns it into a position on market conditions.

Its second job is to audit its own model. A regime call is only worth acting on
if the state is persistent, the posterior is concentrated, and an independent
read of the tape agrees. So the agent cross-checks the HMM against a plain
EMA-200 trend rule and reports the disagreement as a finding rather than hiding
it. When a 200-day moving average and a five-state hidden Markov model disagree
about whether this is a bull market, that is information, and the Manager should
see it.
"""

from __future__ import annotations

from typing import Any

from .base import (Agent, AgentReport, AnalysisContext, Evidence, Finding,
                   Scope, Severity, Stance)


class RegimeAgent(Agent):
    """Determines the market regime and how much to trust that determination."""

    name = "regime"
    role = "Identifies the prevailing market regime via HMM and reports its reliability."

    def __init__(self, *, min_confidence: float = 0.55,
                 min_duration: float = 5.0) -> None:
        self.min_confidence = min_confidence
        self.min_duration = min_duration

    def analyze(self, ctx: AnalysisContext) -> AgentReport:
        findings: list[Finding] = []
        label = ctx.regime_label
        confidence = float(ctx.regime_confidence)
        diagnostics: dict[str, Any] = ctx.regime_diagnostics or {}

        probs = {k: round(v, 4) for k, v in sorted(
            ctx.regime_probs.items(), key=lambda kv: -kv[1])}
        duration = float(diagnostics.get("expected_duration", 0.0))
        exposure = float(ctx.regime_exposure)

        findings.append(self._finding(
            f"Current regime is {label} with posterior {confidence:.0%}",
            severity=Severity.INFO,
            confidence=confidence,
            supports=None,
            evidence=[
                Evidence("filtered_posterior", probs, "P(state | data up to today), forward algorithm", "hmm"),
                Evidence("expected_duration_days", round(duration, 1),
                         "model-implied persistence, 1/(1-a_ii)", "hmm"),
                Evidence("suggested_exposure", round(exposure, 3),
                         "volatility-targeted baseline for this regime", "hmm"),
            ],
        ))

        # --- Is the call strong enough to act on? ---------------------------
        if confidence < self.min_confidence:
            findings.append(self._finding(
                f"Regime posterior {confidence:.0%} is below the {self.min_confidence:.0%} "
                "threshold; the model is not committing to a state",
                severity=Severity.SERIOUS, confidence=1.0 - confidence, supports=False,
                evidence=[Evidence("posterior", round(confidence, 4),
                                   f"threshold {self.min_confidence}", "hmm"),
                          Evidence("distribution", probs, "mass is spread across states", "hmm")],
            ))

        if 0 < duration < self.min_duration:
            findings.append(self._finding(
                f"{label} persists only ~{duration:.1f} bars; too short-lived to "
                "position around",
                severity=Severity.CONCERN, confidence=0.7, supports=False,
                evidence=[Evidence("expected_duration_days", round(duration, 1),
                                   f"minimum useful {self.min_duration}", "hmm")],
            ))

        # --- Transition risk -------------------------------------------------
        leave_prob = float(diagnostics.get("transition_out_prob", 0.0))
        if leave_prob > 0.15:
            findings.append(self._finding(
                f"{leave_prob:.0%} chance of leaving {label} on any given day",
                severity=Severity.CONCERN, confidence=leave_prob, supports=False,
                evidence=[Evidence("p_transition_out", round(leave_prob, 4),
                                   "row of the transition matrix, off-diagonal sum", "hmm")],
            ))

        # --- Independent trend cross-check -----------------------------------
        close = ctx.value("close")
        ema200 = ctx.value("ema_200")
        if close == close and ema200 == ema200:  # both non-NaN
            trend_up = close > ema200
            hmm_bullish = label in ("Bull", "Recovery")
            gap = (close - ema200) / ema200
            if trend_up != hmm_bullish:
                findings.append(self._finding(
                    f"HMM says {label} but price is {'above' if trend_up else 'below'} "
                    f"its 200-day EMA by {abs(gap):.1%}; the two disagree",
                    severity=Severity.SERIOUS, confidence=0.65, supports=False,
                    evidence=[
                        Evidence("close", round(close, 4), source="prices"),
                        Evidence("ema_200", round(ema200, 4), source="indicators"),
                        Evidence("gap_pct", round(gap, 4),
                                 "positive means price above trend", "indicators"),
                        Evidence("hmm_regime", label, source="hmm"),
                    ],
                ))
            else:
                findings.append(self._finding(
                    f"200-day EMA agrees with the {label} call "
                    f"(price {gap:+.1%} vs trend)",
                    severity=Severity.INFO, confidence=0.7, supports=hmm_bullish,
                    evidence=[Evidence("gap_pct", round(gap, 4), source="indicators"),
                              Evidence("hmm_regime", label, source="hmm")],
                ))

        # --- External stress check -------------------------------------------
        vix = ctx.live.get("vix_level") or ctx.regime_diagnostics.get("vix_level")
        vix_pct = ctx.regime_diagnostics.get("vix_percentile")
        if vix is not None:
            severity = Severity.SERIOUS if float(vix) >= 30 else (
                Severity.CONCERN if float(vix) >= 22 else Severity.INFO)
            stressed = float(vix) >= 22
            findings.append(self._finding(
                f"VIX at {float(vix):.1f}"
                + (f" ({vix_pct:.0%} of its 3-year range)" if vix_pct is not None else "")
                + (" indicates market stress" if stressed else " is unremarkable"),
                severity=severity, confidence=0.75, supports=None if not stressed else False,
                evidence=[Evidence("vix", round(float(vix), 2), source="prices")]
                + ([Evidence("vix_percentile", round(float(vix_pct), 3),
                             "rank within trailing 756 bars", "prices")] if vix_pct is not None else []),
            ))

        # --- Surface model warnings as findings, not footnotes ----------------
        for warning in diagnostics.get("model_warnings", []) or []:
            findings.append(self._finding(
                f"Model diagnostic: {warning}",
                severity=Severity.CONCERN, confidence=0.6, supports=False,
                scope=Scope.MODEL,
                evidence=[Evidence("diagnostic", warning, source="hmm")],
            ))

        stance, stance_conf = self._stance(label, confidence, exposure)
        summary = (
            f"{label} at {confidence:.0%} posterior, ~{duration:.0f} bar persistence, "
            f"baseline exposure {exposure:.0%}."
        )
        return self._report(stance, stance_conf, findings, summary)

    def _stance(self, label: str, confidence: float,
                exposure: float) -> tuple[Stance, float]:
        """Map the regime onto a position, scaled by how sure we are."""
        if confidence < self.min_confidence:
            return Stance.ABSTAIN, confidence
        if label in ("Bull", "Recovery"):
            return Stance.BUY, confidence * (0.6 + 0.4 * exposure)
        if label == "Bear":
            return Stance.AVOID, confidence
        if label == "HighVolatility":
            return Stance.AVOID, confidence * 0.9
        return Stance.HOLD, confidence

    def rebut(self, ctx: AnalysisContext, peers: list[AgentReport]) -> AgentReport:
        """Hold the regime call, but concede when peers find a contradiction.

        The regime is measured, not argued, so peer opinion does not change it.
        What peers *can* change is our confidence: if two independent agents
        both report evidence that contradicts the regime, that is a reason to
        widen the error bars on the model, not to ignore them.
        """
        report = self.analyze(ctx)
        contradictions = [
            f for p in peers if p.agent != self.name
            for f in p.findings
            if f.supports is False and f.severity >= Severity.SERIOUS
            and any(term in f.claim.lower()
                    for term in ("regime", "trend", "volatility", "stress", "drawdown"))
        ]
        if len(contradictions) >= 2:
            before = report.confidence
            report.confidence = round(before * 0.75, 4)
            report.revised = True
            report.findings.append(self._finding(
                f"{len(contradictions)} peer findings contradict the regime read; "
                f"lowering confidence {before:.0%} -> {report.confidence:.0%}",
                severity=Severity.CONCERN, confidence=0.6, supports=False,
                evidence=[Evidence("peer_objection", f.claim[:120], f"from {f.agent}", "debate")
                          for f in contradictions[:4]],
            ))
            report.summary += f" Revised down after {len(contradictions)} peer contradictions."
        return report
