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
