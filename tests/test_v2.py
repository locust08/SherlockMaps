from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from batch_collect_malaysia_v2 import (
    QueryTask,
    PENINSULAR_EXPANSION_GDP_2025,
    PENINSULAR_PILOT_LOCALITIES,
    CLASSIFICATION_ONLY_INDUSTRIES,
    SECTOR_TERMS,
    build_manifest,
    desired_worker_count,
    recent_error_rate,
    is_storage_lock_error,
    open_db,
    persist_observation,
    qualified_count,
    ram_operating_state,
    register_manifest,
    rolling_metrics,
    worker_upscale_stable_seconds,
    submission_allowed,
    TARGET,
    observed_ab_yields,
    observed_ab_hourly_rates,
    rank_market_tasks,
    rank_peninsular_expansion,
    weighted_market_order,
    with_expected_speed,
    yield_estimate_cache,
    expected_ab_yield,
)
from core.browser.browser_manager import BrowserManager
from core.exceptions import MemoryPressureError
from core.extractors.maps_extractor import MapsExtractor
from core.models import CrawlerConfig
from lead_intelligence_v4 import backfill_v4, bulk_score_missing, organization_key, update_sales_lead
from progress_dashboard import NAV_ITEMS, call_list_query, dashboard_page, navigation


class V3CollectorTests(unittest.TestCase):
    def test_recent_sales_ready_query_speed_and_exploration(self) -> None:
        tasks = [QueryTask(f"dentist speed {i}", self.task.sector, self.task.locality,
                           self.task.state, self.task.term, "district") for i in range(3)]
        register_manifest(self.conn, tasks)
        self.conn.execute(
            """UPDATE search_jobs SET status='completed',qualified_new=20,ab_leads_new=20,
               started_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-10 minutes'),
               completed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"""
        )
        self.conn.commit()
        rates = observed_ab_hourly_rates(self.conn)
        self.assertAlmostEqual(rates[(self.task.sector, self.task.term, self.task.state, "district")], 120, delta=1)
        candidates = [QueryTask(f"speed{i}", self.task.sector, "Bangsar", self.task.state,
                                self.task.term, "district", priority=20,
                                expected_ab_per_hour=100-i*10) for i in range(5)]
        explorer = QueryTask("explore", self.task.sector, "Bangsar", self.task.state,
                             "new term", "district", priority=1, expected_ab_per_hour=0)
        ranked = rank_market_tasks(candidates + [explorer])
        self.assertEqual([task.prompt for task in ranked[:5]],
                         ["speed0", "speed1", "speed2", "speed3", "explore"])
        self.assertEqual(with_expected_speed([self.task], rates)[0].expected_ab_per_hour, 120)

    def test_observed_sales_yield_preserves_zero_and_bounds_overlap(self) -> None:
        tasks = [QueryTask(f"dentist fixture {i}", self.task.sector, self.task.locality,
                           self.task.state, self.task.term, "district") for i in range(3)]
        register_manifest(self.conn, tasks)
        self.conn.execute("UPDATE search_jobs SET status='completed',qualified_new=0,ab_leads_new=5")
        self.conn.commit()
        key = (self.task.sector, self.task.term)
        self.assertEqual(observed_ab_yields(self.conn)[key], 0)
        self.assertEqual(yield_estimate_cache(self.conn)[key], 0)
        self.assertEqual(expected_ab_yield(self.conn, *key), 0)
        self.conn.execute("UPDATE search_jobs SET qualified_new=2,ab_leads_new=5")
        self.conn.commit()
        self.assertEqual(observed_ab_yields(self.conn)[key], 2)
        self.conn.execute("UPDATE search_jobs SET status='pending' WHERE prompt=?", (tasks[0].prompt,))
        self.conn.commit()
        self.assertNotIn(key, observed_ab_yields(self.conn))

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
        self.assertEqual({task.state for task in manifest}, {
            "Selangor", "Federal Territory", "Johor", "Penang",
            "Perak", "Pahang", "Kedah", "Negeri Sembilan", "Melaka",
        })
        self.assertEqual([state for state, _ in PENINSULAR_EXPANSION_GDP_2025],
                         ["Perak", "Pahang", "Kedah", "Negeri Sembilan", "Melaka"])
        self.assertTrue(all(task.state in PENINSULAR_PILOT_LOCALITIES
                            for task in manifest if task.state not in {
                                "Selangor", "Federal Territory", "Johor", "Penang",
                            }))

    def test_market_cycle_protects_core_and_interleaves_gdp_pilot(self) -> None:
        states = ["Selangor", "Johor", "Penang", "Perak", "Pahang", "Kedah",
                  "Negeri Sembilan", "Melaka"]
        tasks = [QueryTask(f"{state}-{index}", "Finance", "City", state, "accounting firm")
                 for state in states for index in range(30)]
        ranked = weighted_market_order(tasks)
        first = ranked[:23]
        self.assertEqual(sum(task.state == "Selangor" for task in first), 11)
        self.assertEqual(sum(task.state == "Johor" for task in first), 5)
        self.assertEqual(sum(task.state == "Penang" for task in first), 4)
        self.assertEqual(sum(task.state not in {"Selangor", "Johor", "Penang"}
                             for task in first), 3)
        expansion = rank_peninsular_expansion([
            task for task in tasks if task.state in PENINSULAR_PILOT_LOCALITIES
        ])
        self.assertEqual([task.state for task in expansion[:5]], ["Perak"] * 5)
        self.assertEqual([task.state for task in expansion[5:9]], ["Pahang"] * 4)

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
        for current_limit in (1, 2, 3):
            for free_ram in (0.5, 1.07, 1.49):
                self.assertEqual(desired_worker_count(free_ram, current_limit=current_limit), current_limit)
            self.assertEqual(desired_worker_count(1.5, current_limit=current_limit), 4)
        self.assertEqual(desired_worker_count(1.5, current_limit=4), 5)
        self.assertEqual(desired_worker_count(1.49, current_limit=4), 4)
        self.assertEqual(desired_worker_count(1.5, current_limit=5), 5)
        self.assertEqual(desired_worker_count(
            1.5, current_limit=5, five_worker_canary_complete=True
        ), 6)
        self.assertEqual(desired_worker_count(0.49, current_limit=6), 5)
        self.assertEqual(desired_worker_count(0.49, current_limit=4), 3)
        self.assertEqual(desired_worker_count(0.5, current_limit=6), 6)
        self.assertEqual(desired_worker_count(4.0, cooldown=True), 1)
        self.assertEqual(desired_worker_count(4.0, error_rate=0.05), 1)
        self.assertEqual(ram_operating_state(1.5), "healthy")
        self.assertEqual(ram_operating_state(1.0), "constrained")
        self.assertEqual(ram_operating_state(0.49), "critical")
        self.assertEqual(worker_upscale_stable_seconds(4), 10)
        self.assertEqual(worker_upscale_stable_seconds(5), 60)
        self.assertEqual(worker_upscale_stable_seconds(6), 300)

    def test_cooldown_blocks_new_queries_even_with_free_memory(self) -> None:
        self.assertFalse(submission_allowed(8.0, 100.0, 1000.0))
        self.assertFalse(submission_allowed(8.0, 999.99, 1000.0))
        self.assertTrue(submission_allowed(8.0, 1000.0, 1000.0))
        self.assertFalse(submission_allowed(0.49, 1001.0, 1000.0))
        self.assertTrue(submission_allowed(0.5, 1001.0, 1000.0))

    def test_sqlite_lock_recovery_keeps_progress_without_disabling_google_controls(self) -> None:
        self.assertTrue(is_storage_lock_error(
            "ExtractionError: Failed (Caused by: database is locked)"))
        self.assertFalse(is_storage_lock_error("GOOGLE_BLOCK: unusual traffic"))
        self.assertEqual(desired_worker_count(3.29, current_limit=6,
                                             five_worker_canary_complete=True,
                                             storage_lock_recovery=True), 3)
        self.assertEqual(desired_worker_count(3.29, current_limit=1,
                                             storage_lock_recovery=True), 3)
        self.assertEqual(desired_worker_count(3.29, current_limit=3,
                                             cooldown=True,
                                             storage_lock_recovery=True), 1)
        self.assertEqual(desired_worker_count(3.29, current_limit=3,
                                             error_rate=0.05,
                                             storage_lock_recovery=True), 1)

    def test_browser_errors_age_out_instead_of_latching_at_one_browser(self) -> None:
        from collections import deque
        outcomes = deque([(100.0, 1), (105.0, 0)], maxlen=20)
        self.assertEqual(recent_error_rate(outcomes, 106.0), 0.5)
        self.assertEqual(recent_error_rate(outcomes, 706.0), 0.0)
        self.assertEqual(len(outcomes), 0)

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
                extractor._extract_company_details = lambda _url, _index, track_reviews=True: None
                with patch("core.extractors.maps_extractor.time.sleep"):
                    extractor._process_links([f"url-{index}" for index in range(result_count)])
                self.assertEqual(extractor.page_recycle_count, expected_recycles)

        recycled_pages = []
        extractor = MapsExtractor(
            FakePage(context), max_results=1, page_recycler=lambda: recycled_pages.append(1) or FakePage(context)
        )
        extractor.processing_limit = 1
        extractor._extract_company_details = lambda _url, _index, track_reviews=True: None
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

    def test_dashboard_navigation_lists_every_page_and_marks_active_page(self) -> None:
        html = navigation("pipeline")
        for key, url, label in NAV_ITEMS:
            self.assertIn(f"href='{url}'", html)
            self.assertIn(label.replace("&", "&amp;"), html)
            expected_class = "active" if key == "pipeline" else ""
            self.assertIn(f"href='{url}' class='{expected_class}'", html)
        self.assertIn("aria-label='Dashboard pages'", html)

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
