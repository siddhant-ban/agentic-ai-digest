# Comparative Evaluation Report: Architecture & Model Benchmark

> **Date:** 2026-11-27 (UTC)  
> **Dataset:** Frozen golden corpus (`evals/datasets/golden_corpus.json` — 3 recipient scenarios, 27 total input articles, 2 planted developing storylines, 1 cross-day semantic re-hash) + 2 `ResearchAgent` tool-trajectory scenarios (`evals/datasets/trajectory_cases.json`).  
> **Independent Judge:** `gemini-3.5-flash` (temperature `0.0`, structured `JudgeScorecard` schema) held constant across all three configurations to avoid self-grading bias.

---

## 1. Experiment Design & Reproducible Commands

To empirically evaluate both the **agentic architecture** (embeddings + semantic clustering + cross-day storyline linking + `CriticAgent` revision loop) and **synthesis model choice** (`gemini-3.1-flash-lite` vs. `gemini-3.5-flash`), we ran three full live `agentic-emailer-eval --mode full` evaluations sequentially against the exact same frozen dataset and independent judge:

| Config | Description | Reproducible Command |
|---|---|---|
| **Config A (`single_shot`)** | Single-shot baseline: `gemini-3.1-flash-lite` synthesis only, `--no-clustering` (no embeddings, cosine clustering, semantic cross-day dedup, or developing-story linking), `--no-critic` (no critic audit/revisions). Deterministic URL/schema verifier still active. | `uv run agentic-emailer-eval --mode full --single-shot --synthesis-model gemini-3.1-flash-lite --judge-model gemini-3.5-flash` |
| **Config B (`full_agentic`, Flash-Lite)** | Production `v1.0` default: `ResearchAgent` (`gemini-3.1-flash-lite`) + `gemini-embedding-001` (768-dim) clustering & storyline linking + `SynthesisAgent` (`gemini-3.1-flash-lite`) + deterministic verifier + `CriticAgent` (`gemini-3.1-flash-lite`). | `uv run agentic-emailer-eval --mode full --synthesis-model gemini-3.1-flash-lite --judge-model gemini-3.5-flash` |
| **Config C (`full_agentic`, Flash)** | Full `v1.0` architecture with upgraded synthesis model: swaps `SYNTHESIS_MODEL` to `gemini-3.5-flash` while keeping research, critic, embeddings, and judge constant. | `uv run agentic-emailer-eval --mode full --synthesis-model gemini-3.5-flash --judge-model gemini-3.5-flash` |

> **Cost Accounting Note:** `evals/runner.py` isolates `pipeline_budget` from `judge_budget`. All `estimated_cost_usd`, `pipeline_llm_calls`, and `pipeline_tokens` values below measure **pipeline spend only** (embeddings + research + synthesis + critic), excluding judge tokens.

---

## 2. Measured Results Summary

