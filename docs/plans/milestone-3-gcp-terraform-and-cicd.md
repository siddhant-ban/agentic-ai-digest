# Milestone 3: Containerization, Terraform GCP IaC, Keyless WIF CI/CD & Cost Guardrails

> **Antigravity Workflow:** Execute after Milestone 2 merges, in a fresh conversation with `/boost` (or `/goal`) on branch `milestone-3/gcp-terraform-cicd`. Commit cleanly at each task checkpoint and run a fresh-context security & cost review pass before merging.  
> **Master Spec:** `docs/plans/DESIGN_SPEC.md`

> [!IMPORTANT]
> **Verification Policy (Declarative Infra Validation)**
> - **DO NOT** write Python unit tests mocking Terraform or GitHub Actions.
> - **DO** verify via `docker build`, local container smoke test (`--dry-run` / `--help`), `terraform fmt -check`, `terraform validate`, `terraform plan` (once GCP project variables are supplied), and GitHub Actions YAML validation.
> - **Strict Always Free Guardrails:**
>   1. Default region MUST be `us-central1` (Tier-1 Always Free eligible for Cloud Run & Firestore).
>   2. Artifact Registry MUST include a cleanup policy keeping ≤3 recent images (<0.5 GiB).
>   3. Secret Manager MUST contain ≤2 secrets (`smtp-password` only in Vertex AI mode) so total active versions stay ≤6.
>   4. Cloud Scheduler MUST provision only 1 job (≤3 Always Free limit).

**Goal:** Package the application in a lean, secure multi-stage container, codify the entire enterprise GCP architecture in **Terraform** (Cloud Run Job, Cloud Scheduler, Firestore, Secret Manager, Artifact Registry, 3 least-privilege Service Accounts, GitHub OIDC Workload Identity Federation, and Billing Budgets), and automate validation + keyless deployment via GitHub Actions.

**Tech Stack:** Docker (multi-stage `python:3.12-slim` + `uv`), Terraform `>= 1.9`, Google Cloud Provider `hashicorp/google >= 6.0`, GitHub Actions with `google-github-actions/auth@v2` (WIF).

---

## File Structure Map

- **Create:** `Dockerfile` (multi-stage `uv` production build, non-root user, slim image <350 MB)
- **Create:** `.dockerignore`
- **Create:** `infra/terraform/versions.tf`
- **Create:** `infra/terraform/variables.tf`
- **Create:** `infra/terraform/terraform.tfvars.example`
- **Create:** `infra/terraform/main.tf` (APIs, Artifact Registry + cleanup policy, Firestore Native DB, Secret Manager shell)
- **Create:** `infra/terraform/iam.tf` (3 least-privilege SAs + GitHub OIDC Workload Identity Pool & Provider)
- **Create:** `infra/terraform/cloud_run.tf` (Cloud Run Job v2 + Cloud Scheduler daily HTTP trigger)
- **Create:** `infra/terraform/budgets.tf` (GCP Billing Budget alerts at \$2 / \$5 / \$8 / \$10)
- **Create:** `infra/terraform/outputs.tf` (WIF provider path, deploy SA email, Artifact Registry repo URL, Cloud Run Job name)
- **Create:** `docs/GCP_BOOTSTRAP.md` (exact 10-minute one-time user bootstrap steps: link Google AI Pro billing, add SMTP secret version, run `terraform apply`, seed Firestore profiles)
- **Delete:** `.github/workflows/digest.yml`, `.github/workflows/test.yml` (superseded by new CI/CD workflows)
- **Create:** `.github/workflows/ci.yml` (PR gate: ruff, mypy, thin invariants, deterministic offline evals, terraform fmt/validate)
- **Create:** `.github/workflows/deploy.yml` (main push: keyless WIF auth -> Docker build & push -> `gcloud run jobs update`)

---

### Task 1: Multi-Stage Production Dockerfile & `.dockerignore`

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

