"""SUPERSEDED on 2026-10-07 by scripts/fix_schedule_fallback.py.

This script added eight early cron entries, taking the schedule to 32.
That was the right answer while GitHub cron was the only trigger. The
session is now started by an external scheduler POSTing to the
workflow_dispatch API at 9:31am ET, so additional cron entries are noise
rather than insurance: each arrives hours late, finds the day already
recorded, and exits in about 30 seconds.

Running this would undo the current three-entry fallback schedule.
Kept as a stub so the git history explains itself.
"""

import sys

print(
    "SUPERSEDED: run scripts/fix_schedule_fallback.py instead. "
    "This script adds cron entries, which is no longer the fix.",
    file=sys.stderr,
)
raise SystemExit(2)
