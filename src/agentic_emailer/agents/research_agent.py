"""Google ADK ResearchAgent for gap-driven search and selective full-article extraction."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import LlmAgent
from google.genai import types

from agentic_emailer.agents.runtime import run_adk_agent
from agentic_emailer.budget import BudgetExceededError, RunBudgetTracker
from agentic_emailer.config import Settings
from agentic_emailer.ingestion.extractor import extract_article_text
from agentic_emailer.ingestion.rss import normalize_url
from agentic_emailer.ingestion.search import search_news
from agentic_emailer.models import RawArticle

logger = logging.getLogger(__name__)

SearchFn = Callable[[str, int, int, str | None], list[RawArticle]]
ExtractFn = Callable[[str, int], Awaitable[str | None]]

THIN_SNIPPET_CHAR_THRESHOLD = 180
MIN_ARTICLES_PER_TOPIC = 3


def _default_search_fn(
    query: str,
    lookback_hours: int,
    max_results: int,
    topic: str | None,
) -> list[RawArticle]:
    return search_news(query, lookback_hours=lookback_hours, max_results=max_results, topic=topic)


@dataclass
class ResearchSessionContext:
    """Mutable working corpus and budget guardrails shared with ResearchAgent tools."""

    articles: dict[str, RawArticle]
    lookback_hours: int
    budget: RunBudgetTracker
    seen_urls: set[str] = field(default_factory=set)
    search_fn: SearchFn = _default_search_fn
    extract_fn: ExtractFn = extract_article_text

    def is_seen(self, article: RawArticle) -> bool:
        return (
            article.canonical_url in self.seen_urls
            or normalize_url(article.url) in self.seen_urls
            or article.url in self.seen_urls
        )


def build_research_tools(ctx: ResearchSessionContext) -> list[Callable[..., Any]]:
    """Build ADK function tools bound to a specific ResearchSessionContext."""

    async def search_web_news(query: str, topic: str) -> str:
        """Search the web for recent news articles to fill a topic coverage gap.

        Args:
            query: Concise, focused web news search query.
            topic: The target recipient topic this search is addressing.

        Returns:
            Compact JSON summary of newly discovered articles or budget status.
        """
        if not ctx.budget.can_search():
            return json.dumps(
                {
                    "status": "budget_exhausted",
                    "message": "Search call budget reached. Stop searching and finish research.",
                }
            )
        try:
            ctx.budget.consume_search()
        except BudgetExceededError:
            return json.dumps(
                {
                    "status": "budget_exhausted",
                    "message": "Search call budget reached. Stop searching and finish research.",
                }
            )

        try:
            found = await asyncio.to_thread(ctx.search_fn, query, ctx.lookback_hours, 8, topic)
        except Exception as exc:
            logger.warning("search_web_news tool failed for %r: %s", query, exc)
            return json.dumps(
                {
                    "status": "search_failed",
                    "query": query,
                    "topic": topic,
                    "message": "Search provider temporarily unavailable.",
                }
            )
        added_summaries: list[dict[str, Any]] = []
        for article in found:
            if ctx.is_seen(article):
                continue
            existing = ctx.articles.get(article.article_id)
            if existing is None:
                if topic and topic not in article.topic_hints:
                    article.topic_hints.append(topic)
                ctx.articles[article.article_id] = article
                added_summaries.append(
                    {
                        "article_id": article.article_id,
                        "title": article.title,
                        "source": article.source,
                        "snippet_len": len(article.snippet),
                    }
                )
            elif topic and topic not in existing.topic_hints:
                existing.topic_hints.append(topic)

        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "topic": topic,
                "new_articles_added": len(added_summaries),
                "articles": added_summaries,
            }
        )

    async def fetch_article_content(article_id: str) -> str:
        """Fetch and extract clean full-article body text for a thin-snippet article.

        Args:
            article_id: The exact article_id from the current article inventory.

        Returns:
            Compact JSON status showing extracted text length or failure reason.
        """
        article = ctx.articles.get(article_id.strip())
        if article is None:
            return json.dumps({"status": "not_found", "article_id": article_id})

        if article.full_text:
            return json.dumps(
                {
                    "status": "already_fetched",
                    "article_id": article.article_id,
                    "extracted_chars": len(article.full_text),
                }
            )

        if ctx.is_seen(article):
            return json.dumps(
                {
                    "status": "skipped_seen",
                    "article_id": article.article_id,
                    "message": "Article was already sent in a prior digest.",
                }
            )

        if not ctx.budget.can_scrape():
            return json.dumps(
                {
                    "status": "budget_exhausted",
                    "message": "Scrape budget reached. Stop fetching full text and finish.",
                }
            )
        try:
            ctx.budget.consume_scrape()
        except BudgetExceededError:
            return json.dumps(
                {
                    "status": "budget_exhausted",
                    "message": "Scrape budget reached. Stop fetching full text and finish.",
                }
            )

        try:
            extracted = await ctx.extract_fn(article.url, 4000)
        except Exception as exc:
            logger.warning("fetch_article_content failed for %s: %s", article.article_id, exc)
            extracted = None
        if not extracted:
            return json.dumps(
                {
                    "status": "extraction_failed",
                    "article_id": article.article_id,
                    "message": "Article could not be extracted (blocked, paywalled, or empty).",
                }
            )

        article.full_text = extracted
        return json.dumps(
            {
                "status": "ok",
                "article_id": article.article_id,
                "extracted_chars": len(extracted),
                "preview": extracted[:240],
            }
        )

    return [search_web_news, fetch_article_content]


RESEARCH_INSTRUCTION = f"""You are a disciplined news Research Agent preparing a verified source
corpus for a daily executive digest.

