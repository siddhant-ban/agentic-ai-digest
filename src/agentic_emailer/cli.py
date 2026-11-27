"""CLI entry point for the agentic news digest pipeline."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from agentic_emailer.config import Settings
from agentic_emailer.memory import create_memory_store
from agentic_emailer.pipeline import (
    load_json_config,
    load_profiles_from_file,
    run_digest_pipeline,
    safe_print,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="agentic-emailer",
        description="Google ADK multi-agent news digest pipeline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Gather news, run ADK research/synthesis/critic loop, and deliver digests.",
    )
    run_parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to optional JSON config file (default: config.json).",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full ADK pipeline and print rendered digests without sending emails.",
    )
    run_parser.add_argument(
        "--gather-only",
        action="store_true",
        help="Fetch and print normalized RSS articles without running LLM agents or sending email.",
    )
    run_parser.add_argument(
        "--recipient",
        type=str,
        default=None,
        help="Filter run to a specific recipient profile ID or email address.",
    )

    seed_parser = subparsers.add_parser(
        "seed-profiles",
        help="Validate and upsert recipient profiles from a JSON file into MemoryStore.",
    )
    seed_parser.add_argument(
        "--from",
        dest="source_path",
        type=Path,
        required=True,
        help="Path to recipients JSON file (list or single recipient object).",
    )
    seed_parser.add_argument(
        "--backend",
        choices=("local", "firestore"),
        default=None,
        help="Override configured memory backend (`local` or `firestore`).",
    )
    seed_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional base config JSON providing fallback feeds/lookback defaults.",
    )
    return parser


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _handle_run(args: argparse.Namespace) -> int:
    config_path: Path | None = args.config
    if config_path is not None and not config_path.exists():
        # Allow running from env vars, memory store, or recipients.json when config.json is absent
        if str(config_path) == "config.json":
            config_path = None
        else:
            logger.error("Config file not found: %s", config_path)
            return 1

    settings = Settings()
    base_dir = config_path.resolve().parent if config_path is not None else Path.cwd()
    if not args.gather_only:
        settings.apply_genai_env(base_dir=base_dir)

    results, metrics = await run_digest_pipeline(
        settings=settings,
        config_path=config_path,
        dry_run=args.dry_run,
        gather_only=args.gather_only,
        recipient_filter=args.recipient,
    )

    safe_print(
        f"\n[Run {metrics.run_id}] recipients={metrics.recipients_processed} "
        f"failed={metrics.recipients_failed} articles={metrics.articles_gathered} "
        f"deduped={metrics.articles_deduped_cross_day} clusters={metrics.clusters_formed} "
        f"developing={metrics.developing_stories_linked} scrapes={metrics.scrapes_performed} "
        f"llm_calls={metrics.llm_calls} tokens={metrics.input_tokens + metrics.output_tokens} "
        f"est_cost=${metrics.estimated_cost_usd:.5f} "
        f"grounding_pass_rate={metrics.deterministic_grounding_pass_rate:.2%}"
    )

    if results and metrics.recipients_failed == metrics.recipients_processed:
        return 1
    return 0


async def _handle_seed_profiles(args: argparse.Namespace) -> int:
    source_path: Path = args.source_path
    if not source_path.exists() or not source_path.is_file():
        logger.error("Recipient profiles file not found: %s", source_path)
        return 1

    global_config: dict[str, object] = {}
    if args.config is not None:
        if not args.config.exists() or not args.config.is_file():
            logger.error("Config file not found: %s", args.config)
            return 1
        global_config = load_json_config(args.config)

    profiles = load_profiles_from_file(
        source_path,
        global_config=global_config,
        include_disabled=True,
    )
    if not profiles:
        logger.error("No valid recipient profiles found in %s", source_path)
        return 1

    settings = Settings()
    if args.backend is not None:
        settings = settings.model_copy(update={"memory_backend": args.backend})

    store = create_memory_store(settings)
    try:
        upserted = await store.upsert_profiles(profiles)
    finally:
        close_fn = getattr(store, "close", None)
        if callable(close_fn):
            close_fn()

    enabled_count = sum(1 for p in profiles if p.enabled)
    disabled_count = len(profiles) - enabled_count
    target_desc = (
        f"local ({settings.local_memory_dir})"
        if settings.memory_backend == "local"
        else (
            f"firestore (project={settings.gcp_project_id or 'default'}, "
            f"db={settings.firestore_database})"
        )
    )
    safe_print(
        f"Seeded {upserted} recipient profile(s) "
        f"({enabled_count} enabled, {disabled_count} disabled) into {target_desc}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point registered as `agentic-emailer`."""
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        try:
            return asyncio.run(_handle_run(args))
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            logger.exception("Fatal pipeline error: %s", exc)
            return 1

    if args.command == "seed-profiles":
        try:
            return asyncio.run(_handle_seed_profiles(args))
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            logger.exception("Failed to seed profiles: %s", exc)
            return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
