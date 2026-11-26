# Milestone 1: Package Refactor, Core Google ADK Agents, Grounding Verifier & Offline Eval Harness

> **Antigravity Workflow:** Run in a fresh conversation with `/boost` (or `/goal`) on a dedicated branch `milestone-1/adk-core-and-evals`. Commit cleanly at each task checkpoint and run a fresh-context review pass before merging.  
> **Master Spec:** `docs/plans/DESIGN_SPEC.md`

> [!IMPORTANT]
> **Verification Policy (Eval-First — NO Mock-Heavy TDD)**
> - **DO NOT** write unit tests that mock `genai.Client`, Google ADK `Runner`, or assert on prompt text fragments.
> - **DO** delete the old mock-heavy tests in `tests/` and replace them with **only** the thin deterministic invariant checks specified in Task 8.
> - **DO** verify every task using its exact Verification Commands (`ruff`, `mypy`, invariant checks, dry-run smoke check, and offline eval runner), iterating on agent prompts/verifier rules if needed until the Exit Gate passes.

**Goal:** Transform the flat linear script (`aggregator.py`, `sources.py`, `summarizer.py`, `emailer.py`) into a cleanly architected Python package powered by **Google ADK (`google-adk`)** (`ResearchAgent`, `SynthesisAgent`, `CriticAgent`), a deterministic grounding verifier, an `EmailSender` protocol + responsive HTML renderer, hard per-run budget guardrails, thin deterministic invariant checks, and a working offline eval harness running locally.

**Architecture:** Deterministic async RSS ingestion -> ADK `ResearchAgent` (gap-driven search + SSRF-safe selective full-article scraping) -> Per-recipient bounded editorial loop (`SynthesisAgent` outputting Pydantic `DigestDraft` -> deterministic URL/schema verifier -> `CriticAgent` outputting `CriticVerdict`, max 2 revisions) -> `EmailSender` protocol (Gmail SMTP adapter).

**Tech Stack:** Python 3.12+, `uv`, `google-adk`, `google-genai`, `pydantic`, `pydantic-settings`, `httpx`, `feedparser`, `trafilatura`, `duckduckgo-search`, `ruff`, `mypy`, `pytest` (thin invariants only).

---

## File Structure Map

- **Modify:** `pyproject.toml` (switch to `package = true` with `hatchling`, `src/` layout, add ADK/Pydantic/httpx/trafilatura/ruff/mypy deps, register `agentic-emailer` and `agentic-emailer-eval` CLI scripts)
- **Create:** `src/agentic_emailer/__init__.py`
- **Create:** `src/agentic_emailer/config.py` (Pydantic `Settings`, Vertex AI vs. Gemini API toggle, budget caps, SMTP config)
- **Create:** `src/agentic_emailer/models.py` (All core Pydantic v2 domain models)
- **Create:** `src/agentic_emailer/budget.py` (`RunBudgetTracker` enforcing hard LLM call, token, search, and scrape ceilings)
- **Create:** `src/agentic_emailer/ingestion/__init__.py`
- **Create:** `src/agentic_emailer/ingestion/rss.py` (Async concurrent RSS fetch + URL canonicalization + deterministic `article_id`)
- **Create:** `src/agentic_emailer/ingestion/search.py` (DuckDuckGo news/text search tool with timeouts and polite rate limiting)
- **Create:** `src/agentic_emailer/ingestion/extractor.py` (SSRF-safe selective full-article extractor via `httpx` + `trafilatura`)
- **Create:** `src/agentic_emailer/agents/__init__.py`
- **Create:** `src/agentic_emailer/agents/runtime.py` (ADK `Runner` execution helper, token extraction, Pydantic structured-output parsing)
- **Create:** `src/agentic_emailer/agents/verifier.py` (100% deterministic grounding, URL-verbatim, freshness & schema verifier)
- **Create:** `src/agentic_emailer/agents/research_agent.py` (ADK `LlmAgent` with `search_web_news` and `fetch_article_content` tools)
- **Create:** `src/agentic_emailer/agents/synthesis_agent.py` (ADK `LlmAgent` producing `DigestDraft`)
- **Create:** `src/agentic_emailer/agents/critic_agent.py` (ADK `LlmAgent` producing `CriticVerdict`)
- **Create:** `src/agentic_emailer/delivery/__init__.py`
- **Create:** `src/agentic_emailer/delivery/protocol.py` (`EmailSender` Protocol)
- **Create:** `src/agentic_emailer/delivery/renderer.py` (Clean responsive HTML + plain-text renderer from `DigestDraft`)
- **Create:** `src/agentic_emailer/delivery/smtp_sender.py` (Gmail SMTP `EmailSender` adapter with exponential backoff retry)
- **Create:** `src/agentic_emailer/pipeline.py` (End-to-end async orchestrator with bounded editorial loop & per-recipient error isolation)
- **Create:** `src/agentic_emailer/cli.py` (`run` command supporting `--dry-run`, `--gather-only`, `--recipient`, `--config`)
- **Delete (after migration verified):** Root legacy files `aggregator.py`, `sources.py`, `summarizer.py`, `emailer.py`, `requirements.txt`, and old mock-heavy tests (`tests/conftest.py`, `tests/test_aggregator.py`, `tests/test_sources.py`, `tests/test_summarizer.py`, `tests/test_emailer.py`)
- **Create:** `tests/test_invariants_grounding.py`, `tests/test_invariants_budget.py` (thin pure-logic invariant checks only)
- **Create:** `evals/__init__.py`, `evals/datasets/golden_corpus.json`, `evals/datasets/trajectory_cases.json`, `evals/metrics.py`, `evals/judges.py`, `evals/runner.py`, `evals/reports/baseline.json`

