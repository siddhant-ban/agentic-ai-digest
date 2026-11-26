# Milestone 4: OpenTelemetry Cloud Trace, Online Telemetry, CI Eval Gate & Portfolio Polish

> **Antigravity Workflow:** Execute after Milestone 3 merges and GCP bootstrap succeeds, in a fresh conversation with `/goal` on branch `milestone-4/observability-and-polish`. Commit cleanly at each task checkpoint and run a final fresh-context review pass before merging.  
> **Master Spec:** `docs/plans/DESIGN_SPEC.md`

> [!IMPORTANT]
> **Verification Policy (Eval-First — NO Mock-Heavy TDD)**
> - **DO NOT** write unit tests mocking OpenTelemetry exporters or Cloud Trace gRPC clients.
> - **DO** verify observability via structured JSON log inspection locally, live Cloud Run Job execution showing a complete trace waterfall in GCP Cloud Trace, and a documented model/prompt comparison scorecard produced by `evals/runner.py`.

**Goal:** Instrument the Google ADK pipeline with production **OpenTelemetry -> Google Cloud Trace** tracing and trace-correlated **GCP Cloud Logging** JSON logs, add an on-demand/release **LLM-as-Judge CI Eval workflow**, run a real **model/architecture benchmark experiment** (documenting concrete eval numbers for your resume!), and polish `docs/ARCHITECTURE.md` and `README.md` into an interview-ready portfolio showcase.

**Tech Stack:** `opentelemetry-sdk`, `opentelemetry-exporter-cloud-trace`, `opentelemetry-instrumentation-google-genai`, GCP Cloud Logging JSON format, GitHub Actions eval workflow.

---

## File Structure Map

- **Modify:** `pyproject.toml` (add `opentelemetry-sdk`, `opentelemetry-exporter-cloud-trace`, `opentelemetry-instrumentation-google-genai`)
- **Create:** `src/agentic_emailer/observability/__init__.py`
- **Create:** `src/agentic_emailer/observability/logging_config.py` (GCP Cloud Logging JSON formatter with `logging.googleapis.com/trace` correlation)
- **Create:** `src/agentic_emailer/observability/telemetry.py` (OTel `TracerProvider`, `CloudTraceSpanExporter`, `GoogleGenAiSdkInstrumentor`, and guaranteed `force_flush()` / `shutdown()` hook for Cloud Run Jobs)
- **Modify:** `src/agentic_emailer/config.py` (add `otel_cloud_trace_enabled: bool = False`, `log_format: Literal["text", "json"] = "text"`)
- **Modify:** `src/agentic_emailer/pipeline.py` (wrap pipeline stages in explicit OTel spans, attach `trace_id` to `RunMetrics`, ensure `telemetry.shutdown()` runs in `finally`)
- **Modify:** `src/agentic_emailer/cli.py` (initialize logging & telemetry at startup, add `inspect-runs` subcommand to display recent Firestore/local `RunMetrics`)
- **Create:** `.github/workflows/eval-full.yml` (manual + release-tag LLM-as-judge eval workflow posting Markdown scorecard summary to GitHub Step Summary and uploading JSON artifact)
- **Create:** `docs/ARCHITECTURE.md` (portfolio-ready system design doc: Mermaid diagrams, deterministic-vs-agentic rationale, cost table, failure modes, eval results table)
- **Modify:** `README.md` (clean production README highlighting agentic design, eval scorecard, GCP architecture, and quickstart)
- **Modify:** `docs/AGENTIC_WORKFLOW.md` (update current state to v1.0 production architecture)

---

### Task 1: OpenTelemetry Cloud Trace & Trace-Correlated Structured JSON Logging

**Files:**
- Modify: `pyproject.toml`
- Create: `src/agentic_emailer/observability/__init__.py`
- Create: `src/agentic_emailer/observability/logging_config.py`
- Create: `src/agentic_emailer/observability/telemetry.py`
- Modify: `src/agentic_emailer/config.py`

