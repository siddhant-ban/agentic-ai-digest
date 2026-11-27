"""End-to-end async digest orchestrator with bounded editorial loop and recipient isolation."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from agentic_emailer.agents.critic_agent import run_critic
from agentic_emailer.agents.research_agent import run_research_stage
from agentic_emailer.agents.synthesis_agent import run_synthesis
from agentic_emailer.agents.verifier import sanitize_draft_grounding, verify_digest_draft
from agentic_emailer.budget import BudgetExceededError, RunBudgetTracker
from agentic_emailer.config import Settings
from agentic_emailer.delivery.protocol import EmailSender
from agentic_emailer.delivery.renderer import (
    build_fallback_draft,
    render_html,
    render_plain_text,
    render_subject,
)
from agentic_emailer.delivery.smtp_sender import SmtpEmailSender
from agentic_emailer.ingestion.rss import (
    fetch_rss_feeds,
    normalize_seen_urls,
    normalize_url,
    within_lookback,
)
from agentic_emailer.memory import (
    MemoryStore,
    build_article_embedding_text,
    cluster_and_link_stories,
    create_memory_store,
    embed_texts,
)
from agentic_emailer.models import (
    CriticVerdict,
    DeliveryReceipt,
    DigestDraft,
    RawArticle,
    RecipientProfile,
    RssFeedConfig,
    RunMetrics,
    StoryCluster,
    VerificationResult,
)
from agentic_emailer.observability import (
    TelemetryHandle,
    get_current_trace_id,
    get_tracer,
    init_telemetry,
    log_context,
)

logger = logging.getLogger(__name__)

MAX_CLUSTERS_PER_RECIPIENT = 30


@dataclass
class EditorialResult:
    """Outcome of the bounded Synthesis -> Verifier -> Critic editorial loop."""

    draft: DigestDraft
    revisions: int
    verification: VerificationResult
    critic_verdict: CriticVerdict | None = None
    used_fallback: bool = False


def safe_print(text: str) -> None:
    """Print text safely even on narrow Windows console code pages (e.g., cp1252)."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
        encoded = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(encoded)


def load_json_config(config_path: Path | None) -> dict[str, Any]:
    """Load an optional JSON configuration file."""
    if config_path is None or not config_path.exists():
        return {}
    with config_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {config_path}, got {type(data).__name__}")
    return data


def _parse_rss_feeds(raw_feeds: Any) -> list[RssFeedConfig]:
    feeds: list[RssFeedConfig] = []
    if not isinstance(raw_feeds, list):
        return feeds
    for item in raw_feeds:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        name = str(item.get("name") or url).strip()
        try:
            feeds.append(RssFeedConfig(name=name, url=url))
        except Exception as exc:
            logger.warning("Skipping invalid RSS feed entry %r: %s", item, exc)
    return feeds


def _parse_recipient_dict(
    raw: dict[str, Any],
    global_config: dict[str, Any],
    default_id: str,
    *,
    include_disabled: bool = False,
) -> RecipientProfile | None:
    is_enabled = raw.get("enabled") is not False
    if not is_enabled and not include_disabled:
        logger.info("Skipping disabled recipient profile: %s", raw.get("id") or default_id)
        return None

    profile_id = str(raw.get("id") or default_id).strip()
    email_field = raw.get("email")
    email_dict = email_field if isinstance(email_field, dict) else {}
    global_email = global_config.get("email")
    global_email_dict = global_email if isinstance(global_email, dict) else {}

    recipient_email = (
        raw.get("email_address")
        or raw.get("recipient_email")
        or (email_field if isinstance(email_field, str) else None)
        or email_dict.get("recipient")
    )
    if not recipient_email:
        logger.warning("Recipient profile %s is missing an email address", profile_id)
        return None

    raw_topics = raw.get("topics") if raw.get("topics") is not None else global_config.get("topics")
    if isinstance(raw_topics, str):
        topics_list = [raw_topics.strip()] if raw_topics.strip() else []
    elif isinstance(raw_topics, list):
        topics_list = [str(t).strip() for t in raw_topics if t is not None and str(t).strip()]
    else:
        topics_list = []

    rss_feeds = _parse_rss_feeds(raw.get("rss_feeds") or global_config.get("rss_feeds") or [])
    subject_prefix = (
        raw.get("subject_prefix")
        or email_dict.get("subject_prefix")
        or global_email_dict.get("subject_prefix")
        or "[Daily Digest]"
    )
    raw_lookback = (
        raw.get("lookback_hours")
        if raw.get("lookback_hours") is not None
        else global_config.get("lookback_hours")
    )
    raw_max_highlights = (
        raw.get("max_highlights")
        if raw.get("max_highlights") is not None
        else global_config.get("max_highlights")
    )

    try:
        lookback_hours = int(raw_lookback) if raw_lookback is not None else 36
        max_highlights = int(raw_max_highlights) if raw_max_highlights is not None else 8
        return RecipientProfile(
            id=profile_id,
            email_address=str(recipient_email),
            subject_prefix=str(subject_prefix),
            enabled=is_enabled,
            topics=topics_list,
            rss_feeds=rss_feeds,
            lookback_hours=lookback_hours,
            max_highlights=max_highlights,
        )
    except Exception as exc:
        logger.warning("Invalid recipient profile %s: %s", profile_id, exc)
        return None


def _filter_profiles_by_recipient(
    profiles: list[RecipientProfile],
    recipient_filter: str | None,
) -> list[RecipientProfile]:
    if not recipient_filter:
        return list(profiles)
    target = recipient_filter.strip().lower()
    return [
        p
        for p in profiles
        if p.id.lower() == target or p.email_address.lower() == target
    ]