---

### Task 1: Modernize `pyproject.toml` and Package Scaffold (`src/` Layout)

**Files:**
- Modify: `pyproject.toml`
- Create: `src/agentic_emailer/__init__.py`

**Instructions:**
1. Update `pyproject.toml`:
   - Set `requires-python = ">=3.11"` (clean match for `python:3.12-slim` and modern `google-adk`).
   - Add `[build-system]` using `hatchling` and configure `[tool.hatch.build.targets.wheel] packages = ["src/agentic_emailer"]`.
   - Set `[tool.uv] package = true`.
   - Dependencies: `google-adk>=1.0.0`, `google-genai>=1.10.0`, `pydantic>=2.10.0`, `pydantic-settings>=2.7.0`, `httpx>=0.28.0`, `feedparser>=6.0.0`, `trafilatura>=2.0.0`, `duckduckgo-search>=7.0.0`, `python-dateutil>=2.9.0`.
   - Dev dependency group: `pytest>=8.0.0`, `ruff>=0.9.0`, `mypy>=1.14.0`, `types-python-dateutil`.
   - Register CLI scripts:
     - `agentic-emailer = "agentic_emailer.cli:main"`
     - `agentic-emailer-eval = "evals.runner:main"`
   - Add `[tool.ruff]` (`line-length = 100`, `target-version = "py312"`) and `[tool.mypy]` (`python_version = "3.12"`, `ignore_missing_imports = true`) and `[tool.pytest.ini_options]` (`testpaths = ["tests"]`, `pythonpath = ["src", "."]`).
2. Create `src/agentic_emailer/__init__.py` exporting `__version__ = "1.0.0"`.
3. Run `uv sync --all-groups` and confirm installed `google-adk` version and imports (`LlmAgent`, `Runner`, `InMemorySessionService`).

**Verification Commands:**
```powershell
uv sync --all-groups
uv run python -c "import agentic_emailer, google.adk; from google.adk.agents import LlmAgent; from google.adk.runners import Runner; print(agentic_emailer.__version__, google.adk.__version__)"
```
Expected: Prints version numbers cleanly with exit code 0.

**Commit:** `chore: migrate project to src package layout with google-adk dependencies`

---

### Task 2: Domain Models, Configuration Settings & Hard Budget Tracker

**Files:**
- Create: `src/agentic_emailer/models.py`
- Create: `src/agentic_emailer/config.py`
- Create: `src/agentic_emailer/budget.py`

