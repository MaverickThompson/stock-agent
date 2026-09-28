# Task 3 Report: Idempotent Broker Order Retries

## Status

Implemented broker-side idempotency for V2 order submissions. The requested
commit is `fix: make V2 broker retries idempotent`.

## Implementation

- Added the required `client_order_id` argument to `BrokerLike.submit` and
  `AlpacaBroker.submit`, and pass it into Alpaca's `MarketOrderRequest`.
- Added `make_client_order_id()` in `session.py`. IDs are deterministic for a
  V2 UTC market date, symbol, and action; the visible `v2-YYYYMMDD-` prefix and
  32-character SHA-256 prefix keep IDs to 44 characters.
- Entry IDs use the `entry` action; exit IDs use their exit reason.
- Transient submission retries reuse the same request/client ID. When Alpaca
  reports that `client_order_id` must be unique, the broker retrieves the
  existing order with `get_order_by_client_id()` and sends that order through
  the existing `_await_fill()` reconciliation and `Fill` construction path.
- Updated the paper-order smoke script to provide distinct IDs for its buy and
  sell orders.
- Added fake-backed broker tests and session tests for retry reuse, UTC date
  stability, action distinction, duplicate lookup/no resubmission, and fill
  equivalence between first submission and recovered order.

No dependency, credential, V2 progress-state, workflow, V1 log/CSV schema,
frozen-universe, or Section 5 target changes were made. The 2.0R minimum and
configured `(2.0, 4.0)` targets remain unchanged.

## TDD and Validation

### RED

Command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Output (after correcting test-module registration in the fake-loader setup):

