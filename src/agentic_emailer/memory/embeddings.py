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
MAX_EMBED_BATCH_CHARS = 48_000
MAX_EMBED_CHARS = 4000
MAX_VERTEX_EMBED_CONCURRENCY = 8
EMBED_TIMEOUT_SECONDS = 60.0
TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
MAX_TRANSIENT_RETRIES = 2


def _is_transient_genai_error(exc: BaseException) -> bool:
    if isinstance(exc, (genai_errors.ServerError, TimeoutError)):
        return True
    if isinstance(exc, genai_errors.APIError):
        return getattr(exc, "code", None) in TRANSIENT_HTTP_CODES
    return False


def _is_single_item_batch_error(exc: BaseException) -> bool:
    """Detect Vertex AI rejections caused by sending >1 text in a single embed request."""
    if isinstance(exc, ValueError) and "one content at a time" in str(exc).lower():
        return True
    if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 400:
        msg = str(exc).lower()
        return any(
            marker in msg
            for marker in (
                "at most 1",
                "single input",
                "one content",
                "instances",
                "batch",
            )
        )
    return False


def _uses_vertex_single_item_embeddings(settings: Settings) -> bool:
    """Return True when the configured Vertex AI embedding model requires 1 text per request."""
    if not settings.google_genai_use_vertexai:
        return False
    model_lower = settings.embedding_model.strip().lower()
    return "gemini-embedding" in model_lower or "maas" in model_lower


def _chunk_texts_for_embedding(
    texts: list[str],
    *,
    max_batch_size: int = MAX_EMBED_BATCH_SIZE,
    max_batch_chars: int = MAX_EMBED_BATCH_CHARS,
) -> list[list[str]]:
    """Split sanitized texts into chunks bounded by both item count and total characters."""
    chunks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0

    for text in texts:
        text_len = len(text)
        if current and (
            len(current) >= max_batch_size or current_chars + text_len > max_batch_chars
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(text)
        current_chars += text_len

    if current:
        chunks.append(current)
    return chunks


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
    return genai.Client(vertexai=False)


async def _call_embed_with_retry(
    client: genai.Client,
    model: str,
    contents: str | list[str],
    embed_config: types.EmbedContentConfig,
) -> Any:
    """Invoke `client.aio.models.embed_content` with exponential backoff on 429/5xx/timeouts."""
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        try:
            return await asyncio.wait_for(
                client.aio.models.embed_content(
                    model=model,
                    contents=contents,
                    config=embed_config,
                ),
                timeout=EMBED_TIMEOUT_SECONDS,
            )
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
    raise RuntimeError("Unreachable retry exhaustion in _call_embed_with_retry")


async def _embed_chunk_single_items(
    client: genai.Client,
    model: str,
    chunk: list[str],
    embed_config: types.EmbedContentConfig,
    output_dimensionality: int,
) -> tuple[list[list[float]], int]:
    """Embed a chunk via bounded-concurrency single-text requests (Vertex AI gemini-embedding)."""
    semaphore = asyncio.Semaphore(MAX_VERTEX_EMBED_CONCURRENCY)

    async def _embed_one(text: str) -> tuple[list[float], int]:
        async with semaphore:
            response = await _call_embed_with_retry(client, model, text, embed_config)
        raw_embeddings = response.embeddings or []
        if len(raw_embeddings) != 1:
            raise RuntimeError(
                f"Embedding count mismatch: sent 1 text, received {len(raw_embeddings)} embeddings"
            )
        emb = raw_embeddings[0]
        values = list(emb.values) if emb.values is not None else None
        tokens = _extract_batch_tokens(response, [text])
        return _l2_normalize_vector(values, output_dimensionality), tokens

    results = await asyncio.gather(*(_embed_one(text) for text in chunk))
    vectors = [vec for vec, _ in results]
    total_tokens = sum(tok for _, tok in results)
    return vectors, total_tokens


async def embed_texts(
    texts: list[str],
    settings: Settings,
    budget: RunBudgetTracker,
    *,
    output_dimensionality: int = EMBEDDING_DIMENSIONS,
    task_type: str | None = None,
) -> list[list[float]]:
    """Embed texts in chunks of <=64 via `gemini-embedding-001` and return L2-normalized vectors.

    Uses unprompted `gemini-embedding-001` vectors (`task_type=None`) by default because
    `task_type="CLUSTERING"` compresses distinct stories within the same topic into ~0.80-0.88
    cosine similarity, whereas unprompted embeddings cleanly separate distinct same-topic stories
    (<=0.72) from developing follow-ups (~0.82-0.90), same-day multi-outlet coverage (>=0.84),
    and cross-day near-duplicate re-hashes (>=0.94).

    Respects `GOOGLE_GENAI_USE_VERTEXAI` automatically via `Settings`. Uses `:batchEmbedContents`
    on Gemini Developer API and bounded-concurrency single-text calls on Vertex AI models that
    enforce 1 input text per request (e.g., `gemini-embedding-001`), while accounting every logical
    chunk and summed token count in `budget`.
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

    single_item_mode = _uses_vertex_single_item_embeddings(settings)
    chunks = _chunk_texts_for_embedding(sanitized)
    normalized_vectors: list[list[float]] = []

    client = _create_genai_client(settings)
    try:
        for batch in chunks:
            budget.assert_can_call_llm()

            if single_item_mode and len(batch) > 1:
                chunk_vectors, batch_tokens = await _embed_chunk_single_items(
                    client,
                    settings.embedding_model,
                    batch,
                    embed_config,
                    output_dimensionality,
                )
            else:
                try:
                    response = await _call_embed_with_retry(
                        client,
                        settings.embedding_model,
                        batch if len(batch) > 1 else batch[0],
                        embed_config,
                    )
                    raw_embeddings = response.embeddings or []
                    if len(raw_embeddings) != len(batch):
                        raise RuntimeError(
                            f"Embedding count mismatch: sent {len(batch)} texts, "
                            f"received {len(raw_embeddings)} embeddings"
                        )
                    batch_tokens = _extract_batch_tokens(response, batch)
                    chunk_vectors = [
                        _l2_normalize_vector(
                            list(emb.values) if emb.values is not None else None,
                            output_dimensionality,
                        )
                        for emb in raw_embeddings
                    ]
                except Exception as exc:
                    if (
                        settings.google_genai_use_vertexai
                        and len(batch) > 1
                        and _is_single_item_batch_error(exc)
                    ):
                        logger.info(
                            "Vertex AI model %s rejected multi-text batch (%s); "
                            "falling back to bounded single-text embedding requests",
                            settings.embedding_model,
                            exc,
                        )
                        single_item_mode = True
                        chunk_vectors, batch_tokens = await _embed_chunk_single_items(
                            client,
                            settings.embedding_model,
                            batch,
                            embed_config,
                            output_dimensionality,
                        )
                    else:
                        raise

            budget.consume_llm_call(
                input_tokens=batch_tokens,
                output_tokens=0,
                model=settings.embedding_model,
            )
            normalized_vectors.extend(chunk_vectors)
    finally:
        try:
            await client.aio.aclose()
        finally:
            client.close()

    return normalized_vectors
