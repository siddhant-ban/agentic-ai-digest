"""Tests for sources.py module."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sources import (
    Article,
    _dedupe_articles,
    _ensure_utc,
    _fetch_rss_feed,
    _fetch_search_results,
    _matches_topics,
    _normalize_title,
    _normalize_url,
    _parse_published,
    _topic_keywords,
    _within_lookback,
    filter_articles_for_recipient,
    gather_all,
)



def test_article_to_prompt_block():
    dt = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    article = Article(
        title="Test Article",
        url="https://example.com/test",
        source="Test Source",
        published_at=dt,
        snippet="Test snippet text",
    )
    block = article.to_prompt_block()
    assert "- Title: Test Article" in block
    assert "  Source: Test Source" in block
    assert "  Date: 2026-08-27" in block
    assert "  URL: https://example.com/test" in block
    assert "  Snippet: Test snippet text" in block

    article_no_date = Article(
        title="No Date",
        url="https://example.com/nodate",
        source="Test Source",
        published_at=None,
        snippet="",
    )
    block_no_date = article_no_date.to_prompt_block()
    assert "  Date: unknown date" in block_no_date
    assert "  Snippet: (no snippet)" in block_no_date


def test_ensure_utc():
    naive = datetime(2026, 1, 1, 10, 0)
    utc = _ensure_utc(naive)
    assert utc.tzinfo == timezone.utc
    assert utc.hour == 10

    aware = datetime(2026, 1, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))
    converted = _ensure_utc(aware)
    assert converted.tzinfo == timezone.utc
    assert converted.hour == 8


def test_parse_published():
    entry_str = {"published": "Thu, 27 Aug 2026 12:00:00 GMT"}
    parsed = _parse_published(entry_str)
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.month == 8

    entry_tuple = {"published_parsed": (2026, 8, 27, 12, 0, 0, 3, 239, 0)}
    parsed_tuple = _parse_published(entry_tuple)
    assert parsed_tuple is not None
    assert parsed_tuple.day == 27

    assert _parse_published({}) is None
    assert _parse_published({"published": "invalid date string"}) is None


def test_normalize_url():
    raw_url = "https://EXAMPLE.com/path/to/article/?utm_source=twitter&utm_medium=social&ref=123"
    clean = _normalize_url(raw_url)
    assert clean == "https://example.com/path/to/article"

    raw_with_param = "http://example.com/news?id=456&utm_campaign=digest"
    clean_param = _normalize_url(raw_with_param)
    assert clean_param == "http://example.com/news?id=456"


def test_normalize_title():
    raw = "  Breakthrough! New AI Model: 100% Accuracy?  "
    clean = _normalize_title(raw)
    assert clean == "breakthrough new ai model 100 accuracy"


def test_topic_keywords():
    topics = ["AI Research", "LLM Benchmark v2"]
    keywords = _topic_keywords(topics)
    assert "research" in keywords
    assert "benchmark" in keywords
    # Words < 3 chars excluded
    assert "ai" not in keywords


def test_matches_topics():
    article = Article(
        title="New LLM Breakthrough",
        url="https://example.com/llm",
        source="Tech Blog",
        published_at=None,
        snippet="Researchers published findings on language models.",
    )
    assert _matches_topics(article, ["LLM", "Robotics"]) is True
    assert _matches_topics(article, ["Quantum Computing"]) is False
    assert _matches_topics(article, []) is True



def test_within_lookback():
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=10)
    old = now - timedelta(hours=100)

    assert _within_lookback(recent, 48) is True
    assert _within_lookback(old, 48) is False
    assert _within_lookback(None, 48) is True


def test_dedupe_articles():
    now = datetime.now(timezone.utc)
    older = now - timedelta(hours=1)
    a1 = Article("Title 1", "https://example.com/1", "Source A", now, "Snippet 1")
    a2 = Article("Title 1", "https://example.com/1?utm_source=rss", "Source B", older, "Snippet 1 diff")
    a3 = Article("Unique Title", "https://example.com/2", "Source C", now, "Snippet 2")

    deduped = _dedupe_articles([a2, a1, a3])
    assert len(deduped) == 2
    urls = [a.url for a in deduped]
    assert "https://example.com/1" in urls
    assert "https://example.com/2" in urls


@patch("sources.feedparser.parse")
def test_fetch_rss_feed(mock_parse):
    now = datetime.now(timezone.utc)
    recent_date_str = (now - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    mock_parsed = MagicMock()
    mock_parsed.bozo = False
    mock_parsed.entries = [
        {
            "title": "AI Model Launch",
            "link": "https://example.com/rss1",
            "published": recent_date_str,
            "summary": "<p>Great new model released.</p>",
        },
        {
            "title": "Unrelated Topic",
            "link": "https://example.com/rss2",
            "published": recent_date_str,
            "summary": "Sports scores for today.",
        },
    ]
    mock_parse.return_value = mock_parsed

    articles = _fetch_rss_feed(
        "Test RSS",
        "https://example.com/feed.xml",
        lookback_hours=48,
        max_articles=5,
        topics=["AI"],
    )

    assert len(articles) == 1
    assert articles[0].title == "AI Model Launch"
    assert articles[0].snippet == "Great new model released."


@patch("sources.DDGS")
@patch("sources.time.sleep", return_value=None)
def test_fetch_search_results(mock_sleep, mock_ddgs_cls):
    now = datetime.now(timezone.utc)
    recent_iso_str = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_ddgs = MagicMock()
    mock_ddgs_cls.return_value = mock_ddgs
    mock_ddgs.news.return_value = [
        {
            "title": "DDG AI Update",
            "url": "https://example.com/ddg1",
            "date": recent_iso_str,
            "body": "DuckDuckGo search result body.",
        }
    ]

    articles = _fetch_search_results(
        topics=["AI"],
        lookback_hours=48,
        queries_per_topic=1,
        max_articles=5,
    )

    assert len(articles) == 1
    assert articles[0].title == "DDG AI Update"
    assert articles[0].source == "DuckDuckGo (AI)"



@patch("sources._fetch_rss_feed")
@patch("sources._fetch_search_results")
def test_gather_all(mock_search, mock_rss, sample_config, sample_articles):
    mock_rss.return_value = [sample_articles[0]]
    mock_search.return_value = [sample_articles[1]]

    result = gather_all(sample_config)
    assert len(result) == 2
    assert mock_rss.called
    assert mock_search.called


def test_filter_articles_for_recipient(sample_articles):
    now = datetime.now(timezone.utc)
    a_rss = Article(
        title="Unrelated Topic Header",
        url="https://example.com/rss",
        source="Custom Feed",
        published_at=now,
        snippet="Snippet",
    )
    all_articles = sample_articles + [a_rss]

    recipient_config = {
        "topics": ["LLM"],
        "rss_feeds": [{"name": "Custom Feed", "url": "https://example.com/feed"}],
    }

    filtered = filter_articles_for_recipient(all_articles, recipient_config)
    # Should include sample_articles[1] (matches topic LLM) and a_rss (matches source Custom Feed)
    assert len(filtered) == 2
    titles = [a.title for a in filtered]
    assert "Open Source LLM Benchmark Breakthrough" in titles
    assert "Unrelated Topic Header" in titles

