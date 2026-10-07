#!/usr/bin/env python3
"""Stop a failed-but-traded session from being re-run by later cron arrivals.

Section 11 defect correction, 2026-10-07. Infrastructure only: no strategy
parameter, gate, threshold, universe entry, sizing rule or log schema changes.

WHAT HAPPENED ON 2026-10-06
---------------------------
signals.csv holds 100 rows for that date. Every other study day holds 20.

    2026-09-24   20
    2026-09-25   20
    2026-09-29   20
    2026-09-30   20
    2026-10-01   20
    2026-10-02   20
    2026-10-06  100     <- the same 20 evaluations, logged five times

Run #164 evaluated the universe, entered GLW and AVGO, and then exited
non-zero. The workflow's commit step stages the study state only when
`run_session.conclusion == 'success'`:

    git add study/signals.csv study/trades.csv ...          # always
    if [ "$conclusion" = "success" ]; then
        git add study/v2_state.json study/last_session.txt ...
    fi

So signals.csv gained its rows and `v2_state.json` / `last_session.txt` did
not. check_study_eligibility.py reads only those two, so every later cron
arrival concluded the day had not happened and re-ran the whole session:

    #165   927s   failure
    #171  1452s   failure
    #172   998s   failure
    #173   980s   failure

plus three more cancelled mid-run against the concurrency group.

WHAT DID NOT BREAK
------------------
The order layer defended itself. signals.csv shows

    GLW  | duplicate_entry_action | SKIPPED   x4
    AVGO | duplicate_entry_action | SKIPPED   x4

and trades.csv holds exactly two rows for the date. No position was opened
twice. The damage is confined to the signal log.

That damage still matters: PROTOCOL Rule 3 requires every signal to be
logged precisely so rejection statistics are honest, and five-fold
duplication makes any naive rejection rate wrong.

THE FIX
-------
Give the guard a witness that survives a failure. signals.csv is committed
`if: always()`, so rows dated today are proof the day was already attempted
regardless of what the state files say.

    today has rows in signals.csv  ->  skip

Scope is deliberately narrow:

  * a run that fails BEFORE logging anything leaves no rows, so the day can
    still be retried -- which is what you want when a data fetch dies early
  * a run that fails AFTER evaluating is not retried, because re-evaluating
    a day whose orders are already placed produces duplicate log rows and
    leans on the duplicate-entry guard as a last line of defence

Per Section 10 an incomplete attempt does not advance progress, so the day
is still recorded as incomplete and disclosed rather than silently counted.

THE 80 DUPLICATE ROWS ARE LEFT ALONE
------------------------------------
PROTOCOL Rule 8 is append-only: "The trade log and weekly reviews are never
edited retroactively. Corrections are new entries." Deleting them would be a
retroactive edit of the primary record. They stay, this script appends one
SYSTEM_ERROR row documenting the duplication, and the write-up reports
de-duplicated rejection statistics with the method stated.

Idempotent. Run from the repository root.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "check_study_eligibility.py"
TEST_WORKFLOW = ROOT / "tests" / "test_study_workflow.py"
SIGNALS = ROOT / "study" / "signals.csv"

COMMIT_MSG_PATH = ROOT / ".git" / "rerunguard-commit-msg.txt"

WORKFLOW = ROOT / ".github" / "workflows" / "study-session.yml"

OLD_DEP_STEP = """\
        run: |
          mkdir -p study/env
          python -m pip freeze > "study/env/$(date -u +%Y-%m-%d).txt"
          python -c "import sys; print('python', sys.version.split()[0])" >> "study/env/$(date -u +%Y-%m-%d).txt"
"""

NEW_DEP_STEP = """\
        # Write-once per day. This step sits before the market-open wait, so it
        # runs on EVERY eligible firing -- including the many that arrive
        # outside market hours and do no session work. Rewriting the file each
        # time changed it each time (pip freeze ordering), which staged a diff
        # and produced a "study: session <date>" commit with no study content
        # in it. Twenty such commits accumulated between 2026-10-06 and
        # 2026-10-07 and made the history look like the session was running
        # over and over. The dependency set for a given day does not change
        # between firings, so recording it once is both correct and quiet.
        run: |
          mkdir -p study/env
          stamp="study/env/$(date -u +%Y-%m-%d).txt"
          if [ -f "$stamp" ]; then
            echo "dependency set already recorded for today"
          else
            python -m pip freeze > "$stamp"
            python -c "import sys; print('python', sys.version.split()[0])" >> "$stamp"
          fi