**Instructions:**
1. In `src/agentic_emailer/models.py`, implement strict Pydantic v2 models matching Section 5 of the Master Spec:
   - `RssFeedConfig(name: str, url: str)`
   - `RecipientProfile` (with email validation via regex/field validator)
   - `RawArticle` (with `to_prompt_block()` helper that includes truncated `full_text` when available, otherwise `snippet`)
   - `StoryCluster` (includes optional `embedding`, `is_developing_story`, `prior_story_summary`, `prior_digest_date` so Milestone 2 slots in seamlessly)
   - `Citation(source_name: str, url: str, article_id: str)`
   - `DigestHighlight(title: str, topic: str, summary: str, why_it_matters: str, story_status: Literal["new", "developing_update"] = "new", citations: list[Citation])`
   - `DigestDraft(title: str, executive_summary: str, highlights: list[DigestHighlight], notable_trends: list[str] = [])`
   - `VerificationResult(passed: bool, errors: list[str], invalid_urls: list[str], stale_urls: list[str])`
   - `CriticVerdict(approved: bool, faithfulness_score: int, relevance_score: int, issues: list[str], revision_instructions: str)` (scores bounded `ge=1, le=5`)
   - `DeliveryReceipt`, `RunMetrics`
2. In `src/agentic_emailer/config.py`, implement `Settings(BaseSettings)` supporting `.env` and environment variables:
   - Backend toggle: `google_genai_use_vertexai: bool = False`, `gcp_project_id: str | None = None`, `gcp_location: str = "us-central1"`, `gemini_api_key: str | None = None` (plus backwards-compatible local file fallback to `gemini_api_key.txt` when not on Vertex AI).
   - Models: `research_model: str = "gemini-3.1-flash-lite"`, `synthesis_model: str = "gemini-3.1-flash-lite"`, `critic_model: str = "gemini-3.1-flash-lite"`, `embedding_model: str = "gemini-embedding-001"`.
   - Budgets: `max_llm_calls_per_run: int = 60`, `max_total_tokens_per_run: int = 350_000`, `max_article_scrapes_per_run: int = 20`, `max_search_calls_per_run: int = 20`, `max_critic_revisions: int = 2`.
   - Email: `smtp_host: str = "smtp.gmail.com"`, `smtp_port: int = 587`, `smtp_sender: str = ""`, `email_password: str | None = None` (with local fallback to `smtp_password.txt`).
   - Helper `apply_genai_env()` that configures `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, or `GOOGLE_API_KEY` for `google-adk`.
3. In `src/agentic_emailer/budget.py`, implement `RunBudgetTracker` and `BudgetExceededError`:
   - Tracks `llm_calls`, `input_tokens`, `output_tokens`, `search_calls`, `scrape_calls` with an `asyncio.Lock` / thread lock.
   - Methods: `consume_llm_call(input_tokens=0, output_tokens=0)`, `can_scrape() -> bool`, `consume_scrape()`, `can_search() -> bool`, `consume_search()`, `estimated_cost_usd() -> float` (configurable per-1M token price table for Flash-Lite / Flash models).

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer/models.py src/agentic_emailer/config.py src/agentic_emailer/budget.py
uv run mypy src/agentic_emailer/models.py src/agentic_emailer/config.py src/agentic_emailer/budget.py
```
Expected: 0 lint errors, 0 type errors.

**Commit:** `feat: add Pydantic domain contracts, settings, and hard run budget tracker`

---

### Task 3: Async Ingestion Layer (RSS, Search Wrapper, SSRF-Safe Article Extractor)

**Files:**
- Create: `src/agentic_emailer/ingestion/__init__.py`
- Create: `src/agentic_emailer/ingestion/rss.py`
- Create: `src/agentic_emailer/ingestion/search.py`
- Create: `src/agentic_emailer/ingestion/extractor.py`

**Instructions:**
1. **`ingestion/rss.py`:**
   - Implement `normalize_url(url: str) -> str` (strip tracking params `utm_*`, `ref`, `source`, `fbclid`, `gclid`; normalize scheme/host lowercase; strip trailing slash and fragment).
   - Implement `make_article_id(canonical_url: str) -> str` (first 16 hex chars of SHA-256 of canonical URL).
   - Implement `async def fetch_rss_feeds(feeds: list[RssFeedConfig], lookback_hours: int, max_per_feed: int, concurrency: int = 8) -> list[RawArticle]` using `httpx.AsyncClient(timeout=15.0, follow_redirects=True)` + `feedparser.parse(response.text)` in `asyncio.Semaphore`. Graceful warning + skip per failing feed.
2. **`ingestion/search.py`:**
   - Implement `search_news(query: str, lookback_hours: int, max_results: int = 8) -> list[RawArticle]` wrapping `DDGS(timeout=10)` with news -> text fallback, date parsing, URL normalization, and deterministic `article_id`.
