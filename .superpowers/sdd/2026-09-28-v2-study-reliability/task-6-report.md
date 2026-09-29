# Task 6: Risk alignment and final validation

## Changes

- Set `RiskConfig.min_reward_risk` to `2.0`, matching Section 5's
  `MIN_REWARD_TO_RISK`.
- Corrected the worked-example reward:risk description to `2.0 to Target 1`.
- Corrected the contradictory `knowledge/Trading-Rules.md` minimum.
- Added a regression assertion tying the risk configuration to the Section 5
  constant, and checked the worked example's Target 1 ratio arithmetically.
- Left configured targets at `(2.0, 4.0)` and did not change the Section 5
  minimum or any CSV schema/dependencies.

## RED/GREEN

- **RED:** Added the risk-agent floor assertion first, then ran
  `python -m pytest tests/test_risk.py tests/test_study_rules.py -q`.
  It failed as expected: configured floor `1.5` versus Section 5's `2.0`
  (`1 failed, 62 passed`).
- **GREEN:** After aligning the configuration and documentation, the same
  focused command passed (`63 passed`).
- **Full suite:** `python -m pytest -q` passed (`263 passed`, one
  `websockets.legacy` deprecation warning).

## Final preservation checks

- `study/v2_state.json` starts with `completed_sessions: 0`,
  `last_session_date: null`, and `start_date: "2026-09-29"`.
- Compared with `origin/main`, the branch has no changes to
  `study/signals.csv`, `study/trades.csv`, or `study/universe.txt`.
  Signal CSV hash: `a6302a0f276239d93f9cacf0ecfa09a3cde11be5`;
  trade CSV hash: `66726cca6476b16c22316022fba4c18c83ea71c6`;
  frozen universe: 505 symbols, hash
  `1740586cb9153486954ea0eb311fdd11c7f6d752`.
- `git diff --check` completed without errors.
- Final branch diff against `origin/main`: **28 files changed, 2,916
  insertions(+), 131 deletions(-)**, including this report.
- Risk alignment commit: `b88a184` (`fix: align risk agent with Section 5
  floor`), with trailer `Co-authored-by: Copilot App
  <223556219+Copilot@users.noreply.github.com>`.

## Concerns

The full suite emitted only the reported deprecation warning. Scheduled
attempts and the watchdog improve recovery and detection but depend on GitHub
and provider availability; they do not guarantee uptime.
