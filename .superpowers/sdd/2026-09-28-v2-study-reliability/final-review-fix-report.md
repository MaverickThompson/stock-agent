# Final whole-branch review fix report

## Implementation

1. **Incomplete sessions do not advance V2.** The runner now records a day and
   writes the daily marker only when the market session ran, there were no
   session errors, and the analysis adapter loaded successfully. It saves
   mutated open positions after any live `ran=True` attempt, including partial
   failures, then exits nonzero with a visible `session incomplete` diagnostic.
   Adapter initialization/evaluation failures are logged as
   `analysis_adapter` system errors, reported to observability, and cannot turn
   an empty candidate list into a successful study evaluation. Closed-market
   attempts remain non-counted and non-failing.
2. **Failure commits keep evidence together.** The workflow stages
   `open_positions.json` with the signals and trades logs even when the runner
   fails. `v2_state.json` and `last_session.txt` remain staged only after a
   successful runner result. This keeps partial fills/position changes and
   their execution logs in one commit without success-shaped progress.
3. **Entry IDs are ranking-independent and support re-entry.** Candidate
   identity no longer includes score or sector; it uses explicit `action_id`
   when supplied or normalized symbol plus same-symbol candidate occurrence.
   A cycle suffix counts fully closed same-day trade groups, preserving a
   stable retry ID while an action is open and creating a fresh deterministic
   client ID after the trade is fully closed. IDs remain hashed and within
   Alpaca's 48-character limit. Existing duplicate/orphan safeguards and
   distinct same-symbol candidate behavior remain in place. No trade CSV
   columns changed.
4. **Health checks respect the Alpaca close.** Broker calendar retrieval now
   exposes each session's actual close time. The manual health check compares
   that close against a New York-local timestamp: an active session before its
   close is healthy/pending; after its close an unrecorded eligible session
   uses the existing missed-session recovery summary. Early closes use the
   calendar-provided time.
5. **Related global account failure handling.** If account retrieval fails
   after exits have already filled, the session now records the failure and
   returns an errored result instead of raising before the runner can persist
   the position changes. Candidates are logged as not evaluated.

## TDD evidence

- Initial RED run for retry identity, partial runner failure, missing adapter,
  and close-time health tests: **6 failed, 1 passed**. The retry ID changed
  with score/sector; runner failure and missing adapter incorrectly returned
  success; the health checker did not accept calendar-close/time inputs.
- The first re-entry test used changed score/sector and therefore did not
  expose the defect. It was tightened to retry the exact same candidate after
  a full same-day close. With the cycle suffix temporarily removed, that test
  went RED (`second.entered == 0`, expected `1`); restoring cycle identity made
  it GREEN.
- Account-failure test went RED with an uncaught account endpoint exception
  after the preceding exit; it passed after the session converted that failure
  to logged incomplete state.
- Focused GREEN run before the final full suite: **69 passed** across
  `test_session.py`, `test_study_workflow.py`, and
  `test_study_health_check.py`.

## Final verification

- `python -m pytest -q`: **272 passed**.
- `git diff --check`: passed.
- Frozen V1 CSV schemas and dependencies were not changed.
- Workflow watchdog cron remains `30 22 * * 1-5`, with
  `workflow_dispatch` and read-only `contents`/`actions` permissions unchanged.
  The session workflow's timeout, market-open bound, eligibility/readiness
  gates, and data-fetch behavior were preserved.

## Warning and concerns

The full suite reports one third-party `DeprecationWarning` from
`websockets.legacy`. I traced it with warnings promoted to errors: importing
`alpaca.trading.requests.GetCalendarRequest` loads Alpaca's trading stream,
which imports `websockets.legacy`. This is an existing dependency/import-path
warning, not a warning from the changed application code; it was not broadly
suppressed and no dependency was added or changed.
