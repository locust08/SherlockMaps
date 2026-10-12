"""Deadline regressions: only browser/network work is replaced with local hangs."""
import asyncio
import importlib
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core.extractors.email_extractor import EmailCrawlerConfig, EmailExtractor, extract_emails_from_websites
from core.api.queue_manager import QueueManager
from core.async_deadline import bounded_await, CancellationStalled
from core.models import EmailData


class EmailTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_browser_driver_is_disposed_without_launching_after_timeout(self):
        release = asyncio.Event()
        driver = AsyncMock()

        async def late_start():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return driver

        manager = MagicMock()
        manager.start = AsyncMock(side_effect=late_start)
        with patch("playwright.async_api.async_playwright", return_value=manager):
            extractor = EmailExtractor(EmailCrawlerConfig(timeout_per_website=0.01))
            with self.assertRaises(TimeoutError):
                await extractor.__aenter__()
            release.set()
            await asyncio.sleep(0.03)
            driver.stop.assert_awaited_once()
            driver.chromium.launch.assert_not_awaited()

    async def test_job_deadline_cleans_browser_even_if_initialization_resists_cancellation(self):
        release, stopped = asyncio.Event(), asyncio.Event()
        driver, browser = AsyncMock(), AsyncMock()
        driver.chromium.launch.return_value = browser

        async def stuck_context():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                stopped.set()
            raise RuntimeError("test driver disconnected")

        browser.new_context.side_effect = stuck_context
        manager = MagicMock()
        manager.start = AsyncMock(return_value=driver)
        try:
            with patch("playwright.async_api.async_playwright", return_value=manager):
                with self.assertRaises((TimeoutError, CancellationStalled)):
                    await bounded_await(extract_emails_from_websites([], "parent",
                        EmailCrawlerConfig(cleanup_timeout=0.01)), 0.01, cancellation_grace=0.05)
                browser.close.assert_awaited_once()
                driver.stop.assert_awaited_once()
        finally:
            release.set()
            await asyncio.wait_for(stopped.wait(), 0.2)

    async def test_deadline_does_not_wait_forever_for_cancellation_acknowledgement(self):
        release = asyncio.Event()
        stopped = asyncio.Event()

        async def resistant():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                stopped.set()

        try:
            with self.assertRaises(CancellationStalled):
                await asyncio.wait_for(bounded_await(resistant(), 0.01, cancellation_grace=0.01), 0.2)
        finally:
            release.set()
            await asyncio.wait_for(stopped.wait(), 0.2)

    async def test_invalid_deadlines_cannot_disable_timeout_protection(self):
        for value in (0, -1, float("inf"), float("nan")):
            for field in ("timeout_per_website", "cleanup_timeout"):
                with self.subTest(value=value, field=field), self.assertRaises(ValueError):
                    EmailCrawlerConfig(**{field: value})

    async def test_website_timeout_preserves_already_discovered_email(self):
        extractor = EmailExtractor(EmailCrawlerConfig(timeout_per_website=0.01, crawl_delay=0))
        email = EmailData(email="contact@slow.example", company_name="slow",
                          source_url="https://slow.example", job_id="parent", domain="slow.example")

        async def crawl(url, name, job):
            extractor._emails = {email.email: email}
            await asyncio.Event().wait()

        with patch.object(extractor, "crawl_website", side_effect=crawl):
            results = await extractor.crawl_websites([
                {"website": "https://slow.example", "name": "slow"}], "parent")
        self.assertEqual([item.email for item in results], ["contact@slow.example"])

    async def test_stalled_website_is_skipped_and_next_website_runs(self):
        extractor = EmailExtractor(EmailCrawlerConfig(timeout_per_website=0.01, crawl_delay=0))
        visited = []

        async def crawl(url, name, job):
            visited.append(url)
            if name == "slow":
                await asyncio.Event().wait()
            return []

        with patch.object(extractor, "crawl_website", side_effect=crawl):
            await asyncio.wait_for(extractor.crawl_websites([
                {"website": "https://slow.example", "name": "slow"},
                {"website": "https://fast.example", "name": "fast"}], "parent"), 0.2)
        self.assertEqual(visited, ["https://slow.example", "https://fast.example"])

    async def test_browser_cleanup_hang_does_not_prevent_driver_cleanup(self):
        extractor = EmailExtractor()
        extractor.config.cleanup_timeout = 0.01
        context, browser, driver = AsyncMock(), AsyncMock(), AsyncMock()

        async def hang():
            await asyncio.Event().wait()

        context.close.side_effect = hang
        extractor._browser_context, extractor._browser, extractor._playwright = context, browser, driver
        await asyncio.wait_for(extractor.close(), 0.2)
        driver.stop.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_job_timeout_releases_real_queue_and_next_job_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"JOBS_DATA_DIR": directory, "SMTP_DATA_DIR": directory}):
                server = importlib.import_module("core.api.server")
                queue = QueueManager()
            slow = await queue.add_email_job("parent-slow", [])
            fast = await queue.add_email_job("parent-fast", [])

            async def extract(websites, job_id, config):
                if job_id == "parent-slow":
                    await asyncio.Event().wait()
                return []

            with patch.object(server, "queue_manager", queue), patch.object(
                server, "extract_emails_from_websites", side_effect=extract
            ), patch.object(server, "_email_dispatch_lock", asyncio.Lock()), patch.object(
                server, "EMAIL_JOB_TIMEOUT_SECONDS", 0.01, create=True
            ):
                await asyncio.wait_for(asyncio.gather(
                    server._process_email_job(slow.job_id), server._process_email_job(fast.job_id)), 0.3)
            self.assertEqual(slow.status, server.JobStatus.FAILED)
            self.assertIn("timeout", slow.error.lower())
            self.assertEqual(fast.status, server.JobStatus.COMPLETED)
            self.assertEqual(queue.active_email_jobs, 0)
