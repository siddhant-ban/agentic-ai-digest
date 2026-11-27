# GCP Production Bootstrap Runbook

This guide walks through the one-time (~10-minute) setup to provision **Agentic Emailer** on Google Cloud using Terraform, keyless GitHub Actions Workload Identity Federation (WIF), and GCP Always Free + $10/month Google AI Pro developer credits.

---

## Prerequisites

Install and verify these CLIs locally:
- [Google Cloud CLI (`gcloud`)](https://cloud.google.com/sdk/docs/install)
- [Terraform `>= 1.9`](https://developer.hashicorp.com/terraform/install)
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- Optional: Docker Desktop (only needed if building the initial bootstrap image locally; Cloud Build can be used instead)

---

## Step 1: Create GCP Project & Link Google AI Pro Credits

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create (or select) a dedicated project (e.g., `agentic-emailer-prod`).
2. Claim and link your **$10/month Google AI Pro / Google Developer Program** billing credits to your billing account, then attach that billing account to your project (**Billing → Link a billing account**).
3. Note your **Project ID** and **Billing Account ID** (format `XXXXXX-XXXXXX-XXXXXX` under **Billing → Account management**).
4. Authenticate `gcloud` and set your active project:

```powershell
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_GCP_PROJECT_ID
```

---

## Step 2: Configure Terraform Variables

Copy the example variables file and fill in your project details (`terraform.tfvars` is gitignored):

```powershell
Copy-Item infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
```

Edit `infra/terraform/terraform.tfvars`:
- `project_id`: your GCP Project ID
- `billing_account_id`: your Billing Account ID (or `""` to skip budget alerts)
- `region`: keep `"us-central1"` (Tier-1 Always Free for Cloud Run & Firestore)
- `github_repo`: `"siddhant-ban/agentic-ai-digest"` (or your fork in `owner/repo` format)
- `smtp_sender`: your Gmail sender address

---

## Step 3: Bootstrap Registry & Secret Shell, Push Initial Image, and Add SMTP Secret

Initialize Terraform and review the plan:

```powershell
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform plan
```

> **Why a two-step first apply?** When creating `agentic-digest-job`, Cloud Run Jobs validates that **both** the initial container image in Artifact Registry **and** a `latest` version of the `agentic-emailer-smtp-password` secret already exist. On a brand-new project, create the Artifact Registry repo and Secret Manager shell first, populate them out-of-band (so plaintext credentials never enter Terraform state), and then apply the full stack.

### 3a. Enable APIs and Create Artifact Registry + Secret Manager Shell

```powershell
terraform -chdir=infra/terraform apply `
  -target=google_artifact_registry_repository.docker `
  -target=google_secret_manager_secret.smtp_password
```

### 3b. Push the Initial Container Image (Choose ONE Option)

```powershell
# Option A (no local Docker required — uses Cloud Build's 120 free build-min/day):
gcloud services enable cloudbuild.googleapis.com
gcloud builds submit --tag us-central1-docker.pkg.dev/YOUR_GCP_PROJECT_ID/agentic-emailer/agentic-emailer:latest .

# Option B (if Docker Desktop is installed locally):
# gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
# docker build -t us-central1-docker.pkg.dev/YOUR_GCP_PROJECT_ID/agentic-emailer/agentic-emailer:latest .
# docker push us-central1-docker.pkg.dev/YOUR_GCP_PROJECT_ID/agentic-emailer/agentic-emailer:latest
```

### 3c. Add the SMTP Password Secret Version (Out-of-Band)

Add your Gmail App Password directly to Secret Manager so plaintext credentials never touch Terraform state:

**PowerShell (writes exact UTF-8 bytes without trailing CRLF):**
```powershell
$pw = Read-Host "Enter Gmail App Password" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw)
$tmp = [System.IO.Path]::GetTempFileName()
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [System.IO.File]::WriteAllText($tmp, $plain)
    gcloud secrets versions add agentic-emailer-smtp-password --data-file=$tmp
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    $plain = $null
}
```

**macOS / Linux (bash):**
```bash
printf '%s' "YOUR_GMAIL_APP_PASSWORD" | gcloud secrets versions add agentic-emailer-smtp-password --data-file=-
```

> **Always Free Guardrail (<= 6 Active Secret Versions):** When rotating your SMTP password in the future, destroy old versions (`gcloud secrets versions destroy <VERSION> --secret=agentic-emailer-smtp-password`) so total active versions remain `<= 6`.

---

## Step 4: Provision the Complete GCP Stack (`terraform apply`)

Now apply the full Terraform stack (IAM, WIF, Firestore, Cloud Run Job, Cloud Scheduler, and Billing Budget alerts):

```powershell
terraform -chdir=infra/terraform apply
```

Capture the Terraform outputs for Step 6:

```powershell
terraform -chdir=infra/terraform output
```

---

## Step 5: Seed Recipient Profiles into Firestore

Populate the Firestore `recipients` collection from your local `recipients.json`:

```powershell
$env:GCP_PROJECT_ID = "YOUR_GCP_PROJECT_ID"
uv run agentic-emailer seed-profiles --from recipients.json --backend firestore
```

You can re-run this command anytime you add, disable, or update recipient topics/RSS feeds.

---

## Step 6: Configure GitHub Repository Variables (Keyless WIF CD)

In your GitHub repository, open **Settings → Secrets and variables → Actions → Variables** (the **Variables** tab — none of these are secrets, and zero static JSON keys are created!) and add these 4 repository variables:

| Variable Name | Value |
|---|---|
| `GCP_PROJECT_ID` | Your GCP Project ID |
| `GCP_REGION` | `us-central1` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Output of `terraform -chdir=infra/terraform output -raw workload_identity_provider` |
| `GCP_DEPLOY_SA_EMAIL` | Output of `terraform -chdir=infra/terraform output -raw github_deploy_sa_email` |

Once configured, every push to `main` automatically authenticates via GitHub OIDC Workload Identity Federation, builds and pushes a new container image to Artifact Registry, and updates `agentic-digest-job`.

---

## Step 7: Trigger & Verify Your First Live Cloud Run Job Execution

Run the job manually and stream completion status:

```powershell
gcloud run jobs execute agentic-digest-job --region us-central1 --wait
```

Inspect structured logs if needed:

```powershell
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="agentic-digest-job"' --limit 50 --format="value(timestamp,severity,textPayload)"
```

Verify in the Google Cloud Console:
1. **Cloud Run → Jobs → `agentic-digest-job`**: Execution succeeded with exit code `0`.
2. **Firestore → Data**: `recipients`, `seen_articles`, and `story_clusters` collections are populated.
3. **Cloud Scheduler**: `daily-agentic-digest` is enabled (`0 12 * * *` UTC).
4. **Billing → Budgets & alerts**: Monthly $10 budget guardrail is active (if `billing_account_id` was provided).

---

## Always Free Guardrails Summary

| Guardrail | Configured Value | Always Free Limit |
|---|---|---|
| Region | `us-central1` | Tier-1 Always Free eligible |
| Cloud Run Job | 1 vCPU, 512 MiB, ~3 min/day | 180k vCPU-sec / 360k GiB-sec per month |
| Artifact Registry Cleanup | Keep 3 recent versions, delete older >24h | 0.5 GiB storage |
| Secret Manager | 1 secret (`agentic-emailer-smtp-password`) | 6 active secret versions |
| Cloud Scheduler | 1 job (`daily-agentic-digest`) | 3 jobs |
| Firestore Retention | Programmatic 30-day pruning in app code | 1 GiB storage, 50k reads / 20k writes / 20k deletes per day |
