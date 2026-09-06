import subprocess
import sys
import unittest
from unittest.mock import patch

from core.crawl_runner import run_crawl_in_process
from core.models import CompanyData


class CrawlRunnerTests(unittest.TestCase):
    def test_worker_import_has_no_api_state(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import core.crawl_runner; "
             "assert not any(k.startswith('core.api') for k in sys.modules); "
             "assert 'fastapi' not in sys.modules"], capture_output=True, text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preserves_settings_callbacks_and_metrics(self):
        callback, memory_guard = lambda company: None, lambda: False
        company = CompanyData(name="Example Clinic", website="https://example.com")
        with patch("core.crawl_runner.BrowserManager") as manager_type, patch(
            "core.crawl_runner.MapsExtractor"
        ) as extractor_type:
            extractor = extractor_type.return_value
            extractor.extract_all.return_value = [company]
            extractor.links_discovered = 3
            extractor.end_of_results = True
            extractor.page_recycle_count = 1
            extractor.memory_cleanup_count = 2
            result = run_crawl_in_process(
                "dentist in Kuala Lumpur", headless=True, locale="en-MY",
                max_results=100, adaptive_results=200, hard_result_cap=500,
                scroll_timeout=120, max_scroll_attempts=8, include_metrics=True,
                result_callback=callback, memory_guard=memory_guard, track_reviews=False,
            )
            config = manager_type.call_args.args[0]
            self.assertEqual((config.locale, config.scroll_timeout, config.max_scroll_attempts), ("en-MY", 120, 8))
            self.assertEqual(extractor_type.call_args.kwargs["hard_result_cap"], 500)
            self.assertIs(extractor_type.call_args.kwargs["result_callback"], callback)
            self.assertIs(extractor_type.call_args.kwargs["memory_guard"], memory_guard)
            extractor.extract_all.assert_called_once_with(track_reviews=False)
            self.assertEqual(result["results"], [company.to_dict()])
            self.assertEqual(result["processed_count"], 1)
            self.assertEqual(result["links_discovered"], 3)
            self.assertEqual(result["memory_cleanup_count"], 2)
            manager_type.return_value.close.assert_called_once()

    def test_extraction_failure_still_closes_browser(self):
        with patch("core.crawl_runner.BrowserManager") as manager_type, patch(
            "core.crawl_runner.MapsExtractor"
        ) as extractor_type:
            extractor_type.return_value.extract_all.side_effect = RuntimeError("persistence failed")
            with self.assertRaisesRegex(RuntimeError, "persistence failed"):
                run_crawl_in_process("fixture")
            manager_type.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