def load_profiles_from_file(
    profiles_path: Path,
    global_config: dict[str, Any] | None = None,
    *,
    include_disabled: bool = False,
) -> list[RecipientProfile]:
    """Load and validate RecipientProfiles from a JSON file (list or single object)."""
    data = json.loads(profiles_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [data]
    else:
        raise ValueError(
            f"Expected JSON list or object in {profiles_path}, got {type(data).__name__}"
        )

    cfg = global_config or {}
    profiles: list[RecipientProfile] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            logger.warning("Skipping non-object profile entry #%d in %s", idx, profiles_path)
            continue
        profile = _parse_recipient_dict(
            item,
            cfg,
            f"profile_{idx}",
            include_disabled=include_disabled,
        )
        if profile is not None:
            profiles.append(profile)
    return profiles


def discover_recipient_profiles(
    config_path: Path | None,
    global_config: dict[str, Any],
    recipient_filter: str | None = None,
) -> list[RecipientProfile]:
    """Load active RecipientProfiles from RECIPIENTS_JSON, recipients.json, or config fallback."""
    profiles: list[RecipientProfile] = []
    source_loaded = False

    # 1. RECIPIENTS_JSON environment variable
    env_recipients = os.environ.get("RECIPIENTS_JSON", "").strip()
    if env_recipients:
        try:
            parsed = json.loads(env_recipients)
            items = parsed if isinstance(parsed, list) else [parsed]
            source_loaded = True
            for idx, item in enumerate(items, start=1):
                if isinstance(item, dict):
                    profile = _parse_recipient_dict(item, global_config, f"env_profile_{idx}")
                    if profile is not None:
                        profiles.append(profile)
        except Exception as exc:
            logger.warning("Failed to parse RECIPIENTS_JSON env var: %s", exc)

    # 2. Local recipients.json file next to config_path (or CWD)
    if not source_loaded:
        raw_candidates: list[Path] = []
        if config_path is not None:
            raw_candidates.append(config_path.resolve().parent / "recipients.json")
        raw_candidates.append(Path.cwd() / "recipients.json")

        candidates: list[Path] = []
        seen_paths: set[Path] = set()
        for candidate in raw_candidates:
            resolved = candidate.resolve()
            if resolved not in seen_paths:
                seen_paths.add(resolved)
                candidates.append(resolved)

        for recipients_file in candidates:
            if recipients_file.exists() and recipients_file.is_file():
                try:
                    data = json.loads(recipients_file.read_text(encoding="utf-8"))
                    items = data if isinstance(data, list) else [data]
                    source_loaded = True
                    for idx, item in enumerate(items, start=1):
                        if isinstance(item, dict):
                            profile = _parse_recipient_dict(item, global_config, f"profile_{idx}")
                            if profile is not None:
                                profiles.append(profile)
                    break
                except Exception as exc:
                    logger.warning("Could not parse %s: %s", recipients_file, exc)

    # 3. Single-recipient fallback in global_config (e.g. config.json / config.example.json)
    if not source_loaded and global_config:
        email_section = global_config.get("email")
        email_dict = email_section if isinstance(email_section, dict) else {}
        fallback_email = email_dict.get("recipient")
        if fallback_email or global_config.get("topics") or global_config.get("rss_feeds"):
            synthetic = {
                "id": "default",
                "email_address": fallback_email or "default@example.com",
                "topics": global_config.get("topics", []),
                "rss_feeds": global_config.get("rss_feeds", []),
                "subject_prefix": email_dict.get("subject_prefix", "[Daily Digest]"),
                "lookback_hours": global_config.get("lookback_hours", 36),
                "max_highlights": global_config.get("max_highlights", 8),
            }
            profile = _parse_recipient_dict(synthetic, global_config, "default")
            if profile is not None:
                profiles.append(profile)

    return _filter_profiles_by_recipient(profiles, recipient_filter)


def build_singleton_clusters(articles: list[RawArticle]) -> list[StoryCluster]:
    """Wrap each unique normalized article into a 1-to-1 StoryCluster (Milestone 1 hook)."""
    clusters: list[StoryCluster] = []
    seen_canonical: set[str] = set()
    for article in articles:
        if article.canonical_url in seen_canonical:
            continue
        seen_canonical.add(article.canonical_url)
        topic = next(
            (hint.strip() for hint in article.topic_hints if hint and hint.strip()),
            "General",
        )
        clusters.append(
            StoryCluster(
                cluster_id=f"cluster-{article.article_id}",
                canonical_title=article.title,
                topic=topic,
                articles=[article],
            )
        )
    return clusters


TOPIC_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "latest",
        "new",
        "news",
        "of",
        "on",
        "or",
        "the",
        "to",
        "updates",
        "use",
        "with",
    }
)
SHORT_DOMAIN_TOKENS = frozenset({"ai", "ml", "vr", "ar", "ev", "5g", "ui", "ux", "ip", "uk", "us"})


