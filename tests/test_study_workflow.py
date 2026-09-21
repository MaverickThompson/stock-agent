"""Regression coverage for the unattended study workflow."""

from __future__ import annotations

import pathlib


WORKFLOW = (
    pathlib.Path(__file__).parent.parent / ".github" / "workflows" / "study-session.yml"
)


def test_session_fetches_price_history_before_running() -> None:
    """A clean Actions checkout must bootstrap the ignored ``data/`` directory."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    fetch = workflow.index("python scripts/fetch_data.py")
    session = workflow.index("python scripts/run_session.py")

    assert fetch < session
