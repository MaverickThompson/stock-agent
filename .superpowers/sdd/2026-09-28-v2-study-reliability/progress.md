# SDD ledger — plan: docs/superpowers/plans/2026-09-28-v2-study-reliability.md

## Setup

- Worktree verified: current branch is `maverickthompson-fix-market-data-fetch`, not `main`.
- Latest `origin/main` merged into the worktree before planning.
- Plan reviewed for coverage, task conflicts, test paths, and stated interfaces.
- User delegated design and execution decisions while unavailable; proceed with the recommended GitHub-native design.

## Tasks

- Task 1: fix round 1/5 (2 addressed, 0 open — commits 4856d16..51ff61a)
- Task 1: complete (commits 8fd2e1e..51ff61a, review clean)
- Task 2: fix round 1/5 (2 addressed, 0 open — gate progress-file staging on successful session; clarify V1 comment; commits 70be727..389d9a7)
- Task 2: complete (commits 51ff61a..389d9a7, review clean)
- Task 3: fix round 1/5 (2 addressed, 1 Important and 1 new Important open — commits 00b6604..cd6cef2)
- Task 3: fix round 2/5 (2 addressed, 1 new Important open — trade-row heuristic blocks distinct re-entry after same-day exit; commits cd6cef2..c6d112e)
- Task 3: fix round 3/5 (crash-window trade-row reconciliation added; focused tests 35 passed; awaiting scoped review)
- Task 3: fix round 4/5 (ID-less position no longer blocks a distinct same-day action; focused tests 36 passed; awaiting scoped review)
- Task 3: complete (commits 00b6604..4ff2f0a, review clean)
- Task 4: fix round 1/5 (timeout increased to 45 minutes to leave setup/commit headroom; commits 8298bbb..920e299, review clean)
- Task 4: complete (commits 4ff2f0a..920e299, review clean)
- Task 5: complete (commits 920e299..c966a0e, review clean)
- Task 6: complete (commits c966a0e..c87c6a7, task diff reviewed; focused/full-suite and preservation checks reported)
- Final review fix wave: commit 6d586bf addressed partial-session counting, failure-state grouping, and pre-close watchdog behavior.
- Final review finding: `_candidate_action_identities` appended occurrence to explicit action IDs, so same-symbol candidate reordering could change the broker order ID.
- Follow-up fix: explicit IDs now are used as the complete logical identity; no-ID candidates retain the occurrence fallback. Added reordered same-symbol retry regression; full suite 273 passed; awaiting scoped re-review.
