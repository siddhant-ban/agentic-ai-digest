"""Core Pydantic v2 domain contracts for the agentic digest pipeline."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


class RssFeedConfig(BaseModel):
    """Configuration for a single RSS/Atom news feed."""

    name: str = Field(min_length=1)
    url: str = Field(min_length=1)

    @field_validator("name", "url")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned


class RecipientProfile(BaseModel):
    """Configuration profile for a single digest recipient."""

    id: str = Field(min_length=1)
    email_address: str
    subject_prefix: str = "[Daily Digest]"
    enabled: bool = True
    topics: list[str] = Field(default_factory=list)
    rss_feeds: list[RssFeedConfig] = Field(default_factory=list)
    lookback_hours: int = Field(default=36, ge=1, le=720)
    max_highlights: int = Field(default=8, ge=1, le=20)

    @field_validator("id")
    @classmethod
    def _strip_non_empty_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Recipient id must not be blank")
        return cleaned

    @field_validator("subject_prefix")
    @classmethod
    def _strip_subject_prefix(cls, value: str) -> str:
        return value.strip()

    @field_validator("email_address")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        cleaned = value.strip()
        if not EMAIL_REGEX.match(cleaned):
            raise ValueError(f"Invalid email address: {value!r}")
        return cleaned


class RawArticle(BaseModel):
    """Normalized news article gathered from RSS or targeted search."""

    article_id: str = Field(min_length=1)
    canonical_url: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source: str = Field(min_length=1)
    published_at: datetime | None = None
    snippet: str = ""
    full_text: str | None = None
    discovered_via: Literal["rss", "search"] = "rss"
    topic_hints: list[str] = Field(default_factory=list)

    @field_validator("article_id", "canonical_url", "url", "title", "source")
    @classmethod
    def _strip_non_empty_fields(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned

    def to_prompt_block(self, max_body_chars: int = 2000) -> str:
        """Render a compact, citation-ready prompt block for LLM agents."""
        if self.published_at:
            dt = (
                self.published_at.replace(tzinfo=UTC)
                if self.published_at.tzinfo is None
                else self.published_at.astimezone(UTC)
            )
            date_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        else:
            date_str = "unknown date"
        body_source = "Full Text" if self.full_text else "Snippet"
        raw_body = (self.full_text or self.snippet or "(no content)").strip()
        if max_body_chars > 0 and len(raw_body) > max_body_chars:
            if max_body_chars > 3:
                raw_body = raw_body[: max_body_chars - 3].rstrip() + "..."
            else:
                raw_body = raw_body[:max_body_chars]
        hints = ", ".join(self.topic_hints) if self.topic_hints else "general"
        return (
            f"[Article ID: {self.article_id}]\n"
            f"Title: {self.title}\n"
            f"Source: {self.source}\n"
            f"Discovery: {self.discovered_via}\n"
            f"Published: {date_str}\n"
            f"URL: {self.url}\n"
            f"Topic Hints: {hints}\n"
            f"{body_source}: {raw_body}"
        )


class StoryCluster(BaseModel):
    """Grouped multi-outlet coverage of a single story, with optional prior context."""

    cluster_id: str = Field(min_length=1)
    canonical_title: str = Field(min_length=1)
    topic: str = "General"
    articles: list[RawArticle] = Field(default_factory=list)
    embedding: list[float] | None = None
    is_developing_story: bool = False
    prior_story_summary: str | None = None
    prior_digest_date: str | None = None

    @field_validator("cluster_id", "canonical_title")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned

    def to_prompt_block(self, max_body_chars: int = 1800) -> str:
        """Render the cluster and its source articles for Synthesis/Critic prompts."""
        lines = [
            f"=== Story Cluster: {self.cluster_id} ===",
            f"Canonical Title: {self.canonical_title}",
            f"Topic Hint: {self.topic}",
            f"Developing Story: {'YES' if self.is_developing_story else 'NO'}",
        ]
        if self.is_developing_story and self.prior_story_summary:
            prior_date = self.prior_digest_date or "a recent digest"
            lines.append(f"Prior Coverage ({prior_date}): {self.prior_story_summary}")
        for article in self.articles:
            lines.append(article.to_prompt_block(max_body_chars=max_body_chars))
        return "\n".join(lines)


class Citation(BaseModel):
    """Verbatim source attribution linking a highlight back to an allowed article."""

    source_name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    article_id: str = Field(min_length=1)

    @field_validator("source_name", "url", "article_id")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Citation field must not be blank")
        return cleaned


class DigestHighlight(BaseModel):
    """Single synthesized news highlight within a digest."""

    title: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    story_status: Literal["new", "developing_update"] = "new"
    citations: list[Citation] = Field(default_factory=list)

    @field_validator("title", "topic", "summary", "why_it_matters")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("DigestHighlight field must not be blank")
        return cleaned


class DigestDraft(BaseModel):
    """Structured output contract produced by SynthesisAgent."""

    title: str = Field(min_length=1)
    executive_summary: str = Field(min_length=1)
    highlights: list[DigestHighlight] = Field(default_factory=list)
    notable_trends: list[str] = Field(default_factory=list)

    @field_validator("title", "executive_summary")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("DigestDraft field must not be blank")
        return cleaned


class VerificationResult(BaseModel):
    """Result of 100% deterministic grounding, freshness, and schema verification."""

    passed: bool
    errors: list[str] = Field(default_factory=list)
    invalid_urls: list[str] = Field(default_factory=list)
    stale_urls: list[str] = Field(default_factory=list)


class CriticVerdict(BaseModel):
    """Structured editorial & faithfulness audit produced by CriticAgent."""

    approved: bool
    faithfulness_score: int = Field(ge=1, le=5)
    relevance_score: int = Field(ge=1, le=5)
    issues: list[str] = Field(default_factory=list)
    revision_instructions: str = ""


class DeliveryReceipt(BaseModel):
    """Outcome record for attempting email delivery to one recipient."""

    recipient_id: str
    recipient_email: str
    status: Literal["sent", "dry_run", "failed"]
    provider: str
    error: str | None = None
    sent_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunMetrics(BaseModel):
    """Operational and quality telemetry for a single digest pipeline execution."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    recipients_processed: int = 0
    recipients_failed: int = 0
    articles_gathered: int = 0
    articles_deduped_cross_day: int = 0
    clusters_formed: int = 0
    developing_stories_linked: int = 0
    scrapes_performed: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    avg_revisions_per_recipient: float = 0.0
    deterministic_grounding_pass_rate: float = 1.0
    trace_id: str | None = None
