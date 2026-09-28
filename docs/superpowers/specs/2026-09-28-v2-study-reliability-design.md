# V2 Study Reliability Design

## Goal and operating assumptions

V2 starts on the next regular U.S. equity session, 2026-09-29. It completes
after 60 successful, live market-session evaluations, not 60 weekdays and not
the old V1 calendar window. A market day counts only after the market was
confirmed open, evaluation finished, and its state and logs were committed.
Dry runs, holidays, closed-market attempts, and failed jobs do not count.
Missed sessions extend the end date rather than reducing the experiment to
fewer than 60 observations.

GitHub Actions cannot guarantee that its scheduler, runner pool, Alpaca, or
Yahoo Finance will always be available. The objective is therefore to maximize
the chance of a same-day run, make duplicate trading requests idempotent, keep
the 60-session count correct, and make any missed market day visible. This is
not an absolute availability guarantee.

## Chosen architecture

### Durable V2 state

Add a committed `study/v2_state.json` as the authoritative V2 control record:
schema version, fixed V2 start date, count of completed sessions, last
completed market date, and active/completed status. Initialize it at zero; do
not infer V2 progress from V1's `last_session.txt` or historical CSV rows.

The scheduler checks the start date, status, count, and last completed date
before expensive work. It cannot bypass these checks with a `force` input.
Under the existing serialized `study-session` concurrency group, a live
session increments the count exactly once for its market date. A successful
session with no trade is still a completed observation. Dry runs and failures
do not advance state. At 60, the system marks V2 complete and all later
scheduled or manually dispatched attempts exit without trading.

The existing signal and trade CSVs remain append-only and retain V1 evidence;
the frozen 505-symbol universe is reused. The current open-position file is
empty, so V2 begins with no V1 positions to carry forward.

Replace the old hard-coded V1 date-based end guard with the V2 completion
state. Keep the Section 10 CSV schemas unchanged. Align `RiskConfig`'s
reward-to-risk floor and the risk-management worked example with Section 5's
2.0R minimum and the configured 2R/4R targets.

### Same-day execution and recovery

Keep the existing 19 market-hour cron attempts and daily marker, but make each
attempt:

1. Check V2 eligibility and duplicate-day state.
2. Check Alpaca's clock before the expensive history fetch. Wait only within a
   bounded interval that fits the 20-minute job timeout; otherwise exit cleanly
   so a later cron attempt can retry.
3. Fetch current data and evaluate once the market is open. Existing per-symbol
   retries and later cron attempts provide bounded retry opportunities.
4. Commit logs, V2 state, position state, and the daily marker together. A
   failed push remains a failed run rather than a success-shaped completion.

Give each Alpaca order a deterministic client order ID derived from V2,
market date, symbol, and action. If a submit response is ambiguous or a retry
finds that ID already used, look up the existing order and reconcile its
result rather than placing a duplicate. This is required because a runner can
lose its local state after a broker order has been accepted.

### Missed-day watchdog

Add a separate, manually dispatchable health-check workflow with a weekday
post-close schedule. It uses Alpaca's market calendar to distinguish exchange
holidays from missed trading days. If V2 is active and the market was open but
no successful session was committed for that date, it fails with a concise
summary naming the missing date and recent study workflow runs. This exposes a
miss even if every main workflow cron attempt failed to start. The next market
day remains eligible, so a missed day does not shorten the 60-session sample.

Manual dispatch remains a recovery option during a market session, but uses
the same start, deduplication, and 60-session guards as scheduled runs.

## Error handling and safety

- A far-away next open is a clean skip, not an overnight sleep.
- Data, market-clock, and broker errors remain visible in Actions and the
  append-only study log where the session code can record them.
- Only successful open-market evaluations advance the V2 counter.
- Workflow serialization and committed state prevent normal duplicate daily
  runs; deterministic broker IDs protect against duplicate orders when local
  state or a Git push is lost after an accepted order.
- A watchdog failure is a detection mechanism, not a promise that a missed
  market day can be backfilled after the close.
- GitHub Actions and external provider outages can still delay or prevent a
  same-day run; the system must report this accurately rather than claim
  guaranteed uptime.

## Validation

Add tests for start-date gating, legacy V1 state isolation, one-count-per-date
semantics, dry-run and failure non-increments, the 60-session terminal guard,
workflow timeout versus wait bounds, watchdog holiday/missed-day decisions,
and deterministic broker order reconciliation. Run the complete existing test
suite and inspect the generated workflow configuration. Confirm V2 state is
initialized to zero and V1 CSV evidence and the frozen universe remain intact.
