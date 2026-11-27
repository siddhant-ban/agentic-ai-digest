"""CLI entry point for the agentic news digest pipeline."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from agentic_emailer.config import Settings
from agentic_emailer.memory import create_memory_store, ensure_utc
from agentic_emailer.models import RunMetrics
from agentic_emailer.observability import configure_logging, init_telemetry
from agentic_emailer.pipeline import (
    close_memory_store,
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

    inspect_parser = subparsers.add_parser(
        "inspect-runs",
        help="Display recent RunMetrics telemetry recorded in local memory or Firestore.",
    )
    inspect_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of recent runs to display (default: 10).",
    )
    inspect_parser.add_argument(
        "--backend",
        choices=("local", "firestore"),
        default=None,
        help="Override configured memory backend (`local` or `firestore`).",
    )
    return parser


def _format_runs_table(runs: list[RunMetrics]) -> str:
    """Format RunMetrics entries into a compact fixed-width console table."""
    headers = [
        "Date (UTC)",
        "Run ID",
        "Recip(ok/fail)",
        "Art(tot/dup)",
        "Clust",
        "Dev",
        "LLM",
        "Cost USD",
        "Ground%",
        "Trace ID",
    ]
    rows: list[list[str]] = []
    for run in runs:
        dt_str = ensure_utc(run.started_at).strftime("%Y-%m-%d %H:%M")
        sent_ok = max(0, run.recipients_processed - run.recipients_failed)
        recip_str = f"{sent_ok}/{run.recipients_failed}"
        art_str = f"{run.articles_gathered}/{run.articles_deduped_cross_day}"
        trace_str = run.trace_id or "-"
        rows.append(
            [
                dt_str,
                run.run_id,
                recip_str,
                art_str,
                str(run.clusters_formed),
                str(run.developing_stories_linked),
                str(run.llm_calls),
                f"${run.estimated_cost_usd:.5f}",
                f"{run.deterministic_grounding_pass_rate * 100:.1f}%",
                trace_str,
            ]
        )

    widths = [
        max(len(headers[col]), *(len(row[col]) for row in rows))
        for col in range(len(headers))
    ]
    right_align = {4, 5, 6, 7, 8}

    def _fmt_row(values: list[str]) -> str:
        cells = [
            val.rjust(widths[idx]) if idx in right_align else val.ljust(widths[idx])
            for idx, val in enumerate(values)
        ]
        return " | ".join(cells)

    separator = "-+-".join("-" * width for width in widths)
    lines = [_fmt_row(headers), separator, *(_fmt_row(row) for row in rows)]
    return "\n".join(lines)


async def _handle_run(args: argparse.Namespace, settings: Settings) -> int:
    config_path: Path | None = args.config
    if config_path is not None and not config_path.exists():
        # Allow running from env vars, memory store, or recipients.json when config.json is absent
        if str(config_path) == "config.json":
            config_path = None
        else:
            logger.error("Config file not found: %s", config_path)
            return 1

    base_dir = config_path.resolve().parent if config_path is not None else Path.cwd()
    if not args.gather_only:
        settings.apply_genai_env(base_dir=base_dir)

    telemetry = init_telemetry(settings)
    try:
        results, metrics = await run_digest_pipeline(
            settings=settings,
            config_path=config_path,
            dry_run=args.dry_run,
            gather_only=args.gather_only,
            recipient_filter=args.recipient,
        )
    finally:
        telemetry.shutdown()

    trace_suffix = f" trace_id={metrics.trace_id}" if metrics.trace_id else ""
    safe_print(
        f"\n[Run {metrics.run_id}] recipients={metrics.recipients_processed} "
        f"failed={metrics.recipients_failed} articles={metrics.articles_gathered} "
        f"deduped={metrics.articles_deduped_cross_day} clusters={metrics.clusters_formed} "
        f"developing={metrics.developing_stories_linked} scrapes={metrics.scrapes_performed} "
        f"llm_calls={metrics.llm_calls} tokens={metrics.input_tokens + metrics.output_tokens} "
        f"est_cost=${metrics.estimated_cost_usd:.5f} "
        f"grounding_pass_rate={metrics.deterministic_grounding_pass_rate:.2%}"
        f"{trace_suffix}"
    )

    if results and metrics.recipients_failed == metrics.recipients_processed:
        return 1
    return 0


async def _handle_seed_profiles(args: argparse.Namespace, settings: Settings) -> int:
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

    deduped_profiles = list({p.id: p for p in profiles}.values())

    effective_settings = (
        settings.model_copy(update={"memory_backend": args.backend})
        if args.backend is not None
        else settings
    )

    store = create_memory_store(effective_settings)
    try:
        upserted = await store.upsert_profiles(deduped_profiles)
    finally:
        await close_memory_store(store)

    enabled_count = sum(1 for p in deduped_profiles if p.enabled)
    disabled_count = len(deduped_profiles) - enabled_count
    target_desc = (
        f"local ({effective_settings.local_memory_dir})"
        if effective_settings.memory_backend == "local"
        else (
            f"firestore (project={effective_settings.gcp_project_id or 'default'}, "
            f"db={effective_settings.firestore_database})"
        )
    )
    safe_print(
        f"Seeded {upserted} recipient profile(s) "
        f"({enabled_count} enabled, {disabled_count} disabled) into {target_desc}."
    )
    return 0


async def _handle_inspect_runs(args: argparse.Namespace, settings: Settings) -> int:
    if args.limit < 1:
        logger.error("--limit must be at least 1, got %d", args.limit)
        return 1

    effective_settings = (
        settings.model_copy(update={"memory_backend": args.backend})
        if args.backend is not None
        else settings
    )
    store = create_memory_store(effective_settings)
    try:
        runs = await store.list_recent_runs(limit=args.limit)
    finally:
        await close_memory_store(store)

    target_desc = (
        f"local ({effective_settings.local_memory_dir})"
        if effective_settings.memory_backend == "local"
        else (
            f"firestore (project={effective_settings.gcp_project_id or 'default'}, "
            f"db={effective_settings.firestore_database})"
        )
    )
    if not runs:
        safe_print(f"No recorded runs found in {target_desc}.")
        return 0

    safe_print(f"Recent runs in {target_desc} (showing {len(runs)}):\n")
    safe_print(_format_runs_table(runs))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point registered as `agentic-emailer`."""
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_format, settings.gcp_project_id)

    if args.command == "run":
        try:
            return asyncio.run(_handle_run(args, settings))
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            logger.exception("Fatal pipeline error: %s", exc)
            return 1

    if args.command == "seed-profiles":
        try:
            return asyncio.run(_handle_seed_profiles(args, settings))
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            logger.exception("Failed to seed profiles: %s", exc)
            return 1

    if args.command == "inspect-runs":
        try:
            return asyncio.run(_handle_inspect_runs(args, settings))
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            logger.exception("Failed to inspect runs: %s", exc)
            return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
