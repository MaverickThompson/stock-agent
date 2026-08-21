"""Agent 3: Stock Discovery.

Scans the universe and produces a ranked watchlist. Ranking is a weighted blend
of momentum, trend structure, volume behaviour and a penalty for being
stretched, with a hard liquidity gate applied first.

The composite score is a *screen*, not a thesis. It says "these names are worth
the work", and its output is deliberately consumed by three agents whose job is
to attack it. Rank 1 on a momentum screen is also the name most likely to be
extended, which is exactly the Devil's Advocate's opening argument.

On earnings and revenue growth
------------------------------
The spec asks this agent to search for earnings and revenue growth. Fundamental
data is not in the local CSVs, so the agent reads it from
``ctx.live["fundamentals"][symbol]`` when an MCP connector has populated it, and
reports its absence as a limitation when it has not. It does not silently score
zero and pretend the factor was considered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..logging_setup import get_logger
from .base import (Agent, AgentReport, AnalysisContext, Evidence, Finding,
                   Severity, Stance)

LOG = get_logger("agents.discovery")


@dataclass
class Candidate:
    """One ranked name."""

    symbol: str
    score: float
    components: dict[str, float]
    notes: list[str]
    price: float
    dollar_volume: float

    def explain(self) -> str:
        parts = ", ".join(f"{k} {v:+.2f}" for k, v in sorted(
            self.components.items(), key=lambda kv: -abs(kv[1])))
        return f"{self.symbol} score {self.score:+.3f} ({parts})"


#: Weights for the composite score. They sum to 1.0 over the reward terms;
#: `extension` is a penalty and is intentionally negative.
WEIGHTS: dict[str, float] = {
    "momentum": 0.35,
    "trend": 0.30,
    "volume": 0.15,
    "quality": 0.20,
    "extension": -0.25,
}


class DiscoveryAgent(Agent):
    """Ranks the universe and nominates candidates."""

    name = "discovery"
    role = "Scans the universe and maintains a ranked watchlist of opportunities."

    def __init__(self, universe: dict[str, pd.DataFrame] | None = None,
                 *, min_dollar_volume: float = 5_000_000.0,
                 top_n: int = 10) -> None:
        self.universe = universe or {}
        self.min_dollar_volume = min_dollar_volume
        self.top_n = top_n

    # ------------------------------------------------------------------ score

    def score_symbol(self, symbol: str, frame: pd.DataFrame,
                     fundamentals: dict[str, Any] | None = None) -> Candidate | None:
        """Score one symbol. Returns ``None`` if it fails the liquidity gate."""
        if frame.empty:
            return None
        last = frame.iloc[-1]

        def get(col: str, default: float = np.nan) -> float:
            value = last.get(col, default)
            return float(value) if pd.notna(value) else float("nan")

        price = get("close")
        dollar_volume = get("dollar_volume", 0.0)
        notes: list[str] = []

        if not np.isfinite(price) or price <= 0:
            return None
        if np.isfinite(dollar_volume) and dollar_volume < self.min_dollar_volume:
            # Liquidity is a gate, not a score term. A brilliant setup you
            # cannot exit at a sane price is not an opportunity.
            LOG.debug("%s fails liquidity: %.0f < %.0f", symbol,
                      dollar_volume, self.min_dollar_volume)
            return None

        components: dict[str, float] = {}

        # Momentum: blended 3/6/12-month, tanh-squashed so one explosive name
        # cannot dominate the ranking on a single lookback.
        mom = [get(f"mom_{n}") for n in (63, 126, 252)]
        usable = [m for m in mom if np.isfinite(m)]
        components["momentum"] = float(np.tanh(np.mean(usable) * 3.0)) if usable else 0.0
        if not usable:
            notes.append("no momentum history")

        # Trend structure: position relative to the moving-average stack.
        trend = np.nanmean([
            get("above_ema_50", 0.0), get("above_ema_200", 0.0), get("golden_cross", 0.0)
        ])
        components["trend"] = float(trend * 2.0 - 1.0) if np.isfinite(trend) else 0.0

        # Volume: unusual participation, capped. Heavy volume confirms a move;
        # extreme volume is often capitulation, so the term saturates.
        volume_z = get("volume_z", 0.0)
        components["volume"] = float(np.clip(volume_z, -2.0, 2.0) / 2.0) if np.isfinite(volume_z) else 0.0

        # Quality: MACD confirmation plus RSI in the constructive band.
        macd_hist = get("macd_hist", 0.0)
        rsi = get("rsi", 50.0)
        macd_term = float(np.tanh(macd_hist / max(price * 0.01, 1e-9))) if np.isfinite(macd_hist) else 0.0
        # Reward RSI near 55-65; penalise both extremes.
        rsi_term = float(1.0 - abs(rsi - 60.0) / 40.0) if np.isfinite(rsi) else 0.0
        components["quality"] = float(np.clip((macd_term + rsi_term) / 2.0, -1.0, 1.0))

        # Extension penalty: how far above the 200-day EMA, and how overbought.
        dist = get("dist_ema_200", 0.0)
        extension = 0.0
        if np.isfinite(dist):
            extension += float(np.clip(dist / 0.25, 0.0, 1.5))
        if np.isfinite(rsi) and rsi > 70:
            extension += float((rsi - 70.0) / 30.0)
            notes.append(f"RSI {rsi:.0f} is overbought")
        components["extension"] = float(extension)

        # Fundamentals, only when they were actually supplied.
        if fundamentals:
            growth = fundamentals.get("revenue_growth")
            earnings = fundamentals.get("earnings_growth")
            supplied = [v for v in (growth, earnings) if isinstance(v, (int, float))]
            if supplied:
                components["quality"] += float(np.clip(np.mean(supplied) * 2.0, -0.5, 0.5))
                notes.append(f"fundamentals: {', '.join(f'{k}={fundamentals[k]}' for k in ('revenue_growth','earnings_growth') if k in fundamentals)}")
        else:
            notes.append("no fundamental data supplied; earnings/revenue growth not assessed")

        score = sum(WEIGHTS[k] * v for k, v in components.items() if k in WEIGHTS)
        return Candidate(symbol=symbol, score=float(score), components=components,
                         notes=notes, price=price,
                         dollar_volume=float(dollar_volume) if np.isfinite(dollar_volume) else 0.0)

    def rank(self, fundamentals: dict[str, dict[str, Any]] | None = None) -> list[Candidate]:
        """Score and sort the whole universe, best first."""
        fundamentals = fundamentals or {}
        out: list[Candidate] = []
        for symbol, frame in self.universe.items():
            try:
                candidate = self.score_symbol(symbol, frame, fundamentals.get(symbol))
            except (KeyError, ValueError) as exc:
                LOG.warning("could not score %s: %s", symbol, exc)
                continue
            if candidate is not None:
                out.append(candidate)
        return sorted(out, key=lambda c: c.score, reverse=True)

    # ----------------------------------------------------------------- agent

    def analyze(self, ctx: AnalysisContext) -> AgentReport:
        ranked = self.rank((ctx.live or {}).get("fundamentals"))
        findings: list[Finding] = []

        if not ranked:
            return self._report(
                Stance.ABSTAIN, 0.0,
                [self._finding(
                    "No symbol in the universe passed the liquidity gate",
                    severity=Severity.SERIOUS, confidence=0.9, supports=False,
                    evidence=[Evidence("universe_size", len(self.universe), source="prices"),
                              Evidence("min_dollar_volume", self.min_dollar_volume,
                                       "20-day median", "config")])],
                "Nothing to rank.")

        top = ranked[:self.top_n]
        findings.append(self._finding(
            f"Ranked {len(ranked)} names; leader is {top[0].symbol} ({top[0].score:+.3f})",
            severity=Severity.INFO, confidence=0.6, supports=None,
            evidence=[Evidence("watchlist", [c.symbol for c in top], "best first", "computed"),
                      Evidence("scores", {c.symbol: round(c.score, 3) for c in top},
                               "composite of momentum/trend/volume/quality less extension",
                               "computed")],
        ))

        subject = next((c for c in ranked if c.symbol == ctx.symbol), None)
        if subject is None:
            findings.append(self._finding(
                f"{ctx.symbol} is not rankable -- it failed the liquidity gate or has "
                "insufficient history",
                severity=Severity.SERIOUS, confidence=0.8, supports=False,
                evidence=[Evidence("symbol", ctx.symbol, source="prices"),
                          Evidence("min_dollar_volume", self.min_dollar_volume, source="config")],
            ))
            return self._report(Stance.ABSTAIN, 0.2, findings,
                                f"{ctx.symbol} could not be scored.")

        rank_index = ranked.index(subject) + 1
        percentile = 1.0 - (rank_index - 1) / max(len(ranked), 1)
        findings.append(self._finding(
            f"{ctx.symbol} ranks {rank_index} of {len(ranked)} ({percentile:.0%} percentile)",
            severity=Severity.INFO, confidence=0.65,
            supports=percentile >= 0.5,
            evidence=[
                Evidence("rank", rank_index, f"of {len(ranked)}", "computed"),
                Evidence("score", round(subject.score, 4), source="computed"),
                Evidence("components", {k: round(v, 3) for k, v in subject.components.items()},
                         "weighted into the composite", "computed"),
                Evidence("dollar_volume", round(subject.dollar_volume), "20-day median", "prices"),
            ],
        ))

        for note in subject.notes:
            findings.append(self._finding(
                f"{ctx.symbol}: {note}",
                severity=Severity.CONCERN if "overbought" in note or "no " in note else Severity.INFO,
                confidence=0.55, supports=False if "overbought" in note else None,
                evidence=[Evidence("note", note, source="computed")],
            ))

        if subject.score > 0.15 and percentile >= 0.6:
            stance, confidence = Stance.BUY, min(0.85, 0.4 + subject.score)
        elif subject.score < -0.15:
            stance, confidence = Stance.AVOID, min(0.8, 0.4 + abs(subject.score))
        else:
            stance, confidence = Stance.HOLD, 0.45

        return self._report(
            stance, confidence, findings,
            f"{ctx.symbol} ranks {rank_index}/{len(ranked)}, score {subject.score:+.3f}. "
            f"Watchlist: {', '.join(c.symbol for c in top[:5])}.")
