# Cloud Run Job v2 executing `agentic-emailer run` on a serverless container.
# Sized at 1 vCPU / 512 MiB to stay well within the GCP Always Free quota
# (180,000 vCPU-sec and 360,000 GiB-sec per month in Tier-1 regions).
resource "google_cloud_run_v2_job" "digest" {
  project             = var.project_id
  name                = "agentic-digest-job"
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1

    template {
      service_account = google_service_account.digest_runner.email
      max_retries     = 1
      timeout         = "600s"

      containers {
        image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker.repository_id}/agentic-emailer:latest"

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        env {
          name  = "GOOGLE_GENAI_USE_VERTEXAI"
          value = "true"
        }

        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }

        env {
          name  = "GOOGLE_CLOUD_LOCATION"
          value = var.vertex_ai_location
        }

        env {
          name  = "MEMORY_BACKEND"
          value = "firestore"
        }

        env {
          name  = "SMTP_SENDER"
          value = var.smtp_sender
        }

        env {
          name  = "RESEARCH_MODEL"
          value = var.research_model
        }

        env {
          name  = "SYNTHESIS_MODEL"
          value = var.synthesis_model
        }

        env {
          name  = "CRITIC_MODEL"
          value = var.critic_model
        }

        env {
          name  = "OTEL_CLOUD_TRACE_ENABLED"
          value = "true"
        }

        env {
          name  = "LOG_FORMAT"
          value = "json"
        }

        env {
          name = "EMAIL_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.smtp_password.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  # Allow GitHub Actions deploy workflow to update the container image tag without Terraform drift.
  lifecycle {
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }

  depends_on = [
    google_project_service.services["run.googleapis.com"],
    google_secret_manager_secret_iam_member.digest_runner_smtp_secret,
  ]
}

# Scoped invoker permission on ONLY this Cloud Run Job for Cloud Scheduler.
resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_job.digest.location
  name     = google_cloud_run_v2_job.digest.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

# Single Cloud Scheduler job (<= 3 Always Free limit) triggering the Cloud Run Job daily.
resource "google_cloud_scheduler_job" "daily_digest" {
  project          = var.project_id
  region           = var.region
  name             = "daily-agentic-digest"
  description      = "Daily schedule invoking the Agentic Emailer Cloud Run Job."
  schedule         = var.schedule_cron
  time_zone        = var.schedule_timezone
  attempt_deadline = "320s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.digest.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler_invoker.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.services["cloudscheduler.googleapis.com"],
    google_cloud_run_v2_job_iam_member.scheduler_invoker,
  ]
}
