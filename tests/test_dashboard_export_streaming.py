"""Exporter parity and resource tests; no live database or HTTP calls."""
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import progress_dashboard as dashboard


class ExportStreamingTests(unittest.TestCase):
    rows = [("雪兰莪 & <Clinic>", "0012345", None, "=literal"),
            ("Second location", "https://example.test/?a=1&b=2", "", "line\nbreak")]

    def connection(self):
        conn = MagicMock()
        conn.execute.return_value = iter(self.rows)
        return conn

    def test_all_zip_members_match_previous_export_exactly(self):
        # SHA-256 of uncompressed XML from the exporter at 0c98d13 with these
        # exact fixture rows. Works in source archives and shallow clones too.
        expected = {
            "[Content_Types].xml": "c6c0a8a09eabe192f832febd62473a71a230df09117085c947bcb36ce6aa1d11",
            "_rels/.rels": "9f96bb683e71c45171c7ae310bf6c37fb5a834bb9738497ac7084c42ac9c2d9a",
            "xl/workbook.xml": "67bc9ecab8914fc813f8fe5fc12dc3aeba15a758b9997bfbf4ca43c9751ede8c",
            "xl/_rels/workbook.xml.rels": "1383f7ed84526efdb36373e9ee3ea5d69e68664c893d998812243af05dff88f5",
            "xl/worksheets/sheet1.xml": "45a63258cb0167bec03d69e5202c6360bce0a7e2a1bbbbea836a22d36b658dd8",
        }
        with tempfile.TemporaryDirectory() as folder, patch.object(dashboard, "connection", side_effect=self.connection):
            path = Path(folder) / "export.xlsx"
            self.assertIsNone(dashboard.company_export_xlsx(path))
            with zipfile.ZipFile(path) as new:
                self.assertEqual(set(expected), set(new.namelist()))
                for name, digest in expected.items():
                    self.assertEqual(hashlib.sha256(new.read(name)).hexdigest(), digest, name)

    def test_query_failure_closes_database(self):
        conn = self.connection()
        conn.execute.side_effect = RuntimeError("fixture query failure")
        with patch.object(dashboard, "connection", return_value=conn):
            with self.assertRaisesRegex(RuntimeError, "fixture query failure"):
                dashboard.company_export_xlsx()
        conn.close.assert_called_once()

    def test_concurrent_requests_have_isolated_files_and_cleanup(self):
        barrier = threading.Barrier(2)
        paths = []
        def request(_):
            handler = dashboard.Handler.__new__(dashboard.Handler)
            handler.path = "/export/companies.xlsx"
            handler.send_error = MagicMock()
            def download(path, *args):
                paths.append(path)
                barrier.wait(timeout=10)
                self.assertTrue(zipfile.is_zipfile(path))
                raise ConnectionAbortedError("fixture disconnect")
            handler.download_file = download
            handler.do_GET()
            handler.send_error.assert_not_called()
        with patch.object(dashboard, "connection", side_effect=self.connection):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(request, range(2)))
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(not path.parent.exists() for path in paths))
