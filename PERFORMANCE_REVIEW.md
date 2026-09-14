# Crawling performance review

## Consolidated verification (11 September 2026)

Status: code review, optimization, regression verification and rollout completed.
All **51 automated tests pass**; Next.js production build/type checks pass.
Final service rollout at 18:27 MYT loaded the remaining cooldown, branch-dedupe
and export changes. Controller PID 17352 resumed the existing queue; dashboard
PID 12544 serves the new export code. All six dashboard routes returned HTTP
200 (Search Queries required one retry after an aborted connection). New
observations were persisted at 18:28:38 MYT, with four active workers, 2.9 GB
free RAM and no memory reclaims in the new session. Watchdog remains enabled.
The intentional process-tree shutdown logged one BrokenProcessPool error in
the old controller before it exited; it is not a post-restart failure.

The performance review covered the collector/scheduler and SQLite persistence,
Maps browser/extractor lifecycle, result models and processors, website/email
enrichment, standalone API queues, dashboard/export handlers, UI polling/build,
launchers and watchdog. Generated browser-profile binaries are not application
source. No SMTP sends, outreach, VPN/proxy changes or CAPTCHA bypasses were used.

Implemented improvements:

- Batch feed-link reads instead of one browser round trip per anchor.
- Lightweight worker imports/connections and atomic per-listing persistence.
- Correct opening-hours fallback and retry of unconfirmed feed failures.
- Release controller checkpoint and enrichment startup write locks promptly.
- Cover organization representative lookup with an organization/score index.
- Apply existing RAM launch headroom consistently; enforce cooldown submission
  pauses without weakening repeated-block halting.
- Release optional email browser/driver resources and serialize standalone API
  queue dispatch with correct job identity/cancellation handling.
- Preserve physical branches in standalone result deduplication using Place ID
  or name/address/site, rather than merging a shared name/site across branches.
- Stream dashboard XLSX exports with per-request temporary files; fix CSV model
  field/header consistency and retain isolated, bounded-memory export behavior.
- Use observed, bounded A/B query-yield estimates while retaining geography
  priorities and existing query history.

Validation includes real SQLite competing-writer/rollback tests; browser DOM
fixtures; full-field paired live detail checks (12 pairs, no field differences);
export XML parity and concurrent-download cleanup; resource lifecycle and
scheduler boundaries; and a successful Next.js production build including type
checks. Grouped contact-field extraction remains OFF: the paired live sample
showed only about 1.04x improvement, too little to justify enabling it broadly.

Live equal-window observation after the 18:11:30 MYT restart, sampled at about
18:24:56 (13.44 minutes each):

| Metric | Before | After |
| --- | ---: | ---: |
| Completed queries | 1 | 12 |
| Processed listings in completed queries | 117 | 627 |
| New qualified locations attributed to completed queries | 40 | 52 |

Thus processed volume was 5.36x and qualified yield 1.30x in these short windows.
This is NOT a controlled benchmark or a sustained multiplier: query/category
mixes differ, jobs straddle boundaries, and the post window includes ramp-up.
There were no completed-query errors after restart and no memory reclaims in
the observed session. All 55 newly stored locations at sampling had the required
name/address/contact fields; that structural check is not an independent manual
category audit. The live organization lookup now uses the covering index and a
sample took 0.0000785 seconds versus 0.886 seconds before (different organizations,
so rely on the identical-fixture benchmark below for controlled comparison).

Remaining operational limitations: Google supply and duplicate rate dominate
qualified-lead yield; no guaranteed overall multiplier is claimed. Historical
A/B attribution may exceed the new count, so scheduling estimates are bounded.
The API JSON job store and optional legacy snapshot exporter still retain their
history in memory; production Malaysia crawling uses SQLite, not those stores.
The newest dashboard code is active following the final restart. The localhost UI
is intended for trusted local use, not unauthenticated public hosting.

The checkpoint narrative below records intermediate states; statements such as
"deployment pending" there describe those earlier checkpoints, not all changes.

## September 11: controller write-lock regression