3. **`ingestion/extractor.py`:**
   - Implement SSRF guard `_is_safe_public_url(url: str) -> bool`: allow only `http`/`https`, resolve hostname via `socket.getaddrinfo`, reject private/loopback/link-local/metadata IP ranges (including GCP metadata `169.254.169.254`!).
   - Implement `async def extract_article_text(url: str, max_chars: int = 4000) -> str | None`: streams max 1 MB via `httpx.AsyncClient(timeout=12.0)`, extracts clean body text via `trafilatura.extract(...)`, truncates cleanly to `max_chars`. Returns `None` on blocked/paywalled/non-HTML responses without raising.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer/ingestion
uv run mypy src/agentic_emailer/ingestion
uv run python -c "from agentic_emailer.ingestion.extractor import _is_safe_public_url; assert not _is_safe_public_url('http://169.254.169.254/latest/meta-data'); assert not _is_safe_public_url('http://127.0.0.1:8080'); print('SSRF guard OK')"
```
Expected: Lint/type pass + `SSRF guard OK`.

**Commit:** `feat: add async RSS fetcher, search wrapper, and SSRF-safe article extractor`

---

### Task 4: Deterministic Grounding Verifier & ADK Runtime Helper

**Files:**
- Create: `src/agentic_emailer/agents/__init__.py`
- Create: `src/agentic_emailer/agents/verifier.py`
- Create: `src/agentic_emailer/agents/runtime.py`

**Instructions:**
1. **`agents/verifier.py` (100% deterministic, zero LLM calls):**
   - Implement `verify_digest_draft(draft: DigestDraft, allowed_articles: list[RawArticle], seen_urls: set[str], topics: list[str]) -> VerificationResult`:
     - Build lookup sets of exact `url`, `canonical_url`, and `article_id` from `allowed_articles`.
     - Check `executive_summary` is non-empty (>= 30 chars) and `highlights` is non-empty (when `allowed_articles` is non-empty).
     - For every `highlight` and every `citation`:
       - Verify `citation.url` (or its normalized form) exists in `allowed_articles`; otherwise record in `invalid_urls` and `errors`.
       - Verify normalized `citation.url` is NOT in `seen_urls`; otherwise record in `stale_urls` and `errors`.
     - Verify no duplicate URLs repeated across different highlights in the same digest.
     - Return `VerificationResult(passed=len(errors) == 0, ...)`.
2. **`agents/runtime.py`:**
   - Implement `async def run_adk_agent(agent: LlmAgent, prompt: str, budget: RunBudgetTracker, app_name: str = "agentic_emailer", user_id: str = "system") -> tuple[str, list[dict[str, Any]]]`:
     - Uses `InMemorySessionService` + `Runner(agent=agent, app_name=app_name, session_service=session_service)`.
     - Collects final response text, tool call trajectory (`name`, `args`, `response_summary`) for evals, and `usage_metadata` token counts from ADK events into `budget.consume_llm_call(...)`.
   - Implement `parse_structured_output(raw_text: str, schema: type[T]) -> T`: strips markdown code fences if present, validates via `schema.model_validate_json(...)`, raises clear `ValueError` on mismatch so caller can trigger one repair pass.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer/agents/verifier.py src/agentic_emailer/agents/runtime.py
uv run mypy src/agentic_emailer/agents/verifier.py src/agentic_emailer/agents/runtime.py
```
Expected: 0 lint errors, 0 type errors.

**Commit:** `feat: add deterministic grounding verifier and ADK runner runtime helper`

---

### Task 5: Google ADK Agents (`ResearchAgent`, `SynthesisAgent`, `CriticAgent`)

**Files:**
- Create: `src/agentic_emailer/agents/research_agent.py`
- Create: `src/agentic_emailer/agents/synthesis_agent.py`
- Create: `src/agentic_emailer/agents/critic_agent.py`

