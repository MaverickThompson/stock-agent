"""Agent 4: Risk.

Sizes the trade and then tries to talk the system out of it. The asymmetry is
intentional -- this agent is rewarded for finding reasons not to trade, and it
holds a veto. A missed opportunity costs nothing but regret; a preventable loss
costs capital, and capital is the thing that lets you keep playing.

Checks, in the order they bind:

1. **Liquidity** -- can the position be exited at a sane price?
2. **Sizing** -- does 1%-of-equity risk buy a whole share?
3. **Heat** -- would this breach the portfolio-wide open-risk cap?
4. **Stop quality** -- is the stop far enough to survive normal noise?
5. **Reward-to-risk** -- does the first target justify the risk taken?
6. **Regime** -- does the market environment support risk-taking at all?
7. **Correlation** -- is this the same bet already on the book, twice?
8. **Drawdown** -- what does the worst case do to the account?
9. **News blackout** -- is a scheduled event about to make price random?
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import RiskConfig
from ..portfolio import Portfolio
from .base import (Agent, AgentReport, AnalysisContext, Evidence, Finding,
                   Scope, Severity, Stance, TradeIdea)


class RiskAgent(Agent):
    """Challenges every trade and can block it outright."""

    name = "risk"
    role = "Sizes positions, enforces capital-preservation limits, and vetoes dangerous trades."

    def __init__(self, portfolio: Portfolio, cfg: RiskConfig | None = None) -> None:
        self.portfolio = portfolio
        self.cfg = cfg or portfolio.cfg
        #: Populated by :meth:`analyze` so the Manager can inspect the sized idea.
        self.last_idea: TradeIdea | None = None

    def analyze(self, ctx: AnalysisContext) -> AgentReport:
        # Clear first: a sized idea left over from a previous symbol or debate
        # round must never survive into a decision where sizing failed.
        self.last_idea = None
        findings: list[Finding] = []
        price = ctx.value("close")
        atr = ctx.value("atr")
        atr_pct = ctx.value("atr_pct")
        equity = float(ctx.portfolio.get("equity", self.portfolio.equity))

        if not np.isfinite(price) or price <= 0:
            return self._report(
                Stance.AVOID, 1.0,
                [self._finding("No usable price; cannot size or risk-manage a position",
                               severity=Severity.BLOCKING, confidence=1.0, supports=False,
                               evidence=[Evidence("close", price, source="prices")])],
                "Blocked: no price.")

        # --- 1. Liquidity ----------------------------------------------------
        dollar_volume = ctx.value("dollar_volume", 0.0)
        if np.isfinite(dollar_volume) and 0 < dollar_volume < self.cfg.min_dollar_volume:
            findings.append(self._finding(
                f"20-day median dollar volume {dollar_volume:,.0f} is below the "
                f"{self.cfg.min_dollar_volume:,.0f} floor; exits will cost more than "
                "the edge is worth",
                severity=Severity.BLOCKING, confidence=0.9, supports=False,
                evidence=[Evidence("dollar_volume", round(dollar_volume), "20d median", "prices"),
                          Evidence("floor", self.cfg.min_dollar_volume, source="config")],
            ))

        # --- 2/3. Sizing and heat -------------------------------------------
        sizing = self.portfolio.size(price, atr, equity=equity)
        stop, targets = sizing.stop, sizing.targets

        if not sizing.is_tradeable:
            findings.append(self._finding(
                f"Position cannot be sized: {sizing.rejected_reason}",
                severity=Severity.BLOCKING, confidence=0.95, supports=False,
                evidence=[
                    Evidence("equity", round(equity, 2), source="portfolio"),
                    Evidence("risk_budget", round(equity * self.cfg.max_risk_per_trade, 2),
                             f"{self.cfg.max_risk_per_trade:.1%} of equity", "config"),
                    Evidence("risk_per_share", round(abs(price - stop), 4),
                             f"{self.cfg.atr_stop_multiple}x ATR", "computed"),
                    Evidence("reason", sizing.rejected_reason, source="portfolio"),
                ],
            ))
        else:
            idea = TradeIdea(
                symbol=ctx.symbol, direction="long", entry=price, stop=stop,
                targets=targets, shares=sizing.shares,
                risk_amount=sizing.risk_amount,
                risk_pct_equity=sizing.risk_pct_equity,
                rationale=f"ATR({self.cfg.atr_stop_multiple}x) stop, fixed-fractional sizing",
            )
            self.last_idea = idea
            problems = idea.validate()
            findings.append(self._finding(
                f"Sized {idea.quantity_str()} shares at {price:.2f} "
                f"(position value {idea.position_value:.2f}), stop {stop:.2f}, "
                f"risking {idea.risk_amount:.2f} ({idea.risk_pct_equity:.2%} of equity)",
                severity=Severity.BLOCKING if problems else Severity.INFO,
                confidence=0.9, supports=None if problems else True,
                evidence=[
                    Evidence("shares", idea.shares, source="computed"),
                    Evidence("entry", round(price, 4), source="prices"),
                    Evidence("stop", round(stop, 4),
                             f"{self.cfg.atr_stop_multiple}x ATR below entry", "computed"),
                    Evidence("targets", [round(t, 4) for t in targets],
                             f"R multiples {self.cfg.target_r_multiples}", "computed"),
                    Evidence("risk_pct_equity", round(idea.risk_pct_equity, 5), source="computed"),
                ] + ([Evidence("problems", problems, source="computed")] if problems else []),
            ))

            projected_heat = self.portfolio.heat + idea.risk_amount / max(equity, 1e-9)
            if projected_heat > self.cfg.max_portfolio_heat:
                findings.append(self._finding(
                    f"Taking this trade lifts portfolio heat to {projected_heat:.2%}, "
                    f"past the {self.cfg.max_portfolio_heat:.2%} cap",
                    severity=Severity.BLOCKING, confidence=0.95, supports=False,
                    evidence=[Evidence("current_heat", round(self.portfolio.heat, 5), source="portfolio"),
                              Evidence("projected_heat", round(projected_heat, 5), source="computed"),
                              Evidence("cap", self.cfg.max_portfolio_heat, source="config")],
                ))

            # --- 5. Reward-to-risk -------------------------------------------
            # Tolerance, because targets are rounded to 4dp when they are
            # built. Without it a target placed at exactly 1.5R comes back as
            # 1.4999... and the agent objects that "1.50 is below 1.50".
            if idea.reward_risk < self.cfg.min_reward_risk - 1e-6:
                findings.append(self._finding(
                    f"Reward-to-risk {idea.reward_risk:.2f} is below the required "
                    f"{self.cfg.min_reward_risk:.2f}",
                    severity=Severity.SERIOUS, confidence=0.85, supports=False,
                    evidence=[Evidence("reward_risk", round(idea.reward_risk, 3),
                                       "to first target", "computed"),
                              Evidence("minimum", self.cfg.min_reward_risk, source="config")],
                ))

            # --- 8. Drawdown exposure ----------------------------------------
            all_stops_hit = self.portfolio.open_risk + idea.risk_amount
            findings.append(self._finding(
                f"If every stop fills, the account loses {all_stops_hit:.2f} "
                f"({all_stops_hit / max(equity, 1e-9):.2%} of equity)",
                severity=Severity.INFO, confidence=0.9, supports=None,
                evidence=[Evidence("worst_case_loss", round(all_stops_hit, 2),
                                   "sum of open risk including this trade", "computed"),
                          Evidence("positions_after", len(self.portfolio.positions) + 1,
                                   f"max {self.cfg.max_open_positions}", "portfolio")],
            ))

        # --- 4. Stop quality -------------------------------------------------
        if np.isfinite(atr_pct):
            if atr_pct > 0.06:
                findings.append(self._finding(
                    f"ATR is {atr_pct:.1%} of price; a {self.cfg.atr_stop_multiple}x stop "
                    f"sits {atr_pct * self.cfg.atr_stop_multiple:.1%} away and the "
                    "position must be tiny to respect the risk cap",
                    severity=Severity.CONCERN, confidence=0.7, supports=False,
                    evidence=[Evidence("atr_pct", round(atr_pct, 4), "ATR / price", "indicators")],
                ))
            elif atr_pct < 0.005:
                findings.append(self._finding(
                    f"ATR is only {atr_pct:.2%} of price; the stop sits inside normal "
                    "noise and will be taken out by a routine wiggle",
                    severity=Severity.CONCERN, confidence=0.7, supports=False,
                    evidence=[Evidence("atr_pct", round(atr_pct, 5), source="indicators")],
                ))

        # --- 6. Regime -------------------------------------------------------
        regime = ctx.regime_label
        if regime in ("Bear", "HighVolatility") and ctx.regime_confidence >= 0.5:
            findings.append(self._finding(
                f"{regime} regime at {ctx.regime_confidence:.0%} confidence. Long "
                "exposure is not warranted; capital preservation takes precedence",
                severity=Severity.BLOCKING, confidence=ctx.regime_confidence, supports=False,
                evidence=[Evidence("regime", regime, source="hmm"),
                          Evidence("confidence", round(ctx.regime_confidence, 4), source="hmm"),
                          Evidence("regime_exposure", round(ctx.regime_exposure, 3),
                                   "volatility-targeted baseline", "hmm")],
            ))
        elif regime == "Recovery":
            findings.append(self._finding(
                "Recovery regime: direction is right but volatility is still elevated. "
                "Half size at most",
                severity=Severity.CONCERN, confidence=0.7, supports=False,
                evidence=[Evidence("regime", regime, source="hmm"),
                          Evidence("regime_exposure", round(ctx.regime_exposure, 3), source="hmm")],
            ))

        # --- 7. Correlation --------------------------------------------------
        correlated = self._correlation_check(ctx)
        if correlated:
            findings.append(correlated)

        # --- 9. News blackout ------------------------------------------------
        blackout = (ctx.live or {}).get("news_blackout")
        if blackout:
            findings.append(self._finding(
                f"Scheduled event inside the blackout window: {blackout}. Price is "
                "about to be driven by news, not by the setup",
                severity=Severity.BLOCKING, confidence=0.85, supports=False,
                evidence=[Evidence("event", blackout, source="live"),
                          Evidence("blackout_days", self.cfg.news_blackout_days, source="config")],
            ))

        # --- Data quality is a risk too --------------------------------------
        for warning in ctx.data_warnings or []:
            findings.append(self._finding(
                f"Data quality: {warning}",
                severity=Severity.CONCERN, confidence=0.6, supports=False,
                scope=Scope.DATA,
                evidence=[Evidence("warning", warning, source="prices")],
            ))

        blocking = [f for f in findings if f.severity is Severity.BLOCKING]
        objections = [f for f in findings if f.supports is False]
        if blocking:
            stance, confidence = Stance.AVOID, max(f.confidence for f in blocking)
            summary = f"BLOCKED: {blocking[0].claim}"
        elif len(objections) >= 2:
            stance, confidence = Stance.HOLD, 0.6
            summary = f"{len(objections)} unresolved risk objections; not comfortable."
        else:
            stance, confidence = Stance.BUY, 0.6
            summary = (f"Risk acceptable: {self.last_idea.quantity_str()} shares "
                       f"({self.last_idea.position_value:.2f}), "
                       f"{self.last_idea.risk_pct_equity:.2%} of equity at risk."
                       if self.last_idea else "Risk acceptable.")
        return self._report(stance, confidence, findings, summary)

    def _correlation_check(self, ctx: AnalysisContext) -> Finding | None:
        """Flag when the candidate moves with something already held.

        Ten uncorrelated 1% risks is a 1% expected worst day. Ten copies of the
        same 1% risk is a 10% day, and correlations converge toward 1 in exactly
        the selloffs the stops are meant to protect against.
        """
        held = ctx.portfolio.get("positions") or {}
        returns_by_symbol = (ctx.live or {}).get("returns_by_symbol") or {}
        if not held or ctx.symbol not in returns_by_symbol:
            return None

        subject = pd.Series(returns_by_symbol[ctx.symbol]).tail(126)
        worst_symbol, worst_corr = None, 0.0
        for symbol in held:
            if symbol == ctx.symbol or symbol not in returns_by_symbol:
                continue
            other = pd.Series(returns_by_symbol[symbol]).tail(126)
            joined = pd.concat([subject, other], axis=1).dropna()
            if len(joined) < 60:
                continue
            corr = float(joined.corr().iloc[0, 1])
            if np.isfinite(corr) and abs(corr) > abs(worst_corr):
                worst_symbol, worst_corr = symbol, corr

        if worst_symbol is None or abs(worst_corr) < 0.7:
            return None
        return self._finding(
            f"{ctx.symbol} is {worst_corr:.2f} correlated with {worst_symbol}, already "
            "held. This adds concentration, not diversification",
            severity=Severity.SERIOUS if abs(worst_corr) > 0.85 else Severity.CONCERN,
            confidence=abs(worst_corr), supports=False,
            evidence=[Evidence("correlation", round(worst_corr, 3),
                               f"126-day, vs {worst_symbol}", "computed"),
                      Evidence("open_positions", list(held), source="portfolio")],
        )

    def rebut(self, ctx: AnalysisContext, peers: list[AgentReport]) -> AgentReport:
        """Re-examine, and harden if peers are more bullish than the risk supports.

        Enthusiasm from other agents is not evidence. If Discovery and Regime
        both want the trade while this agent has open objections, that is the
        moment to state them more forcefully, not to soften.
        """
        report = self.analyze(ctx)
        bullish_peers = [p for p in peers
                         if p.agent != self.name and p.stance is Stance.BUY
                         and p.confidence > 0.6]
        objections = report.objections()
        if bullish_peers and objections and not report.blocking:
            report.revised = True
            report.confidence = min(1.0, report.confidence + 0.1)
            report.findings.append(self._finding(
                f"{len(bullish_peers)} agents want this trade at high confidence while "
                f"{len(objections)} risk objections stand unanswered. Enthusiasm is not "
                "evidence; the objections must be addressed on their merits",
                severity=Severity.SERIOUS, confidence=0.75, supports=False,
                evidence=[Evidence("bullish_agents", [p.agent for p in bullish_peers], source="debate"),
                          Evidence("open_objections", [f.claim[:100] for f in objections[:4]],
                                   source="debate")],
            ))
            report.summary += " Hardened against peer enthusiasm."
        return report
