"""Actual Chromium selector tests for localized opening-hours extraction."""

import html
import unittest

from playwright.sync_api import sync_playwright
from core.extractors.maps_extractor import MapsExtractor


class OpeningHoursTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        cls.browser = cls.runtime.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.extractor = MapsExtractor(self.page)

    def tearDown(self):
        self.page.close()

    def test_supported_locale_labels(self):
        for label in ("Open ⋅ Closes 10 pm", "Closed ⋅ Opens 9 am", "Open 24 hours",
                      "Buka ⋅ Tutup 10 PTG", "Dibuka 9 PG", "Geschlossen",
                      "Öffnet um 09:00", "Rund um die Uhr geöffnet"):
            with self.subTest(label=label):
                self.page.set_content(f'<div class="fontBodyMedium"><span>{html.escape(label)}</span></div>')
                self.assertEqual(self.extractor._extract_opening_hours(), label)

    def test_does_not_mistake_business_name_for_hours(self):
        self.page.set_content('<div class="fontBodyMedium"><span>Open Studio Design</span></div>')
        self.assertEqual(self.extractor._extract_opening_hours(), "N/A")

    def test_main_container_remains_preferred(self):
        self.page.set_content('<div class="MkV9">Monday 9 am–5 pm</div>'
                              '<div class="fontBodyMedium"><span>Closed</span></div>')
        self.assertEqual(self.extractor._extract_opening_hours(), "Monday 9 am–5 pm")

    def test_late_label_uses_shared_wait(self):
        self.page.set_content('''<div class="fontBodyMedium" id="hours"></div>
            <script>setTimeout(() => {document.getElementById('hours').innerHTML =
            '<span>Open 24 hours</span>';}, 1000)</script>''')
        self.assertEqual(self.extractor._extract_opening_hours(), "Open 24 hours")


if __name__ == "__main__":
    unittest.main()
