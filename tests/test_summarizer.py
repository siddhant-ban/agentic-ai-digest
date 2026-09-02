"""Tests for summarizer.py module."""

from unittest.mock import MagicMock, patch

import pytest
from summarizer import _build_prompt, _fallback_digest, build_digest


def test_build_prompt(sample_articles):
    topics = ["AI", "LLMs"]
    prompt = _build_prompt(sample_articles, topics)

    assert "User topics of interest:" in prompt
    assert "- AI" in prompt
    assert "- LLMs" in prompt
    assert "Items (2 total):" in prompt
    assert "New Gemini Model Released" in prompt
    assert "Open Source LLM Benchmark Breakthrough" in prompt


def test_fallback_digest(sample_articles):
    topics = ["AI", "LLMs"]
    fallback = _fallback_digest(sample_articles, topics)

    assert "# AI & LLMs Digest (Fallback)" in fallback
    assert "Topics: AI, LLMs" in fallback
    assert "## New Gemini Model Released" in fallback
    assert "- Source: TechCrunch AI" in fallback
    assert "## Open Source LLM Benchmark Breakthrough" in fallback


def test_build_digest_empty_articles():
    md, html = build_digest([], ["AI"], "fake-key", "gemini-2.0-flash")

    assert "# AI Digest" in md
    assert "No new items were found" in md
    assert "<h1" in html
    assert "No new items" in html


@patch("summarizer.genai.Client")
def test_build_digest_success(mock_client_cls, sample_articles):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    mock_response = MagicMock()
    mock_response.text = "# Daily AI Digest\n\n- Highlight 1: Gemini news\n- Highlight 2: LLM news"
    mock_client.models.generate_content.return_value = mock_response

    md, html = build_digest(sample_articles, ["AI"], "fake-key", "gemini-2.0-flash")

    assert md == mock_response.text
    assert "<ul>" in html or "<li>" in html
    mock_client_cls.assert_called_once_with(api_key="fake-key")
    mock_client.models.generate_content.assert_called_once()


@patch("summarizer.time.sleep", return_value=None)
@patch("summarizer.genai.Client")
def test_build_digest_retry_and_fallback(mock_client_cls, mock_sleep, sample_articles):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    # Simulate Gemini error on both attempts
    mock_client.models.generate_content.side_effect = Exception("API rate limit exceeded")

    md, html = build_digest(sample_articles, ["AI"], "fake-key", "gemini-2.0-flash")

    assert "# AI Digest (Fallback)" in md
    assert mock_client.models.generate_content.call_count == 2
    assert mock_sleep.call_count == 1
