@echo off
REM ===================================================================
REM  APPLY-STUDY-FIXES.bat  --  double-click. About 40 seconds.
REM
REM  Run this AFTER the cron-job.org job is set up, because it assumes
REM  an external scheduler is now the primary trigger.
REM
REM  Two Section 11 defect corrections, as two commits:
REM
REM    1. Demote GitHub cron to a thin fallback. 24 entries -> 3.
REM       Those 24 arrive 5-9 hours late, find the day already done,
REM       and exit in ~30 seconds -- 31 runs for one trading day on
REM       2026-10-06. Raises the market-open wait so the 3 survivors
REM       can actually hold for the bell.
REM
REM    2. Re-run guard. Stops a session that traded and then failed
REM       from being re-run by later arrivals. That is what logged 100
REM       signal rows for 2026-10-06 instead of 20. Also stops the
REM       dependency recorder committing on every firing.
REM
REM  Parks your uncommitted work, pulls, patches, then runs the tests.
REM  If any test fails it STOPS and pushes nothing.
REM
REM  Parked work afterwards:  git stash list  /  git stash pop
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo === 0. starting point ===
git rev-parse --short HEAD
if errorlevel 1 goto :fail

echo.
echo === 1. parking uncommitted work ===
git stash push -u -m "pre-study-fixes"
if errorlevel 1 echo    (nothing to stash - continuing)

echo.
echo === 2. pulling ===
git pull --rebase origin main
if errorlevel 1 goto :fail

echo.
echo === 3. finding python ===
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo    Could not find python on PATH. In PyCharm's terminal, run:
  echo        python scripts\fix_schedule_fallback.py
  echo        python scripts\fix_rerun_guard.py
  echo        python -m pytest tests\test_study_workflow.py tests\test_market_open_wait.py -q
  echo        git add -A
  echo        git commit -F .git\fallback-commit-msg.txt
  echo        git push
  goto :fail
)
echo    using: %PY%

echo.
echo === 4. patch 1 of 2: cron becomes a fallback ===
%PY% scripts\fix_schedule_fallback.py
if errorlevel 1 goto :fail

echo.
echo === 5. patch 2 of 2: re-run guard ===
%PY% scripts\fix_rerun_guard.py
if errorlevel 1 goto :fail

echo.
echo === 6. tests, BEFORE committing anything ===
%PY% -m pytest tests\test_study_workflow.py tests\test_market_open_wait.py -q
if errorlevel 1 (
  echo.
  echo    TESTS FAILED. Nothing committed, nothing pushed.
  echo    Files are changed on disk only. To undo:  git checkout -- .
  goto :fail
)

echo.
echo === 7. commit 1 of 2 ===
git add .github/workflows/study-session.yml scripts/wait_for_market_open.py scripts/fix_schedule_fallback.py scripts/fix_cron_timing.py tests/test_market_open_wait.py
if errorlevel 1 goto :fail
if not exist ".git\fallback-commit-msg.txt" (
  echo    fallback commit message missing - patch 1 did not finish
  goto :fail
)
git commit -F ".git\fallback-commit-msg.txt"
if errorlevel 1 goto :fail

echo.
echo === 8. commit 2 of 2 ===
git add scripts/check_study_eligibility.py scripts/fix_rerun_guard.py tests/test_study_workflow.py study/signals.csv
if errorlevel 1 goto :fail
if not exist ".git\rerunguard-commit-msg.txt" (
  echo    rerunguard commit message missing - patch 2 did not finish
  goto :fail
)
git commit -F ".git\rerunguard-commit-msg.txt"
if errorlevel 1 goto :fail

echo.
echo === 9. pushing ===
git push
if errorlevel 1 goto :fail

echo.
echo ===================================================================
echo  DONE - two commits pushed.
echo  Check: https://github.com/MaverickThompson/stock-agent/actions
echo  Parked work:  git stash list
echo ===================================================================
pause
exit /b 0

:fail
echo.
echo ===================================================================
echo  STOPPED. Nothing was pushed. Scroll up for the reason.
echo  Parked work is safe:  git stash list
echo ===================================================================
pause
exit /b 1
