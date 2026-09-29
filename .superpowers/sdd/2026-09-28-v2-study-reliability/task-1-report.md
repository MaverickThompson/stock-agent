# Task 1 Report: Durable V2 Study State

## Status

Completed and committed as `4856d16b6215e772ce9691f0a505efc963063734`
(`feat: add durable V2 session state`).

## Implementation

- Added the frozen `StudyState` model and V2 constants:
  - Start date: `2026-09-29`
  - Completion target: 60 successful sessions
- The model rejects unsupported schema versions, malformed ISO dates, invalid
  counts, and inconsistent count/date/status combinations.
- Eligibility begins on the configured start date, and prevents duplicate or
  out-of-order date increments. Recording a successful eligible date returns a
  new state; session 60 marks the state complete, after which it cannot advance.
- State saves serialize to a sibling temporary file, flush and fsync it, then
  replace the destination with `Path.replace()`.
- Added `study/v2_state.json` as the active zero-progress state and focused tests
  for eligibility, increments, idempotency, completion, schema validation, and
  persistence round-trip.

No V1 signal/trade data or CSV schemas, frozen universe, Section 5 minimum
Target 1 (`2.0R`), or configured targets (`2.0`, `4.0`) were changed. No
dependencies or credentials were added.

## Validation

- RED: `python -m pytest tests/test_study_state.py -q` failed during collection
  with the expected `ModuleNotFoundError: No module named
  'stockagent.study_state'`.
- Focused GREEN: `python -m pytest tests/test_study_state.py -q` — **11 passed**.
- Full suite: `python -m pytest -q` — **218 passed**.
- `git diff --cached --check` passed before commit.
- Self-review confirmed only the three Task 1 implementation/test files were
  committed and the commit contains the required Copilot co-author trailer.

## Scope note

This task provides state persistence only. The runner must call
`record_success()` exclusively after a successful live market-session
evaluation—not for dry runs, closed-market attempts, missed sessions,
exchange holidays, or failed jobs. The runner/workflow task must also commit
`v2_state.json`, position state, the daily marker, and study logs together.
Scheduled attempts and watchdog detection improve recovery but do not guarantee
uptime because GitHub and market-data providers must be available.

## Review findings fixed

- `StudyState` now rejects any `start_date` other than the mandated V2 start
  date (`2026-09-29`), preventing a modified baseline from shifting eligibility.
- Validation now rejects a completed-session count greater than the number of
  distinct calendar dates from `start_date` through `last_session_date`,
  inclusive. Regression cases cover a changed start date, a count of two on
  the start date, and a completed count of 60 on the start date.

## Review-fix validation

- RED: `python -m pytest tests/test_study_state.py -q` — **3 failed, 11
  passed**, with failures on the three new invalid states before the fix.
- GREEN: `python -m pytest tests/test_study_state.py -q` — **14 passed**.
