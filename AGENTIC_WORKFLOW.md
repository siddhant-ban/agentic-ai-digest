# Agentic Workflow Log

> Auto-maintained context for AI coding agents. Updated on each git commit to preserve what changed, why, and what to know for future sessions.

## Current state

- **Project:** Agentic Emailer — local AI news aggregator that emails Gemini-summarized digests
- **Stack:** Python, `uv`, Gemini (`google-genai`), RSS + DuckDuckGo (`sources.py`), SMTP (`emailer.py`)
- **Entry point:** `aggregator.py` (CLI: `--gather-only`, `--dry-run`, default sends email)
- **Secrets (never commit):** `gemini_api_key.txt`, `smtp_password.txt`, `config.json`
- **Last updated:** 2026-07-12

## Active focus

- Agentic workflow tracking established; ready for ongoing commit-time context updates

## Change log

Newest entries first. Each entry corresponds to one git commit.

---

### 2026-07-12 — Add agentic workflow tracking

- **Commit:** `5f79de8`
- **Summary:** Introduced `AGENTIC_WORKFLOW.md` and a Cursor rule so every future commit appends a context entry for later agent sessions.
- **Files:** `AGENTIC_WORKFLOW.md`, `.cursor/rules/update-agentic-workflow-on-commit.mdc`
- **Context:** Read this file at the start of agent sessions for project history. The commit rule prepends a new entry before each commit and amends the hash after commit succeeds.

---

### 2026-06-23 — Merge agentic-ai-digest/main, keep local README and .gitignore

- **Commit:** `134e0b4`
- **Summary:** Merged remote `agentic-ai-digest/main` into local `main`, keeping the fuller local README and project-specific `.gitignore` entries over minimal remote stubs.
- **Files:** `README.md`, `.gitignore`
- **Context:** Local branch has the complete project docs and secret-file gitignore rules; remote had only stubs.

---

### 2026-05-31 — Added uv version management

- **Commit:** `70d34aa`
- **Summary:** Migrated dependency management to `uv` with `pyproject.toml` and `uv.lock`.
- **Files:** `pyproject.toml`, `uv.lock`, related config
- **Context:** Use `uv sync` and `uv run` instead of raw `pip`/`python` for local runs and Task Scheduler wrapper.

---

### 2026-05-31 — Added a readme

- **Commit:** `e9c05f8`
- **Summary:** Wrote project README with setup, CLI usage, and Windows Task Scheduler instructions.
- **Files:** `README.md`
- **Context:** README is the human-facing onboarding doc; this file is the agent-facing change history.

---

### 2026-05-31 — Add initial implementation of AI news aggregator

- **Commit:** `1175f79`
- **Summary:** Built core pipeline: gather articles from RSS/search, summarize with Gemini, email digest via SMTP.
- **Files:** `aggregator.py`, `sources.py`, `summarizer.py`, `emailer.py`, `config.example.json`, `run_digest.ps1`, `requirements.txt`
- **Context:** Orchestration lives in `aggregator.py`; article normalization in `sources.py` (`Article` dataclass); Gemini digest in `summarizer.py`.

---

### 2026-05-31 — Initial commit

- **Commit:** `1454347`
- **Summary:** Created repository with `.gitignore` and stub `README.md`.
- **Files:** `.gitignore`, `README.md`
- **Context:** Greenfield start before agentic implementation.
