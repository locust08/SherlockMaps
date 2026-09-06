import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.models import CompanyData
from core.output.output_handler import OutputHandler


class OutputTests(unittest.TestCase):
    def test_csv_retains_identity_and_contact_fields(self):
        company = CompanyData(name='Clinic, "Central"', website="https://example.com",
                              place_id="location-id", source_url="https://maps.example/location",
                              emails=["office@example.com"], attributes=["Accessible"])
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(OutputHandler, "OUTPUT_DIR", Path(directory)):
                OutputHandler()._output_csv([company])
            with next(Path(directory).glob("*.csv")).open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), set(company.to_dict()))
        self.assertEqual(rows[0]["name"], company.name)
        self.assertEqual(rows[0]["place_id"], company.place_id)
        self.assertEqual(json.loads(rows[0]["emails"]), company.emails)

    def test_model_url_validation_from_repository_root(self):
        self.assertTrue(CompanyData(website="https://example.com").has_valid_website())
        self.assertFalse(CompanyData(website="N/A").has_valid_website())
