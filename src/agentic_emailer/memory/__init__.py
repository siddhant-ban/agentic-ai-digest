"""Persistent cross-day memory backends, embeddings, and semantic story clustering."""

from __future__ import annotations

from agentic_emailer.config import Settings
from agentic_emailer.memory.local_json import LocalJsonMemoryStore
from agentic_emailer.memory.protocol import (
    MemoryStore,
    PriorClusterRecord,
    derive_cluster_summary,
    ensure_utc,
)


def create_memory_store(settings: Settings) -> MemoryStore:
    """Instantiate the configured MemoryStore backend (`local` or `firestore`)."""
    if settings.memory_backend == "firestore":
        from agentic_emailer.memory.firestore_store import FirestoreMemoryStore

        return FirestoreMemoryStore(
            project_id=settings.gcp_project_id,
            database=settings.firestore_database,
        )
    if settings.memory_backend == "local":
        return LocalJsonMemoryStore(base_dir=settings.local_memory_dir)
    raise ValueError(f"Unsupported memory_backend: {settings.memory_backend!r}")


__all__ = [
    "LocalJsonMemoryStore",
    "MemoryStore",
    "PriorClusterRecord",
    "create_memory_store",
    "derive_cluster_summary",
    "ensure_utc",
]
