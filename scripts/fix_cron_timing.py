#!/usr/bin/env python3
"""Let the study session survive GitHub's scheduler delay.

Section 11 defect correction, 2026-10-07. Infrastructure only: no strategy
parameter, gate, threshold, universe entry, sizing rule or log schema changes.
It alters only WHEN a run may start and how long it may wait for the bell.

THE DELAY, MEASURED
-------------------
    2026-09-30   13:22Z due -> 14:05Z fired     35 min
    2026-10-01   13:22Z due -> 19:00Z fired    5h 38m
    2026-10-02   13:22Z due -> 18:27Z fired    5h 05m
    2026-10-05   13:22Z due -> 22:08Z fired    8h 46m   <- after the close
    2026-10-06   13:22Z due -> 18:55Z fired    5h 33m

A 5-to-9 hour delay is now the norm. GitHub community discussion 201738 matches
this fingerprint exactly: new accounts and low-traffic public repos are
scheduled at lower priority, and the delayed events arrive BATCHED in a fixed
window regardless of the minute each cron is written for. That is why the 24
entries added on 2026-09-24 bought nothing -- they do not spread out, they all
land in the same late batch.

WHY THE EXISTING SETUP CANNOT BE RESCUED
----------------------------------------
Two values conspire:

  * the earliest cron is 13:22Z, 8 minutes before the 13:30Z open
  * wait_for_market_open.py gives up if the open is more than 8 MINUTES away

A run arriving early exits instead of waiting for the bell; a run arriving late
has already missed the open. No arrival time yields an open-price entry except
a near-perfect one, and near-perfect has happened once in five sessions.

THE FIX: SCHEDULE EARLY, THEN SLEEP
-----------------------------------
  1. Add 8 cron entries from 09:31Z to 13:11Z -- up to 5 hours BEFORE the open.
     The existing 13:22Z-21:14Z entries are KEPT (see guardrails below).
  2. Raise the market-open wait cap from 8 to 300 minutes.
  3. Raise timeout-minutes from 45 to 340 so the job is allowed to wait.

Both failure directions are then covered:

  * GitHub on time   -> the run starts hours early and sleeps until the bell
  * GitHub 5-9h late -> an early entry lands at or just after the open

2026-10-05's 8h46m delay applied to the new 09:31Z entry lands at 18:17Z,
inside the session, instead of 22:08Z after the close.

RESPECTING THE 2026-09-24 GUARDRAILS
------------------------------------
tests/test_study_workflow.py pins the shape of the previous correction, and
those pins are why the early entries are ADDED rather than swapped in:

  * >= 12 attempts per day                      -> 32 after this change
  * an attempt at or before 13:30Z              -> 09:31Z
  * an attempt at or after 20:00Z               -> the existing 20:48Z, 21:14Z
    (post-DST the session runs to 21:00Z, so the late entries still earn their
    place and must not be removed)
  * no gap over 35 minutes between attempts     -> max gap 33 min
  * no cron on minute :00                       -> none

TIMING ARITHMETIC
-----------------
test_wait_and_job_timeouts_leave_fetch_and_evaluation_room requires

    timeout_minutes - (wait_minutes + 6 + 20) >= 10

The first draft of this script used wait=320, timeout=355, leaving 9 minutes.
That test caught it. With wait=300 the reserve is 340 - 326 = 14 minutes.

The cap of 300 also has to cover the longest real wait: the 09:31Z entry
against a 14:30Z open after the 2026-11-01 DST change is 299 minutes. And
300 + 26 = 326 minutes stays under GitHub's hard 6-hour (360 min) job ceiling.
Those three constraints are what fix the numbers; do not change one alone.

TWO TEST ASSERTIONS ARE UPDATED
-------------------------------
This script edits tests/test_market_open_wait.py and
tests/test_study_workflow.py so the pinned wait bound becomes 300/340 instead
of 8/45. That is deliberate and it is the only part of this change that
rewrites an existing guardrail. The guardrail is not removed -- it is re-aimed
at the new bound, and the arithmetic assertion that caught the error above is
left exactly as it is.

Idempotent. Run from the repository root.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# UTC. Each entry fires at or before both the EDT (13:30Z) and EST (14:30Z)
# opens; the in-job wait absorbs the difference, so no DST edit is needed
# partway through the study window.
EARLY_CRONS = [
    "31 9 * * 1-5",
    "4 10 * * 1-5",
    "36 10 * * 1-5",
    "8 11 * * 1-5",
    "39 11 * * 1-5",
    "12 12 * * 1-5",
    "44 12 * * 1-5",
    "11 13 * * 1-5",
]

WAIT_CAP_MINUTES = 300
TIMEOUT_MINUTES = 340

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "study-session.yml"
WAITER = ROOT / "scripts" / "wait_for_market_open.py"
TEST_WAIT = ROOT / "tests" / "test_market_open_wait.py"
TEST_WORKFLOW = ROOT / "tests" / "test_study_workflow.py"

NOTE_MARK = "# RETIMED 2026-10-07"

COMMIT_MSG_PATH = ROOT / ".git" / "cronfix-commit-msg.txt"

COMMIT_MSG = f"""study: retime the session schedule so it survives GitHub's cron delay

