"""Local JSON-backed MemoryStore implementation for development and offline evals."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentic_emailer.ingestion.rss import normalize_url
from agentic_emailer.memory.protocol import (
    PriorClusterRecord,
    derive_cluster_summary,
    ensure_utc,
)
from agentic_emailer.models import RawArticle, RecipientProfile, RunMetrics, StoryCluster

logger = logging.getLogger(__name__)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return ensure_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


class LocalJsonMemoryStore:
    """File-backed JSON MemoryStore requiring zero cloud credentials or network access."""

    def __init__(self, base_dir: Path | str = Path(".local_memory")) -> None:
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()

    @property
    def profiles_path(self) -> Path:
        return self.base_dir / "profiles.json"

    @property
    def seen_articles_path(self) -> Path:
        return self.base_dir / "seen_articles.json"

    @property
    def story_clusters_path(self) -> Path:
        return self.base_dir / "story_clusters.json"

    @property
    def runs_path(self) -> Path:
        return self.base_dir / "runs.json"

    def close(self) -> None:
        """No-op cleanup matching FirestoreMemoryStore lifecycle."""

    async def aclose(self) -> None:
        """Async no-op cleanup matching FirestoreMemoryStore lifecycle."""
        self.close()

    def _read_mapping(self, path: Path) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse local memory file %s: %s", path, exc)
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "Expected JSON object in %s, got %s; ignoring",
                path,
                type(data).__name__,
            )
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def _write_mapping(self, path: Path, payload: dict[str, dict[str, Any]]) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.base_dir),
            prefix=f".{path.stem}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
            tmp_path.replace(path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def load_active_profiles(self) -> list[RecipientProfile]:
        """Return all enabled RecipientProfiles sorted deterministically by ID."""
        with self._lock:
            raw_map = self._read_mapping(self.profiles_path)

        profiles: list[RecipientProfile] = []
        for profile_id in sorted(raw_map):
            try:
                profile = RecipientProfile.model_validate(raw_map[profile_id])
            except Exception as exc:
                logger.warning("Skipping invalid stored profile %r: %s", profile_id, exc)
                continue
            if profile.enabled:
                profiles.append(profile)
        profiles.sort(key=lambda p: p.id)
        return profiles

    async def upsert_profiles(self, profiles: list[RecipientProfile]) -> int:
        """Idempotently upsert RecipientProfiles (including disabled ones) by ID."""
        if not profiles:
            return 0
        with self._lock:
            raw_map = self._read_mapping(self.profiles_path)
            upserted_ids: set[str] = set()
            for profile in profiles:
                raw_map[profile.id] = profile.model_dump(mode="json")
                upserted_ids.add(profile.id)
            self._write_mapping(self.profiles_path, raw_map)
        return len(upserted_ids)

    async def get_seen_urls(self, since_days: int = 14) -> set[str]:
        """Return normalized canonical URLs seen within the trailing `since_days` window."""
        if since_days < 0:
            raise ValueError("since_days must be non-negative")
        cutoff = datetime.now(UTC) - timedelta(days=since_days)

        with self._lock:
            raw_map = self._read_mapping(self.seen_articles_path)

        urls: set[str] = set()
        for record in raw_map.values():
            first_seen = _parse_iso_datetime(record.get("first_seen_at"))
            if first_seen is None or first_seen < cutoff:
                continue
            canonical = str(record.get("canonical_url") or "").strip()
            if canonical:
                urls.add(normalize_url(canonical))
        return urls

    async def get_recent_clusters(self, since_days: int = 7) -> list[PriorClusterRecord]:
        """Return persisted StoryClusters updated within the trailing `since_days` window."""
        if since_days < 0:
            raise ValueError("since_days must be non-negative")
        cutoff = datetime.now(UTC) - timedelta(days=since_days)

        with self._lock:
            raw_map = self._read_mapping(self.story_clusters_path)

        records: list[PriorClusterRecord] = []
        for cluster_id, raw in raw_map.items():
            updated_at = _parse_iso_datetime(raw.get("last_updated_at"))
            if updated_at is None or updated_at < cutoff:
                continue
            try:
                record = PriorClusterRecord(
                    cluster_id=str(raw.get("cluster_id") or cluster_id),
                    canonical_title=str(raw.get("canonical_title") or ""),
                    topic=str(raw.get("topic") or "General"),
                    summary=str(raw.get("summary") or ""),
                    urls=[str(u) for u in (raw.get("urls") or []) if str(u).strip()],
                    embedding=[float(x) for x in (raw.get("embedding") or [])],
                    last_updated_at=updated_at,
                )
            except Exception as exc:
                logger.warning("Skipping invalid stored cluster %r: %s", cluster_id, exc)
                continue
            records.append(record)

        records.sort(key=lambda r: (-r.last_updated_at.timestamp(), r.cluster_id))
        return records

    async def record_sent_articles(
        self,
        articles: list[RawArticle],
        recipient_id: str,
        cluster_ids: dict[str, str],
    ) -> None:
        """Persist sent articles with recipient tracking while preserving earliest first_seen_at."""
        if not articles:
            return
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        clean_recipient = recipient_id.strip()

        with self._lock:
            raw_map = self._read_mapping(self.seen_articles_path)
            for article in articles:
                existing = raw_map.get(article.article_id)
                recipients: list[str] = []
                if existing and isinstance(existing.get("recipient_ids_sent"), list):
                    recipients = [
                        str(r).strip()
                        for r in existing["recipient_ids_sent"]
                        if str(r).strip()
                    ]
                if clean_recipient and clean_recipient not in recipients:
                    recipients.append(clean_recipient)

                existing_first_seen = (
                    _parse_iso_datetime(existing.get("first_seen_at")) if existing else None
                )
                # Preserve earliest timestamp within the same run/day across recipients, but
                # refresh if a previously expired evergreen article is sent again on a later day.
                if (
                    existing_first_seen is not None
                    and (now - existing_first_seen) <= timedelta(days=1)
                ):
                    first_seen = existing_first_seen.isoformat()
                else:
                    first_seen = now_iso

                cluster_id = (
                    (cluster_ids.get(article.article_id) or "").strip()
                    or (str(existing.get("cluster_id") or "").strip() if existing else "")
                )
                raw_map[article.article_id] = {
                    "article_id": article.article_id,
                    "canonical_url": normalize_url(article.canonical_url or article.url),
                    "title": article.title,
                    "source": article.source,
                    "first_seen_at": first_seen,
                    "recipient_ids_sent": recipients,
                    "cluster_id": cluster_id,
                }
            self._write_mapping(self.seen_articles_path, raw_map)

    async def save_story_clusters(self, clusters: list[StoryCluster]) -> None:
        """Upsert story clusters with embeddings and summaries for cross-day linking."""
        if not clusters:
            return
        now_iso = datetime.now(UTC).isoformat()

        with self._lock:
            raw_map = self._read_mapping(self.story_clusters_path)
            for cluster in clusters:
                existing = raw_map.get(cluster.cluster_id)
                existing_urls = (
                    [str(u) for u in existing.get("urls", []) if str(u).strip()]
                    if existing and isinstance(existing.get("urls"), list)
                    else []
                )
                merged_urls = list(existing_urls)
                seen_url_set = set(existing_urls)
                for article in cluster.articles:
                    canon = normalize_url(article.canonical_url or article.url)
                    if canon not in seen_url_set:
                        seen_url_set.add(canon)
                        merged_urls.append(canon)

                embedding: list[float]
                if cluster.embedding:
                    embedding = [float(x) for x in cluster.embedding]
                elif existing and isinstance(existing.get("embedding"), list):
                    embedding = [float(x) for x in existing["embedding"]]
                else:
                    embedding = []

                existing_summary = (
                    str(existing.get("summary") or "").strip() if existing else ""
                )
                if cluster.summary and cluster.summary.strip():
                    summary = derive_cluster_summary(cluster)
                elif existing_summary:
                    summary = existing_summary
                else:
                    summary = derive_cluster_summary(cluster)

                created_at = (
                    existing.get("created_at")
                    if existing and existing.get("created_at")
                    else now_iso
                )
                raw_map[cluster.cluster_id] = {
                    "cluster_id": cluster.cluster_id,
                    "canonical_title": cluster.canonical_title,
                    "topic": cluster.topic,
                    "summary": summary,
                    "urls": merged_urls,
                    "embedding": embedding,
                    "created_at": created_at,
                    "last_updated_at": now_iso,
                }
            self._write_mapping(self.story_clusters_path, raw_map)

    async def save_run_metrics(self, metrics: RunMetrics) -> None:
        """Persist RunMetrics keyed by run_id."""
        with self._lock:
            raw_map = self._read_mapping(self.runs_path)
            raw_map[metrics.run_id] = metrics.model_dump(mode="json")
            self._write_mapping(self.runs_path, raw_map)

    async def list_recent_runs(self, limit: int = 10) -> list[RunMetrics]:
        """Return up to `limit` most recent RunMetrics ordered newest-first."""
        if limit <= 0:
            return []
        with self._lock:
            raw_map = self._read_mapping(self.runs_path)

        runs: list[RunMetrics] = []
        for run_id, raw in raw_map.items():
            try:
                runs.append(RunMetrics.model_validate(raw))
            except Exception as exc:
                logger.warning("Skipping invalid stored run %r: %s", run_id, exc)
        runs.sort(key=lambda r: (-ensure_utc(r.started_at).timestamp(), r.run_id))
        return runs[:limit]

    async def prune_expired(self, retention_days: int = 30) -> int:
        """Delete seen_articles and story_clusters entries older than `retention_days`."""
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        total_deleted = 0

        with self._lock:
            seen_map = self._read_mapping(self.seen_articles_path)
            kept_seen: dict[str, dict[str, Any]] = {}
            deleted_seen = 0
            for key, record in seen_map.items():
                first_seen = _parse_iso_datetime(record.get("first_seen_at"))
                if first_seen is not None and first_seen < cutoff:
                    deleted_seen += 1
                else:
                    kept_seen[key] = record
            if deleted_seen:
                self._write_mapping(self.seen_articles_path, kept_seen)
                total_deleted += deleted_seen

            cluster_map = self._read_mapping(self.story_clusters_path)
            kept_clusters: dict[str, dict[str, Any]] = {}
            deleted_clusters = 0
            for key, record in cluster_map.items():
                updated_at = _parse_iso_datetime(record.get("last_updated_at"))
                if updated_at is not None and updated_at < cutoff:
                    deleted_clusters += 1
                else:
                    kept_clusters[key] = record
            if deleted_clusters:
                self._write_mapping(self.story_clusters_path, kept_clusters)
                total_deleted += deleted_clusters

        return total_deleted
