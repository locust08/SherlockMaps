"""Deduplication processor for the GoogleMapsCrawler."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import CompanyData

logger = logging.getLogger(__name__)


class DeduplicationProcessor:
    """Removes duplicate physical listings while preserving separate branches.

    Prefer a Maps place ID. Without one, require name, physical address and
    website to agree; a shared brand website alone is not a location identity.

    Usage:
        processor = DeduplicationProcessor()
        unique_companies = processor.process(companies)
    """

    def process(self, companies: list["CompanyData"]) -> list["CompanyData"]:
        """Remove duplicate companies from the list.

        Prefer explicit location IDs; preserve ambiguous address-less records.

        Args:
            companies: A list of CompanyData objects.

        Returns:
            A list of unique CompanyData objects, preserving order.
        """
        if not companies:
            return []

        unique_companies: list[CompanyData] = []
        seen: set[tuple[str, ...]] = set()

        for company in companies:
            place_id = (company.place_id or "").strip()
            if place_id and place_id.casefold() != "n/a":
                key = ("place", place_id)
            else:
                name = " ".join((company.name or "").casefold().split())
                address = " ".join((company.address or "").casefold().split())
                if not name or name == "n/a" or not address or address == "n/a":
                    unique_companies.append(company)
                    continue
                key = ("address", name, address, (company.website or "").strip().lower())

            if key not in seen:
                seen.add(key)
                unique_companies.append(company)

        removed = len(companies) - len(unique_companies)
        if removed > 0:
            logger.info(
                "Removed %d duplicate companies. Remaining: %d",
                removed,
                len(unique_companies),
            )

        return unique_companies
