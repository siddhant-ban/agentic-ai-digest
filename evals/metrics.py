"""Deterministic quality, grounding, dedup, cost, and trajectory metrics for offline evals."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from agentic_emailer.agents.verifier import _is_valid_http_url, verify_digest_draft
from agentic_emailer.ingestion.rss import normalize_url
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
    max_highlights: int | None = None,
    revisions: int = 0,
    latency_ms: float = 0.0,
    used_fallback: bool = False,
    judge: JudgeScorecard | None = None,
) -> CaseEvalResult:
    """Compute deterministic schema, grounding, dedup leakage, and topic coverage metrics."""
    verification = verify_digest_draft(
        draft,
        allowed_articles,
        seen_urls,
        topics,
        max_highlights=max_highlights,
    )

    allowed_raw = {a.url.strip() for a in allowed_articles}
    allowed_canon = {a.canonical_url.strip() for a in allowed_articles} | {
        normalize_url(a.url) for a in allowed_articles
    }
    normalized_seen = {normalize_url(u) for u in seen_urls if u.strip()} | set(seen_urls)

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
            is_stale = raw_u in normalized_seen or norm_u in normalized_seen
            is_dup = norm_u in seen_in_digest
            if is_stale or is_dup:
                leaked_citations += 1
            seen_in_digest.add(norm_u)

    if total_citations == 0:
        unseen_articles = [
            a
            for a in allowed_articles
            if a.canonical_url not in normalized_seen
            and normalize_url(a.url) not in normalized_seen
        ]
        url_grounding_rate = 1.0 if not unseen_articles else 0.0
        duplicate_leakage_rate = 0.0
    else:
        url_grounding_rate = round(grounded_citations / total_citations, 4)
        duplicate_leakage_rate = round(leaked_citations / total_citations, 4)

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

    return CaseEvalResult(
        case_id=case_id,
        schema_valid=verification.passed,
        url_grounding_rate=url_grounding_rate,
        duplicate_leakage_rate=duplicate_leakage_rate,
        topic_coverage_rate=topic_coverage_rate,
        revisions=revisions,
        latency_ms=round(latency_ms, 1),
        used_fallback=used_fallback,
        verification_errors=verification.errors,
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