**Instructions:**
1. **`agents/research_agent.py`:**
   - Define a `ResearchSessionContext` holding the mutable corpus (`dict[str, RawArticle]` keyed by `article_id`), `lookback_hours`, and `RunBudgetTracker`.
   - Expose two ADK function tools closed over `ResearchSessionContext`:
     - `search_web_news(query: str, topic: str) -> str`: checks `budget.can_search()`, runs `search_news(...)`, merges new unique `RawArticle`s into context, returns compact JSON summary of newly discovered `article_id` + titles + snippet lengths (or budget-exhausted note).
     - `fetch_article_content(article_id: str) -> str`: checks `budget.can_scrape()`, fetches full text via `extract_article_text(...)`, attaches `full_text` to the `RawArticle` in context, returns extracted summary status.
   - Build `create_research_agent(model: str, tools: list[Callable]) -> LlmAgent`:
     - Clear instruction: inspect the already-fetched RSS inventory by topic. ONLY call `search_web_news` for topics that have sparse coverage (<3 relevant RSS items) or breaking-news gaps. ONLY call `fetch_article_content` on high-signal articles whose snippet is thin (<180 chars) or ambiguous. Stop once topics have solid coverage; never waste tool calls.
   - Provide `async def run_research_stage(rss_articles, topics, lookback_hours, settings, budget) -> tuple[list[RawArticle], list[dict]]` returning enriched articles + tool trajectory log.
2. **`agents/synthesis_agent.py`:**
   - Build `create_synthesis_agent(model: str) -> LlmAgent` with `output_schema=DigestDraft` and `temperature=0.25`.
   - Instruction enforces: use ONLY provided clusters/articles; cite exact verbatim URLs and `article_id`s from input; mark `story_status="developing_update"` and reference prior context when a cluster has `is_developing_story=True`; group logically across recipient topics; produce concise `why_it_matters` per highlight.
   - Support an optional `revision_feedback: str | None` parameter in `run_synthesis(...)` so Critic/Verifier feedback feeds cleanly into revision iterations.
3. **`agents/critic_agent.py`:**
   - Build `create_critic_agent(model: str) -> LlmAgent` with `output_schema=CriticVerdict` and `temperature=0.0`.
   - Instruction audits a candidate `DigestDraft` strictly against the provided source corpus: check every claim for source support (flag any hallucinated metrics, dates, or claims), check topic relevance for the recipient, flag generic fluff. Set `approved=True` only when `faithfulness_score >= 4`, `relevance_score >= 4`, and zero unsupported claims exist.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer/agents
uv run mypy src/agentic_emailer/agents
```
Expected: 0 lint errors, 0 type errors.

**Commit:** `feat: implement Google ADK ResearchAgent, SynthesisAgent, and CriticAgent`

---

### Task 6: Delivery Layer (`EmailSender` Protocol, Responsive HTML Renderer, Gmail SMTP Adapter)

**Files:**
- Create: `src/agentic_emailer/delivery/__init__.py`
- Create: `src/agentic_emailer/delivery/protocol.py`
- Create: `src/agentic_emailer/delivery/renderer.py`
- Create: `src/agentic_emailer/delivery/smtp_sender.py`

**Instructions:**
1. **`delivery/protocol.py`:** Define `@runtime_checkable class EmailSender(Protocol)` with `def send(self, recipient: RecipientProfile, subject: str, plain_text: str, html_body: str) -> DeliveryReceipt: ...`.
2. **`delivery/renderer.py`:**
   - Implement `render_subject(profile: RecipientProfile, draft: DigestDraft) -> str`.
   - Implement `render_plain_text(draft: DigestDraft) -> str` (clean Markdown-like text fallback).
   - Implement `render_html(draft: DigestDraft) -> str`: escape all user/LLM text via `html.escape`, render clean mobile-friendly inline-CSS email card layout with Executive Summary callout, Topic sections, visual badge for `DEVELOPING UPDATE` stories, `Why it matters` highlight, clickable cited source pills, and Notable Trends footer.
   - Implement `build_fallback_draft(articles: list[RawArticle], topics: list[str]) -> DigestDraft` for budget-exhaustion or LLM-outage resilience.
3. **`delivery/smtp_sender.py`:**
   - Implement `SmtpEmailSender(host, port, sender, password, max_retries=3)` satisfying `EmailSender`.
   - Uses `smtplib.SMTP(timeout=30)` + `starttls()`, retries transient `SMTPException`/`TimeoutError` with exponential backoff (2s, 4s), returns `DeliveryReceipt(status="sent"|"failed", ...)`. Never raises uncaught exception across recipients.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer/delivery
uv run mypy src/agentic_emailer/delivery
```
Expected: 0 lint errors, 0 type errors.

