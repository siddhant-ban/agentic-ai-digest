# Agentic Emailer: AI News Aggregator

This project runs locally on your laptop to gather the latest AI-related updates from multiple internet sources, summarize them with Gemini, and email you a daily digest.

## What it does

1. Reads your topic list from `config.json`
2. Gathers recent articles from:
   - RSS feeds (`feedparser`)
   - DuckDuckGo news/text search (`duckduckgo-search` via `DDGS`)
3. Deduplicates and filters results
4. Summarizes everything into a Markdown digest using Gemini (`google-genai`)
5. Emails the digest via SMTP (Gmail compatible)

## Prerequisites

- `uv` installed ([installation guide](https://docs.astral.sh/uv/getting-started/installation/))
- A Gemini API key
- SMTP access:
  - For Gmail: you typically need an **App Password** (after enabling 2FA)

## Setup

### 1) Sync dependencies from `pyproject.toml`

```powershell
cd c:\projects\agentic_emailer
uv sync
```

### 2) Configure secrets (never commit these)

- Gemini key:
  - Preferred: `gemini_api_key.txt`
  - Or set environment variable `GEMINI_API_KEY`
- SMTP password:
  - Preferred: `smtp_password.txt`
  - Or set environment variable `EMAIL_PASSWORD`

The code loads these in:
- `aggregator.py` (`GEMINI_API_KEY` / `gemini_api_key.txt`, `EMAIL_PASSWORD` / `smtp_password.txt`)
- `emailer.py` uses the SMTP password as the argument to `server.login(...)`

## Configure topics and delivery

Edit `config.json`:

- `topics`: list of topic strings
- `lookback_hours`: how far back to include articles
- `rss_feeds`: RSS sources (optional but recommended)
- `search_queries_per_topic`: how many search queries per topic
- `gemini_model`: model name for Gemini
- `email`: SMTP settings (`smtp_host`, `smtp_port`, `sender`, `recipient`, `subject_prefix`)

Example config notes are in `config.example.json`.

## Run locally

From the repo directory:

```powershell
uv run python aggregator.py --gather-only
uv run python aggregator.py --dry-run
uv run python aggregator.py
```

- `--gather-only`: fetch + print raw article list
- `--dry-run`: summarize with Gemini, print digest, do not email
- no flags: summarize + email

## Windows Task Scheduler

`run_digest.ps1` is provided as a wrapper for Task Scheduler.

Typical Task Scheduler setup:

1. Open Task Scheduler
2. Create Basic Task (daily trigger)
3. Action: Start a program
4. Program/script: `powershell.exe`
5. Arguments:
   ```powershell
   -ExecutionPolicy Bypass -File "c:\projects\agentic_emailer\run_digest.ps1"
   ```
6. Start in: `c:\projects\agentic_emailer`

The wrapper runs `aggregator.py` through `uv run`.

## GitHub Actions

The repo includes a scheduled workflow at `.github/workflows/digest.yml` that runs the digest daily at 12:00 UTC (plus manual triggers).

### Required repository secrets

Set these under **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Source |
|--------|--------|
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) or your local `gemini_api_key.txt` |
| `EMAIL_PASSWORD` | Gmail app password ([create one](https://myaccount.google.com/apppasswords) after enabling 2FA) |

### CI config

Non-secret settings live in `config.ci.json` (committed). The workflow runs:

```powershell
uv run python aggregator.py --config config.ci.json --dry-run
```

The workflow starts in `--dry-run` mode so you can validate Gemini without sending email. After a successful manual run, remove `--dry-run` from the workflow to enable live email delivery.

### Manual trigger

1. Open the **Actions** tab in GitHub
2. Select **Daily AI Digest**
3. Click **Run workflow**

### Test locally with env vars (same as CI)

```powershell
$env:GEMINI_API_KEY = (Get-Content gemini_api_key.txt -Raw).Trim()
$env:EMAIL_PASSWORD = (Get-Content smtp_password.txt -Raw).Trim()
uv run python aggregator.py --config config.ci.json --dry-run
```

Local file-based setup (`gemini_api_key.txt`, `smtp_password.txt`, `config.json`) and Windows Task Scheduler continue to work unchanged. Environment variables take precedence over files when both are set.

## Project structure

- `aggregator.py`: orchestration + CLI flags
- `sources.py`: RSS + DuckDuckGo collection, dedupe, normalization (`Article`)
- `summarizer.py`: Gemini digest generation (with fallback)
- `emailer.py`: SMTP multipart email sender (text + HTML)
- `pyproject.toml`: dependency and environment management for `uv`

## Gmail note (app passwords)

If you see “set up an app password” in Gmail SMTP settings, the project is already designed for that:
- it authenticates with `smtp_password.txt` (or `EMAIL_PASSWORD`) via `server.login(...)`.

