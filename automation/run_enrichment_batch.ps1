param([int]$Limit = 500)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$collector = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'batch_collect_malaysia\.py' }
if ($collector) { throw 'Pause the collector and watchdog before running an enrichment batch.' }
if ($Limit -lt 1 -or $Limit -gt 500) { throw 'Limit must be between 1 and 500.' }
Set-Location -LiteralPath $projectRoot
& (Join-Path $projectRoot '.venv\Scripts\python.exe') website_enrichment.py --limit $Limit
exit $LASTEXITCODE