Dashboard XLSX generation now streams rows into a disk-backed ZIP instead of
building the entire worksheet in StringIO and then a second UTF-8 bytes copy.
Concurrent downloads use separate temporary files, cleaned up on disconnect.
The former `company_export_xlsx()` bytes-return API remains available, but HTTP
downloads use its disk destination option. All exported XML members match the
previous exporter on Unicode/escaped text/phone/blank/literal-formula fixtures.
In a 10,000-row, 26-column synthetic export, tracemalloc peak Python allocation
fell from 70.484 MB to 0.355 MB; elapsed time was 6.580 versus 6.562 seconds.
This measures Python allocation in the export, not total process RAM or crawl
throughput. Existing saved exports are untouched; new downloads are temporary.

Organization representative lookup used only the global intelligence-version /
score index, filtering organizations while scanning ranked rows. A live read-only
lookup took 0.886 seconds. Added a covering index on organization ID, intelligence
version, descending score and company ID. In a 211,000-row in-memory fixture,
four lookups over five repetitions had median 0.288043 seconds before and
0.0000155 seconds after, with identical selected IDs. This is a query-level
microbenchmark, not an end-to-end crawl multiplier. The automated test confirms
the query plan uses the new index and agrees with an unindexed lookup.

Removed the enrichment runner's duplicate schema initialization: `open_db`
already initializes and commits it. The second call opened a metadata write
transaction before network audits. A real SQLite test now verifies another
connection can acquire the writer slot while the audit runs, without networking.

The optional email crawler owned only its context, leaving the separately
launched browser and Playwright driver unclosed. It now tracks and releases all
three resources, including failed initialization. Three async lifecycle tests
cover repeated close, a disconnected context, failed context creation and the
persistent-context path. This is a resource-lifecycle improvement for optional
email jobs, not a measured speed improvement for the Malaysia batch collector.

Follow-up scheduler review: cooldown previously reduced the permitted worker
count to one but left the submission loop open. New submissions now require
the cooldown deadline to have elapsed as well as sufficient RAM. Boundary tests
cover before/at the deadline and the critical-RAM threshold. Existing active
queries are not forcibly killed, and repeated-block halting is unchanged.

Rollout at 18:11 MYT: created a SQLite backup containing 211,332 companies
(`data/backups/before-optimization-20260911T101045159230Z.sqlite`, quick_check
`ok`, 2,976,534,528 bytes). Temporarily disabled the watchdog and terminated
only the verified collector tree rooted at PID 15420. Re-enabled the watchdog
and resumed through its existing script. New controller PID 13036 started at
18:11:30 and submitted queries at 18:11:43, scaling to two workers at 18:11:53.
All 38 tests pass. Sustained post-rollout performance is still unmeasured.

The memory policy also bypassed its existing 2 GB launch threshold when scaling
from one to four workers. Production events showed repeated launches at 1.06–1.07
GB followed by immediate downscales below 0.65 GB and reclaimed work. The same
launch threshold now applies to base workers; the 1 GB reserve, six-worker cap,
upscale stability timers and Google-block controls remain unchanged. Boundary
tests cover all three base upscale transitions. This prevents the observed
low-headroom launch condition without forcing healthy running browsers closed.

The controller's `record_checkpoints` issued `INSERT OR IGNORE` after the
query-completion commit but never committed its own transaction. This includes
already-recorded checkpoints. It could retain SQLite's writer slot while waiting
for the next browser worker, causing that worker's immediate persistence to time
out. Recent production errors repeatedly reported `database is locked`; the
20-query error window then restricted concurrency to one browser.

A real SQLite regression test failed before the fix because the controller
remained in a transaction. Checkpoint writes now commit before returning. The
test checks both first-time and repeated checkpoints and acquisition of the
writer slot from a separate connection. Live rollout/throughput verification
remains required; this is not yet a measured end-to-end speed improvement.

## Scope and baseline (historical kickoff)

Requested: review the full codebase, research methodology, improve throughput on
the existing laptop, preserve consistency and accuracy, test, commit and push.
At this initial checkpoint, review and rollout were incomplete. Do not interpret
any microbenchmark as an end-to-end crawling speed claim; use the consolidated
verification above for the final state.

