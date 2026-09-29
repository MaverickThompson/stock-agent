# V2 Study Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make V2 start on 2026-09-29, complete 60 successful live market sessions, avoid duplicate broker orders on retries, and visibly detect missed trading days.

**Architecture:** Add an authoritative committed V2 state record and pass its completed-session count into the session engine. Keep repeated GitHub schedules, move the bounded Alpaca-open gate before market-data fetch, use deterministic Alpaca client order IDs for safe resubmission, and add an independent post-close health workflow that fails visibly when an exchange session was missed.

**Tech Stack:** Python 3.12, pytest, Alpaca Python SDK, GitHub Actions YAML, Git-backed study state and CSV logs.

## Global Constraints

- V2 start date is `2026-09-29`; V2 ends after 60 successful live market-session evaluations.
- Missed sessions, exchange holidays, closed-market attempts, dry runs, and failed jobs do not increment the V2 count.
- Keep V1 signal/trade rows and CSV schemas append-only; reuse the frozen 505-symbol universe.
- The V2 state file, position state, daily marker, and study logs must be committed together.
- Keep the study's Section 5 Target 1 minimum at `2.0R` and configured targets at `(2.0, 4.0)`.
- Do not claim guaranteed uptime: scheduled attempts and the watchdog improve recovery and detection but depend on GitHub and provider availability.
- Do not add dependencies or external credentials.

---

## File map

- Create `src/stockagent/study_state.py`: validated V2 state model, eligibility, exactly-once-per-date advancement, and atomic JSON persistence.
- Create `study/v2_state.json`: checked-in V2 baseline with zero completed sessions and the fixed start date.
- Create `scripts/check_study_eligibility.py`: reusable preflight CLI that writes a `skip=true/false` GitHub Actions output from V2 state and the daily marker.
- Modify `src/stockagent/session.py` and `src/stockagent/study_rules.py`: accept V2 progress, stop after session 60, and remove V1's fixed calendar dates from the active study-window rules.
- Modify `scripts/run_session.py`: load V2 state, call the session engine with progress, and advance state only for a successful non-dry-run session.
- Modify `src/stockagent/broker.py`: accept deterministic client order IDs and reconcile an already-submitted order after an ambiguous retry.
- Modify `src/stockagent/session.py`: derive a stable V2/date/symbol/action order key for each entry or exit.
- Modify `.github/workflows/study-session.yml`: guard start/count/daily completion, wait for market open before data fetch, keep all work behind the readiness output, and commit V2 state with the existing study files.
- Create `scripts/check_study_health.py`: use Alpaca's exchange calendar and V2 state to detect an uncompleted market day and write an actionable summary.
- Create `.github/workflows/study-health-check.yml`: run the check after the regular close on weekdays and allow manual diagnostics.
- Create `tests/test_study_state.py`, `tests/test_broker.py`, and `tests/test_study_health_check.py`; extend `tests/test_session.py`, `tests/test_market_open_wait.py`, and `tests/test_study_workflow.py`.
- Modify `src/stockagent/config.py` and `strategy/risk-management.md`: align the agent-level reward-to-risk floor and worked example with the already-configured 2R/4R targets.

## Task 1: Add durable V2 session state

**Files:**
- Create: `src/stockagent/study_state.py`
- Create: `study/v2_state.json`
- Create: `tests/test_study_state.py`

**Interfaces:**
- Produces `V2_START_DATE: date`, `V2_SESSION_TARGET: int`, `StudyState.load(path)`, `StudyState.is_eligible(today)`, `StudyState.record_success(today)`, and `StudyState.save(path)`.
- `StudyState` fields are `schema_version: int`, `start_date: date`, `completed_sessions: int`, `last_session_date: date | None`, and `status: Literal["active", "complete"]`.
- `record_success` returns a new state; it is idempotent for a date already recorded and marks complete at 60.
- Initial JSON uses start date `2026-09-29`, count `0`, null last date, and active status.

