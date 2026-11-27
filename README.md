# Agentic Emailer

A serverless, multi-agent news intelligence and executive digest pipeline built with **Google Agent Development Kit (`google-adk`)**, **Vertex AI Gemini**, **Cloud Firestore**, **OpenTelemetry -> Cloud Trace**, and **Terraform**.

Agentic Emailer deliberately separates **deterministic engineering plumbing** (concurrent RSS ingestion, URL canonicalization, SSRF-hardened extraction, cosine clustering math, 100% URL grounding verification, SMTP retries) from **bounded LLM agency** (`ResearchAgent`, `SynthesisAgent`, `CriticAgent`), backed by an offline + CI LLM-as-judge evaluation harness.

> **Deep-Dive Architecture & Trade-Offs:** See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for Mermaid topology diagrams, design trade-offs, failure modes, and cost guardrails.

---

## Key Capabilities

- **Multi-Agent ADK Pipeline:**
  - `ResearchAgent`: Inspects live RSS coverage and selectively scrapes thin snippets (`trafilatura` behind an SSRF guard) or runs targeted DuckDuckGo searches only when topic gaps exist.
  - `SynthesisAgent`: Produces typed Pydantic `DigestDraft` executive briefings with verbatim citations and developing-story continuity.
  - `CriticAgent`: Audits semantic faithfulness and topic relevance in a bounded revision loop (`<= 2` revisions), preceded by a 100% deterministic URL/freshness/schema grounding verifier.
- **Cross-Day Memory & Semantic Story Clustering:** Uses `gemini-embedding-001` (768-dim) + in-process `numpy` cosine similarity backed by **Cloud Firestore** (or local JSON for dev/evals) to merge multi-outlet same-day stories (`>= 0.84`), suppress cross-day re-hashes on different URLs (`>= 0.94`), and link developing multi-day storylines (`0.76 <= sim < 0.94`).
- **Hard Cost & Safety Guardrails:** Thread-safe `RunBudgetTracker` caps LLM calls, tokens, scrapes, and searches per run; SSRF guards block private/metadata IP ranges; per-recipient isolation ensures one failed email never aborts other recipients.
- **Production Observability:** OpenTelemetry multi-span tracing exported to **Google Cloud Trace** (with guaranteed `force_flush(10s)` + `shutdown()` on container exit), trace-correlated **GCP Cloud Logging** JSON logs, and a built-in `inspect-runs` telemetry CLI.
- **Zero-Key Cloud Deployment:** Provisioned via Terraform (`infra/terraform/`) as a **Cloud Run Job** + **Cloud Scheduler** trigger with least-privilege IAM, GitHub OIDC Workload Identity Federation, and **\$0/month out-of-pocket** operating cost inside GCP Always Free + \$10/mo Google AI Pro credits.

---

## Measured Evaluation Scorecard

Benchmarked live on the frozen golden corpus (3 recipient profiles + 2 `ResearchAgent` tool-trajectory scenarios) using an independent `gemini-3.5-flash` LLM-as-judge. Full methodology and per-case results are documented in [`evals/reports/experiment-model-comparison.md`](evals/reports/experiment-model-comparison.md).

| Metric | Single-Shot Baseline (`gemini-3.1-flash-lite`) | Full Agentic Default (`gemini-3.1-flash-lite`) | Full Agentic Upgraded (`gemini-3.5-flash`) | Gate |
|---|---:|---:|---:|---|
| `url_grounding_rate` | `1.0000` | **`1.0000`** | `1.0000` | `== 1.0000` |
| `duplicate_leakage_rate` | `0.0370` (FAIL) | **`0.0000`** | `0.0000` | `== 0.0000` |
| `developing_story_recall` | `0.0000` (FAIL) | **`1.0000`** | `1.0000` | `>= 0.8000` |
| `topic_coverage_rate` | `1.0000` | **`1.0000`** | `1.0000` | `>= 0.8500` |
| `trajectory_precision` | `1.0000` | **`1.0000`** | `1.0000` | `>= 0.8000` |
| `avg_latency_ms` / case | `11,580.1 ms` | **`9,943.8 ms`** | `61,715.6 ms` | info |
| `estimated_cost_usd` (suite) | `$0.002782` | **`$0.004113`** | `$0.009338` | info |
| Judge (`Faith / Rel / Syn / Conc`) | `5.0 / 5.0 / 5.0 / 5.0` | **`5.0 / 5.0 / 5.0 / 5.0`** | `5.0 / 5.0 / 5.0 / 5.0` | `>= 4.0` |
| **Hard Gate Status** | **FAILED** | **PASSED** | **PASSED** | All pass |

---

## GCP Cost Summary

Designed for 2–20 curated recipients/day in `us-central1` (Tier-1 Always Free):
- **Cloud Run Jobs** (1 vCPU / 512 MiB, ~3 min/day), **Cloud Scheduler** (1 job), **Cloud Firestore** (programmatic 30-day pruning), **Secret Manager** (1 secret), **Artifact Registry** (keep-3 cleanup policy), **Cloud Trace**, and **Cloud Logging**: **\$0.00/month** (Always Free).
- **Vertex AI Gemini + Embeddings**: **~\$0.50–\$2.00/month**, covered by \$10/month Google AI Pro developer credits (guarded by Terraform Billing Budget alerts at \$2/\$5/\$8/\$10).
- **Total Out-of-Pocket:** **\$0.00/month**.

