"""MemoryStore Protocol and prior-cluster domain contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from agentic_emailer.models import RawArticle, RecipientProfile, RunMetrics, StoryCluster


def ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def derive_cluster_summary(cluster: StoryCluster, max_chars: int = 500) -> str:
    """Derive a concise summary for persisting a StoryCluster across runs."""
    if cluster.summary and cluster.summary.strip():
        text = cluster.summary.strip()
    elif cluster.articles:
        lead = cluster.articles[0]
        snippet = lead.snippet.strip()
        if snippet:
            text = f"{cluster.canonical_title}: {snippet}"
        else:
            text = cluster.canonical_title
    else:
        text = cluster.canonical_title

    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


class PriorClusterRecord(BaseModel):
    """Persisted story cluster snapshot loaded from prior runs for semantic dedup and linking."""

    cluster_id: str = Field(min_length=1)
    canonical_title: str = Field(min_length=1)
    topic: str = "General"
    summary: str = ""
    urls: list[str] = Field(default_factory=list)
    embedding: list[float] = Field(default_factory=list)
    last_updated_at: datetime

    @field_validator("cluster_id", "canonical_title")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("PriorClusterRecord field must not be blank")
        return cleaned

    @field_validator("last_updated_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


@runtime_checkable
class MemoryStore(Protocol):
    """Abstract persistent memory backend (Local JSON for dev/evals, Firestore for GCP)."""

    async def load_active_profiles(self) -> list[RecipientProfile]: ...

    async def upsert_profiles(self, profiles: list[RecipientProfile]) -> int: ...

    async def get_seen_urls(self, since_days: int = 14) -> set[str]: ...

    async def get_recent_clusters(self, since_days: int = 7) -> list[PriorClusterRecord]: ...

    async def record_sent_articles(
        self,
        articles: list[RawArticle],
        recipient_id: str,
        cluster_ids: dict[str, str],
    ) -> None: ...

    async def save_story_clusters(self, clusters: list[StoryCluster]) -> None: ...

    async def save_run_metrics(self, metrics: RunMetrics) -> None: ...

    async def list_recent_runs(self, limit: int = 10) -> list[RunMetrics]: ...

    async def prune_expired(self, retention_days: int = 30) -> int: ...
