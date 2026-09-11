import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.extractors.email_extractor import EmailExtractor, EmailCrawlerConfig


class EmailLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_releases_browser_and_driver_and_is_idempotent(self):
        extractor = EmailExtractor()
        context, browser, driver = AsyncMock(), AsyncMock(), AsyncMock()
        extractor._browser_context = context
        extractor._browser = browser
        extractor._playwright = driver
        context.close.side_effect = RuntimeError("already disconnected")
        with self.assertLogs("core.extractors.email_extractor", level="WARNING"):
            await extractor.close()
        await extractor.close()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        driver.stop.assert_awaited_once()

    async def test_failed_context_creation_releases_partial_browser(self):
        driver, browser = AsyncMock(), AsyncMock()
        driver.chromium.launch.return_value = browser
        browser.new_context.side_effect = RuntimeError("fixture context failure")
        manager = MagicMock()
        manager.start = AsyncMock(return_value=driver)
        with patch("playwright.async_api.async_playwright", return_value=manager):
            with self.assertRaisesRegex(RuntimeError, "fixture context failure"):
                async with EmailExtractor():
                    self.fail("Initialization should fail")
        browser.close.assert_awaited_once()
        driver.stop.assert_awaited_once()

    async def test_persistent_context_stops_driver_without_separate_browser(self):
        driver, context = AsyncMock(), AsyncMock()
        driver.chromium.launch_persistent_context.return_value = context
        manager = MagicMock()
        manager.start = AsyncMock(return_value=driver)
        with patch("playwright.async_api.async_playwright", return_value=manager):
            async with EmailExtractor(EmailCrawlerConfig(chrome_profile_path="fixture")):
                pass
        context.close.assert_awaited_once()
        driver.stop.assert_awaited_once()
        driver.chromium.launch.assert_not_awaited()
