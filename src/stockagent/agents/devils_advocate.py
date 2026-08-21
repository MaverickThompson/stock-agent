"""Agent 5: Devil's Advocate.

Argues against whatever the others concluded. Its findings are almost always
``supports=False`` -- that is the design, not a bug. Its value is not balance;
it is coverage of the failure modes nobody else is incentivised to look for.

The most useful thing it produces is the **falsification trigger**: a specific,
observable condition that would prove the thesis wrong. A thesis that cannot be
falsified cannot be evaluated, and a position with no falsification condition is
one you will still be holding, and rationalising, at -40%.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import (Agent, AgentReport, AnalysisContext, Evidence, Finding,
                   Scope, Severity, Stance)


class DevilsAdvocateAgent(Agent):
    """Attacks the emerging consensus and states what would disprove it."""

    name = "devils_advocate"
    role = "Argues against every conclusion and defines what would falsify the thesis."

    def analyze(self, ctx: AnalysisContext) -> AgentReport:
        findings: list[Finding] = []
        price = ctx.value("close")
        rsi = ctx.value("rsi")
        atr = ctx.value("atr")

        # --- Extension: the screen's favourite is the most stretched name ----
        dist_200 = ctx.value("dist_ema_200")
        if np.isfinite(dist_200) and dist_200 > 0.15:
            findings.append(self._finding(
                f"Price is {dist_200:.1%} above its 200-day EMA. Buying here is buying "
                "after the move; mean reversion is a real force at this distance",
                severity=Severity.SERIOUS if dist_200 > 0.25 else Severity.CONCERN,
                confidence=min(0.85, 0.4 + dist_200), supports=False,
                evidence=[Evidence("dist_ema_200", round(dist_200, 4), source="indicators"),
                          Evidence("close", round(price, 4), source="prices")],
            ))

        if np.isfinite(rsi) and rsi > 70:
            findings.append(self._finding(
                f"RSI {rsi:.0f} is overbought. Entries here take the worst of the "
                "short-term distribution",
                severity=Severity.CONCERN, confidence=(rsi - 70) / 30 + 0.5, supports=False,
                evidence=[Evidence("rsi", round(rsi, 1), "14-period Wilder", "indicators")],
            ))

        # --- Divergence: price and participation disagreeing -----------------
        divergence = self._divergence(ctx)
        if divergence:
            findings.append(divergence)

        # --- Regime fragility ------------------------------------------------
        confidence = float(ctx.regime_confidence)
        if confidence < 0.7:
            runner_up = sorted(ctx.regime_probs.items(), key=lambda kv: -kv[1])[1:2]
            findings.append(self._finding(
                f"The regime call is only {confidence:.0%} confident"
                + (f"; {runner_up[0][0]} still carries {runner_up[0][1]:.0%}" if runner_up else "")
                + ". Every downstream conclusion inherits that uncertainty",
                severity=Severity.CONCERN, confidence=1.0 - confidence, supports=False,
                evidence=[Evidence("regime_posterior", {k: round(v, 3) for k, v in ctx.regime_probs.items()},
                                   source="hmm")],
            ))

        # --- The model's own blind spots -------------------------------------
        # Model-scope. These are real and they belong in the record, but they
        # are identical for every symbol this model analyses. Scoped as MODEL
        # so they inform the decision without silently vetoing every trade.
        for warning in (ctx.regime_diagnostics or {}).get("model_warnings", []) or []:
            if "no state resembles" in warning:
                findings.append(self._finding(
                    f"The model is structurally blind here: {warning}",
                    severity=Severity.SERIOUS, confidence=0.8, supports=False,
                    scope=Scope.MODEL,
                    evidence=[Evidence("model_warning", warning, source="hmm")],
                ))

        restart_spread = (ctx.regime_diagnostics or {}).get("restart_spread")
        if restart_spread is not None and float(restart_spread) > 100:
            findings.append(self._finding(
                f"Baum-Welch restarts disagree by {float(restart_spread):.0f} nats. The "
                "fit is one of several very different local optima, so the regime map "
                "is not a stable object",
                severity=Severity.SERIOUS, confidence=0.7, supports=False,
                scope=Scope.MODEL,
                evidence=[Evidence("restart_spread", round(float(restart_spread), 1),
                                   "range of restart log-likelihoods", "hmm")],
            ))

        # --- Base rate -------------------------------------------------------
        base_rate = self._base_rate(ctx)
        if base_rate:
            findings.append(base_rate)

        # --- Drawdown history ------------------------------------------------
        drawdown = ctx.value("drawdown")
        if np.isfinite(drawdown) and drawdown < -0.10:
            findings.append(self._finding(
                f"The symbol is {abs(drawdown):.1%} below its running peak. Falling "
                "knives look like discounts right up until they do not",
                severity=Severity.CONCERN, confidence=0.6, supports=False,
                evidence=[Evidence("drawdown", round(drawdown, 4),
                                   "from expanding-window peak", "indicators")],
            ))

        # --- Sample size -----------------------------------------------------
        if len(ctx.frame) < 500:
            findings.append(self._finding(
                f"Only {len(ctx.frame)} bars of history. Every statistic quoted in this "
                "debate has error bars wider than anyone is acknowledging",
                severity=Severity.CONCERN, confidence=0.7, supports=False,
                scope=Scope.DATA,
                evidence=[Evidence("bars", len(ctx.frame), source="prices")],
            ))

        # --- The falsification trigger ---------------------------------------
        findings.append(self._falsification(ctx, price, atr))

        # Stance is driven by objections to *this trade*. Standing model and
        # data caveats are reported but do not by themselves push to AVOID --
        # otherwise this agent votes AVOID on every symbol forever and its
        # dissent stops carrying information.
        trade = [f for f in findings if f.is_trade_objection]
        serious = [f for f in trade if f.severity >= Severity.SERIOUS]
        caveats = [f for f in findings
                   if f.supports is False and f.scope is not Scope.TRADE]
        score = sum(2 if f.severity >= Severity.SERIOUS else 1 for f in trade)

        if score >= 5:
            stance, stance_conf = Stance.AVOID, min(0.9, 0.5 + 0.08 * score)
        elif score >= 3:
            stance, stance_conf = Stance.HOLD, 0.65
        else:
            stance, stance_conf = Stance.HOLD, 0.45

        return self._report(
            stance, stance_conf, findings,
            f"{len(trade)} objections to this trade ({len(serious)} serious, "
            f"score {score}), {len(caveats)} standing caveats. "
            "Falsification condition stated.")

    def _divergence(self, ctx: AnalysisContext) -> Finding | None:
        """Price making highs while volume or momentum does not confirm."""
        frame = ctx.frame
        if len(frame) < 60 or "obv" not in frame.columns:
            return None
        window = frame.tail(60)
        price_change = window["close"].iloc[-1] / window["close"].iloc[0] - 1.0
        obv_start, obv_end = window["obv"].iloc[0], window["obv"].iloc[-1]
        if not np.isfinite(obv_start) or not np.isfinite(obv_end):
            return None
        scale = max(abs(float(obv_start)), 1.0)
        obv_change = (float(obv_end) - float(obv_start)) / scale

        if price_change > 0.03 and obv_change < -0.02:
            return self._finding(
                f"Price is up {price_change:.1%} over 60 days while OBV fell "
                f"{abs(obv_change):.1%}. The advance is not being confirmed by volume",
                severity=Severity.SERIOUS, confidence=0.65, supports=False,
                evidence=[Evidence("price_change_60d", round(price_change, 4), source="prices"),
                          Evidence("obv_change_60d", round(obv_change, 4),
                                   "normalised by starting OBV", "indicators")],
            )
        return None

    def _base_rate(self, ctx: AnalysisContext) -> Finding | None:
        """What actually happened, historically, after conditions like today's.

        Conditioned on RSI band and trend side. This is a crude conditional
        frequency over overlapping windows, not a backtest -- overlapping
        20-day forward returns are heavily autocorrelated, so the hit rate is
        far less precise than the sample count suggests. It is reported anyway
        because a bad base rate beats no base rate, provided the weakness is
        stated rather than buried.
        """
        frame = ctx.frame
        if len(frame) < 400 or "rsi" not in frame.columns:
            return None
        rsi_now = ctx.value("rsi")
        above = ctx.value("above_ema_200")
        if not np.isfinite(rsi_now) or not np.isfinite(above):
            return None

        horizon = 20
        forward = frame["close"].shift(-horizon) / frame["close"] - 1.0
        similar = (
            (frame["rsi"] - rsi_now).abs() < 7.5
        ) & (frame["above_ema_200"] == above)
        sample = forward[similar].dropna()
        if len(sample) < 40:
            return None

        hit_rate = float((sample > 0).mean())
        median = float(sample.median())
        severity = Severity.SERIOUS if hit_rate < 0.45 else (
            Severity.CONCERN if hit_rate < 0.55 else Severity.INFO)
        return self._finding(
            f"Base rate: in {len(sample)} historically similar setups (RSI ~{rsi_now:.0f}, "
            f"{'above' if above else 'below'} the 200-day EMA), the next {horizon} days "
            f"were positive {hit_rate:.0%} of the time, median {median:+.2%}",
            severity=severity, confidence=0.55,
            supports=hit_rate >= 0.55,
            evidence=[
                Evidence("sample_size", len(sample), "overlapping windows", "computed"),
                Evidence("hit_rate", round(hit_rate, 3), f"{horizon}-day forward", "computed"),
                Evidence("median_return", round(median, 4), source="computed"),
                Evidence("caveat", "overlapping windows: effective sample is much smaller "
                                   "than the count suggests", source="computed"),
            ],
        )

    def _falsification(self, ctx: AnalysisContext, price: float,
                       atr: float) -> Finding:
        """State the specific condition that would prove the thesis wrong."""
        conditions: list[str] = []
        if np.isfinite(price) and np.isfinite(atr) and atr > 0:
            conditions.append(f"a close below {price - 2.0 * atr:.2f} (2x ATR under entry)")
        ema50 = ctx.value("ema_50")
        if np.isfinite(ema50):
            conditions.append(f"a close below the 50-day EMA at {ema50:.2f}")
        if ctx.regime_label in ("Bull", "Recovery"):
            conditions.append("the HMM posterior for Bull/Recovery dropping under 40%")
        conditions.append("VIX closing above 30")

        trigger = "; OR ".join(conditions)
        return self._finding(
            f"Falsification trigger -- the thesis is wrong if any of these occur: {trigger}",
            severity=Severity.INFO, confidence=0.8, supports=None,
            evidence=[
                Evidence("falsification_conditions", conditions,
                         "pre-committed exit criteria, defined before entry", "computed"),
                Evidence("atr", round(atr, 4) if np.isfinite(atr) else None, source="indicators"),
            ],
        )

    def rebut(self, ctx: AnalysisContext, peers: list[AgentReport]) -> AgentReport:
        """Attack the consensus specifically, including unsupported claims.

        The Manager can reject findings that arrive without evidence; this agent
        makes sure they are named first.
        """
        report = self.analyze(ctx)
        others = [p for p in peers if p.agent != self.name]

        unsupported = [(p.agent, f) for p in others for f in p.unsupported]
        if unsupported:
            report.revised = True
            report.findings.append(self._finding(
                f"{len(unsupported)} claims in this debate carry no evidence and should "
                "not count toward consensus",
                severity=Severity.SERIOUS, confidence=0.8, supports=False,
                evidence=[Evidence("unsupported_claim", f.claim[:110], f"from {agent}", "debate")
                          for agent, f in unsupported[:5]],
            ))

        buyers = [p for p in others if p.stance is Stance.BUY]
        if len(buyers) >= 2 and all(p.confidence > 0.6 for p in buyers):
            report.revised = True
            report.findings.append(self._finding(
                f"{len(buyers)} agents agree at high confidence with no dissent among "
                "them. Unanimity on a noisy signal is a warning sign about the process, "
                "not a confirmation of the conclusion",
                severity=Severity.CONCERN, confidence=0.6, supports=False,
                evidence=[Evidence("agreeing_agents",
                                   {p.agent: round(p.confidence, 3) for p in buyers},
                                   source="debate")],
            ))
        return report
