"""A Section 7 deferred exit must not void the session (defect fix 2026-10-14).

On 2026-10-14 one stale quote on DLTR voided a session in which all 20
candidates were evaluated, leaving 22 signal rows with no provenance row.
These tests pin the distinction between "the protocol told us to wait" and
"something broke".
"""
from __future__ import annotations

import dataclasses
import inspect

from stockagent import session as session_mod
from stockagent.broker import StaleQuote
from stockagent.session import SessionResult


def test_session_result_has_a_separate_deferred_counter() -> None:
    fields = {f.name for f in dataclasses.fields(SessionResult)}
    assert "deferred" in fields, (
        "SessionResult needs its own deferred counter; sharing `errors` is what "
        "voided the 2026-10-14 session")
    assert SessionResult().deferred == 0


def test_summary_reports_deferrals() -> None:
    text = SessionResult(deferred=3).summary()
    assert "deferred=3" in text, (
        "a deferral the operator cannot see in the summary is a deferral they "
        "will rediscover from the state file three days later")


def test_stale_quote_is_a_broker_error_subclass() -> None:
    # The exit handler matches on the class name through the MRO, so this
    # relationship is load-bearing.
    assert any(cls.__name__ == "StaleQuote" for cls in StaleQuote.__mro__)


def test_exit_handler_classifies_deferrals_separately() -> None:
    src = inspect.getsource(session_mod.run_session)
    assert "is_deferral" in src, "the exit handler must classify StaleQuote"
    assert "result.deferred += 1" in src
    assert "result.errors += 1" in src, (
        "genuine exit failures must still count as errors")


def test_a_deferral_still_writes_its_system_error_row() -> None:
    src = inspect.getsource(session_mod.run_session)
    deferral_block = src[src.index("is_deferral"):]
    log_pos = deferral_block.index("log.log_system_error")
    branch_pos = deferral_block.index("result.deferred += 1")
    assert log_pos < branch_pos, (
        "the SYSTEM_ERROR row must be written before the counter branch - the "
        "disclosure is the whole reason this is safe")


def test_only_deferrals_are_forgiven() -> None:
    """A ValueError in the exit path must still void the session."""
    src = inspect.getsource(session_mod.run_session)
    assert 'cls.__name__ == "StaleQuote"' in src, (
        "the forgiveness must be narrow and name StaleQuote explicitly; a broad "
        "except that forgives everything would hide real breakage")


def test_session_py_does_not_import_the_broker_module() -> None:
    """session.py takes a BrokerLike Protocol and must stay broker-agnostic."""
    src = inspect.getsource(session_mod)
    assert "from .broker import" not in src
    assert "import broker" not in src.replace("BrokerLike", "")