Section 11 defect correction. Infrastructure only -- no strategy parameter,
gate, threshold, universe entry, sizing rule or log schema is touched. This
changes only when a run may start and how long it may wait for the open.

Measured delay between a cron entry's written time and its delivery:

  2026-09-30   13:22Z due -> 14:05Z fired     35 min
  2026-10-01   13:22Z due -> 19:00Z fired    5h 38m
  2026-10-02   13:22Z due -> 18:27Z fired    5h 05m
  2026-10-05   13:22Z due -> 22:08Z fired    8h 46m  (after the close; day lost)
  2026-10-06   13:22Z due -> 18:55Z fired    5h 33m

The 24 entries added on 2026-09-24 did not help because delayed schedule
events arrive batched in a fixed window regardless of the minute each entry
is written for (GitHub community discussion 201738: new accounts and
low-traffic public repos are scheduled at lower priority).

Two values made the day unrecoverable either way: the earliest cron was
13:22Z, eight minutes before the open, and wait_for_market_open.py abandoned
the wait if the open was more than eight minutes away. A run arriving early
exited; a run arriving late had already missed the open.

Changes:
  - add 8 cron entries from 09:31Z to 13:11Z, up to 5h before the open
  - keep the 13:22Z-21:14Z entries (they cover the post-DST 21:00Z close)
  - market-open wait cap 8 min -> {WAIT_CAP_MINUTES} min
  - timeout-minutes 45 -> {TIMEOUT_MINUTES}

Applied to 2026-10-05's 8h46m delay, the new 09:31Z entry lands at 18:17Z,
inside the session, instead of 22:08Z after the close.

