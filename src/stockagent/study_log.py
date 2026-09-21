"""Frozen study logs for the 60-day live paper-trading study.

The two CSV schemas in this module are declared FROZEN in Section 10 of
PROTOCOL.md and must never change. Nothing here may be "improved" -- a column
added, renamed or reordered mid-study invalidates the record.

Append-only, and why a trade appears on more than one row
--------------------------------------------------------
Section 10: "Append-only. Corrections are new rows with a note referencing the
original. No row is ever edited or deleted."

``trades.csv`` carries both entry and exit fields on one row, which cannot be
filled in at a single moment: the exit is unknown at entry, and the protocol
forbids going back to edit the row later. So a position writes:

  1. an ENTRY row at fill time, with the exit fields empty. ``thesis_at_entry``
     and ``invalidation_condition`` are populated here, before any outcome is
     known -- Section 10 calls this "the point of the exercise".
  2. a further row at each exit, complete, sharing the same
     ``entry_timestamp`` and ``ticker``.

Target 1 closes 50% of the position and Target 2 closes the remainder
(Section 5), so two exit rows for one entry is the normal case, not an error.
Analysis pairs rows on (entry_timestamp, ticker) and reads exits in order. No
row is ever rewritten.

Every write is flushed and fsynced. A session that dies mid-write must not lose
the row that explains why.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import pathlib
from typing import Any, Final, Sequence

try:
    from .logging_setup import get_logger
except ImportError:  # pragma: no cover - allows standalone import in tests
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)

LOG = get_logger("study_log")

# ---------------------------------------------------------------------------
# FROZEN SCHEMAS -- Section 10 of PROTOCOL.md. Do not edit.
# ---------------------------------------------------------------------------

SIGNALS_COLUMNS: Final[tuple[str, ...]] = (
    "timestamp",
    "ticker",
    "signal_type",
    "triggered_rule",
    "action_taken",
    "reason_if_rejected",
    "price_at_signal",
    "notes",
)

TRADES_COLUMNS: Final[tuple[str, ...]] = (
    "entry_timestamp",
    "ticker",
    "direction",
    "entry_price",
    "size",
    "thesis_at_entry",
    "invalidation_condition",
    "exit_timestamp",
    "exit_price",
    "pnl",
    "pnl_pct",
    "exit_reason",
    "was_thesis_correct",
)

#: Section 10: failed scans, connector outages and missed sessions use this.
SYSTEM_ERROR: Final[str] = "SYSTEM_ERROR"

#: The closed set of action_taken values. ENTERED and REJECTED cover the
#: ordinary path; SYSTEM_ERROR is mandated by Section 10; SKIPPED records a
#: candidate the session never reached, which is not the same as a rejection.
ACTIONS: Final[frozenset[str]] = frozenset(
    {"ENTERED", "REJECTED", "EXITED", "SKIPPED", SYSTEM_ERROR}
)


def utc_now_iso() -> str:
    """Timestamps are UTC, second precision, always suffixed Z.

    Local time would make the record ambiguous across the DST change that falls
    inside any 60-day autumn window.
    """
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value: Any) -> str:
    """Render a value for CSV without ever emitting a newline.

    A newline inside a quoted CSV field is legal but makes the file painful to
    diff, grep and eyeball -- and this file is evidence, so it must stay
    readable by hand.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return " ".join(str(value).split())


