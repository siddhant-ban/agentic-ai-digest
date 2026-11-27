"""Deterministic quality, grounding, dedup, cost, and trajectory metrics for offline evals."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from agentic_emailer.agents.verifier import _is_valid_http_url, verify_digest_draft
from agentic_emailer.ingestion.rss import normalize_seen_urls, normalize_url
from agentic_emailer.models import DigestDraft, RawArticle


class JudgeScorecard(BaseModel):
    """Structured LLM-as-judge rubric scores evaluated strictly against frozen source corpus."""

    faithfulness: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    synthesis_quality: int = Field(ge=1, le=5)
    conciseness: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)


class CaseEvalResult(BaseModel):
    """Per-case deterministic and judge metrics for one golden corpus scenario."""

    case_id: str
    schema_valid: bool
    url_grounding_rate: float
    duplicate_leakage_rate: float
    topic_coverage_rate: float
    developing_story_recall: float | None = None
    expected_developing_stories: int = 0
    matched_developing_stories: int = 0
    cluster_compression_ratio: float = 1.0
    clusters_formed: int = 0
    unseen_articles_count: int = 0
    revisions: int
    latency_ms: float
    used_fallback: bool = False
    verification_errors: list[str] = Field(default_factory=list)
    judge: JudgeScorecard | None = None
    draft: DigestDraft | None = None


class TrajectoryEvalResult(BaseModel):
    """Tool-use discipline evaluation for one ResearchAgent trajectory case."""

    case_id: str
    trajectory_precision: float
    scrape_calls: list[str] = Field(default_factory=list)
    search_calls: list[dict[str, Any]] = Field(default_factory=list)
    expected_scrape_hits: int = 0
    forbidden_scrape_hits: int = 0
    unnecessary_search_calls: int = 0
    latency_ms: float = 0.0


class EvalSummary(BaseModel):
    """Aggregated offline evaluation metrics across all corpus and trajectory cases."""

    schema_validity_rate: float
    url_grounding_rate: float
    duplicate_leakage_rate: float
    developing_story_recall: float = 1.0
    cluster_compression_ratio: float = 1.0
    topic_coverage_rate: float
    trajectory_precision: float
    avg_revisions: float
    avg_latency_ms: float
    estimated_cost_usd: float
    mean_faithfulness: float | None = None
    mean_relevance: float | None = None
    mean_synthesis_quality: float | None = None
    mean_conciseness: float | None = None


class EvalReport(BaseModel):
    """Complete serialized report persisted as JSON baseline or run artifact."""

    mode: str
    model_config_snapshot: dict[str, str] = Field(default_factory=dict)
    timestamp_utc: str
    summary: EvalSummary
    cases: list[CaseEvalResult] = Field(default_factory=list)
    trajectories: list[TrajectoryEvalResult] = Field(default_factory=list)
    gates_passed: bool = True
    gate_failures: list[str] = Field(default_factory=list)


TOPIC_STOPWORDS = frozenset(
    {
        "and",
        "the",
        "for",
        "with",
        "from",
        "use",
        "new",
        "news",
        "of",
        "in",
        "on",
        "to",
        "updates",
    }
)


def _topic_keywords(topic: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", topic.lower()))
    return {t for t in tokens if (len(t) >= 3 or t in {"ai", "ml"}) and t not in TOPIC_STOPWORDS}


def _topic_matches_text(topic: str, text: str) -> bool:
    topic_lower = topic.strip().lower()
    text_lower = text.lower()
    if topic_lower and topic_lower in text_lower:
        return True
    keywords = _topic_keywords(topic_lower)
    if not keywords:
        return False
    text_tokens = set(re.findall(r"[a-z0-9]+", text_lower))
    hits = len(keywords & text_tokens)
    required = 1 if len(keywords) <= 2 else max(2, (len(keywords) + 1) // 2)
    return hits >= required


def compute_case_metrics(
    case_id: str,
    draft: DigestDraft,
    allowed_articles: list[RawArticle],
    seen_urls: set[str],
    topics: list[str],
    *,
    available_topics: list[str] | None = None,
    suppressed_urls: set[str] | None = None,
    expected_developing_article_ids: list[str] | None = None,
    clusters_formed: int | None = None,
    clustered_articles_count: int | None = None,
    active_cluster_urls: set[str] | None = None,
    max_highlights: int | None = None,
    revisions: int = 0,
    latency_ms: float = 0.0,
    used_fallback: bool = False,
    judge: JudgeScorecard | None = None,
) -> CaseEvalResult:
    """Compute deterministic schema, grounding, dedup, developing-story, and clustering metrics."""
    disallowed_urls = set(seen_urls) | set(suppressed_urls or set())
    verification = verify_digest_draft(
        draft,
        allowed_articles,
        disallowed_urls,
        topics,
        max_highlights=max_highlights,
    )

    allowed_raw = {a.url.strip() for a in allowed_articles}
    allowed_canon = {a.canonical_url.strip() for a in allowed_articles} | {
        normalize_url(a.url) for a in allowed_articles
    }
    normalized_seen_exact = normalize_seen_urls(seen_urls)
    normalized_disallowed = normalize_seen_urls(disallowed_urls)

    unseen_articles = [
        a
        for a in allowed_articles
        if a.canonical_url not in normalized_seen_exact
        and normalize_url(a.url) not in normalized_seen_exact
        and a.url not in normalized_seen_exact
    ]
    unseen_non_suppressed = [
        a
        for a in unseen_articles
        if a.canonical_url not in normalized_disallowed
        and normalize_url(a.url) not in normalized_disallowed
        and a.url not in normalized_disallowed
    ]

    total_citations = 0
    grounded_citations = 0
    leaked_citations = 0
    seen_in_digest: set[str] = set()

    for highlight in draft.highlights:
        for cit in highlight.citations:
            total_citations += 1
            raw_u = cit.url.strip()
            norm_u = normalize_url(raw_u)
            if _is_valid_http_url(raw_u) and (raw_u in allowed_raw or norm_u in allowed_canon):
                grounded_citations += 1
            is_stale_or_rehash = raw_u in normalized_disallowed or norm_u in normalized_disallowed
            is_dup = norm_u in seen_in_digest
            if is_stale_or_rehash or is_dup:
                leaked_citations += 1
            seen_in_digest.add(norm_u)

    verification_errors = list(verification.errors)
    leaked_suppressed_clusters = 0
    if active_cluster_urls is not None and suppressed_urls:
        normalized_active = normalize_seen_urls(active_cluster_urls)
        for sup_url in sorted(normalize_seen_urls(suppressed_urls)):
            if sup_url in normalized_active:
                leaked_suppressed_clusters += 1
                verification_errors.append(
                    f"Expected suppressed near-duplicate URL survived Stage 4 clustering: {sup_url}"
                )

    if total_citations == 0:
        url_grounding_rate = 1.0 if not unseen_non_suppressed else 0.0
        duplicate_leakage_rate = 1.0 if leaked_suppressed_clusters > 0 else 0.0
    else:
        url_grounding_rate = round(grounded_citations / total_citations, 4)
        total_leak_checks = total_citations + leaked_suppressed_clusters
        total_leaks = leaked_citations + leaked_suppressed_clusters
        duplicate_leakage_rate = round(total_leaks / total_leak_checks, 4)

    # Topic coverage over recipient topics that actually have available unseen articles
    target_topics = available_topics if available_topics is not None else topics
    if not target_topics:
        topic_coverage_rate = 1.0
    else:
        covered = 0
        for topic in target_topics:
            topic_covered = any(
                _topic_matches_text(topic, f"{h.topic} {h.title} {h.summary}")
                for h in draft.highlights
            )
            if topic_covered:
                covered += 1
        topic_coverage_rate = round(covered / len(target_topics), 4)

    # Developing storyline recall: % of planted follow-ups flagged `developing_update`
    expected_dev_ids = [
        aid.strip() for aid in (expected_developing_article_ids or []) if aid.strip()
    ]
    expected_dev_count = len(expected_dev_ids)
    matched_dev_count = 0
    developing_story_recall: float | None = None

    if expected_dev_count > 0:
        article_by_id = {a.article_id: a for a in allowed_articles}
        developing_cited_ids: set[str] = set()
        developing_cited_urls: set[str] = set()
        for highlight in draft.highlights:
            if highlight.story_status != "developing_update":
                continue
            for cit in highlight.citations:
                developing_cited_ids.add(cit.article_id.strip())
                developing_cited_urls.add(normalize_url(cit.url))

        for expected_id in expected_dev_ids:
            art = article_by_id.get(expected_id)
            expected_canon = normalize_url(art.canonical_url or art.url) if art is not None else ""
            if expected_id in developing_cited_ids or (
                expected_canon and expected_canon in developing_cited_urls
            ):
                matched_dev_count += 1
        developing_story_recall = round(matched_dev_count / expected_dev_count, 4)

    # Cluster compression ratio: active clustered articles / clusters formed (>= 1.0),
    # excluding cross-day suppressed duplicates so clustering compression stays pure.
    effective_article_count = (
        clustered_articles_count
        if clustered_articles_count is not None
        else len(unseen_non_suppressed)
    )
    effective_clusters = clusters_formed if clusters_formed is not None else effective_article_count
    cluster_compression_ratio = (
        round(effective_article_count / effective_clusters, 4) if effective_clusters > 0 else 1.0
    )

    return CaseEvalResult(
        case_id=case_id,
        schema_valid=verification.passed,
        url_grounding_rate=url_grounding_rate,
        duplicate_leakage_rate=duplicate_leakage_rate,
        topic_coverage_rate=topic_coverage_rate,
        developing_story_recall=developing_story_recall,
        expected_developing_stories=expected_dev_count,
        matched_developing_stories=matched_dev_count,
        cluster_compression_ratio=cluster_compression_ratio,
        clusters_formed=effective_clusters,
        unseen_articles_count=effective_article_count,
        revisions=revisions,
        latency_ms=round(latency_ms, 1),
        used_fallback=used_fallback,
        verification_errors=verification_errors,
        judge=judge,
        draft=draft,
    )


def evaluate_trajectory_case(
    case_id: str,
    trajectory: list[dict[str, Any]],
    *,
    expected_scrape_ids: list[str],
    forbidden_scrape_ids: list[str],
    max_searches: int,
    max_scrapes: int | None = None,
    latency_ms: float = 0.0,
) -> TrajectoryEvalResult:
    """Score ResearchAgent tool-use precision against expected vs. unnecessary tool calls."""
    scrape_ids: list[str] = []
    search_calls: list[dict[str, Any]] = []

    for step in trajectory:
        name = str(step.get("name") or "")
        args = step.get("args") or {}
        if name == "fetch_article_content":
            art_id = str(args.get("article_id") or "").strip()
            if art_id:
                scrape_ids.append(art_id)
        elif name == "search_web_news":
            search_calls.append(dict(args))

    expected_set = set(expected_scrape_ids)
    forbidden_set = set(forbidden_scrape_ids)

    unique_scrapes = set(scrape_ids)
    expected_hits = len(unique_scrapes & expected_set)
    forbidden_hits = len(unique_scrapes & forbidden_set)
    unnecessary_searches = max(0, len(search_calls) - max_searches)

    if expected_set:
        # Score combines scrape precision (penalizing forbidden or duplicate scrapes) and recall
        # of thin snippets, minus a penalty for unnecessary searches beyond max_searches.
        if not scrape_ids:
            scrape_score = 0.0
        else:
            precision = expected_hits / len(scrape_ids)
            recall = expected_hits / len(expected_set)
            scrape_score = 0.6 * precision + 0.4 * recall
        search_penalty = 0.2 * unnecessary_searches
        score = max(0.0, min(1.0, scrape_score - search_penalty))
    else:
        # Rich-coverage scenario: ideal is 0 unnecessary scrapes and 0 unnecessary searches
        allowed_scrapes = 0 if max_scrapes is None else max_scrapes
        extra_scrapes = max(0, len(scrape_ids) - allowed_scrapes)
        total_extra = extra_scrapes + unnecessary_searches
        score = 1.0 if total_extra == 0 else max(0.0, round(1.0 - 0.25 * total_extra, 4))

    return TrajectoryEvalResult(
        case_id=case_id,
        trajectory_precision=round(score, 4),
        scrape_calls=scrape_ids,
        search_calls=search_calls,
        expected_scrape_hits=expected_hits,
        forbidden_scrape_hits=forbidden_hits,
        unnecessary_search_calls=unnecessary_searches,
        latency_ms=round(latency_ms, 1),
    )


def aggregate_metrics(
    cases: list[CaseEvalResult],
    trajectories: list[TrajectoryEvalResult],
    estimated_cost_usd: float,
) -> EvalSummary:
    """Aggregate per-case deterministic, trajectory, and judge metrics into an EvalSummary."""
    n = len(cases)
    schema_validity_rate = round(sum(1 for c in cases if c.schema_valid) / n, 4) if n else 1.0
    url_grounding_rate = round(sum(c.url_grounding_rate for c in cases) / n, 4) if n else 1.0
    duplicate_leakage_rate = (
        round(sum(c.duplicate_leakage_rate for c in cases) / n, 4) if n else 0.0
    )
    topic_coverage_rate = round(sum(c.topic_coverage_rate for c in cases) / n, 4) if n else 1.0
    avg_revisions = round(sum(c.revisions for c in cases) / n, 2) if n else 0.0
    avg_latency_ms = round(sum(c.latency_ms for c in cases) / n, 1) if n else 0.0

    total_expected_dev = sum(c.expected_developing_stories for c in cases)
    total_matched_dev = sum(c.matched_developing_stories for c in cases)
    developing_story_recall = (
        round(total_matched_dev / total_expected_dev, 4) if total_expected_dev > 0 else 1.0
    )

    total_unseen = sum(c.unseen_articles_count for c in cases)
    total_clusters = sum(c.clusters_formed for c in cases)
    cluster_compression_ratio = (
        round(total_unseen / total_clusters, 4) if total_clusters > 0 else 1.0
    )

    t_n = len(trajectories)
    trajectory_precision = (
        round(sum(t.trajectory_precision for t in trajectories) / t_n, 4) if t_n else 1.0
    )

    judged = [c.judge for c in cases if c.judge is not None]
    j_n = len(judged)
    mean_faithfulness = (
        round(sum(j.faithfulness for j in judged) / j_n, 2) if j_n else None
    )
    mean_relevance = round(sum(j.relevance for j in judged) / j_n, 2) if j_n else None
    mean_synthesis_quality = (
        round(sum(j.synthesis_quality for j in judged) / j_n, 2) if j_n else None
    )
    mean_conciseness = round(sum(j.conciseness for j in judged) / j_n, 2) if j_n else None

    return EvalSummary(
        schema_validity_rate=schema_validity_rate,
        url_grounding_rate=url_grounding_rate,
        duplicate_leakage_rate=duplicate_leakage_rate,
        developing_story_recall=developing_story_recall,
        cluster_compression_ratio=cluster_compression_ratio,
        topic_coverage_rate=topic_coverage_rate,
        trajectory_precision=trajectory_precision,
        avg_revisions=avg_revisions,
        avg_latency_ms=avg_latency_ms,
        estimated_cost_usd=round(estimated_cost_usd, 6),
        mean_faithfulness=mean_faithfulness,
        mean_relevance=mean_relevance,
        mean_synthesis_quality=mean_synthesis_quality,
        mean_conciseness=mean_conciseness,
    )