**Commit:** `feat: add EmailSender protocol, responsive HTML renderer, and Gmail SMTP adapter`

---

### Task 7: End-to-End Async Pipeline Orchestrator & CLI Entry Point

**Files:**
- Create: `src/agentic_emailer/pipeline.py`
- Create: `src/agentic_emailer/cli.py`

**Instructions:**
1. **`pipeline.py`:**
   - Implement `async def run_digest_pipeline(...) -> tuple[list[tuple[RecipientProfile, DigestDraft, DeliveryReceipt]], RunMetrics]`:
     - Stage 1: Load active `RecipientProfile`s (in Milestone 1: from `RECIPIENTS_JSON` env var, `recipients.json`, or `config.json` fallback via a clean loader; Milestone 2 adds `MemoryStore` profile loading).
     - Stage 2: Pool unique feeds & topics; run concurrent RSS ingestion (`fetch_rss_feeds`).
     - Stage 3: Run `ResearchAgent` stage under `RunBudgetTracker` (skipped only if `gather_only=True`).
     - Stage 4: In Milestone 1, wrap articles into 1-to-1 `StoryCluster`s per normalized URL (Milestone 2 replaces this hook with semantic embedding clustering + cross-day memory).
     - Stage 5: For each recipient (isolated `try/except` so one recipient failure never aborts others):
       - Filter relevant clusters for recipient.
       - Run bounded editorial loop (`attempt = 0 .. settings.max_critic_revisions`):
         1. `SynthesisAgent` -> `DigestDraft`
         2. Deterministic `verify_digest_draft(...)` -> if failed, pass errors as `revision_feedback` and continue loop (do not waste a Critic LLM call on broken URLs!).
         3. `CriticAgent` -> `CriticVerdict` -> if `approved` (or budget exhausted), break; else pass `revision_instructions` into next iteration.
       - If final draft still fails deterministic grounding verification after max revisions, strip invalid citations or fall back safely to `build_fallback_draft` so **an ungrounded URL is NEVER emailed**.
       - Render HTML + plain text; print on `--dry-run`, or dispatch via `EmailSender`.
     - Return results + populated `RunMetrics`.
2. **`cli.py`:**
   - Subcommand CLI via `argparse`: `agentic-emailer run [--config PATH] [--dry-run] [--gather-only] [--recipient ID_OR_EMAIL]`.
   - Calls `settings.apply_genai_env()` before ADK execution.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer
