"""Google ADK SynthesisAgent producing typed DigestDraft outputs."""

from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent
from google.genai import types

from agentic_emailer.agents.runtime import run_structured_agent
from agentic_emailer.budget import RunBudgetTracker
from agentic_emailer.config import Settings
from agentic_emailer.models import DigestDraft, StoryCluster

SYNTHESIS_INSTRUCTION = """You are a senior executive news briefing editor producing a structured
DigestDraft JSON response tailored strictly to the recipient's target topics.

NON-NEGOTIABLE GROUNDING & QUALITY RULES:
1. STRICT FAITHFULNESS: Base EVERY factual claim, number, date, product name, and implication
   strictly on the provided source articles. NEVER invent facts, metrics, benchmarks, quotes,
   or speculation presented as fact.
2. VERBATIM CITATIONS: Every highlight MUST include at least one citation in `citations` whose
   `url`, `article_id`, and `source_name` are copied VERBATIM from the provided input articles.
   Never modify, shorten, or invent a URL or `article_id`.
3. NO STALE URLS: NEVER cite or summarize any article whose URL appears in the disallowed /
   already-seen URL list.
4. NO DUPLICATE STORIES OR REPEATED URLS: Never cite the same URL in two different highlights.
   If multiple input articles cover the same announcement or event, synthesize them into ONE
   highlight with citations to those articles rather than creating duplicate highlights.
5. DEVELOPING STORIES: If a cluster is marked `Developing Story: YES`, set
   `story_status="developing_update"` and briefly explain what is new compared to the provided
   `Prior Coverage`. For all other stories, set `story_status="new"`.
6. TOPIC ALIGNMENT & SPARSE-DAY HONESTY: Group highlights cleanly under the recipient's target
   topics and exclude off-topic articles. If there are only a few relevant articles on a sparse
   day, produce only highlights genuinely supported by those articles—never fabricate stories.
7. CONCISE EXECUTIVE STYLE:
   - `executive_summary`: 2-3 crisp sentences (>= 50 chars) capturing top themes and takeaways.
   - Each highlight's `summary`: 2-3 clear, information-dense sentences synthesizing key facts.
   - Each highlight's `why_it_matters`: 1-2 concrete sentences explaining practical impact
     grounded in the article's facts (no vague clichés).
   - `notable_trends`: 0-3 short, grounded cross-cutting patterns observed in the input corpus.
"""


def create_synthesis_agent(model: str) -> LlmAgent:
    """Create the Google ADK LlmAgent producing structured DigestDraft JSON."""
    return LlmAgent(
        name="synthesis_agent",
        model=model,
        description="Editorial synthesis agent producing grounded DigestDraft briefings.",
        instruction=SYNTHESIS_INSTRUCTION,
        output_schema=DigestDraft,
        generate_content_config=types.GenerateContentConfig(temperature=0.25),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def build_synthesis_prompt(
    clusters: list[StoryCluster],
    topics: list[str],
    *,
    max_highlights: int = 8,
    seen_urls: set[str] | None = None,
    revision_feedback: str | None = None,
) -> str:
    """Construct the synthesis user prompt from story clusters and optional revision feedback."""
    topic_lines = "\n".join(f"- {t}" for t in topics) if topics else "- General updates"
    cluster_blocks = "\n\n".join(cluster.to_prompt_block() for cluster in clusters)

    parts = [
        f"Recipient target topics:\n{topic_lines}",
        f"Maximum highlights allowed: {max_highlights}",
    ]

    if seen_urls:
        seen_list = "\n".join(f"- {u}" for u in sorted(seen_urls)[:50])
        parts.append(
            f"Already-Seen / DISALLOWED URLs (DO NOT cite or include these):\n{seen_list}"
        )

    parts.append(f"Available Story Clusters ({len(clusters)} total):\n\n{cluster_blocks}")

    if revision_feedback:
        parts.append(
            "REVISION REQUIRED — Fix EVERY issue below in this updated DigestDraft:\n"
            f"{revision_feedback}"
        )

    parts.append(
        "Produce a complete, grounded DigestDraft JSON object using ONLY the articles above."
    )
    return "\n\n".join(parts)


async def run_synthesis(
    clusters: list[StoryCluster],
    topics: list[str],
    settings: Settings,
    budget: RunBudgetTracker,
    *,
    max_highlights: int = 8,
    seen_urls: set[str] | None = None,
    revision_feedback: str | None = None,
) -> tuple[DigestDraft, list[dict[str, Any]]]:
    """Run SynthesisAgent via Google ADK Runner and return a validated DigestDraft."""
    agent = create_synthesis_agent(settings.synthesis_model)
    prompt = build_synthesis_prompt(
        clusters,
        topics,
        max_highlights=max_highlights,
        seen_urls=seen_urls,
        revision_feedback=revision_feedback,
    )
    return await run_structured_agent(agent, prompt, DigestDraft, budget)
