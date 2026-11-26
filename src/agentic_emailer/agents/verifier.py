"""100% deterministic grounding, freshness, and schema verifier (zero LLM calls)."""

from __future__ import annotations

from agentic_emailer.ingestion.rss import is_valid_http_url, normalize_url
from agentic_emailer.models import (
    Citation,
    DigestDraft,
    DigestHighlight,
    RawArticle,
    VerificationResult,
)

MIN_EXECUTIVE_SUMMARY_CHARS = 30
_is_valid_http_url = is_valid_http_url


def _build_allowed_lookups(
    allowed_articles: list[RawArticle],
) -> tuple[set[str], set[str], dict[str, RawArticle], dict[str, RawArticle]]:
    allowed_raw_urls: set[str] = set()
    allowed_canonical_urls: set[str] = set()
    allowed_by_id: dict[str, RawArticle] = {}
    allowed_by_canonical: dict[str, RawArticle] = {}

    for article in allowed_articles:
        raw = article.url.strip()
        canonical = article.canonical_url.strip() or normalize_url(raw)
        normalized_raw = normalize_url(raw)

        allowed_raw_urls.add(raw)
        allowed_canonical_urls.add(canonical)
        allowed_canonical_urls.add(normalized_raw)
        allowed_by_id[article.article_id] = article
        allowed_by_canonical[canonical] = article
        allowed_by_canonical[normalized_raw] = article

    return allowed_raw_urls, allowed_canonical_urls, allowed_by_id, allowed_by_canonical


def _build_seen_lookup(seen_urls: set[str]) -> set[str]:
    normalized: set[str] = set()
    for url in seen_urls:
        cleaned = url.strip()
        if not cleaned:
            continue
        normalized.add(cleaned)
        normalized.add(normalize_url(cleaned))
    return normalized


