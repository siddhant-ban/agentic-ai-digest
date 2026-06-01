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

- Python 3.x
- A Gemini API key
- SMTP access:
  - For Gmail: you typically need an **App Password** (after enabling 2FA)

## Setup

### 1) Create a virtual environment and install dependencies

```powershell
cd c:\projects\agentic_emailer
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2) Configure secrets (never commit these)

- Gemini key: `gemini_api_key.txt`
- SMTP password:
  - Preferred: `smtp_password.txt`
  - Or set environment variable `EMAIL_PASSWORD`

The code loads these in:
- `aggregator.py` (`gemini_api_key.txt`, `smtp_password.txt` / `EMAIL_PASSWORD`)
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

## Run locally (no scheduling yet)

From the repo directory:

```powershell
python aggregator.py --gather-only
python aggregator.py --dry-run
python aggregator.py
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

The wrapper runs `aggregator.py` using the repo’s `.venv` if it exists.

## Project structure

- `aggregator.py`: orchestration + CLI flags
- `sources.py`: RSS + DuckDuckGo collection, dedupe, normalization (`Article`)
- `summarizer.py`: Gemini digest generation (with fallback)
- `emailer.py`: SMTP multipart email sender (text + HTML)

## Gmail note (app passwords)

If you see “set up an app password” in Gmail SMTP settings, the project is already designed for that:
- it authenticates with `smtp_password.txt` (or `EMAIL_PASSWORD`) via `server.login(...)`.

