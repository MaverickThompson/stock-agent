"""Durable progress state for the V2 live-session study."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import pathlib
import tempfile
from typing import Literal

V2_START_DATE = dt.date(2026, 9, 29)
V2_SESSION_TARGET = 60
_SCHEMA_VERSION = 1
_STATE_FIELDS = {
    "schema_version",
    "start_date",
    "completed_sessions",
    "last_session_date",
    "status",
}


def _parse_date(value: object, field_name: str) -> dt.date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date string")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date string") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must be an ISO date string")
    return parsed


@dataclasses.dataclass(frozen=True)
class StudyState:
    schema_version: int
    start_date: dt.date
    completed_sessions: int
    last_session_date: dt.date | None
    status: Literal["active", "complete"]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {_SCHEMA_VERSION}")
        if type(self.start_date) is not dt.date:
            raise ValueError("start_date must be a date")
        if self.start_date != V2_START_DATE:
            raise ValueError(f"start_date must be {V2_START_DATE.isoformat()}")
        if (type(self.completed_sessions) is not int
                or not 0 <= self.completed_sessions <= V2_SESSION_TARGET):
            raise ValueError(
                f"completed_sessions must be between 0 and {V2_SESSION_TARGET}")
        if self.last_session_date is not None:
            if type(self.last_session_date) is not dt.date:
                raise ValueError("last_session_date must be a date or null")
            if self.last_session_date < self.start_date:
                raise ValueError("last_session_date cannot precede start_date")
            available_dates = (self.last_session_date - self.start_date).days + 1
            if self.completed_sessions > available_dates:
                raise ValueError(
                    "completed_sessions cannot exceed the dates through "
                    "last_session_date")
        if self.status not in ("active", "complete"):
            raise ValueError("status must be 'active' or 'complete'")
        if (self.completed_sessions == 0) != (self.last_session_date is None):
            raise ValueError("zero progress must have no last_session_date")
        expected_status = (
            "complete" if self.completed_sessions == V2_SESSION_TARGET else "active")
        if self.status != expected_status:
            raise ValueError("status must agree with completed_sessions")

    @classmethod
    def load(cls, path: pathlib.Path | str) -> StudyState:
        with pathlib.Path(path).open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        if not isinstance(document, dict) or set(document) != _STATE_FIELDS:
            raise ValueError("state JSON must contain exactly the supported fields")
        start_date = _parse_date(document["start_date"], "start_date")
        last_value = document["last_session_date"]
        last_session_date = (
            None if last_value is None
            else _parse_date(last_value, "last_session_date")
        )
        return cls(
            schema_version=document["schema_version"],
            start_date=start_date,
            completed_sessions=document["completed_sessions"],
            last_session_date=last_session_date,
            status=document["status"],
        )

    def is_eligible(self, today: dt.date) -> bool:
        if type(today) is not dt.date:
            raise TypeError("today must be a date")
        return (
            self.status == "active"
            and today >= self.start_date
            and (self.last_session_date is None or today > self.last_session_date)
        )

    def record_success(self, today: dt.date) -> StudyState:
        if not self.is_eligible(today):
            return self
        completed_sessions = self.completed_sessions + 1
        return StudyState(
            schema_version=self.schema_version,
            start_date=self.start_date,
            completed_sessions=completed_sessions,
            last_session_date=today,
            status=(
                "complete"
                if completed_sessions == V2_SESSION_TARGET
                else "active"
            ),
        )

    def save(self, path: pathlib.Path | str) -> None:
        destination = pathlib.Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": self.schema_version,
            "start_date": self.start_date.isoformat(),
            "completed_sessions": self.completed_sessions,
            "last_session_date": (
                None if self.last_session_date is None
                else self.last_session_date.isoformat()
            ),
            "status": self.status,
        }
        temporary_path: pathlib.Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = pathlib.Path(handle.name)
                json.dump(document, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
