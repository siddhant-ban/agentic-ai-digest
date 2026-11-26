"""Google ADK CriticAgent auditing DigestDraft faithfulness, relevance, and editorial quality."""

from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent
from google.genai import types

from agentic_emailer.agents.runtime import run_structured_agent
from agentic_emailer.budget import RunBudgetTracker
from agentic_emailer.config import Settings
from agentic_emailer.models import CriticVerdict, DigestDraft, StoryCluster

CRITIC_INSTRUCTION = """You are a strict fact-checking and editorial Critic auditing a candidate
DigestDraft against the exact source corpus provided to the writer.

AUDIT CRITERIA:
1. FAITHFULNESS (1-5):
   - Check EVERY factual statement, number, date, product claim, and `why_it_matters`
     implication in `executive_summary`, `highlights`, and `notable_trends` strictly against
     the provided source articles.
   - Flag ANY unsupported claim, invented metric/date, distorted attribution, or overclaim
     not backed by the sources.
   - 5 = 100% strictly faithful; 4 = faithful with minor acceptable synthesis;
     <= 3 = contains unsupported/hallucinated claims or material distortion.
2. RELEVANCE & DEDUPLICATION (1-5):
   - Check that highlights match the recipient's target topics and exclude off-topic filler.
   - Flag if two separate highlights cover the exact same underlying story/announcement when
     they should have been merged into one highlight.
   - Check that stories marked `Developing Story: YES` in the source clusters use
     `story_status="developing_update"` and reference the prior context.
3. EDITORIAL QUALITY:
   - Flag generic fluff, vague buzzwords, or headline copy-pasting without real synthesis.
   - Note: On a sparse day with very few relevant source articles, a short digest covering
     only the genuinely relevant articles is GOOD—do not penalize brevity when sources are sparse.

APPROVAL RULE:
- Set `approved=True` ONLY IF `faithfulness_score >= 4`, `relevance_score >= 4`, AND there
  are ZERO unsupported/hallucinated factual claims.
- Otherwise set `approved=False`, list each concrete defect in `issues`, and write concise,
  actionable `revision_instructions` telling the writer what to fix, remove, or merge.
"""


def create_critic_agent(model: str) -> LlmAgent:
    """Create the Google ADK LlmAgent returning a structured CriticVerdict."""
    return LlmAgent(
        name="critic_agent",
        model=model,
        description="Fact-checking and editorial quality critic auditing candidate DigestDrafts.",
        instruction=CRITIC_INSTRUCTION,
        output_schema=CriticVerdict,
        generate_content_config=types.GenerateContentConfig(temperature=0.0),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def build_critic_prompt(
    draft: DigestDraft,
    clusters: list[StoryCluster],
    topics: list[str],
) -> str:
    """Construct the audit prompt pairing source clusters with the candidate DigestDraft."""
    topic_lines = "\n".join(f"- {t}" for t in topics) if topics else "- General updates"
    cluster_blocks = "\n\n".join(cluster.to_prompt_block() for cluster in clusters)
    draft_json = draft.model_dump_json(indent=2)

    return (
        f"Recipient target topics:\n{topic_lines}\n\n"
        f"SOURCE CORPUS ({len(clusters)} cluster(s)):\n\n{cluster_blocks}\n\n"
        f"CANDIDATE DIGEST DRAFT (JSON):\n{draft_json}\n\n"
        "Audit the candidate DigestDraft strictly against the SOURCE CORPUS and return a "
        "CriticVerdict JSON object."
    )


async def run_critic(
    draft: DigestDraft,
    clusters: list[StoryCluster],
    topics: list[str],
    settings: Settings,
    budget: RunBudgetTracker,
) -> tuple[CriticVerdict, list[dict[str, Any]]]:
    """Run CriticAgent via Google ADK Runner and enforce hard score-based approval floor."""
    agent = create_critic_agent(settings.critic_model)
    prompt = build_critic_prompt(draft, clusters, topics)
    verdict, trajectory = await run_structured_agent(agent, prompt, CriticVerdict, budget)

    # Deterministic floor: never allow approved=True when scores fall below threshold
    if verdict.approved and (verdict.faithfulness_score < 4 or verdict.relevance_score < 4):
        verdict = verdict.model_copy(update={"approved": False})

    return verdict, trajectory
