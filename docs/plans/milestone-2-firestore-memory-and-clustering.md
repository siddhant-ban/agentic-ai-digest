# Milestone 2: Cross-Day Memory Store, Semantic Story Clustering & Multi-Day Storylines

> **Antigravity Workflow:** Execute after Milestone 1 merges, in a fresh conversation with `/goal` on branch `milestone-2/memory-and-clustering`. Commit cleanly at each task checkpoint and run a fresh-context review pass before merging.  
> **Master Spec:** `docs/plans/DESIGN_SPEC.md`

> [!IMPORTANT]
> **Verification Policy (Eval-First — NO Mock-Heavy TDD)**
> - **DO NOT** mock Firestore SDK internals or embedding API calls in bloated unit tests.
> - **DO** add only 3 pure-logic deterministic invariant tests in `tests/test_invariants_dedup.py` (using synthetic unit-norm numpy vectors and `LocalJsonMemoryStore`).
> - **DO** validate semantic clustering, cross-day dedup, and multi-day storyline detection via `evals/` (`duplicate_leakage_rate == 0.0` and `developing_story_recall >= 0.80`).

**Goal:** Give the digest agent persistent episodic memory across daily runs via a clean `MemoryStore` protocol (`LocalJsonMemoryStore` for local dev/evals + `FirestoreMemoryStore` for GCP production), batch Gemini embeddings (`gemini-embedding-001`, 768 dims), cosine similarity story clustering, cross-day duplicate suppression, and multi-day developing-storyline linking.

**Architecture:** `MemoryStore` protocol abstracts persistence -> `embeddings.py` embeds candidate articles in one batch API call -> `clustering.py` clusters same-day multi-outlet coverage (`sim >= 0.84`), drops near-duplicate re-hashes of already-sent stories (`sim >= 0.94`), and links evolving multi-day storylines (`0.76 <= sim < 0.94` against prior 7-day clusters) -> `SynthesisAgent` receives enriched `StoryCluster`s with `prior_story_summary`.

**Tech Stack:** `google-cloud-firestore>=2.20.0`, `numpy>=2.0.0`, `google-genai` (`gemini-embedding-001`, `output_dimensionality=768`).

---

## File Structure Map

- **Modify:** `pyproject.toml` (add `google-cloud-firestore`, `numpy`)
- **Create:** `src/agentic_emailer/memory/__init__.py`
- **Create:** `src/agentic_emailer/memory/protocol.py` (`MemoryStore` Protocol + `PriorClusterRecord` model)
- **Create:** `src/agentic_emailer/memory/local_json.py` (`LocalJsonMemoryStore` for local dev & offline evals)
- **Create:** `src/agentic_emailer/memory/firestore_store.py` (`FirestoreMemoryStore` optimized for Always Free quotas + programmatic 30-day pruning)
- **Create:** `src/agentic_emailer/memory/embeddings.py` (Batch `gemini-embedding-001` embedder with `output_dimensionality=768`)
- **Create:** `src/agentic_emailer/memory/clustering.py` (Deterministic cosine clustering, cross-day semantic dedup, developing-storyline linking)
- **Modify:** `src/agentic_emailer/config.py` (add `memory_backend: Literal["local", "firestore"]`, `local_memory_dir`, `memory_lookback_days`, clustering thresholds)
- **Modify:** `src/agentic_emailer/pipeline.py` (integrate `MemoryStore` load/filter -> embeddings -> clustering -> record sent articles/clusters -> 30-day prune)
- **Modify:** `src/agentic_emailer/cli.py` (add `seed-profiles` subcommand to upload `recipients.json` into Firestore or local memory store)
- **Create:** `tests/test_invariants_dedup.py` (3 thin deterministic invariants on synthetic vectors + `LocalJsonMemoryStore`)
- **Modify:** `evals/datasets/golden_corpus.json`, `evals/metrics.py`, `evals/runner.py` (add multi-day storyline & cross-outlet clustering eval cases + `developing_story_recall` and `cluster_compression_ratio` metrics)

