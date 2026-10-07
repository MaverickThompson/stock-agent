<#
  dispatch_session.ps1 -- start the study session WITHOUT GitHub's cron.

  WHY THIS EXISTS
  ---------------
  The session workflow carries 24 cron entries. On 2026-10-05 every one of
  them was dropped: zero scheduled runs, an entire trading day lost. GitHub's
  own status page showed "Incident with Actions" opened 19:11Z that day, and
  "Actions Job Delays" on 2026-10-01 14:47Z-17:56Z -- the same window in which
  that session fired 5h30m late.

  GitHub documents `schedule` as best effort. Events are delayed during high
  load and DROPPED outright when the backlog is long enough. Nothing in this
  repository can fix that, because the trigger lives on their side.

  `workflow_dispatch` is different. It is an API call, not a queued timer
  event: it is accepted or it returns an error. There is no silent drop. So
  this script runs on Maverick's own machine, on Windows Task Scheduler, and
  asks GitHub to start the run. The crons stay in place as a free fallback.

  THE TOKEN IS NEVER IN THIS FILE
  -------------------------------
  Set it once, as your own Windows user environment variable:

    [Environment]::SetEnvironmentVariable(
        'STUDY_DISPATCH_TOKEN','<the token>','User')

  Make it a fine-grained personal access token scoped to this one repository
  with a single permission: Actions = Read and write. Nothing else. It cannot
  read your other repositories and cannot push code.

  This changes no parameter, gate, universe or log schema. It only makes the
  protocol execute on the days it is supposed to execute.
#>

[CmdletBinding()]
param(
    [string]$Repo     = 'MaverickThompson/stock-agent',
    [string]$Workflow = 'study-session.yml',
    [string]$Ref      = 'main',
    [int]$Attempts    = 4
)

$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent $PSScriptRoot
$logDir  = Join-Path $root 'logs'
$logFile = Join-Path $logDir 'dispatch.log'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $Level, $Message
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Host $line
}

$token = $env:STUDY_DISPATCH_TOKEN
if ([string]::IsNullOrWhiteSpace($token)) {
    Write-Log 'STUDY_DISPATCH_TOKEN is not set. Cannot dispatch. See the header of this file.' 'ERROR'
    exit 2
}

$headers = @{
    'Accept'               = 'application/vnd.github+json'
    'Authorization'        = "Bearer $token"
    'X-GitHub-Api-Version' = '2022-11-28'
    'User-Agent'           = 'study-dispatch'
}

# Count the runs that already exist, so we can prove a NEW one appeared rather
# than trusting the 204 and walking away.
$runsUrl = "https://api.github.com/repos/$Repo/actions/workflows/$Workflow/runs?per_page=1"
$before = $null
try {
    $before = (Invoke-RestMethod -Uri $runsUrl -Headers $headers -Method Get).total_count
    Write-Log "runs before dispatch: $before"
} catch {
    Write-Log "could not read the run count before dispatching ($($_.Exception.Message)); continuing anyway" 'WARN'
}

$dispatchUrl = "https://api.github.com/repos/$Repo/actions/workflows/$Workflow/dispatches"
$body = @{ ref = $Ref } | ConvertTo-Json -Compress

$accepted = $false
for ($i = 1; $i -le $Attempts; $i++) {
    try {
        Invoke-RestMethod -Uri $dispatchUrl -Headers $headers -Method Post `
                          -Body $body -ContentType 'application/json' | Out-Null
        Write-Log "dispatch accepted on attempt $i"
        $accepted = $true
        break
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        Write-Log "attempt $i failed (HTTP $status): $($_.Exception.Message)" 'WARN'
        # 401/403/404 are configuration faults -- the token is wrong, expired or
        # lacks Actions write. Retrying cannot fix those, so stop loudly.
        if ($status -in 401,403,404) {
            Write-Log 'token or permission problem. Retrying will not help. Fix the token.' 'ERROR'
            exit 3
        }
        Start-Sleep -Seconds ([Math]::Min(60, 5 * [Math]::Pow(2, $i)))
    }
}

if (-not $accepted) {
    Write-Log "all $Attempts attempts failed. No run was started." 'ERROR'
    exit 4
}

# Verify. A 204 means GitHub took the request, not that a run exists.
if ($null -ne $before) {
    for ($w = 0; $w -lt 12; $w++) {
        Start-Sleep -Seconds 10
        try {
            $r = Invoke-RestMethod -Uri $runsUrl -Headers $headers -Method Get
            if ($r.total_count -gt $before) {
                $run = $r.workflow_runs[0]
                Write-Log "run #$($run.run_number) created ($($run.html_url))"
                exit 0
            }
        } catch {
            Write-Log "poll $w failed: $($_.Exception.Message)" 'WARN'
        }
    }
    Write-Log 'dispatch was accepted but no new run appeared within 2 minutes. Check the Actions tab.' 'WARN'
    exit 5
}
exit 0