```text
FF......FFFF..F.F...                                                     [100%]
================================== FAILURES ==================================
____________ test_transient_retry_reuses_the_same_client_order_id _____________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x00000292F9172D50>

    def test_transient_retry_reuses_the_same_client_order_id(monkeypatch):
        install_sdk_fakes(monkeypatch)
        attempts = 0

        def submit(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("request timed out")
            return settled_order()

        monkeypatch.setattr(broker.time, "sleep", lambda _seconds: None)
        client = FakeTradingClient(submit=submit)
>       fill = make_broker(client).submit(
            "AAPL", 1, "buy", client_order_id="v2-20260929-example", quote=quote())
E       TypeError: AlpacaBroker.submit() got an unexpected keyword argument 'client_order_id'

tests\test_broker.py:95: TypeError
____ test_duplicate_client_order_id_looks_up_and_reconciles_existing_order ____

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x00000292F91C86D0>

    def test_duplicate_client_order_id_looks_up_and_reconciles_existing_order(monkeypatch):
        install_sdk_fakes(monkeypatch)

        class DuplicateOrderError(Exception):
            status_code = 422

        duplicate_order = settled_order()
        normal_client = FakeTradingClient()
>       normal_fill = make_broker(normal_client).submit(
            "AAPL", 1, "buy", client_order_id="v2-20260929-entry", quote=quote())
E       TypeError: AlpacaBroker.submit() got an unexpected keyword argument 'client_order_id'

tests\test_broker.py:112: TypeError
_____________________ test_clean_entry_writes_both_files ______________________

tmp_path = WindowsPath('C:/Users/chasi/AppData/Local/Temp/pytest-of-chasi/pytest-49/test_clean_entry_writes_both_f0')

    def test_clean_entry_writes_both_files(tmp_path):
        log = StudyLog(tmp_path)
        broker = FakeBroker({"AAPL": FakeQuote(100.9, 101.0)})
        result = session.run_session(broker=broker, log=log,
                                     candidates=[Candidate("AAPL", 1.0, "tech")],
                                     thesis_for=lambda c: thesis_ok(),
                                     open_positions=[], now=NOW)
>       assert result.entered == 1
E       assert 0 == 1
E        +  where 0 = SessionResult(entered=0, exited=0, rejected=0, errors=1, skipped=0, ran=True).entered

tests\test_session.py:154: AssertionError
------------------------------ Captured log call ------------------------------
ERROR    stockagent.session:session.py:326 entry handling failed for AAPL
Traceback (most recent call last):
  File "C:\Users\chasi\.copilot\repos\copilot-worktrees\stock-agent\maverickthompson-effective-waffle\src\stockagent\session.py", line 301, in run_session
    fill = broker.submit(candidate.symbol, size, "buy", quote=quote)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: FakeBroker.submit() missing 1 required keyword-only argument: 'client_order_id'
________________ test_retried_entry_reuses_its_client_order_id ________________

tmp_path = WindowsPath('C:/Users/chasi/AppData/Local/Temp/pytest-of-chasi/pytest-49/test_retried_entry_reuses_its_0')

    def test_retried_entry_reuses_its_client_order_id(tmp_path):
        broker = FakeBroker({"AAPL": FakeQuote(100.9, 101.0)})
        for _ in range(2):
            session.run_session(
                broker=broker, log=StudyLog(tmp_path), candidates=[Candidate("AAPL", 1.0)],
                thesis_for=lambda c: thesis_ok(), open_positions=[], now=NOW)

>       assert len(broker.client_order_ids) == 2
E       assert 0 == 2
E        +  where 0 = len([])
E        +    where [] = <test_session.FakeBroker object at 0x00000292F9AFF390>.client_order_ids

tests\test_session.py:171: AssertionError
------------------------------ Captured log call ------------------------------
ERROR    stockagent.session:session.py:326 entry handling failed for AAPL
Traceback (most recent call last):
  File "C:\Users\chasi\.copilot\repos\copilot-worktrees\stock-agent\maverickthompson-effective-waffle\src\stockagent\session.py", line 301, in run_session
    fill = broker.submit(candidate.symbol, size, "buy", quote=quote)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: FakeBroker.submit() missing 1 required keyword-only argument: 'client_order_id'
ERROR    stockagent.session:session.py:326 entry handling failed for AAPL
Traceback (most recent call last):
  File "C:\Users\chasi\.copilot\repos\copilot-worktrees\stock-agent\maverickthompson-effective-waffle\src\stockagent\session.py", line 301, in run_session
    fill = broker.submit(candidate.symbol, size, "buy", quote=quote)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: FakeBroker.submit() missing 1 required keyword-only argument: 'client_order_id'
_________________ test_entry_and_exit_use_distinct_action_ids _________________

tmp_path = WindowsPath('C:/Users/chasi/AppData/Local/Temp/pytest-of-chasi/pytest-49/test_entry_and_exit_use_distin0')

    def test_entry_and_exit_use_distinct_action_ids(tmp_path):
        broker = FakeBroker({"AAPL": FakeQuote(100.9, 101.0)})
        held = []
        session.run_session(
            broker=broker, log=StudyLog(tmp_path), candidates=[Candidate("AAPL", 1.0)],
            thesis_for=lambda c: thesis_ok(), open_positions=held, now=NOW)

        broker.quotes["AAPL"] = FakeQuote(111.0, 111.1)
        session.run_session(
            broker=broker, log=StudyLog(tmp_path), candidates=[],
            thesis_for=lambda c: None, open_positions=held, now=NOW)

>       assert len(broker.client_order_ids) == 2
E       assert 0 == 2
E        +  where 0 = len([])
E        +    where [] = <test_session.FakeBroker object at 0x00000292F9A8D250>.client_order_ids

tests\test_session.py:187: AssertionError
------------------------------ Captured log call ------------------------------
ERROR    stockagent.session:session.py:326 entry handling failed for AAPL
Traceback (most recent call last):
  File "C:\Users\chasi\.copilot\repos\stock-agent\maverickthompson-effective-waffle\src\stockagent\session.py", line 301, in run_session
    fill = broker.submit(candidate.symbol, size, "buy", quote=quote)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: FakeBroker.submit() missing 1 required keyword-only argument: 'client_order_id'
_______ test_client_order_id_uses_utc_market_date_and_fits_alpaca_limit _______

    def test_client_order_id_uses_utc_market_date_and_fits_alpaca_limit():
        utc = dt.datetime(2026, 9, 29, 14, 31, tzinfo=dt.timezone.utc)
        equivalent_next_day_local = dt.datetime(
            2026, 9, 30, 0, 31, tzinfo=dt.timezone(dt.timedelta(hours=10)))

>       order_id = session.make_client_order_id("AAPL", "entry", utc)
                   ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E       AttributeError: module 'sa.session' has no attribute 'make_client_order_id'

tests\test_session.py:196: AttributeError
_____________ test_broker_outage_logs_system_error_and_continues ______________

tmp_path = WindowsPath('C:/Users/chasi/AppData/Local/Temp/pytest-of-chasi/pytest-49/test_broker_outage_logs_system0')

    def test_broker_outage_logs_system_error_and_continues(tmp_path):
        log = StudyLog(tmp_path)
        broker = FakeBroker({"AAPL": FakeQuote(100.9, 101.0),
                             "MSFT": FakeQuote(100.9, 101.0)}, fail_on=["AAPL"])
        result = session.run_session(
            broker=broker, log=log,
            candidates=[Candidate("AAPL", 2.0), Candidate("MSFT", 1.0)],
            thesis_for=lambda c: thesis_ok(), open_positions=[], now=NOW)
>       assert result.errors == 1 and result.entered == 1
E       assert (2 == 1)
E        +  where 2 = SessionResult(entered=0, exited=0, rejected=0, errors=2, skipped=0, ran=True).errors

tests\test_session.py:244: AssertionError
------------------------------ Captured log call ------------------------------
ERROR    stockagent.session:session.py:326 entry handling failed for AAPL
Traceback (most recent call last):
  File "C:\Users\chasi\.copilot\repos\copilot-worktrees\stock-agent\maverickthompson-effective-waffle\src\stockagent\session.py", line 261, in run_session
    quote = broker.quote(candidate.symbol)
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\chasi\.copilot\repos\copilot-worktrees\stock-agent\maverickthompson-effective-waffle\tests\test_session.py", line 74, in quote
    raise RuntimeError(f"connector outage for {symbol}")
RuntimeError: connector outage for AAPL
ERROR    stockagent.session:session.py:326 entry handling failed for MSFT
Traceback (most recent call last):
  File "C:\Users\chasi\.copilot\repos\copilot-worktrees\stock-agent\maverickthompson-effective-waffle\src\stockagent\session.py", line 301, in run_session
    fill = broker.submit(candidate.symbol, size, "buy", quote=quote)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: FakeBroker.submit() missing 1 required keyword-only argument: 'client_order_id'
__________ test_partial_exit_moves_stop_to_entry_and_halves_position __________

tmp_path = WindowsPath('C:/Users/chasi/AppData/Local/Temp/pytest-of-chasi/pytest-49/test_partial_exit_moves_stop_t0')

    def test_partial_exit_moves_stop_to_entry_and_halves_position(tmp_path):
        log = StudyLog(tmp_path)
        held = [position()]
        broker = FakeBroker({"AAPL": FakeQuote(111.0, 111.1)})
        result = session.run_session(broker=broker, log=log, candidates=[],
                                     thesis_for=lambda c: None,
                                     open_positions=held, now=NOW)
>       assert result.exited == 1
E       assert 0 == 1
E        +  where 0 = SessionResult(entered=0, exited=0, rejected=0, errors=1, skipped=0, ran=True).exited

tests\test_session.py:272: AssertionError
------------------------------ Captured log call ------------------------------
ERROR    stockagent.session:session.py:235 exit handling failed for AAPL
Traceback (most recent call last):
  File "C:\Users\chasi\.copilot\repos\copilot-worktrees\stock-agent\maverickthompson-effective-waffle\src\stockagent\session.py", line 213, in run_session
    fill = broker.submit(position.ticker, qty, "sell", quote=quote)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: FakeBroker.submit() missing 1 required keyword-only argument: 'client_order_id'
=========================== short test summary info ============================
FAILED tests\test_broker.py::test_transient_retry_reuses_the_same_client_order_id
FAILED tests\test_broker.py::test_duplicate_client_order_id_looks_up_and_reconciles_existing_order
FAILED tests\test_session.py::test_clean_entry_writes_both_files - assert 0 == 1
FAILED tests\test_session.py::test_retried_entry_reuses_its_client_order_id
FAILED tests\test_session.py::test_entry_and_exit_use_distinct_action_ids - a...
FAILED tests\test_session.py::test_client_order_id_uses_utc_market_date_and_fits_alpaca_limit
FAILED tests\test_session.py::test_broker_outage_logs_system_error_and_continues
FAILED tests\test_session.py::test_partial_exit_moves_stop_to_entry_and_halves_position
8 failed, 12 passed in 0.66s
```

