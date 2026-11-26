"""Thin deterministic invariants for URL normalization and grounding verification (zero mocks)."""

from __future__ import annotations

from agentic_emailer.agents.verifier import verify_digest_draft
from agentic_emailer.ingestion.rss import make_article_id, normalize_url
from agentic_emailer.models import Citation, DigestDraft, DigestHighlight, RawArticle


def _make_article(url: str, title: str = "Test Article", source: str = "Test Source") -> RawArticle:
    canonical = normalize_url(url)
    return RawArticle(
        article_id=make_article_id(canonical),
        canonical_url=canonical,
        url=url,
        title=title,
        source=source,
        snippet="Concise factual summary of a verified story.",
    )


def test_url_normalization_strips_tracking_and_dedups_id() -> None:
    url_a = (
        "HTTPS://Example.COM:443/news/launch/?utm_source=newsletter&utm_medium=email"
        "&fbclid=abc123&gclid=xyz&ref=homepage&id=42#top"
    )
    url_b = "https://example.com/news/launch?id=42&source=rss"

    canon_a = normalize_url(url_a)
    canon_b = normalize_url(url_b)

    assert canon_a == "https://example.com/news/launch?id=42"
    assert canon_a == canon_b
    assert make_article_id(canon_a) == make_article_id(canon_b)
    assert len(make_article_id(canon_a)) == 16
    # Malformed ports / broken IPv6 must never raise unhandled ValueError
    assert normalize_url("https://example.com:99999/bad") == "https://example.com:99999/bad"
    assert normalize_url("http://[::1/bad") == "http://[::1/bad"


def test_verifier_rejects_hallucinated_url() -> None:
    allowed = _make_article("https://blog.example.com/real-release", title="Real Release")
    hallucinated_url = "https://blog.example.com/invented-release"

    draft = DigestDraft(
        title="AI Digest",
        executive_summary="An executive briefing summarizing the real and invented releases.",
        highlights=[
            DigestHighlight(
                title="Invented Story",
                topic="LLM releases",
                summary="Describes a fabricated launch that never appeared in the input corpus.",
                why_it_matters="Hallucinated URLs destroy reader trust.",
                citations=[
                    Citation(
                        source_name="Test Source",
                        url=hallucinated_url,
                        article_id=make_article_id(normalize_url(hallucinated_url)),
                    )
                ],
            )
        ],
    )

    result = verify_digest_draft(draft, [allowed], seen_urls=set(), topics=["LLM releases"])
    assert not result.passed
    assert hallucinated_url in result.invalid_urls
    assert result.errors

    # Malformed port and broken IPv6 citation URLs must be rejected cleanly without raising
    malformed_draft = draft.model_copy(
        update={
            "highlights": [
                draft.highlights[0].model_copy(
                    update={
                        "citations": [
                            Citation(
                                source_name="Bad Port",
                                url="https://blog.example.com:99999/bad",
                                article_id="0000000000000001",
                            ),
                            Citation(
                                source_name="Broken IPv6",
                                url="http://[::1/broken",
                                article_id="0000000000000002",
                            ),
                        ]
                    }
                )
            ]
        }
    )
    malformed_result = verify_digest_draft(
        malformed_draft, [allowed], seen_urls=set(), topics=["LLM releases"]
    )
    assert not malformed_result.passed
    assert "https://blog.example.com:99999/bad" in malformed_result.invalid_urls
    assert "http://[::1/broken" in malformed_result.invalid_urls


def test_verifier_rejects_stale_seen_url() -> None:
    stale_url = "https://news.example.com/yesterday-story"
    allowed = _make_article(f"{stale_url}?utm_source=feed", title="Yesterday Story")

    draft = DigestDraft(
        title="AI Digest",
        executive_summary="An executive briefing attempting to resurface a previously sent story.",
        highlights=[
            DigestHighlight(
                title="Yesterday Story",
                topic="AI agents",
                summary="Rehashes a story that was already delivered in a previous digest run.",
                why_it_matters="Stale repeats degrade daily digest freshness.",
                citations=[
                    Citation(
                        source_name=allowed.source,
                        url=allowed.url,
                        article_id=allowed.article_id,
                    )
                ],
            )
        ],
    )

    result = verify_digest_draft(draft, [allowed], seen_urls={stale_url}, topics=["AI agents"])
    assert not result.passed
    assert allowed.url in result.stale_urls
    assert result.errors


def test_verifier_accepts_valid_verbatim_citations() -> None:
    art_1 = _make_article("https://openai.com/news/agent-sdk", title="Agent SDK", source="OpenAI")
    art_2 = _make_article("https://huggingface.co/blog/evals", title="Open Evals", source="HF")

    draft = DigestDraft(
        title="AI Engineering Digest",
        executive_summary=(
            "Two verified engineering updates covering agent tooling and open evaluation suites."
        ),
        highlights=[
            DigestHighlight(
                title="Agent SDK Update",
                topic="AI agents",
                summary="OpenAI published updates to its agent SDK.",
                why_it_matters="Streamlines tool calling and observability for engineers.",
                citations=[
                    Citation(
                        source_name=art_1.source,
                        url=art_1.url,
                        article_id=art_1.article_id,
                    )
                ],
            ),
            DigestHighlight(
                title="Open Evaluation Benchmarks",
                topic="LLM evaluation",
                summary="Hugging Face detailed reproducible offline evaluation benchmarks.",
                why_it_matters="Enables systematic regression testing across model upgrades.",
                citations=[
                    Citation(
                        source_name=art_2.source,
                        url=art_2.url,
                        article_id=art_2.article_id,
                    )
                ],
            ),
        ],
    )

    result = verify_digest_draft(
        draft,
        [art_1, art_2],
        seen_urls={"https://example.com/unrelated-old-story"},
        topics=["AI agents", "LLM evaluation"],
    )
    assert result.passed
    assert result.errors == []
    assert result.invalid_urls == []
    assert result.stale_urls == []
