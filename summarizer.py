"""Gemini-powered digest summarization."""

from __future__ import annotations

import logging
import time
from typing import Any

import markdown
from google import genai
from google.genai import types

from sources import Article

logger = logging.getLogger(__name__)

def _build_system_instruction(topics: list[str]) -> str:
    topic_str = ", ".join(topics) if topics else "the user's interests"
    return f"""You are a digest curator covering: {topic_str}.

Rules:
- Only summarize items explicitly provided in the input list.
- Do not invent content, sources, or URLs.
- Group highlights by the user's topics where possible.
- For each highlight, include the source name and URL from the input.
- Be concise but informative; prefer bullet points over long paragraphs.
- Start with a 2-3 sentence executive summary of what matters most.
- Use Markdown formatting suitable for email."""

MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 3.0


def _build_prompt(articles: list[Article], topics: list[str]) -> str:
    topic_list = "\n".join(f"- {topic}" for topic in topics)
    article_blocks = "\n\n".join(article.to_prompt_block() for article in articles)
    return f"""Create a digest from the items below.

User topics of interest:
{topic_list}

Items ({len(articles)} total):
{article_blocks}

Output structure:
1. Executive Summary (2-3 sentences)
2. Highlights by Topic (bullets with title, brief summary, source, URL)
3. Notable Trends (optional short list of emerging patterns or themes)

Use only the items above. Include URLs for every highlight."""


def _digest_title(topics: list[str]) -> str:
    """Build a human-readable digest title from the topic list."""
    if not topics:
        return "Daily Digest"
    # Use up to 3 topics joined with " & " to keep the title concise
    joined = " & ".join(topics[:3])
    suffix = " & more" if len(topics) > 3 else ""
    return f"{joined}{suffix} Digest"


def _fallback_digest(articles: list[Article], topics: list[str]) -> str:
    title = _digest_title(topics)
    lines = [
        f"# {title} (Fallback)",
        "",
        "Gemini summarization was unavailable. Raw article list:",
        "",
        f"Topics: {', '.join(topics)}",
        "",
    ]
    for article in articles:
        date_str = (
            article.published_at.strftime("%Y-%m-%d")
            if article.published_at
            else "unknown date"
        )
        lines.extend(
            [
                f"## {article.title}",
                f"- Source: {article.source}",
                f"- Date: {date_str}",
                f"- URL: {article.url}",
                f"- Snippet: {article.snippet or '(no snippet)'}",
                "",
            ]
        )
    return "\n".join(lines)


def build_digest(
    articles: list[Article],
    topics: list[str],
    api_key: str,
    model: str,
) -> tuple[str, str]:
    if not articles:
        title = _digest_title(topics)
        md = (
            f"# {title}\n\n"
            "No new items were found for your topics in the configured lookback window."
        )
        return md, markdown.markdown(md)

    prompt = _build_prompt(articles, topics)
    client = genai.Client(api_key=api_key)

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_build_system_instruction(topics),
                    temperature=0.3,
                ),
            )
            digest_md = (response.text or "").strip()
            if not digest_md:
                raise ValueError("Gemini returned an empty response")
            digest_html = markdown.markdown(digest_md, extensions=["extra", "nl2br"])
            return digest_md, digest_html
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Gemini summarization attempt %d/%d failed: %s",
                attempt,
                MAX_RETRIES,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)

    logger.error("Using fallback digest after Gemini failure: %s", last_error)
    digest_md = _fallback_digest(articles, topics)
    return digest_md, markdown.markdown(digest_md, extensions=["extra", "nl2br"])
