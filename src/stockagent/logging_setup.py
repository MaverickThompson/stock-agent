"""Logging: a human-readable console/file log plus a machine-readable audit log.

Two streams, because they answer different questions.

``configure_logging`` sets up the narrative log -- what the system did, in
order, for a human reading a terminal or tailing a file.

:class:`DecisionLog` appends one JSON object per line to ``logs/decisions.jsonl``.
That is the audit trail: every agent finding, every rebuttal, and every Manager
verdict, with the evidence attached. It exists so that months later you can ask
"why did the system say that on 2026-03-14" and get an answer that is not a
reconstruction from memory.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import logging.handlers
import pathlib
import uuid
from typing import Any

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(funcName)s:%(lineno)d %(message)s"


def configure_logging(log_dir: pathlib.Path | str, *, level: int = logging.INFO,
                      console: bool = True) -> logging.Logger:
    """Configure the ``stockagent`` logger tree. Safe to call more than once."""
    log_dir = pathlib.Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("stockagent")
    logger.setLevel(level)
    logger.handlers.clear()
    # Handlers are attached here, not on the root logger, so importing this
    # package never hijacks logging for an application that embeds it.
    logger.propagate = False

    if console:
        stream = logging.StreamHandler()
        stream.setLevel(level)
        stream.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
        logger.addHandler(stream)

    rotating = logging.handlers.RotatingFileHandler(
        log_dir / "stockagent.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    rotating.setLevel(logging.DEBUG)
    rotating.setFormatter(logging.Formatter(_FILE_FORMAT))
    logger.addHandler(rotating)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Child logger under the ``stockagent`` tree."""
    return logging.getLogger(f"stockagent.{name}")


class _Encoder(json.JSONEncoder):
    """Serialises the value types that show up in agent findings."""

    def default(self, o: Any) -> Any:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if isinstance(o, (dt.datetime, dt.date)):
            return o.isoformat()
        if isinstance(o, pathlib.Path):
            return str(o)
        if isinstance(o, set):
            return sorted(o)
        # numpy scalars/arrays, pandas Timestamps -- all expose one of these.
        for attr in ("item", "tolist", "isoformat"):
            method = getattr(o, attr, None)
            if callable(method):
                try:
                    return method()
                except Exception:  # noqa: BLE001 - fall through to repr
                    pass
        return repr(o)


class DecisionLog:
    """Append-only JSONL audit trail.

    Each :meth:`record` call writes exactly one line and flushes, so a crash
    mid-run still leaves every decision made up to that point on disk.
    """

    def __init__(self, path: pathlib.Path | str, *, run_id: str | None = None) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._log = get_logger("decision_log")

    def record(self, event: str, **payload: Any) -> dict[str, Any]:
        """Append one event. Returns the record that was written."""
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "event": event,
            **payload,
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, cls=_Encoder, default=str) + "\n")
                handle.flush()
        except OSError as exc:
            # An unwritable audit log must never take down the analysis, but it
            # must be loud -- a silent audit gap is worse than no audit at all.
            self._log.error("could not write audit record %r: %s", event, exc)
        return record

    def read_all(self) -> list[dict[str, Any]]:
        """Read the whole trail back, skipping any corrupted lines."""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                self._log.warning("skipping malformed audit line %d", i)
        return out
