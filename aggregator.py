"""AI news aggregator: gather, summarize, and email a digest."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any


from emailer import send_digest
from sources import Article, filter_articles_for_recipient, gather_all
from summarizer import build_digest

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def is_valid_email(email: str) -> bool:
    """Validate email address format."""
    if not isinstance(email, str):
        return False
    return bool(EMAIL_REGEX.match(email.strip()))


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
GEMINI_KEY_PATH = PROJECT_DIR / "gemini_api_key.txt"
SMTP_PASSWORD_PATH = PROJECT_DIR / "smtp_password.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found at {config_path}. Copy config.example.json to config.json."
        )
    with config_path.open(encoding="utf-8") as f:
        config = json.load(f)

    return config


def parse_recipient_raw(
    raw: dict[str, Any],
    global_config: dict[str, Any],
    default_id: str = "recipient",
) -> dict[str, Any] | None:
    if raw.get("enabled") is False:
        logger.info("Skipping disabled recipient profile: %s", raw.get("id") or default_id)
        return None

    profile_id = str(raw.get("id") or default_id)
    recipient_email = (
        raw.get("email_address")
        or raw.get("recipient_email")
        or (raw.get("email") if isinstance(raw.get("email"), str) else None)
        or (raw.get("email", {}) if isinstance(raw.get("email"), dict) else {}).get("recipient")
    )

    if not recipient_email:
        logger.warning("Recipient profile %s is missing an email address", profile_id)
        return None

    if not is_valid_email(recipient_email):
        logger.warning("Recipient profile %s has an invalid email address: %s", profile_id, recipient_email)
        return None

    topics = raw.get("topics") or global_config.get("topics") or []
    rss_feeds = raw.get("rss_feeds") or global_config.get("rss_feeds") or []
    subject_prefix = (
        raw.get("subject_prefix")
        or (raw.get("email", {}) if isinstance(raw.get("email"), dict) else {}).get("subject_prefix")
        or global_config.get("email", {}).get("subject_prefix", "[Daily Digest]")
    )

    email_config = {
        "smtp_host": global_config.get("email", {}).get("smtp_host", "smtp.gmail.com"),
        "smtp_port": global_config.get("email", {}).get("smtp_port", 587),
        "sender": global_config.get("email", {}).get("sender", ""),
        "recipient": recipient_email,
        "subject_prefix": subject_prefix,
    }

    return {
        "id": profile_id,
        "email_address": recipient_email,
        "topics": topics,
        "rss_feeds": rss_feeds,
        "lookback_hours": raw.get("lookback_hours") or global_config.get("lookback_hours", 48),
        "gemini_model": raw.get("gemini_model") or global_config.get("gemini_model", "gemini-2.0-flash"),
        "email": email_config,
    }


def discover_recipients(
    config_path: Path,
    global_config: dict[str, Any],
    recipient_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Discover recipient profiles from RECIPIENTS_JSON env var, `recipients.json` file, or global config fallback."""
    profiles: list[dict[str, Any]] = []

    # 1. Check RECIPIENTS_JSON environment variable (CI secret injection)
    env_recipients = os.environ.get("RECIPIENTS_JSON")
    if env_recipients and env_recipients.strip():
        try:
            parsed_env = json.loads(env_recipients)
            items = parsed_env if isinstance(parsed_env, list) else [parsed_env]
            for idx, item in enumerate(items, start=1):
                p = parse_recipient_raw(item, global_config, default_id=f"env_profile_{idx}")
                if p:
                    profiles.append(p)
        except Exception as exc:
            logger.warning("Could not parse RECIPIENTS_JSON environment variable: %s", exc)

    # 2. Check recipients.json single file if no env profiles found
    if not profiles:
        recipients_file = config_path.parent / "recipients.json"
        if recipients_file.exists() and recipients_file.is_file():
            try:
                with recipients_file.open(encoding="utf-8") as f:
                    data = json.load(f)
                items = data if isinstance(data, list) else [data]
                for idx, item in enumerate(items, start=1):
                    p = parse_recipient_raw(item, global_config, default_id=f"profile_{idx}")
                    if p:
                        profiles.append(p)
            except Exception as exc:
                logger.warning("Could not parse %s: %s", recipients_file, exc)

    # 3. Fallback to single recipient in global_config
    if not profiles:
        email_section = global_config.get("email", {})
        recipient_email = email_section.get("recipient")
        topics = global_config.get("topics", [])
        if recipient_email and not is_valid_email(recipient_email):
            logger.warning("Global config has an invalid recipient email address: %s", recipient_email)
            recipient_email = None

        if recipient_email or topics:
            profiles.append(
                {
                    "id": "default",
                    "email_address": recipient_email or "default@example.com",
                    "topics": topics,
                    "rss_feeds": global_config.get("rss_feeds", []),
                    "lookback_hours": global_config.get("lookback_hours", 48),
                    "gemini_model": global_config.get("gemini_model", "gemini-2.0-flash"),
                    "email": email_section,
                }
            )

    if recipient_filter:
        target = recipient_filter.lower()
        profiles = [
            p
            for p in profiles
            if p["email_address"].lower() == target or p["id"].lower() == target
        ]

    return profiles



