"""Shared test fixtures for agentic_emailer tests."""

from datetime import datetime, timezone
import pytest
from sources import Article


@pytest.fixture
def sample_config():
    return {
        "topics": ["Artificial Intelligence", "LLMs"],
        "lookback_hours": 48,
        "max_articles_per_source": 10,
        "search_queries_per_topic": 2,
        "gemini_model": "gemini-2.0-flash",
        "rss_feeds": [
            {
                "name": "TechCrunch AI",
                "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
            }
        ],
        "email": {
            "sender": "sender@example.com",
            "recipient": "recipient@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "subject_prefix": "[Test Digest]",
        },
    }


@pytest.fixture
def sample_articles():
    now = datetime.now(timezone.utc)
    return [
        Article(
            title="New Gemini Model Released",
            url="https://example.com/gemini-news",
            source="TechCrunch AI",
            published_at=now,
            snippet="Google announces the latest update to Gemini AI models.",
        ),
        Article(
            title="Open Source LLM Benchmark Breakthrough",
            url="https://example.com/llm-benchmark",
            source="DuckDuckGo (LLMs)",
            published_at=now,
            snippet="A new open source model achieves top benchmark scores.",
        ),
    ]
