# Install the per-user watchdog task without requiring administrator rights.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$watchdog = Join-Path $PSScriptRoot 'watch_malaysia_collector.ps1'
$taskName = 'SherlockMaps Malaysia Collector Watchdog'

if (-not (Test-Path $pythonExe)) {
    throw "Python environment not found at $pythonExe. Complete PORTABLE_SETUP.md first."
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$watchdog`"" -WorkingDirectory $projectRoot
$atLogOn = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME -RandomDelay (New-TimeSpan -Seconds 30)
$repeating = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($atLogOn, $repeating) -Settings $settings -Description 'Keeps the SherlockMaps Malaysia dashboard and collector running.' -Force | Out-Null
Write-Host "Installed $taskName for $env:USERNAME"
