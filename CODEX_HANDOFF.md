# LOCUS-T Lead Intelligence V4 — PIC and Codex handoff

## Purpose

This is a local, crash-safe lead-intelligence system for LOCUS-T Malaysia. It discovers active Malaysian business locations from Google Maps, identifies the best initial offer—Website Development, SEO, or Google Ads—and prepares organization-deduplicated call queues for human sales follow-up.

The completion target is **400,000 qualified unique physical locations**, not raw results. Existing nationwide data remains append-only. New V4 collection is deliberately concentrated where LOCUS-T expects higher conversion value and can meet prospects more easily:

- Klang Valley: 55% of new query scheduling.
- Johor: 25%.
- Penang: 20%.

Always read live counts from `data/malaysia_batch_status.json` or [http://localhost:8765/](http://localhost:8765/); counts in messages or documents become stale.

## Ideal customer and search direction

Priority order:

1. Construction, renovation, home improvement, interior design, commercial cleaning, facilities and landscaping.
2. Healthcare, dental, aesthetics, veterinary, fitness and private education.
3. Automotive repair, detailing, tyres and workshops.
4. Accounting, insurance, corporate services and property services.
5. Industrial suppliers, manufacturers, machinery, fabrication, logistics and high-value B2B.
6. Beauty, events, hospitality, travel and printing.
7. F&B and retail only when their observed A/B yield warrants it.

The collector uses city, district, neighbourhood, industrial-park and postcode prompts with English, Bahasa Malaysia and selected Chinese aliases. It ranks pending work by expected A/B sales-ready yield. Government and Others are classification-only and never create queries.

## Qualification and scoring

A qualified location needs a relevant commercial category, Malaysian physical address, business name, phone or website, and an active Maps listing. Physical branches remain separate local-SEO opportunities. `organizations` groups branches so sales does not repeatedly contact the same brand.

Primary offers:

- `WEBSITE_BUILD`: phone is available but no functional owned website exists.
- `SEO_UPGRADE`: a live website has at least two actionable SEO/local-search gaps.
- `GOOGLE_ADS_READY`: HTTPS site and conversion path exist for a high-intent business, but ads/conversion infrastructure is missing or weak.
- `ENRICHMENT_REQUIRED`: more evidence is needed before outreach.

The versioned score is out of 100: reachability 20, service need 30, capacity proxies 25, demand 15, and freshness 10. Ranks are A 75–100, B 60–74, C 45–59 and D below 45. A/B leads form the default call queue.

## Architecture and source of truth

- `data/malaysia_qualified_companies.sqlite`: live SQLite/WAL source of truth; never commit it.
- `batch_collect_malaysia.py`: stable launcher.
- `batch_collect_malaysia_v2.py`: V4 manifest, RAM-aware worker control, qualification, deduplication and persistence.
- `lead_intelligence_v4.py`: append-only organization grouping, scoring, sales state and suppression.
- `website_enrichment.py`: separate website/contact/tracking enrichment queue.
- `progress_dashboard.py`: dashboard, ranked leads, pipeline, coverage, exports and CSV re-import.
- `automation/watch_malaysia_collector.ps1`: no-token watchdog that resumes collector, dashboard and enrichment.
- Windows task: `SherlockMaps Malaysia Collector Watchdog`.

Maps fields are persisted immediately. Place ID/CID is the first deduplication key, followed by phone, normalized name/address, and domain/name/location evidence. Website enrichment runs independently so it does not slow Maps extraction. Never run two collectors on one database.

## Install, transfer and launch

See `PORTABLE_SETUP.md` for full commands. The short Windows path is:

```powershell
git clone <the project GitHub URL>
cd SherlockMaps
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r core\requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

Privately transfer the entire stopped `data` folder from the old device. The live database can be multiple gigabytes and must go through OneDrive, encrypted storage, or an external drive—not GitHub.

Start/resume everything:

```powershell
powershell -ExecutionPolicy Bypass -File automation\watch_malaysia_collector.ps1
Start-Process http://localhost:8765/
```

Install automatic resume after Windows sign-in:

```powershell
powershell -ExecutionPolicy Bypass -File automation\install_malaysia_watchdog.ps1
```

Check locally without Codex/token usage:

```powershell
powershell -ExecutionPolicy Bypass -File automation\show_malaysia_progress.ps1
Get-Content data\malaysia_batch.log -Tail 30
```

## Dashboard, export and PIC workflow

Open [http://localhost:8765/](http://localhost:8765/):

- `/leads`: filter ranks, offers, industries, states, PIC and pipeline status.
- `/coverage`: compare query/A-B yield and coverage gaps.
- `/pipeline`: download or re-import the sales CSV and view activities.
- `/export/call-list.csv`: brand-deduplicated A/B list; suppressed and do-not-contact organizations are excluded.
- `/export/companies.csv` and `.xlsx`: every qualified physical location.

Sales lifecycle:

`NEW → REVIEWED → CONTACTED → QUALIFIED → DISCOVERY → PROPOSAL_SENT → WON / LOST / DO_NOT_CONTACT`

The CSV supports Assigned PIC, next action, call outcome, requirement, budget range, proposal value/link, lost reason and notes. Import creates append-only `sales_activities`. `DO_NOT_CONTACT` also creates organization-level suppression covering every branch.

Default outreach is human call-first using published business lines. Email and WhatsApp outreach remain disabled until LOCUS-T approves channel-consent and compliance rules. Retain and enforce every opt-out.

## Safe backup and GitHub rules

Before moving the database, stop the collector and enrichment, checkpoint WAL, then copy the whole `data` folder. Never commit `data/`, exports, credentials or personal sales notes. Commit only code and documentation.

```powershell
git status --short
git add README.md PORTABLE_SETUP.md CODEX_HANDOFF.md automation *.py core tests
git commit -m "Add LOCUS-T V4 lead intelligence and sales handoff"
git push
```

If `git push` requests authentication, sign in with Git Credential Manager or use a GitHub personal-access token, then retry. Do not replace or delete the private SQLite state merely to make a push work.

## Operating rules and limitations

- Windows must be awake, powered and online. The watchdog cannot crawl while the PC is asleep or shut down.
- Do not use VPN/proxy rotation, CAPTCHA bypassing or multiple collectors on the same SQLite file.
- On Google throttle/CAPTCHA the collector pauses, reduces concurrency and records the reason.
- Organization grouping is evidence-based and should be audited before large outreach batches.
- Public emails/contact forms are evidence for reachability only; they do not grant marketing consent.
- Google Maps supply, duplicates and website availability mean no fixed query guarantees a fixed number of qualified leads.

## Next recommended action

Let the 120-query V4 pilot run, then validate a stratified 500-record sample and a 1,000-lead A/B sales pilot. Feed actual contact, qualification, proposal and win outcomes back into query prioritization weekly before scaling outreach.
