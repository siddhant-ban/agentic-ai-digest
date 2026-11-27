output "workload_identity_provider" {
  description = "Full resource name of the GitHub OIDC Workload Identity Provider (set as GitHub repo variable GCP_WORKLOAD_IDENTITY_PROVIDER)."
  value       = google_iam_workload_identity_pool_provider.github_oidc.name
}

output "github_deploy_sa_email" {
  description = "Email of the GitHub Actions deploy Service Account (set as GitHub repo variable GCP_DEPLOY_SA_EMAIL)."
  value       = google_service_account.github_deploy.email
}

output "artifact_registry_repo" {
  description = "Full Artifact Registry Docker repository path for pushing container images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker.repository_id}"
}

output "cloud_run_job_name" {
  description = "Name of the Cloud Run Job provisioned for daily digest execution."
  value       = google_cloud_run_v2_job.digest.name
}