uv run mypy src/agentic_emailer
uv run agentic-emailer run --config config.ci.json --gather-only
```
Expected: Lint + type pass; `--gather-only` fetches RSS feeds and prints normalized articles cleanly.

**Commit:** `feat: wire async ADK digest pipeline orchestrator and CLI`

---

### Task 8: Replace Mock-Heavy Tests with Thin Deterministic Invariants & Remove Legacy Flat Modules

**Files:**
- Delete: `aggregator.py`, `sources.py`, `summarizer.py`, `emailer.py`, `requirements.txt`
- Delete: `tests/conftest.py`, `tests/test_aggregator.py`, `tests/test_sources.py`, `tests/test_summarizer.py`, `tests/test_emailer.py`
- Create: `tests/test_invariants_grounding.py`
- Create: `tests/test_invariants_budget.py`
- Modify: `run_digest.ps1` (point to `uv run agentic-emailer run`)

**Instructions:**
1. Delete the legacy flat scripts and mock-heavy unit tests listed above (keeping `config.ci.json`, `config.example.json`, `recipients.example.json`).
2. Create `tests/test_invariants_grounding.py` (4 fast deterministic tests, ZERO mocks):
   - `test_url_normalization_strips_tracking_and_dedups_id`
   - `test_verifier_rejects_hallucinated_url`
   - `test_verifier_rejects_stale_seen_url`
   - `test_verifier_accepts_valid_verbatim_citations`
3. Create `tests/test_invariants_budget.py` (3 fast deterministic tests, ZERO mocks):
   - `test_budget_tracker_enforces_llm_call_and_token_caps`
   - `test_budget_tracker_enforces_scrape_and_search_caps`
   - `test_ssrf_guard_blocks_private_and_metadata_ips`
4. Update `run_digest.ps1` to run `uv run agentic-emailer run`.

**Verification Commands:**
```powershell
uv run pytest -v
uv run ruff check src tests
```
Expected: Exactly 7 fast (<1s) pure-logic invariant tests pass; 0 mock tests; 0 lint errors.

**Commit:** `refactor: remove legacy flat scripts and mock tests; add thin deterministic invariants`

---

### Task 9: Build Offline Eval Harness (`evals/`) & Capture Milestone 1 Baseline

**Files:**
- Create: `evals/__init__.py`
- Create: `evals/datasets/golden_corpus.json`
- Create: `evals/datasets/trajectory_cases.json`
- Create: `evals/metrics.py`
- Create: `evals/judges.py`
- Create: `evals/runner.py`
- Create: `evals/reports/baseline.json`

**Instructions:**
1. **`evals/datasets/golden_corpus.json`:** Create 3 frozen realistic evaluation cases:
   - Case 1 (`ai_engineering`): 12 realistic articles across 3 AI topics, including 2 duplicate-outlet stories, 1 stale URL present in `seen_urls`, and 1 thin-snippet article.
   - Case 2 (`general_tech_science`): 10 non-AI articles (climate/energy + robotics) to verify domain generalization.
   - Case 3 (`sparse_day`): Only 2 articles across 3 topics to verify zero-hallucination behavior when news is sparse.
2. **`evals/datasets/trajectory_cases.json`:** 2 scenarios testing `ResearchAgent` tool discipline (one with thin snippets expecting `fetch_article_content`, one with rich RSS coverage expecting minimal/zero unnecessary web searches).
3. **`evals/metrics.py`:** Compute deterministic metrics per case and aggregated:
   - `schema_validity_rate`, `url_grounding_rate`, `duplicate_leakage_rate`, `topic_coverage_rate`, `trajectory_precision`, `avg_revisions`, `avg_latency_ms`, `estimated_cost_usd`.
4. **`evals/judges.py`:** Implement Gemini LLM-as-Judge returning Pydantic `JudgeScorecard(faithfulness: int, relevance: int, synthesis_quality: int, conciseness: int, rationale: str)` evaluated strictly against the frozen input corpus.
5. **`evals/runner.py`:**
   - CLI: `uv run agentic-emailer-eval --mode deterministic|full [--baseline evals/reports/baseline.json] [--output PATH] [--save-baseline]`
   - `--mode deterministic`: runs schema/grounding/dedup checks on cached or generated digests without judge calls (fast/cheap CI gate).
   - `--mode full`: executes `SynthesisAgent` + `verifier` + `CriticAgent` + `ResearchAgent` trajectory cases + LLM-as-judge against `golden_corpus.json` using live Gemini API/Vertex AI, prints a crisp Markdown scorecard table with deltas vs. baseline, and exits non-zero if hard gates fail (`url_grounding_rate < 1.0`, `duplicate_leakage_rate > 0.0`, `mean_faithfulness < 4.0`).
6. Run the eval suite locally once with `--mode full --save-baseline` (using local `GEMINI_API_KEY` or `gemini_api_key.txt`) to verify everything works end-to-end and commit `evals/reports/baseline.json`.

**Verification Commands:**
```powershell
uv run ruff check src tests evals
uv run mypy src evals
uv run agentic-emailer-eval --mode full --save-baseline
uv run agentic-emailer run --config config.ci.json --dry-run
```
Expected: Full eval passes all hard gates (`url_grounding_rate == 1.0`, `duplicate_leakage_rate == 0.0`, `mean_faithfulness >= 4.0`), baseline JSON written, and live `--dry-run` produces a cleanly grounded digest.

**Commit:** `feat: add offline eval harness, golden dataset, LLM-as-judge, and v1 baseline`

---

## Milestone 1 Exit Gate
- [ ] `uv run ruff check src tests evals` and `uv run mypy src evals` pass cleanly.
- [ ] `uv run pytest` passes the 7 thin deterministic invariant checks in <2 seconds with zero mocked LLM tests.
- [ ] `uv run agentic-emailer-eval --mode full` passes with `url_grounding_rate == 1.0`, `duplicate_leakage_rate == 0.0`, and `mean_faithfulness >= 4.0`.
- [ ] `uv run agentic-emailer run --config config.ci.json --dry-run` completes cleanly using Google ADK.
- [ ] Fresh-context code review pass (SSRF/security, Google ADK usage, grounding invariants, and spec compliance) completed with all Critical/Important findings resolved before merge.
