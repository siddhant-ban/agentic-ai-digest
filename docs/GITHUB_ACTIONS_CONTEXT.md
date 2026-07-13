# GitHub Actions Scheduling — Implementation Context

> **Audience:** A coding agent with no prior chat history. Use this document to plan and implement scheduled digest runs via GitHub Actions with secure credential handling.
>
> **Status:** Not implemented — planning brief only (as of 2026-07-12).

---

## 1. Goal

Schedule the existing AI news digest pipeline to run automatically on GitHub Actions (cron), replacing or supplementing local Windows Task Scheduler runs.

**Success criteria:**

1. Digest can run in CI without committing secrets to the repo.
2. Local development continues to work unchanged (file-based secrets on laptop).
3. One unified secret-loading pattern: **environment variables first, local files as fallback**.
4. Non-secret configuration is committed and version-controlled.
5. Workflow uses least-privilege permissions and does not log credentials.

---

## 2. Project overview (read this first)

**Agentic Emailer** is a Python CLI that:

1. Reads topics and source settings from a JSON config file.
2. Gathers articles from RSS feeds and DuckDuckGo search (`sources.py`).
3. Summarizes them with Google Gemini (`summarizer.py`).
4. Emails a Markdown/HTML digest via SMTP (`emailer.py`).

**Orchestration:** `aggregator.py` — the only entry point agents need to understand for scheduling.