**Instructions:**
1. Create `.dockerignore` excluding `.git`, `.venv`, `.local_memory`, `tests`, `evals/reports`, `infra`, `docs`, `*.txt` (secrets!), `recipients.json`, `config.json`, `__pycache__`, `.pytest_cache`.
2. Create a lean multi-stage `Dockerfile`:
   - Stage 1 (`builder`): `FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder`, copy `pyproject.toml`, `uv.lock`, `src/`, run `uv sync --frozen --no-dev --no-editable` (installing cleanly into `/app/.venv`).
   - Stage 2 (`runtime`): `FROM python:3.12-slim-bookworm AS runtime`:
     - Create non-root user `appuser` (`uid 10001`).
     - Copy `/app/.venv` from builder; set `PATH="/app/.venv/bin:$PATH"` and `PYTHONUNBUFFERED=1`.
     - Switch to `USER appuser`.
     - `ENTRYPOINT ["agentic-emailer", "run"]`.
3. Verify image size is well under 500 MB (target ~200–350 MB so 3 retained images stay easily under Artifact Registry's 0.5 GiB free tier!).

**Verification Commands:**
```powershell
docker build -t agentic-emailer:local .
docker images agentic-emailer:local --format "{{.Repository}}:{{.Tag}} {{.Size}}"
docker run --rm agentic-emailer:local --help
```
Expected: Build succeeds cleanly, image size < 400 MB, `--help` prints CLI usage as non-root user.

**Commit:** `build: add lean multi-stage uv Dockerfile for Cloud Run Jobs`

---

### Task 2: Terraform Foundation — APIs, Artifact Registry (+ Cleanup), Firestore & Secret Manager

**Files:**
- Create: `infra/terraform/versions.tf`
- Create: `infra/terraform/variables.tf`
- Create: `infra/terraform/terraform.tfvars.example`
- Create: `infra/terraform/main.tf`

**Instructions:**
1. **`versions.tf`:** Require Terraform `>= 1.9` and `hashicorp/google ~> 6.0`. Document optional GCS remote state backend block (commented for single-developer bootstrap with clear instructions).
2. **`variables.tf`:** Define typed variables with sensible Always Free defaults:
   - `project_id` (required), `billing_account_id` (optional — enables budget alert when set), `region` (default `"us-central1"`), `github_repo` (e.g. `"siddhant-ban/agentic-ai-digest"`), `smtp_sender` (required), `schedule_cron` (default `"0 12 * * *"`), `schedule_timezone` (default `"Etc/UTC"`), `research_model` (default `"gemini-3.1-flash-lite"`), `synthesis_model` (default `"gemini-3.1-flash-lite"`), `critic_model` (default `"gemini-3.1-flash-lite"`), `monthly_budget_usd` (default `10`).
3. **`main.tf`:**
   - Enable required GCP services via `google_project_service` (`run`, `cloudscheduler`, `firestore`, `secretmanager`, `artifactregistry`, `aiplatform`, `cloudtrace`, `logging`, `iam`, `iamcredentials`, `sts`, `billingbudgets`).
   - Provision `google_artifact_registry_repository` (`agentic-emailer`, format `DOCKER`) with `cleanup_policies`:
     - Policy 1 (`keep-recent-3`): `action = "KEEP"`, `most_recent_versions { keep_count = 3 }`.
     - Policy 2 (`delete-untagged`): `action = "DELETE"`, `condition { tag_state = "UNTAGGED", older_than = "86400s" }`.
   - Provision `google_firestore_database` (`name = "(default)"`, `location_id = var.region`, `type = "FIRESTORE_NATIVE"`, `delete_protection_state = "DELETE_PROTECTION_DISABLED"`).
   - Provision `google_secret_manager_secret` (`secret_id = "agentic-emailer-smtp-password"`, automatic replication) — **note:** secret *version* value is added out-of-band via `gcloud secrets versions add` so plaintext credentials never touch Terraform state!

**Verification Commands:**
```powershell
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
```
Expected: `Success! The configuration is valid.`

**Commit:** `infra: add Terraform core GCP services, Artifact Registry cleanup, Firestore, and Secret Manager`

---

### Task 3: Least-Privilege IAM & Keyless GitHub Actions Workload Identity Federation

**Files:**
- Create: `infra/terraform/iam.tf`

**Instructions:**
1. Create 3 distinct least-privilege Service Accounts:
   - **`digest-runner-sa`:**
     - `roles/aiplatform.user` (Vertex AI Gemini + embeddings)
     - `roles/datastore.user` (Firestore read/write)
     - `roles/cloudtrace.agent` (OpenTelemetry Cloud Trace export)
     - `roles/logging.logWriter` (Structured Cloud Logging)
     - Scoped secret IAM (`google_secret_manager_secret_iam_member` on `agentic-emailer-smtp-password` ONLY): `roles/secretmanager.secretAccessor`.
   - **`scheduler-invoker-sa`:**
     - Scoped `roles/run.invoker` granted ONLY on the Cloud Run Job (defined in `cloud_run.tf`).
   - **`github-deploy-sa`:**
     - `roles/artifactregistry.writer` (scoped to the Artifact Registry repo)
     - `roles/run.developer` (update Cloud Run Job image)
     - `roles/iam.serviceAccountUser` (scoped ONLY to `digest-runner-sa`).
2. Provision **Workload Identity Federation (WIF)**:
   - `google_iam_workload_identity_pool` (`github-actions-pool`)
   - `google_iam_workload_identity_pool_provider` (`github-oidc-provider`, issuer `https://token.actions.githubusercontent.com`, `attribute_condition = "assertion.repository == '${var.github_repo}' && assertion.ref == 'refs/heads/main'"` so PRs from forks can NEVER authenticate!).
   - Bind `roles/iam.workloadIdentityUser` on `github-deploy-sa` to that exact repository principal.

**Verification Commands:**
```powershell
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform validate
```
Expected: Valid configuration with zero warnings.

**Commit:** `infra: add least-privilege service accounts and GitHub OIDC Workload Identity Federation`

---

### Task 4: Cloud Run Job, Cloud Scheduler & Monthly Billing Budget Alerts

**Files:**
- Create: `infra/terraform/cloud_run.tf`
- Create: `infra/terraform/budgets.tf`
- Create: `infra/terraform/outputs.tf`

**Instructions:**
1. **`cloud_run.tf`:**
   - Provision `google_cloud_run_v2_job` (`name = "agentic-digest-job"`):
     - Container image pointing to Artifact Registry `latest` tag (with `lifecycle { ignore_changes = [template[0].template[0].containers[0].image] }` so CI/CD image updates don't cause Terraform drift!).
     - Resources: `cpu = "1"`, `memory = "512Mi"`.
     - Execution limits: `max_retries = 1`, `timeout = "600s"`.
     - `service_account = google_service_account.digest_runner.email`.
     - Environment variables: `GOOGLE_GENAI_USE_VERTEXAI="true"`, `GOOGLE_CLOUD_PROJECT=var.project_id`, `GOOGLE_CLOUD_LOCATION=var.region`, `MEMORY_BACKEND="firestore"`, `SMTP_SENDER=var.smtp_sender`, `RESEARCH_MODEL=var.research_model`, `SYNTHESIS_MODEL=var.synthesis_model`, `CRITIC_MODEL=var.critic_model`, `OTEL_CLOUD_TRACE_ENABLED="true"`.
     - Secret env var `EMAIL_PASSWORD` mounted directly from `agentic-emailer-smtp-password` (`version = "latest"`).
   - Grant `roles/run.invoker` on this job to `scheduler-invoker-sa`.
   - Provision `google_cloud_scheduler_job` (`name = "daily-agentic-digest"`):
     - `schedule = var.schedule_cron`, `time_zone = var.schedule_timezone`.
     - `http_target`: `POST https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/agentic-digest-job:run` with `oauth_token { service_account_email = google_service_account.scheduler_invoker.email }`.
2. **`budgets.tf`:**
   - Conditionally provision `google_billing_budget` (when `var.billing_account_id != ""`):
     - `amount = var.monthly_budget_usd` (default \$10 USD), scoped to `projects/${var.project_id}`.
     - Threshold rules at `0.2`, `0.5`, `0.8`, `1.0` (`CURRENT_SPEND` + `0.8` `FORECASTED_SPEND`).
3. **`outputs.tf`:** Output `workload_identity_provider`, `github_deploy_sa_email`, `artifact_registry_repo`, and `cloud_run_job_name`.

**Verification Commands:**
```powershell
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform validate
```
Expected: `Success! The configuration is valid.`

**Commit:** `infra: add Cloud Run Job, Cloud Scheduler trigger, and billing budget alerts`

---

### Task 5: GitHub Actions CI & Keyless CD Workflows + Bootstrap Guide

**Files:**
- Delete: `.github/workflows/digest.yml`, `.github/workflows/test.yml`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/deploy.yml`
- Create: `docs/GCP_BOOTSTRAP.md`

**Instructions:**
1. **`.github/workflows/ci.yml`** (triggers on PRs and pushes to `main`):
   - Job 1 (`code-and-invariants`): `uv sync --all-groups`, `uv run ruff check src tests evals`, `uv run mypy src evals`, `uv run pytest -v`, `uv run agentic-emailer-eval --mode deterministic` (zero live LLM spend on PRs).
   - Job 2 (`terraform-validate`): `hashicorp/setup-terraform@v3`, `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`.
   - Job 3 (`docker-build-check`): `docker build .` (validates container builds cleanly on every PR without pushing).
2. **`.github/workflows/deploy.yml`** (triggers on push to `main` or `workflow_dispatch`):
   - `permissions: { contents: read, id-token: write }` (required for keyless OIDC WIF!).
   - Uses `google-github-actions/auth@v2` with `vars.GCP_WORKLOAD_IDENTITY_PROVIDER` and `vars.GCP_DEPLOY_SA_EMAIL`.
   - Builds Docker image tagged with `${{ github.sha }}` and `latest`, pushes to Artifact Registry, and updates `agentic-digest-job` via `gcloud run jobs update agentic-digest-job --image ... --region us-central1`.
3. **`docs/GCP_BOOTSTRAP.md`:** Step-by-step runbook (written clearly for non-DevOps engineers) covering:
   - Linking Google AI Pro \$10/month credits to the GCP project.
   - Running `terraform apply`.
   - Adding `smtp-password` secret version (and remembering to destroy old versions to stay ≤6).
   - Seeding `recipients.json` into Firestore (`uv run agentic-emailer seed-profiles --from recipients.json --backend firestore`).
   - Setting the 4 non-secret GitHub Repository Variables (`GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_DEPLOY_SA_EMAIL`).
   - Triggering a first live test run via `gcloud run jobs execute agentic-digest-job --region us-central1 --wait`.

**Verification Commands:**
```powershell
uv run ruff check src tests evals
uv run pytest -v
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform validate
```
Expected: All local CI gates pass cleanly.

**Commit:** `ci: replace legacy workflows with CI gate, keyless WIF Cloud Run deploy, and GCP bootstrap runbook`

---

## Milestone 3 Exit Gate
- [ ] `docker build` produces a <400 MB non-root image and runs `--help` cleanly.
- [ ] `terraform fmt -check` and `terraform validate` pass with zero errors.
- [ ] Fresh-context security & cost review pass (least-privilege IAM, GitHub OIDC WIF `attribute_condition`, container non-root hygiene, and Always Free quota guardrails) completed before merge.
- [ ] User follows `docs/GCP_BOOTSTRAP.md`, runs `terraform apply`, seeds Firestore profiles, and verifies one manual execution of `agentic-digest-job` in GCP Cloud Run Jobs.
- [ ] Pushing to `main` triggers `.github/workflows/deploy.yml` and updates the Cloud Run Job via keyless WIF with zero static JSON keys.
