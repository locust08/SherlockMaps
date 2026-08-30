from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from batch_collect_malaysia_v2 import (
    QueryTask,
    CLASSIFICATION_ONLY_INDUSTRIES,
    SECTOR_TERMS,
    build_manifest,
    desired_worker_count,
    open_db,
    persist_observation,
    qualified_count,
    ram_operating_state,
    register_manifest,
    rolling_metrics,
    worker_upscale_stable_seconds,
    TARGET,
)
from core.browser.browser_manager import BrowserManager
from core.exceptions import MemoryPressureError
from core.extractors.maps_extractor import MapsExtractor
from core.models import CrawlerConfig
from lead_intelligence_v4 import backfill_v4, bulk_score_missing, organization_key, update_sales_lead
from progress_dashboard import call_list_query, dashboard_page


class V3CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "test.sqlite"
        self.conn = open_db(self.db_path)
        self.task = QueryTask(
            "dentist in Bangsar, Federal Territory, Malaysia",
            "Health, Fitness & Wellness",
            "Bangsar",
            "Federal Territory",
            "dentist",
            "district",
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def test_manifest_has_primary_market_capacity_and_industries(self) -> None:
        manifest = build_manifest()
        self.assertGreaterEqual(len(manifest), 15_000)
        self.assertEqual(len({task.sector for task in manifest}), 26)
        self.assertTrue(set(CLASSIFICATION_ONLY_INDUSTRIES).isdisjoint(SECTOR_TERMS))
        self.assertEqual(TARGET, 400_000)
        self.assertEqual({task.state for task in manifest}, {"Selangor", "Federal Territory", "Johor", "Penang"})

    def test_website_build_lead_is_accepted(self) -> None:
        reason = persist_observation(self.conn, self.task, {
            "name": "Klinik Pergigian Example",
            "category": "Dental clinic",
            "address": "Bangsar, Kuala Lumpur, Malaysia",
            "phone": "+60312345678",
            "website": "N/A",
            "place_id": "place-one",
            "source_url": "https://www.google.com/maps/place/example",
        })
        self.assertEqual(reason, "new")
        self.assertEqual(qualified_count(self.conn), 1)
        tier = self.conn.execute("SELECT lead_tier FROM companies").fetchone()[0]
        self.assertEqual(tier, "WEBSITE_BUILD")

    def test_phone_deduplicates_without_place_id_but_preserves_distinct_branches(self) -> None:
        base = {
            "name": "Example Dental",
            "category": "Dentist",
            "address": "1 Jalan Example, Kuala Lumpur, Malaysia",
            "phone": "03-1234 5678",
            "website": "https://example.test",
            "source_url": "https://www.google.com/maps/place/example",
        }
        self.assertEqual(persist_observation(self.conn, self.task, dict(base, address="1 Jalan A", place_id="one")), "new")
        self.assertEqual(persist_observation(self.conn, self.task, dict(base, address="2 Jalan B", place_id="two", source_url="https://www.google.com/maps/place/example2")), "new")
        self.assertEqual(qualified_count(self.conn), 2)
        no_place = dict(base, address="3 Jalan C", source_url="https://maps.example/3")
        self.assertEqual(persist_observation(self.conn, self.task, no_place), "duplicate")
        self.assertEqual(qualified_count(self.conn), 2)

    def test_irrelevant_listing_is_rejected(self) -> None:
        reason = persist_observation(self.conn, self.task, {
            "name": "Example Petrol Station", "category": "Gas station",
            "address": "Kuala Lumpur, Malaysia", "phone": "0311111111",
            "source_url": "https://www.google.com/maps/place/petrol",
        })
        self.assertEqual(reason, "irrelevant_category")
        self.assertEqual(qualified_count(self.conn), 0)

    def test_maps_place_identifier_parsing(self) -> None:
        url = "https://www.google.com/maps/place/Example/data=!4m2!3m1!1s0x31cc:0xabcd?entry=ttu"
        self.assertEqual(MapsExtractor.extract_place_id(url), "0x31cc:0xabcd")
        self.assertEqual(MapsExtractor.extract_place_id("https://maps.google.com/?cid=12345"), "12345")

    def test_versioned_jobs_are_append_only(self) -> None:
        task = build_manifest()[0]
        register_manifest(self.conn, [task])
        self.conn.execute(
            """INSERT INTO jobs(prompt,sector,locality,state,term,status,collector_version)
               VALUES(?,?,?,?,?,'completed',2)""",
            (task.prompt, "Legacy", task.locality, task.state, task.term),
        )
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM search_jobs").fetchone()[0], 1)

    def test_ram_worker_headroom(self) -> None:
        self.assertEqual(desired_worker_count(4.0, current_limit=1), 4)
        self.assertEqual(desired_worker_count(2.0, current_limit=4), 5)
        self.assertEqual(desired_worker_count(1.99, current_limit=4), 4)
        self.assertEqual(desired_worker_count(2.0, current_limit=5), 5)
        self.assertEqual(desired_worker_count(
            2.0, current_limit=5, five_worker_canary_complete=True
        ), 6)
        self.assertEqual(desired_worker_count(0.99, current_limit=6), 4)
        self.assertEqual(desired_worker_count(0.64, current_limit=4), 3)
        self.assertEqual(desired_worker_count(4.0, cooldown=True), 1)
        self.assertEqual(desired_worker_count(4.0, error_rate=0.05), 1)
        self.assertEqual(ram_operating_state(2.0), "healthy")
        self.assertEqual(ram_operating_state(1.0), "constrained")
        self.assertEqual(ram_operating_state(0.64), "critical")
        self.assertEqual(worker_upscale_stable_seconds(4), 10)
        self.assertEqual(worker_upscale_stable_seconds(5), 60)
        self.assertEqual(worker_upscale_stable_seconds(6), 300)

    def test_lightweight_browser_config(self) -> None:
        config = CrawlerConfig(headless=True)
        self.assertNotIn("user_data_dir", config.to_browser_args())
        self.assertIn("--disable-gpu", config.to_browser_args()["args"])
        self.assertEqual(config.to_context_args()["viewport"], {"width": 1024, "height": 768})

    def test_resource_filter_blocks_only_visual_and_tracking_assets(self) -> None:
        class Request:
            def __init__(self, resource_type: str, url: str) -> None:
                self.resource_type = resource_type
                self.url = url

        class Route:
            def __init__(self, request: Request) -> None:
                self.request = request
                self.action = ""

            def abort(self) -> None:
                self.action = "abort"

            def continue_(self) -> None:
                self.action = "continue"

        image = Route(Request("image", "https://maps.gstatic.com/tile.png"))
        BrowserManager._route_lightweight_resource(image)
        self.assertEqual(image.action, "abort")
        xhr = Route(Request("xhr", "https://www.google.com/maps/preview/place"))
        BrowserManager._route_lightweight_resource(xhr)
        self.assertEqual(xhr.action, "continue")

    def test_detail_page_recycling_and_memory_guard(self) -> None:
        class FakeContext:
            def new_page(self):
                return FakePage(self)

        class FakePage:
            def __init__(self, context: FakeContext) -> None:
                self.context = context

            def close(self) -> None:
                return None

        context = FakeContext()
        for result_count, expected_recycles in ((20, 2), (100, 10), (200, 20)):
            with self.subTest(result_count=result_count):
                extractor = MapsExtractor(
                    FakePage(context), max_results=result_count, page_recycle_interval=10
                )
                extractor.processing_limit = result_count
                extractor._extract_company_details = lambda _url, _index: None
                with patch("core.extractors.maps_extractor.time.sleep"):
                    extractor._process_links([f"url-{index}" for index in range(result_count)])
                self.assertEqual(extractor.page_recycle_count, expected_recycles)

        recycled_pages = []
        extractor = MapsExtractor(
            FakePage(context), max_results=1, page_recycler=lambda: recycled_pages.append(1) or FakePage(context)
        )
        extractor.processing_limit = 1
        extractor._extract_company_details = lambda _url, _index: None
        with patch("core.extractors.maps_extractor.time.sleep"):
            extractor._process_links(["url"])
        self.assertEqual(len(recycled_pages), 1)
        self.assertEqual(extractor.page_recycle_count, 1)

        pressured = MapsExtractor(FakePage(context), max_results=1, memory_guard=lambda: True)
        pressured.processing_limit = 1
        with self.assertRaises(MemoryPressureError):
            pressured._process_links(["url"])

    def test_dashboard_distinguishes_active_and_allowed_workers(self) -> None:
        html = dashboard_page({
            "status": {
                "qualified_companies": 1,
                "target_qualified_companies": 200_000,
                "actual_browser_processes": 2,
                "scheduler_worker_limit": 3,
                "maximum_workers": 4,
                "ram_operating_state": "constrained",
            },
            "totals": (0, 0, 0, 0), "tiers": [], "states": [],
            "sectors": [], "recent": [], "jobs": [], "events": [],
        })
        self.assertIn("2 active / 3 allowed / 4 max", html)

    def test_v4_scoring_organization_and_suppression(self) -> None:
        reason = persist_observation(self.conn, self.task, {
            "name": "Klinik Pergigian Sales Ready", "category": "Dental clinic",
            "address": "Bangsar, Kuala Lumpur, Malaysia", "phone": "+60388889999",
            "website": "N/A", "place_id": "sales-ready-one",
            "source_url": "https://www.google.com/maps/place/sales-ready",
            "rating": "4.8", "reviews_count": "150 reviews",
        })
        self.assertEqual(reason, "new")
        backfill_v4(self.conn)
        lead = self.conn.execute(
            "SELECT organization_id,primary_offer,sales_readiness_score,sales_rank FROM lead_intelligence"
        ).fetchone()
        self.assertEqual(lead[1], "WEBSITE_BUILD")
        self.assertIn(lead[3], {"A", "B"})
        update_sales_lead(self.conn, {
            "organization_id": lead[0], "status": "DO_NOT_CONTACT",
            "assigned_pic": "Test PIC", "lost_reason": "Business opt-out",
        })
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contact_suppression").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(call_list_query()).fetchall(), [])

    def test_shared_social_domain_does_not_merge_unrelated_businesses(self) -> None:
        row = (1, "Example", "+60120000000", "facebook.com")
        key, method, _ = organization_key(row)
        self.assertEqual(key, "phone:+60120000000")
        self.assertEqual(method, "business_phone")

    def test_bulk_scoring_backfill(self) -> None:
        persist_observation(self.conn, self.task, {
            "name": "Bulk Dental Clinic", "category": "Dental clinic",
            "address": "Bangsar, Kuala Lumpur, Malaysia", "phone": "0312349999",
            "website": "N/A", "place_id": "bulk-one", "rating": "4.7",
            "reviews_count": "45 reviews", "source_url": "https://maps.example/bulk",
        })
        self.conn.execute("DELETE FROM sales_leads")
        self.conn.execute("DELETE FROM lead_intelligence")
        self.assertEqual(bulk_score_missing(self.conn), 1)
        score = self.conn.execute(
            "SELECT primary_offer,sales_rank FROM lead_intelligence WHERE intelligence_version=4"
        ).fetchone()
        self.assertEqual(score[0], "WEBSITE_BUILD")
        self.assertIn(score[1], {"A", "B"})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sales_leads").fetchone()[0], 1)

    def test_rolling_metrics_uses_partial_window(self) -> None:
        self.conn.execute(
            """INSERT INTO search_jobs(taxonomy_version,prompt,sector,locality,state,term,status,
               completed_at,qualified_new) VALUES(3,'test','Education','KL','FT','school','completed',datetime('now','-30 minutes'),10)"""
        )
        self.conn.commit()
        metrics = rolling_metrics(self.conn, 24)
        self.assertEqual(metrics["queries_completed"], 1)
        self.assertEqual(metrics["qualified_added"], 10)
        self.assertGreaterEqual(metrics["window_hours"], 0.49)
        self.assertLess(metrics["window_hours"], 1.0)


if __name__ == "__main__":
    unittest.main()
