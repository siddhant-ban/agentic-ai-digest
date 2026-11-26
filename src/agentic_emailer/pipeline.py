"""End-to-end async digest orchestrator with bounded editorial loop and recipient isolation."""

from __future__ import annotations

import asyncio
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
from agentic_emailer.ingestion.rss import fetch_rss_feeds, normalize_url, within_lookback
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
) -> RecipientProfile | None:
    if raw.get("enabled") is False:
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

    topics = raw.get("topics") or global_config.get("topics") or []
    rss_feeds = _parse_rss_feeds(raw.get("rss_feeds") or global_config.get("rss_feeds") or [])
    subject_prefix = (
        raw.get("subject_prefix")
        or email_dict.get("subject_prefix")
        or global_email_dict.get("subject_prefix")
        or "[Daily Digest]"
    )
    lookback_hours = int(raw.get("lookback_hours") or global_config.get("lookback_hours") or 36)
    max_highlights = int(raw.get("max_highlights") or global_config.get("max_highlights") or 8)

    try:
        return RecipientProfile(
            id=profile_id,
            email_address=str(recipient_email),
            subject_prefix=str(subject_prefix),
            enabled=True,
            topics=[str(t).strip() for t in topics if str(t).strip()],
            rss_feeds=rss_feeds,
            lookback_hours=lookback_hours,
            max_highlights=max_highlights,
        )
    except Exception as exc:
        logger.warning("Invalid recipient profile %s: %s", profile_id, exc)
        return None


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
        candidates: list[Path] = []
        if config_path is not None:
            candidates.append(config_path.resolve().parent / "recipients.json")
        candidates.append(Path.cwd() / "recipients.json")

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

    if recipient_filter:
        target = recipient_filter.strip().lower()
        profiles = [
            p
            for p in profiles
            if p.id.lower() == target or p.email_address.lower() == target
        ]

    return profiles


def build_singleton_clusters(articles: list[RawArticle]) -> list[StoryCluster]:
    """Wrap each unique normalized article into a 1-to-1 StoryCluster (Milestone 1 hook)."""
    clusters: list[StoryCluster] = []
    seen_canonical: set[str] = set()
    for article in articles:
        if article.canonical_url in seen_canonical:
            continue
        seen_canonical.add(article.canonical_url)
        topic = article.topic_hints[0] if article.topic_hints else "General"
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
) -> EditorialResult:
    """Execute the bounded Synthesis -> Deterministic Verifier -> Critic editorial loop."""
    allowed_articles = [article for cluster in clusters for article in cluster.articles]
    normalized_seen = {normalize_url(u) for u in seen_urls if u.strip()} | set(seen_urls)
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
        verification = verify_digest_draft(empty_draft, [], seen_urls, profile.topics)
        return EditorialResult(
            draft=empty_draft,
            revisions=0,
            verification=verification,
            critic_verdict=None,
            used_fallback=True,
        )

    max_revisions = settings.max_critic_revisions
    revision_feedback: str | None = None
    best_verified_draft: DigestDraft | None = None
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
            draft, _ = await run_synthesis(
                clusters,
                profile.topics,
                settings,
                budget,
                max_highlights=profile.max_highlights,
                seen_urls=seen_urls,
                revision_feedback=revision_feedback,
            )
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

        verification = verify_digest_draft(
            draft,
            allowed_articles,
            seen_urls,
            profile.topics,
            max_highlights=profile.max_highlights,
        )
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

        # Short-circuit Critic when budget is exhausted after deterministic check passes
        if not budget.can_call_llm():
            best_verified_draft = draft
            logger.warning(
                "Skipping Critic for %s because LLM budget is exhausted; using verified draft",
                profile.id,
            )
            break

        try:
            verdict, _ = await run_critic(
                draft, clusters, profile.topics, settings, budget
            )
            last_verdict = verdict
        except BudgetExceededError as exc:
            best_verified_draft = draft
            logger.warning(
                "Critic hit budget limit for %s (%s); proceeding with verified draft",
                profile.id,
                exc,
            )
            break
        except Exception as exc:
            best_verified_draft = draft
            logger.warning(
                "Critic failed for %s (%s); proceeding with deterministically verified draft",
                profile.id,
                exc,
            )
            break

        current_score = (verdict.faithfulness_score, verdict.relevance_score)
        if best_verified_score is None or current_score >= best_verified_score:
            best_verified_draft = draft
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
    return EditorialResult(
        draft=final_draft,
        revisions=revisions_used,
        verification=final_verification,
        critic_verdict=last_verdict,
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
        if "smtp_port" not in explicit_fields and email_cfg.get("smtp_port"):
            updates["smtp_port"] = int(email_cfg["smtp_port"])

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

    active_profiles = (
        profiles
        if profiles is not None
        else discover_recipient_profiles(config_path, global_config, recipient_filter)
    )

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
        )
        return [], metrics

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
    logger.info("Stage 2 complete: %d unique RSS article(s) gathered", len(rss_articles))

    active_seen_urls = set(seen_urls or set())

    # Stage 3: Adaptive ADK ResearchAgent (skipped on --gather-only)
    if gather_only:
        all_articles = rss_articles
    else:
        logger.info("Stage 3: Running ADK ResearchAgent across %d topic(s)", len(pooled_topics))
        all_articles, _ = await run_research_stage(
            rss_articles,
            pooled_topics,
            max_lookback,
            effective_settings,
            budget,
            seen_urls=active_seen_urls,
        )

    # Stage 4: Build 1-to-1 StoryClusters (Milestone 2 plugs semantic clustering here)
    clusters = build_singleton_clusters(all_articles)

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
    recipients_failed = 0
    total_revisions = 0
    grounding_passes = 0

    for profile in active_profiles:
        try:
            recipient_clusters = filter_clusters_for_recipient(
                clusters,
                profile,
                feed_url_to_source=feed_url_to_source,
            )
            recipient_articles = [a for c in recipient_clusters for a in c.articles]

            if gather_only:
                header = f"=== Recipient: {profile.email_address} ({profile.id}) ==="
                safe_print(f"{header}\n{_format_articles_for_console(recipient_articles)}")
                draft = build_fallback_draft(
                    recipient_articles, profile.topics, max_highlights=profile.max_highlights
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

            editorial = await run_editorial_loop(
                profile,
                recipient_clusters,
                active_seen_urls,
                effective_settings,
                budget,
            )
            total_revisions += editorial.revisions
            if editorial.verification.passed:
                grounding_passes += 1

            subject = render_subject(profile, editorial.draft)
            plain_text = render_plain_text(editorial.draft)
            html_body = render_html(editorial.draft)

            if dry_run:
                header = f"=== Recipient: {profile.email_address} ({profile.id}) ==="
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

            results.append((profile, editorial.draft, receipt))
        except Exception as exc:
            recipients_failed += 1
            logger.exception("Recipient %s (%s) failed: %s", profile.id, profile.email_address, exc)
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
        articles_deduped_cross_day=0,
        clusters_formed=len(clusters),
        developing_stories_linked=0,
        scrapes_performed=budget.scrape_calls,
        llm_calls=budget.llm_calls,
        input_tokens=budget.input_tokens,
        output_tokens=budget.output_tokens,
        estimated_cost_usd=budget.estimated_cost_usd(effective_settings.synthesis_model),
        avg_revisions_per_recipient=round(total_revisions / processed, 2) if processed else 0.0,
        deterministic_grounding_pass_rate=(
            round(grounding_passes / processed, 4) if processed else 1.0
        ),
        trace_id=None,
    )
    return results, metrics
