<#
.SYNOPSIS
  Registers Windows scheduled tasks so BD Stock Tracker monitors DSE every trading day.

.DESCRIPTION
  "BD Stock Tracker - Daily"      runs `run.py daily` at 14:30, Sunday-Thursday (DSE trading days).
                                  If the PC was off or asleep then, it runs as soon as possible afterwards.
  "BD Stock Tracker - Dashboard"  (only with -StartServerAtLogon) starts the dashboard + live monitor
                                  in the background when you sign in to Windows.

  The tasks run as your user, only while you are signed in (so desktop notifications are visible).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -StartServerAtLogon
  powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Uninstall
#>
param(
    [string]$Time = "14:30",
    [switch]$StartServerAtLogon,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dailyName = 'BD Stock Tracker - Daily'
$serverName = 'BD Stock Tracker - Dashboard'

if ($Uninstall) {
    foreach ($name in @($dailyName, $serverName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed task: $name"
        }
    }
    return
}

$pythonw = $null
$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) { $pythonw = $cmd.Source }
if (-not $pythonw) {
    $py = (Get-Command python.exe -ErrorAction Stop).Source
    $pythonw = Join-Path (Split-Path $py) 'pythonw.exe'
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to python.exe ($pythonw)" }
$runPy = Join-Path $root 'run.py'
if (-not (Test-Path (Join-Path $root 'config.ini'))) {
    Write-Warning "config.ini not found. Run 'python run.py setup' first so the task can reach MySQL."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$runPy`" daily" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday, Monday, Tuesday, Wednesday, Thursday -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RunOnlyIfNetworkAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $dailyName -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "Stores DSE end-of-day prices, runs analysis, checks alerts, builds the daily report and notifies you." | Out-Null
Write-Host "Registered '$dailyName' - Sun-Thu at $Time ($pythonw)"

if ($StartServerAtLogon) {
    $action2 = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$runPy`" serve" -WorkingDirectory $root
    $trigger2 = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings2 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $serverName -Action $action2 -Trigger $trigger2 -Settings $settings2 -Force `
        -Description "Runs the BD Stock Tracker dashboard (http://127.0.0.1:8050) and live market monitor." | Out-Null
    Write-Host "Registered '$serverName' - starts at sign-in"
}

Write-Host ""
Write-Host "Done. Check with:  Get-ScheduledTask -TaskName 'BD Stock Tracker*'"
Write-Host "Remove with:      powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Uninstall"