def verify_digest_draft(
    draft: DigestDraft,
    allowed_articles: list[RawArticle],
    seen_urls: set[str],
    topics: list[str],
    *,
    max_highlights: int | None = None,
) -> VerificationResult:
    """Deterministically verify URL grounding, freshness, uniqueness, and basic completeness."""
    del topics  # Reserved for deterministic topic metadata checks; relevance is judged by Critic.
    errors: list[str] = []
    invalid_urls: list[str] = []
    stale_urls: list[str] = []

    allowed_raw_urls, allowed_canonical_urls, allowed_by_id, _ = _build_allowed_lookups(
        allowed_articles
    )
    normalized_seen = _build_seen_lookup(seen_urls)

    # Check executive summary completeness
    if len(draft.executive_summary.strip()) < MIN_EXECUTIVE_SUMMARY_CHARS:
        errors.append(
            f"executive_summary is too short ({len(draft.executive_summary.strip())} chars; "
            f"minimum {MIN_EXECUTIVE_SUMMARY_CHARS})."
        )

    if max_highlights is not None and len(draft.highlights) > max_highlights:
        errors.append(
            f"highlights count ({len(draft.highlights)}) exceeds max_highlights ({max_highlights})."
        )

    unseen_allowed = [
        article
        for article in allowed_articles
        if article.canonical_url not in normalized_seen
        and normalize_url(article.url) not in normalized_seen
        and article.url not in normalized_seen
    ]
    if unseen_allowed and not draft.highlights:
        errors.append(
            "highlights list is empty even though unseen allowed articles were provided."
        )

    seen_across_highlights: dict[str, int] = {}

    for idx, highlight in enumerate(draft.highlights, start=1):
        if not highlight.citations:
            errors.append(f"Highlight #{idx} ({highlight.title!r}) has no citations.")
            continue

        seen_in_this_highlight: set[str] = set()
        for citation in highlight.citations:
            cited_url = citation.url.strip()

            if not _is_valid_http_url(cited_url):
                if cited_url not in invalid_urls:
                    invalid_urls.append(cited_url)
                errors.append(
                    f"Highlight #{idx} ({highlight.title!r}) cites a malformed or non-HTTP(S) "
                    f"URL: {cited_url}"
                )
                continue

            norm_url = normalize_url(cited_url)

            url_allowed = cited_url in allowed_raw_urls or norm_url in allowed_canonical_urls
            if not url_allowed:
                if cited_url not in invalid_urls:
                    invalid_urls.append(cited_url)
                errors.append(
                    f"Highlight #{idx} ({highlight.title!r}) cites an ungrounded URL not in "
                    f"allowed input articles: {cited_url}"
                )
            else:
                # Verify article_id matches an allowed article if provided
                matched_article = allowed_by_id.get(citation.article_id)
                if matched_article is None:
                    errors.append(
                        f"Highlight #{idx} ({highlight.title!r}) cites unknown article_id "
                        f"{citation.article_id!r} for URL {cited_url}"
                    )
                else:
                    matched_norm = normalize_url(matched_article.url)
                    if norm_url not in (matched_article.canonical_url, matched_norm):
                        errors.append(
                            f"Highlight #{idx} ({highlight.title!r}) pairs article_id "
                            f"{citation.article_id!r} with mismatched URL {cited_url}"
                        )

            if cited_url in normalized_seen or norm_url in normalized_seen:
                if cited_url not in stale_urls:
                    stale_urls.append(cited_url)
                errors.append(
                    f"Highlight #{idx} ({highlight.title!r}) cites an already-seen stale URL: "
                    f"{cited_url}"
                )

            if norm_url in seen_in_this_highlight:
                errors.append(
                    f"Highlight #{idx} ({highlight.title!r}) repeats duplicate citation URL: "
                    f"{cited_url}"
                )
            else:
                seen_in_this_highlight.add(norm_url)

            prev_highlight = seen_across_highlights.get(norm_url)
            if prev_highlight is not None and prev_highlight != idx:
                errors.append(
                    f"Duplicate URL {cited_url} repeated across highlights #{prev_highlight} "
                    f"and #{idx}."
                )
            elif prev_highlight is None:
                seen_across_highlights[norm_url] = idx

    return VerificationResult(
        passed=len(errors) == 0,
        errors=errors,
        invalid_urls=invalid_urls,
        stale_urls=stale_urls,
    )


def sanitize_draft_grounding(
    draft: DigestDraft,
    allowed_articles: list[RawArticle],
    seen_urls: set[str],
    *,
    max_highlights: int | None = None,
) -> DigestDraft:
    """Deterministically strip ungrounded, stale, or duplicate citations from a draft.

    Guarantees that any highlight surviving sanitization cites only verified, unseen,
    deduplicated URLs with canonical source/article_id metadata.
    """
    _, _, _, allowed_by_canonical = _build_allowed_lookups(allowed_articles)
    normalized_seen = _build_seen_lookup(seen_urls)

    used_urls: set[str] = set()
    clean_highlights: list[DigestHighlight] = []

    for highlight in draft.highlights:
        valid_citations: list[Citation] = []
        for citation in highlight.citations:
            cited_url = citation.url.strip()
            if not _is_valid_http_url(cited_url):
                continue
            norm_url = normalize_url(cited_url)
            if norm_url in normalized_seen or cited_url in normalized_seen:
                continue
            if norm_url in used_urls:
                continue
            matched = allowed_by_canonical.get(norm_url)
            if matched is None or not _is_valid_http_url(matched.url):
                continue
            used_urls.add(norm_url)
            valid_citations.append(
                Citation(
                    source_name=matched.source,
                    url=matched.url,
                    article_id=matched.article_id,
                )
            )

        if valid_citations:
            clean_highlights.append(
                highlight.model_copy(update={"citations": valid_citations})
            )
            if max_highlights is not None and len(clean_highlights) >= max_highlights:
                break

    return draft.model_copy(update={"highlights": clean_highlights})
