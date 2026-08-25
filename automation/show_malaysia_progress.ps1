# Prints collector progress locally without using Codex tokens.

$projectRoot = 'C:\Users\WeigW\OneDrive\Desktop\Codex\SherlockMaps'
$statusPath = Join-Path $projectRoot 'data\malaysia_batch_status.json'
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'batch_collect_malaysia\.py' }

if ($running) { 'Collector: RUNNING' } else { 'Collector: STOPPED' }
Get-Content -Raw $statusPath | ConvertFrom-Json |
    Select-Object qualified_companies, target_qualified_companies, job_status_counts, halt_reason, updated_at |
    Format-List