The 2026-09-24 guardrails in tests/test_study_workflow.py still pass: 32
attempts, one before 13:30Z, one after 20:00Z, max gap 33 min, none on :00,
and timeout - (wait + 6 + 20) = {TIMEOUT_MINUTES - (WAIT_CAP_MINUTES + 26)}
minutes of reserve. That last assertion caught an earlier draft of this change
that left only 9 minutes. Two pinned bounds are re-aimed from 8/45 to
{WAIT_CAP_MINUTES}/{TIMEOUT_MINUTES}; the arithmetic assertion itself is
unchanged.
"""

NOTE = f"""\
{NOTE_MARK} (Section 11 defect correction) -- the first eight entries fire
# BEFORE the open on purpose. The "Wait for market open" step holds the job
# until the bell. Scheduling early and sleeping is the only arrangement that
# survives both an on-time delivery and GitHub's observed 5-9 hour batch
# delay; see scripts/fix_cron_timing.py for the measured data and arithmetic.
# The later entries are retained to cover the post-DST session to 21:00Z.
"""


def _minute_of_day(cron: str) -> int:
    minute, hour = cron.split()[0], cron.split()[1]
    return int(hour) * 60 + int(minute)


def patch_workflow(text: str) -> tuple[str, list[str]]:
    existing = re.findall(r'- cron: "([^"]+)"', text)
    if not existing:
        raise SystemExit("no cron entries found in the workflow")

    # Keep everything from the original 13:22Z onward; the late entries cover
    # the EST session and are pinned by test_attempts_span_both_dst_regimes.
    kept = [c for c in existing if _minute_of_day(c) >= _minute_of_day("22 13 * * 1-5")]
    merged = sorted(set(EARLY_CRONS + kept), key=_minute_of_day)

    lines = text.splitlines()
    out: list[str] = []
    seen = False
    dropping = False
    for line in lines:
        if not seen and re.match(r"^\s*schedule:\s*$", line):
            out.append(line)
            out.extend(NOTE.rstrip("\n").splitlines())
            out.extend(f'    - cron: "{c}"' for c in merged)
            seen = True
            dropping = True
            continue
        if dropping:
            # Inside a schedule list only cron entries and comments appear, so
            # consuming both is what makes a second run a no-op instead of
            # doubling the list.
            if re.match(r"^\s*-\s*cron:", line) or re.match(r"^\s*#", line):
                continue
            dropping = False
        out.append(line)

    if not seen:
        raise SystemExit("could not find a 'schedule:' block in the workflow")

    result = "\n".join(out) + ("\n" if text.endswith("\n") else "")
    result, n = re.subn(
        r"^(\s*)timeout-minutes:\s*\d+\s*$",
        rf"\g<1>timeout-minutes: {TIMEOUT_MINUTES}",
        result,
        flags=re.MULTILINE,
    )
    if not n:
        raise SystemExit("could not find timeout-minutes in the workflow")

    return result, [
        f"schedule -> {len(merged)} entries, {merged[0]} .. {merged[-1]}",
        f"timeout-minutes -> {TIMEOUT_MINUTES}",
    ]


def patch_waiter(text: str) -> tuple[str, list[str]]:
    new, n = re.subn(
        r"max_wait_seconds:\s*float\s*=\s*\d+\s*\*\s*60",
        f"max_wait_seconds: float = {WAIT_CAP_MINUTES} * 60",
        text,
    )
    if not n:
        raise SystemExit("could not find max_wait_seconds in wait_for_market_open.py")
    return new, [f"max_wait_seconds -> {WAIT_CAP_MINUTES} min"]


def patch_tests(wait_text: str, wf_text: str) -> tuple[str, str, list[str]]:
    changes: list[str] = []

    # tests/test_market_open_wait.py: the "exceeds eight minutes" case is now
    # the "exceeds the wait cap" case.
    new_wait = wait_text.replace(
        "def test_wait_skips_when_next_open_exceeds_eight_minutes():",
        "def test_wait_skips_when_next_open_exceeds_the_wait_cap():",
    )
    new_wait = re.sub(
        r"(def test_wait_skips_when_next_open_exceeds_the_wait_cap\(\):.*?"
        r"next_open = now \+ dt\.timedelta\(minutes=)\d+",
        rf"\g<1>{WAIT_CAP_MINUTES + 1}",
        new_wait,
        flags=re.DOTALL,
    )
    if new_wait != wait_text:
        changes.append(
            f"test_market_open_wait.py: skip case -> {WAIT_CAP_MINUTES + 1} min")

    # tests/test_study_workflow.py: re-aim the pinned bound. The arithmetic
    # assertion below it is left alone -- it is the check that caught the
    # first draft of this change.
    new_wf = re.sub(r"assert wait_minutes == \d+",
                    f"assert wait_minutes == {WAIT_CAP_MINUTES}", wf_text)
    new_wf = re.sub(r"assert timeout_minutes == \d+",
                    f"assert timeout_minutes == {TIMEOUT_MINUTES}", new_wf)
    if new_wf != wf_text:
        changes.append(
            f"test_study_workflow.py: pinned bound -> "
            f"{WAIT_CAP_MINUTES}/{TIMEOUT_MINUTES}")

    return new_wait, new_wf, changes


def verify(workflow_text: str) -> None:
    """Re-run the repo's own guardrails here, so a bad patch never gets pushed."""
    crons = re.findall(r'- cron: "([^"]+)"', workflow_text)
    mins = sorted(_minute_of_day(c) for c in crons)
    gaps = [b - a for a, b in zip(mins, mins[1:])]

    checks = [
        ("at least 12 attempts", len(crons) >= 12, len(crons)),
        ("an attempt at or before 13:30Z", mins[0] <= 13 * 60 + 30, mins[0]),
        ("an attempt at or after 20:00Z", mins[-1] >= 20 * 60, mins[-1]),
        ("no gap over 35 min", max(gaps) <= 35, max(gaps)),
        ("no cron on minute :00", all(c.split()[0] != "0" for c in crons), "-"),
        ("timeout reserves >= 10 min",
         TIMEOUT_MINUTES - (WAIT_CAP_MINUTES + 6 + 20) >= 10,
         TIMEOUT_MINUTES - (WAIT_CAP_MINUTES + 26)),
        ("worst case under GitHub's 360 min job ceiling",
         WAIT_CAP_MINUTES + 26 < 360, WAIT_CAP_MINUTES + 26),
        ("earliest entry waits <= the cap (post-DST 14:30Z open)",
         14 * 60 + 30 - mins[0] <= WAIT_CAP_MINUTES, 14 * 60 + 30 - mins[0]),
    ]
    bad = [(n, v) for n, ok, v in checks if not ok]
    for name, ok, value in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}  ({value})")
    if bad:
        raise SystemExit(f"guardrail check failed: {bad}")


