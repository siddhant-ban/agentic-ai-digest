"""Fetch and normalize articles from RSS feeds and web search."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import feedparser
from dateutil import parser as date_parser
from duckduckgo_search import DDGS

logger = logging.getLogger(__name__)

MAX_ARTICLES_FOR_GEMINI = 40
SEARCH_DELAY_SECONDS = 1.0


@dataclass
class Article:
    title: str
    url: str
    source: str
    published_at: datetime | None
    snippet: str

    def to_prompt_block(self) -> str:
        date_str = (
            self.published_at.strftime("%Y-%m-%d")
            if self.published_at
            else "unknown date"
        )
        return (
            f"- Title: {self.title}\n"
            f"  Source: {self.source}\n"
            f"  Date: {date_str}\n"
            f"  URL: {self.url}\n"
            f"  Snippet: {self.snippet or '(no snippet)'}"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_published(entry: dict[str, Any]) -> datetime | None:
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if raw:
            try:
                return _ensure_utc(date_parser.parse(raw))
            except (ValueError, TypeError, OverflowError):
                continue

    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = parse_qs(parsed.query, keep_blank_values=False)
    for tracking_key in (
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "ref",
        "source",
    ):
        query.pop(tracking_key, None)
    clean_query = "&".join(
        f"{key}={value[0]}" for key, value in sorted(query.items()) if value
    )
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (parsed.scheme, parsed.netloc.lower(), path, parsed.params, clean_query, "")
    )


def _normalize_title(title: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", title.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _topic_keywords(topics: list[str]) -> set[str]:
    keywords: set[str] = set()
    for topic in topics:
        for word in re.findall(r"[a-z0-9]+", topic.lower()):
            if len(word) >= 3:
                keywords.add(word)
    return keywords


def _matches_topics(article: Article, topics: list[str]) -> bool:
    if not topics:
        return True

    haystack = " ".join(
        part.lower() for part in (article.title, article.snippet, article.source)
    )
    keywords = _topic_keywords(topics)
    if any(keyword in haystack for keyword in keywords):
        return True

    for topic in topics:
        topic_lower = topic.lower()
        if topic_lower in haystack:
            return True
        topic_words = [w for w in re.findall(r"[a-z0-9]+", topic_lower) if len(w) >= 3]
        if topic_words and sum(1 for w in topic_words if w in haystack) >= max(
            1, len(topic_words) // 2
        ):
            return True
    return False


def _within_lookback(published_at: datetime | None, lookback_hours: int) -> bool:
    if published_at is None:
        return True
    cutoff = _utc_now() - timedelta(hours=lookback_hours)
    return published_at >= cutoff


def _dedupe_articles(articles: list[Article]) -> list[Article]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[Article] = []

    for article in sorted(
        articles,
        key=lambda a: a.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    ):
        url_key = _normalize_url(article.url)
        title_key = _normalize_title(article.title)
        if url_key in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        unique.append(article)
    return unique


def _fetch_rss_feed(
    feed_name: str,
    feed_url: str,
    lookback_hours: int,
    max_articles: int,
    topics: list[str],
) -> list[Article]:
    articles: list[Article] = []
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:
        logger.warning("Failed to fetch RSS feed %s: %s", feed_name, exc)
        return articles

    if getattr(parsed, "bozo", False) and not parsed.entries:
        logger.warning("RSS feed %s returned no entries (%s)", feed_name, parsed.bozo_exception)

    for entry in parsed.entries[: max_articles * 3]:
        url = entry.get("link") or entry.get("id")
        title = entry.get("title")
        if not url or not title:
            continue

        published_at = _parse_published(entry)
        if not _within_lookback(published_at, lookback_hours):
            continue

        snippet = entry.get("summary") or entry.get("description") or ""
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()[:500]

        article = Article(
            title=title.strip(),
            url=url.strip(),
            source=feed_name,
            published_at=published_at,
            snippet=snippet,
        )
        if _matches_topics(article, topics):
            articles.append(article)
        if len(articles) >= max_articles:
            break
    return articles


def _search_queries_for_topic(topic: str, count: int) -> list[str]:
    year = _utc_now().year
    templates = [
        f"{topic} AI news {year}",
        f"latest {topic} artificial intelligence",
        f"{topic} AI breakthrough {year}",
        f"new {topic} AI model release",
    ]
    return templates[: max(1, count)]


def _fetch_search_results(
    topics: list[str],
    lookback_hours: int,
    queries_per_topic: int,
    max_articles: int,
) -> list[Article]:
    articles: list[Article] = []
    try:
        ddgs = DDGS()
    except Exception as exc:
        logger.warning("Failed to initialize DuckDuckGo search: %s", exc)
        return articles

    for topic in topics:
        for query in _search_queries_for_topic(topic, queries_per_topic):
            try:
                results = list(
                    ddgs.news(
                        query,
                        max_results=max_articles,
                        timelimit="w" if lookback_hours <= 168 else "m",
                    )
                )
            except Exception:
                try:
                    results = list(ddgs.text(query, max_results=max_articles))
                except Exception as exc:
                    logger.warning("Search failed for query %r: %s", query, exc)
                    results = []

            for result in results:
                url = result.get("url") or result.get("href") or result.get("link")
                title = result.get("title")
                if not url or not title:
                    continue

                published_at = None
                raw_date = result.get("date") or result.get("published")
                if raw_date:
                    try:
                        published_at = _ensure_utc(date_parser.parse(str(raw_date)))
                    except (ValueError, TypeError, OverflowError):
                        published_at = None

                if not _within_lookback(published_at, lookback_hours):
                    continue

                snippet = (
                    result.get("body")
                    or result.get("excerpt")
                    or result.get("snippet")
                    or ""
                )
                snippet = re.sub(r"\s+", " ", snippet).strip()[:500]

                articles.append(
                    Article(
                        title=title.strip(),
                        url=url.strip(),
                        source=f"DuckDuckGo ({topic})",
                        published_at=published_at,
                        snippet=snippet,
                    )
                )

            time.sleep(SEARCH_DELAY_SECONDS)

    return articles


def gather_all(config: dict[str, Any]) -> list[Article]:
    topics: list[str] = config.get("topics") or []
    lookback_hours = int(config.get("lookback_hours", 48))
    max_articles = int(config.get("max_articles_per_source", 15))
    queries_per_topic = int(config.get("search_queries_per_topic", 2))
    rss_feeds = config.get("rss_feeds") or []

    all_articles: list[Article] = []

    for feed in rss_feeds:
        name = feed.get("name") or feed.get("url") or "RSS"
        url = feed.get("url")
        if not url:
            continue
        logger.info("Fetching RSS feed: %s", name)
        all_articles.extend(
            _fetch_rss_feed(name, url, lookback_hours, max_articles, topics)
        )

    logger.info("Running web search for %d topic(s)", len(topics))
    all_articles.extend(
        _fetch_search_results(
            topics, lookback_hours, queries_per_topic, max_articles
        )
    )

    deduped = _dedupe_articles(all_articles)
    return deduped[:MAX_ARTICLES_FOR_GEMINI]
