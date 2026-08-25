# Local Malaysia collector automation

The Python collector runs locally and does not use Codex tokens. These PowerShell helpers also run locally.

`watch_malaysia_collector.ps1` checks whether the collector is running and resumes it from the SQLite checkpoint if needed. It never creates a duplicate collector process.

`show_malaysia_progress.ps1` prints the current qualified-company count and halt status.

The scheduled task `SherlockMaps Malaysia Collector Watchdog` runs the watchdog every 15 minutes. It can only operate while Windows is awake; no local process can crawl during Sleep, hibernation, shutdown, or loss of internet.