| Metric | Config A: Single-Shot Baseline (`gemini-3.1-flash-lite`) | Config B: Full Agentic Default (`gemini-3.1-flash-lite`) | Config C: Full Agentic Upgraded (`gemini-3.5-flash`) | Target / Hard Gate |
|---|---:|---:|---:|---|
| **Run Timestamp (UTC)** | `2026-11-27T04:57:45Z` | `2026-11-27T05:00:40Z` | `2026-11-27T05:07:09Z` | — |
| `schema_validity_rate` | `1.0000` | `1.0000` | `1.0000` | `== 1.0000` |
| `url_grounding_rate` | `1.0000` | `1.0000` | `1.0000` | `== 1.0000` |
| `duplicate_leakage_rate` | **`0.0370` (FAIL)** | **`0.0000` (PASS)** | **`0.0000` (PASS)** | `== 0.0000` |
| `developing_story_recall` | **`0.0000` (FAIL)** | **`1.0000` (PASS)** | **`1.0000` (PASS)** | `>= 0.8000` |
| `cluster_compression_ratio` | `1.0000x` | `1.0870x` | `1.0870x` | info |
| `topic_coverage_rate` | `1.0000` | `1.0000` | `1.0000` | `>= 0.8500` |
| `trajectory_precision` | `1.0000` | `1.0000` | `1.0000` | `>= 0.8000` |
| `avg_revisions` | `0.00` | `0.00` | `0.00` | info |
| `avg_latency_ms` (per corpus case) | `11,580.1 ms` | **`9,943.8 ms`** | `61,715.6 ms` | info |
| `pipeline_llm_calls` (3 cases + 2 traj) | `6` | `12` | `12` | info |
| `pipeline_tokens` (3 cases + 2 traj) | `16,377` | `28,470` | `36,084` | info |
| `estimated_cost_usd` (pipeline total) | `$0.002782` | **`$0.004113`** | `$0.009338` | info |
| `judge_cost_usd` (`gemini-3.5-flash`) | `$0.010884` | `$0.008471` | `$0.009989` | info |
| `mean_faithfulness` (1–5) | `5.00` | `5.00` | `5.00` | `>= 4.00` |
| `mean_relevance` (1–5) | `5.00` | `5.00` | `5.00` | `>= 4.00` |
| `mean_synthesis_quality` (1–5) | `5.00` | `5.00` | `5.00` | `>= 3.80` |
| `mean_conciseness` (1–5) | `5.00` | `5.00` | `5.00` | `>= 4.00` |
| **Hard Gate Status** | **FAILED (2 gates)** | **PASSED** | **PASSED** | All pass |

---

## 3. Per-Case Breakdown

### Golden Corpus Cases

| Case | Metric | Config A (`single_shot`) | Config B (`full_agentic`, Flash-Lite) | Config C (`full_agentic`, Flash) |
|---|---|---:|---:|---:|
| **`ai_engineering`** | Clusters / Unseen Articles | `14 / 14` (`1.00x`) | `11 / 13` (`1.18x`) | `11 / 13` (`1.18x`) |
| | Duplicate Leakage | **`0.1111`** (InfoWorld re-hash survived) | `0.0000` | `0.0000` |
| | Developing Story Recall | **`0.0000`** (`0/1`) | `1.0000` (`1/1`) | `1.0000` (`1/1`) |
| | Latency | `17,457.5 ms` | `10,020.1 ms` | `73,821.6 ms` |
| | Judge (`F / R / S / C`) | `5 / 5 / 5 / 5` | `5 / 5 / 5 / 5` | `5 / 5 / 5 / 5` |
| **`general_tech_science`** | Clusters / Unseen Articles | `10 / 10` (`1.00x`) | `10 / 10` (`1.00x`) | `10 / 10` (`1.00x`) |
| | Duplicate Leakage | `0.0000` | `0.0000` | `0.0000` |
| | Developing Story Recall | **`0.0000`** (`0/1`) | `1.0000` (`1/1`) | `1.0000` (`1/1`) |
| | Latency | `14,663.0 ms` | `8,505.0 ms` | `96,156.0 ms` |
| | Judge (`F / R / S / C`) | `5 / 5 / 5 / 5` | `5 / 5 / 5 / 5` | `5 / 5 / 5 / 5` |
| **`sparse_day`** | Clusters / Unseen Articles | `2 / 2` (`1.00x`) | `2 / 2` (`1.00x`) | `2 / 2` (`1.00x`) |
| | Duplicate Leakage | `0.0000` | `0.0000` | `0.0000` |
| | Latency | `2,619.9 ms` | `11,306.1 ms` | `15,168.8 ms` |
| | Judge (`F / R / S / C`) | `5 / 5 / 5 / 5` | `5 / 5 / 5 / 5` | `5 / 5 / 5 / 5` |

### `ResearchAgent` Tool-Trajectory Cases

