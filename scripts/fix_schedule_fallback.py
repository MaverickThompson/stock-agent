#!/usr/bin/env python3
"""Demote GitHub's cron from primary trigger to a thin fallback.

Section 11 defect correction, 2026-10-07. Infrastructure only: no strategy
parameter, gate, threshold, universe entry, sizing rule or log schema changes.
It alters only WHEN a run may start and how long it may wait for the open.

WHY THIS SUPERSEDES scripts/fix_cron_timing.py
----------------------------------------------
That script added eight early cron entries, taking the schedule to 32. It was
the right answer while GitHub's `schedule` was the only trigger available. It
is the wrong answer now: the session is started by an external scheduler
(cron-job.org) POSTing to the workflow_dispatch API at 9:31am ET, which is a
direct API call and is not subject to the delay that afflicts scheduled
events on this repository.

Measured delay on `schedule` events here:

    2026-09-30   13:22Z due -> 14:05Z fired     35 min
    2026-10-01   13:22Z due -> 19:00Z fired    5h 38m
    2026-10-02   13:22Z due -> 18:27Z fired    5h 05m
    2026-10-05   13:22Z due -> 22:08Z fired    8h 46m   (after the close)
    2026-10-06   13:22Z due -> 18:55Z fired    5h 33m

With a reliable primary trigger, 24 entries stop being insurance and become
noise: each one arrives hours late, finds the day already recorded, and exits
in about 30 seconds. On 2026-10-06 that produced 31 workflow runs for one
trading day, five of them cancelled against the concurrency group.

Three entries are kept so that a cron-job.org outage still has a chance of
catching the day. They are placed inside the trading window in BOTH daylight
regimes, since the study crosses the 2026-11-01 change (open 13:30Z -> 14:30Z,
close 20:00Z -> 21:00Z).

WHAT CHANGES
------------
    cron entries        24  ->  3   (13:26Z, 16:33Z, 19:11Z)
    market-open wait     8  ->  90 minutes
    timeout-minutes     45  ->  135

The wait cap of 90 exists for one reason: the 13:26Z entry sits 64 minutes
before the post-DST 14:30Z open, so without a wait longer than that it would
exit instead of holding for the bell. 90 covers it with margin. The timeout
then has to clear 90 + 6 (fetch) + 20 (evaluation) + 10 (reserve) = 126, so
135 is the smallest round value that satisfies the repository's own
arithmetic assertion, and it sits far below GitHub's 6-hour job ceiling.

THE 2026-09-24 GUARDRAILS ARE REPLACED, NOT REMOVED
---------------------------------------------------
tests/test_study_workflow.py pinned the shape of the previous correction:

    test_the_session_is_attempted_many_times_a_day   >= 12 cron entries
    test_attempts_span_both_dst_regimes              span 13:30Z..20:00Z,
                                                     no gap over 35 minutes

Both encode "cron is the only trigger", which is no longer true, so both are
rewritten rather than deleted. The replacements bite harder in the new design:

  * workflow_dispatch must exist -- the primary trigger cannot be removed
  * between 2 and 6 cron entries -- catches a regression back to 24 AND a
    regression down to zero fallback
  * every entry must fall inside the trading window in both DST regimes, or
    close enough before the open that the wait absorbs it
  * the wait cap must cover the earliest entry's gap to the post-DST open --
    the arithmetic that actually decides whether a fallback works
  * the timeout-reserve assertion is kept untouched; it is the check that
    caught a bad draft of the previous correction

Idempotent. Run from the repository root.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# UTC. Open/close is 13:30Z/20:00Z until 2026-10-31 and 14:30Z/21:00Z from
# 2026-11-01. Every entry below is inside the window in at least one regime
# and within the wait cap of the open in the other.
FALLBACK_CRONS = [
    "26 13 * * 1-5",   # 4 min before the EDT open; waits 64 min for the EST open
    "33 16 * * 1-5",   # mid-session in both regimes
    "11 19 * * 1-5",   # late session in both regimes
]

WAIT_CAP_MINUTES = 90
TIMEOUT_MINUTES = 135

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "study-session.yml"
WAITER = ROOT / "scripts" / "wait_for_market_open.py"
TEST_WAIT = ROOT / "tests" / "test_market_open_wait.py"
TEST_WORKFLOW = ROOT / "tests" / "test_study_workflow.py"
SUPERSEDED = ROOT / "scripts" / "fix_cron_timing.py"
COMMIT_MSG_PATH = ROOT / ".git" / "fallback-commit-msg.txt"

NOTE = """\
# FALLBACK ONLY, set 2026-10-07 (Section 11 defect correction). The session is
# started by an external scheduler POSTing to workflow_dispatch at 9:31am ET.
# GitHub's own schedule events arrive 5-9 hours late on this repository, so
# these three entries exist only to give a day a chance if that external
# trigger fails. They sit inside the trading window in both daylight regimes;
# the "Wait for market open" step absorbs the pre-open gap. See
# scripts/fix_schedule_fallback.py for the measured data and the arithmetic.
"""


def _minute_of_day(cron: str) -> int:
    parts = cron.split()
    return int(parts[1]) * 60 + int(parts[0])


def patch_workflow(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    out: list[str] = []
    seen = False
    dropping = False
    for line in lines:
        if not seen and re.match(r"^\s*schedule:\s*$", line):
            out.append(line)
            out.extend(NOTE.rstrip("\n").splitlines())
            out.extend(f'    - cron: "{c}"' for c in FALLBACK_CRONS)
            seen = True
            dropping = True
            continue
        if dropping:
            # Only cron entries and comments live inside a schedule list, so
            # consuming both is what makes a second run a no-op.
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

    changed = []
    if result != text:
        changed = [f"schedule -> {len(FALLBACK_CRONS)} fallback entries",
                   f"timeout-minutes -> {TIMEOUT_MINUTES}"]
    return result, changed


def patch_waiter(text: str) -> tuple[str, list[str]]:
    new, n = re.subn(
        r"max_wait_seconds:\s*float\s*=\s*\d+\s*\*\s*60",
        f"max_wait_seconds: float = {WAIT_CAP_MINUTES} * 60",
        text,
    )
    if not n:
        raise SystemExit("could not find max_wait_seconds in wait_for_market_open.py")
    return new, [f"max_wait_seconds -> {WAIT_CAP_MINUTES} min"] if new != text else []


NEW_GUARDRAILS = '''

def test_workflow_dispatch_is_available() -> None:
    """The primary trigger is an external POST to workflow_dispatch.

    GitHub's schedule events arrive 5-9 hours late on this repository, so the
    session is started by an external scheduler. Removing workflow_dispatch
    would remove the only reliable way to start a day.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow, (
        "workflow_dispatch is the primary trigger and must not be removed")


