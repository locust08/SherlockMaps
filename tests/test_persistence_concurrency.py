"""Exercise transaction failure and competing writers against actual SQLite."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from batch_collect_malaysia_v2 import QueryTask, open_db, persist_observation, worker_connection, record_checkpoints
from core.extractors.maps_extractor import MapsExtractor
from core.models import CompanyData


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "leads.sqlite"
        self.conn = open_db(self.path)
        self.task = QueryTask("dentist in Bangsar, Malaysia", "Health, Fitness & Wellness",
                              "Bangsar", "Federal Territory", "dentist", "district")
        self.raw = dict(name="Bangsar Dental Clinic", address="Bangsar, Kuala Lumpur, Malaysia",
                        category="Dental clinic", phone="0312345678", website="N/A",
                        place_id="fixture-place", source_url="https://maps.example/fixture")

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def test_failed_score_rolls_back_entire_observation_and_retry_succeeds(self):
        with patch("batch_collect_malaysia_v2.score_company", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                persist_observation(self.conn, self.task, self.raw)
        self.assertFalse(self.conn.in_transaction)
        for table in ("companies", "provenance", "raw_observations", "company_industry_classification"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(persist_observation(self.conn, self.task, self.raw), "new")

    def test_competing_workers_commit_one_location_and_observation(self):
        def save(_):
            connection = worker_connection(self.path)
            try:
                return persist_observation(connection, self.task, self.raw)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(8)))
        self.assertEqual(results.count("new"), 1)
        self.assertEqual(results.count("duplicate_observation"), 7)
        for table in ("companies", "provenance", "raw_observations"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)

    def test_worker_never_creates_empty_database(self):
        missing = self.path.parent / "missing.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            worker_connection(missing)
        self.assertFalse(missing.exists())

    def test_checkpoints_release_writer_before_controller_waits(self):
        # Even INSERT OR IGNORE of an existing checkpoint acquires a write
        # transaction. A controller must not retain it while awaiting workers.
        for _ in range(2):
            record_checkpoints(self.conn, 211000, 400000)
            self.assertFalse(self.conn.in_transaction)
            other = worker_connection(self.path)
            other.execute("PRAGMA busy_timeout=100")
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
                self.assertEqual(other.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 6)
            finally:
                other.close()

    def test_enrichment_does_not_hold_writer_during_http_audit(self):
        import website_enrichment
        raw = dict(self.raw, website="https://fixture.example")
        persist_observation(self.conn, self.task, raw)

        def audit_without_network(company_id, website):
            other = worker_connection(self.path)
            other.execute("PRAGMA busy_timeout=100")
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            finally:
                other.close()
            return website_enrichment.AuditResult(company_id=company_id)

        with patch.object(website_enrichment, "DB_PATH", self.path), patch.object(
            website_enrichment, "audit", side_effect=audit_without_network
        ), patch.object(website_enrichment, "save_result"):
            self.assertEqual(website_enrichment.run(limit=1), 1)

    def test_representative_lookup_uses_organization_index_without_changing_result(self):
        persist_observation(self.conn, self.task, self.raw)
        org = self.conn.execute("SELECT organization_id FROM lead_intelligence LIMIT 1").fetchone()[0]
        query = ("SELECT company_id FROM lead_intelligence WHERE organization_id=? "
                 "AND intelligence_version=? ORDER BY sales_readiness_score DESC,company_id LIMIT 1")
        expected = self.conn.execute(query.replace("FROM lead_intelligence", "FROM lead_intelligence NOT INDEXED"), (org, 4)).fetchall()
        actual = self.conn.execute(query, (org, 4)).fetchall()
        self.assertEqual(actual, expected)
        plan = self.conn.execute("EXPLAIN QUERY PLAN " + query, (org, 4)).fetchall()
        self.assertIn("ix_lead_intelligence_org_score", " ".join(str(row[3]) for row in plan))

    def test_callback_failure_aborts_remaining_links(self):
        def fail(_):
            raise sqlite3.OperationalError("database is locked")
        extractor = MapsExtractor(None, max_results=2, result_callback=fail)
        with patch.object(extractor, "_recycle_page"), patch.object(
            extractor, "_extract_company_details", return_value=CompanyData(**self.raw)
        ) as extract:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                extractor._process_links(["first", "second"], track_reviews=False)
        self.assertEqual(extract.call_count, 1)


if __name__ == "__main__":
    unittest.main()