def load_secret_file(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def load_gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or load_secret_file(GEMINI_KEY_PATH)
    if not key:
        raise FileNotFoundError(
            "Gemini API key not found. Set GEMINI_API_KEY or add gemini_api_key.txt."
        )
    return key


def load_smtp_password() -> str:
    password = os.environ.get("EMAIL_PASSWORD") or load_secret_file(SMTP_PASSWORD_PATH)
    if not password:
        raise FileNotFoundError(
            "SMTP password not found. Set EMAIL_PASSWORD or add smtp_password.txt."
        )
    return password


def format_articles_for_console(articles: list[Article]) -> str:
    lines = [f"Found {len(articles)} article(s):\n"]
    for index, article in enumerate(articles, start=1):
        date_str = (
            article.published_at.strftime("%Y-%m-%d %H:%M UTC")
            if article.published_at
            else "unknown date"
        )
        lines.extend(
            [
                f"{index}. {article.title}",
                f"   Source: {article.source}",
                f"   Date:   {date_str}",
                f"   URL:    {article.url}",
                f"   Snippet: {article.snippet[:200]}{'...' if len(article.snippet) > 200 else ''}",
                "",
            ]
        )
    return "\n".join(lines)


def safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
        encoded = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(encoded)


def aggregator(
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    gather_only: bool = False,
    recipient: str | None = None,
    config_path: Path | None = None,
) -> str:
    effective_config_path = config_path or DEFAULT_CONFIG_PATH
    profiles = discover_recipients(effective_config_path, config, recipient_filter=recipient)

    if not profiles:
        logger.warning("No active recipient profiles found to process.")
        return "No recipient profiles to process."

    combined_topics: set[str] = set()
    combined_feeds: list[dict[str, Any]] = []
    seen_feed_urls: set[str] = set()

    for p in profiles:
        combined_topics.update(p["topics"])
        for feed in p["rss_feeds"]:
            feed_url = feed.get("url")
            if feed_url and feed_url not in seen_feed_urls:
                seen_feed_urls.add(feed_url)
                combined_feeds.append(feed)

    global_gather_config = {
        "topics": list(combined_topics),
        "rss_feeds": combined_feeds,
        "lookback_hours": max((p["lookback_hours"] for p in profiles), default=48),
        "max_articles_per_source": config.get("max_articles_per_source", 15),
        "search_queries_per_topic": config.get("search_queries_per_topic", 2),
    }

    logger.info("Gathering global articles across %d recipient profile(s)...", len(profiles))
    global_articles = gather_all(global_gather_config)
    logger.info("Gathered %d unique global article(s)", len(global_articles))

    outputs: list[str] = []

    api_key = None if gather_only else load_gemini_api_key()
    smtp_password = None if (gather_only or dry_run) else load_smtp_password()

    for profile in profiles:
        recipient_email = profile["email_address"]
        logger.info("Processing recipient: %s (%s)", recipient_email, profile["id"])
        recipient_articles = filter_articles_for_recipient(global_articles, profile)

        if gather_only:
            header = f"=== Recipient: {recipient_email} ({profile['id']}) ==="
            output = f"{header}\n" + format_articles_for_console(recipient_articles)
            safe_print(output)
            outputs.append(output)
            continue

        model = profile["gemini_model"]
        digest_md, digest_html = build_digest(recipient_articles, profile["topics"], api_key, model)

        if dry_run:
            header = f"=== Recipient: {recipient_email} ({profile['id']}) ==="
            output = f"{header}\n" + digest_md
            safe_print(output)
            outputs.append(digest_md)
            continue

        try:
            send_digest(
                profile["email"],
                digest_md,
                digest_html,
                smtp_password,
                article_count=len(recipient_articles),
                topic_count=len(profile["topics"]),
            )
            outputs.append(digest_md)
        except Exception as exc:
            logger.error("Failed to send digest email to %s (%s): %s", recipient_email, profile["id"], exc)

    return "\n\n".join(outputs)




def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gather AI news, summarize with Gemini, and email a digest."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config JSON (default: config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Gather and summarize, print digest to console, do not send email",
    )
    parser.add_argument(
        "--gather-only",
        action="store_true",
        help="Gather articles only and print the raw list",
    )
    parser.add_argument(
        "--recipient",
        type=str,
        default=None,
        help="Filter run to specific recipient email or profile ID",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        aggregator(
            config,
            dry_run=args.dry_run,
            gather_only=args.gather_only,
            recipient=args.recipient,
            config_path=args.config,
        )
        return 0
    except Exception as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