---

### Task 1: Add Memory Dependencies & Define `MemoryStore` Protocol

**Files:**
- Modify: `pyproject.toml`
- Create: `src/agentic_emailer/memory/__init__.py`
- Create: `src/agentic_emailer/memory/protocol.py`
- Modify: `src/agentic_emailer/config.py`

**Instructions:**
1. Add `google-cloud-firestore>=2.20.0` and `numpy>=2.0.0` to `pyproject.toml` and run `uv sync --all-groups`.
2. In `src/agentic_emailer/memory/protocol.py`, define:
   - `PriorClusterRecord(cluster_id: str, canonical_title: str, topic: str, summary: str, urls: list[str], embedding: list[float], last_updated_at: datetime)`
   - `@runtime_checkable class MemoryStore(Protocol)`:
     - `async def load_active_profiles(self) -> list[RecipientProfile]: ...`
     - `async def upsert_profiles(self, profiles: list[RecipientProfile]) -> int: ...`
     - `async def get_seen_urls(self, since_days: int = 14) -> set[str]: ...`
     - `async def get_recent_clusters(self, since_days: int = 7) -> list[PriorClusterRecord]: ...`
     - `async def record_sent_articles(self, articles: list[RawArticle], recipient_id: str, cluster_ids: dict[str, str]) -> None: ...`
     - `async def save_story_clusters(self, clusters: list[StoryCluster]) -> None: ...`
     - `async def save_run_metrics(self, metrics: RunMetrics) -> None: ...`
     - `async def prune_expired(self, retention_days: int = 30) -> int: ...`
3. In `src/agentic_emailer/config.py`, add settings:
   - `memory_backend: Literal["local", "firestore"] = "local"`
   - `local_memory_dir: Path = Path(".local_memory")`
   - `seen_url_window_days: int = 14`
   - `storyline_window_days: int = 7`
   - `memory_retention_days: int = 30`
   - `cluster_merge_threshold: float = 0.84`
   - `storyline_link_threshold: float = 0.76`
   - `semantic_dedup_threshold: float = 0.94`

**Verification Commands:**
```powershell
uv sync --all-groups
uv run ruff check src/agentic_emailer/memory src/agentic_emailer/config.py
uv run mypy src/agentic_emailer/memory src/agentic_emailer/config.py
```
Expected: Clean sync, 0 lint/type errors.

**Commit:** `feat: define MemoryStore protocol and memory configuration settings`

---

### Task 2: Implement `LocalJsonMemoryStore` (Dev & Offline Eval Backend)

**Files:**
- Create: `src/agentic_emailer/memory/local_json.py`
- Modify: `.gitignore` (ignore `.local_memory/`)

**Instructions:**
1. Implement `LocalJsonMemoryStore(base_dir: Path)` satisfying `MemoryStore`:
   - Persists clean JSON files (`profiles.json`, `seen_articles.json`, `story_clusters.json`, `runs.json`) inside `base_dir`.
   - Implements date-window filtering (`since_days`), idempotent upserts by ID, and `prune_expired(retention_days)`.
   - Zero network or GCP credentials needed—enables completely reproducible offline eval runs and local development.