The initial collection attempt exposed a fake-loader setup error (the imported
module was not registered in `sys.modules` for dataclass processing). The test
loader was corrected and the RED run then failed for the missing required
client-ID contract and helper behavior.

### GREEN

Focused command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Output:

```text
....................                                                     [100%]
20 passed in 0.59s
```

Full suite command:

```text
python -m pytest -q
```

Output:

```text
........................................................................ [ 30%]
........................................................................ [ 61%]
........................................................................ [ 92%]
..................                                                       [100%]
234 passed in 9.28s
```

`git diff --check` passed. Source, tests, and the paper smoke script were
self-reviewed. The Alpaca SDK package and live paper credentials were not
available in this checkout; broker behavior is covered using SDK/client fakes,
and no live order was submitted.

Syntax validation also passed:

```text
python -m py_compile src/stockagent/broker.py src/stockagent/session.py scripts/smoke_broker.py tests/test_broker.py
```

## Review Fixes (2026-09-28)

Addressed the three Task 3 review findings:

- Fill reconciliation now accepts only terminal broker statuses and requires a
  positive broker-reported `filled_qty` no greater than the requested amount.
  Canceled/rejected orders with no fill and orders still pending at timeout
  raise `BrokerError`; a canceled partial order records its actual filled
  quantity. Session trade rows and position sizes/remaining quantities now use
  the returned quantity, and sector exposure is based on the actual filled
  quantity and fill price.