---

## Local Quickstart

### Prerequisites
- Python `>= 3.11` and [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- A Gemini API key (`GEMINI_API_KEY` env var or gitignored `gemini_api_key.txt`) for local runs, or Vertex AI IAM auth (`GOOGLE_GENAI_USE_VERTEXAI=true`)
- For live email sending: Gmail App Password (`EMAIL_PASSWORD` env var or gitignored `smtp_password.txt`)

### 1. Install Dependencies

```powershell
uv sync --all-groups
```

### 2. Configure Profiles & Run Locally

Copy `recipients.example.json` to `recipients.json` (gitignored) or use `config.ci.json` for a smoke run:

```powershell
# Gather and print normalized RSS articles only (zero LLM calls)
uv run agentic-emailer run --config config.ci.json --gather-only

# Run the full ADK pipeline + semantic clustering + critic loop without sending email
uv run agentic-emailer run --config config.ci.json --dry-run

# Inspect recorded local (or Firestore) RunMetrics and trace IDs
uv run agentic-emailer inspect-runs --limit 10
uv run agentic-emailer inspect-runs --backend firestore --limit 10

# Seed/update recipient profiles into LocalJsonMemoryStore or Firestore
uv run agentic-emailer seed-profiles --from recipients.json --backend local

# Execute live delivery (renders HTML + plain text and sends via SMTP)
uv run agentic-emailer run
```

To test GCP trace-correlated structured JSON logging locally:

```powershell
$env:LOG_FORMAT = "json"
$env:GCP_PROJECT_ID = "your-gcp-project-id"
uv run agentic-emailer run --config config.ci.json --dry-run
```

---

## Evaluation & Quality Verification

This repository follows an **eval-first verification policy** (zero mock-LLM unit tests):

```powershell
# Static analysis & thin zero-mock deterministic invariants (<2s)
uv run ruff check src tests evals
uv run mypy src evals
uv run pytest -v

# Zero-cost deterministic offline eval (runs on every PR in CI)
uv run agentic-emailer-eval --mode deterministic

# Full live ADK + LLM-as-judge eval with scorecard artifacts
uv run agentic-emailer-eval --mode full --output eval-report.json --markdown-summary eval-summary.md

# Benchmark an alternative synthesis model or single-shot baseline
uv run agentic-emailer-eval --mode full --synthesis-model gemini-3.5-flash --judge-model gemini-3.5-flash
uv run agentic-emailer-eval --mode full --single-shot --judge-model gemini-3.5-flash
```

---

## Production GCP Deployment & CI/CD

Full step-by-step bootstrap instructions (Terraform provisioning, Artifact Registry, Secret Manager, Firestore profile seeding, and keyless WIF GitHub Actions setup) live in [`docs/GCP_BOOTSTRAP.md`](docs/GCP_BOOTSTRAP.md).

### GitHub Actions Workflows
- **`.github/workflows/ci.yml`** (PRs & `main`): Runs `ruff`, `mypy`, deterministic invariants (`pytest`), deterministic offline evals, `terraform fmt -check` + `terraform validate`, and a non-root Docker build smoke check.
- **`.github/workflows/deploy.yml`** (`main` push & manual): Authenticates via keyless GitHub OIDC Workload Identity Federation (`refs/heads/main` only), builds and pushes the container image to Artifact Registry, and updates `agentic-digest-job`.
- **`.github/workflows/eval-full.yml`** (manual dispatch & `v*` tags): Runs the full live LLM-as-judge eval suite (with optional `synthesis_model`, `judge_model`, and `single_shot` inputs), posts the Markdown scorecard directly to GitHub Step Summary, and uploads JSON/Markdown artifacts.

---

## Repository Structure

```text
agentic_emailer/
├── src/agentic_emailer/
│   ├── cli.py                # CLI: run | seed-profiles | inspect-runs
│   ├── config.py             # Typed Pydantic Settings (models, GCP, budgets, OTel, SMTP)
│   ├── models.py             # Pydantic v2 domain contracts
│   ├── budget.py             # Hard per-run LLM/token/scrape/search ceilings
│   ├── pipeline.py           # Async stage orchestrator + bounded editorial loop + OTel spans
│   ├── ingestion/            # Concurrent RSS, DuckDuckGo search, SSRF-safe trafilatura extractor
│   ├── memory/               # MemoryStore Protocol, LocalJson, Firestore, embeddings, cosine clustering
│   ├── agents/               # ADK ResearchAgent, SynthesisAgent, CriticAgent, deterministic verifier
│   ├── delivery/             # EmailSender Protocol, responsive HTML/text renderer, Gmail SMTP adapter
│   └── observability/        # OpenTelemetry Cloud Trace setup + trace-correlated Cloud Logging JSON
├── evals/                    # Golden corpus, trajectory cases, deterministic metrics, LLM-as-judge, reports
├── tests/                    # Thin zero-mock deterministic invariants (dedup, grounding, budget/SSRF)
├── infra/terraform/          # Cloud Run Job, Scheduler, Firestore, AR cleanup, IAM/WIF, Billing Budgets
├── .github/workflows/        # ci.yml, deploy.yml, eval-full.yml
├── Dockerfile                # Multi-stage non-root python:3.12-slim + uv container
└── docs/                     # ARCHITECTURE.md, GCP_BOOTSTRAP.md, AGENTIC_WORKFLOW.md, plans/
```
