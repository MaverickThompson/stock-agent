# Daily start-of-work check for the stock-agent repo.
#
# A GitHub Actions bot ("study-session") commits to main every trading day,
# so this working copy is stale every morning whether or not anything is
# wrong. This script does the whole routine in one go and then tells you, in
# plain words, whether you are clear to work.
#
# It does NOT commit and it does NOT push. Every change to a repo that backs
# a pre-registered study should be a deliberate act by a person who can say
# why. Run this, read it, then use GitHub Desktop as normal.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

function Head($t) { Write-Host ""; Write-Host "== $t " -ForegroundColor Cyan -NoNewline
                    Write-Host ("=" * [Math]::Max(0, 58 - $t.Length)) -ForegroundColor DarkGray }
function Good($t) { Write-Host "   $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   $t" -ForegroundColor Yellow }
function Bad($t)  { Write-Host "   $t" -ForegroundColor Red }

Head "1. Clearing stale git locks"
$locks = Get-ChildItem -Path .git -Recurse -Filter *.lock -ErrorAction SilentlyContinue |
         Where-Object { $_.LastWriteTime -lt (Get-Date).AddMinutes(-10) }
if ($locks) {
    foreach ($l in $locks) { Remove-Item $l.FullName -Force -ErrorAction SilentlyContinue
                             Warn "removed stale lock: $($l.Name)" }
} else { Good "no stale locks" }

Head "2. Pulling (rebase, so no merge-commit noise)"
git -c pull.rebase=true pull --rebase 2>&1 | ForEach-Object { Write-Host "   $_" }
if ($LASTEXITCODE -ne 0) {
    Bad "PULL FAILED -- stop here and read the message above."
    Bad "Do not commit on top of a failed pull."
    Read-Host "`nPress Enter to close"; exit 1
}

Head "3. What the robot changed while you were away"
$changed = git diff --stat HEAD@{1} HEAD 2>$null
if ($changed) { $changed | ForEach-Object { Write-Host "   $_" } } else { Good "nothing new" }

Head "4. Study status"
if (Test-Path study\v2_state.json) {
    $s = Get-Content study\v2_state.json -Raw | ConvertFrom-Json
    Write-Host "   sessions completed : $($s.completed_sessions) of 60"
    Write-Host "   last session date  : $($s.last_session_date)"
    Write-Host "   status             : $($s.status)"
}
if (Test-Path study\sessions.csv) {
    $last = Import-Csv study\sessions.csv | Select-Object -Last 1
    Write-Host "   last run fired     : $($last.timestamp)"
    Write-Host "   config fingerprint : $($last.config_fingerprint)  (targets $($last.target_r_multiples))"
    if ($last.conformance -eq "OK") { Good "conformance OK -- the running rules match the recorded v2 rules" }
    else { Bad  "CONFORMANCE: $($last.conformance)  <-- investigate before doing anything else" }
}

Head "5. Uncommitted work in your copy"
$st = git status --short
if ($st) { $st | ForEach-Object { Write-Host "   $_" }
           Warn "you have local changes -- commit them in GitHub Desktop when you are done" }
else     { Good "working tree clean" }

Head "6. Tests"
python -m pytest -q --no-header 2>&1 | Select-Object -Last 6 | ForEach-Object { Write-Host "   $_" }

Head "Done"
Write-Host "   This script never commits or pushes. Use GitHub Desktop for that." -ForegroundColor DarkGray
Read-Host "`nPress Enter to close"
