"""The live overlay: current quotes, news and events from MCP connectors.

Why this is a file and not an API client
----------------------------------------
The MCP connectors (Alpha Vantage, Longbridge, FMP, Bigdata.com) are tools
available to the *agent session*, not HTTP endpoints this package can call. So
the boundary is drawn explicitly:

    agent session  --(MCP connectors)-->  data/live_snapshot.json
    Python         --(this module)---->   ctx.live

An agent with connector access refreshes the snapshot; the analysis code reads
it. That keeps the compute layer offline, deterministic and testable — a
backtest over 5,000 bars must never depend on a network call — while still
letting live information reach the agents.

The cost is staleness, which is why :func:`load_live_snapshot` checks the
timestamp and *drops* fields that have aged out rather than passing them
through. A stale VIX quoted as current is worse than no VIX at all.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

from .logging_setup import get_logger

LOG = get_logger("live")

#: Fields older than this (hours) are dropped rather than served stale.
MAX_AGE_HOURS = 36.0


def load_live_snapshot(path: pathlib.Path | str, *,
                       max_age_hours: float = MAX_AGE_HOURS,
                       now: dt.datetime | None = None) -> dict[str, Any]:
    """Load and freshness-check the connector snapshot.

    Returns ``{}`` when the file is missing or unreadable -- the live overlay is
    strictly optional, and every agent handles its absence. Returns a dict with
    a ``stale`` warning when the data is too old to rely on.
    """
    path = pathlib.Path(path)
    if not path.exists():
        LOG.debug("no live snapshot at %s; running on price history alone", path)
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("could not read live snapshot %s: %s", path, exc)
        return {}

    asof_raw = payload.get("asof")
    if not asof_raw:
        LOG.warning("live snapshot has no 'asof'; treating it as unusable")
        return {}

    try:
        asof = dt.datetime.fromisoformat(str(asof_raw).replace("Z", "+00:00"))
    except ValueError:
        LOG.warning("live snapshot has an unparseable 'asof': %r", asof_raw)
        return {}
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=dt.timezone.utc)

    now = now or dt.datetime.now(dt.timezone.utc)
    age_hours = (now - asof).total_seconds() / 3600.0

    if age_hours > max_age_hours:
        LOG.warning("live snapshot is %.1fh old (limit %.0fh); dropping quotes and "
                    "sentiment, keeping only scheduled events",
                    age_hours, max_age_hours)
        # Scheduled events survive staleness because a known future earnings
        # date does not expire the way a quote does.
        return {
            "stale": True,
            "age_hours": round(age_hours, 1),
            "news_blackout": payload.get("news_blackout"),
            "upcoming_events": payload.get("upcoming_events", []),
        }

    payload["stale"] = False
    payload["age_hours"] = round(age_hours, 2)
    LOG.info("live snapshot loaded, %.1fh old, sources: %s",
             age_hours, ", ".join(payload.get("sources", {})) or "unspecified")
    return payload


def summarize(live: dict[str, Any]) -> list[str]:
    """Human-readable lines describing the overlay, for CLI output."""
    if not live:
        return ["no live overlay; analysis is on price history only"]

    out: list[str] = []
    if live.get("stale"):
        out.append(f"STALE by {live.get('age_hours', '?')}h -- quotes dropped")
    else:
        out.append(f"live data {live.get('age_hours', '?')}h old")

    for symbol, quote in (live.get("quotes") or {}).items():
        change = quote.get("change_pct")
        out.append(f"{symbol}: {quote.get('price')}"
                   + (f" ({change:+.2%})" if isinstance(change, (int, float)) else ""))

    if (vix := live.get("vix_level")) is not None:
        percentile = live.get("vix_percentile")
        out.append(f"VIX {vix}"
                   + (f" ({percentile:.0%} of 3y range)" if percentile is not None else ""))

    sentiment = live.get("news_sentiment") or {}
    if sentiment.get("mean_score") is not None:
        out.append(f"news sentiment {sentiment['mean_score']:+.3f} "
                   f"over {sentiment.get('article_count', '?')} articles")

    if blackout := live.get("news_blackout"):
        out.append(f"NEWS BLACKOUT ACTIVE: {blackout}")
    return out