Live status observed 6 September 2026: 206,009 qualified locations, 32.96 queries
per hour over 24 hours, 126.08 new qualified locations per hour, six active
browsers, 1.89 GB available RAM. Recent jobs contain many duplicate results.

## Research

- Playwright supports evaluating a group of elements in the browser, eliminating
  individual protocol calls: https://playwright.dev/python/docs/locators#rare-use-cases
  and https://playwright.dev/python/docs/api/class-locator#locator-evaluate-all
- SQLite WAL supports concurrent readers but only one writer. Short transactions
  and bounded read workloads matter: https://www.sqlite.org/wal.html and
  https://www.sqlite.org/isolation.html

## Implemented: batched feed links

Read href attributes for the whole feed in one browser call instead of one call
per anchor. Preserve original attribute values, order, duplicates and filtering;
the existing deduplication and scrolling decisions are unchanged.

Run ` .venv\Scripts\python.exe -m tests.benchmark_feed_links` from the repository.
This uses offline DOM fixtures in real Chromium and does not write lead data or
contact Google. Three repetitions per case, median wall time:

| Links | Original seconds | Batched seconds | Exact output parity |
| --- | ---: | ---: | --- |
| 20 | 0.1796 | 0.0062 | Yes |
| 100 | 0.6544 | 0.0062 | Yes |
| 200 | 1.1241 | 0.0078 | Yes |
| 500 | 3.1124 | 0.0114 | Yes |

The existing 16-test suite passes. The current collector was not restarted for
this first change; long-lived workers may still hold the original implementation.

## Findings requiring further implementation/verification

### Ninth checkpoint: backed-up production rollout

All 33 tests passed before rollout. On 6 September at 18:06 MYT, temporarily
disabled the watchdog, stopped the verified collector process tree, and removed
four multiprocessing workers whose September 3 parent no longer existed. These
were confirmed by parent PID, command line and creation time; no user browser
processes were targeted.

Added `automation/backup_database.py` using SQLite's backup API, an exclusive
timestamped destination and `PRAGMA quick_check`. The actual backup under
`data/backups/before-optimization-20260906T100644176913Z.sqlite` passed quick_check,
contains 206,042 companies and is 2,840,104,960 bytes. It remains excluded from git.

Re-enabled the scheduled watchdog and resumed through its existing script at
18:07 MYT. Controller PID 17996 (launcher 30056) rebuilt the manifest and started
queries successfully. At 18:08 the status endpoint returned a fresh heartbeat,
three active browsers, 4.23 GB available RAM and no halt reason. Grouped contact
extraction remains disabled. This proves startup, not sustained throughput:
durable new observations, completed-query outcomes and post-rollout metrics
still require inspection before closing the goal.

### Eighth checkpoint: measured sales-yield scheduling

Read-only 24-hour analysis: 786 completed queries, 62,174 processed listings,
35,105 duplicates (56.46%), 2,981 new locations and 3,029 attributed A/B leads.
The last two counters demonstrate that overlapping-query attribution cannot be
assumed to be unique. No historical counters were rewritten.

Scheduling estimates now prefer measured V4 A/B yield after at least three
completed queries for a sector/term, bounded by each query's new-location counter.
Unknown terms retain the existing historical/sector fallback. Explicit zero
yield is preserved instead of being replaced with an eight-lead default.
Market allocation and industry priority bands are unchanged. Estimates rebuild
when the manifest is built; current long-lived worker scheduling is not yet
reloaded. Runtime reprioritization and the live rollout remain outstanding.

Current data supplies 94 measured terms; one is zero-yield. Highest observed
averages include bengkel kereta (26.52 A/B/query), klinik perubatan (23.87), and
motorcycle workshop (20.26). These are historical, not a forecast for new areas.
Regression coverage verifies zero preservation, overlap capping and the
three-sample threshold. All 17 tests in `tests.test_v2` pass.

### Seventh checkpoint: whole-detail paired baseline and export review

