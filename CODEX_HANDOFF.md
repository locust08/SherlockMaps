# LOCUS-T Lead Intelligence V4 — PIC and Codex handoff

## Purpose

This is a local, crash-safe lead-intelligence system for LOCUS-T Malaysia. It discovers active Malaysian business locations from Google Maps, identifies the best initial offer—Website Development, SEO, or Google Ads—and prepares organization-deduplicated call queues for human sales follow-up.

The completion target is **400,000 qualified unique physical locations**, not raw results. Existing nationwide data remains append-only. New V4 collection is deliberately concentrated where LOCUS-T expects higher conversion value and can meet prospects more easily:

- Klang Valley: about 48% of new query scheduling while the pilot is available.
- Johor: about 22%.
- Penang: about 17%.
- GDP-ranked Peninsular pilot: about 13%, initially Perak, Pahang, Kedah,
  Negeri Sembilan, then Melaka. These are ranked by 2025 **total** real GDP,
  not GDP per capita, using the [official DOSM state release](https://www.dosm.gov.my/portal-main/release-content/gross-domestic-product-gdp-by-state-2025).

The pilot uses a small set of high-intent searches in two or three cities per
state. Keep the core markets dominant; expand a pilot state further only after
its observed unique and A/B lead yield justifies it. The live two-hour Codex
monitor compares performance and reports material changes or failures.

Always read live counts from `data/malaysia_batch_status.json` or [http://localhost:8765/](http://localhost:8765/); counts in messages or documents become stale.

## Performance maintenance

Read `PERFORMANCE_REVIEW.md` for the September 2026 optimization results, test
evidence and limitations. Run `.venv\Scripts\python.exe -m unittest discover -s tests -q`
before changing extraction, scheduling, persistence or export behavior.
Keep per-listing transactions short: checkpoint/status work must not retain a
SQLite writer lock while waiting for browsers or HTTP requests. Preserve the
organization-score covering index and RAM launch threshold. Do not enable the
experimental grouped contact extraction by default; its measured live gain
was small. Excel downloads now stream through request-specific temporary files,
which are removed after download; prior saved exports remain untouched.
Live before/after numbers are short-window observations, not a guaranteed speed
multiplier. Preserve Google cooldown/halting, branch identity, and immediate
per-listing persistence when tuning further.

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

Deduplication applies across crawl prompts and restarts. When a later crawl supplies a Place ID, a single existing phone match without a Place ID is upgraded in place, retaining its lead ID and sales history. A different known Place ID is not merged through a shared phone. Each new query's observation and provenance are retained separately from the unique company record.

### Dashboard collection controls

The overview now offers **Start / Resume** and **Pause**, with an editable total
qualified-location target and maximum browser-worker count (1–6). Start launches
`batch_collect_malaysia.py` using the dashboard's exact database and status-file
paths. Pause stops new submissions, lets active searches finish, and preserves
saved leads and search-job completion state. Resume skips completed searches.
Closing or restarting the dashboard does not stop an active collector.

`collector_control.py` provides per-database OS locks shared by dashboard and CLI
launches. A second collector exits with code 3 instead of touching the database.
Pause requests survive restarts in `<database>.pause`; the Windows watchdog
respects that marker. Resume from the dashboard removes it. Lifecycle state and
dashboard-launched stdout/stderr are kept in `<database>.collector.json` and
`<database>.collector.log`. A killed collector releases its OS lock automatically;
stale startup or running state is shown as failed, not falsely running.

Control endpoints are local-only and require the dashboard's per-process request
token. They are not a remotely authenticated service. Website enrichment remains
a separate process; Start does not launch it. Custom isolated databases can be
selected with `progress_dashboard.py --database PATH --status-file PATH`, and the
collector accepts the same flags. The defaults still use the existing project
database and status file. Do not point a test dashboard at production data.

### Industry and location selection

Choose **Choose industries and locations** in the overview's Search plan control.
Select industries and Malaysian states, then optionally cities. With no cities
or custom areas specified, the collector uses all catalogued cities in the
selected states. Custom neighbourhoods, postcodes and industrial parks are
entered one per line and assigned a selected state. These control search queries,
not geographic boundaries: Google Maps may return nearby businesses.
For custom areas in multiple states, use `State | Area` per line; every named
state must also be selected. Multistate settings retain those assignments on reload.

Optional custom keywords replace the preset keywords; assign them to one of the
existing industries for scoring and reporting. The industry relevance check also
uses meaningful custom-keyword prefixes against the listing name/category, so
niches are not rejected solely because they are absent from preset keywords.
Missing contact/name/address and permanently-closed checks still apply.

**Preview searches** validates the selection and shows the pending-query count
and a sample of up to 30 searches without launching a browser or changing leads.
Completed searches are skipped. Start saves settings atomically in
`<database>.collection.json`; dashboard reload, Resume, CLI launch and the
watchdog use that saved selection. Selected plans never carry forward unrelated
old jobs or automatically expand into unselected areas. The original default
recommended plan remains available. The target is still the total qualified
location count in the database, not the number of additional or selected-industry
leads. Unsaved form edits prevent the periodic page reload from discarding them.

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

- `/leads`: filter ranks, offers, industries, states and pipeline status; compact desktop rows and mobile lead cards.
- `/lead?id=<company ID>`: review business contacts and score evidence; edit the organization's assigned salesperson, status, contact notes/outcome, Malaysia-time follow-up and proposal value (MYR).
- `/coverage`: compare query/A-B yield and coverage gaps.
- `/pipeline`: review open organizations, assign salespeople through lead details, and view the funnel/activity history. Detailed conversion analytics and CSV handoff are expandable sections.
- `/export/call-list.csv`: brand-deduplicated A/B list; suppressed and do-not-contact organizations are excluded.
- `/export/companies.csv` and `.xlsx`: every qualified physical location.

Sales lifecycle:

`NEW → REVIEWED → CONTACTED → QUALIFIED → DISCOVERY → PROPOSAL_SENT → WON / LOST / DO_NOT_CONTACT`

The CSV supports Assigned PIC, next action, call outcome, requirement, budget range, proposal value/link, lost reason and notes. Import creates append-only `sales_activities`. `DO_NOT_CONTACT` also creates organization-level suppression covering every branch.

### First-time dashboard experience (2026-10-05)

The collection page guides users through scope, total lead goal, and preview/start. Start is enabled only after a successful preview and settings changes invalidate that preview. Active collection locks scope editing with an explanation; pausing still drains current searches. The broad default plan displays its industry/market scope. Analytics that have not loaded are identified rather than presented as empty results.

The overview distinguishes collected locations from eligible, organization-deduplicated A/B sales-ready records. It labels targets as goals and shows non-zero progress below 0.1% explicitly. Workers, memory and throughput are in Advanced diagnostics; execution tables are expandable. All main pages identify the database environment, prominently warning when the database is in a temporary directory. Temporary databases may contain live collected leads, not merely synthetic fixtures.

Direct sales edits use the existing sales tables and append-only activity history, not a new CRM database. `POST /api/sales/update` requires the dashboard's local-host/same-origin token and the record's current `updated_at` value. A transaction rejects stale edits with HTTP 409; the user must reload. Notes/assignment saves do not imply contact: tick **Record a contact made outside this dashboard** to increment the contact count and timestamp. Clearing a proposal amount removes it, and zero is a valid value. Suppressed leads cannot be reactivated or marked contacted here. CSV updates remain supported with their legacy contact inference. No outreach is sent.

Coverage shows planned versus completed searches and observed results, with estimated A/B yield labelled per search. Users can hide groups without started searches; collected coverage includes all states, including selected campaigns outside the default markets. Fresh devices without a lead database receive setup guidance rather than database errors.

### Cross-platform memory reporting

`system_memory.py` uses native Windows available physical memory, Linux `MemAvailable`, or a macOS `vm_stat` estimate (free + inactive + speculative pages). Values are GiB of available/reclaimable memory, not strictly unused RAM or an OS pressure indicator. Failed measurements display **Unavailable**, and new collector processes conservatively block new browser submissions instead of assuming 99 GiB.

The dashboard samples host memory itself, so its display is corrected immediately after a dashboard restart. An already-running collector keeps the old memory policy until it is restarted; Advanced diagnostics warns about that legacy process. Do not interrupt a live collector merely to refresh the dashboard.

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