2. Add `.local_memory/` to `.gitignore`.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer/memory/local_json.py
uv run mypy src/agentic_emailer/memory/local_json.py
```
Expected: 0 lint/type errors.

**Commit:** `feat: implement LocalJsonMemoryStore for local dev and offline evals`

---

### Task 3: Implement `FirestoreMemoryStore` (Production Always Free Backend)

**Files:**
- Create: `src/agentic_emailer/memory/firestore_store.py`

**Instructions:**
1. Implement `FirestoreMemoryStore(project_id: str | None = None, database: str = "(default)")` using `google.cloud.firestore.AsyncClient` satisfying `MemoryStore`:
   - Collections: `recipients`, `seen_articles`, `story_clusters`, `runs` (matching Section 6 of Master Spec).
   - **Always Free read/write optimization:**
     - `get_seen_urls(since_days)` queries `seen_articles` with a single range filter (`first_seen_at >= cutoff`) and projects only `canonical_url` (1 read per doc, typically <300 reads/day).
     - `get_recent_clusters(since_days)` queries `story_clusters` where `last_updated_at >= cutoff` (typically <150 reads/day).
     - `record_sent_articles(...)` and `save_story_clusters(...)` use `AsyncWriteBatch` (chunked at 400 writes per batch) to minimize round-trips.
     - `prune_expired(retention_days)` deletes documents older than `cutoff` in `seen_articles` and `story_clusters` using batched deletes (avoids Firestore managed TTL billing nuances).
2. Add factory helper `create_memory_store(settings: Settings) -> MemoryStore` in `src/agentic_emailer/memory/__init__.py`.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer/memory
uv run mypy src/agentic_emailer/memory
```
Expected: 0 lint/type errors.

**Commit:** `feat: implement FirestoreMemoryStore with Always Free batching and pruning`

---

### Task 4: Batch Gemini Embeddings & Cosine Story Clustering / Storyline Linker

**Files:**
- Create: `src/agentic_emailer/memory/embeddings.py`
- Create: `src/agentic_emailer/memory/clustering.py`

**Instructions:**
1. **`memory/embeddings.py`:**
   - Implement `async def embed_texts(texts: list[str], settings: Settings, budget: RunBudgetTracker) -> list[list[float]]`:
     - Uses `google.genai.Client` (respecting `GOOGLE_GENAI_USE_VERTEXAI` automatically!) calling `models.embed_content` with `model=settings.embedding_model` (`gemini-embedding-001`) and `types.EmbedContentConfig(output_dimensionality=768, task_type="CLUSTERING")`.
     - Batches up to 64 texts per call to minimize latency and API overhead; records call count in `budget`.
     - L2-normalizes returned vectors so cosine similarity is a single dot product.
2. **`memory/clustering.py`:**
   - Implement `cluster_and_link_stories(articles: list[RawArticle], embeddings: list[list[float]], seen_urls: set[str], prior_clusters: list[PriorClusterRecord], *, merge_threshold: float = 0.84, storyline_threshold: float = 0.76, semantic_dedup_threshold: float = 0.94) -> tuple[list[StoryCluster], int]`:
     - Step 1 (Hard Exact Dedup): drop any article whose `canonical_url` is in `seen_urls`; increment `deduped_count`.
     - Step 2 (Greedy Same-Day Clustering): sort remaining articles newest-first; attach article to existing cluster if cosine similarity to cluster centroid `>= merge_threshold`, updating the normalized centroid; otherwise start a new `StoryCluster`.
     - Step 3 (Cross-Day Semantic Dedup & Storyline Linking): compare each new cluster centroid against `prior_clusters` matrix via `numpy` dot product:
       - If `max_sim >= semantic_dedup_threshold`: suppress entire cluster as a redundant re-hash of an already-covered story; increment `deduped_count`.
       - Else if `max_sim >= storyline_threshold`: set `cluster.is_developing_story = True`, `cluster.prior_story_summary = matched.summary`, and `cluster.prior_digest_date = matched.last_updated_at.strftime("%Y-%m-%d")`.
     - Return `(active_clusters, deduped_count)`.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer/memory/embeddings.py src/agentic_emailer/memory/clustering.py