"""


def patch_dependency_step(text: str) -> tuple[str, list[str]]:
    """Stop the dependency recorder from producing a commit on every firing."""
    if "already recorded for today" in text:
        return text, []
    if OLD_DEP_STEP not in text:
        return text, ["dependency step not in the expected shape; left alone"]
    return text.replace(OLD_DEP_STEP, NEW_DEP_STEP, 1), [
        "study-session.yml: dependency set recorded once per day, not per firing"]


HELPER = '''

DEFAULT_SIGNALS_PATH = ROOT / "study" / "signals.csv"


def signals_logged_for(signals_path: pathlib.Path, today: dt.date) -> bool:
    """Whether the signal log already holds rows for ``today``.

    This is the only witness of an attempt that survives a failed run.
    ``v2_state.json`` and ``last_session.txt`` are staged by the workflow only
    when ``run_session`` succeeds, but ``signals.csv`` is staged ``if:
    always()``. On 2026-10-06 run #164 traded and then exited non-zero, so the
    state files stayed stale and four later cron arrivals re-ran the whole
    session, logging the same twenty evaluations five times.
    """
    if not signals_path.exists():
        return False
    stamp = today.isoformat()
    try:
        with signals_path.open(encoding="utf-8") as handle:
            next(handle, None)  # header row
            return any(line.startswith(stamp) for line in handle)
    except OSError:  # an unreadable log must not silently permit a re-run
        return True
'''

SYSTEM_ERROR_ROW = (
    '2026-10-06T23:59:00Z,-,system,duplicate_session_logging,SYSTEM_ERROR,'
    '"Run #164 entered GLW and AVGO then exited non-zero, so v2_state.json and '
    'last_session.txt were not staged. Four later cron arrivals (#165, #171, '
    '#172, #173) re-ran the full session and re-logged the same 20 evaluations, '
    'giving 100 rows for this date instead of 20. No position opened twice -- '
    'the duplicate_entry_action guard skipped both re-entries and trades.csv '
    'holds 2 rows. Rows are retained per Rule 8 (append-only); rejection '
    'statistics for this date must be computed on de-duplicated rows.",,'
    'logged per Section 10; guard corrected by scripts/fix_rerun_guard.py'
)


def patch_guard(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    if "signals_logged_for" in text:
        return text, changes

    # 1. the helper, after the DEFAULT_MARKER_PATH constant
    anchor = 'DEFAULT_MARKER_PATH = ROOT / "study" / "last_session.txt"'
    if anchor not in text:
        raise SystemExit("could not find DEFAULT_MARKER_PATH in the guard")
    text = text.replace(anchor, anchor + HELPER, 1)
    changes.append("added signals_logged_for()")

    # 2. the extra condition on the pure predicate
    old_sig = (
        "def is_study_eligible(\n"
        "    state: StudyState,\n"
        "    today: dt.date,\n"
        '    marker_contents: str = "",\n'
        ") -> bool:"
    )
    new_sig = (
        "def is_study_eligible(\n"
        "    state: StudyState,\n"
        "    today: dt.date,\n"
        '    marker_contents: str = "",\n'
        "    signals_logged: bool = False,\n"
        ") -> bool:"
    )
    if old_sig not in text:
        raise SystemExit("could not find the is_study_eligible signature")
    text = text.replace(old_sig, new_sig, 1)

    old_body = "        and marker_contents.strip() != today.isoformat()\n    )"
    new_body = (
        "        and marker_contents.strip() != today.isoformat()\n"
        "        # An attempt that logged rows today is not retried, even when\n"
        "        # it failed before writing the marker.\n"
        "        and not signals_logged\n"
        "    )"
    )
    if old_body not in text:
        raise SystemExit("could not find the is_study_eligible body")
    text = text.replace(old_body, new_body, 1)
    changes.append("is_study_eligible() now takes signals_logged")

    # 3. the CLI flag
    old_arg = ('    parser.add_argument("--marker", type=pathlib.Path, '
               'default=DEFAULT_MARKER_PATH)')
    new_arg = old_arg + (
        '\n    parser.add_argument(\n'
        '        "--signals", type=pathlib.Path, default=DEFAULT_SIGNALS_PATH,\n'
        '        help="signal log consulted for an already-attempted date")'
    )
    if old_arg not in text:
        raise SystemExit("could not find the --marker argument")
    text = text.replace(old_arg, new_arg, 1)

    # 4. wire it through main()
    old_call = "    eligible = is_study_eligible(state, today, marker_contents)"
    new_call = (
        "    signals_logged = signals_logged_for(args.signals, today)\n"
        "    eligible = is_study_eligible(\n"
        "        state, today, marker_contents, signals_logged)"
    )
    if old_call not in text:
        raise SystemExit("could not find the is_study_eligible call in main()")
    text = text.replace(old_call, new_call, 1)

    old_reason = '            f"status={state.status}, marker={marker_contents.strip() or \'absent\'}"'
    new_reason = (
        '            f"status={state.status}, marker={marker_contents.strip() or \'absent\'}, "\n'
        '            f"signals_logged={signals_logged}"'
    )
    if old_reason not in text:
        raise SystemExit("could not find the reason string in main()")
    text = text.replace(old_reason, new_reason, 1)
    changes.append("--signals flag wired through main()")

    return text, changes


def patch_test(text: str) -> tuple[str, list[str]]:
    """Point the existing guard test at a temp signal log and cover the new case.

    Without this the test would consult the real study/signals.csv, which holds
    rows for 2026-09-29, and its `skip=false` assertion for that date would
    fail for the wrong reason.
    """
    if "signals_path" in text:
        return text, []

    old = '''    state_path = tmp_path / "v2_state.json"
    marker_path = tmp_path / "last_session.txt"
    output_path = tmp_path / "github_output.txt"'''
    new = '''    state_path = tmp_path / "v2_state.json"
    marker_path = tmp_path / "last_session.txt"
    output_path = tmp_path / "github_output.txt"
    # An empty temp log, so this test does not consult the real study log.
    signals_path = tmp_path / "signals.csv"
    signals_path.write_text(
        "timestamp,ticker,signal_type,triggered_rule,action_taken,"
        "reason_if_rejected,price_at_signal,notes\\n",
        encoding="utf-8",
    )'''
    if old not in text:
        raise SystemExit("could not find the guard test's path setup")
    text = text.replace(old, new, 1)

    old_cmd = '''                "--state", str(state_path), "--marker", str(marker_path),'''
    new_cmd = '''                "--state", str(state_path), "--marker", str(marker_path),
                "--signals", str(signals_path),'''
    if old_cmd not in text:
        raise SystemExit("could not find the guard test's subprocess args")
    text = text.replace(old_cmd, new_cmd, 1)

    # new case: rows already logged for the date -> skip, whatever the marker says
    old_tail = '''    state_path.write_text(json.dumps(active_state), encoding="utf-8")
    marker_path.write_text("2026-09-29\\n", encoding="utf-8")
    assert "skip=true" in check("2026-09-29")'''
    new_tail = old_tail + '''

    # A session that logged rows and then failed before writing the marker must
    # not be re-run. This is the 2026-10-06 case: the marker stayed stale and
    # four later cron arrivals re-logged the same twenty evaluations.
    marker_path.write_text("", encoding="utf-8")
    with signals_path.open("a", encoding="utf-8") as handle:
        handle.write("2026-09-29T14:00:00Z,AAPL,entry_candidate,section5_gate,"
                     "REJECTED,R:R 1.90 below the 2.0 floor,100.00,\\n")
    assert "skip=true" in check("2026-09-29")'''
    if old_tail not in text:
        raise SystemExit("could not find the guard test's final assertion")
    text = text.replace(old_tail, new_tail, 1)

    return text, ["test_study_workflow.py: guard test uses a temp signal log "
                  "and covers the re-run case"]


def append_system_error_row() -> list[str]:
    if not SIGNALS.exists():
        return ["study/signals.csv not found; skipped the SYSTEM_ERROR row"]
    text = SIGNALS.read_text(encoding="utf-8")
    if "duplicate_session_logging" in text:
        return []
    if not text.endswith("\n"):
        text += "\n"
    SIGNALS.write_text(text + SYSTEM_ERROR_ROW + "\n", encoding="utf-8")
    return ["study/signals.csv: appended a SYSTEM_ERROR row for the duplication"]


COMMIT_MSG = """\
study: stop a failed-but-traded session from being re-run