- [ ] **Step 1: Write failing state tests** for before-start/ineligible dates, successful date increments, duplicate-date no-op, the 60th-session transition, no increments after complete, invalid JSON/schema, and atomic persistence round-trip. Test dry-run non-incrementing in Task 2 at the runner boundary.
- [ ] **Step 2: Run the focused tests and verify they fail** because `study_state.py` does not exist.

Run: `python -m pytest tests/test_study_state.py -q`
Expected: collection/import failure for the missing `stockagent.study_state` module.

- [ ] **Step 3: Implement the immutable state model.** Validate schema version, date formats, count range `0..60`, and status/count consistency. Save through a sibling temporary file and `Path.replace()` so an interrupted write cannot leave truncated JSON.
- [ ] **Step 4: Add the initial `study/v2_state.json` and run focused tests.**

Run: `python -m pytest tests/test_study_state.py -q`
Expected: all state tests pass; the initial file reads as active with zero completed sessions.

- [ ] **Step 5: Commit the state model and tests.**

```powershell
git add src/stockagent/study_state.py study/v2_state.json tests/test_study_state.py
git commit -m "feat: add durable V2 session state"
```

## Task 2: Count completed sessions and enforce the V2 stop

**Files:**
- Modify: `src/stockagent/session.py`
- Modify: `src/stockagent/study_rules.py`
- Modify: `scripts/run_session.py`
- Modify: `tests/test_session.py`
- Modify: `tests/test_study_workflow.py`

**Interfaces:**
- `run_session(..., completed_sessions: int = 0)` receives the durable count before the current market date.
- `run_session` is inert when `completed_sessions >= V2_SESSION_TARGET`; when it receives `59`, it runs the final session and preserves the existing end-of-window mark-to-market behavior.
- `study_rules.py` no longer exports V1's `STUDY_DAY_1`/`STUDY_DAY_60` as the active window; V2 start and completion are defined by `study_state.py`.
- `scripts/run_session.py` loads `study/v2_state.json`, passes the count, and records a successful live date only when `result.ran` is true and `STUDY_DRY_RUN` is false.
- `study/last_session.txt` remains the same-day workflow marker; it is not used to infer V2 count.

- [ ] **Step 1: Add failing tests** for the 60-session inert boundary, final-session marking at count 59 on any date after V2 start, duplicate date not incrementing, a closed-market result not incrementing, and a dry run not persisting V2 progress. Replace the old fixed-date window test and references to `STUDY_DAY_1`/`STUDY_DAY_60` in `tests/test_study_workflow.py`.
- [ ] **Step 2: Run only the new/affected tests and verify failure.**

Run: `python -m pytest tests/test_session.py tests/test_study_workflow.py -q`
Expected: new `completed_sessions` cases fail because the engine and runner do not yet accept V2 state.

- [ ] **Step 3: Replace the V1 calendar end check** in `session.py` with the passed count and remove the obsolete fixed dates from `study_rules.py`. Keep the final-session mark behavior only for count 59.
- [ ] **Step 4: Integrate state in `run_session.py`.** Do not advance the state for dry runs, closed-market returns, exceptions, or repeated dates. Persist positions, state, and the marker only after an open-market run returns.
- [ ] **Step 5: Update workflow tests and run focused tests.**

Run: `python -m pytest tests/test_session.py tests/test_study_workflow.py tests/test_study_state.py -q`
Expected: all session, runner-state, and workflow unit tests pass.

- [ ] **Step 6: Commit the counter and stop boundary.**

```powershell
git add src/stockagent/session.py scripts/run_session.py tests/test_session.py tests/test_study_workflow.py
git commit -m "feat: enforce the V2 60-session boundary"
```

## Task 3: Make broker order retries idempotent

**Files:**
- Modify: `src/stockagent/broker.py`
- Modify: `src/stockagent/session.py`
- Modify: broker/session fake implementations in `tests/test_broker.py` and `tests/test_session.py`
- Test: `tests/test_broker.py`, `tests/test_session.py`

