"""Compare legacy contact reads with the experimental grouped read in Chromium."""

import json
import time

from playwright.sync_api import sync_playwright
from core.extractors.maps_extractor import MapsExtractor


CONTACTS = '''<button data-item-id="address"><div class="Io6YTe">Kuala Lumpur, Malaysia</div></button>
<a data-item-id="authority" href="https://example.com/">Website</a>
<button data-item-id="phone:tel:0312345678"><div class="Io6YTe">03 1234 5678</div></button>
<button data-item-id="oloc"><div class="Io6YTe">ABCD+12</div></button>'''


def legacy(extractor):
    page = extractor._page
    return {
        "address": extractor._safe_text(page.locator(extractor.ADDRESS_CONTAINER_SELECTOR)),
        "website": extractor._safe_attribute(page.locator(extractor.WEBSITE_SELECTOR), "href"),
        "phone": extractor._safe_text(page.locator(extractor.PHONE_CONTAINER_SELECTOR)),
        "plus_code": extractor._safe_text(page.locator(extractor.PLUS_CODE_SELECTOR)),
    }


def main():
    rows = []
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            extractor = MapsExtractor(page)
            fixtures = {
                "all_present": CONTACTS,
                "all_absent": "<h1>Business without contacts</h1>",
                "delayed_1500ms": "<div id='details'></div><script>setTimeout(() => {document.getElementById('details').innerHTML = "
                    + json.dumps(CONTACTS) + ";}, 1500)</script>",
                "duplicate_phone": CONTACTS + '<button data-item-id="phone:tel:2"><div class="Io6YTe">Other phone</div></button>',
            }
            for name, markup in fixtures.items():
                samples = {}
                values = {}
                for label, method in (("legacy", lambda: legacy(extractor)), ("batched", extractor._extract_contact_fields)):
                    page.goto("about:blank")
                    page.set_content(markup)
                    start = time.perf_counter()
                    values[label] = method()
                    samples[label] = round(time.perf_counter() - start, 3)
                assert values["legacy"] == values["batched"], (name, values)
                if name in ("all_present", "delayed_1500ms"):
                    assert all(value != "N/A" for value in values["batched"].values())
                rows.append({"fixture": name, "seconds": samples, "exact_parity": True})
        finally:
            browser.close()
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
