# Running the study cleanly for 60 sessions

Operator runbook. Session 1 was **2026-09-29**. Session 60 lands on
**2026-12-22** — 84 calendar days. Halfway point is **2026-11-09**.

This document is about *keeping the study alive and honest*. It is not about
making it perform better. See "The line you do not cross" at the bottom.

---

## PART 1 — Fix these once, before anything else

### 1.1 Move the repository out of OneDrive  ← highest priority

The repo currently lives at `C:\Users\chasi\OneDrive\Work\hedge fund\stock-agent`.
OneDrive syncs `.git/` — the index, the object store, the lock files. It grabs
files while git is mid-write. This is the most likely cause of the stale
`.git/index.lock` that broke GitHub Desktop, and there is still a
`.git/objects/maintenance.lock` sitting there from 25 September.

This will keep happening for 60 days if it is not fixed.

**Option A — move it (preferred).**

1. Close PyCharm and GitHub Desktop.
2. In File Explorer, move the whole `stock-agent` folder to `C:\Users\chasi\dev\stock-agent`.
   (Create `dev` first.)
3. GitHub Desktop → the repo will show as missing → **Locate** → point at the new path.
4. PyCharm → File → Open → the new path. The interpreter will need re-selecting
   (Settings → Project → Python Interpreter → the `.venv` under the new path),
   or just recreate the venv, which takes a minute.

**Option B — exclude it from sync (faster, less clean).**
Right-click the `stock-agent` folder → *Free up space* will NOT do it.
You need OneDrive → Settings → Account → Choose folders → uncheck it. Note this
also stops backing it up — which is fine, because GitHub is the backup.

### 1.2 Stop generating merge commits

```
git config pull.rebase true
```

Run once, in the repo. Your history currently has repeated
`Merge branch 'main' of https://github.com/...` commits, which is the signature
of pulling while holding local changes. They add noise to a history you intend
to publish.

### 1.3 Decide on VIX and BTC

Both are market-context symbols that ended up in the tradeable universe:

* `VIX` (line 468) is an **index**. It cannot be bought. Any signal on it is meaningless.
* `BTC` (line 69) trains on `BTC-USD` (~$81,000) but the broker resolves a
  **$38.32** instrument. Every BTC decision compares mismatched data.

Both surface as *rejections*, not errors — silently wrong and invisible in the
outcome data. Removing them is defect correction, permitted by Section 11,
**but it must be logged**. Suggested amendment wording:

> 2026-09-30 — Removed VIX (an index, not tradeable) and BTC (model trained on
> BTC-USD; the broker resolves a different instrument) from the tradeable
> universe. Both were market-context symbols included in error. No threshold
> or decision parameter changed.

SPY and QQQ are real tradeable ETFs. Leaving them in is defensible; say which
you chose and why.

### 1.4 Write two tests before you forget

`observability.init_sentry` and `check_study_health._alert` both shipped
untested. Codecov will flag the patch. Two small tests:

* a malformed `SENTRY_DSN` returns `False` and does not raise
* `_alert` never raises and never changes the caller's exit code

These matter more than the coverage number. The whole failure mode of this
study is *code that fails silently*.

---

## PART 2 — The daily routine (2 minutes)

You only need this on days you intend to touch the repo. The study runs itself.

**Double-click `DAILY.bat`.** It clears stale locks, pulls with rebase, shows
what the robot changed, prints study status and conformance, lists your
uncommitted work, and runs the tests.

Read three lines of its output:

| Line | What it means |
|---|---|
| `conformance OK` | the rules that ran are the rules that were recorded. **If this ever says anything else, stop and investigate before doing anything.** |
| `sessions completed : N of 60` | should go up by exactly 1 per trading day |
| `last run fired` | should be 13:30–14:30 UTC. Later than ~16:00 UTC means the cron backlog is bad again |

Then work normally and commit in GitHub Desktop. **Write real commit messages.**
`update`, `update`, `update sum` is not a record a reviewer can read.

---

## PART 3 — The weekly check (Sunday, 10 minutes)

1. `sessions completed` should equal the number of trading days since 29 Sep.
   If it is short, find which day is missing and log it as a `SYSTEM_ERROR` row
   per Section 10. **A missing day that is not disclosed is the one thing that
   can invalidate the whole study.**
2. Open Sentry → Issues. Anything tagged `study_health_missed_session` is a
   missed day the watchdog caught. Resolve it only once it is logged.
3. Skim `study/sessions.csv`. The `config_fingerprint` column should be the
   **same value every row**. A change means the running configuration changed —
   deliberate or not.
4. Diff `study/env/` between the newest file and the oldest. Any library that
   changed version is a potential behaviour change you now have a record of.
5. Check the Alpaca paper account is still funded and the positions match
   `study/open_positions.json`.

---

## PART 4 — When something breaks

| Symptom | Cause | Do this |
|---|---|---|
| GitHub Desktop: "file is already on there" / repo won't commit | stale `.git/*.lock` | run `DAILY.bat` — it clears locks older than 10 min. Then fix 1.1 properly. |
| `sessions completed` did not go up | session missed or failed | GitHub → Actions → Study session → read the failed run. Log a SYSTEM_ERROR row for that date. Do **not** re-run it later in the day and pretend it was on time. |
| Sentry issue `study_health_missed_session` | watchdog caught a missed day | as above |
| `conformance` is not `OK` | running config ≠ recorded v2 rules | **stop.** Do not trade. Read the message; it names which of the three places disagree. |
| Session ran but placed nothing | normal — the pass rate is ~3.6% | nothing to do. This is the finding, not a fault. |
| Workflow fails on `pip install` | a dependency published a broken release | check `study/env/` for the last good version set and pin that one release, logged as an amendment |
| Alpaca 401 / unauthorized | keys rotated or expired | regenerate in Alpaca, update the repo secrets `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` |
| Codecov red | new code without tests | expected. `fail_ci_if_error: false`, so it never blocks. Write the test. |

---

## PART 5 — Dates that will bite

| Date | What happens |
|---|---|
| **2026-11-09** | session 30 — halfway. Good point for an interim write-up. |
| **2026-11-26** | **Thanksgiving. Market closed.** No session. Not a missed day — do not log it as one. |
| **2026-11-27** | **Early close, 1pm ET = 18:00 UTC.** Nine of the 24 crons fire after that and will hit a closed market. Seven fire before 14:00 UTC, so a session should still land — but check this day specifically. |
| **2026-12-22** | session 60. The study ends. |

Christmas (25 Dec) and the 24 Dec early close both fall *after* session 60.

---

## PART 6 — The line you do not cross

PROTOCOL Section 11 permits **defect corrections** and prohibits, without
exception, any change motivated by results, by a deadline, or intended to
produce more trades, faster results, or better-looking data.

**Allowed** — fixing the BTC symbol mismatch, removing an untradeable index,
wrapping a crash so alerting cannot kill a session, recording what versions ran,
adding tests, clearing lock files.

**Not allowed** — lowering the 2.0 R:R floor, widening the entry zone,
loosening the probability buffer or the sector cap, adding symbols to get more
candidates. The pass rate is 3 entries out of 83 evaluated. That is the result.
Changing a threshold to improve it destroys the study and hands a reviewer the
exact criticism the UROP proposal makes of other people's work.

Every permitted change gets a dated line in the amendment log saying what
changed and why. If you cannot write that sentence honestly, do not make the
change.