**Interfaces:**
- Extend `BrokerLike.submit` and `AlpacaBroker.submit` with required `client_order_id: str`.
- Add a small pure helper in `session.py` that creates a stable ID from V2 version, UTC market date, symbol, and action (`entry` or exit reason); keep it within Alpaca's 48-character limit.
- If submission reports that the client ID already exists, retrieve that existing order by client ID and use its normal fill reconciliation path.

- [ ] **Step 1: Write failing tests** asserting retries for a single logical entry reuse one client ID, exit IDs differ from entry IDs, a duplicate-ID response looks up rather than resubmits, and the recovered order produces the same fill record as a first submission.
- [ ] **Step 2: Run the broker/session tests and verify failure.**

Run: `python -m pytest tests/test_broker.py tests/test_session.py -q`
Expected: failures for the missing client-ID parameter and duplicate-order reconciliation.

- [ ] **Step 3: Implement stable action IDs and broker reconciliation.** Send the ID in `MarketOrderRequest`; when the API identifies an existing client ID, call Alpaca's client-ID lookup and pass that order to `_await_fill`. Never generate a new ID on an automatic retry.
- [ ] **Step 4: Update fake brokers and verify focused tests pass.**

Run: `python -m pytest tests/test_broker.py tests/test_session.py -q`
Expected: all broker and session tests pass; existing session decisions remain unchanged apart from the ID metadata.

- [ ] **Step 5: Commit broker idempotency.**

```powershell
git add src/stockagent/broker.py src/stockagent/session.py tests/test_broker.py tests/test_session.py
git commit -m "fix: make V2 broker retries idempotent"
```

## Task 4: Harden the scheduled runner

**Files:**
- Modify: `.github/workflows/study-session.yml`
- Modify: `scripts/wait_for_market_open.py`
- Create: `scripts/check_study_eligibility.py`
- Modify: `tests/test_market_open_wait.py`
- Modify: `tests/test_study_workflow.py`

**Interfaces:**
- The eligibility CLI uses `StudyState` start date, completion status/count, and the same-day marker; the workflow consumes its output and removes `force` as a bypass.
- Market readiness is written to `steps.market_open.outputs.ready`.
- Set `max_wait_seconds` to 8 minutes and `timeout-minutes` to 45, reserving at least 10 minutes beyond the wait, fetch, and evaluation allowances for setup, snapshotting, and commit overhead. The market-open wait must run before market-data fetch; fetch, universe snapshot, and session are gated on both `skip == 'false'` and `ready == 'true'`.

- [ ] **Step 1: Add failing workflow tests** for the start/count guards, no force bypass, clock check before fetch, readiness gating of fetch/snapshot/session, and wait bound being below the job timeout with enough room for fetch/evaluation.
- [ ] **Step 2: Run workflow and wait tests to verify failure.**

Run: `python -m pytest tests/test_market_open_wait.py tests/test_study_workflow.py -q`
Expected: tests fail for the old force override, wait-after-fetch order, and old timing bounds.

- [ ] **Step 3: Move the Alpaca readiness check before fetch.** Gate all expensive/evaluation steps on readiness, use an 8-minute maximum wait, preserve the repeated UTC schedules, and set a 45-minute job timeout to leave at least 10 minutes for setup and other overhead beyond the 8-minute wait, ~6-minute fetch, and 20-minute evaluation allowance. A next open beyond the bounded wait exits cleanly for the next cron attempt.
- [ ] **Step 4: Run focused tests and inspect the rendered YAML text.**

Run: `python -m pytest tests/test_market_open_wait.py tests/test_study_workflow.py -q`
Expected: all timing and workflow-guard tests pass.

- [ ] **Step 5: Commit the scheduler hardening.**

```powershell
git add .github/workflows/study-session.yml scripts/wait_for_market_open.py scripts/check_study_eligibility.py tests/test_market_open_wait.py tests/test_study_workflow.py
git commit -m "fix: bound V2 market-open execution"
```

