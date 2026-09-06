"""Dispatch integration checks without browsers, email or production API state."""
import asyncio
import importlib
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"JOBS_DATA_DIR": cls.directory.name,
                                     "SMTP_DATA_DIR": cls.directory.name}):
            cls.server = importlib.import_module("core.api.server")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    async def test_out_of_order_callbacks_keep_results_and_browsers_serial(self):
        server = self.server
        jobs = [SimpleNamespace(job_id=str(i), prompt=f"prompt-{i}", output_format="json",
                    headless=True, locale="en-MY", max_results=100, track_reviews=False,
                    auto_email_crawl=False, status=server.JobStatus.RUNNING) for i in range(2)]
        queue = SimpleNamespace(get_next_job=AsyncMock(side_effect=jobs),
                                complete_job=AsyncMock(), fail_job=AsyncMock())
        active, peak = 0, 0
        def crawl(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            time.sleep(0.03)
            active -= 1
            return [{"name": kwargs["prompt"]}]
        with patch.object(server, "queue_manager", queue), patch.object(
            server, "run_crawl_in_process", side_effect=crawl
        ), patch.object(server, "_crawl_dispatch_lock", asyncio.Lock()):
            await asyncio.gather(server._process_job("1"), server._process_job("0"))
        self.assertEqual(peak, 1)
        self.assertEqual([call.args for call in queue.complete_job.await_args_list],
                         [("0", [{"name": "prompt-0"}]), ("1", [{"name": "prompt-1"}])])
        queue.fail_job.assert_not_awaited()

    async def test_cancelled_running_job_does_not_complete_or_start_email(self):
        server = self.server
        job = SimpleNamespace(job_id="actual", prompt="fixture", output_format="json",
                headless=True, locale="en-MY", max_results=1, track_reviews=False,
                auto_email_crawl=True, status=server.JobStatus.RUNNING)
        queue = SimpleNamespace(get_next_job=AsyncMock(return_value=job),
                                complete_job=AsyncMock(), fail_job=AsyncMock())
        def crawl(**kwargs):
            job.status = server.JobStatus.CANCELLED
            return [{"website": "https://example.com"}]
        with patch.object(server, "queue_manager", queue), patch.object(
            server, "run_crawl_in_process", side_effect=crawl
        ), patch.object(server, "_crawl_dispatch_lock", asyncio.Lock()):
            await server._process_job("callback")
        queue.complete_job.assert_not_awaited()
        queue.fail_job.assert_not_awaited()

    async def test_email_results_use_dequeued_job_id(self):
        server = self.server
        job = SimpleNamespace(job_id="email-actual", parent_job_id="parent",
                              websites=[], chrome_profile_path="", status=server.JobStatus.RUNNING)
        queue = SimpleNamespace(get_next_email_job=AsyncMock(return_value=job),
                                complete_email_job=AsyncMock(), fail_email_job=AsyncMock())
        with patch.object(server, "queue_manager", queue), patch.object(
            server, "extract_emails_from_websites", AsyncMock(return_value=[])
        ), patch.object(server, "_email_dispatch_lock", asyncio.Lock()):
            await server._process_email_job("different-callback-id")
        queue.complete_email_job.assert_awaited_once_with("email-actual", [])
        queue.fail_email_job.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
