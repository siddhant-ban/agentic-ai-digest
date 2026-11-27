# Agentic Workflow Log

> Auto-maintained context for AI coding agents. Updated on major milestones to preserve architecture decisions, current production state, and operational guardrails for future sessions.

## Current state

- **Project:** Agentic Emailer (`v1.0`) — Serverless multi-agent news intelligence and executive digest pipeline built with Google ADK, Vertex AI Gemini, Cloud Firestore, OpenTelemetry Cloud Trace, and Terraform.
- **Stack:** Python `>= 3.11`, `uv`, `google-adk`, `google-genai`, `google-cloud-firestore`, `opentelemetry-sdk` + `opentelemetry-exporter-gcp-trace` + `opentelemetry-instrumentation-google-genai`, `httpx`, `feedparser`, `trafilatura`, `numpy`, Pydantic v2, Terraform (`>= 1.9`), multi-stage non-root Docker (`python:3.12-slim`).
- **Package & Entry Points:**
  - `uv run agentic-emailer run [--config PATH] [--dry-run] [--gather-only] [--recipient ID]`
  - `uv run agentic-emailer seed-profiles --from PATH [--backend local|firestore]`
  - `uv run agentic-emailer inspect-runs [--limit N] [--backend local|firestore]`
  - `uv run agentic-emailer-eval --mode deterministic|full [--synthesis-model M] [--judge-model M] [--single-shot] [--markdown-summary PATH] [--output PATH]`
- **GCP Architecture (`infra/terraform/`):** Cloud Run Job (`agentic-digest-job`, 1 vCPU / 512 MiB, `us-central1`), Cloud Scheduler (`daily-agentic-digest`, `0 12 * * *` UTC), Cloud Firestore Native (programmatic 30-day pruning), Artifact Registry (keep-3 cleanup policy), Secret Manager (`agentic-emailer-smtp-password`), 3 least-privilege Service Accounts (`digest-runner-sa`, `scheduler-invoker-sa`, `github-deploy-sa`), keyless GitHub OIDC WIF restricted to `refs/heads/main`, and monthly Billing Budget alerts (\$10 cap).
- **CI/CD (`.github/workflows/`):**
  - `ci.yml`: `ruff`, `mypy`, thin zero-mock invariants (`pytest`), deterministic offline evals, Terraform fmt/validate, non-root Docker smoke check.
  - `deploy.yml`: Keyless WIF container build/push + Cloud Run Job update on pushes to `main`.
  - `eval-full.yml`: Manual (`workflow_dispatch`) and release-tag (`v*`) LLM-as-judge eval gate using `GEMINI_API_KEY` secret, publishing Markdown scorecards to `$GITHUB_STEP_SUMMARY` and uploading artifacts.
- **Verification Policy (`AGENTS.md`):** Eval-first — zero mock-LLM unit tests. Keep `tests/` restricted to fast (<2s), pure deterministic invariants and validate agent quality via `evals/` + live `--dry-run`.
- **Secrets (never commit):** `gemini_api_key.txt`, `smtp_password.txt`, `config.json`, `recipients.json`, `infra/terraform/terraform.tfvars`, `.local_memory/`.
- **Last updated:** 2026-11-27

## Active focus

- `v1.0` Milestone 4 complete locally (OpenTelemetry Cloud Trace + trace-correlated JSON logging, `inspect-runs` CLI, `eval-full.yml` CI gate, 3-way measured eval benchmark, portfolio `docs/ARCHITECTURE.md` and `README.md`).
- Next human step: run one-time GCP bootstrap (`docs/GCP_BOOTSTRAP.md`) and verify live Cloud Run Job execution, Cloud Trace waterfall, Cloud Logging JSON correlation, and `agentic-emailer inspect-runs --backend firestore`.

## Change log

Newest entries first.

---

### 2026-11-27 — Milestone 4: OpenTelemetry Cloud Trace, CI Eval Gate, Benchmark & Portfolio Polish

