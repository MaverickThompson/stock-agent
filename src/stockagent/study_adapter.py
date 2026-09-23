"""Bridge from the existing analysis stack to the study session.

``DiscoveryAgent`` and ``analyze_symbol`` were written for interactive use and
produce their own shapes: a discovery ``Candidate`` with a score, and a
``DebateResult`` whose ``verdict.idea`` is a ``TradeIdea``. The session needs
:class:`~stockagent.session.Candidate` and :class:`~stockagent.session.Thesis`.
This module translates, and every place the two do not line up is written down
here rather than buried.

Four mappings were fixed BEFORE day 1 and belong in Section 12
--------------------------------------------------------------
1. **Predicted probability = ``verdict.confidence``.** Section 5 asks for a
   predicted probability of reaching Target 1; the debate produces a manager
   confidence. They are not necessarily the same quantity. Mapping fixed in
   advance, on the record.

2. **Entry zone = entry +/- 10% of the risk distance.** ``TradeIdea`` gives a
   single entry price; Section 5 requires a zone and a fill inside it. The
   band is scaled to the trade's own risk rather than a flat percentage, so it
   means the same thing on a $20 stock and a $600 one. It also directly
   protects the R:R gate: paying more than a tenth of your risk above the plan
   erodes the 2:1 the trade was approved on.

3. **Falsification = the manager's conditions when present**, otherwise a
   daily close through the stop. Section 5 requires a falsification condition
   distinct from the stop; the debate exposes ``verdict.conditions``, which is
   the nearest thing the stack produces.

4. **An unknown earnings position fails the gate.** If the calendar could not
   be consulted, the candidate is rejected rather than entered. Section 5's
   condition is "no earnings within 48 hours", and "we could not check" is not
   a way of satisfying it.
"""

from __future__ import annotations

import pathlib
from typing import Any

from . import earnings as earnings_mod
from .session import Candidate, Thesis

try:
    from .logging_setup import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)

LOG = get_logger("study_adapter")

#: Mapping 2 above. Fraction of (entry - stop) tolerated either side of entry.
ENTRY_ZONE_RISK_FRACTION: float = 0.10


def entry_zone_for(entry: float, stop: float,
                   fraction: float = ENTRY_ZONE_RISK_FRACTION) -> tuple[float, float]:
    """Zone scaled to the trade's own risk, not a flat percentage."""
    band = abs(entry - stop) * fraction
    return (entry - band, entry + band)


def falsification_for(conditions: list[str] | None, stop: float) -> str:
    """The manager's conditions if it gave any, else a close through the stop."""
    if conditions:
        joined = "; ".join(c.strip() for c in conditions if c and c.strip())
        if joined:
            return joined
    return f"daily close below {stop:.2f}"


# PROTOCOL Section 3 restricts the universe to "US-listed common equities".
# fetch_data.py's DEFAULT_SYMBOLS also carries two context series that are NOT
# eligible instruments and must never reach layer 1:
#
#   VIX  -> ^VIX, an index. No volume, not tradeable.
#   BTC  -> BTC-USD, a crypto pair. Worse, the local file name "BTC" collides
#           with a real NYSE ticker, so broker.quote("BTC") returns a ~$38 ETF
#           while the stop was derived from $81,000 crypto bars. That mismatch
#           produced a live REJECTED row on 2026-09-22:
#             "stop 81841.20 is not below entry ask 38.32"
#           A rejection is the lucky outcome; the same collision could size a
#           position against the wrong instrument entirely.
#
# Excluded here rather than in fetch_data.py so the series stay available as
# market context to layer 2 without being tradeable candidates.
NON_EQUITY_SYMBOLS: frozenset[str] = frozenset({"VIX", "BTC"})


def candidates_for_session(cfg: Any = None, *, top_n: int = 20) -> list[Candidate]:
    """Layer 1: rank the universe and hand back the survivors."""
    excluded = NON_EQUITY_SYMBOLS
    from .agents.discovery_agent import DiscoveryAgent
    from .config import Config
    from .data_io import load_universe
    from .indicators import add_all_indicators

    cfg = cfg or Config()
    universe = load_universe(cfg.data.data_dir, cfg.data.universe or None,
                             min_bars=cfg.data.min_bars)
    scored = {sym: add_all_indicators(df) for sym, df in universe.items()}
    ranked = DiscoveryAgent(scored, min_dollar_volume=cfg.risk.min_dollar_volume,
                            top_n=top_n).rank()
    eligible = [c for c in ranked if c.symbol not in excluded]
    dropped = len(ranked) - len(eligible)
    if dropped:
        LOG.info("dropped %d non-equity symbol(s) per Section 3: %s", dropped,
                 sorted({c.symbol for c in ranked} & excluded))
    LOG.info("layer 1 produced %d eligible candidates from %d ranked",
             len(eligible), len(ranked))
    return [Candidate(symbol=c.symbol, score=float(c.score)) for c in eligible[:top_n]]


def make_thesis_source(cfg: Any = None, *, equity: float = 100_000.0,
                       cache_dir: pathlib.Path | str = "study"):
    """Return a ``thesis_for(candidate)`` callable for the session.

    The earnings calendar is loaded once and closed over, so a 20-symbol scan
    makes one upstream request rather than twenty.
    """
    from .config import Config
    from .pipeline import analyze_symbol

    cfg = cfg or Config()
    calendar = earnings_mod.EarningsCalendar.load(cache_dir)

    def thesis_for(candidate: Candidate) -> Thesis | None:
        result, _engine = analyze_symbol(candidate.symbol, cfg, equity=equity)
        verdict = result.verdict

        if not verdict.approved or verdict.idea is None:
            LOG.info("%s: layer 2 did not approve", candidate.symbol)
            return None

        idea = verdict.idea
        targets = list(idea.targets or [])
        if len(targets) < 2:
            LOG.info("%s: idea has %d targets, Section 5 needs two",
                     candidate.symbol, len(targets))
            return None

        hours = calendar.hours_until(candidate.symbol)
        if earnings_mod.is_unknown(hours):
            # Mapping 4: unverifiable is not the same as clear. Handing the
            # gate a tiny number makes it reject with an honest reason.
            hours = 0.0

        return Thesis(
            entry_zone=entry_zone_for(float(idea.entry), float(idea.stop)),
            stop=float(idea.stop),
            target_1=float(targets[0]),
            target_2=float(targets[1]),
            predicted_probability=float(verdict.confidence),
            falsification=falsification_for(verdict.conditions, float(idea.stop)),
            layer_2_favoured=True,
            rationale=(idea.rationale or verdict.rationale or "")[:400],
            hours_to_earnings=hours,
        )

    return thesis_for


#: The entry point expects this name; wired lazily so importing the module
#: does not fit an HMM.
def thesis_for(candidate: Candidate) -> Thesis | None:  # pragma: no cover
    global _SOURCE
    try:
        _SOURCE
    except NameError:
        _SOURCE = make_thesis_source()
    return _SOURCE(candidate)
