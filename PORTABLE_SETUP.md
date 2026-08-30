# LOCUS-T Lead Intelligence V4 — portable setup

Read `CODEX_HANDOFF.md` first for the business goal, lead scoring, PIC workflow and safety rules. This repository contains the crawler, sales dashboard, taxonomy/search manifest logic, RAM-aware browser scheduling, and Windows watchdog. The live crawl database is intentionally **not** stored in GitHub: it is large, changes continuously, and contains business and sales-workflow data.

## 1. Install on the new Windows PC

Open PowerShell:

```powershell
git clone https://github.com/Ayyouboss0011/SherlockMaps.git
cd SherlockMaps
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r core\requirements.txt
python -m playwright install chromium
```

If PowerShell blocks activation, run the commands with `.venv\Scripts\python.exe` directly instead.

## 2. Transfer the current crawl state

Stop the collector on the old PC first, then copy the `data` folder into the cloned repository. The important files are:

```text
data\malaysia_qualified_companies.sqlite
data\malaysia_qualified_companies.sqlite-wal   (copy if present)
data\malaysia_qualified_companies.sqlite-shm   (copy if present)
data\malaysia_batch_status.json
```

The database is the checkpoint/resume state. Copying the whole `data` folder is simplest; old logs and backups are optional. Use OneDrive, an external drive, or another private transfer method. Do not commit the database, WAL/SHM files, logs, or exports to GitHub.

## 3. Start the dashboard and collector

From the repository folder:

```powershell
.venv\Scripts\python.exe progress_dashboard.py
```

In a second PowerShell window:

```powershell
cd path\to\SherlockMaps
.venv\Scripts\python.exe batch_collect_malaysia.py
```

Open [http://localhost:8765/](http://localhost:8765/). The collector resumes pending searches from SQLite; it does not start from zero.

The dashboard provides live progress, ranked leads, market/industry coverage, a sales pipeline, a brand-deduplicated A/B call-list CSV, full CSV/Excel exports and PIC CSV re-import. Generated Excel files are written to `data\sherlockmaps-companies.xlsx`.

V4 resumes toward 400,000 unique locations. New searches focus on Klang Valley, Johor and Penang; legacy nationwide locations and provenance remain unchanged.

## 4. Optional automatic restart after sign-in

Run this once in PowerShell from the repository folder:

```powershell
powershell -ExecutionPolicy Bypass -File automation\install_malaysia_watchdog.ps1
```

The task starts after the Windows user signs in and checks every five minutes. It restarts missing or unresponsive dashboard, collector and website-enrichment processes without creating duplicates. Windows must remain awake and connected to the internet while crawling.

## 5. Useful checks

```powershell
powershell -ExecutionPolicy Bypass -File automation\show_malaysia_progress.ps1
Invoke-WebRequest http://localhost:8765/ -UseBasicParsing
```

The status file reports qualified companies, active browsers, free RAM, query rate, reclaimed jobs, and any halt reason.

## Safety notes

- Never run two collectors against the same SQLite database.
- Stop the collector before copying SQLite WAL/SHM files.
- Keep the database and exports private; they contain collected business data.
- Preserve organization-level `DO_NOT_CONTACT` suppression across every branch and device transfer.
- Default outreach is a human call to published business lines; email/WhatsApp outreach is disabled unless compliance approval is documented.
- The collector does not use Codex tokens. It runs locally through Python/Playwright.
