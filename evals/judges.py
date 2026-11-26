"""Gemini LLM-as-Judge evaluator returning Pydantic JudgeScorecards."""

from __future__ import annotations

import asyncio
import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from agentic_emailer.agents.runtime import parse_structured_output
from agentic_emailer.budget import RunBudgetTracker
from agentic_emailer.models import DigestDraft, RawArticle
from evals.metrics import JudgeScorecard

logger = logging.getLogger(__name__)
TRANSIENT_CODES = frozenset({429, 500, 502, 503, 504})

JUDGE_SYSTEM_INSTRUCTION = """You are an impartial, rigorous AI evaluation judge grading a daily
executive news digest strictly against its frozen input source articles.

Score each dimension from 1 (very poor) to 5 (excellent):
- faithfulness (1-5): Is EVERY factual claim, number, entity, and `why_it_matters` takeaway
  strictly supported by the provided input articles with zero hallucinated facts or unsupported
  extrapolations? (5 = 100% grounded; 4 = grounded with negligible synthesis phrasing;
  <= 3 = contains unsupported claims or factual distortion.)
- relevance (1-5): Do highlights match the recipient's target topics without off-topic noise
  or stale/disallowed articles? On a sparse day with only 1-2 relevant articles, covering only
  what exists without hallucinating earns 5.
- synthesis_quality (1-5): Does the digest synthesize multi-outlet stories smoothly (merging
  duplicate coverage rather than repeating the same story twice) and articulate genuine
  significance instead of merely copy-pasting headlines?
- conciseness (1-5): Is the digest crisp, scannable, and well-structured for a 2-minute morning
  executive read?

Provide a concise `rationale` citing specific strengths or any defects found.
"""


async def evaluate_digest_with_judge(
    draft: DigestDraft,
    articles: list[RawArticle],
    topics: list[str],
    seen_urls: set[str],
    *,
    model: str,
    budget: RunBudgetTracker,
) -> JudgeScorecard:
    """Grade a candidate DigestDraft against its frozen source corpus using Gemini."""
    budget.assert_can_call_llm()

    topic_lines = "\n".join(f"- {t}" for t in topics)
    article_blocks = "\n\n".join(a.to_prompt_block(max_body_chars=1800) for a in articles)
    seen_block = "\n".join(f"- {u}" for u in sorted(seen_urls)) if seen_urls else "(none)"
    draft_json = draft.model_dump_json(indent=2)

    prompt = (
        f"RECIPIENT TARGET TOPICS:\n{topic_lines}\n\n"
        f"DISALLOWED / ALREADY-SEEN URLs:\n{seen_block}\n\n"
        f"FROZEN INPUT SOURCE ARTICLES ({len(articles)} total):\n\n{article_blocks}\n\n"
        f"GENERATED DIGEST DRAFT (JSON):\n{draft_json}\n\n"
        "Return a JudgeScorecard JSON object grading the generated digest strictly against "
        "the frozen input source articles."
    )

    client = genai.Client()
    response = None
    for attempt in range(3):
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=JUDGE_SYSTEM_INSTRUCTION,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=JudgeScorecard,
                ),
            )
            break
        except genai_errors.APIError as exc:
            is_transient = isinstance(exc, genai_errors.ServerError) or (
                getattr(exc, "code", None) in TRANSIENT_CODES
            )
            if not is_transient or attempt >= 2:
                raise
            delay = 2.0 * (2**attempt)
            logger.warning(
                "Transient GenAI judge error (attempt %d/3): %s; retrying in %.1fs",
                attempt + 1,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    assert response is not None

    usage = getattr(response, "usage_metadata", None)
    in_tok = int(getattr(usage, "prompt_token_count", None) or 0) if usage else 0
    cand_tok = int(getattr(usage, "candidates_token_count", None) or 0) if usage else 0
    thought_tok = int(getattr(usage, "thoughts_token_count", None) or 0) if usage else 0
    out_tok = cand_tok + thought_tok
    budget.consume_llm_call(input_tokens=in_tok, output_tokens=out_tok, model=model)

    raw_text = (response.text or "").strip()
    return parse_structured_output(raw_text, JudgeScorecard)