def test_cron_is_a_thin_fallback_not_the_primary_trigger() -> None:
    """Between two and six entries.

    Fewer than two leaves no fallback if the external scheduler fails. More
    than about six recreates the 2026-10-06 problem, where 24 late arrivals
    produced 31 workflow runs for a single trading day and five cancellations
    against the concurrency group.
    """
    crons = _crons()
    assert 2 <= len(crons) <= 6, (
        f"{len(crons)} cron entries; fallback should be a handful, not a swarm")


def test_every_fallback_entry_can_reach_an_open_market() -> None:
    """Each entry must be usable in both daylight regimes.

    The study window crosses 2026-11-01: the US session is 13:30-20:00Z before
    it and 14:30-21:00Z after. An entry is usable if it fires while the market
    is open, or early enough that the in-job wait still catches the bell.
    """
    import re as _re

    waiter = (pathlib.Path(__file__).parent.parent
              / "scripts" / "wait_for_market_open.py").read_text(encoding="utf-8")
    cap = _re.search(r"max_wait_seconds:\\s*float\\s*=\\s*(\\d+)\\s*\\*\\s*60", waiter)
    assert cap, "market-open wait bound is not explicit in minutes"
    wait_minutes = int(cap.group(1))

    for cron in _crons():
        fired = _minute_of_day_for_test(cron)
        for open_m, close_m, label in ((13 * 60 + 30, 20 * 60, "EDT"),
                                       (14 * 60 + 30, 21 * 60, "EST")):
            if open_m <= fired < close_m:
                continue  # fires inside the session
            gap = open_m - fired
            assert 0 < gap <= wait_minutes, (
                f"{cron} is unusable in {label}: fires {fired} min into the day, "
                f"open at {open_m}, wait cap {wait_minutes} min")


def _minute_of_day_for_test(cron: str) -> int:
    parts = cron.split()
    return int(parts[1]) * 60 + int(parts[0])