Added `python -m tests.live_detail_comparison`: loads the trusted pre-optimization
extractor from git commit 684bc52, freezes a 12-location sample alternating sites
and phone-only businesses, uses fresh contexts and alternates baseline/candidate
order. It compares every CompanyData field including name, address, category,
contact, ratings, hours and closure state. Results are private under ignored
`data/detail_comparison.json`.

Completed 12 pairs: baseline 41.782 seconds total, candidate 40.248 seconds,
ratio 1.038x. All fields matched in all pairs, with no failed extractions or
reported blocks. This small sequential sample shows only a modest difference,
not a reliable multiplier. It excludes feed scrolling, persistence and scheduling.
The grouped-contact experiment therefore stays disabled pending stronger evidence;
duplicate work and scheduling remain the more promising next areas.

Reviewed CompanyData, URL validation, output serialization and dashboard read/
export paths. Found and fixed a stale CSV header that rejected V4 identity/contact
fields; exports now retain every model field and serialize emails as JSON.
Removed an emoji that crashed file-export confirmation on Windows cp1252 consoles.
Fixed the CompanyData URL-validator import for repository-root invocation.
Added CSV round-trip and URL-validation regression tests.

### Sixth checkpoint: API dispatch correctness and bounded concurrency

Standalone API crawl and email callbacks now serialize their respective queues,
matching their single-active-job state. Each callback uses the actual dequeued
job ID for completion/failure, eliminating cross-job result attribution when
callbacks arrive out of order. Cancelled running jobs discard late results and
do not trigger automatic email extraction or become failed on a late exception.
An in-flight browser is still allowed to finish before releasing its queue slot;
this change does not claim immediate browser termination on cancellation.

The Malaysia batch ProcessPoolExecutor is independent and retains adaptive
concurrency. Three integration tests isolate API stores in a temporary directory
and use no network: reordered callbacks with a maximum of one active crawl,
cancelled-job handling, and email result identity. The full 30-test suite passes.
No production API or collector restart has been performed for these checkpoints.

### Fifth checkpoint: worker import isolation

Moved the shared synchronous crawl function unchanged into `core/crawl_runner.py`.
The API re-exports the same function; batch workers import the lightweight module
directly, avoiding FastAPI, queue loading and SMTP-store initialization. Existing
browser lifecycle, callbacks, settings, metrics and result serialization remain
the same. Fresh-process inspection confirmed no `core.api` or `fastapi` modules
loaded. The isolated import took 0.469 seconds on this laptop; an old-path timing
baseline has not been measured, so no relative speed claim is made.

Three additional tests verify fresh-process import isolation, settings/callbacks/
metric propagation and browser cleanup following extraction failure. They pass,
as do the previous 24 tests. The full live rollout remains pending.

### Fourth checkpoint: localized opening-hours fallback

The fallback loop stopped on the truthy string `N/A`, preventing it from reaching
other selectors. Replaced it with a shared two-second locator wait for English,
Malay and German hours labels, retaining the main-container preference. Patterns
avoid treating a business name such as `Open Studio Design` as opening hours.
Real Chromium tests cover eight localized labels, a delayed label, absent hours,
and main-container precedence. All 24 regression tests pass.

A second eight-listing live diagnostic completed without blocks or contact-field
differences. Five returned opening hours in 0–31 ms; three returned `N/A` after
the bounded two-second wait. This did not compare old versus new hours on the
same document and is not a population-level accuracy or speed measurement.

Additional review finding: batch workers import `core.api.server`, which creates
queue and email-store globals at import time. Investigate separating the shared
crawl runner from the web application so collection need not load API state.
The standalone API also still dequeues a job independently of its supplied job
ID; its concurrency and cancellation behavior need review before final acceptance.

### Third checkpoint: experimental grouped contact extraction

`MapsExtractor(batched_details=True)` reads address, website, phone and plus code
after one shared two-second optional-field wait. Default remains false pending
broader tests, so production extraction behavior is unchanged by this experiment.
Run `python -m tests.benchmark_contact_fields` for offline Chromium fixtures.

