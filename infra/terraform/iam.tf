# --- 1. Runtime Service Account (Cloud Run Job execution) ---

resource "google_service_account" "digest_runner" {
  project      = var.project_id
  account_id   = "digest-runner-sa"
  display_name = "Agentic Emailer Digest Runner"
  description  = "Least-privilege runtime identity for the agentic-digest-job Cloud Run Job."

  depends_on = [google_project_service.services["iam.googleapis.com"]]
}

locals {
  digest_runner_project_roles = toset([
    "roles/aiplatform.user",
    "roles/datastore.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
  ])
}

resource "google_project_iam_member" "digest_runner_roles" {
  for_each = local.digest_runner_project_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.digest_runner.email}"
}

# Scoped access to ONLY the SMTP password secret (no project-wide secret access).
resource "google_secret_manager_secret_iam_member" "digest_runner_smtp_secret" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.smtp_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.digest_runner.email}"
}


# --- 2. Scheduler Invoker Service Account (Cloud Scheduler -> Cloud Run Job) ---
# Scoped roles/run.invoker is bound directly on the Cloud Run Job in cloud_run.tf.

resource "google_service_account" "scheduler_invoker" {
  project      = var.project_id
  account_id   = "scheduler-invoker-sa"
  display_name = "Agentic Emailer Scheduler Invoker"
  description  = "Least-privilege identity used by Cloud Scheduler to trigger agentic-digest-job."

  depends_on = [google_project_service.services["iam.googleapis.com"]]
}


# --- 3. GitHub Actions Deploy Service Account (Keyless CD via WIF) ---

resource "google_service_account" "github_deploy" {
  project      = var.project_id
  account_id   = "github-deploy-sa"
  display_name = "Agentic Emailer GitHub Deployer"
  description  = "Keyless CI/CD identity for pushing images to Artifact Registry and updating agentic-digest-job."

  depends_on = [google_project_service.services["iam.googleapis.com"]]
}

# Scoped write access to ONLY the agentic-emailer Artifact Registry repository.
resource "google_artifact_registry_repository_iam_member" "github_deploy_ar_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.docker.location
  repository = google_artifact_registry_repository.docker.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_deploy.email}"
}

resource "google_project_iam_member" "github_deploy_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.github_deploy.email}"
}

# Scoped actAs permission on ONLY digest-runner-sa so Cloud Run Job updates can attach it.
resource "google_service_account_iam_member" "github_deploy_act_as_runner" {
  service_account_id = google_service_account.digest_runner.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deploy.email}"
}


# --- 4. GitHub OIDC Workload Identity Federation (Zero Static Keys) ---

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions-pool"
  display_name              = "GitHub Actions Pool"
  description               = "Workload Identity Pool for keyless GitHub Actions deployments."

  depends_on = [
    google_project_service.services["iam.googleapis.com"],
    google_project_service.services["iamcredentials.googleapis.com"],
    google_project_service.services["sts.googleapis.com"],
  ]
}

resource "google_iam_workload_identity_pool_provider" "github_oidc" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc-provider"
  display_name                       = "GitHub OIDC Provider"
  description                        = "OIDC provider strictly restricted to refs/heads/main on the configured repository."

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.actor"      = "assertion.actor"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # Strict guard: only pushes/dispatches on refs/heads/main of the target repo may authenticate.
  # Pull requests, forks, and non-main branches are unconditionally rejected.
  attribute_condition = "assertion.repository == '${var.github_repo}' && assertion.ref == 'refs/heads/main'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_wif_user" {
  service_account_id = google_service_account.github_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}