Across all three runs, `ResearchAgent` (`gemini-3.1-flash-lite`) achieved **`1.0000` `trajectory_precision`** on both scenarios:
- **`thin_snippets_require_scrape`**: Scraped exactly the 2 planted thin-snippet articles (`58f072b105b3135c`, `850bdad7a98ee8a7`), `0` forbidden scrapes, `0` unnecessary web searches.
- **`rich_rss_skip_unnecessary_tools`**: Performed `0` scrapes and `0` web searches when RSS snippets already provided dense coverage (`1.0000` precision).

---

## 4. Key Engineering Findings & Production Decision

1. **Cross-Day Memory & Semantic Clustering Are Non-Negotiable (`Config B` vs. `Config A`):**
   - Without Stage 4 embeddings and cosine clustering (`Config A`), the pipeline failed **two hard gates**:
     - `duplicate_leakage_rate` rose from `0.0000` to **`0.0370`** (`0.1111` on `ai_engineering`) because InfoWorld's cross-day re-hash of Mistral Enterprise Hub (`https://www.infoworld.com/article/2026/11/mistral-enterprise-hub-on-prem-governance`) had a new URL and bypassed exact-URL filtering.
     - `developing_story_recall` dropped from `1.0000` to **`0.0000`** (`0/2`) because neither FrontierAI's EU AI Office follow-up nor Form Energy's Minnesota commissioning was linked to prior digest context.
   - Enabling the full agentic architecture (`Config B`) closed both gaps completely for only **+\$0.001331** across the entire 3-case + 2-trajectory suite (`$0.002782` -> `$0.004113`), while actually *reducing* average per-case latency (`11,580.1 ms` -> `9,943.8 ms`) because same-day clustering pre-compressed 3 outlets covering the Gemini 3.1 Flash-Lite launch into 1 cluster (`1.18x` compression on `ai_engineering`).
   - Notably, `Config A` still held `1.0000` `url_grounding_rate` and `1.0000` `schema_validity_rate` because Pydantic `output_schema` and the deterministic grounding verifier (`verifier.py`) enforce hard invariants independently of the LLM loop.

2. **Why `gemini-3.1-flash-lite` Wins as Production Default Over `gemini-3.5-flash` (`Config B` vs. `Config C`):**
   - Upgrading `SYNTHESIS_MODEL` to `gemini-3.5-flash` (`Config C`) achieved **identical 5.00 / 5.00** scores across all four judge rubrics and identical deterministic gate metrics (`1.0000` grounding, `0.0000` leakage, `1.0000` developing-story recall).
   - However, `gemini-3.5-flash` incurred **6.2x higher average latency** (`61,715.6 ms` vs. `9,943.8 ms`, driven by default reasoning/thinking tokens on `gemini-3.5-flash`) and **2.27x higher pipeline cost** (`$0.009338` vs. `$0.004113`).
   - Qualitatively, `gemini-3.5-flash` produced slightly richer narrative continuity prose on developing updates (e.g., explicitly opening with *"Following an inquiry opened earlier this week by the EU AI Office..."* and *"Moving beyond initial construction..."*), whereas `gemini-3.1-flash-lite` stayed slightly more concise while still accurately setting `story_status: "developing_update"` and capturing all technical metrics.
   - **Decision:** Keep **`gemini-3.1-flash-lite`** as the default `SYNTHESIS_MODEL` in Terraform (`infra/terraform/variables.tf`) and `Settings` for optimal latency and Always Free / \$10-credit cost efficiency. Because `SYNTHESIS_MODEL` is a single environment variable and GitHub Actions input (`eval-full.yml`), upgrading models in the future requires zero code changes and is immediately verifiable against this benchmark.

3. **Operational Observation (API Transient Variance):**
   - During the live benchmark window, the Gemini API returned occasional transient `503 UNAVAILABLE` spikes (automatically recovered by the exponential-backoff retry loop in `agents/runtime.py`, which also inflated one trajectory call in `Config C` to `85.3s`). In production Cloud Run Jobs, Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=true`) provides higher stability alongside the same retry guardrails and a `600s` Cloud Run Job timeout.
