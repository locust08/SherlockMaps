import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError
from core.exceptions import ExtractionError
from core.extractors.maps_extractor import MapsExtractor


class FeedOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        self.page.url = "https://www.google.com/maps/search/fixture"
        self.page.wait_for_selector.side_effect = TimeoutError("fixture timeout")
        self.page.get_by_text.return_value.first.is_visible.return_value = False
        self.extractor = MapsExtractor(self.page)

    def test_timeout_is_retriable_not_zero_yield(self):
        with patch.object(self.extractor, "_raise_if_blocked"):
            with self.assertRaisesRegex(ExtractionError, "FEED_TIMEOUT"):
                self.extractor._collect_result_links()

    def test_visible_empty_message_returns_empty(self):
        self.page.get_by_text.return_value.first.is_visible.return_value = True
        with patch.object(self.extractor, "_raise_if_blocked"):
            self.assertEqual(self.extractor._collect_result_links(), [])

    def test_direct_place_search_yields_one_link(self):
        self.page.url = "https://www.google.com/maps/place/Fixture/data=!1sfixture"
        self.page.locator.return_value.count.return_value = 1
        with patch.object(self.extractor, "_raise_if_blocked"):
            self.assertEqual(self.extractor._collect_result_links(), [self.page.url])
        self.assertEqual(self.extractor.links_discovered, 1)
        self.assertEqual(self.extractor.processing_limit, 1)

    def test_block_is_not_treated_as_empty(self):
        with patch.object(self.extractor, "_raise_if_blocked", side_effect=ExtractionError("GOOGLE_BLOCK")):
            with self.assertRaisesRegex(ExtractionError, "GOOGLE_BLOCK"):
                self.extractor._collect_result_links()