class StudyLog:
    """Append-only writer for the two frozen study files."""

    def __init__(self, directory: pathlib.Path | str) -> None:
        self.directory = pathlib.Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.signals_path = self.directory / "signals.csv"
        self.trades_path = self.directory / "trades.csv"
        self._ensure_header(self.signals_path, SIGNALS_COLUMNS)
        self._ensure_header(self.trades_path, TRADES_COLUMNS)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _ensure_header(path: pathlib.Path, columns: Sequence[str]) -> None:
        """Create the file with its header, or verify an existing one.

        A mismatched header means the file was written by different code than
        the protocol froze. That is not recoverable by guessing, so it raises.
        """
        if not path.exists() or path.stat().st_size == 0:
            with path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(columns)
                handle.flush()
                os.fsync(handle.fileno())
            return

        with path.open("r", newline="", encoding="utf-8") as handle:
            existing = next(csv.reader(handle), [])
        if tuple(existing) != tuple(columns):
            raise ValueError(
                f"{path.name} header does not match the frozen Section 10 schema.\n"
                f"  found:    {existing}\n"
                f"  expected: {list(columns)}\n"
                "Refusing to append. The schemas are frozen; investigate rather "
                "than deleting the file."
            )

    def _append(self, path: pathlib.Path, columns: Sequence[str],
                row: dict[str, Any]) -> None:
        unknown = set(row) - set(columns)
        if unknown:
            raise ValueError(
                f"not in the frozen schema for {path.name}: {sorted(unknown)}")
        with path.open("a", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow([_clean(row.get(c)) for c in columns])
            handle.flush()
            os.fsync(handle.fileno())

    # -- signals ------------------------------------------------------------

    def log_signal(self, *, ticker: str, signal_type: str, triggered_rule: str,
                   action_taken: str, price_at_signal: float | None = None,
                   reason_if_rejected: str = "", notes: str = "",
                   timestamp: str | None = None) -> None:
        """Append one signals.csv row.

        Section 10: EVERY signal is logged, including rejected ones and errors.
        """
        if action_taken not in ACTIONS:
            raise ValueError(
                f"action_taken must be one of {sorted(ACTIONS)}, got {action_taken!r}")
        if action_taken == "REJECTED" and not reason_if_rejected:
            raise ValueError("a REJECTED signal must record reason_if_rejected")
        self._append(self.signals_path, SIGNALS_COLUMNS, {
            "timestamp": timestamp or utc_now_iso(),
            "ticker": ticker,
            "signal_type": signal_type,
            "triggered_rule": triggered_rule,
            "action_taken": action_taken,
            "reason_if_rejected": reason_if_rejected,
            "price_at_signal": price_at_signal,
            "notes": notes,
        })

    def log_system_error(self, *, stage: str, detail: str, ticker: str = "-") -> None:
        """Record a failed scan, connector outage or missed session.

        Mandated by Section 10. Silence here is the failure mode that makes the
        study unfalsifiable -- a gap nobody can distinguish from a day that
        genuinely produced no signals.
        """
        self.log_signal(ticker=ticker, signal_type="system", triggered_rule=stage,
                        action_taken=SYSTEM_ERROR, reason_if_rejected=detail,
                        notes="logged per Section 10")

    # -- trades -------------------------------------------------------------

    def open_trade(self, *, entry_timestamp: str, ticker: str, direction: str,
                   entry_price: float, size: int, thesis_at_entry: str,
                   invalidation_condition: str) -> None:
        """Append the ENTRY row. Exit fields stay empty by design."""
        if not thesis_at_entry.strip():
            raise ValueError("thesis_at_entry is required before entry (Section 5)")
        if not invalidation_condition.strip():
            raise ValueError("invalidation_condition is required before entry (Section 5)")
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")
        self._append(self.trades_path, TRADES_COLUMNS, {
            "entry_timestamp": entry_timestamp,
            "ticker": ticker,
            "direction": direction,
            "entry_price": entry_price,
            "size": size,
            "thesis_at_entry": thesis_at_entry,
            "invalidation_condition": invalidation_condition,
        })

    def close_trade(self, *, entry_timestamp: str, ticker: str, direction: str,
                    entry_price: float, size: int, thesis_at_entry: str,
                    invalidation_condition: str, exit_timestamp: str,
                    exit_price: float, exit_reason: str,
                    was_thesis_correct: bool | None = None) -> dict[str, float]:
        """Append a complete row for an exit, pairing on (entry_timestamp, ticker).

        ``size`` is the quantity leaving on THIS exit, so a Target 1 partial
        writes half and the later exit writes the remainder. P/L is computed
        here rather than passed in, so the arithmetic lives in one place.
        """
        sign = 1.0 if direction.lower() in ("long", "buy") else -1.0
        pnl = (exit_price - entry_price) * size * sign
        pnl_pct = ((exit_price - entry_price) / entry_price) * sign if entry_price else 0.0
        self._append(self.trades_path, TRADES_COLUMNS, {
            "entry_timestamp": entry_timestamp,
            "ticker": ticker,
            "direction": direction,
            "entry_price": entry_price,
            "size": size,
            "thesis_at_entry": thesis_at_entry,
            "invalidation_condition": invalidation_condition,
            "exit_timestamp": exit_timestamp,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "was_thesis_correct": was_thesis_correct,
        })
        return {"pnl": pnl, "pnl_pct": pnl_pct}

    # -- reading ------------------------------------------------------------

    def read(self, which: str) -> list[dict[str, str]]:
        path = self.signals_path if which == "signals" else self.trades_path
        with path.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def row_counts(self) -> dict[str, int]:
        """Row counts excluding headers -- what the dry run is judged on."""
        return {"signals": len(self.read("signals")),
                "trades": len(self.read("trades"))}


def dry_run_passed(counts: dict[str, int]) -> bool:
    """Section 10: 'A dry run that produces no rows does not count as passing.'"""
    return counts.get("signals", 0) > 0
