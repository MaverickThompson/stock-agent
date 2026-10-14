"""Section 11 defect correction, 2026-10-14: a deferred exit is not a session error.

THE DEFECT
----------
``run_session.py`` advances study state only when ``result.errors == 0``.
Every failure path in ``session.py`` increments the same counter, so a single
stale quote on a single open position voids the whole session -- even when all
candidates were evaluated and logged correctly.

That is what happened on 2026-10-14. DLTR's quote came back stale, the exit
was deferred, and the run was marked failed. 22 signal rows were written for a
day with no ``sessions.csv`` provenance row and no counter increment.

WHY IT IS A DEFECT AND NOT A DESIGN CHOICE
------------------------------------------
PROTOCOL Section 7 states that a stale quote means the fill is *deferred to the
next regular session*. Deferring is the prescribed, correct behaviour. The
system followed the protocol and then refused to count the day for having done
so. The result is an audit hole (orphan signal rows) and a standing re-run
hazard: with state unadvanced, only the signals guard stops a second session.

WHAT CHANGES
------------
``SessionResult`` gains a ``deferred`` counter. A StaleQuote raised while
exiting a position increments ``deferred`` instead of ``errors``. The
SYSTEM_ERROR row is still written, so the disclosure is unchanged.

WHAT DOES NOT CHANGE
--------------------
No strategy parameter. MAX_QUOTE_AGE_SECONDS, the R:R floor, the cash reserve
and every gate are untouched. The exit still defers to the next session, so no
trade happens that would not have happened before. Genuine failures -- config
conformance mismatch, adapter failure, broker init failure, any unexpected
exception -- still void the session exactly as they do today.

This is bookkeeping, not strategy. It is not motivated by results and produces
no additional trades.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SESSION = ROOT / "src" / "stockagent" / "session.py"
MSG = ROOT / ".git" / "deferred-commit-msg.txt"

MARK = "deferred"


def patch_session() -> list[str]:
    text = SESSION.read_text(encoding="utf-8")
    notes: list[str] = []

    old_field = ("    rejected: int = 0\n"
                 "    errors: int = 0\n")
    new_field = ("    rejected: int = 0\n"
                 "    errors: int = 0\n"
                 "    #: Exits the protocol told us to postpone (Section 7 stale quote).\n"
                 "    #: Logged as SYSTEM_ERROR but NOT a session error: the session did\n"
                 "    #: its job, the fill moves to the next regular session.\n"
                 "    deferred: int = 0\n")
    if "deferred: int = 0" in text:
        notes.append("session.py: deferred counter already present")
    elif old_field in text:
        text = text.replace(old_field, new_field, 1)
        notes.append("session.py: added SessionResult.deferred")
    else:
        return ["ERROR: could not find the SessionResult fields - aborted"]

    old_sum = 'f"errors={self.errors} ran={self.ran}")'
    new_sum = 'f"errors={self.errors} deferred={self.deferred} ran={self.ran}")'
    if new_sum in text:
        notes.append("session.py: summary already reports deferred")
    elif old_sum in text:
        text = text.replace(old_sum, new_sum, 1)
        notes.append("session.py: summary now reports deferred")
    else:
        return ["ERROR: could not find SessionResult.summary - aborted"]

    old_catch = (
        '        except Exception as exc:  # noqa: BLE001 - logged, never fatal\n'
        '            LOG.exception("exit handling failed for %s", position.ticker)\n'
        '            log.log_system_error(stage="exit", detail=f"{type(exc).__name__}: {exc}",\n'
        '                                 ticker=position.ticker)\n'
        '            result.errors += 1\n')
    new_catch = (
        '        except Exception as exc:  # noqa: BLE001 - logged, never fatal\n'
        '            # Section 7 says a stale quote defers the fill to the next regular\n'
        '            # session. That is the protocol working, not the session failing,\n'
        '            # so it is counted separately and does not void the day.\n'
        '            # Matched by name: session.py stays broker-agnostic (BrokerLike is\n'
        '            # a Protocol) and must not import the Alpaca-backed broker module.\n'
        '            is_deferral = any(cls.__name__ == "StaleQuote"\n'
        '                              for cls in type(exc).__mro__)\n'
        '            if not is_deferral:\n'
        '                LOG.exception("exit handling failed for %s", position.ticker)\n'
        '            else:\n'
        '                LOG.warning("exit deferred for %s: %s", position.ticker, exc)\n'
        '            log.log_system_error(stage="exit", detail=f"{type(exc).__name__}: {exc}",\n'
        '                                 ticker=position.ticker)\n'
        '            if is_deferral:\n'
        '                result.deferred += 1\n'
        '            else:\n'
        '                result.errors += 1\n')
    if "is_deferral" in text:
        notes.append("session.py: exit handler already classifies deferrals")
    elif old_catch in text:
        text = text.replace(old_catch, new_catch, 1)
        notes.append("session.py: exit handler now separates deferrals from errors")
    else:
        return ["ERROR: could not find the exit except block - aborted"]

    SESSION.write_text(text, encoding="utf-8")
    return notes


COMMIT = """study: a deferred exit is no longer a session error

Section 11 defect correction, 2026-10-14.

run_session.py advances study state only when result.errors == 0, and every
failure path shared that counter. One stale quote on one open position voided
the entire session.

That happened on 2026-10-14: DLTR's quote was stale, the exit deferred per
Section 7, and the run was marked failed. 22 signal rows were written for a day
with no sessions.csv provenance row and no counter increment.

Section 7 states a stale quote defers the fill to the next regular session.
Deferring is the prescribed behaviour. The system followed the protocol and
then refused to count the day for having done so.

SessionResult now carries a separate `deferred` counter. A StaleQuote during an
exit increments it instead of `errors`. The SYSTEM_ERROR row is unchanged, so
the disclosure is unchanged.

No strategy parameter is touched. MAX_QUOTE_AGE_SECONDS, the R:R floor and the
cash reserve are unchanged. The exit still defers, so no trade occurs that
would not have occurred before. Config conformance mismatch, adapter failure
and unexpected exceptions still void the session.

2026-10-14 itself stays uncounted. Section 10 logs are append-only (Rule 8) and
are not edited retroactively; the day is disclosed, not repaired.
"""


def main() -> int:
    if not SESSION.exists():
        print(f"not found: {SESSION}", file=sys.stderr)
        return 1
    notes = patch_session()
    for line in notes:
        print(" ", line)
    if any(n.startswith("ERROR") for n in notes):
        return 1
    MSG.parent.mkdir(parents=True, exist_ok=True)
    MSG.write_text(COMMIT, encoding="utf-8")
    print(f"  commit message written to {MSG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
