"""AI news aggregator: gather, summarize, and email a digest."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from emailer import send_digest
from sources import Article, gather_all
from summarizer import build_digest

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

    if not config.get("topics"):
        raise ValueError("Config must include at least one topic in 'topics'.")
    if not config.get("email"):
        raise ValueError("Config must include an 'email' section.")

    return config


def load_secret_file(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def load_gemini_api_key() -> str:
    key = load_secret_file(GEMINI_KEY_PATH)
    if not key:
        raise FileNotFoundError(
            f"Gemini API key not found. Add it to {GEMINI_KEY_PATH.name}."
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


def aggregator(
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    gather_only: bool = False,
) -> str:
    topics: list[str] = config["topics"]
    model = config.get("gemini_model", "gemini-2.0-flash")

    logger.info("Gathering articles for topics: %s", ", ".join(topics))
    articles = gather_all(config)
    logger.info("Gathered %d unique article(s)", len(articles))

    if gather_only:
        output = format_articles_for_console(articles)
        print(output)
        return output

    api_key = load_gemini_api_key()
    logger.info("Summarizing with Gemini model: %s", model)
    digest_md, digest_html = build_digest(articles, topics, api_key, model)

    if dry_run:
        print(digest_md)
        return digest_md

    smtp_password = load_smtp_password()
    send_digest(
        config["email"],
        digest_md,
        digest_html,
        smtp_password,
        article_count=len(articles),
        topic_count=len(topics),
    )
    return digest_md


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        aggregator(
            config,
            dry_run=args.dry_run,
            gather_only=args.gather_only,
        )
        return 0
    except Exception as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
