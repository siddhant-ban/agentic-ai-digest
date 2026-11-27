# Agent Guidelines — Agentic Emailer

> Master design spec: `docs/plans/DESIGN_SPEC.md`  
> Milestone plans: `docs/plans/milestone-*.md`

## Core Engineering & Verification Policy
- **Eval-First (No Mock-Heavy Unit TDD):** Never write unit tests that mock `genai.Client`, Google ADK `Runner`, Firestore SDK internals, or assert on prompt text fragments.
- **Thin Deterministic Invariants Only (`tests/`):** Keep `tests/` restricted to fast (<2s), zero-mock pure-logic invariants (URL normalization, grounding verification, dedup/clustering math, budget caps, SSRF blocking).
- **Real Quality Verification (`evals/`):** Validate agent quality, grounding, duplicate suppression, and storylines via `uv run agentic-emailer-eval` and live `--dry-run` smoke runs.
- **GCP Always Free + $10/mo Budget Guardrails:** Keep default GCP region `us-central1`, Artifact Registry cleanup ≤3 images (<0.5 GiB), Secret Manager ≤6 active secret versions total, Cloud Scheduler ≤3 jobs, and programmatic 30-day Firestore pruning instead of managed TTL.
- **Scoped Review Discipline:** Fix Critical and Important bugs (security, correctness, spec violations, billing/quota risks). Log minor polish or out-of-scope edge cases in your report instead of triggering additional full revision loops.

## Windows + `uv` + Antigravity Shell Guardrails
- **Always set `NotificationTimeoutSeconds` on `run_command`:** Use `30`–`60` seconds for local checks/probes/tests and `180` seconds for live API, eval, or Docker commands so a stuck background process wakes you automatically instead of hanging silently.
- **Windows Sandbox + `asyncio.run()` Deadlock:** On Windows Python 3.14, `asyncio.run()` hangs indefinitely inside the terminal sandbox (`ProactorEventLoop._make_self_pipe` blocks on `socket.accept()` even without external network calls).
  - Keep pure deterministic tests, linters, and `--mode deterministic` evals strictly synchronous.
  - Any command invoking `asyncio.run()` (async probes, live RSS/Gemini dry-runs, `--mode full` evals) will stall in the sandbox; recognize an empty-log timeout quickly, kill the stuck task with `manage_task`, and rerun that command with network/sandbox bypass enabled.
- **Offline `uv` Invocations:** Inside the offline sandbox (once dependencies are already synced), prefer `uv run --no-sync ...` or `.venv\Scripts\python.exe ...` so `uv` does not attempt to reach PyPI for build backends like `hatchling`.
- **PowerShell Hygiene:** Use Windows PowerShell 5.1 syntax (`;` or `; if ($LASTEXITCODE -eq 0) { ... }` instead of `&&`, and PowerShell cmdlets instead of `ls -la`). Never pipe large multiline PowerShell here-strings (`@' ... '@`) into `python -`; write temporary probe scripts to your scratch directory instead.
- **Git Lock Files on Windows:** Git write commands (`checkout`, `pull`, `commit`, `push`) may fail inside the sandbox with `.git/index.lock` / `FETCH_HEAD` permission denied; retry only the git write step outside the sandbox if blocked.
- **Keep Existing CI/Workflows Consistent:** If a milestone deletes or renames a script referenced by `.github/workflows/`, update or explicitly document the workflow impact in the same milestone so merges never leave `main` red.
