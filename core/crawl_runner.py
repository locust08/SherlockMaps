"""Synchronous crawl runner shared by the batch collector and HTTP API.

Keep this module free of API queue, SMTP-store and web-app initialization.
"""

from __future__ import annotations

import logging
from typing import Any

from core.browser import BrowserManager
from core.extractors import MapsExtractor
from core.models import CrawlerConfig
from core.processors import DeduplicationProcessor

logger = logging.getLogger(__name__)


def run_crawl_in_process(
    prompt: str,
    output_format: str = "json",
    headless: bool = False,
    locale: str = "de-DE",
    max_results: int | None = None,
    scroll_timeout: int = 45,
    max_scroll_attempts: int = 5,
    adaptive_results: int | None = None,
    hard_result_cap: int = 500,
    include_metrics: bool = False,
    result_callback: Any = None,
    memory_guard: Any = None,
    page_recycle_interval: int = 10,
    track_reviews: bool = True,
) -> Any:
    """Run a crawl in a worker process.

    This function performs the actual crawl operation using synchronous Playwright code
    in a separate process to avoid asyncio loop conflicts.

    Args:
        prompt: Search prompt for Google Maps.
        output_format: Output format.
        headless: Run in headless mode.
        locale: Browser locale.
        track_reviews: Whether to extract reviews for each company.

    Returns:
        List of company dictionaries.
    """
    # Every crawl receives an isolated, non-persistent browser context.
    config = CrawlerConfig(
        search_prompt=prompt,
        headless=headless,
        output_format=output_format,
        locale=locale,
        scroll_timeout=scroll_timeout,
        max_scroll_attempts=max_scroll_attempts,
        initial_results=max_results or 100,
        adaptive_results=adaptive_results or max_results or 100,
        hard_result_cap=hard_result_cap,
        track_reviews=track_reviews,
    )

    browser_mgr = BrowserManager(config)
    browser_mgr.initialize()

    try:
        # Navigate to Google Maps
        page = browser_mgr.navigate_to_maps(prompt)

        # Extract data
        extractor = MapsExtractor(
            page,
            config.selector_timeout,
            max_results=max_results,
            scroll_timeout=config.scroll_timeout,
            max_scroll_attempts=config.max_scroll_attempts,
            adaptive_results=config.adaptive_results,
            hard_result_cap=config.hard_result_cap,
            result_callback=result_callback,
            memory_guard=memory_guard,
            page_recycle_interval=page_recycle_interval,
            page_recycler=browser_mgr.recycle_context,
            memory_cleanup_callback=browser_mgr.collect_garbage,
            discovery_cleanup_interval=30,
        )
        raw_results = extractor.extract_all(track_reviews=track_reviews)

        if not raw_results:
            logger.warning("No companies found for prompt: %s", prompt)
            if include_metrics:
                return {
                    "results": [],
                    "links_discovered": extractor.links_discovered,
                    "processed_count": 0,
                    "end_of_results": extractor.end_of_results,
                    "page_recycle_count": extractor.page_recycle_count,
                    "memory_cleanup_count": extractor.memory_cleanup_count,
                }
            return []

        logger.info("Extracted %d raw companies", len(raw_results))

        # Remove duplicates
        deduplicator = DeduplicationProcessor()
        unique_results = deduplicator.process(raw_results)

        result_dicts = [company.to_dict() for company in unique_results]
        logger.info("Crawl complete. Found %d unique companies", len(result_dicts))
        if include_metrics:
            return {
                "results": result_dicts,
                "links_discovered": extractor.links_discovered,
                "processed_count": len(raw_results),
                "end_of_results": extractor.end_of_results,
                "page_recycle_count": extractor.page_recycle_count,
                "memory_cleanup_count": extractor.memory_cleanup_count,
            }
        return result_dicts

    finally:
        browser_mgr.close()