uv run mypy src/agentic_emailer/memory/embeddings.py src/agentic_emailer/memory/clustering.py
```
Expected: 0 lint/type errors.

**Commit:** `feat: add batch Gemini embeddings, cosine story clustering, and storyline linking`

---

### Task 5: Wire Memory & Clustering into Pipeline + Add `seed-profiles` CLI

**Files:**
- Modify: `src/agentic_emailer/pipeline.py`
- Modify: `src/agentic_emailer/cli.py`

**Instructions:**
1. In `pipeline.py`:
   - Accept optional `memory_store: MemoryStore | None = None` (defaulting to `create_memory_store(settings)`).
   - Load active profiles from `memory_store.load_active_profiles()` (falling back to local `recipients.json` / `config.json` if store has 0 profiles and local config exists).
   - Load `seen_urls` (14d) and `prior_clusters` (7d) upfront before research & clustering.
   - Pass `seen_urls` into `ResearchAgent` context (so it doesn't waste scrapes on already-sent articles!) and into `verify_digest_draft(...)`.
   - Embed newly gathered articles via `embed_texts(...)` and run `cluster_and_link_stories(...)`.
   - Record `articles_deduped_cross_day`, `clusters_formed`, and `developing_stories_linked` in `RunMetrics`.
   - When not `--dry-run` and not `--gather-only`: persist sent articles (`record_sent_articles`), new clusters (`save_story_clusters`), run metrics (`save_run_metrics`), and call `prune_expired(settings.memory_retention_days)`.
2. In `cli.py`:
   - Add `agentic-emailer seed-profiles --from recipients.json [--backend local|firestore]` subcommand to validate and upsert recipient profiles into the active `MemoryStore`.

**Verification Commands:**
```powershell
uv run ruff check src/agentic_emailer
uv run mypy src/agentic_emailer
uv run agentic-emailer run --config config.ci.json --dry-run
```
Expected: Clean lint/type pass; dry-run runs embeddings + clustering + ADK synthesis cleanly.

**Commit:** `feat: integrate cross-day memory, clustering, and profile seeding into pipeline`

---

### Task 6: Add Thin Dedup/Clustering Invariants & Upgrade Eval Suite for Memory + Storylines

**Files:**
- Create: `tests/test_invariants_dedup.py`
- Modify: `evals/datasets/golden_corpus.json`
- Modify: `evals/metrics.py`
- Modify: `evals/runner.py`
- Modify: `evals/reports/baseline.json`

**Instructions:**
1. **`tests/test_invariants_dedup.py` (3 pure-logic deterministic checks, ZERO mocks):**
   - `test_same_day_clustering_merges_near_vectors_and_separates_orthogonal_topics`
   - `test_cross_day_thresholds_suppress_rehash_and_link_developing_story`
   - `test_local_json_memory_store_roundtrip_and_pruning`
2. **Upgrade `evals/`:**
   - Extend `evals/datasets/golden_corpus.json` with `prior_clusters` containing:
     - 1 story that should be **suppressed** as a near-duplicate re-hash (`sim >= 0.94`).
     - 2 stories that have **new follow-up developments** today (expected `story_status == "developing_update"`).
     - 1 story covered by **3 different outlets** today (expected compression into 1 multi-source highlight).
   - Add `developing_story_recall` (gate `>= 0.80`) and `cluster_compression_ratio` to `evals/metrics.py` and `evals/runner.py`.
   - Re-run `uv run agentic-emailer-eval --mode full --save-baseline` to update `evals/reports/baseline.json`.

**Verification Commands:**
```powershell
uv run pytest -v
uv run ruff check src tests evals
uv run mypy src evals
uv run agentic-emailer-eval --mode full --save-baseline
```
Expected: All 10 thin deterministic invariant checks pass; full eval passes with `url_grounding_rate == 1.0`, `duplicate_leakage_rate == 0.0`, and `developing_story_recall >= 0.80`.

**Commit:** `feat: add memory invariants and multi-day storyline evals`

---

## Milestone 2 Exit Gate
- [ ] Running `agentic-emailer run --dry-run` twice against a seeded `.local_memory/` shows cross-day dedup & cluster persistence working without errors.
- [ ] `uv run pytest` passes all 10 thin invariant checks in <2 seconds.
- [ ] `uv run agentic-emailer-eval --mode full` meets all hard thresholds (`url_grounding_rate == 1.0`, `duplicate_leakage_rate == 0.0`, `developing_story_recall >= 0.80`, `mean_faithfulness >= 4.0`).
- [ ] Fresh-context code review pass (Firestore Always Free query/batching discipline, cosine clustering edge cases, and spec compliance) completed with all Critical/Important findings resolved before merge.
