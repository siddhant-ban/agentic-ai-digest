"""Async RSS ingestion, DuckDuckGo search, and SSRF-safe full-article extraction."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "_is_safe_public_url": ("agentic_emailer.ingestion.extractor", "_is_safe_public_url"),
    "extract_article_text": ("agentic_emailer.ingestion.extractor", "extract_article_text"),
    "fetch_rss_feeds": ("agentic_emailer.ingestion.rss", "fetch_rss_feeds"),
    "is_valid_http_url": ("agentic_emailer.ingestion.rss", "is_valid_http_url"),
    "make_article_id": ("agentic_emailer.ingestion.rss", "make_article_id"),
    "normalize_url": ("agentic_emailer.ingestion.rss", "normalize_url"),
    "search_news": ("agentic_emailer.ingestion.search", "search_news"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        module_name, attr_name = _EXPORTS[name]
        return getattr(import_module(module_name), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
