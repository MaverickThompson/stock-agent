@echo off
REM ===================================================================
REM  APPLY-DEFERRED-FIX.bat  --  double-click. About 30 seconds.
REM
REM  Fixes what broke the 2026-10-14 session.
REM
REM  One stale quote on DLTR deferred one exit. Section 7 says that IS the
REM  correct behaviour -- the fill moves to the next regular session. But
REM  run_session.py only advances study state when errors == 0, and the
REM  deferral shared that counter. So the whole session was voided:
REM  22 signal rows written, no sessions.csv row, no counter increment.
REM
REM  After this a deferral counts as a deferral. Real failures -- config
REM  mismatch, adapter failure, unexpected exceptions -- still void the
REM  session exactly as before. No strategy parameter changes.
REM
REM  Also repairs tests/test_study_state.py, which asserted the LIVE state
REM  file was still at zero sessions and has failed since session 1.
REM
REM  The code changes are ALREADY on disk and already tested. This commits
REM  and pushes them. Tests run first; nothing is pushed if they fail.
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo === 0. starting point ===
git rev-parse --short HEAD
if errorlevel 1 goto :fail

echo.
echo === 1. finding python ===
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo    Could not find python on PATH. In PyCharm's terminal run:
  echo        python scripts\fix_deferred_exit.py
  echo        python -m pytest tests\test_deferred_exit.py tests\test_study_state.py -q
  echo        git add -A ^&^& git commit -F .git\deferred-commit-msg.txt
  echo        git pull --rebase origin main ^&^& git push
  goto :fail
)
echo    using: %PY%

echo.
echo === 2. re-applying the patch (safe to repeat - it is idempotent) ===
%PY% scripts\fix_deferred_exit.py
if errorlevel 1 goto :fail

echo.
echo === 3. tests, BEFORE committing anything ===
%PY% -m pytest tests\test_deferred_exit.py tests\test_study_state.py tests\test_study_workflow.py -q
if errorlevel 1 (
  echo.
  echo    TESTS FAILED. Nothing committed, nothing pushed.
  echo    To undo:  git checkout -- .
  goto :fail
)

echo.
echo === 4. committing ===
git add src/stockagent/session.py scripts/fix_deferred_exit.py tests/test_deferred_exit.py tests/test_study_state.py APPLY-DEFERRED-FIX.bat
if errorlevel 1 goto :fail
if not exist ".git\deferred-commit-msg.txt" (
  echo    commit message missing - the patch did not finish
  goto :fail
)
git commit -F ".git\deferred-commit-msg.txt"
if errorlevel 1 goto :fail

echo.
echo === 5. pulling the robot's commits, then pushing ===
git pull --rebase origin main
if errorlevel 1 goto :fail
git push
if errorlevel 1 goto :fail

echo.
echo ===================================================================
echo  DONE - pushed.
echo  Check: https://github.com/MaverickThompson/stock-agent/actions
echo ===================================================================
pause
exit /b 0

:fail
echo.
echo ===================================================================
echo  STOPPED. Nothing was pushed. Scroll up for the reason.
echo ===================================================================
pause
exit /b 1
