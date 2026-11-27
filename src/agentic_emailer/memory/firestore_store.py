"""Production Cloud Firestore MemoryStore optimized for GCP Always Free quotas."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from agentic_emailer.ingestion.rss import normalize_url
from agentic_emailer.memory.protocol import (
    PriorClusterRecord,
    derive_cluster_summary,
    ensure_utc,
)
from agentic_emailer.models import RawArticle, RecipientProfile, RunMetrics, StoryCluster

logger = logging.getLogger(__name__)

COLLECTION_RECIPIENTS = "recipients"
COLLECTION_SEEN_ARTICLES = "seen_articles"
COLLECTION_STORY_CLUSTERS = "story_clusters"
COLLECTION_RUNS = "runs"

# Firestore hard write-batch ceiling is 500 operations; 400 leaves safe headroom.
MAX_BATCH_OPS = 400
MAX_DOC_ID_BYTES = 1500

T = TypeVar("T")


def _validate_doc_id(doc_id: str, label: str) -> str:
    cleaned = doc_id.strip()
    if not cleaned:
        raise ValueError(f"{label} document ID must not be blank")
    if "/" in cleaned or cleaned in {".", ".."}:
        raise ValueError(f"Invalid Firestore {label} document ID: {doc_id!r}")
    if cleaned.startswith("__") and cleaned.endswith("__"):
        raise ValueError(f"Reserved Firestore {label} document ID: {doc_id!r}")
    if len(cleaned.encode("utf-8")) > MAX_DOC_ID_BYTES:
        raise ValueError(f"Firestore {label} document ID exceeds {MAX_DOC_ID_BYTES} bytes")
    return cleaned


def _chunked(items: list[T], size: int) -> Iterable[list[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _parse_firestore_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return ensure_utc(datetime.fromisoformat(raw))
        except ValueError:
            return None
    return None


async def _close_async_stream(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        await aclose()


class FirestoreMemoryStore:
    """Async Cloud Firestore MemoryStore engineered for GCP Always Free tier quotas.

    Quota discipline:
    - Single-field filters only (zero composite indexes required).
    - Server-side field masks (`select(["canonical_url"])` / `select([])`) to minimize egress.
    - Chunked `AsyncWriteBatch` commits (<= 400 operations/batch) to minimize round-trips.
    - Programmatic 30-day retention pruning instead of managed TTL policies.
    """

    def __init__(
        self,
        project_id: str | None = None,
        database: str = "(default)",
    ) -> None:
        self.project_id = project_id
        self.database = database
        self._client: firestore.AsyncClient | None = None

    def _get_client(self) -> firestore.AsyncClient:
        if self._client is None:
            self._client = firestore.AsyncClient(
                project=self.project_id,
                database=self.database,
            )
        return self._client

    def close(self) -> None:
        """Synchronously release the underlying Firestore client reference if initialized."""
        if self._client is not None:
            self._client.close()
            self._client = None

    async def aclose(self) -> None:
        """Asynchronously close the Firestore AsyncClient gRPC asyncio channel if initialized."""
        client = self._client
        self._client = None
        if client is None:
            return
        transport = getattr(client, "_transport", None)
        try:
            if transport is not None:
                close_fn = getattr(transport, "close", None)
                if callable(close_fn):
                    close_result = close_fn()
                    if inspect.isawaitable(close_result):
                        await close_result
        finally:
            client.close()

    async def load_active_profiles(self) -> list[RecipientProfile]:
        """Load enabled RecipientProfiles using a single-field equality filter."""
        client = self._get_client()
        query = client.collection(COLLECTION_RECIPIENTS).where(
            filter=FieldFilter("enabled", "==", True)
        )

        profiles: list[RecipientProfile] = []
        stream = query.stream()
        try:
            async for snap in stream:
                data = snap.to_dict() or {}
                if not data.get("id"):
                    data["id"] = snap.id
                try:
                    profile = RecipientProfile.model_validate(data)
                except Exception as exc:
                    logger.warning(
                        "Skipping invalid Firestore recipient doc %r: %s", snap.id, exc
                    )
                    continue
                if profile.enabled:
                    profiles.append(profile)
        finally:
            await _close_async_stream(stream)

        profiles.sort(key=lambda p: p.id)
        return profiles

    async def upsert_profiles(self, profiles: list[RecipientProfile]) -> int:
        """Upsert RecipientProfiles in chunked AsyncWriteBatches (<= 400 writes/batch)."""
        if not profiles:
            return 0
        deduped: dict[str, RecipientProfile] = {}
        for profile in profiles:
            doc_id = _validate_doc_id(profile.id, "recipient")
            deduped[doc_id] = profile

        client = self._get_client()
        collection = client.collection(COLLECTION_RECIPIENTS)
        items = list(deduped.items())

        for chunk in _chunked(items, MAX_BATCH_OPS):
            batch = client.batch()
            for doc_id, profile in chunk:
                batch.set(collection.document(doc_id), profile.model_dump(mode="python"))
            await batch.commit()

        return len(deduped)

    async def get_seen_urls(self, since_days: int = 14) -> set[str]:
        """Fetch canonical URLs seen within `since_days` via a single range filter + projection."""
        if since_days < 0:
            raise ValueError("since_days must be non-negative")
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        client = self._get_client()
        query = (
            client.collection(COLLECTION_SEEN_ARTICLES)
            .where(filter=FieldFilter("first_seen_at", ">=", cutoff))
            .select(["canonical_url"])
        )

        urls: set[str] = set()
        stream = query.stream()
        try:
            async for snap in stream:
                data = snap.to_dict() or {}
                canonical = str(data.get("canonical_url") or "").strip()
                if canonical:
                    urls.add(normalize_url(canonical))
        finally:
            await _close_async_stream(stream)
        return urls

    async def get_recent_clusters(self, since_days: int = 7) -> list[PriorClusterRecord]:
        """Fetch recent StoryClusters updated within `since_days` via a single range filter."""
        if since_days < 0:
            raise ValueError("since_days must be non-negative")
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        client = self._get_client()
        query = client.collection(COLLECTION_STORY_CLUSTERS).where(
            filter=FieldFilter("last_updated_at", ">=", cutoff)
        )

        records: list[PriorClusterRecord] = []
        stream = query.stream()
        try:
            async for snap in stream:
                data = snap.to_dict() or {}
                updated_at = _parse_firestore_timestamp(data.get("last_updated_at"))
                if updated_at is None:
                    logger.warning("Skipping Firestore cluster %r with missing timestamp", snap.id)
                    continue
                try:
                    record = PriorClusterRecord(
                        cluster_id=str(data.get("cluster_id") or snap.id),
                        canonical_title=str(data.get("canonical_title") or ""),
                        topic=str(data.get("topic") or "General"),
                        summary=str(data.get("summary") or ""),
                        urls=[str(u) for u in (data.get("urls") or []) if str(u).strip()],
                        embedding=[float(x) for x in (data.get("embedding") or [])],
                        last_updated_at=updated_at,
                    )
                except Exception as exc:
                    logger.warning("Skipping invalid Firestore cluster %r: %s", snap.id, exc)
                    continue
                records.append(record)
        finally:
            await _close_async_stream(stream)

        records.sort(key=lambda r: (-r.last_updated_at.timestamp(), r.cluster_id))
        return records

    async def record_sent_articles(
        self,
        articles: list[RawArticle],
        recipient_id: str,
        cluster_ids: dict[str, str],
    ) -> None:
        """Record sent articles in chunked AsyncWriteBatches using zero extra reads."""
        if not articles:
            return
        deduped: dict[str, RawArticle] = {}
        for article in articles:
            doc_id = _validate_doc_id(article.article_id, "seen_article")
            deduped[doc_id] = article

        now = datetime.now(UTC)
        clean_recipient = recipient_id.strip()
        client = self._get_client()
        collection = client.collection(COLLECTION_SEEN_ARTICLES)

        for chunk in _chunked(list(deduped.items()), MAX_BATCH_OPS):
            batch = client.batch()
            for doc_id, article in chunk:
                payload: dict[str, Any] = {
                    "article_id": doc_id,
                    "canonical_url": normalize_url(article.canonical_url or article.url),
                    "title": article.title,
                    "source": article.source,
                    "first_seen_at": now,
                }
                cluster_id = (cluster_ids.get(doc_id) or "").strip()
                if cluster_id:
                    payload["cluster_id"] = cluster_id
                if clean_recipient:
                    payload["recipient_ids_sent"] = firestore.ArrayUnion([clean_recipient])
                batch.set(collection.document(doc_id), payload, merge=True)
            await batch.commit()

    async def save_story_clusters(self, clusters: list[StoryCluster]) -> None:
        """Persist story clusters with 768-dim embeddings in chunked AsyncWriteBatches."""
        if not clusters:
            return
        deduped: dict[str, StoryCluster] = {}
        for cluster in clusters:
            doc_id = _validate_doc_id(cluster.cluster_id, "story_cluster")
            deduped[doc_id] = cluster

        now = datetime.now(UTC)
        client = self._get_client()
        collection = client.collection(COLLECTION_STORY_CLUSTERS)

        for chunk in _chunked(list(deduped.items()), MAX_BATCH_OPS):
            batch = client.batch()
            for doc_id, cluster in chunk:
                urls: list[str] = []
                seen_urls: set[str] = set()
                for article in cluster.articles:
                    canon = normalize_url(article.canonical_url or article.url)
                    if canon not in seen_urls:
                        seen_urls.add(canon)
                        urls.append(canon)

                payload: dict[str, Any] = {
                    "cluster_id": doc_id,
                    "canonical_title": cluster.canonical_title,
                    "topic": cluster.topic,
                    "summary": derive_cluster_summary(cluster),
                    "created_at": now,
                    "last_updated_at": now,
                }
                if urls:
                    payload["urls"] = firestore.ArrayUnion(urls)
                if cluster.embedding:
                    payload["embedding"] = [float(x) for x in cluster.embedding]
                batch.set(collection.document(doc_id), payload, merge=True)
            await batch.commit()

    async def save_run_metrics(self, metrics: RunMetrics) -> None:
        """Persist RunMetrics into runs/{run_id} with native Firestore Timestamps."""
        doc_id = _validate_doc_id(metrics.run_id, "run")
        client = self._get_client()
        await client.collection(COLLECTION_RUNS).document(doc_id).set(
            metrics.model_dump(mode="python")
        )

    async def _prune_collection(
        self,
        collection_name: str,
        timestamp_field: str,
        cutoff: datetime,
    ) -> int:
        client = self._get_client()
        query = (
            client.collection(collection_name)
            .where(filter=FieldFilter(timestamp_field, "<", cutoff))
            .select([])
        )

        deleted = 0
        batch = client.batch()
        ops_in_batch = 0

        stream = query.stream()
        try:
            async for snap in stream:
                batch.delete(snap.reference)
                ops_in_batch += 1
                deleted += 1
                if ops_in_batch >= MAX_BATCH_OPS:
                    await batch.commit()
                    batch = client.batch()
                    ops_in_batch = 0
        finally:
            await _close_async_stream(stream)

        if ops_in_batch > 0:
            await batch.commit()

        return deleted

    async def prune_expired(self, retention_days: int = 30) -> int:
        """Programmatically delete seen_articles and story_clusters older than `retention_days`."""
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        seen_deleted = await self._prune_collection(
            COLLECTION_SEEN_ARTICLES, "first_seen_at", cutoff
        )
        clusters_deleted = await self._prune_collection(
            COLLECTION_STORY_CLUSTERS, "last_updated_at", cutoff
        )
        return seen_deleted + clusters_deleted
