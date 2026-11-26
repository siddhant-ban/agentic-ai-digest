"""Offline evaluation CLI runner (`agentic-emailer-eval`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic_emailer.agents.research_agent import run_research_stage
from agentic_emailer.budget import RunBudgetTracker
from agentic_emailer.config import Settings
from agentic_emailer.delivery.renderer import build_fallback_draft
from agentic_emailer.ingestion.rss import make_article_id, normalize_url
from agentic_emailer.models import DigestDraft, RawArticle, RecipientProfile
from agentic_emailer.pipeline import build_singleton_clusters, run_editorial_loop, safe_print
from evals.judges import evaluate_digest_with_judge
from evals.metrics import (
    CaseEvalResult,
    EvalReport,
    EvalSummary,
    TrajectoryEvalResult,
    aggregate_metrics,
    compute_case_metrics,
    evaluate_trajectory_case,
)

logger = logging.getLogger(__name__)

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_GOLDEN_CORPUS = EVALS_DIR / "datasets" / "golden_corpus.json"
DEFAULT_TRAJECTORY_CASES = EVALS_DIR / "datasets" / "trajectory_cases.json"
DEFAULT_BASELINE_PATH = EVALS_DIR / "reports" / "baseline.json"

HARD_GATE_URL_GROUNDING = 1.0
HARD_GATE_DUPLICATE_LEAKAGE = 0.0
HARD_GATE_SCHEMA_VALIDITY = 1.0
HARD_GATE_FAITHFULNESS = 4.0


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _hydrate_article(raw: dict[str, Any]) -> RawArticle:
    url = str(raw["url"]).strip()
    canonical = normalize_url(url)
    article_id = str(raw.get("article_id") or make_article_id(canonical))
    return RawArticle(
        article_id=article_id,
        canonical_url=canonical,
        url=url,
        title=str(raw["title"]),
        source=str(raw["source"]),
        published_at=raw.get("published_at"),
        snippet=str(raw.get("snippet") or ""),
        full_text=raw.get("full_text"),
        discovered_via=raw.get("discovered_via", "rss"),
        topic_hints=list(raw.get("topic_hints") or []),
    )


def _check_hard_gates(summary: EvalSummary, mode: str) -> list[str]:
    failures: list[str] = []
    if summary.schema_validity_rate < HARD_GATE_SCHEMA_VALIDITY:
        failures.append(
            f"schema_validity_rate {summary.schema_validity_rate:.4f} < {HARD_GATE_SCHEMA_VALIDITY}"
        )
    if summary.url_grounding_rate < HARD_GATE_URL_GROUNDING:
        failures.append(
            f"url_grounding_rate {summary.url_grounding_rate:.4f} < {HARD_GATE_URL_GROUNDING}"
        )
    if summary.duplicate_leakage_rate > HARD_GATE_DUPLICATE_LEAKAGE:
        failures.append(
            f"duplicate_leakage_rate {summary.duplicate_leakage_rate:.4f} > "
            f"{HARD_GATE_DUPLICATE_LEAKAGE}"
        )
    if mode == "full":
        if summary.mean_faithfulness is None or summary.mean_faithfulness < HARD_GATE_FAITHFULNESS:
            failures.append(
                f"mean_faithfulness {summary.mean_faithfulness} < {HARD_GATE_FAITHFULNESS}"
            )
    return failures


def _format_scorecard_markdown(
    report: EvalReport,
    baseline: EvalReport | None,
) -> str:
    base_summary = baseline.summary if baseline else None

    def _fmt(val: float | None, decimals: int = 4) -> str:
        if val is None:
            return "n/a"
        return f"{val:.{decimals}f}"

    def _delta(curr: float | None, prev: float | None, decimals: int = 4) -> str:
        if curr is None or prev is None:
            return "—"
        diff = curr - prev
        sign = "+" if diff >= 0 else ""
        return f"{sign}{diff:.{decimals}f}"

    s = report.summary
    b = base_summary

    def _row(
        metric: str,
        dec: int,
        gate: str,
    ) -> tuple[str, float | None, float | None, int, str]:
        curr_val: float | None = getattr(s, metric)
        prev_val: float | None = getattr(b, metric) if b is not None else None
        return (metric, curr_val, prev_val, dec, gate)

    rows = [
        _row("schema_validity_rate", 4, "== 1.0000"),
        _row("url_grounding_rate", 4, "== 1.0000"),
        _row("duplicate_leakage_rate", 4, "== 0.0000"),
        _row("topic_coverage_rate", 4, ">= 0.8500"),
        _row("trajectory_precision", 4, ">= 0.8000"),
        _row("avg_revisions", 2, "info"),
        _row("avg_latency_ms", 1, "info"),
        _row("estimated_cost_usd", 6, "info"),
        _row("mean_faithfulness", 2, ">= 4.00"),
        _row("mean_relevance", 2, ">= 4.00"),
        _row("mean_synthesis_quality", 2, ">= 3.80"),
        _row("mean_conciseness", 2, ">= 4.00"),
    ]

    lines = [
        f"## Agentic Emailer Eval Scorecard (`mode={report.mode}`)",
        "",
        "| Metric | Current | Baseline | Delta | Target / Gate |",
        "|---|---:|---:|---:|---|",
    ]
    for name, curr, prev, dec, gate in rows:
        lines.append(
            f"| `{name}` | {_fmt(curr, dec)} | {_fmt(prev, dec)} | "
            f"{_delta(curr, prev, dec)} | {gate} |"
        )

    lines.append("")
    lines.append("### Per-Case Breakdown")
    for case in report.cases:
        judge_str = ""
        if case.judge is not None:
            judge_str = (
                f" | judge(F={case.judge.faithfulness}, R={case.judge.relevance}, "
                f"S={case.judge.synthesis_quality}, C={case.judge.conciseness})"
            )
        lines.append(
            f"- **{case.case_id}**: schema={case.schema_valid}, "
            f"grounding={case.url_grounding_rate:.2f}, leakage={case.duplicate_leakage_rate:.2f}, "
            f"topic_cov={case.topic_coverage_rate:.2f}, rev={case.revisions}, "
            f"latency={case.latency_ms:.0f}ms{judge_str}"
        )

    if report.trajectories:
        lines.append("")
        lines.append("### ResearchAgent Trajectory Breakdown")
        for traj in report.trajectories:
            lines.append(
                f"- **{traj.case_id}**: precision={traj.trajectory_precision:.2f}, "
                f"scrapes={traj.scrape_calls}, searches={len(traj.search_calls)}, "
                f"expected_hits={traj.expected_scrape_hits}, "
                f"forbidden_hits={traj.forbidden_scrape_hits}"
            )

    lines.append("")
    status_label = "PASSED" if report.gates_passed else "FAILED"
    lines.append(f"**Hard Gate Status:** {status_label}")
    if report.gate_failures:
        for failure in report.gate_failures:
            lines.append(f"- FAIL: {failure}")

    return "\n".join(lines)


def run_deterministic_eval(
    corpus_path: Path,
    trajectory_path: Path,
    baseline: EvalReport | None,
) -> EvalReport:
    """Run zero-LLM deterministic grounding, schema, dedup, and dataset validation checks."""
    corpus_data = _load_json(corpus_path)
    trajectory_data = _load_json(trajectory_path)

    cached_drafts: dict[str, DigestDraft] = {}
    if baseline is not None:
        for case in baseline.cases:
            if case.draft is not None:
                cached_drafts[case.case_id] = case.draft

    case_results: list[CaseEvalResult] = []
    for raw_case in corpus_data["cases"]:
        case_id = str(raw_case["case_id"])
        t0 = time.perf_counter()
        profile = RecipientProfile.model_validate(raw_case["recipient"])
        articles = [_hydrate_article(a) for a in raw_case["articles"]]
        seen_urls = set(raw_case.get("seen_urls") or [])
        available_topics = list(raw_case.get("available_topics") or profile.topics)

        normalized_seen = {normalize_url(u) for u in seen_urls} | seen_urls
        unseen_articles = [
            a
            for a in articles
            if a.canonical_url not in normalized_seen
            and normalize_url(a.url) not in normalized_seen
        ]

        draft = cached_drafts.get(case_id) or build_fallback_draft(
            unseen_articles,
            profile.topics,
            max_highlights=profile.max_highlights,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        case_results.append(
            compute_case_metrics(
                case_id,
                draft,
                articles,
                seen_urls,
                profile.topics,
                available_topics=available_topics,
                max_highlights=profile.max_highlights,
                revisions=0,
                latency_ms=latency_ms,
                used_fallback=case_id not in cached_drafts,
            )
        )

    # Validate trajectory dataset schemas and deterministically re-score cached trajectories
    trajectory_results: list[TrajectoryEvalResult] = []
    for raw_traj in trajectory_data["cases"]:
        hydrated = [_hydrate_article(a) for a in raw_traj["articles"]]
        hydrated_ids = {a.article_id for a in hydrated}
        expected_ids = list(raw_traj.get("expected_scrape_ids") or [])
        forbidden_ids = list(raw_traj.get("forbidden_scrape_ids") or [])
        missing_ids = (set(expected_ids) | set(forbidden_ids)) - hydrated_ids
        if missing_ids:
            raise ValueError(
                f"Trajectory case {raw_traj['case_id']!r} references unknown article_ids: "
                f"{sorted(missing_ids)}"
            )
        traj_list = baseline.trajectories if baseline else []
        cached_traj = next(
            (t for t in traj_list if t.case_id == raw_traj["case_id"]),
            None,
        )
        if cached_traj is not None:
            replayed_steps: list[dict[str, Any]] = [
                {"name": "fetch_article_content", "args": {"article_id": art_id}}
                for art_id in cached_traj.scrape_calls
            ] + [
                {"name": "search_web_news", "args": dict(call_args)}
                for call_args in cached_traj.search_calls
            ]
            trajectory_results.append(
                evaluate_trajectory_case(
                    str(raw_traj["case_id"]),
                    replayed_steps,
                    expected_scrape_ids=expected_ids,
                    forbidden_scrape_ids=forbidden_ids,
                    max_searches=int(raw_traj.get("max_searches", 0)),
                    max_scrapes=raw_traj.get("max_scrapes"),
                    latency_ms=cached_traj.latency_ms,
                )
            )

    summary = aggregate_metrics(case_results, trajectory_results, estimated_cost_usd=0.0)
    failures = _check_hard_gates(summary, mode="deterministic")
    return EvalReport(
        mode="deterministic",
        timestamp_utc=datetime.now(UTC).isoformat(),
        summary=summary,
        cases=case_results,
        trajectories=trajectory_results,
        gates_passed=len(failures) == 0,
        gate_failures=failures,
    )


async def run_full_eval(
    corpus_path: Path,
    trajectory_path: Path,
    settings: Settings,
    *,
    judge_model: str | None = None,
) -> EvalReport:
    """Execute live ADK Synthesis/Verifier/Critic + ResearchAgent trajectories + LLM-as-judge."""
    settings.apply_genai_env(base_dir=EVALS_DIR.parent)
    resolved_judge_model = judge_model or settings.eval_judge_model or settings.critic_model
    corpus_data = _load_json(corpus_path)
    trajectory_data = _load_json(trajectory_path)

    budget = RunBudgetTracker(
        max_llm_calls=settings.max_llm_calls_per_run,
        max_total_tokens=settings.max_total_tokens_per_run,
        max_scrapes=settings.max_article_scrapes_per_run,
        max_searches=settings.max_search_calls_per_run,
    )

    case_results: list[CaseEvalResult] = []
    for raw_case in corpus_data["cases"]:
        case_id = str(raw_case["case_id"])
        profile = RecipientProfile.model_validate(raw_case["recipient"])
        articles = [_hydrate_article(a) for a in raw_case["articles"]]
        seen_urls = set(raw_case.get("seen_urls") or [])
        available_topics = list(raw_case.get("available_topics") or profile.topics)

        # Include all case articles (including any planted stale URLs) so SynthesisAgent
        # prompt adherence AND deterministic verifier stale-URL blocking are tested end-to-end.
        clusters = build_singleton_clusters(articles)

        logger.info("Running full eval case: %s (%d clusters)", case_id, len(clusters))
        t0 = time.perf_counter()
        editorial = await run_editorial_loop(
            profile,
            clusters,
            seen_urls,
            settings,
            budget,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0

        judge_scorecard = await evaluate_digest_with_judge(
            editorial.draft,
            articles,
            profile.topics,
            seen_urls,
            model=resolved_judge_model,
            budget=budget,
        )

        case_results.append(
            compute_case_metrics(
                case_id,
                editorial.draft,
                articles,
                seen_urls,
                profile.topics,
                available_topics=available_topics,
                max_highlights=profile.max_highlights,
                revisions=editorial.revisions,
                latency_ms=latency_ms,
                used_fallback=editorial.used_fallback,
                judge=judge_scorecard,
            )
        )

    trajectory_results: list[TrajectoryEvalResult] = []
    for raw_traj in trajectory_data["cases"]:
        case_id = str(raw_traj["case_id"])
        topics = list(raw_traj["topics"])
        articles = [_hydrate_article(a) for a in raw_traj["articles"]]
        fixture_texts: dict[str, str] = dict(raw_traj.get("fixture_full_texts") or {})
        fixture_search_articles = [
            _hydrate_article(a) for a in raw_traj.get("fixture_search_results") or []
        ]

        async def _fixture_extract(
            url: str,
            max_chars: int,
            *,
            texts: dict[str, str] = fixture_texts,
        ) -> str | None:
            body = texts.get(url) or texts.get(normalize_url(url))
            return body[:max_chars] if body else None

        def _fixture_search(
            query: str,
            lookback_hours: int,
            max_results: int,
            topic: str | None,
            *,
            results: list[RawArticle] = fixture_search_articles,
        ) -> list[RawArticle]:
            del query, lookback_hours, topic
            return results[:max_results]

        logger.info("Running ResearchAgent trajectory eval case: %s", case_id)
        t0 = time.perf_counter()
        _, trajectory = await run_research_stage(
            articles,
            topics,
            lookback_hours=36,
            settings=settings,
            budget=budget,
            search_fn=_fixture_search,
            extract_fn=_fixture_extract,
        )
        traj_ms = (time.perf_counter() - t0) * 1000.0

        trajectory_results.append(
            evaluate_trajectory_case(
                case_id,
                trajectory,
                expected_scrape_ids=list(raw_traj.get("expected_scrape_ids") or []),
                forbidden_scrape_ids=list(raw_traj.get("forbidden_scrape_ids") or []),
                max_searches=int(raw_traj.get("max_searches", 0)),
                max_scrapes=raw_traj.get("max_scrapes"),
                latency_ms=traj_ms,
            )
        )

    summary = aggregate_metrics(
        case_results,
        trajectory_results,
        estimated_cost_usd=budget.estimated_cost_usd(settings.synthesis_model),
    )
    failures = _check_hard_gates(summary, mode="full")

    return EvalReport(
        mode="full",
        model_config_snapshot={
            "research_model": settings.research_model,
            "synthesis_model": settings.synthesis_model,
            "critic_model": settings.critic_model,
            "judge_model": resolved_judge_model,
        },
        timestamp_utc=datetime.now(UTC).isoformat(),
        summary=summary,
        cases=case_results,
        trajectories=trajectory_results,
        gates_passed=len(failures) == 0,
        gate_failures=failures,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-emailer-eval",
        description="Offline evaluation harness for Agentic Emailer.",
    )
    parser.add_argument(
        "--mode",
        choices=("deterministic", "full"),
        default="deterministic",
        help="Evaluation mode: deterministic (zero LLM cost) or full (live ADK + LLM-as-judge).",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_GOLDEN_CORPUS,
        help="Path to golden corpus dataset JSON.",
    )
    parser.add_argument(
        "--trajectories",
        type=Path,
        default=DEFAULT_TRAJECTORY_CASES,
        help="Path to ResearchAgent trajectory cases JSON.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE_PATH,
        help="Path to baseline report JSON for delta comparison.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write full eval JSON report.",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=None,
        help=(
            "Optional Gemini model override for LLM-as-judge "
            "(default: EVAL_JUDGE_MODEL or critic_model)."
        ),
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Write this run's JSON report to --baseline (default: evals/reports/baseline.json).",
    )
    return parser


def _run_cli(args: argparse.Namespace) -> int:
    baseline_report: EvalReport | None = None
    if args.baseline and args.baseline.exists():
        try:
            baseline_report = EvalReport.model_validate_json(
                args.baseline.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning("Could not load baseline report %s: %s", args.baseline, exc)

    if args.mode == "deterministic":
        report = run_deterministic_eval(args.corpus, args.trajectories, baseline_report)
    else:
        settings = Settings()
        report = asyncio.run(
            run_full_eval(
                args.corpus,
                args.trajectories,
                settings,
                judge_model=args.judge_model,
            )
        )

    safe_print(_format_scorecard_markdown(report, baseline_report))

    serialized = report.model_dump_json(indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
        logger.info("Wrote eval report to %s", args.output)

    if args.save_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(serialized, encoding="utf-8")
        logger.info("Saved baseline report to %s", args.baseline)

    return 0 if report.gates_passed else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point registered as `agentic-emailer-eval`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run_cli(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.exception("Eval runner failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
