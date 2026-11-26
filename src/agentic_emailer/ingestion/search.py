"""DuckDuckGo news/web search wrapper with timeouts and polite rate limiting."""

from __future__ import annotations

import html
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any

from duckduckgo_search import DDGS
from pydantic import ValidationError

from agentic_emailer.ingestion.rss import (
    clean_snippet,
    is_valid_http_url,
    make_article_id,
    normalize_url,
    parse_published_datetime,
    within_lookback,
)
from agentic_emailer.models import RawArticle

logger = logging.getLogger(__name__)

SEARCH_MIN_INTERVAL_SECONDS = 0.75
_rate_lock = threading.Lock()
_last_search_ts = 0.0


def _wait_for_rate_limit() -> None:
    global _last_search_ts
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_search_ts
        if elapsed < SEARCH_MIN_INTERVAL_SECONDS:
            time.sleep(SEARCH_MIN_INTERVAL_SECONDS - elapsed)
        _last_search_ts = time.monotonic()


def _timelimit_for_hours(lookback_hours: int) -> str:
    if lookback_hours <= 24:
        return "d"
    if lookback_hours <= 168:
        return "w"
    return "m"


def _parse_search_results(
    raw_results: list[Any],
    lookback_hours: int,
    *,
    topic: str | None = None,
) -> list[RawArticle]:
    cleaned_topic = topic.strip() if topic else ""
    deduped: dict[str, RawArticle] = {}
    for item in raw_results:
        if not isinstance(item, Mapping):
            continue
        raw_url = str(item.get("url") or item.get("href") or item.get("link") or "").strip()
        title = " ".join(html.unescape(str(item.get("title") or "")).split())
        if not raw_url or not title or not is_valid_http_url(raw_url):
            continue

        published_at = parse_published_datetime(item.get("date") or item.get("published"))
        if not within_lookback(published_at, lookback_hours):
            continue

        raw_snippet = str(
            item.get("body") or item.get("excerpt") or item.get("snippet") or ""
        )
        snippet = clean_snippet(raw_snippet)
        canonical_url = normalize_url(raw_url)
        article_id = make_article_id(canonical_url)

        source_name = " ".join(html.unescape(str(item.get("source") or "")).split())
        if not source_name:
            source_name = f"Web Search ({cleaned_topic})" if cleaned_topic else "Web Search"

        try:
            article = RawArticle(
                article_id=article_id,
                canonical_url=canonical_url,
                url=raw_url,
                title=title,
                source=source_name,
                published_at=published_at,
                snippet=snippet,
                discovered_via="search",
                topic_hints=[cleaned_topic] if cleaned_topic else [],
            )
        except (ValidationError, ValueError) as exc:
            logger.warning("Skipping invalid search result (%r): %s", raw_url, exc)
            continue

        deduped.setdefault(article_id, article)

    return list(deduped.values())


def search_news(
    query: str,
    lookback_hours: int = 48,
    max_results: int = 8,
    *,
    topic: str | None = None,
    timeout: int = 10,
) -> list[RawArticle]:
    """Search DuckDuckGo News (with web text fallback) and return normalized RawArticles."""
    cleaned_query = query.strip()
    if not cleaned_query:
        return []

    _wait_for_rate_limit()
    timelimit = _timelimit_for_hours(lookback_hours)

    raw_results: list[dict[str, Any]] = []
    try:
        ddgs = DDGS(timeout=timeout)
    except Exception as exc:
        logger.warning("Failed to initialize DDGS client: %s", exc)
        return []

    try:
        raw_results = list(
            ddgs.news(cleaned_query, max_results=max_results, timelimit=timelimit) or []
        )
    except Exception as news_exc:
        logger.warning("DDG news search failed for %r: %s", cleaned_query, news_exc)

    if not raw_results:
        _wait_for_rate_limit()
        try:
            raw_results = list(
                ddgs.text(cleaned_query, max_results=max_results, timelimit=timelimit) or []
            )
        except Exception as text_exc:
            logger.warning("DDG text fallback failed for %r: %s", cleaned_query, text_exc)
            return []

    return _parse_search_results(raw_results, lookback_hours, topic=topic)