**Instructions:**
1. Add `opentelemetry-sdk>=1.30.0`, `opentelemetry-exporter-cloud-trace>=1.8.0`, and `opentelemetry-instrumentation-google-genai>=0.1.0` to `pyproject.toml` and run `uv sync --all-groups`.
2. **`observability/logging_config.py`:**
   - Implement `configure_logging(log_format: Literal["text", "json"], project_id: str | None = None)`:
     - When `json` (Cloud Run default): emit single-line JSON with `severity` (`INFO`/`WARNING`/`ERROR`), `message`, `logger`, `timestamp`, optional `run_id`/`recipient_id` extra fields, and `logging.googleapis.com/trace` formatted as `projects/{project_id}/traces/{trace_id_hex}` pulled dynamically from `opentelemetry.trace.get_current_span()`.
     - When `text` (local dev default): clean human-readable console logs.
3. **`observability/telemetry.py`:**
   - Implement `TelemetryHandle` with `init_telemetry(settings: Settings) -> TelemetryHandle`:
     - Sets `OTEL_SERVICE_NAME = "agentic-emailer"`.
     - Initializes `TracerProvider` and attaches `BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.gcp_project_id))` when `settings.otel_cloud_trace_enabled` is `True` (and no-op/local provider when `False`).
     - Calls `GoogleGenAiSdkInstrumentor().instrument()` to capture Gemini token & latency spans automatically.
   - Implement `TelemetryHandle.shutdown()`: **explicitly calls `provider.force_flush(timeout_millis=10_000)` then `provider.shutdown()`** so Cloud Run Job container exit never loses spans.
   - Implement `get_current_trace_id() -> str | None`.
4. Add `otel_cloud_trace_enabled: bool = False` and `log_format: Literal["text", "json"] = "text"` to `Settings`, and set `LOG_FORMAT = "json"` in `infra/terraform/cloud_run.tf`.

**Verification Commands:**
```powershell
uv sync --all-groups
uv run ruff check src/agentic_emailer/observability src/agentic_emailer/config.py
uv run mypy src/agentic_emailer/observability src/agentic_emailer/config.py
uv run python -c "from agentic_emailer.observability.logging_config import configure_logging; import logging; configure_logging('json', 'demo-proj'); logging.getLogger('test').info('hello')"
```
Expected: Valid JSON log line printed with `"severity": "INFO"`.

**Commit:** `feat: add OpenTelemetry Cloud Trace setup and GCP trace-correlated JSON logging`

---

### Task 2: Instrument Pipeline Spans & Add `inspect-runs` Telemetry CLI

**Files:**
- Modify: `src/agentic_emailer/pipeline.py`
- Modify: `src/agentic_emailer/cli.py`
- Modify: `infra/terraform/cloud_run.tf` (add `LOG_FORMAT = "json"` env var)

**Instructions:**
1. In `pipeline.py`:
   - Wrap the full execution in a root span `digest.run` (setting span attributes: `run_id`, `recipient_count`, `dry_run`).
   - Wrap each pipeline stage in child spans: `ingestion.rss`, `agent.research`, `memory.embed_and_cluster`, `recipient.editorial_loop` (with `recipient_id` and `revision_count` attributes), `verifier.deterministic`, `delivery.send`, and `memory.persist_and_prune`.
   - Record `trace_id = get_current_trace_id()` into `RunMetrics` before persisting to Firestore/local store.
