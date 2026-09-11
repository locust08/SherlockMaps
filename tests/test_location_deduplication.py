import unittest
from core.models import CompanyData
from core.processors import DeduplicationProcessor


class LocationDedupTests(unittest.TestCase):
    def test_same_brand_site_different_branches_are_kept(self):
        branches = [CompanyData(name="Clinic", website="https://brand.test", address=address)
                    for address in ("1 Jalan A, KL", "2 Jalan B, Johor")]
        self.assertEqual(DeduplicationProcessor().process(branches), branches)

    def test_same_place_id_is_deduplicated_despite_name_change(self):
        first = CompanyData(name="Clinic", place_id="ChIJCaseSensitive")
        later = CompanyData(name="Clinic renamed", place_id=first.place_id)
        other = CompanyData(name="Clinic", place_id="ChIJOtherBranch")
        self.assertEqual(DeduplicationProcessor().process([first, later, other]), [first, other])

    def test_address_fallback_preserves_order_and_removes_exact_location(self):
        first = CompanyData(name="Clinic", address="1 Jalan A", website="https://brand.test")
        repeat = CompanyData(name=" CLINIC ", address="1  JALAN A", website=first.website)
        self.assertEqual(DeduplicationProcessor().process([first, repeat]), [first])

    def test_missing_location_is_not_evidence_of_a_duplicate(self):
        rows = [CompanyData(name="Clinic", website="https://brand.test") for _ in range(2)]
        self.assertEqual(DeduplicationProcessor().process(rows), rows)
