<#
  install_dispatch_task.ps1 -- register the Windows Scheduled Task that starts
  the study session every weekday, independent of GitHub's cron.

  Run ONCE, in a normal (non-admin) PowerShell window:

    cd "$env:USERPROFILE\OneDrive\Work\hedge fund\stock-agent"
    powershell -ExecutionPolicy Bypass -File scripts\install_dispatch_task.ps1

  WHAT IT CREATES
  ---------------
  One task, "Study session dispatch", with two weekday triggers in LOCAL time:

    08:25  five minutes before the Central-time open. No DST arithmetic is
           needed because the market opens at 08:30 local all year, and the
           workflow's own wait_for_market_open step holds until the bell.
    12:00  a catch-up. The workflow's eligibility guard makes this a no-op
           costing about ten seconds if the morning run already recorded the
           day, so there is no risk of trading a day twice.

  Both triggers use StartWhenAvailable, so a start missed because the laptop
  was shut sleeps until the machine wakes and then fires. WakeToRun wakes the
  machine for the 08:25 trigger when it is plugged in.

  This does not remove the crons in study-session.yml. Two independent paths
  to the same run is the point: GitHub's timer can be dropped, and a laptop
  can stay shut, but both failing on the same day is far less likely than
  either failing alone.
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'Study session dispatch'
)

$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root 'scripts\dispatch_session.ps1'

if (-not (Test-Path $script)) { throw "cannot find $script" }

if ([string]::IsNullOrWhiteSpace($env:STUDY_DISPATCH_TOKEN)) {
    Write-Warning @'
STUDY_DISPATCH_TOKEN is not set in this shell, so the task will fail when it
fires. Create a fine-grained personal access token at

  https://github.com/settings/personal-access-tokens/new

  Resource owner : MaverickThompson
  Repository     : only MaverickThompson/stock-agent
  Permissions    : Actions -> Read and write   (nothing else)
  Expiration     : pick a date past 2026-12-22, the last session of the study

then set it once with

  [Environment]::SetEnvironmentVariable('STUDY_DISPATCH_TOKEN','<token>','User')

and open a NEW PowerShell window before re-running this installer.
'@
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $root

$triggers = @(
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '08:25'
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '12:00'
)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Description 'Starts the stock-agent study session via workflow_dispatch, because GitHub cron drops scheduled events.' -Force | Out-Null

Write-Host "Registered '$TaskName'." -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime | Format-List

Write-Host 'Test it now with:' -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Get-Content '$root\logs\dispatch.log' -Tail 20"