Section 11 defect correction. Infrastructure only -- no strategy parameter,
gate, threshold, universe entry, sizing rule or log schema is touched.

signals.csv holds 100 rows for 2026-10-06. Every other study day holds 20.

Run #164 evaluated the universe, entered GLW and AVGO, then exited non-zero.
The workflow stages study/v2_state.json and study/last_session.txt only when
run_session succeeds, but stages study/signals.csv if: always(). So the log
gained its rows and the state files stayed stale.

check_study_eligibility.py consulted only those two state files, so four
later cron arrivals (#165, #171, #172, #173 -- 927s, 1452s, 998s, 980s)
concluded the day had not happened and re-ran the full session, re-logging
the same twenty evaluations five times. Three further runs were cancelled
mid-flight against the concurrency group.

No position was opened twice: the duplicate_entry_action guard skipped both
re-entries (GLW x4, AVGO x4 in the log) and trades.csv holds 2 rows for the
date. The damage is confined to the signal log, but Rule 3 requires every
signal to be logged accurately, and five-fold duplication makes a naive
rejection rate wrong.

Fix: give the guard a witness that survives a failure. Rows dated today in
signals.csv are proof the day was already attempted, whatever the state files
say. A run that fails before logging anything leaves no rows and can still be
retried; a run that fails after evaluating is not retried, because re-running
a day whose orders are already placed produces duplicate rows and leans on
the duplicate-entry guard as a last line of defence.

The 80 duplicate rows are retained -- Rule 8 is append-only and deleting them
would be a retroactive edit of the primary record. A SYSTEM_ERROR row is
appended documenting the duplication, and rejection statistics for that date
must be computed on de-duplicated rows.

Also fixes a separate noise source introduced on 2026-10-05 by the dependency
recorder. That step sits before the market-open wait, so it ran on every
eligible firing, rewrote study/env/<date>.txt each time, staged a diff and
produced a "study: session <date>" commit containing no study data. Twenty
such commits accumulated across 10-06 and 10-07. It now writes once per day.
"""


def main() -> int:
    missing = [p for p in (GUARD, TEST_WORKFLOW) if not p.exists()]
    if missing:
        print("missing: " + ", ".join(str(p) for p in missing), file=sys.stderr)
        return 1

    g_old = GUARD.read_text(encoding="utf-8")
    t_old = TEST_WORKFLOW.read_text(encoding="utf-8")

    g_new, g_ch = patch_guard(g_old)
    t_new, t_ch = patch_test(t_old)
    w_old = WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.exists() else ""
    w_new, w_ch = patch_dependency_step(w_old) if w_old else (w_old, [])
    row_ch = append_system_error_row()

    if (g_new == g_old and t_new == t_old and w_new == w_old
            and not row_ch and not w_ch):
        print("already patched; nothing to do")
        return 0

    if g_new != g_old:
        GUARD.write_text(g_new, encoding="utf-8")
    if t_new != t_old:
        TEST_WORKFLOW.write_text(t_new, encoding="utf-8")
    if w_new != w_old:
        WORKFLOW.write_text(w_new, encoding="utf-8")

    for c in g_ch + t_ch + w_ch + row_ch:
        print(f"  {c}")

    # The guard imports dt and pathlib already; prove it still compiles.
    import py_compile  # noqa: PLC0415

    try:
        py_compile.compile(str(GUARD), doraise=True)
        py_compile.compile(str(TEST_WORKFLOW), doraise=True)
        print("  [ok] both files compile")
    except py_compile.PyCompileError as exc:
        raise SystemExit(f"patched file does not compile: {exc}")

    # Sanity-check the predicate the way the workflow will call it.
    src = GUARD.read_text(encoding="utf-8")
    for needed in ("signals_logged_for", "and not signals_logged",
                   '"--signals"', "signals_logged = signals_logged_for"):
        if needed not in src:
            raise SystemExit(f"expected {needed!r} in the patched guard")
    print("  [ok] guard wiring verified")

    COMMIT_MSG_PATH.write_text(COMMIT_MSG, encoding="utf-8")
    print(f"  [ok] commit message written to {COMMIT_MSG_PATH.name}")

    print("\nPatched. Commit and push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
