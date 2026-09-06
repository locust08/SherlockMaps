"""Exercise transaction failure and competing writers against actual SQLite."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from batch_collect_malaysia_v2 import QueryTask, open_db, persist_observation, worker_connection
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
