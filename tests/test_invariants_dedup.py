"""Thin deterministic invariants for cosine clustering, cross-day dedup, and local memory."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from agentic_emailer.ingestion.rss import make_article_id, normalize_url
from agentic_emailer.memory.clustering import cluster_and_link_stories
from agentic_emailer.memory.local_json import LocalJsonMemoryStore
from agentic_emailer.memory.protocol import PriorClusterRecord
from agentic_emailer.models import RawArticle, RecipientProfile, RunMetrics, StoryCluster


def _unit(values: list[float]) -> list[float]:
    vec = np.asarray(values, dtype=np.float64)
    return (vec / float(np.linalg.norm(vec))).tolist()


def _make_article(
    url: str,
    title: str,
    *,
    published_at: datetime | None = None,
    topic: str = "AI",
    source: str = "Test Source",
) -> RawArticle:
    canonical = normalize_url(url)
    return RawArticle(
        article_id=make_article_id(canonical),
        canonical_url=canonical,
        url=url,
        title=title,
        source=source,
        published_at=published_at,
        snippet=f"Summary for {title}.",
        topic_hints=[topic],
    )


def test_same_day_clustering_merges_near_vectors_and_separates_orthogonal_topics() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    art_new = _make_article(
        "https://techcrunch.com/gemini-launch",
        "Google debuts Gemini 3.1 Flash-Lite",
        published_at=now,
        topic="LLM releases",
        source="TechCrunch",
    )
    art_mid = _make_article(
        "https://venturebeat.com/gemini-launch",
        "Google launches Gemini 3.1 Flash-Lite",
        published_at=now - timedelta(hours=1),
        topic="LLM releases",
        source="VentureBeat",
    )
    art_old = _make_article(
        "https://theverge.com/gemini-launch",
        "Google rolls out Gemini 3.1 Flash-Lite",
        published_at=now - timedelta(hours=2),
        topic="LLM releases",
        source="The Verge",
    )
    art_energy = _make_article(
        "https://canarymedia.com/iron-air-battery",
        "Form Energy commissions iron-air battery",
        published_at=now - timedelta(minutes=30),
        topic="Clean energy",
        source="Canary Media",
    )
    art_robotics = _make_article(
        "https://ieee.org/atlas-pilot",
        "Boston Dynamics deploys Atlas in factory pilot",
        published_at=now - timedelta(minutes=45),
        topic="Robotics",
        source="IEEE Spectrum",
    )

    # Deliberately pass unsorted articles; clustering must sort newest-first deterministically.
    articles = [art_old, art_energy, art_new, art_robotics, art_mid]
    embeddings = [
        _unit([0.95, 0.31, 0.0, 0.0]),  # near [1, 0, 0, 0] (sim ~0.95)
        _unit([0.0, 0.0, 1.0, 0.0]),    # orthogonal topic 1
        _unit([1.0, 0.0, 0.0, 0.0]),    # lead article (newest)
        _unit([0.0, 0.0, 0.0, 1.0]),    # orthogonal topic 2
        _unit([0.96, -0.28, 0.0, 0.0]), # near [1, 0, 0, 0] (sim ~0.96)
    ]

    clusters, deduped = cluster_and_link_stories(
        articles,
        embeddings,
        seen_urls=set(),
        prior_clusters=[],
        merge_threshold=0.84,
    )

    assert deduped == 0
    assert len(clusters) == 3

    merged = next(c for c in clusters if len(c.articles) == 3)
    assert merged.canonical_title == art_new.title
    assert [a.article_id for a in merged.articles] == [
        art_new.article_id,
        art_mid.article_id,
        art_old.article_id,
    ]
    assert merged.embedding is not None
    assert float(np.linalg.norm(merged.embedding)) == pytest.approx(1.0, abs=1e-6)

    # Edge cases: empty input returns empty, mismatched lengths raise ValueError
    assert cluster_and_link_stories([], [], set(), []) == ([], 0)
    with pytest.raises(ValueError, match="equal length"):
        cluster_and_link_stories([art_new], [], set(), [])

    # Same-day duplicate canonical URLs coalesce while preserving full_text and merged topic_hints
    dup_thin = _make_article(
        "https://example.com/dup-story",
        "Duplicate Story",
        published_at=now,
        topic="AI",
    )
    dup_rich = _make_article(
        "https://example.com/dup-story?utm_source=feed",
        "Duplicate Story",
        published_at=now - timedelta(minutes=5),
        topic="Cloud",
    ).model_copy(update={"full_text": "Full extracted body text."})
    dup_clusters, dup_deduped = cluster_and_link_stories(
        [dup_thin, dup_rich],
        [_unit([1.0, 0.0, 0.0, 0.0]), _unit([1.0, 0.0, 0.0, 0.0])],
        seen_urls=set(),
        prior_clusters=[],
    )
    assert dup_deduped == 0
    assert len(dup_clusters) == 1
    assert dup_clusters[0].articles[0].full_text == "Full extracted body text."
    assert set(dup_clusters[0].articles[0].topic_hints) == {"AI", "Cloud"}


def test_cross_day_thresholds_suppress_rehash_and_link_developing_story() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    prior_date = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)

    seen_url = "https://arstechnica.com/already-sent-story"
    art_exact_seen = _make_article(
        f"{seen_url}?utm_source=rss",
        "Already Sent Story",
        published_at=now,
    )
    art_rehash = _make_article(
        "https://infoworld.com/mistral-governance-rehash",
        "Mistral Enterprise Hub governance rehash",
        published_at=now - timedelta(minutes=10),
    )
    art_developing = _make_article(
        "https://reuters.com/eu-ai-office-followup",
        "FrontierAI publishes transparency dossier for EU AI Office",
        published_at=now - timedelta(minutes=20),
    )
    art_fresh = _make_article(
        "https://nature.com/quantum-error-threshold",
        "Quantum processor cuts surface-code error rate",
        published_at=now - timedelta(minutes=30),
    )

    prior_rehash = PriorClusterRecord(
        cluster_id="prior-rehash",
        canonical_title="Mistral launches Enterprise Hub",
        topic="AI",
        summary="Mistral introduced an on-premise governance suite for regulated firms.",
        urls=["https://example.com/prior-mistral"],
        embedding=_unit([1.0, 0.0, 0.0, 0.0]),
        last_updated_at=prior_date,
    )
    prior_developing = PriorClusterRecord(
        cluster_id="prior-dev",
        canonical_title="EU AI Office opens inquiry into FrontierAI",
        topic="AI",
        summary="EU regulators opened a compliance inquiry over missing training-data summaries.",
        urls=["https://example.com/prior-eu-inquiry"],
        embedding=_unit([0.0, 1.0, 0.0, 0.0]),
        last_updated_at=prior_date,
    )

    articles = [art_exact_seen, art_rehash, art_developing, art_fresh]
    embeddings = [
        _unit([0.0, 0.0, 1.0, 0.0]),    # dropped in Step 1 via exact seen_urls
        _unit([0.98, 0.20, 0.0, 0.0]),  # cos sim ~0.980 >= 0.94 -> suppressed as re-hash
        _unit([0.0, 0.85, 0.5268, 0.0]),  # cos sim ~0.850 in [0.76, 0.94) -> developing story
        _unit([0.0, 0.0, 0.0, 1.0]),    # cos sim 0.0 < 0.76 -> brand-new story
    ]

    clusters, deduped = cluster_and_link_stories(
        articles,
        embeddings,
        seen_urls={seen_url},
        prior_clusters=[prior_rehash, prior_developing],
        merge_threshold=0.84,
        storyline_threshold=0.76,
        semantic_dedup_threshold=0.94,
    )

    assert deduped == 2  # 1 exact seen URL + 1 semantic cross-day re-hash
    assert len(clusters) == 2

    by_title = {c.canonical_title: c for c in clusters}
    assert art_rehash.title not in by_title
    assert art_exact_seen.title not in by_title

    dev_cluster = by_title[art_developing.title]
    assert dev_cluster.is_developing_story is True
    assert dev_cluster.prior_story_summary == prior_developing.summary
    assert dev_cluster.prior_digest_date == "2026-11-24"

    fresh_cluster = by_title[art_fresh.title]
    assert fresh_cluster.is_developing_story is False
    assert fresh_cluster.prior_story_summary is None
    assert fresh_cluster.prior_digest_date is None

    # Duplicate copies of the same already-seen URL must only increment deduped count once
    art_exact_seen_dup = _make_article(
        f"{seen_url}?ref=homepage",
        "Already Sent Story (dup copy)",
        published_at=now - timedelta(minutes=1),
    )
    _, deduped_dup = cluster_and_link_stories(
        [art_exact_seen, art_exact_seen_dup],
        [_unit([0.0, 0.0, 1.0, 0.0]), _unit([0.0, 0.0, 1.0, 0.0])],
        seen_urls={seen_url},
        prior_clusters=[],
    )
    assert deduped_dup == 1


def test_local_json_memory_store_roundtrip_and_pruning(tmp_path: Path) -> None:
    async def _exercise() -> None:
        store = LocalJsonMemoryStore(tmp_path / "memory")

        alice = RecipientProfile(
            id="alice",
            email_address="alice@example.com",
            enabled=True,
            topics=["AI agents"],
        )
        bob = RecipientProfile(
            id="bob",
            email_address="bob@example.com",
            enabled=False,
            topics=["Robotics"],
        )
        assert await store.upsert_profiles([alice, bob]) == 2
        active = await store.load_active_profiles()
        assert [p.id for p in active] == ["alice"]

        fresh_article = _make_article(
            "https://example.com/fresh-story?utm_source=feed",
            "Fresh Story",
            published_at=datetime.now(UTC),
        )
        await store.record_sent_articles(
            [fresh_article],
            recipient_id="alice",
            cluster_ids={fresh_article.article_id: "cluster-fresh"},
        )
        await store.record_sent_articles(
            [fresh_article],
            recipient_id="carol",
            cluster_ids={fresh_article.article_id: "cluster-fresh"},
        )

        seen_urls = await store.get_seen_urls(since_days=14)
        assert "https://example.com/fresh-story" in seen_urls

        seen_raw = json.loads(store.seen_articles_path.read_text(encoding="utf-8"))
        assert seen_raw[fresh_article.article_id]["recipient_ids_sent"] == ["alice", "carol"]

        fresh_cluster = StoryCluster(
            cluster_id="cluster-fresh",
            canonical_title="Fresh Story",
            topic="AI agents",
            articles=[fresh_article],
            summary="Concise summary of the fresh story.",
            embedding=_unit([1.0, 0.0, 0.0, 0.0]),
        )
        await store.save_story_clusters([fresh_cluster])
        # Updating a cluster without embedding/summary must preserve stored embedding and summary
        await store.save_story_clusters(
            [
                StoryCluster(
                    cluster_id="cluster-fresh",
                    canonical_title="Fresh Story",
                    topic="AI agents",
                    articles=[fresh_article],
                    summary=None,
                    embedding=[],
                )
            ]
        )
        recent_clusters = await store.get_recent_clusters(since_days=7)
        assert len(recent_clusters) == 1
        assert recent_clusters[0].cluster_id == "cluster-fresh"
        assert recent_clusters[0].summary == "Concise summary of the fresh story."
        assert len(recent_clusters[0].embedding) == 4

        now = datetime.now(UTC)
        await store.save_run_metrics(
            RunMetrics(run_id="run-001", started_at=now, finished_at=now, clusters_formed=1)
        )
        assert "run-001" in json.loads(store.runs_path.read_text(encoding="utf-8"))

        # Inject expired (>30 days old) seen article and cluster directly to verify window + pruning
        expired_iso = (now - timedelta(days=45)).isoformat()
        seen_raw["expired-art"] = {
            "article_id": "expired-art",
            "canonical_url": "https://example.com/expired-story",
            "title": "Expired Story",
            "source": "Archive",
            "first_seen_at": expired_iso,
            "recipient_ids_sent": ["alice"],
            "cluster_id": "cluster-expired",
        }
        store.seen_articles_path.write_text(json.dumps(seen_raw), encoding="utf-8")

        clusters_raw = json.loads(store.story_clusters_path.read_text(encoding="utf-8"))
        clusters_raw["cluster-expired"] = {
            "cluster_id": "cluster-expired",
            "canonical_title": "Expired Cluster",
            "topic": "AI",
            "summary": "Old cluster",
            "urls": ["https://example.com/expired-story"],
            "embedding": _unit([0.0, 1.0, 0.0, 0.0]),
            "created_at": expired_iso,
            "last_updated_at": expired_iso,
        }
        store.story_clusters_path.write_text(json.dumps(clusters_raw), encoding="utf-8")

        # Windowed reads exclude expired entries before pruning
        assert "https://example.com/expired-story" not in await store.get_seen_urls(since_days=14)
        assert [c.cluster_id for c in await store.get_recent_clusters(since_days=7)] == [
            "cluster-fresh"
        ]

        # Programmatic 30-day pruning removes both expired docs and preserves fresh docs
        pruned = await store.prune_expired(retention_days=30)
        assert pruned == 2
        assert "expired-art" not in json.loads(store.seen_articles_path.read_text(encoding="utf-8"))
        assert "cluster-expired" not in json.loads(
            store.story_clusters_path.read_text(encoding="utf-8")
        )
        assert "https://example.com/fresh-story" in await store.get_seen_urls(since_days=14)
        assert len(await store.get_recent_clusters(since_days=7)) == 1

        # Re-sending an evergreen article older than 14 days refreshes its 14-day suppression window
        evergreen = _make_article("https://example.com/evergreen", "Evergreen Guide")
        seen_now = json.loads(store.seen_articles_path.read_text(encoding="utf-8"))
        seen_now[evergreen.article_id] = {
            "article_id": evergreen.article_id,
            "canonical_url": evergreen.canonical_url,
            "title": evergreen.title,
            "source": evergreen.source,
            "first_seen_at": (now - timedelta(days=15)).isoformat(),
            "recipient_ids_sent": ["alice"],
            "cluster_id": "cluster-evergreen",
        }
        store.seen_articles_path.write_text(json.dumps(seen_now), encoding="utf-8")
        assert evergreen.canonical_url not in await store.get_seen_urls(since_days=14)
        await store.record_sent_articles([evergreen], "alice", {evergreen.article_id: "c-new"})
        assert evergreen.canonical_url in await store.get_seen_urls(since_days=14)
        await store.aclose()

    asyncio.run(_exercise())
