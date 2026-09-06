"""Offline browser benchmark: python -m tests.benchmark_feed_links.

Uses generated DOM fixtures, never contacts Google or touches the lead database.
Checks exact output parity with the old per-element extraction path.
"""

import html
import json
import statistics
import time

from playwright.sync_api import sync_playwright

from core.extractors.maps_extractor import MapsExtractor


def legacy_links(feed):
    links = []
    for element in feed.query_selector_all(MapsExtractor.LINK_SELECTOR):
        href = element.get_attribute("href")
        if href and href.startswith("https://www.google.com/maps/place/") and len(href) > 40:
            links.append(href)
    return links


def main():
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for count in (20, 100, 200, 500):
                valid = [f"https://www.google.com/maps/place/Business-{index}?x=1&y=2" for index in range(count)]
                hrefs = valid + [valid[0], "/maps/place/relative", "https://example.com/irrelevant", ""]
                page.set_content('<div role="feed">' + ''.join(
                    f'<a class="hfpxzc" href="{html.escape(url, quote=True)}">Business</a>'
                    for url in hrefs
                ) + '<a class="hfpxzc">Missing href</a></div>')
                feed = page.query_selector('[role="feed"]')
                extractor = MapsExtractor(page)
                timings = {"legacy": [], "batched": []}
                for _ in range(3):
                    for name, extract in (("legacy", legacy_links), ("batched", extractor._extract_links_from_feed)):
                        started = time.perf_counter()
                        actual = extract(feed)
                        timings[name].append(time.perf_counter() - started)
                        assert actual == valid + [valid[0]], (name, count, actual)
                old, new = (statistics.median(timings[key]) for key in ("legacy", "batched"))
                results.append({"links": count, "parity": True, "legacy_seconds": round(old, 4),
                                "batched_seconds": round(new, 4), "speedup": round(old / new, 2)})
        finally:
            browser.close()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
