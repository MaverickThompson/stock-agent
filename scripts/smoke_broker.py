#!/usr/bin/env python3
"""One-off proof that the ORDER PATH works, before day 1 of the study.

Why this exists
---------------
The dry run on 2026-09-21 rejected all three candidates at the Section 5 gates,
so ``AlpacaBroker.submit`` was never called. Coverage on ``broker.py`` is 0%.
Starting a sixty-day unattended study whose order-placement code has never once
executed means the first execution happens with nobody watching.

This script is NOT part of the study. It writes nothing to ``study/``, logs no
signals and no trades. It submits one tiny marketable order on the paper account
and reports the fill, purely to prove the code path runs end to end. Run it once,
before day 1, then never again.

    python scripts/smoke_broker.py            # checks only, places nothing
    python scripts/smoke_broker.py --order    # also places 1 share of SPY

It buys one share and sells it again in the same run, then asserts the book is
flat -- Section 12 asserts zero open positions before day 1, so the smoke test
must not leave one behind.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from stockagent.broker import AlpacaBroker, BrokerError, StaleQuote  # noqa: E402

SYMBOL = "SPY"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", action="store_true",
                    help="actually submit 1 share (paper). Without this, checks only.")
    args = ap.parse_args()

    print("1. connecting ...")
    broker = AlpacaBroker()
    print("   ok")

    print("2. account ...")
    acct = broker.account()
    print(f"   {acct}")

    print("3. positions ...")
    pos = broker.positions()
    print(f"   {pos or '(none)'}")

    print("4. market clock ...")
    is_open = broker.market_is_open()
    print(f"   market_is_open = {is_open}")

    print(f"5. quote for {SYMBOL} ...")
    try:
        q = broker.quote(SYMBOL)
        print(f"   bid={q.bid} ask={q.ask} mid={q.mid():.2f}")
    except StaleQuote as exc:
        print(f"   STALE: {exc}")
        print("   (expected outside market hours -- rerun during the session)")
        q = None

    if not args.order:
        print("\nchecks passed. order path NOT exercised.")
        print("rerun with --order during market hours to prove submit()/fill.")
        return 0

    if not is_open:
        print("\nmarket is closed; refusing to submit. Rerun during market hours.")
        return 1

    print(f"6. BUY 1 {SYMBOL} (paper) ...")
    try:
        bought = broker.submit(SYMBOL, 1, "buy")
    except BrokerError as exc:
        print(f"   BUY FAILED: {type(exc).__name__}: {exc}")
        return 1
    print(f"   FILLED: {bought}")

    # Round-trip immediately. Section 12 asserts zero open positions before
    # day 1, so the smoke test must not leave one behind. Selling also proves
    # the other half of the order path, which an entry-only test would miss.
    print(f"7. SELL 1 {SYMBOL} (paper) ...")
    try:
        sold = broker.submit(SYMBOL, 1, "sell")
    except BrokerError as exc:
        print(f"   SELL FAILED: {type(exc).__name__}: {exc}")
        print("   !! 1 share is still open. Close it in the Alpaca UI before day 1.")
        return 1
    print(f"   FILLED: {sold}")

    print("8. confirming the book is flat ...")
    remaining = broker.positions()
    print(f"   positions = {remaining or '(none)'}")
    if remaining.get(SYMBOL):
        print("   !! NOT FLAT. Close it in the Alpaca UI before day 1.")
        return 1

    print("\nORDER PATH PROVEN, both sides, book flat. Nothing was written to study/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
