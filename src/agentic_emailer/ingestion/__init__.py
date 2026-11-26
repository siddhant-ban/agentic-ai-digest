"""Async RSS ingestion, DuckDuckGo search, and SSRF-safe full-article extraction."""

from agentic_emailer.ingestion.extractor import _is_safe_public_url, extract_article_text
from agentic_emailer.ingestion.rss import fetch_rss_feeds, make_article_id, normalize_url
from agentic_emailer.ingestion.search import search_news

__all__ = [
    "_is_safe_public_url",
    "extract_article_text",
    "fetch_rss_feeds",
    "make_article_id",
    "normalize_url",
    "search_news",
]