- A position's entry timestamp is included in its exit ID, so same-day exits
  for the same symbol and reason remain distinct but retries of one position
  action reuse its ID. A second same-symbol entry on the same UTC date is
  logged as `SKIPPED`, preventing reuse of the first entry's deterministic ID.
- After exhausted transient submission failures, the broker looks up the
  deterministic client ID. It reconciles a found order normally; if lookup
  fails or confirms no order, it re-raises the original submission failure.

## Review Fix Round 3 (2026-09-28)

The scoped review found that a process could stop after `trades.csv` received
an entry row but before the corresponding ID-bearing signal row was appended.
On retry, neither the position state nor the signal log would identify the
accepted entry, risking a duplicate trade-log row.

The entry guard now reconciles same-day trade rows by `(entry_timestamp,
ticker)`, comparing logged entry quantity less logged exit quantity with the
remaining quantity in tracked positions. Any unexplained open quantity blocks
a replay, while fully exited same-day rows do not prevent a distinct entry.
This preserves the frozen CSV schemas and permits multiple tracked same-symbol
actions in one run.

Regression coverage includes a realistic entry-plus-exit pair followed by a
distinct action, an unmatched same-day entry row representing the write gap,
and an already-recorded client ID.

RED command:

```text
python -m pytest tests/test_session.py::test_unmatched_same_day_entry_trade_blocks_replay_after_crash -q
```

Output: `1 failed` because the unmarked entry row was replayed.

GREEN command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Output: `35 passed in 1.00s`. `git diff --check` also passed.

## Review Fix Round 4 (2026-09-28)

The follow-up review found that an open same-day position with an empty
`position_id` was treated as an exact duplicate. The early position guard now
suppresses only a matching non-empty client order ID; ID-less tracked quantities
are handled by trade-row reconciliation instead.

The regression uses an ID-less tracked position and a distinct entry action.
Before the fix it was incorrectly skipped.

RED command:

```text
python -m pytest tests/test_session.py::test_idless_same_day_position_does_not_block_distinct_entry_action -q
```

Output: `1 failed` because the distinct action was skipped.

GREEN command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Output: `36 passed in 1.03s`. `git diff --check` also passed.

