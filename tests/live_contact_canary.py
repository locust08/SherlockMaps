"""Read-only paired live contact comparison. Does not modify the lead database.

Run manually: python -m tests.live_contact_canary
Small diagnostic sample only; not a substitute for a full rollout canary.
"""

import json
import sqlite3
import time

from batch_collect_malaysia_v2 import DB_PATH
from core.browser.browser_manager import BrowserManager
from core.extractors.maps_extractor import MapsExtractor
from core.models import CrawlerConfig
from tests.benchmark_contact_fields import legacy


def main():
    with sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        urls = [row[0] for row in conn.execute(
            "SELECT source_url FROM companies WHERE source_url LIKE 'https://www.google.com/maps/place/%' ORDER BY id DESC LIMIT 8"
        )]
    manager = BrowserManager(CrawlerConfig(headless=True, locale="en-MY"))
    manager.initialize()
    try:
        for index, url in enumerate(urls):
            page = manager.recycle_context()
            extractor = MapsExtractor(page)
            started = time.monotonic()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                extractor._raise_if_blocked()
                page.wait_for_selector(extractor.NAME_SELECTOR, timeout=15000)
                measured = time.monotonic()
                fast = extractor._extract_contact_fields()
                fast_seconds = time.monotonic() - measured
                measured = time.monotonic()
                reference = legacy(extractor)
                reference_seconds = time.monotonic() - measured
                differences = [key for key in fast if fast[key] != reference[key]]
                print(json.dumps({"index": index, "fast_seconds": round(fast_seconds, 3),
                    "reference_seconds_after_fast": round(reference_seconds, 3),
                    "differences": differences, "present_fields": sum(v != "N/A" for v in reference.values()),
                    "total_seconds": round(time.monotonic() - started, 3)}), flush=True)
            except Exception as exc:
                print(json.dumps({"index": index, "error": str(exc)}), flush=True)
                if any(marker in str(exc).lower() for marker in ("google_block", "captcha", "unusual traffic")):
                    break
    finally:
        manager.close()


if __name__ == "__main__":
    main()