def main() -> int:
    paths = (WORKFLOW, WAITER, TEST_WAIT, TEST_WORKFLOW)
    missing = [p for p in paths if not p.exists()]
    if missing:
        print("missing: " + ", ".join(str(p) for p in missing), file=sys.stderr)
        return 1

    wf_old = WORKFLOW.read_text(encoding="utf-8")
    wt_old = WAITER.read_text(encoding="utf-8")
    tw_old = TEST_WAIT.read_text(encoding="utf-8")
    tf_old = TEST_WORKFLOW.read_text(encoding="utf-8")

    wf_new, wf_ch = patch_workflow(wf_old)
    wt_new, wt_ch = patch_waiter(wt_old)
    tw_new, tf_new, t_ch = patch_tests(tw_old, tf_old)

    if (wf_new, wt_new, tw_new, tf_new) == (wf_old, wt_old, tw_old, tf_old):
        print("already patched; nothing to do")
        verify(wf_old)
        return 0

    for path, new, old, label in (
        (WORKFLOW, wf_new, wf_old, wf_ch),
        (WAITER, wt_new, wt_old, wt_ch),
        (TEST_WAIT, tw_new, tw_old, []),
        (TEST_WORKFLOW, tf_new, tf_old, []),
    ):
        if new != old:
            path.write_text(new, encoding="utf-8")
    for c in wf_ch + wt_ch + t_ch:
        print(f"  {c}")

    print("\nguardrails:")
    verify(wf_new)

    try:
        import yaml  # noqa: PLC0415

        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        trig = doc.get("on") or doc.get(True) or {}  # YAML 1.1 reads `on` as True
        print(f"  [ok] YAML parses; workflow_dispatch present: "
              f"{'workflow_dispatch' in trig}")
    except ImportError:
        print("  (pyyaml not installed; skipped the parse check)")

    COMMIT_MSG_PATH.write_text(COMMIT_MSG, encoding="utf-8")
    print(f"  [ok] commit message written to {COMMIT_MSG_PATH.name}")

    print("\nPatched. Commit and push, then confirm on the Actions tab.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