**Dependency management:** [uv](https://docs.astral.sh/uv/) — use `uv sync` and `uv run python aggregator.py`.

**Existing CLI flags:**

| Flag | Behavior |
|------|----------|
| `--gather-only` | Fetch articles, print list, no Gemini, no email |
| `--dry-run` | Fetch + Gemini summarize, print digest, no email |
| *(none)* | Full run: fetch + summarize + send email |

**Config path:** `--config PATH` (default: `config.json` in project root).

---

## 3. Current credential & config model

### 3.1 Files (local dev today)

| File | Committed? | Purpose |
|------|------------|---------|
| `config.json` | **No** (gitignored) | Topics, RSS feeds, lookback, Gemini model, SMTP settings |
| `config.example.json` | **Yes** | Template with placeholder emails and setup notes |
| `gemini_api_key.txt` | **No** (gitignored) | Gemini API key (single line) |
| `smtp_password.txt` | **No** (gitignored) | Gmail app password (single line) |

### 3.2 How secrets are loaded today (`aggregator.py`)

```python
# Gemini — FILE ONLY (gap for CI)
def load_gemini_api_key() -> str:
    key = load_secret_file(GEMINI_KEY_PATH)  # gemini_api_key.txt
    ...

# SMTP — env var OR file (already CI-friendly)
def load_smtp_password() -> str:
    password = os.environ.get("EMAIL_PASSWORD") or load_secret_file(SMTP_PASSWORD_PATH)
    ...
```

**Key gap:** Gemini has no `GEMINI_API_KEY` env var fallback. SMTP already supports `EMAIL_PASSWORD`.

### 3.3 Config schema (from `config.example.json`)

Required top-level keys validated in `load_config()`:

- `topics` — non-empty list of strings
- `email` — object with at least `sender`, `recipient`; optional `smtp_host`, `smtp_port`, `subject_prefix`

Optional keys used elsewhere:

- `lookback_hours`, `max_articles_per_source`, `rss_feeds`, `search_queries_per_topic`, `gemini_model`
- `_setup_notes` — documentation only, ignored by code

**Security classification:**

| Data | Sensitivity | Recommended CI storage |
|------|-------------|------------------------|
| Gemini API key | **Secret** | GitHub Encrypted Secret → `GEMINI_API_KEY` env |
| Gmail app password | **Secret** | GitHub Encrypted Secret → `EMAIL_PASSWORD` env |
| Topics, RSS URLs, model, lookback | **Non-secret** | Committed JSON file |
| SMTP host/port | **Non-secret** | Committed JSON file |
| Sender/recipient emails | **Semi-sensitive** | OK in committed config if repo is **private**; use secrets/vars if repo is **public** |

---

## 4. Architectural decision (approved direction)

Use a **12-factor split**:

```
┌─────────────────────────────────────────────────────────┐
│  GitHub Actions workflow                                 │
│                                                          │
│  env:                                                    │
│    GEMINI_API_KEY  ← secrets.GEMINI_API_KEY             │
│    EMAIL_PASSWORD  ← secrets.EMAIL_PASSWORD              │
│                                                          │
│  run: uv run python aggregator.py --config config.ci.json│
└─────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
   aggregator.py                  config.ci.json
   (env-first secret loaders)     (committed, no secrets)
```

**Do NOT:**

- Write secrets to files on the runner (`echo "$SECRET" > gemini_api_key.txt`).
- Commit real `config.json`, `gemini_api_key.txt`, or `smtp_password.txt`.
- Store credentials in GitHub **Variables** (use **Secrets**).
- Log or print secret values, config dicts containing secrets, or full env dumps.

**Do:**

- Inject secrets as environment variables at job runtime (GitHub masks them in logs).
- Commit a CI-specific config file with non-secret settings only.
- Keep local file-based loading as fallback for laptop / Task Scheduler use.
- Set workflow `permissions: contents: read` (minimal scope).

---

## 5. Implementation scope

### 5.1 In scope

| # | Task | Priority |
|---|------|----------|
| 1 | Add `GEMINI_API_KEY` env var support to `load_gemini_api_key()` | Required |
| 2 | Align error messages for both secret loaders (mention env var + file options) | Required |
| 3 | Add `config.ci.json` (or equivalent) committed config for CI | Required |
| 4 | Add `.github/workflows/digest.yml` scheduled workflow | Required |
| 5 | Update `README.md` with GitHub Actions setup section | Required |
| 6 | Update `config.example.json` `_setup_notes` to document env vars | Nice-to-have |
| 7 | Update `AGENTIC_WORKFLOW.md` active focus when committing | Required per repo rule |

### 5.2 Out of scope (do not implement unless explicitly asked)

- GitHub Environments with approval gates (optional hardening — document as follow-up).
- OIDC / GCP Workload Identity (overkill for this project).
- Removing local file-based secret loading (must remain for backward compatibility).
- Changing email provider or Gemini auth mechanism.
- Unit test suite (none exists today; only add tests if requested).
- Migrating away from Windows Task Scheduler (both can coexist).

---

## 6. Detailed file changes

### 6.1 `aggregator.py`

**Change `load_gemini_api_key()`** to mirror SMTP:

```python
def load_gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or load_secret_file(GEMINI_KEY_PATH)
    if not key:
        raise FileNotFoundError(
            "Gemini API key not found. Set GEMINI_API_KEY or add gemini_api_key.txt."
        )
    return key
```

**Optionally** tighten `load_smtp_password()` error message for symmetry:

```python
"SMTP password not found. Set EMAIL_PASSWORD or add smtp_password.txt."
```

No other logic changes needed — `aggregator()` already calls these loaders at the right time (Gemini only when not `--gather-only`; SMTP only on full run).

### 6.2 `config.ci.json` (new file)

Create a committed config for CI based on `config.example.json`:

- Copy topics, RSS feeds, timing, and model settings from `config.example.json` (or ask user for preferred values).
- Use placeholder emails (`you@gmail.com`) **or** real emails if implementer confirms repo is private.
- Omit `_setup_notes` or keep minimal — not required by code.
- Do **not** embed API keys or passwords.

Workflow will run: `uv run python aggregator.py --config config.ci.json`

**Alternative considered:** Reuse `config.example.json` directly in CI. Prefer a dedicated `config.ci.json` so example placeholders stay clearly "for humans copying locally" while CI config is explicit and stable.

### 6.3 `.github/workflows/digest.yml` (new file)

Minimum viable workflow:

```yaml
name: Daily AI Digest

on:
  schedule:
    - cron: "0 12 * * *"   # 12:00 UTC daily — adjust as needed
  workflow_dispatch:       # manual trigger for testing

permissions:
  contents: read

jobs:
  digest:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Install dependencies
        run: uv sync

      - name: Run digest
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
        run: uv run python aggregator.py --config config.ci.json
```

**Testing progression for the user:**

1. First run: add a temporary step or manual `workflow_dispatch` with `--dry-run` to validate Gemini without sending email.
2. Then switch to full run once secrets and config are verified.

**Cron note:** GitHub scheduled workflows may be delayed during high load; acceptable for a daily digest.

### 6.4 `README.md`

Add a **GitHub Actions** section covering:

1. Required repo secrets: `GEMINI_API_KEY`, `EMAIL_PASSWORD`
2. How to set them: Settings → Secrets and variables → Actions
3. How to trigger manually: Actions tab → "Daily AI Digest" → Run workflow
4. How to test safely: `--dry-run` locally with env vars set
5. Note that local file-based setup still works for laptop / Task Scheduler

### 6.5 `config.example.json`

Update `_setup_notes.secrets` to mention both env vars:

```
"Store Gemini key in gemini_api_key.txt (or set GEMINI_API_KEY).
 Gmail app password in smtp_password.txt (or set EMAIL_PASSWORD)."
```

---

## 7. Manual setup (user actions — document, do not automate)

The implementing agent **cannot** set GitHub Secrets. Document these steps for the user:

1. Push the branch with workflow + code changes to GitHub.
2. Go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Add:
   - `GEMINI_API_KEY` — from [Google AI Studio](https://aistudio.google.com/apikey) or existing local `gemini_api_key.txt`
   - `EMAIL_PASSWORD` — Gmail app password (requires 2FA; create at https://myaccount.google.com/apppasswords)
4. Edit `config.ci.json` sender/recipient if placeholders are used.
5. Run workflow manually via **workflow_dispatch** before relying on cron.
6. **Recommended:** Use a dedicated Gmail account for sending digests, not a primary personal account.

---

## 8. Local dev compatibility (must not break)

After implementation, these must still work:

```powershell
# Existing local flow (unchanged)
uv sync
# With gemini_api_key.txt, smtp_password.txt, config.json present:
uv run python aggregator.py --dry-run
uv run python aggregator.py
```

```powershell
# New: local env-var flow (same as CI)
$env:GEMINI_API_KEY = "..."
$env:EMAIL_PASSWORD = "..."
uv run python aggregator.py --config config.example.json --dry-run
```

```powershell
# Task Scheduler wrapper (unchanged)
.\run_digest.ps1
```

**Precedence rule everywhere:** env var wins over file.

---

## 9. Security checklist for implementer

- [ ] No secrets in committed files
- [ ] No `echo`, `printenv`, or debug steps that dump secrets in workflow
- [ ] Workflow `permissions` set to minimum (`contents: read`)
- [ ] Secret env vars only referenced via `${{ secrets.* }}`, never hardcoded
- [ ] Error messages don't include secret values (already safe in current code)
- [ ] Do not add secret file paths to workflow artifacts
- [ ] If repo is public, do not commit real email addresses in `config.ci.json`

---

## 10. Verification plan

### Local (agent can run)

```powershell
cd c:\projects\agentic_emailer
uv sync

# Env-var path (simulates CI)
$env:GEMINI_API_KEY = (Get-Content gemini_api_key.txt -Raw).Trim()  # read locally, do not commit
$env:EMAIL_PASSWORD = (Get-Content smtp_password.txt -Raw).Trim()
uv run python aggregator.py --config config.ci.json --gather-only
uv run python aggregator.py --config config.ci.json --dry-run
```

### CI (user verifies)

1. Push to GitHub with secrets configured.
2. Trigger `workflow_dispatch`.
3. Confirm job succeeds and email arrives (or temporarily use `--dry-run` in workflow for first test).

### Regression

- Confirm file-based local run still works when env vars are **unset**.

---

## 11. Related repo conventions

### `AGENTIC_WORKFLOW.md`

When the user requests a git commit, prepend a changelog entry per `.cursor/rules/update-agentic-workflow-on-commit.mdc`. Update `Active focus` to reflect GitHub Actions work.

### `.gitignore`

These must remain ignored (verify `.gitignore` is intact):

```
gemini_api_key.txt
smtp_password.txt
config.json
```

Do **not** gitignore `config.ci.json` — it should be committed.

---

## 12. Key source files reference

| File | Role |
|------|------|
| `aggregator.py` | CLI, config loading, secret loading, orchestration |
| `sources.py` | RSS + DuckDuckGo gathering, `Article` dataclass |
| `summarizer.py` | Gemini API calls via `google-genai` |
| `emailer.py` | SMTP send via `smtplib` + STARTTLS |
| `config.example.json` | Config template |
| `run_digest.ps1` | Windows Task Scheduler wrapper |
| `pyproject.toml` | Dependencies and Python version (`>=3.10`) |
| `AGENTIC_WORKFLOW.md` | Agent-facing project history |

---

## 13. Open decisions (resolve during implementation)

| Decision | Default recommendation |
|----------|------------------------|
| Cron schedule time | `0 12 * * *` (12:00 UTC) — user can adjust |
| Config file name | `config.ci.json` |
| First CI run mode | Start with `--dry-run` in workflow, then switch to full run after user confirms |
| Email addresses in committed config | Placeholders unless user confirms private repo |
| GitHub Environment (`production`) | Skip for v1; document as optional hardening |

---

## 14. Suggested implementation order

1. Read `aggregator.py`, `config.example.json`, `README.md`, this document.
2. Implement env var support for `GEMINI_API_KEY` in `aggregator.py`.
3. Add `config.ci.json`.
4. Add `.github/workflows/digest.yml` (consider `--dry-run` for initial merge).
5. Update `README.md` and `config.example.json` notes.
6. Test locally with env vars (gather-only, then dry-run).
7. On user commit request: update `AGENTIC_WORKFLOW.md` per repo rule.

---

## 15. Example commit message

```
Add GitHub Actions scheduling with env-based secrets

Enable GEMINI_API_KEY env var for CI while keeping local file fallback.
Add scheduled workflow and committed CI config; document secret setup in README.
```

---

## 16. Follow-up work (post-v1)

- Add GitHub `production` environment with branch restrictions.
- Separate CI vs local API keys for easier rotation.
- Add workflow badge to README.
- Consider `--dry-run` as a workflow input (boolean) for manual testing without editing YAML.