2. In `cli.py`:
   - Initialize `configure_logging(...)` and `telemetry = init_telemetry(settings)` in `run`, wrapped in `try ... finally: telemetry.shutdown()`.
   - Add `agentic-emailer inspect-runs [--limit 10] [--backend local|firestore]` command that prints a crisp table of recent runs (date, recipients sent/failed, articles gathered/deduped, clusters, developing stories, LLM calls, estimated cost USD, grounding pass rate, trace ID).

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer
uv run mypy src/agentic_emailer
uv run agentic-emailer run --config config.ci.json --dry-run
uv run agentic-emailer inspect-runs --limit 5
```
Expected: Dry-run completes cleanly and `inspect-runs` renders the run table.

**Commit:** `feat: instrument ADK pipeline with OTel spans and add inspect-runs CLI`

---

### Task 3: On-Demand & Release LLM-as-Judge CI Eval Workflow

**Files:**
- Create: `.github/workflows/eval-full.yml`

**Instructions:**
1. Create `.github/workflows/eval-full.yml`:
   - Triggers on `workflow_dispatch` (with optional `synthesis_model` input so you can benchmark different Gemini models directly from GitHub Actions!) and on version tags `v*`.
   - Authenticates to Vertex AI via keyless WIF (reusing `GCP_WORKLOAD_IDENTITY_PROVIDER` / `GCP_DEPLOY_SA_EMAIL` or a dedicated eval SA with `roles/aiplatform.user`), OR falls back to `GEMINI_API_KEY` secret if configured.
   - Runs `uv run agentic-emailer-eval --mode full --output eval-report.json --markdown-summary eval-summary.md`.
   - Appends `eval-summary.md` straight to `$GITHUB_STEP_SUMMARY` so every eval run displays a rich Markdown scorecard table directly in the GitHub Actions UI.
   - Uploads `eval-report.json` as a downloadable build artifact.

**Verification Commands:**
```powershell
uv run agentic-emailer-eval --mode deterministic
```
Expected: Deterministic eval passes with exit code 0.

**Commit:** `ci: add manual and release LLM-as-judge eval workflow with scorecard summaries`

---

### Task 4: Run Comparative Eval Experiment & Author Portfolio Documentation

**Files:**
- Create: `docs/ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `docs/AGENTIC_WORKFLOW.md`
- Create: `evals/reports/experiment-model-comparison.md` (generated from actual eval runs!)

**Instructions:**
1. **Run a real, defensible engineering experiment using `evals/`:**
   - Run `agentic-emailer-eval --mode full` with:
     - **Config A (Baseline — Single-shot / no critic loop, or Flash-Lite synthesis):** record exact grounding rate, faithfulness, synthesis quality, latency, and estimated cost per digest.
     - **Config B (Full Agentic — ResearchAgent + Clustering + Critic Loop, and optionally Flash vs. Flash-Lite comparison):** record exact deltas across every metric.
   - Save the real measured numbers in `evals/reports/experiment-model-comparison.md` (NEVER fabricate metrics—only report numbers produced by actual runs!).
2. **Write `docs/ARCHITECTURE.md` (Resume / Interview Deep-Dive Artifact):**
   - High-level Mermaid architecture diagram (matching deployed GCP topology).
   - Clear **Design Trade-Offs** section (why deterministic plumbing + bounded ADK agents instead of unconstrained autonomous loops; why Firestore + in-process cosine clustering over a dedicated vector DB to stay inside \$10/month; why eval-first verification + thin invariants over mock-heavy TDD; why Gmail SMTP behind `EmailSender` protocol; why keyless WIF over service account JSON keys).
   - Failure Modes & Guardrails table (SSRF protection, runaway-loop budget cap, deterministic URL verifier preventing hallucinations, per-recipient failure isolation, graceful fallback digest, Cloud Run OTel `force_flush`).
   - Actual measured Eval Scorecard & Cost Breakdown table.
3. **Rewrite `README.md`:**
   - Lead with a crisp portfolio-ready overview, architecture diagram link, live eval scorecard summary, GCP cost summary (\$0 out-of-pocket inside Always Free + Google AI Pro credits), local quickstart, eval commands, and GCP deployment pointer (`docs/GCP_BOOTSTRAP.md`).
4. **Update `docs/AGENTIC_WORKFLOW.md`** so future agent sessions have the accurate `v1.0` project state.

**Verification Commands:**
```powershell
uv run ruff check src tests evals
uv run mypy src evals
uv run pytest -v
uv run agentic-emailer-eval --mode full
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform validate
```
Expected: All static checks, invariants, Terraform validations, and live full eval gates pass cleanly.

**Commit:** `docs: add portfolio architecture guide, measured eval benchmark, and v1 README`

---

## Milestone 4 Exit Gate (Project Production-Ready!)
- [ ] A live Cloud Run Job execution writes a full multi-span waterfall to **GCP Cloud Trace** and correlated JSON logs to **Cloud Logging**.
- [ ] `uv run agentic-emailer inspect-runs --backend firestore` displays recorded production run metrics and trace IDs.
- [ ] `evals/reports/experiment-model-comparison.md` contains real, reproducible evaluation metrics proving the agentic architecture's grounding and quality gains.
- [ ] `README.md` and `docs/ARCHITECTURE.md` accurately reflect the deployed system and are ready to link directly on your resume.
- [ ] Final fresh-context review pass confirms telemetry flushing, accurate documentation, and zero leaked secrets before merge.
