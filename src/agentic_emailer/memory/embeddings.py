"""Batch Gemini embeddings (768-dim, L2-normalized) with retry and budget accounting."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from agentic_emailer.budget import RunBudgetTracker
from agentic_emailer.config import Settings
from agentic_emailer.models import RawArticle

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSIONS = 768
MAX_EMBED_BATCH_SIZE = 64
MAX_EMBED_CHARS = 4000
EMBED_TIMEOUT_SECONDS = 60.0
TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
MAX_TRANSIENT_RETRIES = 2


def _is_transient_genai_error(exc: BaseException) -> bool:
    if isinstance(exc, (genai_errors.ServerError, TimeoutError)):
        return True
    if isinstance(exc, genai_errors.APIError):
        return getattr(exc, "code", None) in TRANSIENT_HTTP_CODES
    return False


def build_article_embedding_text(article: RawArticle, max_chars: int = MAX_EMBED_CHARS) -> str:
    """Build a concise, outlet-agnostic text representation of an article for clustering."""
    title = article.title.strip()
    snippet = article.snippet.strip()
    full_text = (article.full_text or "").strip()

    parts = [title] if title else []
    if snippet:
        parts.append(snippet)
    if full_text and len(snippet) < 160:
        excerpt = full_text[:800].strip()
        if excerpt and excerpt not in snippet:
            parts.append(excerpt)

    combined = "\n".join(parts).strip() or "untitled"
    if len(combined) > max_chars:
        return combined[:max_chars].rstrip()
    return combined


def _l2_normalize_vector(values: list[float] | None, expected_dim: int) -> list[float]:
    if not values:
        return [0.0] * expected_dim
    vec = np.asarray(values, dtype=np.float64)
    if vec.ndim != 1 or vec.size == 0 or not np.all(np.isfinite(vec)):
        return [0.0] * expected_dim
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-12:
        return [0.0] * int(vec.size)
    return (vec / norm).tolist()


def _extract_batch_tokens(response: Any, batch_texts: list[str]) -> int:
    """Extract input token count from embedding response metadata, or estimate conservatively."""
    embeddings = getattr(response, "embeddings", None) or []
    token_sum = 0
    for item in embeddings:
        stats = getattr(item, "statistics", None)
        tok = getattr(stats, "token_count", None) if stats is not None else None
        if tok is not None and tok > 0:
            token_sum += int(tok)
    if token_sum > 0:
        return token_sum

    metadata = getattr(response, "metadata", None)
    billable_chars = (
        getattr(metadata, "billable_character_count", None) if metadata is not None else None
    )
    if billable_chars is not None and billable_chars > 0:
        return max(1, int(billable_chars) // 4)

    return max(1, sum(len(text) for text in batch_texts) // 4)


def _create_genai_client(settings: Settings) -> genai.Client:
    """Construct a google-genai Client honoring Settings Vertex AI vs. Gemini API config."""
    if settings.google_genai_use_vertexai:
        return genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )
    api_key = settings.resolve_gemini_api_key()
    if api_key:
        return genai.Client(vertexai=False, api_key=api_key)
    return genai.Client()


async def embed_texts(
    texts: list[str],
    settings: Settings,
    budget: RunBudgetTracker,
    *,
    output_dimensionality: int = EMBEDDING_DIMENSIONS,
    task_type: str | None = None,
) -> list[list[float]]:
    """Embed texts in batches of <=64 via `gemini-embedding-001` and return L2-normalized vectors.

    Uses unprompted `gemini-embedding-001` vectors (`task_type=None`) by default because
    `task_type="CLUSTERING"` compresses distinct stories within the same topic into ~0.80-0.88
    cosine similarity, whereas unprompted embeddings cleanly separate distinct same-topic stories
    (<=0.72) from developing follow-ups (~0.82-0.90), same-day multi-outlet coverage (>=0.84),
    and cross-day near-duplicate re-hashes (>=0.94).

    Respects `GOOGLE_GENAI_USE_VERTEXAI` automatically via `Settings` and accounts
    every batch API call and estimated/reported token count in `budget`.
    """
    if not texts:
        return []
    if output_dimensionality <= 0:
        raise ValueError("output_dimensionality must be positive")

    sanitized: list[str] = []
    for raw in texts:
        cleaned = raw.strip()
        if not cleaned:
            cleaned = "untitled"
        elif len(cleaned) > MAX_EMBED_CHARS:
            cleaned = cleaned[:MAX_EMBED_CHARS].rstrip()
        sanitized.append(cleaned)

    embed_config = (
        types.EmbedContentConfig(
            output_dimensionality=output_dimensionality,
            task_type=task_type,
        )
        if task_type is not None
        else types.EmbedContentConfig(output_dimensionality=output_dimensionality)
    )

    normalized_vectors: list[list[float]] = []
    client = _create_genai_client(settings)
    try:
        for start in range(0, len(sanitized), MAX_EMBED_BATCH_SIZE):
            batch = sanitized[start : start + MAX_EMBED_BATCH_SIZE]
            budget.assert_can_call_llm()

            response = None
            for attempt in range(MAX_TRANSIENT_RETRIES + 1):
                try:
                    response = await asyncio.wait_for(
                        client.aio.models.embed_content(
                            model=settings.embedding_model,
                            contents=batch,
                            config=embed_config,
                        ),
                        timeout=EMBED_TIMEOUT_SECONDS,
                    )
                    break
                except Exception as exc:
                    if not _is_transient_genai_error(exc) or attempt >= MAX_TRANSIENT_RETRIES:
                        raise
                    delay = 2.0 * (2**attempt)
                    logger.warning(
                        "Transient GenAI embedding error (attempt %d/%d): %s; retrying in %.1fs",
                        attempt + 1,
                        MAX_TRANSIENT_RETRIES + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)

            assert response is not None
            raw_embeddings = response.embeddings or []
            if len(raw_embeddings) != len(batch):
                raise RuntimeError(
                    f"Embedding count mismatch: sent {len(batch)} texts, "
                    f"received {len(raw_embeddings)} embeddings"
                )

            batch_tokens = _extract_batch_tokens(response, batch)
            budget.consume_llm_call(
                input_tokens=batch_tokens,
                output_tokens=0,
                model=settings.embedding_model,
            )

            for emb in raw_embeddings:
                values = list(emb.values) if emb.values is not None else None
                normalized_vectors.append(_l2_normalize_vector(values, output_dimensionality))
    finally:
        try:
            await client.aio.aclose()
        finally:
            client.close()

    return normalized_vectors