## Task 5: Add a post-close missed-session watchdog

**Files:**
- Create: `scripts/check_study_health.py`
- Create: `.github/workflows/study-health-check.yml`
- Create: `tests/test_study_health_check.py`
- Modify: `src/stockagent/broker.py` only if Alpaca calendar access needs a shared typed helper

**Interfaces:**
- `check_study_health(state, market_date, calendar_sessions, summary_path) -> bool` returns true for a non-session, completed V2, or completed date; returns false and writes an actionable failure summary when an exchange session was missed.
- The script uses Alpaca's calendar for the current New York market date, reads `study/v2_state.json`, and writes the result to `GITHUB_STEP_SUMMARY`.
- The workflow runs at `30 22 * * 1-5` UTC and supports `workflow_dispatch`; it requires only existing Alpaca secrets and read access to the repository.

- [ ] **Step 1: Write failing watchdog tests** for a normal market holiday, a completed market day, an active V2 missed market day, V2 not started, and V2 already complete.
- [ ] **Step 2: Run the watchdog tests and verify failure.**

Run: `python -m pytest tests/test_study_health_check.py -q`
Expected: import failure for the missing health-check script/module.

- [ ] **Step 3: Implement the calendar-aware check.** Distinguish a closed exchange day from an actual missed session; on a miss include the date, completed-session count, expected marker/state path, and a command to dispatch recovery on the next open market session.
- [ ] **Step 4: Add the scheduled/manual workflow.** Give it read-only contents/actions permissions, checkout `main`, install the existing project requirements, run the checker, and fail the job on a missed market session.
- [ ] **Step 5: Verify watchdog tests and workflow structure.**

Run: `python -m pytest tests/test_study_health_check.py tests/test_study_workflow.py -q`
Expected: holidays and completed dates are green; only a true missed trading day returns a failing health result.

- [ ] **Step 6: Commit the watchdog.**

```powershell
git add scripts/check_study_health.py .github/workflows/study-health-check.yml tests/test_study_health_check.py src/stockagent/broker.py
git commit -m "feat: alert on missed V2 market sessions"
```

## Task 6: Align the risk documentation and verify end-to-end

**Files:**
- Modify: `src/stockagent/config.py`
- Modify: `strategy/risk-management.md`
- Modify: `knowledge/Trading-Rules.md` only if the current text disagrees with the same 2R minimum.
- Test: `tests/test_risk.py`, `tests/test_study_rules.py`, and the full suite.

- [ ] **Step 1: Add a failing assertion** that the risk-agent `min_reward_risk` equals `MIN_REWARD_TO_RISK`; update the worked example assertion to say reward:risk is 2.0 to Target 1.
- [ ] **Step 2: Run the risk tests and verify failure.**

Run: `python -m pytest tests/test_risk.py tests/test_study_rules.py -q`
Expected: failure because the current agent config remains `1.5` and the risk-management prose still says `1.5`.

- [ ] **Step 3: Align the agent-level floor and docs** to `2.0`, keeping configured targets `(2.0, 4.0)` and the Section 5 minimum unchanged.
- [ ] **Step 4: Run the focused risk tests, then the full suite.**

Run: `python -m pytest tests/test_risk.py tests/test_study_rules.py -q`
Expected: all focused tests pass.

Run: `python -m pytest -q`
Expected: the full repository suite passes with no regressions.

- [ ] **Step 5: Verify final V2 state and preserved study evidence.**

Run: `git diff --check; git status --short; git diff --stat origin/main...HEAD`
Expected: no whitespace errors; initial V2 state is zero; prior signal/trade CSV entries and the frozen 505-symbol universe are unchanged by implementation.

- [ ] **Step 6: Commit the aligned risk rule and documentation.**

```powershell
git add src/stockagent/config.py strategy/risk-management.md knowledge/Trading-Rules.md tests/test_risk.py tests/test_study_rules.py
git commit -m "fix: align risk agent with Section 5 floor"
```
