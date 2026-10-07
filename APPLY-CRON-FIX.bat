@echo off
REM ===================================================================
REM  APPLY-CRON-FIX.bat  --  double-click this. About 30 seconds.
REM
REM  WHAT IT DOES
REM    1. Parks your uncommitted work in a git stash (nothing is lost).
REM    2. Pulls the commits your local copy is behind by.
REM    3. Runs scripts\fix_cron_timing.py, which retimes the cron entries
REM       so a session can survive GitHub's 5-9 hour scheduler delay.
REM    4. Runs the affected tests. If any fail it STOPS and pushes nothing.
REM    5. Commits and pushes.
REM
REM  Your parked work is still there afterwards:
REM      git stash list        to see it
REM      git stash pop         to bring it back
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo === 0. starting point ===
git rev-parse --short HEAD
if errorlevel 1 goto :fail

echo.
echo === 1. parking uncommitted work ===
git stash push -u -m "pre-cron-fix"
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
  echo    Could not find python on PATH.
  echo    Open PyCharm's terminal in this folder and run these two lines:
  echo        python scripts\fix_cron_timing.py
  echo        git add -A ^&^& git commit -F .git\cronfix-commit-msg.txt ^&^& git push
  goto :fail
)
echo    using: %PY%

echo.
echo === 4. applying the retime ===
%PY% scripts\fix_cron_timing.py
if errorlevel 1 goto :fail

echo.
echo === 5. running the affected tests ===
%PY% -m pytest tests\test_study_workflow.py tests\test_market_open_wait.py -q
if errorlevel 1 (
  echo.
  echo    TESTS FAILED. Nothing has been pushed.
  echo    The files are changed on disk but not committed. To undo:
  echo        git checkout -- .
  goto :fail
)

echo.
echo === 6. committing ===
git add .github/workflows/study-session.yml scripts/wait_for_market_open.py scripts/fix_cron_timing.py tests/test_market_open_wait.py tests/test_study_workflow.py
if errorlevel 1 goto :fail

if not exist ".git\cronfix-commit-msg.txt" (
  echo    commit message file is missing - the patch script did not finish
  goto :fail
)
git commit -F ".git\cronfix-commit-msg.txt"
if errorlevel 1 goto :fail

echo.
echo === 7. pushing ===
git push
if errorlevel 1 goto :fail

echo.
echo ===================================================================
echo  DONE.
echo  Check: https://github.com/MaverickThompson/stock-agent/actions
echo  Your parked work:  git stash list
echo ===================================================================
pause
exit /b 0

:fail
echo.
echo ===================================================================
echo  STOPPED. Nothing was pushed. Scroll up for the reason.
echo  Your parked work is safe:  git stash list
echo ===================================================================
pause
exit /b 1