No V1 rows or CSV schemas, study state, daily marker, target configuration,
universe, dependencies, credentials, workflows, or uptime claims were changed.

### TDD RED

Command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Result:

```text
10 failed, 19 passed in 0.98s
```

The failures reproduced unfilled duplicate orders becoming fills, requested
quantity being recorded instead of actual partial quantity, pending-order
timeout fabrication, missing exhausted-timeout lookup, same-day re-entry ID
reuse, missing per-position exit identity, and incorrect session position and
trade quantities.

The partially-filled-pending timeout case was then isolated:

```text
python -m pytest tests/test_broker.py::test_pending_order_at_timeout_does_not_create_a_fill -q
```

It failed because `partially_filled` was being treated as terminal by a
substring status check. Replacing this with exact terminal status matching
ensured a still-pending partial order raises instead of returning a fill.

### TDD GREEN and final validation

Focused command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Result:

```text
29 passed in 0.77s
```

Full-suite command:

```text
python -m pytest -q
```

Result:

```text
243 passed in 10.45s
```

## Follow-up Fix: Action Identity and Partial Target 1 (2026-09-28)

The earlier same-day entry guard prevented an ID collision by skipping every
additional same-symbol entry that day. It is now scoped to the same logical
entry action: distinct candidate actions receive distinct IDs, while a replay
of an already recorded action is skipped. New positions retain their entry
client ID as their stable position identity; legacy positions fall back to
their entry timestamp. Exit IDs also include the action portion, so retries
for one remaining quantity reuse their ID and the next distinct portion gets
a new one.

Target 1 now tracks cumulative filled shares. A partial fill reduces only the
actual remaining position, leaves the stop unchanged, and retries only the
unfilled part of the planned half-exit. The stop moves to entry and
`target_1_hit` changes only after that planned quantity has filled. Stop,
falsification, and terminal exit precedence is unchanged.

No V1 signal/trade CSV columns, V2 start date or 60-session accounting,
frozen universe, workflow state commit boundary, study targets, dependencies,
or credentials changed. Scheduled attempts and watchdog recovery remain
dependent on GitHub and provider availability; no guaranteed uptime is claimed.

### RED

Command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Output:

```text
3 failed, 30 passed in 1.00s
```

The three expected failures showed same-day distinct entries still being
skipped, missing stable position identity, and no cumulative Target 1 fill
state.

### GREEN

Command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Output:

```text
33 passed in 1.03s
```

The regression coverage includes distinct same-symbol entry and exit IDs,
stable retries for an unchanged remainder, a partial Target 1 followed by the
exact outstanding quantity under a new ID, zero-fill non-recording, and the
existing rejected/zero, pending-order, and accepted-then-timeout lookup
protections.

### Full-suite verification

Command:

```text
python -m pytest -q
```

Output:

```text
247 passed in 9.34s
```

## Remaining Review Fix: Closed Same-Day Trade False Positive (2026-09-28)

Removed the same-day trade-row count fallback from
`_entry_action_already_recorded`. Replay protection now compares the requested
client order ID with IDs on open positions and recorded `ENTERED` signals; a
closed trade row alone no longer blocks a distinct same-symbol entry. The
existing identical-client-ID replay test remains in place, and a regression
test covers a closed same-day trade followed by a distinct entry action.

No V1 rows or CSV schemas, V2 start date or 60-session accounting, frozen
universe, workflow state commit boundary, Section 5 minimum or targets,
dependencies, or credentials changed. Scheduled attempts and watchdog
recovery remain dependent on GitHub and provider availability; no guaranteed
uptime is claimed.

### RED

Command:

```text
python -m pytest tests/test_session.py::test_closed_same_day_trade_does_not_block_distinct_entry_action -q
```

Output:

```text
1 failed in 0.32s
```

The candidate was incorrectly skipped after the helper counted the prior
same-day closed trade against the new action.

### GREEN

Focused command:

```text
python -m pytest tests/test_broker.py tests/test_session.py -q
```

Output:

```text
34 passed in 0.97s
```

The focused run includes both the new closed-trade regression and
`test_replaying_recorded_entry_action_is_skipped`, which confirms an identical
recorded client ID remains blocked.