'''


def patch_tests(wf_text: str, wait_text: str) -> tuple[str, str, list[str]]:
    changes: list[str] = []

    # Retire the two assertions that encode "cron is the only trigger".
    old_many = '''def test_the_session_is_attempted_many_times_a_day() -> None:
    """One scheduled time cannot survive a scheduler that is hours late."""
    assert len(_crons()) >= 12, (
        "too few attempts to reliably land inside market hours")'''
    old_span = '''def test_attempts_span_both_dst_regimes() -> None:
    """The window crosses 2026-11-01: the US open moves 13:30 -> 14:30 UTC."""
    minutes = sorted(int(c.split()[1]) * 60 + int(c.split()[0]) for c in _crons())
    assert minutes[0] <= 13 * 60 + 30, "no attempt before the EDT open"
    assert minutes[-1] >= 20 * 60 + 0, "no attempt late enough for the EST session"
    # No gap wide enough for a whole trading session to slip through.
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    assert max(gaps) <= 35, f"largest gap between attempts is {max(gaps)} minutes"'''

    if old_many in wf_text:
        wf_text = wf_text.replace(old_many, "", 1)
        changes.append("retired test_the_session_is_attempted_many_times_a_day")
    if old_span in wf_text:
        wf_text = wf_text.replace(old_span, "", 1)
        changes.append("retired test_attempts_span_both_dst_regimes")

    if "test_cron_is_a_thin_fallback_not_the_primary_trigger" not in wf_text:
        anchor = "def test_crons_avoid_the_top_of_the_hour() -> None:"
        if anchor not in wf_text:
            raise SystemExit("could not find an anchor for the new guardrails")
        wf_text = wf_text.replace(anchor, NEW_GUARDRAILS.strip("\n") + "\n\n\n" + anchor, 1)
        changes.append("added 3 replacement guardrails (dispatch, 2-6 entries, "
                       "window reachability)")

    # Re-aim the pinned bound. The arithmetic assertion under it is untouched.
    wf_text = re.sub(r"assert wait_minutes == \d+",
                     f"assert wait_minutes == {WAIT_CAP_MINUTES}", wf_text)
    wf_text = re.sub(r"assert timeout_minutes == \d+",
                     f"assert timeout_minutes == {TIMEOUT_MINUTES}", wf_text)

    # The wait test's skip case has to exceed the new cap.
    wait_text = wait_text.replace(
        "def test_wait_skips_when_next_open_exceeds_eight_minutes():",
        "def test_wait_skips_when_next_open_exceeds_the_wait_cap():")
    wait_text = re.sub(
        r"(def test_wait_skips_when_next_open_exceeds_the_wait_cap\(\):.*?"
        r"next_open = now \+ dt\.timedelta\(minutes=)\d+",
        rf"\g<1>{WAIT_CAP_MINUTES + 1}", wait_text, flags=re.DOTALL)

    # The "following session" case must still be beyond the cap. 17.5h is.
    return wf_text, wait_text, changes


def neutralise_superseded() -> list[str]:
    """Replace the old script with a stub that explains why it must not run.

    Prepending a banner to the original was the first attempt and it produced a
    SyntaxError, because the original opens with `from __future__ import
    annotations`, which must be the first statement in a file. A stub is both
    correct and easier to read.
    """
    if not SUPERSEDED.exists():
        return []
    if SUPERSEDED.read_text(encoding="utf-8").lstrip().startswith('"""SUPERSEDED'):
        return []
    SUPERSEDED.write_text(
        '"""SUPERSEDED on 2026-10-07 by scripts/fix_schedule_fallback.py.\n'
        "\n"
        "This script added eight early cron entries, taking the schedule to 32.\n"
        "That was the right answer while GitHub cron was the only trigger. The\n"
        "session is now started by an external scheduler POSTing to the\n"
        "workflow_dispatch API at 9:31am ET, so additional cron entries are noise\n"
        "rather than insurance: each arrives hours late, finds the day already\n"
        "recorded, and exits in about 30 seconds.\n"
        "\n"
        "Running this would undo the current three-entry fallback schedule.\n"
        'Kept as a stub so the git history explains itself.\n"""\n'
        "\n"
        "import sys\n"
        "\n"
        'print(\n'
        '    "SUPERSEDED: run scripts/fix_schedule_fallback.py instead. "\n'
        '    "This script adds cron entries, which is no longer the fix.",\n'
        "    file=sys.stderr,\n"
        ")\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    return ["fix_cron_timing.py: replaced with a stub that refuses to run"]


COMMIT_MSG = f"""\
study: demote GitHub cron to a thin fallback

Section 11 defect correction. Infrastructure only -- no strategy parameter,
gate, threshold, universe entry, sizing rule or log schema is touched.

The session is now started by an external scheduler POSTing to GitHub's
workflow_dispatch API at 9:31am ET. That is a direct API call and is not
subject to the delay that scheduled events suffer on this repository:

  2026-09-30   13:22Z due -> 14:05Z fired     35 min
  2026-10-01   13:22Z due -> 19:00Z fired    5h 38m
  2026-10-02   13:22Z due -> 18:27Z fired    5h 05m
  2026-10-05   13:22Z due -> 22:08Z fired    8h 46m  (after the close)
  2026-10-06   13:22Z due -> 18:55Z fired    5h 33m

With a reliable primary trigger, the 24 cron entries added on 2026-09-24 stop
being insurance and become noise. Each arrives hours late, finds the day
already recorded, and exits in about 30 seconds. On 2026-10-06 that produced
31 workflow runs for one trading day, five of them cancelled against the
concurrency group.

  cron entries      24 -> 3 (13:26Z, 16:33Z, 19:11Z)
  market-open wait   8 -> {WAIT_CAP_MINUTES} minutes
  timeout-minutes   45 -> {TIMEOUT_MINUTES}

Three entries remain so a failure of the external scheduler still has a
chance of catching the day. All three are inside the trading window in at
least one daylight regime and within the wait cap of the open in the other,
which matters because the study crosses 2026-11-01.

The wait cap of {WAIT_CAP_MINUTES} is set by the 13:26Z entry, which sits 64
minutes before the post-DST 14:30Z open. The timeout then has to clear
{WAIT_CAP_MINUTES} + 6 + 20 + 10 = {WAIT_CAP_MINUTES + 36}, so {TIMEOUT_MINUTES}
is the smallest round value that satisfies the repository's own reserve
assertion, and it is well under GitHub's 6-hour job ceiling.

The two 2026-09-24 guardrails that encoded "cron is the only trigger" are
replaced rather than deleted. The new ones assert that workflow_dispatch
exists, that the entry count stays between 2 and 6, and that every entry can
reach an open market in both daylight regimes given the wait cap. The
timeout-reserve assertion is left exactly as it was.

scripts/fix_cron_timing.py is marked superseded and now refuses to run.
"""


def verify(workflow_text: str) -> None:
    crons = re.findall(r'- cron: "([^"]+)"', workflow_text)
    checks = [
        ("workflow_dispatch present", "workflow_dispatch:" in workflow_text, "-"),
        ("2-6 cron entries", 2 <= len(crons) <= 6, len(crons)),
        ("no cron on minute :00", all(c.split()[0] != "0" for c in crons), "-"),
        ("timeout reserves >= 10 min",
         TIMEOUT_MINUTES - (WAIT_CAP_MINUTES + 26) >= 10,
         TIMEOUT_MINUTES - (WAIT_CAP_MINUTES + 26)),
        ("worst case under the 360 min job ceiling",
         WAIT_CAP_MINUTES + 26 < 360, WAIT_CAP_MINUTES + 26),
    ]
    for cron in crons:
        fired = _minute_of_day(cron)
        for open_m, close_m, label in ((810, 1200, "EDT"), (870, 1260, "EST")):
            ok = (open_m <= fired < close_m) or (0 < open_m - fired <= WAIT_CAP_MINUTES)
            checks.append((f"{cron} usable in {label}", ok, fired))

    bad = [(n, v) for n, ok, v in checks if not ok]
    for name, ok, value in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}  ({value})")
    if bad:
        raise SystemExit(f"guardrail check failed: {bad}")


def main() -> int:
    missing = [p for p in (WORKFLOW, WAITER, TEST_WAIT, TEST_WORKFLOW)
               if not p.exists()]
    if missing:
        print("missing: " + ", ".join(str(p) for p in missing), file=sys.stderr)
        return 1

    wf_old = WORKFLOW.read_text(encoding="utf-8")
    wt_old = WAITER.read_text(encoding="utf-8")
    tf_old = TEST_WORKFLOW.read_text(encoding="utf-8")
    tw_old = TEST_WAIT.read_text(encoding="utf-8")

    wf_new, wf_ch = patch_workflow(wf_old)
    wt_new, wt_ch = patch_waiter(wt_old)
    tf_new, tw_new, t_ch = patch_tests(tf_old, tw_old)
    sup_ch = neutralise_superseded()

    unchanged = (wf_new == wf_old and wt_new == wt_old
                 and tf_new == tf_old and tw_new == tw_old and not sup_ch)
    if unchanged:
        print("already patched; nothing to do")
        verify(wf_old)
        return 0

    for path, new, old in ((WORKFLOW, wf_new, wf_old), (WAITER, wt_new, wt_old),
                           (TEST_WORKFLOW, tf_new, tf_old), (TEST_WAIT, tw_new, tw_old)):
        if new != old:
            path.write_text(new, encoding="utf-8")

    for c in wf_ch + wt_ch + t_ch + sup_ch:
        print(f"  {c}")

    print("\nguardrails:")
    verify(wf_new)

    try:
        import yaml  # noqa: PLC0415

        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        trig = doc.get("on") or doc.get(True) or {}  # YAML 1.1 reads `on` as True
        print(f"  [ok] YAML parses; {len(trig.get('schedule') or [])} entries; "
              f"dispatch: {'workflow_dispatch' in trig}")
    except ImportError:
        print("  (pyyaml not installed; skipped the parse check)")

    import py_compile  # noqa: PLC0415

    for p in (WAITER, TEST_WORKFLOW, TEST_WAIT):
        py_compile.compile(str(p), doraise=True)
    print("  [ok] python files compile")

    COMMIT_MSG_PATH.write_text(COMMIT_MSG, encoding="utf-8")
    print(f"  [ok] commit message written to {COMMIT_MSG_PATH.name}")

    print("\nPatched. Commit and push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
