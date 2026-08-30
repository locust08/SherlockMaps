# Restarts the local Malaysia collector only when it is not already running.
# Intended for Windows Task Scheduler. This script does not call Codex or any API.

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$collector = 'batch_collect_malaysia.py'
$dataDir = Join-Path $projectRoot 'data'
$watchdogLog = Join-Path $dataDir 'malaysia_watchdog.log'
$stdoutLog = Join-Path $dataDir 'malaysia_batch_autostart_stdout.log'
$stderrLog = Join-Path $dataDir 'malaysia_batch_autostart_stderr.log'
$dashboardStdout = Join-Path $dataDir 'malaysia_dashboard_stdout.log'
$dashboardStderr = Join-Path $dataDir 'malaysia_dashboard_stderr.log'
$enrichmentStdout = Join-Path $dataDir 'malaysia_enrichment_stdout.log'
$enrichmentStderr = Join-Path $dataDir 'malaysia_enrichment_stderr.log'

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'batch_collect_malaysia\.py' }

if ($running) {
    "$(Get-Date -Format o) collector already running: $($running.ProcessId -join ',')" |
        Add-Content -Path $watchdogLog
} else {
    "$(Get-Date -Format o) starting collector" | Add-Content -Path $watchdogLog
    Start-Process -FilePath $pythonExe -ArgumentList '-u', $collector `
        -WorkingDirectory $projectRoot -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog | Out-Null
}

$dashboardRunning = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'progress_dashboard\.py' }
$dashboardHealthy = $false
if ($dashboardRunning) {
    try {
        $dashboardResponse = Invoke-WebRequest -UseBasicParsing `
            -Uri 'http://127.0.0.1:8765/api/status' -TimeoutSec 10
        $dashboardHealthy = $dashboardResponse.StatusCode -eq 200
    } catch {
        "$(Get-Date -Format o) dashboard unhealthy: $($_.Exception.Message)" |
            Add-Content -Path $watchdogLog
        $dashboardRunning.ProcessId | ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2
    }
}
if (-not $dashboardHealthy) {
    "$(Get-Date -Format o) starting dashboard" | Add-Content -Path $watchdogLog
    Start-Process -FilePath $pythonExe -ArgumentList 'progress_dashboard.py' `
        -WorkingDirectory $projectRoot -WindowStyle Hidden `
        -RedirectStandardOutput $dashboardStdout -RedirectStandardError $dashboardStderr | Out-Null
}

$enrichmentRunning = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'website_enrichment\.py' }
if (-not $enrichmentRunning) {
    "$(Get-Date -Format o) starting website enrichment batch" | Add-Content -Path $watchdogLog
    Start-Process -FilePath $pythonExe -ArgumentList 'website_enrichment.py' `
        -WorkingDirectory $projectRoot -WindowStyle Hidden `
        -RedirectStandardOutput $enrichmentStdout -RedirectStandardError $enrichmentStderr | Out-Null
}