You have two tools:
1. `search_web_news(query, topic)` — searches for recent articles to fill topic coverage gaps.
2. `fetch_article_content(article_id)` — downloads and extracts full article body text for a
   high-signal article whose snippet is too thin.

STRICT OPERATING RULES:
- First, inspect the existing RSS article inventory provided in the user message, grouped with
  target topics and each article's `snippet_len`.
- SEARCH DISCIPLINE: ONLY call `search_web_news` for a target topic if that topic clearly has
  sparse coverage (fewer than {MIN_ARTICLES_PER_TOPIC} relevant articles in the inventory) or an
  obvious breaking-news gap. If all target topics already have at least {MIN_ARTICLES_PER_TOPIC}
  relevant articles with solid details, DO NOT call `search_web_news` at all.
- SCRAPE DISCIPLINE: ONLY call `fetch_article_content(article_id)` on high-signal articles
  relevant to target topics whose `snippet_len < {THIN_SNIPPET_CHAR_THRESHOLD}` characters (or
  whose snippet is vague/truncated) and where `has_full_text` is false. NEVER scrape articles
  that already have rich snippets (`snippet_len >= {THIN_SNIPPET_CHAR_THRESHOLD}`) or are off-topic.
- Never waste tool calls or exceed the remaining budgets. Once topic gaps are addressed and key
  thin snippets are enriched (or budgets are exhausted), stop calling tools and respond with a
  brief 2-3 sentence summary of research actions taken.
"""


def create_research_agent(model: str, tools: Sequence[Callable[..., Any]]) -> LlmAgent:
    """Create the Google ADK LlmAgent responsible for gap-driven research."""
    adk_tools: list[Any] = list(tools)
    return LlmAgent(
        name="research_agent",
        model=model,
        description=(
            "Adaptive research agent that selectively fills topic gaps via web search "
            "and extracts full text for high-signal thin-snippet articles."
        ),
        instruction=RESEARCH_INSTRUCTION,
        tools=adk_tools,
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def build_research_prompt(
    articles: list[RawArticle],
    topics: list[str],
    lookback_hours: int,
    budget: RunBudgetTracker,
) -> str:
    """Build a compact inventory prompt summarizing RSS articles and remaining budgets."""
    remaining_searches = max(0, budget.max_searches - budget.search_calls)
    remaining_scrapes = max(0, budget.max_scrapes - budget.scrape_calls)

    topic_lines = "\n".join(f"- {topic}" for topic in topics) if topics else "- General technology"
    inventory_items = [
        {
            "article_id": a.article_id,
            "title": a.title,
            "source": a.source,
            "snippet_len": len(a.snippet),
            "has_full_text": bool(a.full_text),
            "topic_hints": a.topic_hints,
            "snippet_preview": a.snippet[:260],
        }
        for a in articles
    ]
    inventory_json = json.dumps(inventory_items, ensure_ascii=False, indent=2)

    return (
        f"Target topics to cover (lookback window: {lookback_hours} hours):\n"
        f"{topic_lines}\n\n"
        f"Remaining tool budgets: up to {remaining_searches} searches, "
        f"up to {remaining_scrapes} article scrapes.\n\n"
        f"Already-fetched RSS inventory ({len(articles)} article(s)):\n"
        f"{inventory_json}\n\n"
        f"Review the inventory against the target topics. Only invoke tools when clearly needed "
        f"under the Search Discipline and Scrape Discipline rules, then summarize what you did."
    )


async def run_research_stage(
    rss_articles: list[RawArticle],
    topics: list[str],
    lookback_hours: int,
    settings: Settings,
    budget: RunBudgetTracker,
    *,
    seen_urls: set[str] | None = None,
    search_fn: SearchFn | None = None,
    extract_fn: ExtractFn | None = None,
) -> tuple[list[RawArticle], list[dict[str, Any]]]:
    """Execute the ADK ResearchAgent stage under RunBudgetTracker guardrails."""
    normalized_seen = {normalize_url(u) for u in (seen_urls or set()) if u.strip()} | set(
        seen_urls or set()
    )

    corpus: dict[str, RawArticle] = {}
    visible_inventory: list[RawArticle] = []
    for article in rss_articles:
        corpus[article.article_id] = article.model_copy(deep=True)
        if (
            article.canonical_url not in normalized_seen
            and normalize_url(article.url) not in normalized_seen
            and article.url not in normalized_seen
        ):
            visible_inventory.append(corpus[article.article_id])

    if not topics and not visible_inventory:
        return list(corpus.values()), []

    ctx = ResearchSessionContext(
        articles=corpus,
        lookback_hours=lookback_hours,
        budget=budget,
        seen_urls=normalized_seen,
        search_fn=search_fn or _default_search_fn,
        extract_fn=extract_fn or extract_article_text,
    )

    tools = build_research_tools(ctx)
    agent = create_research_agent(settings.research_model, tools)
    prompt = build_research_prompt(visible_inventory, topics, lookback_hours, budget)

    trajectory: list[dict[str, Any]] = []
    try:
        summary, _ = await run_adk_agent(
            agent, prompt, budget, trajectory_out=trajectory
        )
        logger.info(
            "ResearchAgent completed with %d tool call(s); corpus now has %d article(s). Note: %s",
            len(trajectory),
            len(ctx.articles),
            summary[:200],
        )
    except BudgetExceededError as exc:
        logger.warning(
            "ResearchAgent hit budget guardrail (%s); proceeding with %d article(s)",
            exc,
            len(ctx.articles),
        )
    except Exception as exc:
        logger.warning(
            "ResearchAgent failed (%s); proceeding deterministically with %d RSS article(s)",
            exc,
            len(ctx.articles),
        )

    return list(ctx.articles.values()), trajectory
