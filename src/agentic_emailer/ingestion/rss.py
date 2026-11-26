"""Async concurrent RSS ingestion, URL canonicalization, and deterministic article IDs."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx
from dateutil import parser as date_parser

from agentic_emailer.ingestion.extractor import fetch_url_safely
from agentic_emailer.models import RawArticle, RssFeedConfig

logger = logging.getLogger(__name__)

TRACKING_QUERY_KEYS = frozenset(
    {
        "ref",
        "source",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "igshid",
    }
)
MAX_RSS_BYTES = 5_242_880  # 5 MiB cap for RSS/Atom XML documents
MAX_SNIPPET_CHARS = 800
HTML_TAG_RE = re.compile(r"<[^>]+>")


def normalize_url(url: str) -> str:
    """Canonicalize a URL by stripping tracking parameters, fragment, and trailing slash."""
    cleaned = url.strip()
    try:
        parsed = urlparse(cleaned)
        port = parsed.port
    except ValueError:
        return cleaned

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    is_default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    if port is not None and not is_default_port:
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname or parsed.netloc.lower()

    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    filtered_query.sort(key=lambda item: (item[0].lower(), item[0], item[1]))
    clean_query = urlencode(filtered_query)

    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", clean_query, ""))


def make_article_id(canonical_url: str) -> str:
    """Generate a deterministic 16-character hex ID from a canonicalized URL."""
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]


def ensure_utc(dt: datetime) -> datetime:
    """Convert any datetime to timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_published_datetime(raw: Any) -> datetime | None:
    """Best-effort parse of a date string into a UTC datetime."""
    if not raw:
        return None
    try:
        return ensure_utc(date_parser.parse(str(raw)))
    except (ValueError, TypeError, OverflowError):
        return None


def _parse_entry_published(entry: dict[str, Any]) -> datetime | None:
    for key in ("published", "updated", "created"):
        parsed_dt = parse_published_datetime(entry.get(key))
        if parsed_dt is not None:
            return parsed_dt

    for key in ("published_parsed", "updated_parsed"):
        parsed_struct = entry.get(key)
        if parsed_struct and len(parsed_struct) >= 6:
            try:
                return datetime(
                    int(parsed_struct[0]),
                    int(parsed_struct[1]),
                    int(parsed_struct[2]),
                    int(parsed_struct[3]),
                    int(parsed_struct[4]),
                    int(parsed_struct[5]),
                    tzinfo=UTC,
                )
            except (TypeError, ValueError, IndexError):
                continue
    return None


def within_lookback(published_at: datetime | None, lookback_hours: int) -> bool:
    """Return True if an article's timestamp falls within the lookback window (or is unknown)."""
    if published_at is None:
        return True
    cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)
    return ensure_utc(published_at) >= cutoff


def clean_snippet(raw_snippet: str, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace, and truncate cleanly."""
    if not raw_snippet:
        return ""
    unescaped = html.unescape(raw_snippet)
    stripped = HTML_TAG_RE.sub(" ", unescaped)
    compact = re.sub(r"\s+", " ", stripped).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


async def _fetch_single_feed(
    client: httpx.AsyncClient,
    feed: RssFeedConfig,
    lookback_hours: int,
    max_per_feed: int,
    semaphore: asyncio.Semaphore,
) -> list[RawArticle]:
    async with semaphore:
        try:
            fetched = await fetch_url_safely(
                client,
                feed.url,
                max_bytes=MAX_RSS_BYTES,
                allowed_content_types=None,
            )
            if fetched is None:
                logger.warning(
                    "Skipping RSS feed %s (%s): fetch failed or blocked", feed.name, feed.url
                )
                return []

            _, raw_bytes, encoding = fetched
            try:
                xml_text = raw_bytes.decode(encoding, errors="replace")
            except LookupError:
                xml_text = raw_bytes.decode("utf-8", errors="replace")
            parsed = await asyncio.to_thread(feedparser.parse, xml_text)
        except Exception as exc:
            logger.warning("Failed to fetch/parse RSS feed %s (%s): %s", feed.name, feed.url, exc)
            return []

        if getattr(parsed, "bozo", False) and not parsed.entries:
            logger.warning(
                "RSS feed %s returned no entries (%s)",
                feed.name,
                getattr(parsed, "bozo_exception", ""),
            )
            return []

        articles: list[RawArticle] = []
        for entry in parsed.entries[: max_per_feed * 3]:
            raw_url = str(entry.get("link") or entry.get("id") or "").strip()
            title = str(entry.get("title") or "").strip()
            if not raw_url or not title or not raw_url.lower().startswith(("http://", "https://")):
                continue

            published_at = _parse_entry_published(entry)
            if not within_lookback(published_at, lookback_hours):
                continue

            raw_summary = str(entry.get("summary") or entry.get("description") or "")
            snippet = clean_snippet(raw_summary)

            canonical_url = normalize_url(raw_url)
            article_id = make_article_id(canonical_url)

            articles.append(
                RawArticle(
                    article_id=article_id,
                    canonical_url=canonical_url,
                    url=raw_url,
                    title=html.unescape(title),
                    source=feed.name,
                    published_at=published_at,
                    snippet=snippet,
                    discovered_via="rss",
                    topic_hints=[],
                )
            )
            if len(articles) >= max_per_feed:
                break

        logger.info("RSS feed %s yielded %d article(s)", feed.name, len(articles))
        return articles


async def fetch_rss_feeds(
    feeds: list[RssFeedConfig],
    lookback_hours: int = 36,
    max_per_feed: int = 15,
    concurrency: int = 8,
) -> list[RawArticle]:
    """Concurrently fetch RSS feeds, normalize URLs, and deduplicate by article_id."""
    if not feeds:
        return []

    semaphore = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        tasks = [
            _fetch_single_feed(client, feed, lookback_hours, max_per_feed, semaphore)
            for feed in feeds
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    deduped: dict[str, RawArticle] = {}
    for feed, result in zip(feeds, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("Unexpected RSS fetch failure for %s: %s", feed.name, result)
            continue
        for article in result:
            existing = deduped.get(article.article_id)
            if existing is None or len(article.snippet) > len(existing.snippet):
                deduped[article.article_id] = article

    epoch_min = datetime.min.replace(tzinfo=UTC)
    return sorted(
        deduped.values(),
        key=lambda a: ensure_utc(a.published_at) if a.published_at else epoch_min,
        reverse=True,
    )