def _tokenize_text(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _significant_topic_words(topic: str) -> list[str]:
    words: list[str] = []
    for word in re.findall(r"[a-z0-9]+", topic.lower()):
        if word in TOPIC_STOPWORDS:
            continue
        if len(word) >= 3 or word in SHORT_DOMAIN_TOKENS:
            words.append(word)
    return words


def _article_matches_topics(article: RawArticle, topics: list[str]) -> bool:
    if not topics:
        return True
    topic_set = {t.strip().lower() for t in topics if t.strip()}
    if any(hint.strip().lower() in topic_set for hint in article.topic_hints):
        return True

    haystack = " ".join(
        part.lower()
        for part in (
            article.title,
            article.snippet,
            (article.full_text or "")[:600],
            article.source,
            " ".join(article.topic_hints),
        )
    )
    haystack_tokens = _tokenize_text(haystack)

    for topic in topics:
        topic_lower = topic.strip().lower()
        if not topic_lower:
            continue
        if topic_lower in haystack:
            return True
        sig_words = _significant_topic_words(topic_lower)
        if not sig_words:
            continue
        hits = sum(1 for word in sig_words if word in haystack_tokens)
        required = 1 if len(sig_words) == 1 else max(2, (len(sig_words) + 1) // 2)
        if hits >= required:
            return True
    return False


def filter_clusters_for_recipient(
    clusters: list[StoryCluster],
    profile: RecipientProfile,
    *,
    feed_url_to_source: dict[str, str] | None = None,
) -> list[StoryCluster]:
    """Select clusters whose articles match the recipient's lookback window, feeds, or topics."""
    feed_names = {f.name.strip().lower() for f in profile.rss_feeds}
    if feed_url_to_source:
        for feed in profile.rss_feeds:
            pooled_source = feed_url_to_source.get(normalize_url(feed.url))
            if pooled_source:
                feed_names.add(pooled_source.strip().lower())

    selected: list[StoryCluster] = []
    for cluster in clusters:
        in_window = [
            article
            for article in cluster.articles
            if within_lookback(article.published_at, profile.lookback_hours)
        ]
        if not in_window:
            continue

        matched = False
        for article in in_window:
            if article.source.strip().lower() in feed_names:
                matched = True
                break
            if _article_matches_topics(article, profile.topics):
                matched = True
                break
        if matched:
            if len(in_window) == len(cluster.articles):
                selected.append(cluster)
            else:
                selected.append(cluster.model_copy(update={"articles": in_window}))
        if len(selected) >= MAX_CLUSTERS_PER_RECIPIENT:
            break
    return selected


def _format_articles_for_console(articles: list[RawArticle]) -> str:
    lines = [f"Found {len(articles)} normalized article(s):\n"]
    for idx, article in enumerate(articles, start=1):
        date_str = (
            article.published_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
            if article.published_at
            else "unknown date"
        )
        preview = article.snippet[:200] + ("..." if len(article.snippet) > 200 else "")
        lines.extend(
            [
                f"{idx}. [{article.article_id}] {article.title}",
                f"   Source: {article.source} ({article.discovered_via})",
                f"   Date:   {date_str}",
                f"   URL:    {article.url}",
                f"   Snippet: {preview}",
                "",
            ]
        )
    return "\n".join(lines)


async def run_editorial_loop(
    profile: RecipientProfile,
    clusters: list[StoryCluster],
    seen_urls: set[str],
    settings: Settings,
    budget: RunBudgetTracker,
    *,
    enable_critic: bool = True,
) -> EditorialResult:
    """Execute the bounded Synthesis -> Deterministic Verifier -> Critic editorial loop."""
    tracer = get_tracer()
    allowed_articles = [article for cluster in clusters for article in cluster.articles]
    normalized_seen = normalize_seen_urls(seen_urls)
    unseen_articles = [
        article
        for article in allowed_articles
        if article.canonical_url not in normalized_seen
        and normalize_url(article.url) not in normalized_seen
        and article.url not in normalized_seen
    ]

    if not unseen_articles:
        empty_draft = build_fallback_draft(
            [], profile.topics, max_highlights=profile.max_highlights
        )
        with tracer.start_as_current_span("verifier.deterministic") as ver_span:
            ver_span.set_attribute("recipient_id", profile.id)
            ver_span.set_attribute("attempt", 0)
            verification = verify_digest_draft(
                empty_draft,
                [],
                seen_urls,
                profile.topics,
                max_highlights=profile.max_highlights,
            )
            ver_span.set_attribute("passed", verification.passed)
            ver_span.set_attribute("error_count", len(verification.errors))
        return EditorialResult(
            draft=empty_draft,
            revisions=0,
            verification=verification,
            critic_verdict=None,
            used_fallback=True,
        )

    max_revisions = settings.max_critic_revisions if enable_critic else 0
    revision_feedback: str | None = None
    best_verified_draft: DigestDraft | None = None
    best_verified_verdict: CriticVerdict | None = None
    best_verified_score: tuple[int, int] | None = None
    last_draft: DigestDraft | None = None
    last_verdict: CriticVerdict | None = None
    revisions_used = 0

    for attempt in range(max_revisions + 1):
        if not budget.can_call_llm():
            logger.warning(
                "LLM budget exhausted before synthesis attempt %d for %s", attempt, profile.id
            )
            break

        if attempt > 0:
            revisions_used = attempt

        try:
            with tracer.start_as_current_span("agent.synthesis") as syn_span:
                syn_span.set_attribute("recipient_id", profile.id)
                syn_span.set_attribute("attempt", attempt + 1)
                syn_span.set_attribute("model", settings.synthesis_model)
                draft, _ = await run_synthesis(
                    clusters,
                    profile.topics,
                    settings,
                    budget,
                    max_highlights=profile.max_highlights,
                    seen_urls=seen_urls,
                    revision_feedback=revision_feedback,
                )
                syn_span.set_attribute("highlight_count", len(draft.highlights))
            last_draft = draft
        except BudgetExceededError as exc:
            logger.warning("Synthesis hit budget limit for %s: %s", profile.id, exc)
            break
        except Exception as exc:
            logger.warning(
                "Synthesis attempt %d failed for %s: %s", attempt + 1, profile.id, exc
            )
            revision_feedback = f"Previous synthesis failed with error: {exc}"
            continue

        with tracer.start_as_current_span("verifier.deterministic") as ver_span:
            ver_span.set_attribute("recipient_id", profile.id)
            ver_span.set_attribute("attempt", attempt + 1)
            verification = verify_digest_draft(
                draft,
                allowed_articles,
                seen_urls,
                profile.topics,
                max_highlights=profile.max_highlights,
            )
            ver_span.set_attribute("passed", verification.passed)
            ver_span.set_attribute("error_count", len(verification.errors))

        if not verification.passed:
            logger.info(
                "Deterministic verifier rejected draft (attempt %d) for %s: %s",
                attempt + 1,
                profile.id,
                "; ".join(verification.errors[:3]),
            )
            error_bullets = "\n".join(f"- {err}" for err in verification.errors)
            revision_feedback = (
                "Deterministic grounding verification failed (do NOT repeat these errors):\n"
                f"{error_bullets}"
            )
            continue

        if not enable_critic:
            best_verified_draft = draft
            best_verified_verdict = None
            break

        # Short-circuit Critic when budget is exhausted after deterministic check passes
        if not budget.can_call_llm():
            best_verified_draft = draft
            best_verified_verdict = None
            logger.warning(
                "Skipping Critic for %s because LLM budget is exhausted; using verified draft",
                profile.id,
            )
            break

        try:
            with tracer.start_as_current_span("agent.critic") as crit_span:
                crit_span.set_attribute("recipient_id", profile.id)
                crit_span.set_attribute("attempt", attempt + 1)
                crit_span.set_attribute("model", settings.critic_model)
                verdict, _ = await run_critic(
                    draft, clusters, profile.topics, settings, budget
                )
                crit_span.set_attribute("approved", verdict.approved)
                crit_span.set_attribute("faithfulness_score", verdict.faithfulness_score)
                crit_span.set_attribute("relevance_score", verdict.relevance_score)
            last_verdict = verdict
        except BudgetExceededError as exc:
            best_verified_draft = draft
            best_verified_verdict = None
            logger.warning(
                "Critic hit budget limit for %s (%s); proceeding with verified draft",
                profile.id,
                exc,
            )
            break
        except Exception as exc:
            best_verified_draft = draft
            best_verified_verdict = None
            logger.warning(
                "Critic failed for %s (%s); proceeding with deterministically verified draft",
                profile.id,
                exc,
            )
            break

        current_score = (verdict.faithfulness_score, verdict.relevance_score)
        if best_verified_score is None or current_score >= best_verified_score:
            best_verified_draft = draft
            best_verified_verdict = verdict
            best_verified_score = current_score

        if verdict.approved:
            logger.info(
                "Critic approved draft for %s on attempt %d (faithfulness=%d, relevance=%d)",
                profile.id,
                attempt + 1,
                verdict.faithfulness_score,
                verdict.relevance_score,
            )
            break

        logger.info(
            "Critic requested revision for %s (attempt %d, faithfulness=%d, relevance=%d): %s",
            profile.id,
            attempt + 1,
            verdict.faithfulness_score,
            verdict.relevance_score,
            verdict.revision_instructions[:200],
        )
        issue_lines = "\n".join(f"- {issue}" for issue in verdict.issues)
        revision_feedback = (
            f"Critic audit rejected previous draft "
            f"(faithfulness={verdict.faithfulness_score}/5, "
            f"relevance={verdict.relevance_score}/5).\n"
            f"Issues:\n{issue_lines}\n"
            f"Revision instructions: {verdict.revision_instructions}"
        )

    # Hard post-loop grounding guarantee: NEVER emit an ungrounded or stale URL
    used_fallback = False
    final_draft = best_verified_draft or last_draft
    if final_draft is None:
        final_draft = build_fallback_draft(
            unseen_articles, profile.topics, max_highlights=profile.max_highlights
        )
        used_fallback = True
    else:
        final_check = verify_digest_draft(
            final_draft,
            allowed_articles,
            seen_urls,
            profile.topics,
            max_highlights=profile.max_highlights,
        )
        if not final_check.passed:
            logger.warning(
                "Final draft for %s failed grounding after max revisions; sanitizing citations",
                profile.id,
            )
            sanitized = sanitize_draft_grounding(
                final_draft,
                allowed_articles,
                seen_urls,
                max_highlights=profile.max_highlights,
            )
            sanitized_check = verify_digest_draft(
                sanitized,
                allowed_articles,
                seen_urls,
                profile.topics,
                max_highlights=profile.max_highlights,
            )
            if sanitized_check.passed and sanitized.highlights:
                final_draft = sanitized
            else:
                logger.warning(
                    "Sanitized draft empty/invalid for %s; falling back to deterministic draft",
                    profile.id,
                )
                final_draft = build_fallback_draft(
                    unseen_articles, profile.topics, max_highlights=profile.max_highlights
                )
                used_fallback = True

    final_verification = verify_digest_draft(
        final_draft,
        allowed_articles,
        seen_urls,
        profile.topics,
        max_highlights=profile.max_highlights,
    )
    selected_verdict = best_verified_verdict if best_verified_draft is not None else last_verdict
    if used_fallback:
        selected_verdict = None
    return EditorialResult(
        draft=final_draft,
        revisions=revisions_used,
        verification=final_verification,
        critic_verdict=selected_verdict,
        used_fallback=used_fallback,
    )


def _merge_config_into_settings(settings: Settings, global_config: dict[str, Any]) -> Settings:
    """Apply non-secret defaults from legacy JSON config only for fields not explicitly set."""
    explicit_fields = settings.model_fields_set
    updates: dict[str, Any] = {}
    email_cfg = global_config.get("email")
    if isinstance(email_cfg, dict):
        if (
            "smtp_sender" not in explicit_fields
            and not settings.smtp_sender
            and email_cfg.get("sender")
        ):
            updates["smtp_sender"] = str(email_cfg["sender"]).strip()
        if "smtp_host" not in explicit_fields and email_cfg.get("smtp_host"):
            updates["smtp_host"] = str(email_cfg["smtp_host"]).strip()
        if "smtp_port" not in explicit_fields and email_cfg.get("smtp_port") is not None:
            try:
                parsed_port = int(email_cfg["smtp_port"])
                if 1 <= parsed_port <= 65535:
                    updates["smtp_port"] = parsed_port
                else:
                    logger.warning(
                        "Ignoring out-of-range smtp_port in config: %r",
                        email_cfg["smtp_port"],
                    )
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring invalid smtp_port in config: %r",
                    email_cfg["smtp_port"],
                )

    cfg_model = global_config.get("gemini_model")
    if isinstance(cfg_model, str) and cfg_model.strip():
        model_name = cfg_model.strip()
        if "research_model" not in explicit_fields:
            updates["research_model"] = model_name
        if "synthesis_model" not in explicit_fields:
            updates["synthesis_model"] = model_name
        if "critic_model" not in explicit_fields:
            updates["critic_model"] = model_name

    return settings.model_copy(update=updates) if updates else settings


def _collect_sent_articles_and_clusters(
    draft: DigestDraft,
    recipient_clusters: list[StoryCluster],
    clusters_by_id: dict[str, StoryCluster],
) -> tuple[list[RawArticle], dict[str, str], dict[str, StoryCluster]]:
    """Map delivered digest citations back to source articles and clusters for persistence."""
    article_to_cluster_id: dict[str, str] = {}
    url_to_article: dict[str, RawArticle] = {}
    id_to_article: dict[str, RawArticle] = {}

    for cluster in recipient_clusters:
        full_cluster = clusters_by_id.get(cluster.cluster_id, cluster)
        for article in full_cluster.articles:
            article_to_cluster_id[article.article_id] = full_cluster.cluster_id
            id_to_article[article.article_id] = article
            url_to_article[normalize_url(article.canonical_url or article.url)] = article
            url_to_article[normalize_url(article.url)] = article

    sent_articles_by_id: dict[str, RawArticle] = {}
    cluster_ids_for_articles: dict[str, str] = {}
    saved_clusters: dict[str, StoryCluster] = {}

    for highlight in draft.highlights:
        matched_cluster_ids: set[str] = set()
        for citation in highlight.citations:
            matched_article = id_to_article.get(citation.article_id)
            if matched_article is None:
                matched_article = url_to_article.get(normalize_url(citation.url))
            if matched_article is None:
                continue

            sent_articles_by_id[matched_article.article_id] = matched_article
            cid = article_to_cluster_id.get(matched_article.article_id, "")
            if cid:
                cluster_ids_for_articles[matched_article.article_id] = cid
                matched_cluster_ids.add(cid)

        for cid in matched_cluster_ids:
            matched_cluster = clusters_by_id.get(cid)
            if matched_cluster is None:
                continue
            for sibling in matched_cluster.articles:
                sent_articles_by_id[sibling.article_id] = sibling
                cluster_ids_for_articles[sibling.article_id] = cid
            if cid not in saved_clusters:
                saved_clusters[cid] = matched_cluster.model_copy(
                    update={"summary": highlight.summary.strip()}
                )

    return list(sent_articles_by_id.values()), cluster_ids_for_articles, saved_clusters


async def close_memory_store(store: MemoryStore) -> None:
    """Close a MemoryStore asynchronously if it defines `aclose()`, else synchronously."""
    aclose_fn = getattr(store, "aclose", None)
    if callable(aclose_fn):
        result = aclose_fn()
        if inspect.isawaitable(result):
            await result
        return
    close_fn = getattr(store, "close", None)
    if callable(close_fn):
        close_fn()


def _article_sort_timestamp(article: RawArticle) -> float:
    if article.published_at is None:
        return float("-inf")
    dt = (
        article.published_at.replace(tzinfo=UTC)
        if article.published_at.tzinfo is None
        else article.published_at.astimezone(UTC)
    )
    return dt.timestamp()


async def run_digest_pipeline(
    *,
    settings: Settings | None = None,
    config_path: Path | None = None,
    dry_run: bool = False,
    gather_only: bool = False,
    recipient_filter: str | None = None,
    profiles: list[RecipientProfile] | None = None,
    email_sender: EmailSender | None = None,
    seen_urls: set[str] | None = None,
    memory_store: MemoryStore | None = None,
) -> tuple[list[tuple[RecipientProfile, DigestDraft, DeliveryReceipt]], RunMetrics]:
    """Execute the end-to-end async multi-agent digest pipeline."""
    run_id = uuid.uuid4().hex[:12]
    started_at = datetime.now(UTC)

    base_settings = settings or Settings()
    global_config = load_json_config(config_path)
    effective_settings = _merge_config_into_settings(base_settings, global_config)

    if not gather_only:
        base_dir = config_path.resolve().parent if config_path is not None else Path.cwd()
        effective_settings.apply_genai_env(base_dir=base_dir)

    store = memory_store if memory_store is not None else create_memory_store(effective_settings)
    owns_store = memory_store is None

    owned_telemetry: TelemetryHandle | None = None
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        owned_telemetry = init_telemetry(effective_settings)

    tracer = get_tracer()

    try:
        with (
            log_context(run_id=run_id),
            tracer.start_as_current_span("digest.run") as root_span,
        ):
            root_span.set_attribute("run_id", run_id)
            root_span.set_attribute("dry_run", dry_run)
            root_span.set_attribute("gather_only", gather_only)
            current_trace_id = get_current_trace_id()

            # Stage 1: Load active recipient profiles and cross-day memory upfront
            if profiles is not None:
                active_profiles = _filter_profiles_by_recipient(profiles, recipient_filter)
            else:
                store_profiles = await store.load_active_profiles()
                if store_profiles:
                    active_profiles = _filter_profiles_by_recipient(
                        store_profiles, recipient_filter
                    )
                    logger.info(
                        "Stage 1: Loaded %d active profile(s) from %s",
                        len(active_profiles),
                        type(store).__name__,
                    )
                else:
                    active_profiles = discover_recipient_profiles(
                        config_path, global_config, recipient_filter
                    )
                    logger.info(
                        "Stage 1: Memory store has 0 active profiles; loaded %d from local config",
                        len(active_profiles),
                    )

            root_span.set_attribute("recipient_count", len(active_profiles))

            budget = RunBudgetTracker(
                max_llm_calls=effective_settings.max_llm_calls_per_run,
                max_total_tokens=effective_settings.max_total_tokens_per_run,
                max_scrapes=effective_settings.max_article_scrapes_per_run,
                max_searches=effective_settings.max_search_calls_per_run,
            )

            if not active_profiles:
                logger.warning("No active recipient profiles found to process.")
                metrics = RunMetrics(
                    run_id=run_id,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    trace_id=current_trace_id,
                )
                return [], metrics

            stored_seen_urls = await store.get_seen_urls(
                since_days=effective_settings.seen_url_window_days
            )
            prior_clusters = await store.get_recent_clusters(
                since_days=effective_settings.storyline_window_days
            )
            active_seen_urls = normalize_seen_urls(stored_seen_urls | set(seen_urls or set()))
            logger.info(
                "Stage 1 memory loaded: %d seen URL(s) (%dd), %d prior cluster(s) (%dd)",
                len(active_seen_urls),
                effective_settings.seen_url_window_days,
                len(prior_clusters),
                effective_settings.storyline_window_days,
            )

            # Stage 2: Pool unique RSS feeds and target topics across active recipients
            pooled_feeds: list[RssFeedConfig] = []
            feed_url_to_source: dict[str, str] = {}
            pooled_topics: list[str] = []
            seen_topics_lower: set[str] = set()

            for profile in active_profiles:
                for topic in profile.topics:
                    key = topic.strip().lower()
                    if key and key not in seen_topics_lower:
                        seen_topics_lower.add(key)
                        pooled_topics.append(topic.strip())
                for feed in profile.rss_feeds:
                    norm_feed_url = normalize_url(feed.url)
                    if norm_feed_url not in feed_url_to_source:
                        feed_url_to_source[norm_feed_url] = feed.name
                        pooled_feeds.append(feed)

            max_lookback = max((p.lookback_hours for p in active_profiles), default=36)
            max_per_feed = int(global_config.get("max_articles_per_source") or 15)

            with tracer.start_as_current_span("ingestion.rss") as rss_span:
                rss_span.set_attribute("feed_count", len(pooled_feeds))
                rss_span.set_attribute("lookback_hours", max_lookback)
                logger.info(
                    "Stage 2: Fetching %d unique RSS feed(s) for %d active recipient(s)",
                    len(pooled_feeds),
                    len(active_profiles),
                )
                rss_articles = await fetch_rss_feeds(
                    pooled_feeds,
                    lookback_hours=max_lookback,
                    max_per_feed=max_per_feed,
                )
                rss_span.set_attribute("articles_gathered", len(rss_articles))
                logger.info(
                    "Stage 2 complete: %d unique RSS article(s) gathered", len(rss_articles)
                )

            # Stage 3: Adaptive ADK ResearchAgent (skipped on --gather-only)
            if gather_only:
                all_articles = rss_articles
            else:
                with tracer.start_as_current_span("agent.research") as research_span:
                    research_span.set_attribute("topic_count", len(pooled_topics))
                    research_span.set_attribute("model", effective_settings.research_model)
                    logger.info(
                        "Stage 3: Running ADK ResearchAgent across %d topic(s)", len(pooled_topics)
                    )
                    all_articles, _ = await run_research_stage(
                        rss_articles,
                        pooled_topics,
                        max_lookback,
                        effective_settings,
                        budget,
                        seen_urls=active_seen_urls,
                    )
                    research_span.set_attribute("articles_total", len(all_articles))
                    research_span.set_attribute("scrapes_performed", budget.scrape_calls)
                    research_span.set_attribute("search_calls", budget.search_calls)

            # Stage 4: Pre-filter exact seen URLs (avoid embedding already-sent URLs) and coalesce
            # same-day URL duplicates (preferring full_text and preserving merged topic_hints).
            with tracer.start_as_current_span("memory.embed_and_cluster") as cluster_span:
                grouped_unseen: dict[str, list[RawArticle]] = {}
                counted_seen_canonical: set[str] = set()
                exact_seen_deduped = 0

                for article in all_articles:
                    canon = normalize_url(article.canonical_url or article.url)
                    raw_norm = normalize_url(article.url)
                    if (
                        canon in active_seen_urls
                        or raw_norm in active_seen_urls
                        or article.url in active_seen_urls
                    ):
                        if canon not in counted_seen_canonical:
                            counted_seen_canonical.add(canon)
                            exact_seen_deduped += 1
                        continue
                    grouped_unseen.setdefault(canon, []).append(article)

                unseen_candidates: list[RawArticle] = []
                for copies in grouped_unseen.values():
                    if len(copies) == 1:
                        unseen_candidates.append(copies[0])
                        continue
                    copies.sort(
                        key=lambda a: (
                            0 if a.full_text else 1,
                            -_article_sort_timestamp(a),
                            a.article_id,
                        )
                    )
                    winner = copies[0]
                    merged_hints: list[str] = []
                    seen_hints: set[str] = set()
                    for copy_art in copies:
                        for hint in copy_art.topic_hints:
                            cleaned = hint.strip()
                            key = cleaned.lower()
                            if cleaned and key not in seen_hints:
                                seen_hints.add(key)
                                merged_hints.append(cleaned)
                    if merged_hints != winner.topic_hints:
                        winner = winner.model_copy(update={"topic_hints": merged_hints})
                    unseen_candidates.append(winner)

                unseen_candidates.sort(
                    key=lambda a: (-_article_sort_timestamp(a), a.article_id, a.canonical_url)
                )
                cluster_span.set_attribute("candidate_count", len(unseen_candidates))

                articles_deduped_cross_day = exact_seen_deduped
                if gather_only or not unseen_candidates:
                    clusters = build_singleton_clusters(unseen_candidates)
                else:
                    try:
                        embed_inputs = [
                            build_article_embedding_text(article) for article in unseen_candidates
                        ]
                        embeddings = await embed_texts(embed_inputs, effective_settings, budget)
                        clusters, semantic_deduped = cluster_and_link_stories(
                            unseen_candidates,
                            embeddings,
                            active_seen_urls,
                            prior_clusters,
                            merge_threshold=effective_settings.cluster_merge_threshold,
                            storyline_threshold=effective_settings.storyline_link_threshold,
                            semantic_dedup_threshold=effective_settings.semantic_dedup_threshold,
                        )
                        articles_deduped_cross_day += semantic_deduped
                    except Exception as exc:
                        logger.warning(
                            "Stage 4 embeddings/clustering failed (%s); falling back to singleton "
                            "clusters",
                            exc,
                        )
                        clusters = build_singleton_clusters(unseen_candidates)

                developing_stories_linked = sum(
                    1 for cluster in clusters if cluster.is_developing_story
                )
                cluster_span.set_attribute("clusters_formed", len(clusters))
                cluster_span.set_attribute(
                    "articles_deduped_cross_day", articles_deduped_cross_day
                )
                cluster_span.set_attribute(
                    "developing_stories_linked", developing_stories_linked
                )
                logger.info(
                    "Stage 4 complete: %d candidate(s) -> %d cluster(s), %d cross-day deduped, "
                    "%d developing story link(s)",
                    len(unseen_candidates),
                    len(clusters),
                    articles_deduped_cross_day,
                    developing_stories_linked,
                )

            clusters_by_id = {cluster.cluster_id: cluster for cluster in clusters}

            # Prepare EmailSender lazily only when real delivery is required
            sender_instance: EmailSender | None = email_sender
            if sender_instance is None and not dry_run and not gather_only:
                base_dir = config_path.resolve().parent if config_path is not None else Path.cwd()
                smtp_password = effective_settings.resolve_email_password(base_dir=base_dir) or ""
                sender_instance = SmtpEmailSender(
                    host=effective_settings.smtp_host,
                    port=effective_settings.smtp_port,
                    sender=effective_settings.smtp_sender,
                    password=smtp_password,
                )

            # Stage 5: Per-recipient isolated editorial loop & delivery
            results: list[tuple[RecipientProfile, DigestDraft, DeliveryReceipt]] = []
            clusters_to_save: dict[str, StoryCluster] = {}
            recipients_failed = 0
            total_revisions = 0
            grounding_passes = 0

            for profile in active_profiles:
                with log_context(recipient_id=profile.id):
                    try:
                        recipient_clusters = filter_clusters_for_recipient(
                            clusters,
                            profile,
                            feed_url_to_source=feed_url_to_source,
                        )
                        recipient_articles = [a for c in recipient_clusters for a in c.articles]

                        if gather_only:
                            header = f"=== Recipient: {profile.email_address} ({profile.id}) ==="
                            safe_print(
                                f"{header}\n{_format_articles_for_console(recipient_articles)}"
                            )
                            draft = build_fallback_draft(
                                recipient_articles,
                                profile.topics,
                                max_highlights=profile.max_highlights,
                            )
                            receipt = DeliveryReceipt(
                                recipient_id=profile.id,
                                recipient_email=profile.email_address,
                                status="dry_run",
                                provider="gather_only",
                            )
                            results.append((profile, draft, receipt))
                            grounding_passes += 1
                            continue

                        with tracer.start_as_current_span(
                            "recipient.editorial_loop"
                        ) as editorial_span:
                            editorial_span.set_attribute("recipient_id", profile.id)
                            editorial_span.set_attribute(
                                "candidate_clusters", len(recipient_clusters)
                            )
                            editorial = await run_editorial_loop(
                                profile,
                                recipient_clusters,
                                active_seen_urls,
                                effective_settings,
                                budget,
                            )
                            editorial_span.set_attribute("revision_count", editorial.revisions)
                            editorial_span.set_attribute(
                                "grounding_passed", editorial.verification.passed
                            )
                            editorial_span.set_attribute("used_fallback", editorial.used_fallback)

                        total_revisions += editorial.revisions
                        if editorial.verification.passed:
                            grounding_passes += 1

                        subject = render_subject(profile, editorial.draft)
                        plain_text = render_plain_text(editorial.draft)
                        html_body = render_html(editorial.draft)

                        with tracer.start_as_current_span("delivery.send") as delivery_span:
                            delivery_span.set_attribute("recipient_id", profile.id)
                            delivery_span.set_attribute("dry_run", dry_run)
                            if dry_run:
                                header = (
                                    f"=== Recipient: {profile.email_address} ({profile.id}) ==="
                                )
                                safe_print(f"{header}\nSubject: {subject}\n\n{plain_text}")
                                receipt = DeliveryReceipt(
                                    recipient_id=profile.id,
                                    recipient_email=profile.email_address,
                                    status="dry_run",
                                    provider="dry_run",
                                )
                            else:
                                assert sender_instance is not None
                                receipt = await asyncio.to_thread(
                                    sender_instance.send,
                                    profile,
                                    subject,
                                    plain_text,
                                    html_body,
                                )
                                if receipt.status == "failed":
                                    recipients_failed += 1
                                elif receipt.status == "sent":
                                    sent_articles, cluster_id_map, matched_clusters = (
                                        _collect_sent_articles_and_clusters(
                                            editorial.draft,
                                            recipient_clusters,
                                            clusters_by_id,
                                        )
                                    )
                                    if sent_articles:
                                        try:
                                            await store.record_sent_articles(
                                                sent_articles, profile.id, cluster_id_map
                                            )
                                        except Exception as exc:
                                            logger.exception(
                                                "Failed to record sent articles for %s: %s",
                                                profile.id,
                                                exc,
                                            )
                                    for cid, saved_cluster in matched_clusters.items():
                                        if cid not in clusters_to_save:
                                            clusters_to_save[cid] = saved_cluster
                            delivery_span.set_attribute("status", receipt.status)
                            delivery_span.set_attribute("provider", receipt.provider)

                        results.append((profile, editorial.draft, receipt))
                    except Exception as exc:
                        recipients_failed += 1
                        logger.exception(
                            "Recipient %s (%s) failed: %s",
                            profile.id,
                            profile.email_address,
                            exc,
                        )
                        failed_draft = build_fallback_draft([], profile.topics)
                        failed_receipt = DeliveryReceipt(
                            recipient_id=profile.id,
                            recipient_email=profile.email_address,
                            status="failed",
                            provider="pipeline_error",
                            error=str(exc),
                        )
                        results.append((profile, failed_draft, failed_receipt))

            processed = len(active_profiles)
            metrics = RunMetrics(
                run_id=run_id,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                recipients_processed=processed,
                recipients_failed=recipients_failed,
                articles_gathered=len(all_articles),
                articles_deduped_cross_day=articles_deduped_cross_day,
                clusters_formed=len(clusters),
                developing_stories_linked=developing_stories_linked,
                scrapes_performed=budget.scrape_calls,
                llm_calls=budget.llm_calls,
                input_tokens=budget.input_tokens,
                output_tokens=budget.output_tokens,
                estimated_cost_usd=budget.estimated_cost_usd(effective_settings.synthesis_model),
                avg_revisions_per_recipient=(
                    round(total_revisions / processed, 2) if processed else 0.0
                ),
                deterministic_grounding_pass_rate=(
                    round(grounding_passes / processed, 4) if processed else 1.0
                ),
                trace_id=current_trace_id,
            )

            root_span.set_attribute("llm_calls", metrics.llm_calls)
            root_span.set_attribute("estimated_cost_usd", metrics.estimated_cost_usd)
            root_span.set_attribute("recipients_failed", metrics.recipients_failed)

            # Stage 7: Persist story clusters, RunMetrics, and run programmatic 30-day pruning
            if not gather_only:
                with tracer.start_as_current_span("memory.persist_and_prune") as persist_span:
                    persist_span.set_attribute("dry_run", dry_run)
                    clusters_saved_count = 0
                    pruned_count = 0
                    if not dry_run and clusters_to_save:
                        try:
                            await store.save_story_clusters(list(clusters_to_save.values()))
                            clusters_saved_count = len(clusters_to_save)
                        except Exception as exc:
                            logger.exception("Failed to save story clusters: %s", exc)
                    try:
                        await store.save_run_metrics(metrics)
                    except Exception as exc:
                        logger.exception("Failed to save run metrics: %s", exc)
                    if not dry_run:
                        try:
                            pruned_count = await store.prune_expired(
                                retention_days=effective_settings.memory_retention_days
                            )
                            if pruned_count:
                                logger.info(
                                    "Stage 7: Pruned %d expired memory record(s) older than %d "
                                    "days",
                                    pruned_count,
                                    effective_settings.memory_retention_days,
                                )
                        except Exception as exc:
                            logger.exception("Failed to prune expired memory records: %s", exc)
                    persist_span.set_attribute("clusters_saved", clusters_saved_count)
                    persist_span.set_attribute("pruned_count", pruned_count)

            return results, metrics
    finally:
        try:
            if owns_store:
                await close_memory_store(store)
        finally:
            if owned_telemetry is not None:
                owned_telemetry.shutdown()