| Fixture | Sequential seconds | Grouped seconds | Output parity |
| --- | ---: | ---: | --- |
| All fields present | 0.060 | 0.039 | Exact |
| All fields absent | 8.047 | 2.019 | Exact |
| Fields arrive after 1.5 seconds | 1.620 | 1.527 | Exact |
| Duplicate phone selector | 0.044 | 0.024 | Exact |

`python -m tests.live_contact_canary` completed eight live listings with zero
contact differences or blocking. Four listings had all four fields; four had
three. The grouped path was read first and the reference path second on the same
page; reference timing therefore is NOT an independent navigation baseline.
This is only an early field-parity check. Listings with one absent field show
little timing benefit. It does not prove population-level accuracy or overall
throughput improvement. Existing 20 regression tests also pass.

Late fields beyond the shared wait require further characterization. The current
opening-hours fallbacks are German-only despite en-MY production locale; assess
English/Malay extraction and its waits alongside grouped contact rollout.

### Second implementation checkpoint: persistence and worker setup

- Worker processes now open only an existing database with `mode=rw`; startup
  migration remains in the controller. Missing paths fail rather than creating
  an empty database. Twenty-run median in an empty initialized fixture:
  full setup 13.094 ms, worker connection 5.250 ms. This is a modest startup gain,
  not an end-to-end throughput multiplier.
- Observation writes acquire `BEGIN IMMEDIATE` before identity lookup and commit
  atomically, including qualification, provenance and scoring. Exceptions roll
  back the complete observation. Existing per-observation durability is retained.
- Result callbacks execute outside the extractor's skip-on-extraction-error
  handler. Persistence failure now aborts the query and reaches the controller's
  existing retry policy rather than silently completing a partially saved query.
- Processed counters advance only after persistence succeeds.
- All 20 tests pass, including real SQLite concurrent-writer tests (eight saves,
  one location/observation), injected scoring rollback followed by successful
  retry, callback error propagation, and missing-worker-database rejection.
- Deployment is still pending the broader extraction and live-canary work below.

- Missing optional fields each use an independent two-second locator wait.
  A shared readiness check and batched extraction could save seconds per listing,
  but delayed field arrival must be tested before replacing those waits.
- Every query calls `open_db`, including schema checks and migrations. Separate
  startup migration from worker connection setup after measuring its cost.
- Each listing persists and scores synchronously. Preserve immediate commits;
  inspect lock retries and callback failure propagation before increasing load.
- Overlapping queries revisit the same listings. Any detail-cache optimization
  must preserve provenance, category qualification, physical branches, freshness,
  rejection evidence and accurate counters.
- Review scheduler yield and geography, enrichment, API, dashboard/export paths,
  watchdog recovery and tests as part of the remaining whole-codebase review.
- Measure a controlled live canary against a recorded baseline, including field
  completeness, accepted/rejected counts, blocks, RAM and durable persistence.
- Complete rollout and push the final tested changes. Only then mark the overall
  optimization goal complete.

### 14 September 2026: recovery and yield scheduling

- The apparent one-browser RAM limit was caused by SQLite `database is locked`
  failures at six browsers being counted as browser failures. The controller
  now isolates storage-lock recovery from browser and Google-block health,
  temporarily caps concurrency at three for three minutes, and then resumes
  gradual scaling. Storage-lock retries return to pending without consuming a
  query attempt. Google CAPTCHA/throttle rules are unchanged.
- The RAM reserve is 0.5 GB with a 1.5 GB launch threshold. This is a floor,
  not a promise of six concurrent browsers: four active browsers left about
  1.3 GB free on this laptop during rollout. Launching another would be unsafe
  until memory rises or each browser's footprint falls.
- Repeated, identical Maps observations now skip the writer lock and scoring.
  Writes taking at least two seconds log lock-wait and transaction timings.
- Pending work is ranked within the existing 55/25/20 market allocation using
  measured A/B leads per query-hour by term, state and geo level, with one in
  five slots retained for static-priority exploration. Recent rates are
  recalculated every ten completed queries; newly generated children no longer
  monopolize the front of the queue.
- These changes passed 55 local unit/integration tests. The claimed doubling
  of qualified leads per hour is a target, not yet a measured result; compare
  equal rolling windows after enough post-restart queries finish.
