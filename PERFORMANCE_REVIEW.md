# Crawling performance review — in progress

## Scope and baseline

Requested: review the full codebase, research methodology, improve throughput on
the existing laptop, preserve consistency and accuracy, test, commit and push.
The review and rollout are not complete. Do not interpret the microbenchmark as
an end-to-end crawling speed claim.

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
