"""Scheduled earnings dates, for the Section 5 48-hour blackout.

Section 5 requires "No scheduled earnings release within 48 hours of entry".
Nothing else in this package knows earnings dates and Alpaca does not serve
them, so this module exists solely to make that gate evaluable.

Source is Alpha Vantage's EARNINGS_CALENDAR endpoint, which returns CSV for
the next 3 months across the whole market in a single request. It is fetched
once per session and cached on disk, so the per-symbol check costs nothing and
a rate limit cannot stall a session mid-scan.

**If the calendar cannot be fetched, this module does not guess.**
:meth:`EarningsCalendar.hours_until` returns ``UNKNOWN`` rather than ``None``,
and the caller must treat that as a failed gate. Returning "no earnings
scheduled" on a failed fetch would silently convert an unverifiable condition
into a passed one, which is the exact failure this study is about.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import pathlib
from typing import Final

try:
    from .logging_setup import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)

LOG = get_logger("earnings")

ENDPOINT: Final[str] = "https://www.alphavantage.co/query"

#: Sentinel meaning "the calendar could not be consulted", distinct from None
#: which would mean "checked, and nothing is scheduled".
UNKNOWN: Final[float] = float("nan")

#: A cached calendar older than this is refetched.
CACHE_MAX_AGE_HOURS: Final[float] = 24.0


def is_unknown(value: float | None) -> bool:
    """True when the earnings position could not be determined."""
    return value is not None and value != value  # NaN is the only self-unequal float


class EarningsCalendar:
    """Upcoming reporting dates, keyed by symbol."""

    def __init__(self, dates: dict[str, dt.date] | None = None,
                 *, available: bool = True) -> None:
        self._dates = dates or {}
        self.available = available

    # -- construction -------------------------------------------------------

    @classmethod
    def unavailable(cls, reason: str) -> "EarningsCalendar":
        LOG.warning("earnings calendar unavailable: %s", reason)
        return cls({}, available=False)

    @classmethod
    def from_csv(cls, text: str) -> "EarningsCalendar":
        """Parse Alpha Vantage's CSV. Columns: symbol,name,reportDate,..."""
        dates: dict[str, dt.date] = {}
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            symbol = (row.get("symbol") or "").strip().upper()
            raw = (row.get("reportDate") or "").strip()
            if not symbol or not raw:
                continue
            try:
                when = dt.date.fromisoformat(raw)
            except ValueError:
                continue
            # Keep the soonest date if a symbol appears more than once.
            if symbol not in dates or when < dates[symbol]:
                dates[symbol] = when
        if not dates:
            return cls.unavailable("calendar parsed but contained no usable rows")
        LOG.info("earnings calendar loaded: %d symbols", len(dates))
        return cls(dates)

    @classmethod
    def load(cls, cache_dir: pathlib.Path | str,
             *, api_key: str | None = None,
             now: dt.datetime | None = None) -> "EarningsCalendar":
        """Cached fetch. Falls back to a stale cache before giving up entirely."""
        cache = pathlib.Path(cache_dir) / "earnings_calendar.csv"
        now = now or dt.datetime.now(dt.timezone.utc)

        if cache.exists():
            age_h = (now.timestamp() - cache.stat().st_mtime) / 3600.0
            if age_h < CACHE_MAX_AGE_HOURS:
                return cls.from_csv(cache.read_text(encoding="utf-8"))

        api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY")
        if not api_key:
            if cache.exists():
                LOG.warning("no ALPHAVANTAGE_API_KEY; using the stale cached calendar")
                return cls.from_csv(cache.read_text(encoding="utf-8"))
            return cls.unavailable("ALPHAVANTAGE_API_KEY is not set")

        try:
            import requests
            response = requests.get(
                ENDPOINT,
                params={"function": "EARNINGS_CALENDAR", "horizon": "3month",
                        "apikey": api_key},
                timeout=30)
            response.raise_for_status()
            text = response.text
            if "symbol" not in text.split("\n", 1)[0].lower():
                # Alpha Vantage returns a JSON note instead of CSV when rate limited.
                raise ValueError(f"unexpected response: {text[:200]}")
        except Exception as exc:  # noqa: BLE001
            if cache.exists():
                LOG.warning("earnings fetch failed (%s); using the stale cache", exc)
                return cls.from_csv(cache.read_text(encoding="utf-8"))
            return cls.unavailable(f"fetch failed: {exc}")

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        return cls.from_csv(text)

    # -- the gate -----------------------------------------------------------

    def hours_until(self, symbol: str, *,
                    now: dt.datetime | None = None) -> float | None:
        """Hours until the next report.

        ``None``  -- checked, nothing scheduled in the horizon. Gate passes.
        ``UNKNOWN`` (NaN) -- could not be checked. The caller must fail the gate.
        """
        if not self.available:
            return UNKNOWN
        when = self._dates.get(symbol.strip().upper())
        if when is None:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        # Alpha Vantage gives a date, not a time. Assume the open of that day,
        # which is the conservative reading: it makes the blackout start earlier.
        moment = dt.datetime.combine(when, dt.time(13, 30), tzinfo=dt.timezone.utc)
        return (moment - now).total_seconds() / 3600.0
