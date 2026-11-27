"""Deterministic cosine story clustering, cross-day semantic dedup, and storyline linking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from agentic_emailer.ingestion.rss import normalize_seen_urls, normalize_url
from agentic_emailer.memory.protocol import PriorClusterRecord, ensure_utc
from agentic_emailer.models import RawArticle, StoryCluster


@dataclass
class _WorkingCluster:
    articles: list[RawArticle] = field(default_factory=list)
    vector_sum: np.ndarray | None = None
    centroid: np.ndarray | None = None


def _to_unit_vector(raw: list[float] | None) -> np.ndarray | None:
    if not raw:
        return None
    vec = np.asarray(raw, dtype=np.float64)
    if vec.ndim != 1 or vec.size == 0 or not np.all(np.isfinite(vec)):
        return None
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-12:
        return None
    return vec / norm


def _sort_timestamp(dt: datetime | None) -> float:
    if dt is None:
        return float("-inf")
    return ensure_utc(dt).timestamp()


def _pick_cluster_topic(articles: list[RawArticle]) -> str:
    for article in articles:
        for hint in article.topic_hints:
            cleaned = hint.strip()
            if cleaned:
                return cleaned
    return "General"


def _validate_thresholds(
    merge_threshold: float,
    storyline_threshold: float,
    semantic_dedup_threshold: float,
) -> None:
    for name, value in (
        ("merge_threshold", merge_threshold),
        ("storyline_threshold", storyline_threshold),
        ("semantic_dedup_threshold", semantic_dedup_threshold),
    ):
        if not np.isfinite(value) or not (0.0 <= value <= 1.0):
            raise ValueError(f"{name} must be finite and within [0.0, 1.0], got {value!r}")
    if storyline_threshold > semantic_dedup_threshold:
        raise ValueError(
            "storyline_threshold must be <= semantic_dedup_threshold "
            f"({storyline_threshold} > {semantic_dedup_threshold})"
        )


def _index_prior_clusters(
    prior_clusters: list[PriorClusterRecord],
) -> dict[int, tuple[list[PriorClusterRecord], np.ndarray]]:
    """Group valid prior cluster unit vectors by dimensionality, ordered newest-first."""
    buckets: dict[int, list[tuple[PriorClusterRecord, np.ndarray]]] = {}
    for prior in prior_clusters:
        unit = _to_unit_vector(prior.embedding)
        if unit is None:
            continue
        buckets.setdefault(int(unit.shape[0]), []).append((prior, unit))

    indexed: dict[int, tuple[list[PriorClusterRecord], np.ndarray]] = {}
    for dim, items in buckets.items():
        # Deterministic newest-first order ensures np.argmax breaks ties toward most recent prior
        items.sort(
            key=lambda pair: (-ensure_utc(pair[0].last_updated_at).timestamp(), pair[0].cluster_id)
        )
        records = [pair[0] for pair in items]
        matrix = np.vstack([pair[1] for pair in items])
        indexed[dim] = (records, matrix)
    return indexed


def cluster_and_link_stories(
    articles: list[RawArticle],
    embeddings: list[list[float]],
    seen_urls: set[str],
    prior_clusters: list[PriorClusterRecord],
    *,
    merge_threshold: float = 0.84,
    storyline_threshold: float = 0.76,
    semantic_dedup_threshold: float = 0.94,
) -> tuple[list[StoryCluster], int]:
    """Cluster same-day coverage, drop cross-day duplicates, and link developing storylines.

    Steps:
      1. Hard exact URL dedup against `seen_urls` (increments cross-day dedup count) plus
         same-day exact URL dedup so no URL repeats within a cluster.
      2. Greedy same-day clustering newest-first: merge into the highest-similarity cluster
         when cosine similarity to its spherical centroid >= `merge_threshold`.
      3. Cross-day semantic comparison against `prior_clusters` via vectorized numpy dot product:
         - `max_sim >= semantic_dedup_threshold`: suppress cluster as redundant re-hash.
         - `storyline_threshold <= max_sim < semantic_dedup_threshold`: mark developing story
           and attach `prior_story_summary` + `prior_digest_date`.

    Returns:
      `(active_clusters, cross_day_deduped_count)`
    """
    if len(articles) != len(embeddings):
        raise ValueError(
            f"articles and embeddings must have equal length ({len(articles)} != {len(embeddings)})"
        )
    _validate_thresholds(merge_threshold, storyline_threshold, semantic_dedup_threshold)

    if not articles:
        return [], 0

    normalized_seen = normalize_seen_urls(seen_urls)
    deduped_count = 0
    counted_seen_canonical: set[str] = set()

    # Step 1: Hard exact URL dedup vs. prior seen_urls + same-day canonical URL coalescing
    grouped_unseen: dict[str, list[tuple[RawArticle, np.ndarray | None]]] = {}
    for article, raw_emb in zip(articles, embeddings, strict=True):
        canonical = normalize_url(article.canonical_url or article.url)
        raw_norm = normalize_url(article.url)
        if (
            canonical in normalized_seen
            or raw_norm in normalized_seen
            or article.url in normalized_seen
        ):
            if canonical not in counted_seen_canonical:
                counted_seen_canonical.add(canonical)
                deduped_count += 1
            continue
        grouped_unseen.setdefault(canonical, []).append((article, _to_unit_vector(raw_emb)))

    if not grouped_unseen:
        return [], deduped_count

    unique_today: list[tuple[RawArticle, np.ndarray | None]] = []
    for copies in grouped_unseen.values():
        if len(copies) == 1:
            unique_today.append(copies[0])
            continue

        # Among copies of the exact same canonical URL, prefer full_text, valid vector, newest ts
        copies.sort(
            key=lambda pair: (
                0 if pair[0].full_text else 1,
                0 if pair[1] is not None else 1,
                -_sort_timestamp(pair[0].published_at),
                pair[0].article_id,
            )
        )
        best_article, best_vec = copies[0]
        if best_vec is None:
            for _, candidate_vec in copies[1:]:
                if candidate_vec is not None:
                    best_vec = candidate_vec
                    break

        merged_hints: list[str] = []
        seen_hints: set[str] = set()
        for copy_article, _ in copies:
            for hint in copy_article.topic_hints:
                cleaned = hint.strip()
                key = cleaned.lower()
                if cleaned and key not in seen_hints:
                    seen_hints.add(key)
                    merged_hints.append(cleaned)

        if merged_hints != best_article.topic_hints:
            best_article = best_article.model_copy(update={"topic_hints": merged_hints})
        unique_today.append((best_article, best_vec))

    # Sort distinct unseen articles strictly newest-first with deterministic tie-breaking
    unique_today.sort(
        key=lambda pair: (
            -_sort_timestamp(pair[0].published_at),
            pair[0].article_id,
            pair[0].canonical_url,
        )
    )

    # Step 2: Greedy same-day centroid clustering
    working_clusters: list[_WorkingCluster] = []
    for article, unit_vec in unique_today:
        if unit_vec is None:
            working_clusters.append(_WorkingCluster(articles=[article]))
            continue

        best_cluster: _WorkingCluster | None = None
        best_sim = float("-inf")
        for cluster in working_clusters:
            if cluster.centroid is None or cluster.centroid.shape != unit_vec.shape:
                continue
            sim = float(np.dot(unit_vec, cluster.centroid))
            if sim > best_sim:
                best_sim = sim
                best_cluster = cluster

        if best_cluster is not None and best_sim >= merge_threshold:
            best_cluster.articles.append(article)
            assert best_cluster.vector_sum is not None
            updated_sum = best_cluster.vector_sum + unit_vec
            best_cluster.vector_sum = updated_sum
            norm = float(np.linalg.norm(updated_sum))
            if norm > 1e-12:
                best_cluster.centroid = updated_sum / norm
        else:
            working_clusters.append(
                _WorkingCluster(
                    articles=[article],
                    vector_sum=unit_vec.copy(),
                    centroid=unit_vec.copy(),
                )
            )

    # Step 3: Cross-day semantic dedup & developing storyline linking
    prior_index = _index_prior_clusters(prior_clusters)
    active_clusters: list[StoryCluster] = []

    for working in working_clusters:
        lead = working.articles[0]
        is_developing = False
        prior_summary: str | None = None
        prior_date: str | None = None

        if working.centroid is not None:
            indexed = prior_index.get(int(working.centroid.shape[0]))
            if indexed is not None:
                prior_records, prior_matrix = indexed
                sims = prior_matrix @ working.centroid
                best_idx = int(np.argmax(sims))
                max_sim = float(sims[best_idx])

                if max_sim >= semantic_dedup_threshold:
                    deduped_count += len(working.articles)
                    continue
                if max_sim >= storyline_threshold:
                    matched = prior_records[best_idx]
                    is_developing = True
                    prior_summary = matched.summary.strip() or matched.canonical_title
                    prior_date = ensure_utc(matched.last_updated_at).strftime("%Y-%m-%d")

        active_clusters.append(
            StoryCluster(
                cluster_id=f"cluster-{lead.article_id}",
                canonical_title=lead.title,
                topic=_pick_cluster_topic(working.articles),
                articles=working.articles,
                embedding=working.centroid.tolist() if working.centroid is not None else None,
                is_developing_story=is_developing,
                prior_story_summary=prior_summary,
                prior_digest_date=prior_date,
            )
        )

    return active_clusters, deduped_count
