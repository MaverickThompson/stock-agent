# 60-day study — session system

Built 2026-09-21. All 64 tests pass. Nothing here is committed yet — the
laptop went offline mid-push, so these files still need to land in the repo.

## Where each file goes

Everything drops into **`MaverickThompson/stock-agent`**, preserving paths:

| File | New? | What it does |
|---|---|---|
| `src/stockagent/study_log.py` | new | The two FROZEN Section 10 schemas. Append-only, fsynced. |
| `src/stockagent/study_rules.py` | new | Section 5 entry gates + Section 6 sizing. |
| `src/stockagent/session.py` | new | One market session: exits first, then entries. |
| `src/stockagent/broker.py` | new | Alpaca paper wrapper. Retries 5xx. Rejects stale quotes. |
| `src/stockagent/observability.py` | new | Sentry init, tuned for a batch job. |
| `scripts/run_session.py` | new | Entry point the workflow calls. |
| `.github/workflows/study-session.yml` | new | Weekday cron at 14:45 UTC. |
| `requirements.txt` | **replaces** | Adds `alpaca-py` and `sentry-sdk`. |
| `tests/test_study_log.py` | new | 19 tests on the frozen schemas. |
| `tests/test_study_rules.py` | new | 33 tests on gates and sizing arithmetic. |
| `tests/test_session.py` | new | 12 tests on exits, caps, outages. |

Only `requirements.txt` overwrites anything. Everything else is additive —
no existing module was modified.

## Then

```bash
pip install -r requirements.txt
python -m pytest tests/ -q          # expect 64 passed
```

## Before the dry run

1. Confirm `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` and `SENTRY_DSN` are in
   the repo's Actions secrets.
2. Actions tab → "Study session" → Run workflow, with **dry run checked**.
3. Check `study/signals.csv` has rows. Section 10: "A dry run that produces
   no rows does not count as passing."

## What is NOT built yet

`study_adapter.py` — the bridge from the existing `DiscoveryAgent` and
`analyze_symbol()` to the session's `Candidate` and `Thesis` types. Without
it the session manages open positions and logs a SYSTEM_ERROR saying no
entries were evaluated. That is deliberate: it fails loudly rather than
looking like a quiet day.

It could not be written yet because the laptop went offline before those
module signatures could be read.

## Two decisions recorded, both needing your sign-off before day 1

1. **"Round-number stops are prohibited" is undefined in the protocol.**
   Implemented as: within a cent of a whole or half dollar. In
   `study_rules.ROUND_NUMBER_TOLERANCE`.
2. **Stop beats target when a bar touches both.** `evaluate_exit` checks the
   stop first. Assuming the favourable side is the most common way a paper
   study flatters itself.

Both are interpretations made before day 1, which Section 11 permits. They
should be written into Section 12 so they are on the record.

---

# Round 2 — 2026-09-21, the adapter

`study_adapter.py` and `earnings.py` added. **77 tests pass.**

## New files

| File | What |
|---|---|
| `src/stockagent/earnings.py` | Alpha Vantage earnings calendar for the Section 5 48h blackout. Cached daily. |
| `src/stockagent/study_adapter.py` | Bridges DiscoveryAgent / analyze_symbol to the session's Candidate and Thesis. |
| `tests/test_earnings_and_adapter.py` | 13 tests, mostly on the failure direction of the earnings gate. |

## New secret required

`ALPHAVANTAGE_API_KEY` in the stock-agent repo's Actions secrets. Free tier is
enough — one request per session covers the whole market for 3 months.

Without it the calendar is unavailable, every candidate is rejected with an
honest reason, and no trades are taken. That is deliberate.

## FOUR MAPPINGS FIXED BEFORE DAY 1 — copy these into Section 12

1. **Predicted probability = `verdict.confidence`.** Section 5 asks for a
   predicted probability of reaching Target 1; the debate produces a manager
   confidence. They are not necessarily the same quantity. Mapping fixed in
   advance.

2. **Entry zone = entry +/- 10% of the risk distance** (`entry - stop`).
   `TradeIdea` gives one entry price; Section 5 requires a zone. Scaling to the
   trade's own risk rather than a flat percentage means it behaves the same on
   a $20 stock and a $600 one, and it protects the R:R gate directly — paying
   more than a tenth of your risk above plan erodes the 2:1 the trade was
   approved on.

3. **Falsification = the manager's `conditions` when present**, else a daily
   close through the stop.

4. **An unknown earnings position FAILS the gate.** If the calendar could not
   be consulted the candidate is rejected, never entered. "We could not check"
   is not a way of satisfying "no earnings within 48 hours".

Plus the two from round 1: the round-number-stop definition, and stop-beats-
target when a bar touches both.

Six interpretations total. All made before day 1, which Section 11 permits —
but they belong in Section 12 so they are on the record rather than discovered
in the code later.