- **Branch:** `milestone-4/observability-and-polish`
- **Summary:** Added `observability/telemetry.py` (OTel `TracerProvider`, `CloudTraceSpanExporter`, `GoogleGenAiSdkInstrumentor`, guaranteed `force_flush(10_000)` + `shutdown()` in `finally`) and `observability/logging_config.py` (GCP Cloud Logging JSON formatter with dynamic `logging.googleapis.com/trace` correlation and `run_id`/`recipient_id` context). Instrumented `pipeline.py` with explicit stage spans (`digest.run` -> `ingestion.rss`, `agent.research`, `memory.embed_and_cluster`, `recipient.editorial_loop`, `agent.synthesis`, `verifier.deterministic`, `agent.critic`, `delivery.send`, `memory.persist_and_prune`), persisted `trace_id` into `RunMetrics`, and added `list_recent_runs` + `agentic-emailer inspect-runs`. Added `LOG_FORMAT = "json"` to `infra/terraform/cloud_run.tf`. Extended `evals/runner.py` with `--markdown-summary`, model overrides, `--single-shot` / `--no-critic` / `--no-clustering`, and isolated pipeline vs. judge cost accounting; added `.github/workflows/eval-full.yml`. Executed a live 3-way comparative benchmark (`evals/reports/experiment-model-comparison.md`) proving full agentic Flash-Lite passes all gates (`0.0000` leakage, `1.0000` developing-story recall, `5.00/5.00` judge scores, `9.9s`/case, `$0.0041`/suite) vs. single-shot failure (`0.0370` leakage, `0.0000` recall) and Flash 6.2x latency / 2.27x cost overhead. Authored `docs/ARCHITECTURE.md` and rewrote `README.md` for `v1.0`.
- **Files:** `pyproject.toml`, `uv.lock`, `src/agentic_emailer/config.py`, `src/agentic_emailer/observability/*`, `src/agentic_emailer/memory/*`, `src/agentic_emailer/pipeline.py`, `src/agentic_emailer/cli.py`, `infra/terraform/cloud_run.tf`, `evals/runner.py`, `.github/workflows/eval-full.yml`, `evals/reports/experiment-model-comparison.md`, `docs/ARCHITECTURE.md`, `README.md`, `docs/AGENTIC_WORKFLOW.md`

---

### 2026-11-26 — Milestone 3: Docker Containerization, Terraform GCP Stack & Keyless WIF CI/CD

- **Commit:** `e4690a1` (PR #4, `milestone-3/gcp-terraform-cicd`)
- **Summary:** Built multi-stage non-root `uv` `Dockerfile`, provisioned full GCP infrastructure via Terraform (`infra/terraform/`: Cloud Run Job, Cloud Scheduler, Firestore Native, Artifact Registry w/ keep-3 cleanup, Secret Manager, 3 least-privilege SAs, `refs/heads/main`-locked GitHub OIDC WIF, Billing Budgets), replaced legacy GitHub Actions workflows with `ci.yml` + `deploy.yml`, and wrote `docs/GCP_BOOTSTRAP.md`.

---

### 2026-11-26 — Milestone 2: Persistent Cross-Day Memory, Embeddings & Semantic Clustering

- **Commit:** `2c26876` (PR #3, `milestone-2/memory-and-clustering`)
- **Summary:** Implemented `MemoryStore` protocol, `LocalJsonMemoryStore`, `FirestoreMemoryStore` (zero composite indexes, chunked `AsyncWriteBatch`, programmatic 30-day pruning), batch 768-dim `gemini-embedding-001` embeddings, deterministic cosine clustering + cross-day semantic dedup + developing-story linking, `seed-profiles` CLI, and memory-aware golden eval cases.

---

### 2026-11-26 — Milestone 1: Google ADK Multi-Agent Core, Deterministic Guardrails & Eval Harness

- **Branch:** `milestone-1/adk-core-and-evals`
- **Summary:** Refactored legacy scripts into `src/agentic_emailer/`, added Pydantic v2 contracts, `RunBudgetTracker`, async RSS + SSRF-hardened `trafilatura` extractor, ADK `ResearchAgent` / `SynthesisAgent` / `CriticAgent`, deterministic grounding verifier, responsive HTML/text `EmailSender`, thin zero-mock invariants in `tests/`, and offline + LLM-as-judge eval harness in `evals/`.

---

### 2026-07-12 — Legacy `v0.x` Baseline (Archived History)

- **Commits:** `1454347` through `2a272e2`
- **Summary:** Initial stateless linear Python prototype (`aggregator.py`, `sources.py`, `summarizer.py`, `emailer.py`) scheduled via `.github/workflows/digest.yml` (fully replaced by the `v1.0` `src/agentic_emailer` package and GCP Cloud Run Jobs in Milestones 1–4).
