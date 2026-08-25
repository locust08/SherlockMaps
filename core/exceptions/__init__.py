"""Custom exceptions for the GoogleMapsCrawler."""

from .crawler_exceptions import (
    CrawlerBaseException,
    BrowserInitializationError,
    ExtractionError,
    MemoryPressureError,
    NavigationError,
)

__all__ = [
    "CrawlerBaseException",
    "BrowserInitializationError",
    "ExtractionError",
    "MemoryPressureError",
    "NavigationError",
]
